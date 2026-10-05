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
import hashlib
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

from metering import (
    CURRENT_TENANT,
    ConcurrencyExceeded,
    MeteringError,
    QuotaExceeded,
    RateLimitExceeded,
    RequestIdRequired,
    TrialExpired,
    TenantUnauthorized,
    store_from_env,
)

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
MAX_INLINE_ZIP_BYTES = int(
    os.environ.get("MAX_INLINE_ZIP_BYTES", str(MAX_INLINE_BYTES))
)
MAX_BATCH_FILES = max(1, int(os.environ.get("MAX_BATCH_FILES", "100")))
BATCH_CONCURRENCY = max(1, int(os.environ.get("BATCH_CONCURRENCY", "1")))
MAX_BATCH_ITEM_BYTES = int(
    os.environ.get("MAX_BATCH_ITEM_BYTES", str(250 * 1024 * 1024))
)
MAX_ZIP_EXTRACT_BYTES = int(
    os.environ.get("MAX_ZIP_EXTRACT_BYTES", str(1024 * 1024 * 1024))
)
MAX_ZIP_ENTRIES = max(1, int(os.environ.get("MAX_ZIP_ENTRIES", "5000")))
MAX_PROMPT_CHARS = max(1, int(os.environ.get("MAX_PROMPT_CHARS", "1024")))
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
METERING_STORE = store_from_env()

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


def _normalize_prompt(prompt: str | None) -> str | None:
    """Validate request-scoped Whisper context without persisting it."""
    if prompt is None:
        return None
    value = str(prompt).strip()
    if not value:
        return None
    if "\x00" in value:
        raise ValidationError("prompt must not contain NUL bytes")
    if len(value) > MAX_PROMPT_CHARS:
        raise ValidationError(
            f"prompt exceeds {MAX_PROMPT_CHARS} characters"
        )
    return value


def _effective_batch_concurrency(requested: int | None) -> int:
    """Clamp caller concurrency to the operator-defined ceiling."""
    if requested is None:
        return BATCH_CONCURRENCY
    return max(1, min(int(requested), BATCH_CONCURRENCY))


def _decode_base64_limited(data_base64: str, max_bytes: int, label: str) -> bytes:
    """Strictly decode base64 while rejecting oversized payloads before allocation."""
    compact = re.sub(r"\\s+", "", data_base64 or "")
    if not compact:
        raise ValidationError(f"{label} base64 payload is empty")

    # Base64 expands 3 input bytes to 4 text bytes. Reject clearly oversized
    # inputs before decoding to avoid allocating attacker-controlled blobs.
    max_encoded = ((max_bytes + 2) // 3) * 4 + 4
    if len(compact) > max_encoded:
        raise ValidationError(f"{label} payload exceeds {max_bytes} decoded bytes")

    try:
        raw = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        raise ValidationError(f"invalid {label} base64 payload")

    if len(raw) > max_bytes:
        raise ValidationError(f"{label} payload exceeds {max_bytes} decoded bytes")
    return raw


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _probe_duration_seconds(path: Path) -> float:
    """Return media duration using ffprobe; metered tenants fail closed on unknown duration."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise ValidationError(
            f"Unable to determine media duration for quota metering: "
            f"{stderr.decode(errors='replace')[-200:]}"
        )
    try:
        duration = float(stdout.decode().strip())
    except ValueError as exc:
        raise ValidationError("Unable to determine media duration for quota metering") from exc
    if duration <= 0:
        raise ValidationError("Media duration must be greater than zero")
    return duration


async def _reserve_metered_usage(
    *,
    tool: str,
    source: str,
    paths: list[Path],
    request_id: str | None,
):
    """Reserve tenant quota before inference. Master-token calls are not metered."""
    tenant = CURRENT_TENANT.get()
    if tenant is None or METERING_STORE is None:
        return None, 0.0

    total_seconds = 0.0
    for path in paths:
        total_seconds += await _probe_duration_seconds(path)

    try:
        reservation = METERING_STORE.reserve(
            tenant,
            tool=tool,
            source=source,
            seconds=total_seconds,
            request_id=request_id,
        )
    except (
        RequestIdRequired,
        TrialExpired,
        QuotaExceeded,
        ConcurrencyExceeded,
        RateLimitExceeded,
        MeteringError,
    ) as exc:
        raise ValidationError(f"metering: {exc}") from exc
    return reservation, total_seconds


def _complete_metered_usage(reservation, seconds: float) -> None:
    if reservation is not None and METERING_STORE is not None:
        METERING_STORE.complete(reservation, seconds)


def _fail_metered_usage(reservation) -> None:
    if reservation is not None and METERING_STORE is not None:
        METERING_STORE.fail(reservation)


# ---------------- whisper.cpp client ----------------------------------------

async def _post_to_whisper(
    audio_path: Path,
    fmt: Format,
    language: str | None,
    prompt: str | None = None,
    carry_initial_prompt: bool = False,
) -> str | dict:
    whisper_fmt = WHISPER_FORMAT[fmt]
    data = {"response_format": whisper_fmt}
    if language:
        data["language"] = language
    if prompt:
        data["prompt"] = prompt
        data["carry_initial_prompt"] = "true" if carry_initial_prompt else "false"
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
    prompt: str | None = None,
    carry_initial_prompt: bool = False,
) -> dict:
    """Transcribe validated items with bounded concurrency, preserving order."""
    sem = asyncio.Semaphore(_effective_batch_concurrency(concurrency))

    async def one(index: int, item: tuple[Path, str, str, str]) -> dict:
        audio_path, source, source_kind, title = item
        async with sem:
            try:
                response = await _post_to_whisper(
                    audio_path,
                    fmt,
                    language,
                    prompt,
                    carry_initial_prompt,
                )
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


async def _transcribe_zip_archive(
    zip_path: Path,
    archive_label: str,
    fmt: Format,
    language: str | None,
    concurrency: int | None,
    prompt: str | None = None,
    carry_initial_prompt: bool = False,
    *,
    archive_limit: int,
    archive_kind: str,
    metering_tool: str | None = None,
    request_id: str | None = None,
) -> dict:
    """Apply the same bounded/safe ZIP extraction policy to any trusted local temp path."""
    prompt = _normalize_prompt(prompt)
    if zip_path.suffix.lower() != ".zip" or not zipfile.is_zipfile(zip_path):
        raise ValidationError("Input is not a valid .zip archive")
    archive_size = zip_path.stat().st_size
    if archive_size > archive_limit:
        raise ValidationError(
            f"ZIP is {archive_size} bytes; archive limit is {archive_limit}"
        )

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        items: list[tuple[Path, str, str, str]] = []
        members: list[dict] = []
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

                    safe_name = Path(info.filename.replace("\\\\", "/")).name
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
                    digest = hashlib.sha256()
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
                            digest.update(chunk)
                            dst.write(chunk)

                    members.append(
                        {
                            "member": info.filename,
                            "safe_name": safe_name,
                            "bytes": written,
                            "sha256": digest.hexdigest(),
                        }
                    )
                    items.append(
                        (
                            target,
                            f"{archive_label}!{info.filename}",
                            "zip_member",
                            Path(safe_name).stem,
                        )
                    )
        except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
            raise ValidationError(f"unable to read ZIP safely: {exc}") from exc

        if not items:
            raise ValidationError(
                "ZIP contains no supported media files. Supported extensions: "
                + ", ".join(sorted(SUPPORTED_MEDIA_EXTENSIONS))
            )

        reservation = None
        metered_seconds = 0.0
        try:
            reservation, metered_seconds = await _reserve_metered_usage(
                tool=metering_tool or archive_kind,
                source=archive_label,
                paths=[item[0] for item in items],
                request_id=request_id,
            )
            manifest = await _run_batch(
                items,
                fmt,
                language,
                concurrency,
                prompt,
                carry_initial_prompt,
            )
            _complete_metered_usage(reservation, metered_seconds)
        except Exception:
            _fail_metered_usage(reservation)
            raise

        if reservation is not None:
            manifest["usage_seconds"] = round(metered_seconds, 3)
            if METERING_STORE is not None and reservation.tenant_id:
                manifest["usage"] = METERING_STORE.usage_summary(reservation.tenant_id)

        manifest.update(
            {
                "archive": archive_label,
                "archive_kind": archive_kind,
                "archive_bytes": archive_size,
                "archive_sha256": _sha256_file(zip_path),
                "extracted_bytes": extracted_total,
                "members": members,
            }
        )
        Path(manifest["manifest_path"]).write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False)
        )
        return manifest


# ---------------- MCP tools -------------------------------------------------

@mcp.tool()
async def transcribe_file(
    path: str,
    format: Format = "text",
    language: str | None = None,
    request_id: str | None = None,
) -> str:
    """Transcribe a local audio/video file.

    For metered tenants, request_id must be stable across retries so usage is
    idempotent. Master-token/operator calls remain unmetered.
    """
    reservation = None
    try:
        p = _validate_input_path(path)
        reservation, metered_seconds = await _reserve_metered_usage(
            tool="transcribe_file",
            source=str(p),
            paths=[p],
            request_id=request_id,
        )
        title = p.stem
        response = await _post_to_whisper(p, format, language)
        result = _format_result(
            response, format, title=title, source=str(p), source_kind="file"
        )
        _complete_metered_usage(reservation, metered_seconds)
        return result
    except ValidationError as e:
        _fail_metered_usage(reservation)
        return f"Rejected: {e}"
    except Exception:
        _fail_metered_usage(reservation)
        raise


@mcp.tool()
async def transcribe_base64(
    filename: str,
    data_base64: str,
    format: Format = "text",
    language: str | None = None,
    request_id: str | None = None,
) -> str:
    """Transcribe inline base64 audio/video data with optional tenant metering."""
    safe_name = Path(filename).name or "attachment.bin"
    reservation = None
    try:
        raw = _decode_base64_limited(data_base64, MAX_INLINE_BYTES, "inline media")
        with tempfile.TemporaryDirectory() as td:
            local = Path(td) / safe_name
            local.write_bytes(raw)
            reservation, metered_seconds = await _reserve_metered_usage(
                tool="transcribe_base64",
                source=f"{safe_name}:{hashlib.sha256(raw).hexdigest()}",
                paths=[local],
                request_id=request_id,
            )
            response = await _post_to_whisper(local, format, language)
            result = _format_result(
                response,
                format,
                title=local.stem,
                source=safe_name,
                source_kind="inline_base64",
            )
            _complete_metered_usage(reservation, metered_seconds)
            return result
    except ValidationError as exc:
        _fail_metered_usage(reservation)
        return f"Rejected: {exc}"
    except Exception:
        _fail_metered_usage(reservation)
        raise


@mcp.tool()
async def transcribe_batch(
    paths: list[str],
    format: Format = "text",
    language: str | None = None,
    concurrency: int | None = None,
    request_id: str | None = None,
) -> str:
    """Transcribe multiple local audio/video files with bounded concurrency."""
    if not paths:
        return "Rejected: paths must contain at least one file"
    if len(paths) > MAX_BATCH_FILES:
        return f"Rejected: batch has {len(paths)} files; limit is {MAX_BATCH_FILES}"

    items: list[tuple[Path, str, str, str]] = []
    reservation = None
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

        reservation, metered_seconds = await _reserve_metered_usage(
            tool="transcribe_batch",
            source="|".join(str(item[0]) for item in items),
            paths=[item[0] for item in items],
            request_id=request_id,
        )
        manifest = await _run_batch(items, format, language, concurrency)
        _complete_metered_usage(reservation, metered_seconds)
        if reservation is not None:
            manifest["usage_seconds"] = round(metered_seconds, 3)
            if METERING_STORE is not None and reservation.tenant_id:
                manifest["usage"] = METERING_STORE.usage_summary(reservation.tenant_id)
        return json.dumps(manifest, indent=2, ensure_ascii=False)
    except ValidationError as exc:
        _fail_metered_usage(reservation)
        return f"Rejected: {exc}"
    except Exception:
        _fail_metered_usage(reservation)
        raise


@mcp.tool()
async def transcribe_zip(
    path: str,
    format: Format = "text",
    language: str | None = None,
    concurrency: int | None = None,
    request_id: str | None = None,
) -> str:
    """Safely extract and transcribe supported media files from a local ZIP."""
    try:
        zip_path = _validate_input_path(path)
        manifest = await _transcribe_zip_archive(
            zip_path,
            str(zip_path),
            format,
            language,
            concurrency,
            archive_limit=MAX_BATCH_ITEM_BYTES,
            archive_kind="local_zip",
            metering_tool="transcribe_zip",
            request_id=request_id,
        )
        return json.dumps(manifest, indent=2, ensure_ascii=False)
    except ValidationError as exc:
        return f"Rejected: {exc}"


@mcp.tool()
async def transcribe_zip_base64(
    filename: str,
    data_base64: str,
    format: Format = "text",
    language: str | None = None,
    concurrency: int | None = None,
    request_id: str | None = None,
) -> str:
    """Safely transcribe a ZIP supplied inline as base64.

    This is intended for MCP clients with attachment bytes but no shared filesystem.
    The archive is decoded into an isolated temporary directory, capped by
    MAX_INLINE_ZIP_BYTES, and then passes the exact same ZIP entry, symlink,
    extension, per-member, total-extraction and concurrency gates as transcribe_zip.
    """
    safe_name = Path(filename).name or "archive.zip"
    if Path(safe_name).suffix.lower() != ".zip":
        return "Rejected: filename must end in .zip"

    try:
        raw = _decode_base64_limited(
            data_base64, MAX_INLINE_ZIP_BYTES, "inline ZIP"
        )
        with tempfile.TemporaryDirectory() as td:
            zip_path = Path(td) / safe_name
            zip_path.write_bytes(raw)
            manifest = await _transcribe_zip_archive(
                zip_path,
                safe_name,
                format,
                language,
                concurrency,
                archive_limit=MAX_INLINE_ZIP_BYTES,
                archive_kind="inline_base64_zip",
                metering_tool="transcribe_zip_base64",
                request_id=request_id,
            )
            return json.dumps(manifest, indent=2, ensure_ascii=False)
    except ValidationError as exc:
        return f"Rejected: {exc}"


@mcp.tool()
async def transcribe_url(
    url: str,
    format: Format = "text",
    language: str | None = None,
    request_id: str | None = None,
) -> str:
    """Transcribe audio from a public http(s) URL with optional tenant metering."""
    reservation = None
    try:
        with tempfile.TemporaryDirectory() as td:
            local = await _download(url, Path(td))
            reservation, metered_seconds = await _reserve_metered_usage(
                tool="transcribe_url",
                source=url,
                paths=[local],
                request_id=request_id,
            )
            title = Path(urlparse(url).path).stem or "url-audio"
            response = await _post_to_whisper(local, format, language)
            result = _format_result(
                response, format, title=title, source=url, source_kind="url"
            )
            _complete_metered_usage(reservation, metered_seconds)
            return result
    except ValidationError as e:
        _fail_metered_usage(reservation)
        return f"Rejected: {e}"
    except Exception:
        _fail_metered_usage(reservation)
        raise


@mcp.tool()
async def transcribe_youtube(
    url: str,
    format: Format = "text",
    language: str | None = None,
    request_id: str | None = None,
) -> str:
    """Transcribe a yt-dlp-supported URL with optional tenant metering."""
    reservation = None
    try:
        with tempfile.TemporaryDirectory() as td:
            audio, title = await _ytdlp_extract(url, Path(td))
            reservation, metered_seconds = await _reserve_metered_usage(
                tool="transcribe_youtube",
                source=url,
                paths=[audio],
                request_id=request_id,
            )
            response = await _post_to_whisper(audio, format, language)
            result = _format_result(
                response, format, title=title, source=url, source_kind="youtube"
            )
            _complete_metered_usage(reservation, metered_seconds)
            return result
    except ValidationError as e:
        _fail_metered_usage(reservation)
        return f"Rejected: {e}"
    except Exception:
        _fail_metered_usage(reservation)
        raise


@mcp.tool()
async def transcribe_podcast(
    rss_url: str,
    episode_index: int = 0,
    format: Format = "md",
    language: str | None = None,
    request_id: str | None = None,
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

    reservation = None
    try:
        with tempfile.TemporaryDirectory() as td:
            local = await _download(chosen_url, Path(td))
            reservation, metered_seconds = await _reserve_metered_usage(
                tool="transcribe_podcast",
                source=chosen_url,
                paths=[local],
                request_id=request_id,
            )
            response = await _post_to_whisper(local, format, language)
            result = _format_result(
                response,
                format,
                title=full_title,
                source=chosen_url,
                source_kind="podcast",
            )
            _complete_metered_usage(reservation, metered_seconds)
            return result
    except ValidationError as e:
        _fail_metered_usage(reservation)
        return f"Rejected: {e}"
    except Exception:
        _fail_metered_usage(reservation)
        raise


# ---------------- entrypoint ------------------------------------------------

if __name__ == "__main__":
    transport = os.environ.get("TRANSPORT", "stdio").lower()
    if transport in ("http", "streamable-http"):
        import uvicorn
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

        class TenantBearerAuth:
            """Pure ASGI auth middleware so ContextVar reaches MCP tool execution."""

            def __init__(self, inner):
                self.inner = inner

            async def __call__(self, scope, receive, send):
                if scope.get("type") != "http":
                    return await self.inner(scope, receive, send)

                path = scope.get("path", "")
                if path == "/healthz":
                    return await self.inner(scope, receive, send)

                headers = {
                    key.decode("latin-1").lower(): value.decode("latin-1")
                    for key, value in scope.get("headers", [])
                }
                auth = headers.get("authorization", "")
                auth_bytes = auth.encode()

                # Operator/master token preserves existing behavior and bypasses
                # tenant metering. It is never interpreted as a tenant API key.
                if hmac.compare_digest(auth_bytes, expected):
                    ctx_token = CURRENT_TENANT.set(None)
                    try:
                        return await self.inner(scope, receive, send)
                    finally:
                        CURRENT_TENANT.reset(ctx_token)

                if METERING_STORE is None or not auth.startswith("Bearer "):
                    response = JSONResponse({"error": "unauthorized"}, status_code=401)
                    return await response(scope, receive, send)

                api_key = auth[7:].strip()
                try:
                    tenant = METERING_STORE.get_tenant_by_api_key(api_key)
                except TrialExpired:
                    response = JSONResponse({"error": "access_expired"}, status_code=403)
                    return await response(scope, receive, send)
                except TenantUnauthorized:
                    response = JSONResponse({"error": "unauthorized"}, status_code=401)
                    return await response(scope, receive, send)

                ctx_token = CURRENT_TENANT.set(tenant)
                try:
                    return await self.inner(scope, receive, send)
                finally:
                    CURRENT_TENANT.reset(ctx_token)

        # Ensure output dir exists eagerly in HTTP mode so we don't surprise
        # callers with mkdir failures later.
        _ensure_output_dir()

        app = mcp.streamable_http_app()

        async def healthz(_request):
            output_writable = OUTPUT_DIR.exists() and os.access(OUTPUT_DIR, os.W_OK)
            status = "ok" if output_writable else "degraded"
            return JSONResponse(
                {
                    "status": status,
                    "output_writable": output_writable,
                    "metering_enabled": METERING_STORE is not None,
                },
                status_code=200 if output_writable else 503,
            )

        async def usage(_request):
            tenant = CURRENT_TENANT.get()
            if tenant is None or METERING_STORE is None:
                return JSONResponse(
                    {"error": "tenant_context_required"}, status_code=400
                )
            return JSONResponse(METERING_STORE.usage_summary(tenant.id))

        app.add_route("/healthz", healthz, methods=["GET"])
        app.add_route("/usage", usage, methods=["GET"])
        app = TenantBearerAuth(app)
        host = os.environ.get("HOST", "0.0.0.0")
        port = int(os.environ.get("PORT", "8083"))
        uvicorn.run(app, host=host, port=port)
    else:
        mcp.run()
