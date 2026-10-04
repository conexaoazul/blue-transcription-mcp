import json
import os
import subprocess
import sys
import time

import httpx


def main() -> None:
    env = os.environ.copy()
    env.update(
        {
            "OUTPUT_DIR": "/tmp/out",
            "METERING_ENABLED": "1",
            "METERING_DB": "/tmp/metering.sqlite3",
            "METERING_REQUIRE_REQUEST_ID": "1",
            "MCP_AUTH_TOKEN": "master-smoke-token",
            "TRANSPORT": "http",
            "HOST": "127.0.0.1",
            "PORT": "8083",
        }
    )
    os.makedirs(env["OUTPUT_DIR"], exist_ok=True)

    tenant_raw = subprocess.check_output(
        [
            sys.executable,
            "/app/tenant_admin.py",
            "create",
            "--id",
            "ci-trial",
            "--name",
            "CI Trial",
            "--plan",
            "trial",
            "--quota-minutes",
            "120",
            "--days",
            "7",
            "--max-concurrency",
            "1",
        ],
        env=env,
        text=True,
    )
    tenant = json.loads(tenant_raw)
    key = tenant["api_key"]

    server = subprocess.Popen(
        [sys.executable, "/app/server.py"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        base = "http://127.0.0.1:8083"
        for _ in range(80):
            if server.poll() is not None:
                output = server.stdout.read() if server.stdout else ""
                raise RuntimeError(f"server exited early: {output}")
            try:
                r = httpx.get(base + "/healthz", timeout=1)
                if r.status_code == 200:
                    break
            except Exception:
                time.sleep(0.2)
        else:
            raise RuntimeError("server did not become healthy")

        health = r.json()
        assert health["metering_enabled"] is True, health

        r = httpx.get(base + "/usage", timeout=3)
        assert r.status_code == 401, r.text

        headers = {"Authorization": "Bearer " + key}
        r = httpx.get(base + "/usage", headers=headers, timeout=3)
        assert r.status_code == 200, r.text
        usage = r.json()
        assert usage["tenant_id"] == "ci-trial", usage
        assert usage["quota_seconds"] == 7200, usage
        assert usage["used_seconds"] == 0, usage

        mcp_headers = {
            **headers,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "ci-metering-smoke", "version": "1"},
            },
        }
        r = httpx.post(base + "/mcp", headers=mcp_headers, json=init, timeout=5)
        assert r.status_code == 200, r.text
        session_id = r.headers.get("mcp-session-id")
        assert session_id, r.headers

        mcp_headers["Mcp-Session-Id"] = session_id
        r = httpx.post(
            base + "/mcp",
            headers=mcp_headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            timeout=5,
        )
        assert r.status_code in {200, 202}, r.text

        r = httpx.post(
            base + "/mcp",
            headers=mcp_headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            timeout=5,
        )
        assert r.status_code == 200, r.text
        assert "transcribe_zip_base64" in r.text, r.text
        assert "request_id" in r.text, r.text
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)


if __name__ == "__main__":
    main()
