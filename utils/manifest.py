"""Private, local-only removal manifest.

Since 2025-03-31 Google's Library API has NO deletion endpoint (Google
closed issue #109759781; the web UI is the only sanctioned channel). This
tool therefore records, per item to be deleted, an entry in a local JSONL
file. The user opens the web links and deletes the items there.

Privacy:
  * Written with 0600 permissions, never committed to git (.gitignore).
  * Entries contain only: internal account id, truncated hash, extension,
    a Photos web URL and a reason code. No emails, tokens or content.
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional


class RemovalManifest:
    """Append-only JSONL file of items the user should delete manually."""

    def __init__(self, path: str) -> None:
        self._path = path

    def add(
        self,
        account_id: int,
        hash_: str,
        url: Optional[str],
        reason: str,
    ) -> None:
        entry = {
            "ts": int(time.time()),
            "account": int(account_id),
            "hash": (hash_ or "")[:8],
            "url": url or None,
            "reason": reason,
        }
        fd = os.open(self._path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError:
            os.close(fd)  # noqa: B023  (belongs to this method)

    def count(self) -> int:
        if not os.path.exists(self._path):
            return 0
        with open(self._path, "r", encoding="utf-8") as fh:
            return sum(1 for _ in fh)