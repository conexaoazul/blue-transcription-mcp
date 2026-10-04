"""Tenant authentication, quota metering and trial usage accounting.

The module is deliberately dependency-free (stdlib SQLite). It is designed for
one MCP replica / one writable volume. Move the same interface to Postgres
before scaling the public metered endpoint horizontally.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import threading
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


@dataclass(frozen=True)
class Tenant:
    id: str
    name: str
    plan: str
    quota_seconds: float
    expires_at: datetime | None
    max_concurrency: int
    active: bool


@dataclass(frozen=True)
class Reservation:
    event_id: int | None
    tenant_id: str | None
    request_id: str | None
    reserved_seconds: float
    replay: bool = False


CURRENT_TENANT: ContextVar[Tenant | None] = ContextVar("current_tenant", default=None)


class MeteringError(RuntimeError):
    pass


class TenantUnauthorized(MeteringError):
    pass


class TrialExpired(MeteringError):
    pass


class QuotaExceeded(MeteringError):
    pass


class ConcurrencyExceeded(MeteringError):
    pass


class RequestIdRequired(MeteringError):
    pass


class MeteringStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        require_request_id: bool = False,
        lease_seconds: int = 1800,
    ):
        self.db_path = Path(db_path)
        self.require_request_id = require_request_id
        self.lease_seconds = max(60, int(lease_seconds))
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tenants (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    key_hash TEXT NOT NULL UNIQUE,
                    plan TEXT NOT NULL,
                    quota_seconds REAL NOT NULL DEFAULT 0,
                    expires_at TEXT,
                    max_concurrency INTEGER NOT NULL DEFAULT 1,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS usage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL REFERENCES tenants(id),
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_id TEXT,
                    tool TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    reserved_seconds REAL NOT NULL,
                    actual_seconds REAL,
                    status TEXT NOT NULL,
                    lease_expires_at TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_usage_tenant_status
                ON usage_events(tenant_id, status);
                """
            )

    @staticmethod
    def hash_api_key(api_key: str) -> str:
        return hashlib.sha256(api_key.encode("utf-8")).hexdigest()

    def create_tenant(
        self,
        *,
        tenant_id: str,
        name: str,
        plan: str,
        quota_seconds: float,
        expires_at: datetime | None,
        max_concurrency: int = 1,
        api_key: str | None = None,
    ) -> tuple[Tenant, str]:
        clean_id = tenant_id.strip()
        if not clean_id:
            raise ValueError("tenant_id is required")
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        if quota_seconds < 0:
            raise ValueError("quota_seconds must be >= 0")
        api_key = api_key or f"btm_{secrets.token_urlsafe(32)}"
        tenant = Tenant(
            id=clean_id,
            name=name.strip() or clean_id,
            plan=plan.strip() or "custom",
            quota_seconds=float(quota_seconds),
            expires_at=expires_at,
            max_concurrency=int(max_concurrency),
            active=True,
        )
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO tenants
                    (id, name, key_hash, plan, quota_seconds, expires_at,
                     max_concurrency, active, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        tenant.id,
                        tenant.name,
                        self.hash_api_key(api_key),
                        tenant.plan,
                        tenant.quota_seconds,
                        _iso(tenant.expires_at),
                        tenant.max_concurrency,
                        _iso(_utcnow()),
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return tenant, api_key

    def get_tenant_by_api_key(self, api_key: str) -> Tenant:
        key_hash = self.hash_api_key(api_key)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, name, plan, quota_seconds, expires_at,
                       max_concurrency, active
                FROM tenants WHERE key_hash = ?
                """,
                (key_hash,),
            ).fetchone()
        if not row:
            raise TenantUnauthorized("invalid tenant API key")
        expires_at = datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None
        tenant = Tenant(
            id=row["id"],
            name=row["name"],
            plan=row["plan"],
            quota_seconds=float(row["quota_seconds"]),
            expires_at=expires_at,
            max_concurrency=int(row["max_concurrency"]),
            active=bool(row["active"]),
        )
        self._validate_tenant(tenant)
        return tenant

    @staticmethod
    def _validate_tenant(tenant: Tenant) -> None:
        if not tenant.active:
            raise TenantUnauthorized("tenant disabled")
        if tenant.expires_at and _utcnow() >= tenant.expires_at:
            raise TrialExpired("tenant access expired")

    def _cleanup_stale(self, conn: sqlite3.Connection, now: datetime) -> None:
        conn.execute(
            """
            UPDATE usage_events
            SET status='failed', completed_at=?, lease_expires_at=NULL
            WHERE status='reserved' AND lease_expires_at IS NOT NULL
              AND lease_expires_at <= ?
            """,
            (_iso(now), _iso(now)),
        )

    def reserve(
        self,
        tenant: Tenant,
        *,
        tool: str,
        source: str,
        seconds: float,
        request_id: str | None,
    ) -> Reservation:
        self._validate_tenant(tenant)
        seconds = max(0.001, float(seconds))
        request_id = (request_id or "").strip() or None
        if self.require_request_id and not request_id:
            raise RequestIdRequired("request_id is required for metered tenants")
        if not request_id:
            request_id = secrets.token_urlsafe(18)

        source_hash = hashlib.sha256(source.encode("utf-8", "replace")).hexdigest()
        idem = hashlib.sha256(
            f"{tenant.id}\0{tool}\0{request_id}".encode("utf-8")
        ).hexdigest()

        now = _utcnow()
        lease_expires = now + timedelta(seconds=self.lease_seconds)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._cleanup_stale(conn, now)
                existing = conn.execute(
                    """
                    SELECT id, tenant_id, request_id, reserved_seconds, status,
                           source_hash
                    FROM usage_events WHERE idempotency_key = ?
                    """,
                    (idem,),
                ).fetchone()
                if existing:
                    if existing["source_hash"] != source_hash:
                        raise MeteringError(
                            "request_id was already used for a different source"
                        )
                    conn.commit()
                    return Reservation(
                        event_id=int(existing["id"]),
                        tenant_id=existing["tenant_id"],
                        request_id=existing["request_id"],
                        reserved_seconds=float(existing["reserved_seconds"]),
                        replay=True,
                    )

                active = conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM usage_events
                    WHERE tenant_id=? AND status='reserved'
                    """,
                    (tenant.id,),
                ).fetchone()["n"]
                if int(active) >= tenant.max_concurrency:
                    raise ConcurrencyExceeded(
                        f"tenant concurrency limit is {tenant.max_concurrency}"
                    )

                used = conn.execute(
                    """
                    SELECT COALESCE(SUM(
                      CASE
                        WHEN status='completed' THEN COALESCE(actual_seconds, reserved_seconds)
                        WHEN status='reserved' THEN reserved_seconds
                        ELSE 0
                      END
                    ),0) AS seconds
                    FROM usage_events WHERE tenant_id=?
                    """,
                    (tenant.id,),
                ).fetchone()["seconds"]
                projected = float(used) + seconds
                if tenant.quota_seconds > 0 and projected > tenant.quota_seconds:
                    remaining = max(0.0, tenant.quota_seconds - float(used))
                    raise QuotaExceeded(
                        f"quota exceeded; remaining_seconds={remaining:.3f}"
                    )

                cur = conn.execute(
                    """
                    INSERT INTO usage_events
                    (tenant_id, idempotency_key, request_id, tool, source_hash,
                     reserved_seconds, status, lease_expires_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?, ?)
                    """,
                    (
                        tenant.id,
                        idem,
                        request_id,
                        tool,
                        source_hash,
                        seconds,
                        _iso(lease_expires),
                        _iso(now),
                    ),
                )
                event_id = int(cur.lastrowid)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        return Reservation(
            event_id=event_id,
            tenant_id=tenant.id,
            request_id=request_id,
            reserved_seconds=seconds,
            replay=False,
        )

    def complete(self, reservation: Reservation, actual_seconds: float | None = None) -> None:
        if reservation.event_id is None or reservation.replay:
            return
        actual = reservation.reserved_seconds if actual_seconds is None else max(0.0, float(actual_seconds))
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE usage_events
                SET status='completed', actual_seconds=?, completed_at=?,
                    lease_expires_at=NULL
                WHERE id=? AND status='reserved'
                """,
                (actual, _iso(_utcnow()), reservation.event_id),
            )

    def fail(self, reservation: Reservation) -> None:
        if reservation.event_id is None or reservation.replay:
            return
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE usage_events
                SET status='failed', actual_seconds=0, completed_at=?,
                    lease_expires_at=NULL
                WHERE id=? AND status='reserved'
                """,
                (_iso(_utcnow()), reservation.event_id),
            )

    def usage_summary(self, tenant_id: str) -> dict:
        with self._connect() as conn:
            tenant = conn.execute(
                """
                SELECT id, name, plan, quota_seconds, expires_at,
                       max_concurrency, active
                FROM tenants WHERE id=?
                """,
                (tenant_id,),
            ).fetchone()
            if not tenant:
                raise KeyError(tenant_id)
            usage = conn.execute(
                """
                SELECT
                  COALESCE(SUM(CASE WHEN status='completed'
                    THEN COALESCE(actual_seconds,reserved_seconds) ELSE 0 END),0) AS used,
                  COALESCE(SUM(CASE WHEN status='reserved'
                    THEN reserved_seconds ELSE 0 END),0) AS reserved,
                  SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed_calls,
                  SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed_calls
                FROM usage_events WHERE tenant_id=?
                """,
                (tenant_id,),
            ).fetchone()
        quota = float(tenant["quota_seconds"])
        used = float(usage["used"])
        reserved = float(usage["reserved"])
        return {
            "tenant_id": tenant_id,
            "name": tenant["name"],
            "plan": tenant["plan"],
            "active": bool(tenant["active"]),
            "expires_at": tenant["expires_at"],
            "max_concurrency": int(tenant["max_concurrency"]),
            "quota_seconds": quota,
            "used_seconds": used,
            "reserved_seconds": reserved,
            "remaining_seconds": None if quota <= 0 else max(0.0, quota - used - reserved),
            "completed_calls": int(usage["completed_calls"] or 0),
            "failed_calls": int(usage["failed_calls"] or 0),
        }


def store_from_env() -> MeteringStore | None:
    if os.environ.get("METERING_ENABLED", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return None
    path = os.environ.get("METERING_DB", "/data/metering/metering.sqlite3")
    require_id = os.environ.get("METERING_REQUIRE_REQUEST_ID", "1").strip().lower() in {
        "1", "true", "yes", "on"
    }
    lease = int(os.environ.get("METERING_LEASE_SECONDS", "1800"))
    return MeteringStore(path, require_request_id=require_id, lease_seconds=lease)
