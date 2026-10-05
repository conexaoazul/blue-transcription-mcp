"""Admin CLI for Blue Transcription MCP tenant metering.

Examples:
  python tenant_admin.py create --id acme-trial --name "ACME" --plan trial \
    --quota-minutes 120 --days 7 --max-concurrency 1

  python tenant_admin.py usage --id acme-trial
  python tenant_admin.py rotate-key --id acme-trial
  python tenant_admin.py update --id acme-trial --plan pro --quota-minutes 600
  python tenant_admin.py suspend --id acme-trial
  python tenant_admin.py activate --id acme-trial
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone

from metering import MeteringStore


def _store() -> MeteringStore:
    return MeteringStore(
        os.environ.get("METERING_DB", "/data/metering/metering.sqlite3"),
        require_request_id=os.environ.get("METERING_REQUIRE_REQUEST_ID", "1").lower()
        in {"1", "true", "yes", "on"},
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create")
    create.add_argument("--id", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--plan", default="trial")
    create.add_argument("--quota-minutes", type=float, default=120)
    create.add_argument("--days", type=int, default=7)
    create.add_argument("--max-concurrency", type=int, default=1)
    create.add_argument("--max-calls-per-hour", type=int, default=0)

    usage = sub.add_parser("usage")
    usage.add_argument("--id", required=True)

    rotate = sub.add_parser("rotate-key")
    rotate.add_argument("--id", required=True)

    update = sub.add_parser("update")
    update.add_argument("--id", required=True)
    update.add_argument("--name")
    update.add_argument("--plan")
    update.add_argument("--quota-minutes", type=float)
    update.add_argument("--days", type=int)
    update.add_argument("--max-concurrency", type=int)
    update.add_argument("--max-calls-per-hour", type=int)

    suspend = sub.add_parser("suspend")
    suspend.add_argument("--id", required=True)

    activate = sub.add_parser("activate")
    activate.add_argument("--id", required=True)

    events = sub.add_parser("events")
    events.add_argument("--id", required=True)
    events.add_argument("--limit", type=int, default=50)

    args = parser.parse_args()
    store = _store()

    if args.command == "create":
        expires = (
            datetime.now(timezone.utc) + timedelta(days=args.days)
            if args.days > 0
            else None
        )
        tenant, api_key = store.create_tenant(
            tenant_id=args.id,
            name=args.name,
            plan=args.plan,
            quota_seconds=args.quota_minutes * 60,
            expires_at=expires,
            max_concurrency=args.max_concurrency,
            max_calls_per_hour=args.max_calls_per_hour,
        )
        # The key is printed once. The DB stores only its SHA-256 digest.
        print(json.dumps({
            "tenant_id": tenant.id,
            "plan": tenant.plan,
            "quota_seconds": tenant.quota_seconds,
            "expires_at": tenant.expires_at.isoformat() if tenant.expires_at else None,
            "max_concurrency": tenant.max_concurrency,
            "max_calls_per_hour": tenant.max_calls_per_hour,
            "api_key": api_key,
        }, indent=2))
        return

    if args.command == "usage":
        print(json.dumps(store.usage_summary(args.id), indent=2))
        return

    if args.command == "rotate-key":
        api_key = store.rotate_api_key(args.id)
        # Printed once; only the SHA-256 digest is stored in the ledger.
        print(json.dumps({"tenant_id": args.id, "api_key": api_key}, indent=2))
        return

    if args.command == "update":
        expires_at = None
        set_expires_at = args.days is not None
        if args.days is not None and args.days > 0:
            expires_at = datetime.now(timezone.utc) + timedelta(days=args.days)
        tenant = store.update_tenant(
            args.id,
            name=args.name,
            plan=args.plan,
            quota_seconds=None if args.quota_minutes is None else args.quota_minutes * 60,
            expires_at=expires_at,
            set_expires_at=set_expires_at,
            max_concurrency=args.max_concurrency,
            max_calls_per_hour=args.max_calls_per_hour,
        )
        print(json.dumps({
            "tenant_id": tenant.id,
            "name": tenant.name,
            "plan": tenant.plan,
            "quota_seconds": tenant.quota_seconds,
            "expires_at": tenant.expires_at.isoformat() if tenant.expires_at else None,
            "max_concurrency": tenant.max_concurrency,
            "max_calls_per_hour": tenant.max_calls_per_hour,
            "active": tenant.active,
        }, indent=2))
        return

    if args.command in {"suspend", "activate"}:
        tenant = store.set_active(args.id, args.command == "activate")
        print(json.dumps({
            "tenant_id": tenant.id,
            "active": tenant.active,
            "plan": tenant.plan,
        }, indent=2))
        return

    if args.command == "events":
        print(json.dumps(store.lifecycle_events(args.id, args.limit), indent=2))
        return


if __name__ == "__main__":
    main()
