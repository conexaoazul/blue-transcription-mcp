import asyncio
import base64
import io
import json
import wave
import zipfile
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def build_fixture() -> str:
    wav = io.BytesIO()
    with wave.open(wav, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b"\x00\x00" * 8000)

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("voice/sample.wav", wav.getvalue())
        zf.writestr("ignore.txt", "not media")
    return base64.b64encode(archive.getvalue()).decode()


def text_content(result) -> str:
    return "".join(getattr(part, "text", "") for part in result.content)


async def main() -> None:
    token = Path("/run/secrets/transcription_mcp_auth").read_text().strip()
    headers = {"Authorization": f"Bearer {token}"}

    async with streamable_http_client(
        "http://mcp-canary:8083/mcp", headers=headers
    ) as streams:
        read, write, *_ = streams
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = [tool.name for tool in tools.tools]
            assert "transcribe_zip_base64" in names, names

            rejected = await session.call_tool(
                "transcribe_zip_base64",
                arguments={
                    "filename": "bad.zip",
                    "data_base64": "%%%",
                    "format": "text",
                    "language": "pt",
                },
            )
            assert text_content(rejected).startswith("Rejected:"), rejected

            result = await session.call_tool(
                "transcribe_zip_base64",
                arguments={
                    "filename": "canary-smoke.zip",
                    "data_base64": build_fixture(),
                    "format": "text",
                    "language": "pt",
                    "concurrency": 1,
                },
            )
            data = json.loads(text_content(result))

    assert data["count"] == 1, data
    assert data["ok"] == 1, data
    assert data["failed"] == 0, data
    assert data["archive_kind"] == "inline_base64_zip", data
    assert len(data["archive_sha256"]) == 64, data
    assert len(data["members"]) == 1, data
    assert len(data["members"][0]["sha256"]) == 64, data
    assert data["results"][0]["status"] == "ok", data

    print(
        "MCP_CANARY_SMOKE_OK "
        + json.dumps(
            {
                "tools_count": len(names),
                "has_zip_base64": True,
                "invalid_base64_rejected": True,
                "count": data["count"],
                "ok": data["ok"],
                "failed": data["failed"],
                "archive_kind": data["archive_kind"],
                "archive_sha256": data["archive_sha256"],
                "member": data["members"][0]["safe_name"],
                "member_bytes": data["members"][0]["bytes"],
                "member_sha256": data["members"][0]["sha256"],
            },
            separators=(",", ":"),
        )
    )


asyncio.run(main())
