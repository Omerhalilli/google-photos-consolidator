"""Self-checking utilities.

Every critical operation is followed by a verification:

  * upload -> the copy in the target account must hash-match the source
  * deletion candidate -> recorded in the private removal manifest, because
    Google's Library API exposes NO deletion (see backend/base.py)

If a check fails the caller must NOT take the destructive next step and
logs an error instead (data is then preserved rather than lost).
"""
from __future__ import annotations

from typing import Optional

from backend.base import BackendError, BaseBackend, MediaItem
from utils.logger import Logger
from utils.manifest import RemovalManifest


def verify_upload(
    source: BaseBackend,
    source_item: MediaItem,
    target: BaseBackend,
    target_media_id: str,
    log: Logger,
) -> bool:
    """Ensure the newly-uploaded copy truly exists and matches the source.

    The integrity check uses the bytes stored in the TARGET account, so a
    silent/corrupt upload cannot slip through.
    """
    try:
        probe = MediaItem(
            account_id=target.account_id,
            media_id=target_media_id,
            file_name=source_item.file_name,
            media_type=source_item.media_type,
        )
        if not target.verify_present(target_media_id):
            log.check("upload presence", False)
            return False
        ok = target.get_hash(probe) == source_item.hash
        log.check(
            f"upload hash target={target.account_id} "
            f"media={target_media_id[:24]}", ok
        )
        return ok
    except Exception as exc:  # noqa: BLE001  (fail-safe: never move on doubt)
        log.error(f"verify_upload failure: {type(exc).__name__}")
        return False


def queue_removal(
    backend: BaseBackend,
    item: MediaItem,
    manifest: RemovalManifest,
    log: Logger,
    dry_run: bool,
    reason: Optional[str] = None,
) -> bool:
    """Record an item for manual deletion in the Google Photos web UI.

    Google's API cannot delete library items; the web UI is the only
    sanctioned channel. The manifest holds the clickable link + id so the
    user can finish removal quickly. On dry-run nothing is recorded.
    """
    if dry_run:
        log.op("removal-queued", backend.account_id, item.hash,
               "skipped-dryrun")
        return False

    url = None
    try:
        url = backend.removal_url(item)
    except Exception:  # noqa: BLE001
        url = None
    manifest.add(backend.account_id, item.hash, url, reason or "consolidate")
    log.op("removal-queued", backend.account_id, item.hash, "ok",
           "manual step in Photos web UI")
    return True


def safe_upload(
    source: BaseBackend,
    source_item: MediaItem,
    target: BaseBackend,
    file_name: str,
    log: Logger,
    dry_run: bool,
) -> Optional[str]:
    """Upload a photo to `target`, verify it, return the new media_id.

    Returns None on dry-run or failure. On failure the caller must not
    queue anything for removal.
    """
    log.op("upload", target.account_id, source_item.hash, "started")
    if dry_run:
        log.op("upload", target.account_id, source_item.hash, "skipped-dryrun")
        return None

    stream = None
    try:
        stream = source.get_bytes(source_item)
        media_id = target.create_from_stream(stream, file_name)
        stream.close()
        stream = None

        if not verify_upload(source, source_item, target, media_id, log):
            raise BackendError("upload verification failed - source protected")
        log.op("upload", target.account_id, source_item.hash, "ok",
               f"media={media_id[:24]}")
        return media_id
    except BackendError as exc:
        log.op("upload", target.account_id, source_item.hash, "error",
               str(exc))
        return None
    finally:
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass