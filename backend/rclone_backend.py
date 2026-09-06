"""rclone backend.

Runs `rclone` as a subprocess against a pre-configured "google photos"
remote. Byte content is streamed through pipes (never persisted), except
during upload where a temporary file is used and deleted immediately.

PLATFORM FACTS (verified 2026, same as the OAuth backend):
  * From March 31, 2025 rclone can only download photos that rclone (the
    app) itself uploaded (Google policy change; new scopes are
    `photoslibrary.appendonly`, `.readonly.appcreateddata`,
    `.edit.appcreateddata`). Existing user photos are not listable.
  * `rclone deletefile` only works for files under an album the app
    created, and even then the media item is only removed from the album,
    never from the library.
  * `rclone about` is unsupported on the photos backend -> free space is
    read from a Drive remote (storage_rclone) when supplied.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from typing import BinaryIO, Iterator, Optional

from backend.base import BackendError, BaseBackend, Capabilities, MediaItem
from utils.hashing import sha256_stream_size

_UNITS = {"B": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3,
          "T": 1024 ** 4, "P": 1024 ** 5}
_FREE_RE = re.compile(r"Free:\s+([\d.]+)\s*([BKMGT]?)", re.IGNORECASE)
_RETRY_HINTS = ("429", "too many requests", "rate limit", "backoff")


class RcloneResult:
    def __init__(self, stdout: bytes, stderr: bytes, code: int) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.code = code


class RcloneBackend(BaseBackend):
    def __init__(
        self,
        account_id: int,
        remote: str,
        storage_remote: Optional[str],
        retries: int = 5,
        backoff_base: float = 2.0,
        rclone_bin: str = "rclone",
    ) -> None:
        super().__init__(account_id)
        self._remote = remote.rstrip(":")
        self._storage_remote = (storage_remote or "").rstrip(":") or None
        self._retries = retries
        self._backoff = backoff_base
        self._bin = rclone_bin if shutil.which(rclone_bin) else ""
        if not self._bin:
            raise BackendError("rclone executable not found on PATH")

    # -- process helpers -------------------------------------------------
    def _run(self, args, allow_fail: bool = False) -> RcloneResult:
        cmd = [self._bin] + list(args)
        last_err: Optional[Exception] = None
        for attempt in range(self._retries):
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            out, err = proc.communicate()
            if proc.returncode == 0:
                return RcloneResult(out, err, 0)
            hint = err.decode("utf-8", "replace").lower()
            if any(h in hint for h in _RETRY_HINTS):
                last_err = BackendError(
                    f"rclone rate-limited: "
                    f"{err.decode('utf-8', 'replace')[:200]}"
                )
                time.sleep(self._backoff ** attempt)
                continue
            if allow_fail:
                return RcloneResult(out, err, proc.returncode)
            raise BackendError(
                f"rclone {' '.join(args)} failed: "
                f"{err.decode('utf-8', 'replace')[:300]}"
            )
        raise last_err or BackendError(
            f"rclone {' '.join(args)}: retries exhausted")

    def _stream(self, args) -> subprocess.Popen:
        return subprocess.Popen(
            [self._bin] + list(args),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    # -- discovery -------------------------------------------------------
    def check_access(self) -> None:
        self._run(["lsd", f"{self._remote}:", "--max-depth", "1"])

    def capabilities(self) -> Capabilities:
        return Capabilities(
            app_created_only=True,
            can_delete_library_items=False,
            can_remove_from_albums=True,   # album remove only, not library
            originals_visible=False,
        )

    def list_all(self) -> Iterator[MediaItem]:
        args = [
            "lsjson", f"{self._remote}:", "-R", "--files-only",
            "--no-modtime", "--no-mimetype", "--fast-list",
        ]
        result = self._run(args)
        records = json.loads(result.stdout.decode("utf-8", "replace") or "[]")
        for rec in records:
            path = rec.get("Path", "")
            if not path:
                continue
            yield MediaItem(
                account_id=self.account_id,
                media_id=path,
                file_name=rec.get("Name") or path.rsplit("/", 1)[-1],
                media_type="VIDEO" if str(rec.get("MimeType", "")).startswith("video") else "PHOTO",
                size=int(rec.get("Size", 0) or 0),
            )

    # -- content ---------------------------------------------------------
    def get_bytes(self, item: MediaItem) -> BinaryIO:
        proc = self._stream(["cat", f"{self._remote}:{item.media_id}"])
        return _ProcReader(proc)

    def get_hash(self, item: MediaItem) -> str:
        proc = self._stream(["cat", f"{self._remote}:{item.media_id}"])
        try:
            digest, size = sha256_stream_size(_ProcReader(proc))
            item.size = size
            proc.wait()
            if proc.returncode != 0:
                raise BackendError(
                    f"rclone cat failed for {item.media_id[:40]}"
                )
            return digest
        finally:
            _kill(proc)

    def create_from_stream(self, stream: BinaryIO, file_name: str) -> str:
        # Upload through a temporary file (deleted immediately afterwards);
        # rclone has no portable stdin-upload for this backend.
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="gpc_", suffix=file_name, delete=False
            ) as fh:
                tmp = fh.name
                while True:
                    block = stream.read(1024 * 1024)
                    if not block:
                        break
                    fh.write(block)
            self._run(["copyto", tmp, f"{self._remote}:{file_name}"])
            path = self._find_path(file_name) or file_name
            return path
        except BackendError as exc:
            raise BackendError(
                f"upload to {self._remote} unsupported by backend: {exc}"
            ) from exc
        finally:
            if tmp:
                try:
                    import os
                    os.remove(tmp)
                except OSError:
                    pass

    # -- mutation --------------------------------------------------------
    def _find_path(self, file_name: str) -> Optional[str]:
        """Resolve an item's current path (the backend re-organizes names)."""
        result = self._run(["lsjson", f"{self._remote}:", "-R",
                            "--files-only", "--fast-list"])
        records = json.loads(result.stdout.decode("utf-8", "replace") or "[]")
        for rec in records:
            if rec.get("Name") == file_name or rec.get("Path") == file_name:
                return rec.get("Path")
        return None

    def verify_present(self, media_id: str) -> bool:
        return self._find_path(media_id) is not None

    def removal_url(self, item: MediaItem) -> Optional[str]:
        # rclone/google-photos exposes no usable per-item web link.
        return None

    # -- storage ---------------------------------------------------------
    def get_free_storage(self) -> int:
        targets = []
        if self._storage_remote:
            targets.append(f"{self._storage_remote}:")
        targets.append(f"{self._remote}:")

        last = None
        for target in targets:
            result = self._run(["about", target], allow_fail=True)
            if result.code != 0:
                last = target
                continue
            text = result.stdout.decode("utf-8", "replace")
            m = _FREE_RE.search(text)
            if not m:
                continue
            value = float(m.group(1))
            unit = m.group(2).upper() or "B"
            return int(value * _UNITS[unit])
        raise BackendError(
            "free storage undeterminable via rclone about "
            f"(tried {last}); set storage_rclone to a Google Drive remote"
        )


class _ProcReader:
    """Read-only file-like over a streaming subprocess's stdout."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self._proc = proc

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1):
        return self._proc.stdout.read(size)

    def close(self) -> None:
        if self._proc.poll() is None:
            _kill(self._proc)


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass