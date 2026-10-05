import unittest

import public_server
from starlette.testclient import TestClient


class PublicFacadeClosedTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(public_server.app)

    def test_health_reports_closed_mode(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["mode"], "trial-closed")
        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["tools"], [])

    def test_mcp_is_blocked_while_trial_is_closed(self):
        response = self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "trial_closed")


if __name__ == "__main__":
    unittest.main()
