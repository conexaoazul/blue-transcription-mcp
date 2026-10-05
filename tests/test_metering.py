import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from metering import (
    CallLimitExceeded,
    ConcurrencyExceeded,
    MeteringStore,
    QuotaExceeded,
    RateLimitExceeded,
    RequestIdRequired,
    SyncDurationExceeded,
    TrialExpired,
    TenantUnauthorized,
)


class MeteringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MeteringStore(
            Path(self.tmp.name) / "metering.sqlite3",
            require_request_id=True,
            lease_seconds=120,
        )
        self.tenant, self.api_key = self.store.create_tenant(
            tenant_id="trial-1",
            name="Trial One",
            plan="trial",
            quota_seconds=120,
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            max_concurrency=1,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_api_key_is_hashed_and_authenticates(self):
        loaded = self.store.get_tenant_by_api_key(self.api_key)
        self.assertEqual(loaded.id, "trial-1")
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT key_hash FROM tenants WHERE id='trial-1'"
            ).fetchone()
        self.assertNotEqual(row["key_hash"], self.api_key)
        self.assertEqual(row["key_hash"], self.store.hash_api_key(self.api_key))

    def test_request_id_required(self):
        with self.assertRaises(RequestIdRequired):
            self.store.reserve(
                self.tenant,
                tool="transcribe_base64",
                source="audio.wav",
                seconds=10,
                request_id=None,
            )

    def test_replay_does_not_double_charge(self):
        first = self.store.reserve(
            self.tenant,
            tool="transcribe_base64",
            source="audio.wav",
            seconds=30,
            request_id="req-1",
        )
        self.store.complete(first, 30)

        replay = self.store.reserve(
            self.tenant,
            tool="transcribe_base64",
            source="audio.wav",
            seconds=30,
            request_id="req-1",
        )
        self.assertTrue(replay.replay)
        self.store.complete(replay, 30)

        summary = self.store.usage_summary(self.tenant.id)
        self.assertEqual(summary["used_seconds"], 30)
        self.assertEqual(summary["completed_calls"], 1)

    def test_total_call_limit_blocks_new_request_but_not_replay(self):
        limited, _ = self.store.create_tenant(
            tenant_id="total-limited",
            name="Total Limited",
            plan="trial",
            quota_seconds=3600,
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            max_concurrency=1,
            max_calls_total=2,
        )
        first = self.store.reserve(
            limited,
            tool="transcribe_base64",
            source="a.wav",
            seconds=10,
            request_id="total-1",
        )
        self.store.complete(first, 10)
        second = self.store.reserve(
            limited,
            tool="transcribe_base64",
            source="b.wav",
            seconds=10,
            request_id="total-2",
        )
        self.store.complete(second, 10)

        replay = self.store.reserve(
            limited,
            tool="transcribe_base64",
            source="a.wav",
            seconds=10,
            request_id="total-1",
        )
        self.assertTrue(replay.replay)

        with self.assertRaises(CallLimitExceeded):
            self.store.reserve(
                limited,
                tool="transcribe_base64",
                source="c.wav",
                seconds=10,
                request_id="total-3",
            )

        summary = self.store.usage_summary(limited.id)
        self.assertEqual(summary["max_calls_total"], 2)
        self.assertEqual(summary["calls_total"], 2)
        self.assertEqual(summary["completed_calls"], 2)

    def test_hourly_call_limit_blocks_new_request_but_not_replay(self):
        limited, _ = self.store.create_tenant(
            tenant_id="rate-limited",
            name="Rate Limited",
            plan="trial",
            quota_seconds=3600,
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            max_concurrency=1,
            max_calls_per_hour=2,
        )
        first = self.store.reserve(
            limited,
            tool="transcribe_base64",
            source="a.wav",
            seconds=10,
            request_id="rate-1",
        )
        self.store.complete(first, 10)
        second = self.store.reserve(
            limited,
            tool="transcribe_base64",
            source="b.wav",
            seconds=10,
            request_id="rate-2",
        )
        self.store.complete(second, 10)

        replay = self.store.reserve(
            limited,
            tool="transcribe_base64",
            source="a.wav",
            seconds=10,
            request_id="rate-1",
        )
        self.assertTrue(replay.replay)

        with self.assertRaises(RateLimitExceeded):
            self.store.reserve(
                limited,
                tool="transcribe_base64",
                source="c.wav",
                seconds=10,
                request_id="rate-3",
            )

        summary = self.store.usage_summary(limited.id)
        self.assertEqual(summary["max_calls_per_hour"], 2)
        self.assertEqual(summary["calls_last_hour"], 2)
        self.assertEqual(summary["completed_calls"], 2)

    def test_existing_schema_is_migrated_with_rate_limit_column(self):
        legacy_path = Path(self.tmp.name) / "legacy.sqlite3"
        import sqlite3
        conn = sqlite3.connect(legacy_path)
        conn.executescript(
            """
            CREATE TABLE tenants (
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
            """
        )
        conn.close()
        migrated = MeteringStore(legacy_path, require_request_id=True)
        with migrated._connect() as check:
            columns = {
                row["name"]
                for row in check.execute("PRAGMA table_info(tenants)").fetchall()
            }
        self.assertIn("max_calls_per_hour", columns)
        self.assertIn("max_calls_total", columns)
        self.assertIn("max_sync_seconds", columns)

    def test_sync_duration_limit_blocks_new_request_but_not_replay(self):
        limited, _ = self.store.create_tenant(
            tenant_id="sync-limited",
            name="Sync Limited",
            plan="trial",
            quota_seconds=3600,
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            max_concurrency=1,
            max_sync_seconds=30,
        )
        first = self.store.reserve(
            limited,
            tool="transcribe_base64",
            source="a.wav",
            seconds=30,
            request_id="sync-1",
        )
        self.store.complete(first, 30)

        replay = self.store.reserve(
            limited,
            tool="transcribe_base64",
            source="a.wav",
            seconds=120,
            request_id="sync-1",
        )
        self.assertTrue(replay.replay)

        with self.assertRaises(SyncDurationExceeded):
            self.store.reserve(
                limited,
                tool="transcribe_base64",
                source="b.wav",
                seconds=30.001,
                request_id="sync-2",
            )

        summary = self.store.usage_summary(limited.id)
        self.assertEqual(summary["max_sync_seconds"], 30)
        self.assertEqual(summary["completed_calls"], 1)

    def test_quota_blocks_projected_usage(self):
        first = self.store.reserve(
            self.tenant,
            tool="transcribe_base64",
            source="a.wav",
            seconds=100,
            request_id="a",
        )
        self.store.complete(first, 100)
        with self.assertRaises(QuotaExceeded):
            self.store.reserve(
                self.tenant,
                tool="transcribe_base64",
                source="b.wav",
                seconds=21,
                request_id="b",
            )

    def test_concurrency_blocks_second_active_reservation(self):
        self.store.reserve(
            self.tenant,
            tool="transcribe_base64",
            source="a.wav",
            seconds=10,
            request_id="a",
        )
        with self.assertRaises(ConcurrencyExceeded):
            self.store.reserve(
                self.tenant,
                tool="transcribe_base64",
                source="b.wav",
                seconds=10,
                request_id="b",
            )

    def test_failed_reservation_frees_quota_and_concurrency(self):
        first = self.store.reserve(
            self.tenant,
            tool="transcribe_base64",
            source="a.wav",
            seconds=100,
            request_id="a",
        )
        self.store.fail(first)
        second = self.store.reserve(
            self.tenant,
            tool="transcribe_base64",
            source="b.wav",
            seconds=120,
            request_id="b",
        )
        self.store.complete(second, 120)
        summary = self.store.usage_summary(self.tenant.id)
        self.assertEqual(summary["used_seconds"], 120)
        self.assertEqual(summary["failed_calls"], 1)

    def test_rotate_key_revokes_old_key(self):
        old_key = self.api_key
        new_key = self.store.rotate_api_key(self.tenant.id)
        self.assertNotEqual(old_key, new_key)
        with self.assertRaises(TenantUnauthorized):
            self.store.get_tenant_by_api_key(old_key)
        loaded = self.store.get_tenant_by_api_key(new_key)
        self.assertEqual(loaded.id, self.tenant.id)

    def test_suspend_and_activate_control_auth(self):
        self.store.set_active(self.tenant.id, False)
        with self.assertRaises(TenantUnauthorized):
            self.store.get_tenant_by_api_key(self.api_key)
        restored = self.store.set_active(self.tenant.id, True)
        self.assertTrue(restored.active)
        self.assertEqual(
            self.store.get_tenant_by_api_key(self.api_key).id,
            self.tenant.id,
        )

    def test_upgrade_preserves_usage_history(self):
        reservation = self.store.reserve(
            self.tenant,
            tool="transcribe_base64",
            source="a.wav",
            seconds=30,
            request_id="before-upgrade",
        )
        self.store.complete(reservation, 30)
        upgraded = self.store.update_tenant(
            self.tenant.id,
            plan="pro",
            quota_seconds=600,
            max_concurrency=3,
            max_calls_per_hour=120,
            expires_at=None,
            set_expires_at=True,
        )
        self.assertEqual(upgraded.plan, "pro")
        self.assertEqual(upgraded.quota_seconds, 600)
        self.assertEqual(upgraded.max_concurrency, 3)
        self.assertEqual(upgraded.max_calls_per_hour, 120)
        self.assertIsNone(upgraded.expires_at)
        summary = self.store.usage_summary(self.tenant.id)
        self.assertEqual(summary["used_seconds"], 30)
        self.assertEqual(summary["plan"], "pro")
        self.assertEqual(summary["max_concurrency"], 3)

    def test_lifecycle_events_never_store_plaintext_key(self):
        new_key = self.store.rotate_api_key(self.tenant.id)
        self.store.update_tenant(self.tenant.id, plan="cloud")
        self.store.set_active(self.tenant.id, False)
        events = self.store.lifecycle_events(self.tenant.id)
        serialized = repr(events)
        self.assertNotIn(self.api_key, serialized)
        self.assertNotIn(new_key, serialized)
        event_types = {event["event_type"] for event in events}
        self.assertTrue(
            {"tenant_created", "api_key_rotated", "tenant_updated", "tenant_suspended"}
            <= event_types
        )

    def test_expired_tenant_is_rejected(self):
        expired, key = self.store.create_tenant(
            tenant_id="expired",
            name="Expired",
            plan="trial",
            quota_seconds=120,
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            max_concurrency=1,
        )
        with self.assertRaises(TrialExpired):
            self.store.get_tenant_by_api_key(key)


if __name__ == "__main__":
    unittest.main()
