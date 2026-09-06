"""Unit tests for the pure utility modules (no network, no credentials)."""
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.hashing import hash_copy, sha256_file, sha256_stream  # noqa: E402
from utils.logger import Logger, redact, short_hash  # noqa: E402


class TestHashing(unittest.TestCase):
    def test_stream_and_file_agree(self):
        payload = os.urandom(3 * 1024 * 1024)  # 3 MiB
        with tempfile.NamedTemporaryFile() as fh:
            fh.write(payload)
            fh.flush()
            file_hash = sha256_file(fh.name)
        stream_hash = sha256_stream(io.BytesIO(payload))
        self.assertEqual(file_hash, stream_hash)

    def test_known_sha256(self):
        self.assertEqual(
            sha256_stream(io.BytesIO(b"abc")),
            "ba7816bf8f01cfea414140de5dae2223"
            "b00361a396177a9cb410ff61f20015ad",
        )

    def test_hash_copy(self):
        out = []
        digest, size = hash_copy(io.BytesIO(b"hello"), out.append)
        self.assertEqual(size, 5)
        self.assertEqual(digest, sha256_stream(io.BytesIO(b"hello")))
        self.assertEqual(b"".join(out), b"hello")


class TestLogger(unittest.TestCase):
    def test_redact_email_and_tokens(self):
        out = redact("user@example.com used token ABCDEFGHIJKLMNOP1234567890")
        self.assertNotIn("user@example.com", out)
        self.assertNotIn("ABCDEFGHIJKLMNOP1234567890", out)

    def test_short_hash_truncates(self):
        self.assertEqual(short_hash("a1b2c3d4e5f6..." * 8), "a1b2c3d4")

    def test_redact_keeps_plain_text(self):
        self.assertEqual(redact("just numbers: 12 34"), "just numbers: 12 34")


if __name__ == "__main__":
    unittest.main()