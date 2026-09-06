"""Tests for the consolidation planning logic using a fake backend."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.base import MediaItem, BaseBackend, BackendError, Capabilities  # noqa: E402
from utils.hashing import sha256_stream  # noqa: E402
from utils.logger import Logger  # noqa: E402
from utils.manifest import RemovalManifest  # noqa: E402
from utils.validator import safe_upload, queue_removal, verify_upload  # noqa: E402


class FakeBackend(BaseBackend):
    """In-memory backend controllable per test."""

    def __init__(self, account_id: int, items=None, free: int = 0):
        super().__init__(account_id)
        self.items = (items or {}).copy()   # media_id -> bytes
        self.free = free

    def check_access(self):
        return None

    def capabilities(self):
        return Capabilities(app_created_only=True,
                            can_delete_library_items=False,
                            can_remove_from_albums=True,
                            originals_visible=False)

    def list_all(self):
        for mid, data in self.items.items():
            yield MediaItem(account_id=self.account_id, media_id=mid,
                            file_name=mid, media_type="PHOTO",
                            product_url=f"https://photos.google.com/photo/{mid}")

    def get_bytes(self, item):
        class _B:
            def __init__(self, data):
                self._data = data
            def read(self, n=-1):
                if not self._data:
                    return b""
                if n is None or n < 0:
                    out, self._data = self._data, b""
                    return out
                out, self._data = self._data[:n], self._data[n:]
                return out
            def close(self):
                self._data = b""
        return _B(self.items.get(item.media_id, b""))

    def get_hash(self, item):
        import io
        return sha256_stream(io.BytesIO(self.items.get(item.media_id, b"")))

    def create_from_stream(self, stream, file_name):
        data = b""
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            data += chunk
        self.items[file_name] = data
        return file_name

    def verify_present(self, media_id):
        return media_id in self.items

    def removal_url(self, item):
        return f"https://photos.google.com/photo/{item.media_id}"

    def get_free_storage(self):
        return self.free


class TestValidators(unittest.TestCase):
    def setUp(self):
        self.log = Logger("WARNING", "")

    def test_upload_then_verify_ok(self):
        src = FakeBackend(1, {"a.jpg": b"photodata"})
        tgt = FakeBackend(2)
        items = list(src.list_all())
        items[0].hash = src.get_hash(items[0])
        mid = safe_upload(src, items[0], tgt, "gpc_new/a", self.log, False)
        self.assertIsNotNone(mid)
        self.assertTrue(verify_upload(src, items[0], tgt, mid, self.log))
        self.assertEqual(tgt.items[mid], b"photodata")

    def test_corrupt_upload_fails_safely(self):
        # deliberate wrong hash -> verification fails -> safe_upload returns None
        data = b"aaaaaaaa"
        src = FakeBackend(1, {"nope.jpg": data})
        tgt = FakeBackend(2)
        item = list(src.list_all())[0]
        item.hash = "a" * 64  # wrong
        mid = safe_upload(src, item, tgt, "x", self.log, False)
        self.assertIsNone(mid)

    def test_queue_removal_writes_manifest(self):
        import json as _json
        src = FakeBackend(1, {"b.jpg": b"zz"})
        item = list(src.list_all())[0]
        item.hash = "b" * 64
        with tempfile_dir() as d:
            mf = RemovalManifest(os.path.join(d, "m.jsonl"))
            queue_removal(src, item, mf, self.log, False, "duplicate")
            self.assertEqual(mf.count(), 1)
            line = open(os.path.join(d, "m.jsonl"), encoding="utf-8").read()
            entry = _json.loads(line)
            # privacy: manifest must only carry the fixed, safe fields
            self.assertTrue(set(entry.keys()) <=
                            {"ts", "account", "hash", "url", "reason"})
            self.assertEqual(entry["account"], 1)
            self.assertEqual(len(entry["hash"]), 8)
            self.assertTrue(entry["url"].startswith("https://photos.google.com/"))

    def test_dry_run_touches_nothing(self):
        with tempfile_dir() as d:
            src = FakeBackend(1, {"c.jpg": b"yy"})
            tgt = FakeBackend(2)
            mf = RemovalManifest(os.path.join(d, "m.jsonl"))
            item = list(src.list_all())[0]
            item.hash = src.get_hash(item)
            mid = safe_upload(src, item, tgt, "gpc_" + item.hash,
                              self.log, True)
            self.assertIsNone(mid)
            self.assertEqual(tgt.items, {})
            queue_removal(src, item, mf, self.log, True, "duplicate")
            self.assertEqual(mf.count(), 0)


class TestConsolidation(unittest.TestCase):
    def setUp(self):
        self.log = Logger("WARNING", "")

    def test_idempotent_name_index(self):
        from main import marker_name, _register
        cfg = {"consolidation": {"marker_prefix": "gpc_"}}
        item = MediaItem(1, "a.jpg", "a.jpg")
        item.hash = "ab" * 32
        name_index = {}
        _register(name_index, 2, "id-1", marker_name(cfg, item), "PHOTO")
        self.assertIn(marker_name(cfg, item), name_index)
        self.assertEqual(name_index[marker_name(cfg, item)].media_id, "id-1")

    def test_full_two_account_consolidation(self):
        """Simulate main.py's pipeline end-to-end with in-memory data."""
        import tempfile
        from main import compute_hashes, handle_duplicate_group, handle_unique

        DUPLICATE = b"same-photo-bytes"
        B = b"unique-in-target"
        C = b"unique-in-source"

        acct1 = FakeBackend(1, {"dupA.jpg": DUPLICATE, "B.jpg": B}, free=100)
        acct2 = FakeBackend(2, {"dupA2.jpg": DUPLICATE,
                                "dupA3.jpg": DUPLICATE, "C.jpg": C}, free=50)
        backends = [acct1, acct2]

        groups = compute_hashes(backends, self.log, 2)

        name_index = {}
        for copies in groups.values():
            for acct, item in copies:
                if acct == 1:
                    name_index.setdefault(item.file_name, item)

        cfg = {"consolidation": {"marker_prefix": "gpc_"},
               "logging": {}}
        with tempfile.TemporaryDirectory() as d:
            mf = RemovalManifest(os.path.join(d, "m.jsonl"))
            for hash_, copies in groups.items():
                if len(copies) > 1:
                    handle_duplicate_group(cfg, backends, 1, hash_, copies,
                                           name_index, mf, self.log, False, [])
                else:
                    handle_unique(cfg, backends, 1, hash_, copies,
                                  name_index, mf, self.log, False, [])

            # The duplicate photo must exist exactly once inside the target
            # (kept under its original name because it was already there).
            dup = [k for k, v in acct1.items.items() if v == DUPLICATE]
            self.assertEqual(len(dup), 1)
            # Unique source photo moved into the target.
            self.assertTrue(any(v == C for v in acct1.items.values()))
            # No API delete exists: source copies stay, but are recorded.
            self.assertEqual(len(acct2.items), 3)
            self.assertGreaterEqual(mf.count(), 3)  # all removable copies queued
            # Target still has its own unique photo.
            self.assertTrue(any(v == B for v in acct1.items.values()))

            # Idempotency: a second identical run must NOT re-upload.
            groups2 = compute_hashes(backends, self.log, 2)
            count_before = len(acct1.items)

            def raise_upload(*args, **kwargs):
                raise AssertionError("re-run must not upload duplicates")
            original = __import__("main").safe_upload
            __import__("main").safe_upload = raise_upload
            mf2 = RemovalManifest(os.path.join(d, "m2.jsonl"))
            try:
                name_index2 = {}
                for copies in groups2.values():
                    for acct, item in copies:
                        if acct == 1:
                            name_index2.setdefault(item.file_name, item)
                for hash_, copies in groups2.items():
                    if len(copies) > 1:
                        handle_duplicate_group(cfg, backends, 1, hash_, copies,
                                               name_index2, mf2, self.log, False, [])
                    else:
                        handle_unique(cfg, backends, 1, hash_, copies,
                                      name_index2, mf2, self.log, False, [])
            finally:
                __import__("main").safe_upload = original
            self.assertEqual(len(acct1.items), count_before)


class _tempdir:
    def __enter__(self):
        import tempfile
        self._d = tempfile.TemporaryDirectory()
        return self._d.name

    def __exit__(self, *a):
        self._d.cleanup()


def tempfile_dir():
    return _tempdir()


if __name__ == "__main__":
    unittest.main()