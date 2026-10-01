import asyncio
import base64
import io
import json
import stat
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import server


def make_zip(entries):
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return bio.getvalue()


async def fake_run_batch(items, fmt, language, concurrency):
    out = Path("/tmp/out")
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest.json"
    manifest_path.write_text("{}")
    return {
        "count": len(items),
        "ok": len(items),
        "failed": 0,
        "format": fmt,
        "language": language,
        "concurrency": 1,
        "results": [
            {
                "index": i,
                "source": item[1],
                "title": item[3],
                "status": "ok",
                "result": "stub",
            }
            for i, item in enumerate(items)
        ],
        "manifest_path": str(manifest_path),
    }


class InlineZipTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_zip_filters_media_and_hashes_members(self):
        raw = make_zip(
            [
                ("chat/PTT-1.opus", b"abc"),
                ("ignore.txt", b"x"),
                ("PTT-2.ogg", b"defg"),
            ]
        )
        with patch.object(server, "_run_batch", fake_run_batch):
            out = await server.transcribe_zip_base64(
                "whatsapp.zip", base64.b64encode(raw).decode(), language="pt"
            )

        data = json.loads(out)
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["archive_kind"], "inline_base64_zip")
        self.assertEqual(len(data["archive_sha256"]), 64)
        self.assertEqual(
            [member["safe_name"] for member in data["members"]],
            ["PTT-1.opus", "PTT-2.ogg"],
        )
        self.assertTrue(all(len(member["sha256"]) == 64 for member in data["members"]))

    async def test_invalid_base64_is_rejected(self):
        out = await server.transcribe_zip_base64("whatsapp.zip", "%%%")
        self.assertTrue(out.startswith("Rejected: invalid inline ZIP base64 payload"), out)

    async def test_wrong_extension_is_rejected(self):
        raw = make_zip([("a.opus", b"x")])
        out = await server.transcribe_zip_base64(
            "whatsapp.rar", base64.b64encode(raw).decode()
        )
        self.assertEqual(out, "Rejected: filename must end in .zip")

    async def test_symlink_member_is_rejected(self):
        bio = io.BytesIO()
        with zipfile.ZipFile(bio, "w") as zf:
            info = zipfile.ZipInfo("evil.opus")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, "target")

        with patch.object(server, "_run_batch", fake_run_batch):
            out = await server.transcribe_zip_base64(
                "symlink.zip", base64.b64encode(bio.getvalue()).decode()
            )
        self.assertIn("symlink entry is not allowed", out)

    async def test_predecode_limit_rejects_oversized_payload(self):
        raw = make_zip([("a.opus", b"abcdef")])
        with patch.object(server, "MAX_INLINE_ZIP_BYTES", 8):
            out = await server.transcribe_zip_base64(
                "small-limit.zip", base64.b64encode(raw).decode()
            )
        self.assertIn("payload exceeds 8 decoded bytes", out)


if __name__ == "__main__":
    unittest.main()
