import asyncio
import base64
import io
import json
import sys
import wave
import zipfile

sys.path.insert(0, "/app")
import server


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


async def main() -> None:
    raw = await server.transcribe_zip_base64(
        "canary-smoke.zip",
        build_fixture(),
        format="text",
        language="pt",
        concurrency=1,
    )
    data = json.loads(raw)
    assert data["count"] == 1, data
    assert data["ok"] == 1, data
    assert data["failed"] == 0, data
    assert data["archive_kind"] == "inline_base64_zip", data
    assert len(data["archive_sha256"]) == 64, data
    assert len(data["members"]) == 1, data
    assert len(data["members"][0]["sha256"]) == 64, data
    assert data["results"][0]["status"] == "ok", data
    print(
        "CANARY_SMOKE_OK "
        + json.dumps(
            {
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
