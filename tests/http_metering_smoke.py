import base64
import io
import json
import math
import os
import struct
import subprocess
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx


class FakeWhisperHandler(BaseHTTPRequestHandler):
    post_count = 0

    def log_message(self, _format, *_args):
        return

    def do_POST(self):
        type(self).post_count += 1
        length = int(self.headers.get("content-length", "0"))
        if length:
            self.rfile.read(length)
        body = b'{"text":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _wav_base64(seconds: float = 1.0) -> str:
    frames = int(16000 * seconds)
    bio = io.BytesIO()
    with wave.open(bio, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(
            b"".join(
                struct.pack("<h", int(5000 * math.sin(2 * math.pi * 440 * i / 16000)))
                for i in range(frames)
            )
        )
    return base64.b64encode(bio.getvalue()).decode()


def main() -> None:
    fake_whisper = ThreadingHTTPServer(("127.0.0.1", 18082), FakeWhisperHandler)
    whisper_thread = threading.Thread(target=fake_whisper.serve_forever, daemon=True)
    whisper_thread.start()

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
            "WHISPER_URL": "http://127.0.0.1:18082/v1/audio/transcriptions",
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

        payload = _wav_base64(1.0)
        tool_call = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "transcribe_base64",
                "arguments": {
                    "filename": "smoke.wav",
                    "data_base64": payload,
                    "format": "text",
                    "language": "pt",
                    "request_id": "ci-stable-request-1",
                },
            },
        }
        r = httpx.post(base + "/mcp", headers=mcp_headers, json=tool_call, timeout=15)
        assert r.status_code == 200, r.text
        assert "ok" in r.text, r.text

        r = httpx.get(base + "/usage", headers=headers, timeout=3)
        first_usage = r.json()
        assert 0.9 <= first_usage["used_seconds"] <= 1.1, first_usage
        assert first_usage["completed_calls"] == 1, first_usage

        replay = dict(tool_call)
        replay["id"] = 4
        r = httpx.post(base + "/mcp", headers=mcp_headers, json=replay, timeout=15)
        assert r.status_code == 200, r.text
        assert "inference replay suppressed" in r.text, r.text
        assert FakeWhisperHandler.post_count == 1, FakeWhisperHandler.post_count

        r = httpx.get(base + "/usage", headers=headers, timeout=3)
        replay_usage = r.json()
        assert replay_usage["completed_calls"] == 1, replay_usage
        assert abs(replay_usage["used_seconds"] - first_usage["used_seconds"]) < 0.001, replay_usage
    finally:
        fake_whisper.shutdown()
        fake_whisper.server_close()
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)


if __name__ == "__main__":
    main()
