import json
import unittest
from unittest.mock import AsyncMock, patch

import grpc
from google.protobuf.wrappers_pb2 import BytesValue

import grpc_gateway


class AbortError(RuntimeError):
    def __init__(self, code, details):
        super().__init__(details)
        self.code = code
        self.details = details


class FakeContext:
    def __init__(self, metadata):
        self._metadata = metadata

    def invocation_metadata(self):
        return self._metadata

    async def abort(self, code, details):
        raise AbortError(code, details)


async def chunk_stream(*chunks):
    for chunk in chunks:
        yield BytesValue(value=chunk)


class GrpcGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_streams_binary_zip_to_existing_core(self):
        captured = {}

        async def fake_transcribe(
            path,
            archive_label,
            fmt,
            language,
            concurrency,
            *,
            archive_limit,
            archive_kind,
        ):
            captured["bytes"] = path.read_bytes()
            captured["archive_label"] = archive_label
            captured["fmt"] = fmt
            captured["language"] = language
            captured["concurrency"] = concurrency
            captured["archive_limit"] = archive_limit
            captured["archive_kind"] = archive_kind
            return {"count": 1, "ok": 1, "failed": 0}

        gateway = grpc_gateway.TranscriptionGateway("secret")
        context = FakeContext(
            [
                ("authorization", "Bearer secret"),
                ("x-filename", "whatsapp.zip"),
                ("x-format", "text"),
                ("x-language", "pt"),
                ("x-concurrency", "1"),
                ("x-correlation-id", "corr-123"),
            ]
        )

        with patch.object(
            grpc_gateway,
            "_transcribe_zip_archive",
            AsyncMock(side_effect=fake_transcribe),
        ):
            response = await gateway.transcribe_zip(
                chunk_stream(b"abc", b"def"),
                context,
            )

        data = json.loads(response.value)
        self.assertEqual(captured["bytes"], b"abcdef")
        self.assertEqual(captured["archive_label"], "whatsapp.zip")
        self.assertEqual(captured["fmt"], "text")
        self.assertEqual(captured["language"], "pt")
        self.assertEqual(captured["concurrency"], 1)
        self.assertEqual(captured["archive_kind"], "grpc_stream_zip")
        self.assertEqual(data["transport"], "grpc")
        self.assertEqual(data["grpc_received_bytes"], 6)
        self.assertEqual(data["correlation_id"], "corr-123")

    async def test_rejects_unauthorized_request(self):
        gateway = grpc_gateway.TranscriptionGateway("secret")
        context = FakeContext([("authorization", "Bearer wrong")])

        with self.assertRaises(AbortError) as caught:
            await gateway.transcribe_zip(chunk_stream(b"abc"), context)

        self.assertEqual(caught.exception.code, grpc.StatusCode.UNAUTHENTICATED)

    async def test_rejects_oversized_stream_before_core(self):
        gateway = grpc_gateway.TranscriptionGateway("secret")
        context = FakeContext(
            [
                ("authorization", "Bearer secret"),
                ("x-filename", "archive.zip"),
            ]
        )

        with patch.object(grpc_gateway, "MAX_GRPC_ZIP_BYTES", 5):
            with self.assertRaises(AbortError) as caught:
                await gateway.transcribe_zip(
                    chunk_stream(b"abc", b"def"),
                    context,
                )

        self.assertEqual(caught.exception.code, grpc.StatusCode.RESOURCE_EXHAUSTED)

    async def test_rejects_path_like_filename(self):
        gateway = grpc_gateway.TranscriptionGateway("secret")
        context = FakeContext(
            [
                ("authorization", "Bearer secret"),
                ("x-filename", "../archive.zip"),
            ]
        )

        with self.assertRaises(AbortError) as caught:
            await gateway.transcribe_zip(chunk_stream(b"abc"), context)

        self.assertEqual(caught.exception.code, grpc.StatusCode.INVALID_ARGUMENT)

    async def test_rejects_unknown_format(self):
        gateway = grpc_gateway.TranscriptionGateway("secret")
        context = FakeContext(
            [
                ("authorization", "Bearer secret"),
                ("x-filename", "archive.zip"),
                ("x-format", "raw"),
            ]
        )

        with self.assertRaises(AbortError) as caught:
            await gateway.transcribe_zip(chunk_stream(b"abc"), context)

        self.assertEqual(caught.exception.code, grpc.StatusCode.INVALID_ARGUMENT)


if __name__ == "__main__":
    unittest.main()
