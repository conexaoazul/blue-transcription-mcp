"""Authless, stateless MCP facade for hosted web clients.

Only accepts media bytes supplied by the caller. It deliberately does NOT expose
local-file, URL, YouTube, podcast, or other data-retrieval tools.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Literal

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

import server as core

Format = Literal["text", "json", "srt", "vtt", "md"]
PUBLIC_MAX_INLINE_BYTES = int(os.environ.get("PUBLIC_MAX_INLINE_BYTES", str(25 * 1024 * 1024)))
PUBLIC_MAX_INLINE_ZIP_BYTES = int(os.environ.get("PUBLIC_MAX_INLINE_ZIP_BYTES", str(25 * 1024 * 1024)))
PUBLIC_CONCURRENCY = max(1, int(os.environ.get("PUBLIC_CONCURRENCY", "1")))
PUBLIC_FACADE_ENABLED = os.environ.get("PUBLIC_FACADE_ENABLED", "0").strip().lower() in {
    "1", "true", "yes", "on"
}
_gate = asyncio.Semaphore(PUBLIC_CONCURRENCY)

mcp = FastMCP(
    "blue-transcription-public",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "mcp-origin.conexaoazul.com",
            "mcp-origin.conexaoazul.com:*",
            "127.0.0.1:*",
            "localhost:*",
        ],
        allowed_origins=[
            "https://mcp-origin.conexaoazul.com",
            "https://chatgpt.com",
            "https://claude.ai",
            "http://127.0.0.1:*",
            "http://localhost:*",
        ],
    ),
)


def _safe_media_name(filename: str) -> str:
    name = Path(filename).name or "attachment.bin"
    if not core._supported_media_name(name):
        raise core.ValidationError(f"unsupported media extension: {name}")
    return name


@mcp.tool()
async def transcribe_base64(
    filename: str,
    data_base64: str,
    format: Format = "text",
    language: str | None = None,
) -> str:
    """Transcribe caller-supplied inline audio/video bytes.

    This public facade cannot read server files or fetch remote URLs.
    """
    try:
        safe_name = _safe_media_name(filename)
        raw = core._decode_base64_limited(
            data_base64, PUBLIC_MAX_INLINE_BYTES, "inline media"
        )
    except core.ValidationError as exc:
        return f"Rejected: {exc}"

    async with _gate:
        with tempfile.TemporaryDirectory() as td:
            local = Path(td) / safe_name
            local.write_bytes(raw)
            response = await core._post_to_whisper(local, format, language)
            return core._format_result(
                response,
                format,
                title=local.stem,
                source=safe_name,
                source_kind="public_inline_base64",
            )


@mcp.tool()
async def transcribe_zip_base64(
    filename: str,
    data_base64: str,
    format: Format = "text",
    language: str | None = None,
    concurrency: int | None = None,
) -> str:
    """Safely extract and transcribe caller-supplied ZIP bytes."""
    safe_name = Path(filename).name or "archive.zip"
    if Path(safe_name).suffix.lower() != ".zip":
        return "Rejected: filename must end in .zip"

    try:
        raw = core._decode_base64_limited(
            data_base64, PUBLIC_MAX_INLINE_ZIP_BYTES, "inline ZIP"
        )
        async with _gate:
            with tempfile.TemporaryDirectory() as td:
                zip_path = Path(td) / safe_name
                zip_path.write_bytes(raw)
                manifest = await core._transcribe_zip_archive(
                    zip_path,
                    safe_name,
                    format,
                    language,
                    concurrency,
                    archive_limit=PUBLIC_MAX_INLINE_ZIP_BYTES,
                    archive_kind="public_inline_zip",
                )
        return json.dumps(manifest, indent=2, ensure_ascii=False)
    except core.ValidationError as exc:
        return f"Rejected: {exc}"


async def healthz(_request):
    return JSONResponse({
        "status": "ok",
        "mode": "public-stateless" if PUBLIC_FACADE_ENABLED else "trial-closed",
        "enabled": PUBLIC_FACADE_ENABLED,
        "tools": ["transcribe_base64", "transcribe_zip_base64"] if PUBLIC_FACADE_ENABLED else [],
    })


async def closed(_request):
    return JSONResponse(
        {
            "error": "trial_closed",
            "message": (
                "Blue Transcription managed trial is currently assisted and "
                "requires activation."
            ),
        },
        status_code=403,
    )


if PUBLIC_FACADE_ENABLED:
    app = mcp.streamable_http_app()
    app.add_route("/healthz", healthz, methods=["GET"])
else:
    app = Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route("/{path:path}", closed, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]),
        ]
    )


if __name__ == "__main__":
    core._ensure_output_dir()
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8084")))
