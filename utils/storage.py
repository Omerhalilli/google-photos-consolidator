"""Storage-quota logic used to select the consolidation target account."""
from __future__ import annotations

from typing import List

from backend.base import BackendError, BaseBackend


def free_storage_bytes(backend: BaseBackend) -> int:
    """Return free storage for one backend, raising BackendError if unknown.

    By design the process ABORTS when any account's free space can't be
    determined: the target selection would be unreliable otherwise.
    """
    free = backend.get_free_storage()
    if free is None or free < 0:
        raise BackendError(
            f"free storage undeterminable for account {backend.account_id}"
        )
    return free


def choose_target(backends: List[BaseBackend]) -> BaseBackend:
    """Return the account with the largest free storage."""
    best, best_free = None, -1
    for backend in backends:
        free = free_storage_bytes(backend)
        if free > best_free:
            best, best_free = backend, free
    assert best is not None
    return best