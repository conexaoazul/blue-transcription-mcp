from __future__ import annotations

import base64
import io
import json
import math
import os
import struct
import subprocess
import time
import wave
from datetime import datetime, timezone

import httpx

MCP_URL = os.environ.get("MCP_URL", "http://mcp-metered-canary:8083/mcp")
USAGE_URL = os.environ.get("USAGE_URL", "http://mcp-metered-canary:8083/usage")


def admin(*args: str):
    output = subprocess.check_output(
        ["python", "/app/tenant_admin.py", *args],
        text=True,
    )
    return json.loads(output)


def wav_base64(seconds: int, hz: int = 440) -> str:
    bio = io.BytesIO()
    with wave.open(bio, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        frames = (
            struct.pack(
                "<h",
                int(4000 * math.sin(2 * math.pi * hz * i / 16000)),
            )
            for i in range(16000 * seconds)
        )
        out.writeframes(b"".join(frames))
    return base64.b64encode(bio.getvalue()).decode()


def mcp_headers(api_key: str) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
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
            "clientInfo": {"name": "oci-trial-canary", "version": "1"},
        },
    }
    response = httpx.post(MCP_URL, headers=headers, json=init, timeout=20)
    response.raise_for_status()
    session_id = response.headers.get("mcp-session-id")
    if not session_id:
        raise AssertionError("missing MCP session id")
    headers["Mcp-Session-Id"] = session_id
    httpx.post(
        MCP_URL,
        headers=headers,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        timeout=20,
    )
    return headers


def call_tool(
    headers: dict[str, str],
    call_id: int,
    request_id: str,
    seconds: int,
    *,
    prompt: str | None = None,
    carry_initial_prompt: bool = False,
):
    payload = {
        "jsonrpc": "2.0",
        "id": call_id,
        "method": "tools/call",
        "params": {
            "name": "transcribe_base64",
            "arguments": {
                "filename": f"{request_id}.wav",
                "data_base64": wav_base64(seconds),
                "format": "text",
                "language": "pt",
                "request_id": request_id,
                "prompt": prompt,
                "carry_initial_prompt": carry_initial_prompt,
            },
        },
    }
    response = httpx.post(MCP_URL, headers=headers, json=payload, timeout=180)
    response.raise_for_status()
    return response.text


def usage(api_key: str):
    return httpx.get(
        USAGE_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=10,
    )


def main() -> None:
    suffix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    tenant_id = f"oci-trial-canary-{suffix}"

    created = admin(
        "create",
        "--id", tenant_id,
        "--name", "OCI Trial Canary",
        "--plan", "trial",
        "--quota-minutes", "120",
        "--days", "7",
        "--max-concurrency", "1",
        "--max-calls-per-hour", "2",
        "--max-calls-total", "2",
        "--max-sync-minutes", "1",
    )
    key1 = created["api_key"]

    initial = usage(key1)
    initial.raise_for_status()
    initial_usage = initial.json()
    assert initial_usage["quota_seconds"] == 7200.0, initial_usage
    assert initial_usage["max_concurrency"] == 1, initial_usage
    assert initial_usage["max_calls_per_hour"] == 2, initial_usage
    assert initial_usage["max_calls_total"] == 2, initial_usage
    assert initial_usage["max_sync_seconds"] == 60.0, initial_usage

    headers1 = mcp_headers(key1)
    started = time.time()
    first = call_tool(
        headers1,
        2,
        "oci-canary-1",
        1,
        prompt="Conexão Azul, Odoo, n8n, RENAINF, Nettcom",
        carry_initial_prompt=True,
    )
    first_elapsed = time.time() - started
    assert '"isError":true' not in first.replace(" ", ""), first[:800]

    after_first = usage(key1).json()
    assert 0.9 <= after_first["used_seconds"] <= 1.1, after_first
    assert after_first["completed_calls"] == 1, after_first

    rotated = admin("rotate-key", "--id", tenant_id)
    key2 = rotated["api_key"]
    assert key2 != key1

    old_key = usage(key1)
    assert old_key.status_code == 401, old_key.text
    new_key = usage(key2)
    new_key.raise_for_status()

    admin("suspend", "--id", tenant_id)
    suspended = usage(key2)
    assert suspended.status_code == 401, suspended.text
    admin("activate", "--id", tenant_id)
    usage(key2).raise_for_status()

    headers2 = mcp_headers(key2)

    replay = call_tool(headers2, 3, "oci-canary-1", 1)
    assert "inference replay suppressed" in replay, replay[:1200]
    replay_usage = usage(key2).json()
    assert replay_usage["completed_calls"] == 1, replay_usage
    assert 0.9 <= replay_usage["used_seconds"] <= 1.1, replay_usage

    too_long = call_tool(headers2, 4, "oci-canary-long", 61)
    assert "synchronous media duration limit" in too_long, too_long[:1200]
    after_long = usage(key2).json()
    assert after_long["calls_total"] == 1, after_long

    second = call_tool(headers2, 5, "oci-canary-2", 1)
    assert '"isError":true' not in second.replace(" ", ""), second[:800]

    third = call_tool(headers2, 6, "oci-canary-3", 1)
    assert (
        "hourly call limit is 2" in third
        or "total call limit is 2" in third
    ), third[:1200]

    before_upgrade = usage(key2).json()
    assert before_upgrade["calls_total"] == 2, before_upgrade
    assert before_upgrade["completed_calls"] == 2, before_upgrade
    assert 1.8 <= before_upgrade["used_seconds"] <= 2.2, before_upgrade

    admin(
        "update",
        "--id", tenant_id,
        "--plan", "pro",
        "--quota-minutes", "600",
        "--days", "0",
        "--max-concurrency", "3",
        "--max-calls-per-hour", "120",
        "--max-calls-total", "1000",
        "--max-sync-minutes", "0",
    )
    final_usage = usage(key2).json()
    assert final_usage["plan"] == "pro", final_usage
    assert final_usage["quota_seconds"] == 36000.0, final_usage
    assert final_usage["max_concurrency"] == 3, final_usage
    assert final_usage["max_sync_seconds"] == 0.0, final_usage
    assert 1.8 <= final_usage["used_seconds"] <= 2.2, final_usage

    events = admin("events", "--id", tenant_id, "--limit", "30")
    serialized = json.dumps(events)
    assert key1 not in serialized and key2 not in serialized
    event_types = {event["event_type"] for event in events}
    required = {
        "tenant_created",
        "api_key_rotated",
        "tenant_suspended",
        "tenant_activated",
        "tenant_updated",
    }
    assert required <= event_types, event_types

    print(json.dumps({
        "status": "OCI_TRIAL_CANARY_OK",
        "tenant_id": tenant_id,
        "first_inference_seconds": round(first_elapsed, 2),
        "used_seconds_before_upgrade": round(before_upgrade["used_seconds"], 3),
        "calls_total_before_upgrade": before_upgrade["calls_total"],
        "old_key_revoked": True,
        "suspend_blocks_auth": True,
        "replay_no_double_charge": True,
        "replay_no_duplicate_inference": True,
        "prompt_request_accepted": True,
        "sync_ceiling_blocks_before_usage": True,
        "call_caps_block_third_new_request": True,
        "upgrade_preserves_usage": True,
        "audit_contains_plaintext_key": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
