"""Private gRPC streaming adapter for Blue Transcription MCP.

The adapter intentionally owns no transcription logic. It receives ZIP bytes as
client-streamed protobuf BytesValue messages, enforces transport limits/auth,
and delegates safe extraction + ASR to server._transcribe_zip_archive().

RPC:
  /blue.transcription.v1.TranscriptionGateway/TranscribeZip

Request stream:
  google.protobuf.BytesValue

Response:
  google.protobuf.StringValue containing the JSON batch manifest

Required metadata:
  authorization: Bearer <same token used by MCP HTTP>

Optional metadata:
  x-filename: archive.zip
  x-format: text|json|srt|vtt|md
  x-language: pt
  x-concurrency: 1
  x-correlation-id: caller correlation id
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import AsyncIterator, cast

import grpc
from google.protobuf.wrappers_pb2 import BytesValue, StringValue

from server import (
    Format,
    MAX_DOWNLOAD_BYTES,
    ValidationError,
    _transcribe_zip_archive,
)

SERVICE_NAME = "blue.transcription.v1.TranscriptionGateway"
METHOD_NAME = "TranscribeZip"
GRPC_BIND = os.environ.get("GRPC_BIND", "0.0.0.0:50051")
MAX_GRPC_ZIP_BYTES = max(
    1, int(os.environ.get("MAX_GRPC_ZIP_BYTES", str(MAX_DOWNLOAD_BYTES)))
)
MAX_GRPC_CHUNK_BYTES = max(
    64 * 1024, int(os.environ.get("MAX_GRPC_CHUNK_BYTES", str(4 * 1024 * 1024)))
)
MAX_GRPC_RESPONSE_BYTES = max(
    1024 * 1024,
    int(os.environ.get("MAX_GRPC_RESPONSE_BYTES", str(32 * 1024 * 1024))),
)
ALLOWED_FORMATS = frozenset({"text", "json", "srt", "vtt", "md"})


class GatewayError(RuntimeError):
    """Raised only when gRPC context.abort unexpectedly returns."""


def _load_auth_token() -> str:
    token = os.environ.get("MCP_AUTH_TOKEN", "").strip()
    token_file = os.environ.get("MCP_AUTH_TOKEN_FILE", "").strip()
    if not token and token_file:
        try:
            token = Path(token_file).read_text().strip()
        except OSError as exc:
            raise RuntimeError(f"unable to read MCP_AUTH_TOKEN_FILE: {exc}") from exc
    if not token:
        raise RuntimeError(
            "MCP_AUTH_TOKEN is empty or unset; gRPC gateway refuses to start"
        )
    return token


def _metadata_dict(context: grpc.aio.ServicerContext) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for item in context.invocation_metadata():
        if hasattr(item, "key"):
            key, value = item.key, item.value
        else:
            key, value = item[0], item[1]
        metadata[str(key).lower()] = str(value)
    return metadata


async def _abort(
    context: grpc.aio.ServicerContext,
    code: grpc.StatusCode,
    details: str,
) -> None:
    await context.abort(code, details)
    raise GatewayError("gRPC context.abort returned unexpectedly")


class TranscriptionGateway:
    def __init__(self, token: str):
        self._expected_auth = f"Bearer {token}".encode()

    async def transcribe_zip(
        self,
        request_iterator: AsyncIterator[BytesValue],
        context: grpc.aio.ServicerContext,
    ) -> StringValue:
        metadata = _metadata_dict(context)
        received_auth = metadata.get("authorization", "").encode()
        if not hmac.compare_digest(received_auth, self._expected_auth):
            await _abort(context, grpc.StatusCode.UNAUTHENTICATED, "unauthorized")

        raw_filename = metadata.get("x-filename", "archive.zip").strip()
        filename = Path(raw_filename).name
        if (
            not raw_filename
            or raw_filename != filename
            or Path(filename).suffix.lower() != ".zip"
        ):
            await _abort(
                context,
                grpc.StatusCode.INVALID_ARGUMENT,
                "x-filename must be a plain .zip filename",
            )

        fmt_raw = metadata.get("x-format", "text").strip().lower()
        if fmt_raw not in ALLOWED_FORMATS:
            await _abort(
                context,
                grpc.StatusCode.INVALID_ARGUMENT,
                f"unsupported x-format: {fmt_raw}",
            )
        fmt = cast(Format, fmt_raw)

        language = metadata.get("x-language", "").strip() or None
        if language and len(language) > 16:
            await _abort(
                context,
                grpc.StatusCode.INVALID_ARGUMENT,
                "x-language is too long",
            )

        concurrency: int | None = None
        concurrency_raw = metadata.get("x-concurrency", "").strip()
        if concurrency_raw:
            try:
                concurrency = int(concurrency_raw)
            except ValueError:
                await _abort(
                    context,
                    grpc.StatusCode.INVALID_ARGUMENT,
                    "x-concurrency must be an integer",
                )
            if concurrency < 1:
                await _abort(
                    context,
                    grpc.StatusCode.INVALID_ARGUMENT,
                    "x-concurrency must be >= 1",
                )

        correlation_id = (
            metadata.get("x-correlation-id", "").strip() or str(uuid.uuid4())
        )

        total = 0
        with tempfile.TemporaryDirectory() as td:
            zip_path = Path(td) / filename
            with zip_path.open("wb") as handle:
                async for message in request_iterator:
                    chunk = bytes(message.value)
                    if not chunk:
                        continue
                    if len(chunk) > MAX_GRPC_CHUNK_BYTES:
                        await _abort(
                            context,
                            grpc.StatusCode.RESOURCE_EXHAUSTED,
                            f"chunk exceeds {MAX_GRPC_CHUNK_BYTES} bytes",
                        )
                    total += len(chunk)
                    if total > MAX_GRPC_ZIP_BYTES:
                        await _abort(
                            context,
                            grpc.StatusCode.RESOURCE_EXHAUSTED,
                            f"archive exceeds {MAX_GRPC_ZIP_BYTES} bytes",
                        )
                    handle.write(chunk)

            if total == 0:
                await _abort(
                    context,
                    grpc.StatusCode.INVALID_ARGUMENT,
                    "empty ZIP stream",
                )

            try:
                manifest = await _transcribe_zip_archive(
                    zip_path,
                    filename,
                    fmt,
                    language,
                    concurrency,
                    archive_limit=MAX_GRPC_ZIP_BYTES,
                    archive_kind="grpc_stream_zip",
                )
            except ValidationError as exc:
                await _abort(
                    context,
                    grpc.StatusCode.INVALID_ARGUMENT,
                    f"rejected: {exc}",
                )

        manifest["transport"] = "grpc"
        manifest["grpc_received_bytes"] = total
        manifest["correlation_id"] = correlation_id
        return StringValue(value=json.dumps(manifest, ensure_ascii=False))


async def serve() -> None:
    token = _load_auth_token()
    gateway = TranscriptionGateway(token)

    async def transcribe_zip_handler(request_iterator, context):
        return await gateway.transcribe_zip(request_iterator, context)

    rpc_handler = grpc.stream_unary_rpc_method_handler(
        transcribe_zip_handler,
        request_deserializer=BytesValue.FromString,
        response_serializer=StringValue.SerializeToString,
    )
    generic_handler = grpc.method_handlers_generic_handler(
        SERVICE_NAME,
        {METHOD_NAME: rpc_handler},
    )

    server = grpc.aio.server(
        options=[
            ("grpc.max_receive_message_length", MAX_GRPC_CHUNK_BYTES + 1024),
            ("grpc.max_send_message_length", MAX_GRPC_RESPONSE_BYTES),
        ]
    )
    server.add_generic_rpc_handlers((generic_handler,))
    bound_port = server.add_insecure_port(GRPC_BIND)
    if bound_port == 0:
        raise RuntimeError(f"unable to bind gRPC gateway on {GRPC_BIND}")

    await server.start()
    sys.stderr.write(
        f"Blue Transcription gRPC gateway listening on {GRPC_BIND}; "
        "keep this port private to the overlay network\n"
    )
    await server.wait_for_termination()


if __name__ == "__main__":
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
