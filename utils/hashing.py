"""SHA-256 helpers. No file content is ever persisted by this module."""
from __future__ import annotations

import hashlib
from typing import BinaryIO, Callable, Tuple

CHUNK = 1024 * 1024  # 1 MiB


def sha256_file(path: str) -> str:
    """Return the SHA-256 hex digest of a file on disk (streamed)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def sha256_stream(stream: BinaryIO) -> str:
    """Return the SHA-256 hex digest of a binary stream (streamed)."""
    digest = hashlib.sha256()
    while True:
        block = stream.read(CHUNK)
        if not block:
            break
        digest.update(block)
    return digest.hexdigest()


def hash_copy(
    src: BinaryIO, sink: Callable[[bytes], None]
) -> Tuple[str, int]:
    """Read src fully, forwarding each chunk to sink, return (hash, size).

    Used to copy a photo between accounts while computing the digest of the
    transported bytes, so the validator can check integrity of the *copy*
    that was actually uploaded (not just the source).
    """
    digest = hashlib.sha256()
    size = 0
    while True:
        block = src.read(CHUNK)
        if not block:
            break
        digest.update(block)
        sink(block)
        size += len(block)
    return digest.hexdigest(), size