import base64
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import server
from metering import CURRENT_TENANT, MeteringStore


class ToolMeteringTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MeteringStore(
            Path(self.tmp.name) / "metering.sqlite3",
            require_request_id=True,
        )
        self.tenant, _ = self.store.create_tenant(
            tenant_id="trial-tool",
            name="Trial Tool",
            plan="trial",
            quota_seconds=120,
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            max_concurrency=1,
        )
        self.original_store = server.METERING_STORE
        server.METERING_STORE = self.store

    def tearDown(self):
        server.METERING_STORE = self.original_store
        self.tmp.cleanup()

    async def test_tool_charges_duration_once_across_replay(self):
        token = CURRENT_TENANT.set(self.tenant)
        payload = base64.b64encode(b"fake-audio").decode()
        try:
            with (
                patch.object(
                    server, "_probe_duration_seconds", AsyncMock(return_value=30.0)
                ),
                patch.object(
                    server, "_post_to_whisper", AsyncMock(return_value={"text": "ok"})
                ),
            ):
                first = await server.transcribe_base64(
                    "a.wav", payload, request_id="req-1"
                )
                replay = await server.transcribe_base64(
                    "a.wav", payload, request_id="req-1"
                )
        finally:
            CURRENT_TENANT.reset(token)

        self.assertEqual(first, "ok")
        self.assertEqual(replay, "ok")
        summary = self.store.usage_summary(self.tenant.id)
        self.assertEqual(summary["used_seconds"], 30)
        self.assertEqual(summary["completed_calls"], 1)

    async def test_tool_rejects_missing_request_id_for_tenant(self):
        token = CURRENT_TENANT.set(self.tenant)
        payload = base64.b64encode(b"fake-audio").decode()
        try:
            with patch.object(
                server, "_probe_duration_seconds", AsyncMock(return_value=30.0)
            ):
                out = await server.transcribe_base64("a.wav", payload)
        finally:
            CURRENT_TENANT.reset(token)
        self.assertIn("request_id is required", out)

    async def test_tool_rejects_quota_before_inference(self):
        limited, _ = self.store.create_tenant(
            tenant_id="limited",
            name="Limited",
            plan="trial",
            quota_seconds=20,
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            max_concurrency=1,
        )
        token = CURRENT_TENANT.set(limited)
        payload = base64.b64encode(b"fake-audio").decode()
        whisper = AsyncMock(return_value={"text": "should-not-run"})
        try:
            with (
                patch.object(
                    server, "_probe_duration_seconds", AsyncMock(return_value=30.0)
                ),
                patch.object(server, "_post_to_whisper", whisper),
            ):
                out = await server.transcribe_base64(
                    "a.wav", payload, request_id="too-big"
                )
        finally:
            CURRENT_TENANT.reset(token)
        self.assertIn("quota exceeded", out)
        whisper.assert_not_awaited()

    async def test_master_context_bypasses_metering(self):
        token = CURRENT_TENANT.set(None)
        payload = base64.b64encode(b"fake-audio").decode()
        try:
            with patch.object(
                server, "_post_to_whisper", AsyncMock(return_value={"text": "ok"})
            ):
                out = await server.transcribe_base64("a.wav", payload)
        finally:
            CURRENT_TENANT.reset(token)
        self.assertEqual(out, "ok")


if __name__ == "__main__":
    unittest.main()
