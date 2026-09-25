"""MCP server: transcribe audio from file / URL / YouTube / RSS podcast.

Talks to whisper.cpp server on localhost:8082 (OpenAI-compatible).
Output formats: text, json (segments), srt, vtt, md.
File outputs land in the Obsidian Transcripts folder.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import ipaddress
import json
import os
import re
import socket
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import feedparser
import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

WHISPER_URL = os.environ.get(
    "WHISPER_URL", "http://host.docker.internal:8082/v1/audio/transcriptions"
)
OUTPUT_DIR = Path(
    os.environ.get("OUTPUT_DIR", "/home/marcus/Documents/Obsidian Vault/Transcripts")
)
ALLOWED_INPUT_ROOTS = tuple(
    Path(p).resolve()
    for p in os.environ.get(
        "ALLOWED_INPUT_ROOTS",
        "/home/marcus/Downloads:/home/marcus/Music:/home/marcus/whisper.cpp/samples",
    ).split(":")
    if p
)
MAX_DOWNLOAD_BYTES = int(os.environ.get("MAX_DOWNLOAD_BYTES", str(500 * 1024 * 1024)))
MAX_INLINE_BYTES = int(os.environ.get("MAX_INLINE_BYTES", str(25 * 1024 * 1024)))
MAX_BATCH_FILES = max(1, int(os.environ.get("MAX_BATCH_FILES", "100")))
BATCH_CONCURRENCY = max(1, int(os.environ.get("BATCH_CONCURRENCY", "2")))
MAX_BATCH_ITEM_BYTES = int(
    os.environ.get("MAX_BATCH_ITEM_BYTES", str(250 * 1024 * 1024))
)
MAX_ZIP_EXTRACT_BYTES = int(
    os.environ.get("MAX_ZIP_EXTRACT_BYTES", str(1024 * 1024 * 1024))
)
MAX_ZIP_ENTRIES = max(1, int(os.environ.get("MAX_ZIP_ENTRIES", "5000")))
SUPPORTED_MEDIA_EXTENSIONS = frozenset(
    {
        ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac",
        ".wma", ".mp4", ".webm", ".mkv", ".mov", ".avi", ".mpeg", ".mpg",
    }
)

MCP_ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get(
        "MCP_ALLOWED_HOSTS",
        "127.0.0.1:*,localhost:*",
    ).split(",")
    if h.strip()
]
MCP_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        "MCP_ALLOWED_ORIGINS",
        "http://127.0.0.1:*,http://localhost:*",
    ).split(",")
    if origin.strip()
]

Format = Literal["text", "json", "srt", "vtt", "md"]
WHISPER_FORMAT = {"text": "json", "json": "verbose_json", "srt": "srt", "vtt": "vtt", "md": "verbose_json"}

mcp = FastMCP(
    "whisper-transcribe",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=MCP_ALLOWED_HOSTS,
        allowed_origins=MCP_ALLOWED_ORIGINS,
    ),
)


# ---------------- validation helpers ----------------------------------------

class ValidationError(Exception):
    """User-supplied input failed a safety check."""


def _validate_input_path(path: str) -> Path:
    """Resolve user-supplied path and verify it lives under an allowed root.

    Catches `..` traversal and symlink escapes.
    """
    p = Path(path).expanduser().resolve(strict=False)
    if not p.exists():
        raise ValidationError(f"File not found: {p}")
    if not p.is_file():
        raise ValidationError(f"Not a regular file: {p}")
    for root in ALLOWED_INPUT_ROOTS:
        try:
            p.relative_to(root)
            return p
        except ValueError:
            continue
    raise ValidationError(
        f"Path not under any allowed input root. Allowed roots: "
        f"{', '.join(str(r) for r in ALLOWED_INPUT_ROOTS)}"
    )


def _validate_remote_url(url: str) -> str:
    """Reject non-http(s) schemes and private/loopback/link-local hosts.

    Returns the normalized URL.
    """
    try:
        parsed = urlparse(url)
    except Exception as e:
        raise ValidationError(f"Unparseable URL: {e}")

    if parsed.scheme not in ("http", "https"):
        raise ValidationError(
            f"Only http(s) URLs are accepted; got scheme={parsed.scheme!r}"
        )
    if not parsed.hostname:
        raise ValidationError("URL is missing a hostname")

    # Resolve and inspect every address the hostname maps to.
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as e:
        raise ValidationError(f"Hostname does not resolve: {parsed.hostname} ({e})")

    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValidationError(
                f"URL host {parsed.hostname!r} resolves to non-public address {addr}"
            )

    # host.docker.internal and similar are caught by the private-IP check above,
    # but block the exact label too as defense-in-depth.
    if parsed.hostname.lower() in ("host.docker.internal", "gateway.docker.internal"):
        raise ValidationError(f"Blocked hostname: {parsed.hostname}")

    return url


def _slugify(s: str, max_len: int = 80) -> str:
    s = re.sub(r"[^\w\s-]", "", s).strip()
    s = re.sub(r"[\s_-]+", "-", s)
    return s[:max_len] or "transcript"


def _supported_media_name(name: str) -> bool:
    """Return True when a filename has an explicitly allowed media extension."""
    return Path(name).suffix.lower() in SUPPORTED_MEDIA_EXTENSIONS


def _effective_batch_concurrency(requested: int | None) -> int:
    """Clamp caller concurrency to the operator-defined ceiling."""
    if requested is None:
        return BATCH_CONCURRENCY
    return max(1, min(int(requested), BATCH_CONCURRENCY))


# ---------------- whisper.cpp client ----------------------------------------

async def _post_to_whisper(audio_path: Path, fmt: Format, language: str | None) -> str | dict:
    whisper_fmt = WHISPER_FORMAT[fmt]
    data = {"response_format": whisper_fmt}
    if language:
        data["language"] = language
    async with httpx.AsyncClient(timeout=600.0) as client:
        with audio_path.open("rb") as f:
            files = {"file": (audio_path.name, f, "application/octet-stream")}
            r = await client.post(WHISPER_URL, data=data, files=files)
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        if "json" in ctype:
            return r.json()
        return r.text


# ---------------- formatting ------------------------------------------------

def _segments_to_md(segments: list[dict]) -> str:
    lines = []
    for seg in segments:
        t = float(seg.get("start", 0))
        h, rem = divmod(int(t), 3600)
        m, s = divmod(rem, 60)
        ts = f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
        text = seg.get("text", "").strip()
        if text:
            lines.append(f"**[{ts}]** {text}")
    return "\n\n".join(lines)


def _ensure_output_dir() -> Path:
    """Create the output dir lazily — only when a file-format is actually requested."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR


def _format_result(
    response: str | dict, fmt: Format, *, title: str, source: str, source_kind: str
) -> str:
    if fmt == "text":
        return response["text"].strip() if isinstance(response, dict) else str(response).strip()

    if fmt == "json":
        return json.dumps(response, indent=2, ensure_ascii=False)

    out_dir = _ensure_output_dir()
    slug = _slugify(title)
    stamp = datetime.now().strftime("%Y-%m-%d")

    if fmt == "srt":
        out = out_dir / f"{stamp}-{slug}.srt"
        out.write_text(response if isinstance(response, str) else response.get("text", ""))
        return f"Wrote {out}"

    if fmt == "vtt":
        out = out_dir / f"{stamp}-{slug}.vtt"
        out.write_text(response if isinstance(response, str) else response.get("text", ""))
        return f"Wrote {out}"

    if fmt == "md":
        if not isinstance(response, dict):
            raise ValueError("md format requires verbose_json response")
        body_text = response.get("text", "").strip()
        segments = response.get("segments", [])
        seg_md = _segments_to_md(segments) if segments else body_text
        out = out_dir / f"{stamp}-{slug}.md"
        out.write_text(
            f"---\n"
            f"title: {title}\n"
            f"source: {source}\n"
            f"source_kind: {source_kind}\n"
            f"transcribed: {datetime.now().isoformat(timespec='seconds')}\n"
            f"tags: [transcript]\n"
            f"---\n\n"
            f"# {title}\n\n"
            f"## Transcript\n\n"
            f"{seg_md}\n"
        )
        return f"Wrote {out}"

    raise ValueError(f"Unknown format: {fmt}")


# ---------------- downloaders ----------------------------------------------

async def _download(url: str, dest_dir: Path) -> Path:
    """Stream-download a validated URL to dest_dir with a size cap."""
    _validate_remote_url(url)
    parsed = urlparse(url)
    name = Path(parsed.path).name or "download.bin"
    out = dest_dir / name
    total = 0
    async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as client:
        async with client.stream("GET", url) as r:
            r.raise_for_status()
            with out.open("wb") as f:
                async for chunk in r.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise ValidationError(
                            f"Download exceeded {MAX_DOWNLOAD_BYTES} bytes; aborting"
                        )
                    f.write(chunk)
    return out


async def _ytdlp_extract(url: str, dest_dir: Path) -> tuple[Path, str]:
    """Extract audio via yt-dlp from a validated URL."""
    _validate_remote_url(url)
    out_template = str(dest_dir / "%(id)s.%(ext)s")
    max_mb = max(1, MAX_DOWNLOAD_BYTES // (1024 * 1024))
    proc = await asyncio.create_subprocess_exec(
        "yt-dlp",
        "-x", "--audio-format", "mp3",
        "--no-playlist",
        "--max-filesize", f"{max_mb}M",
        "--no-config",
        "--no-call-home",
        "--no-cache-dir",
        "--print", "after_move:%(title)s\t%(filepath)s",
        "-o", out_template,
        url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp failed: {stderr.decode()[-500:]}")
    last_line = stdout.decode().strip().splitlines()[-1]
    title, path = last_line.split("\t", 1)
    return Path(path), title


async def _fetch_feed(rss_url: str) -> "feedparser.FeedParserDict":
    """Fetch RSS via httpx (with URL validation + size cap), then parse.

    Avoids letting feedparser do its own networking (no timeout, no SSRF guard).
    """
    _validate_remote_url(rss_url)
    async with httpx.AsyncClient(
        timeout=30.0, follow_redirects=True
    ) as client:
        async with client.stream("GET", rss_url) as r:
            r.raise_for_status()
            chunks = []
            total = 0
            cap = min(MAX_DOWNLOAD_BYTES, 50 * 1024 * 1024)  # feeds shouldn't be huge
            async for chunk in r.aiter_bytes():
                total += len(chunk)
                if total > cap:
                    raise ValidationError(
                        f"RSS feed exceeded {cap} bytes; aborting"
                    )
                chunks.append(chunk)
    body = b"".join(chunks)
    return feedparser.parse(body)


# ---------------- batch helpers --------------------------------------------

async def _run_batch(
    items: list[tuple[Path, str, str, str]],
    fmt: Format,
    language: str | None,
    concurrency: int | None,
) -> dict:
    """Transcribe validated items with bounded concurrency, preserving order."""
    sem = asyncio.Semaphore(_effective_batch_concurrency(concurrency))

    async def one(index: int, item: tuple[Path, str, str, str]) -> dict:
        audio_path, source, source_kind, title = item
        async with sem:
            try:
                response = await _post_to_whisper(audio_path, fmt, language)
                result = _format_result(
                    response,
                    fmt,
                    title=f"{index + 1:03d}-{title}",
                    source=source,
                    source_kind=source_kind,
                )
                return {
                    "index": index,
                    "source": source,
                    "title": title,
                    "status": "ok",
                    "result": result,
                }
            except Exception as exc:
                return {
                    "index": index,
                    "source": source,
                    "title": title,
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                }

    results = await asyncio.gather(*(one(i, item) for i, item in enumerate(items)))
    ok = sum(1 for row in results if row["status"] == "ok")
    manifest = {
        "count": len(results),
        "ok": ok,
        "failed": len(results) - ok,
        "format": fmt,
        "language": language,
        "concurrency": _effective_batch_concurrency(concurrency),
        "results": results,
    }
    out_dir = _ensure_output_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    manifest_path = out_dir / f"batch-{stamp}.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    manifest["manifest_path"] = str(manifest_path)
    return manifest


# ---------------- MCP tools -------------------------------------------------

@mcp.tool()
async def transcribe_file(path: str, format: Format = "text", language: str | None = None) -> str:
    """Transcribe a local audio/video file.

    The path must resolve inside one of the allowed input roots
    (configured via ALLOWED_INPUT_ROOTS — defaults: ~/Downloads, ~/Music,
    ~/whisper.cpp/samples). Symlink escapes and ../ traversal are rejected.

    Args:
        path: Absolute path to the audio or video file.
        format: text | json | srt | vtt | md.
        language: ISO 639-1 code, omit for auto-detect.
    """
    try:
        p = _validate_input_path(path)
    except ValidationError as e:
        return f"Rejected: {e}"
    title = p.stem
    response = await _post_to_whisper(p, format, language)
    return _format_result(response, format, title=title, source=str(p), source_kind="file")


@mcp.tool()
async def transcribe_base64(
    filename: str, data_base64: str, format: Format = "text", language: str | None = None
) -> str:
    """Transcribe inline base64 audio/video data.

    Intended for MCP clients that have attachment bytes but no shared filesystem or public URL.
    The decoded payload is capped by MAX_INLINE_BYTES (25 MiB by default).
    """
    safe_name = Path(filename).name or "attachment.bin"
    try:
        raw = base64.b64decode(data_base64, validate=True)
    except (binascii.Error, ValueError):
        return "Rejected: invalid base64 payload"
    if len(raw) > MAX_INLINE_BYTES:
        return f"Rejected: decoded payload exceeds {MAX_INLINE_BYTES} bytes"
    with tempfile.TemporaryDirectory() as td:
        local = Path(td) / safe_name
        local.write_bytes(raw)
        response = await _post_to_whisper(local, format, language)
        return _format_result(
            response, format, title=local.stem, source=safe_name, source_kind="inline_base64"
        )


@mcp.tool()
async def transcribe_batch(
    paths: list[str],
    format: Format = "text",
    language: str | None = None,
    concurrency: int | None = None,
) -> str:
    """Transcribe multiple local audio/video files with bounded concurrency.

    Every path must pass the same allowed-root validation as transcribe_file.
    Unsupported extensions and oversized files are rejected before inference.
    """
    if not paths:
        return "Rejected: paths must contain at least one file"
    if len(paths) > MAX_BATCH_FILES:
        return f"Rejected: batch has {len(paths)} files; limit is {MAX_BATCH_FILES}"

    items: list[tuple[Path, str, str, str]] = []
    try:
        for raw_path in paths:
            p = _validate_input_path(raw_path)
            if not _supported_media_name(p.name):
                raise ValidationError(f"Unsupported media extension: {p.name}")
            size = p.stat().st_size
            if size > MAX_BATCH_ITEM_BYTES:
                raise ValidationError(
                    f"File {p.name} is {size} bytes; item limit is {MAX_BATCH_ITEM_BYTES}"
                )
            items.append((p, str(p), "batch_file", p.stem))
    except ValidationError as exc:
        return f"Rejected: {exc}"

    manifest = await _run_batch(items, format, language, concurrency)
    return json.dumps(manifest, indent=2, ensure_ascii=False)


@mcp.tool()
async def transcribe_zip(
    path: str,
    format: Format = "text",
    language: str | None = None,
    concurrency: int | None = None,
) -> str:
    """Safely extract and transcribe supported media files from a local ZIP."""
    try:
        zip_path = _validate_input_path(path)
        if zip_path.suffix.lower() != ".zip" or not zipfile.is_zipfile(zip_path):
            raise ValidationError("Input is not a valid .zip archive")
        if zip_path.stat().st_size > MAX_BATCH_ITEM_BYTES:
            raise ValidationError(
                f"ZIP is {zip_path.stat().st_size} bytes; archive limit is {MAX_BATCH_ITEM_BYTES}"
            )
    except ValidationError as exc:
        return f"Rejected: {exc}"

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        items: list[tuple[Path, str, str, str]] = []
        extracted_total = 0

        try:
            with zipfile.ZipFile(zip_path) as zf:
                infos = zf.infolist()
                if len(infos) > MAX_ZIP_ENTRIES:
                    raise ValidationError(
                        f"ZIP has {len(infos)} entries; limit is {MAX_ZIP_ENTRIES}"
                    )
                for info in infos:
                    if info.is_dir():
                        continue
                    mode = (info.external_attr >> 16) & 0o170000
                    if mode == 0o120000:
                        raise ValidationError(
                            f"ZIP symlink entry is not allowed: {info.filename}"
                        )

                    safe_name = Path(info.filename.replace("\\", "/")).name
                    if not safe_name or not _supported_media_name(safe_name):
                        continue
                    if len(items) >= MAX_BATCH_FILES:
                        raise ValidationError(
                            f"ZIP contains more than {MAX_BATCH_FILES} supported media files"
                        )
                    if info.file_size > MAX_BATCH_ITEM_BYTES:
                        raise ValidationError(
                            f"ZIP member {safe_name} is {info.file_size} bytes; "
                            f"item limit is {MAX_BATCH_ITEM_BYTES}"
                        )

                    target = work / f"{len(items) + 1:03d}-{safe_name}"
                    written = 0
                    with zf.open(info, "r") as src, target.open("wb") as dst:
                        while True:
                            chunk = src.read(1024 * 1024)
                            if not chunk:
                                break
                            written += len(chunk)
                            extracted_total += len(chunk)
                            if written > MAX_BATCH_ITEM_BYTES:
                                raise ValidationError(
                                    f"ZIP member {safe_name} exceeded item limit while extracting"
                                )
                            if extracted_total > MAX_ZIP_EXTRACT_BYTES:
                                raise ValidationError(
                                    f"ZIP extraction exceeded {MAX_ZIP_EXTRACT_BYTES} bytes"
                                )
                            dst.write(chunk)

                    items.append(
                        (
                            target,
                            f"{zip_path}!{info.filename}",
                            "zip_member",
                            Path(safe_name).stem,
                        )
                    )
        except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
            return f"Rejected: unable to read ZIP safely: {exc}"
        except ValidationError as exc:
            return f"Rejected: {exc}"

        if not items:
            return (
                "Rejected: ZIP contains no supported media files. Supported extensions: "
                + ", ".join(sorted(SUPPORTED_MEDIA_EXTENSIONS))
            )

        manifest = await _run_batch(items, format, language, concurrency)
        manifest["archive"] = str(zip_path)
        manifest["extracted_bytes"] = extracted_total
        Path(manifest["manifest_path"]).write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False)
        )
        return json.dumps(manifest, indent=2, ensure_ascii=False)


@mcp.tool()
async def transcribe_url(url: str, format: Format = "text", language: str | None = None) -> str:
    """Transcribe audio from an http(s) URL (direct link to mp3/wav/m4a/etc).

    Only public http(s) URLs are accepted; private/loopback/link-local hosts
    are rejected. The download is capped at MAX_DOWNLOAD_BYTES (default 500MB).
    """
    try:
        with tempfile.TemporaryDirectory() as td:
            local = await _download(url, Path(td))
            title = Path(urlparse(url).path).stem or "url-audio"
            response = await _post_to_whisper(local, format, language)
            return _format_result(response, format, title=title, source=url, source_kind="url")
    except ValidationError as e:
        return f"Rejected: {e}"


@mcp.tool()
async def transcribe_youtube(url: str, format: Format = "text", language: str | None = None) -> str:
    """Transcribe a YouTube (or yt-dlp-supported) video via audio extraction.

    URL is validated (http/https + public host) before yt-dlp sees it,
    closing yt-dlp's file:// and internal-host vectors.
    """
    try:
        with tempfile.TemporaryDirectory() as td:
            audio, title = await _ytdlp_extract(url, Path(td))
            response = await _post_to_whisper(audio, format, language)
            return _format_result(response, format, title=title, source=url, source_kind="youtube")
    except ValidationError as e:
        return f"Rejected: {e}"


@mcp.tool()
async def transcribe_podcast(
    rss_url: str,
    episode_index: int = 0,
    format: Format = "md",
    language: str | None = None,
) -> str:
    """Transcribe a podcast episode from an RSS feed.

    The feed URL and the chosen enclosure URL are both validated against the
    URL allowlist (public http(s) only). If the chosen entry has no audio
    enclosure but does have a video enclosure, the video is transcribed instead.
    """
    try:
        feed = await _fetch_feed(rss_url)
    except ValidationError as e:
        return f"Rejected: {e}"

    if not feed.entries:
        return f"No entries in feed: {rss_url}"
    if episode_index >= len(feed.entries):
        return f"Index {episode_index} out of range (feed has {len(feed.entries)} entries)"

    entry = feed.entries[episode_index]
    title = entry.get("title", "podcast-episode")

    # Prefer audio enclosures; fall back to video so video podcasts still work.
    audio_url = None
    fallback_url = None
    for enc in entry.get("enclosures", []):
        href = enc.get("href") or enc.get("url")
        if not href:
            continue
        etype = enc.get("type", "")
        if etype.startswith("audio"):
            audio_url = href
            break
        if etype.startswith("video") and not fallback_url:
            fallback_url = href
    chosen_url = audio_url or fallback_url
    if not chosen_url:
        return f"No audio or video enclosure found in entry: {title}"

    podcast_title = feed.feed.get("title", "Podcast")
    full_title = f"{podcast_title} - {title}"

    try:
        with tempfile.TemporaryDirectory() as td:
            local = await _download(chosen_url, Path(td))
            response = await _post_to_whisper(local, format, language)
            return _format_result(
                response, format, title=full_title, source=chosen_url, source_kind="podcast"
            )
    except ValidationError as e:
        return f"Rejected: {e}"


# ---------------- entrypoint ------------------------------------------------

if __name__ == "__main__":
    transport = os.environ.get("TRANSPORT", "stdio").lower()
    if transport in ("http", "streamable-http"):
        import uvicorn
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import JSONResponse

        token = os.environ.get("MCP_AUTH_TOKEN", "").strip()
        token_file = os.environ.get("MCP_AUTH_TOKEN_FILE", "").strip()
        if not token and token_file:
            try:
                token = Path(token_file).read_text().strip()
            except OSError as exc:
                sys.stderr.write(f"FATAL: unable to read MCP_AUTH_TOKEN_FILE: {exc}\n")
                sys.exit(1)
        if not token:
            sys.stderr.write(
                "FATAL: MCP_AUTH_TOKEN is empty or unset. "
                "HTTP transport refuses to start without an auth token. "
                "Set MCP_AUTH_TOKEN (e.g. via the .env file) and retry.\n"
            )
            sys.exit(1)
        expected = f"Bearer {token}".encode()

        class BearerAuth(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                if request.url.path == "/healthz":
                    return await call_next(request)
                hdr = request.headers.get("authorization", "").encode()
                # Constant-time compare to avoid token-length timing oracles.
                if not hmac.compare_digest(hdr, expected):
                    return JSONResponse({"error": "unauthorized"}, status_code=401)
                return await call_next(request)

        # Ensure output dir exists eagerly in HTTP mode so we don't surprise
        # callers with mkdir failures later.
        _ensure_output_dir()

        app = mcp.streamable_http_app()

        async def healthz(_request):
            output_writable = OUTPUT_DIR.exists() and os.access(OUTPUT_DIR, os.W_OK)
            status = "ok" if output_writable else "degraded"
            return JSONResponse(
                {"status": status, "output_writable": output_writable},
                status_code=200 if output_writable else 503,
            )

        app.add_route("/healthz", healthz, methods=["GET"])
        app.add_middleware(BearerAuth)
        host = os.environ.get("HOST", "0.0.0.0")
        port = int(os.environ.get("PORT", "8083"))
        uvicorn.run(app, host=host, port=port)
    else:
        mcp.run()
