import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from metering import (
    ConcurrencyExceeded,
    MeteringStore,
    QuotaExceeded,
    RequestIdRequired,
    TrialExpired,
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
