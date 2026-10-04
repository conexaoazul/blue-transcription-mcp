"""Admin CLI for Blue Transcription MCP tenant metering.

Examples:
  python tenant_admin.py create --id acme-trial --name "ACME" --plan trial \
    --quota-minutes 120 --days 7 --max-concurrency 1

  python tenant_admin.py usage --id acme-trial
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

    usage = sub.add_parser("usage")
    usage.add_argument("--id", required=True)

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
        )
        # The key is printed once. The DB stores only its SHA-256 digest.
        print(json.dumps({
            "tenant_id": tenant.id,
            "plan": tenant.plan,
            "quota_seconds": tenant.quota_seconds,
            "expires_at": tenant.expires_at.isoformat() if tenant.expires_at else None,
            "max_concurrency": tenant.max_concurrency,
            "api_key": api_key,
        }, indent=2))
        return

    print(json.dumps(store.usage_summary(args.id), indent=2))


if __name__ == "__main__":
    main()
