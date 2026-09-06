"""Privacy-safe logging.

POLICY (enforced here, single choke point for all output):
  * Only operational counts and abstract identifiers are logged.
  * A "hash" is always truncated to the first 8 hex chars.
  * Accounts are referred to by their internal numeric id only.
  * Never log: real file names, emails, tokens, paths, photo bytes.
  * Any string that must be echoed back (e.g. backend error text) is run
    through `redact()` first, which strips email-like tokens.
"""
from __future__ import annotations

import logging
import re

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
# Heuristic to strip drive/photos resource URLs full of opaque tokens.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{20,}")

HASH_LEN = 8


def redact(text: str) -> str:
    """Strip obvious personal data (emails, long opaque tokens) from text."""
    out = _EMAIL_RE.sub("<redacted>", text)
    out = _TOKEN_RE.sub("<redacted>", out)
    return out


def short_hash(hex_digest: str) -> str:
    """Return the first 8 chars of a 64-char SHA-256 digest."""
    if not hex_digest:
        return "?"
    return hex_digest[:HASH_LEN]


class Logger:
    """Thin wrapper around stdlib logging with redaction guarantees.

    Use the structured helpers (`op`, `check`, etc.) instead of `raw` so
    that nothing sensitive leaks by accident.
    """

    def __init__(
        self, level: str = "INFO", log_file: str = "", name: str = "gpc"
    ) -> None:
        self._log = logging.getLogger(name)
        self._log.setLevel(getattr(logging, level.upper(), logging.INFO))
        self._log.handlers.clear()

        fmt = logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"
        )
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        self._log.addHandler(console)

        if log_file:
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setFormatter(fmt)
            self._log.addHandler(fh)

        self._log.propagate = False

    @property
    def logger(self) -> logging.Logger:
        return self._log

    def raw(self, level: str, message: str) -> None:
        self._log.log(getattr(logging, level.upper(), logging.INFO),
                      redact(message))

    def info(self, message: str) -> None:
        self._log.info(redact(message))

    def warning(self, message: str) -> None:
        self._log.warning(redact(message))

    def error(self, message: str) -> None:
        self._log.error(redact(message))

    # -- structured helpers ----------------------------------------------
    def op(
        self,
        kind: str,
        account: int,
        hash_: str,
        status: str,
        detail: str = "",
    ) -> None:
        """Log one operation: upload/delete, account id, truncated hash."""
        base = f"[op] kind={kind} account={account} hash={short_hash(hash_)} status={status}"
        if detail:
            base = f"{base} detail={redact(detail)}"
        self._log.info(base)

    def check(self, what: str, ok: bool) -> None:
        self._log.info("[check] %s ok=%s", what, "yes" if ok else "NO")

    def count(self, what: str, value) -> None:
        self._log.info("[count] %s=%s", what, value)

    def summary(self, what: str, value) -> None:
        self._log.info("[summary] %s=%s", what, value)