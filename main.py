#!/usr/bin/env python3
"""Consolidate unique photos across N Google Photos accounts into the
account with the most free storage.

Backends are interchangeable: `oauth` (Google Photos Library API) or
`rclone`. Choose via config.yaml, `--backend`, or env GPC_BACKEND.

PLATFORM REALITY (2026): since 2025-03-31 Google's Library API only sees
app-created content and exposes NO deletion endpoint (issue #109759781).
This tool therefore:
  * uploads + dedupes what the API permits, and
  * writes a private removal manifest (JSONL, web links) for the items the
    user must delete manually in the Google Photos web UI.

Safety guarantees:
  * --dry-run      preview only (listing + hashing + plan, no mutations)
  * idempotent     re-runs detect existing consolidated copies (marker names)
  * self-checking  every upload is hash-verified before anything is removed
  * privacy        only numeric account ids and truncated hashes are logged
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

import yaml

from backend.base import BackendError, BaseBackend, MediaItem
from utils.logger import Logger
from utils.manifest import RemovalManifest
from utils.storage import choose_target, free_storage_bytes
from utils.validator import queue_removal, safe_upload

DEFAULT_CONFIG = "config.yaml"


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------
def load_config(path: str) -> dict:
    if not os.path.exists(path):
        raise SystemExit(f"config not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg.setdefault("accounts", [])
    if not cfg["accounts"]:
        raise SystemExit("config.yaml: at least one account is required")
    ids = [a["id"] for a in cfg["accounts"]]
    if len(set(ids)) != len(ids):
        raise SystemExit("config.yaml: duplicate account ids")
    cfg.setdefault("consolidation", {})
    cfg.setdefault("oauth", {})
    cfg.setdefault("logging", {})
    return cfg


def build_backends(kind: str, cfg: dict, only_accounts: Optional[List[int]]):
    """Instantiate one backend per selected account (config order)."""
    backends: List[BaseBackend] = []
    cons = cfg["consolidation"]
    for a in cfg["accounts"]:
        if only_accounts and a["id"] not in only_accounts:
            continue
        idx = int(a["id"])
        if kind == "oauth":
            from backend.google_oauth import GooglePhotosOAuthBackend

            default_dir = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "tokens"
            )
            backends.append(GooglePhotosOAuthBackend(
                account_id=idx,
                token_file=a.get("oauth_token", f"account_{idx}.json"),
                client_secret_env=cfg["oauth"].get(
                    "client_secret_env", "GPC_OAUTH_CLIENT_SECRET"),
                token_dir_env=cfg["oauth"].get("token_dir_env", "") or "",
                token_dir_default=default_dir,
                photos_scopes=cfg["oauth"].get("photos_scopes", [
                    "https://www.googleapis.com/auth/photoslibrary.appendonly",
                    "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata",
                    "https://www.googleapis.com/auth/photoslibrary.edit.appcreateddata",
                ]),
                storage_scope=cfg["oauth"].get(
                    "storage_scope",
                    "https://www.googleapis.com/auth/drive.readonly"),
                retries=cons.get("retries", 5),
                backoff_base=cons.get("backoff_base", 2.0),
            ))
        elif kind == "rclone":
            from backend.rclone_backend import RcloneBackend

            backends.append(RcloneBackend(
                account_id=idx,
                remote=a.get("rclone_remote", ""),
                storage_remote=a.get("storage_rclone"),
                retries=cons.get("retries", 5),
                backoff_base=cons.get("backoff_base", 2.0),
            ))
        else:
            raise SystemExit(f"unknown backend: {kind}")
    return backends


# --------------------------------------------------------------------------
# hashing phase
# --------------------------------------------------------------------------
def compute_hashes(backends, log: Logger,
                   concurrency: int) -> Dict[str, List[Tuple[int, MediaItem]]]:
    """List every account and SHA-256 every item, then group by hash."""
    backends_by_id = {b.account_id: b for b in backends}

    items: List[MediaItem] = []
    for b in backends:
        for item in b.list_all():
            items.append(item)
    log.count("listed_items", len(items))

    groups: Dict[str, List[Tuple[int, MediaItem]]] = defaultdict(list)

    def digest(idx: int):
        item = items[idx]
        h = _hash_with_retry(backends_by_id[item.account_id], item, log)
        item.hash = h
        return idx

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = [pool.submit(digest, i) for i in range(len(items))]
        for fut in as_completed(futures):
            i = fut.result()
            item = items[i]
            groups[item.hash].append((item.account_id, item))

    log.count("unique_hashes", len(groups))
    return groups


def _hash_with_retry(backend: BaseBackend, item: MediaItem, log: Logger) -> str:
    for attempt in range(3):
        try:
            return backend.get_hash(item)
        except BackendError as exc:
            if attempt == 2:
                log.error(f"hash failed account={item.account_id} "
                          f"media={item.media_id[:24]}: {exc}")
                raise
            time.sleep(2 ** attempt)
    raise BackendError("unreachable")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _backend_for(backends, account_id) -> BaseBackend:
    return next(b for b in backends if b.account_id == account_id)


def marker_name(cfg: dict, item: MediaItem) -> str:
    prefix = cfg["consolidation"].get("marker_prefix", "gpc_")
    return f"{prefix}{item.hash}{item.extension}"


def _register(name_index: dict, target_idx: int, media_id: str,
              file_name: str, media_type: str) -> None:
    name_index[file_name] = MediaItem(
        account_id=target_idx, media_id=media_id,
        file_name=file_name, media_type=media_type,
    )


# --------------------------------------------------------------------------
# consolidation of one hash group
# --------------------------------------------------------------------------
def handle_duplicate_group(
    cfg, backends, target_idx, hash_, copies, name_index,
    manifest, log, dry_run, errors,
) -> None:
    """One identical hash found in >=2 places: keep exactly one in target.

    Copy that must be deleted is RECORDED in the removal manifest (Google
    provides no API delete); the item itself is never destroyed here.
    """
    target_backend = _backend_for(backends, target_idx)
    marker = marker_name(cfg, copies[0][1])

    kept = next((c for c in copies if c[0] == target_idx), copies[0])
    uploaded_id = None

    # 1) Ensure a target copy exists (marker-named copies are the idempotency key)
    if kept[0] != target_idx:
        existing = name_index.get(marker)
        if existing is not None:
            uploaded_id = existing.media_id          # previous run's work
        else:
            source = _backend_for(backends, kept[0])
            uploaded_id = safe_upload(source, kept[1], target_backend,
                                      marker, log, dry_run)
            if uploaded_id is None and not dry_run:
                errors.append(("duplicate", hash_,
                               "upload/verify failed, source preserved"))
                return
            if uploaded_id:
                _register(name_index, target_idx, uploaded_id, marker,
                          kept[1].media_type)

    # 2) Queue every copy except the single kept one for manual removal.
    keep = (kept[0], kept[1].media_id)
    if kept[0] != target_idx and uploaded_id:
        keep = (target_idx, uploaded_id)  # now lives in the target
    for acct, item in copies:
        item.hash = hash_
        if (acct, item.media_id) == keep:
            continue
        source = _backend_for(backends, acct)
        queue_removal(source, item, manifest, log, dry_run, "duplicate")


def handle_unique(
    cfg, backends, target_idx, hash_, copies, name_index,
    manifest, log, dry_run, errors,
) -> None:
    """A photo that exists only in one (non-target) account -> move it."""
    acct, item = copies[0]
    if acct == target_idx:
        return  # already in the target, nothing to do

    target_backend = _backend_for(backends, target_idx)
    marker = marker_name(cfg, item)

    existing = name_index.get(marker)
    uploaded_id = existing.media_id if existing is not None else None
    if uploaded_id is None:
        source = _backend_for(backends, acct)
        uploaded_id = safe_upload(source, item, target_backend, marker,
                                  log, dry_run)
        if uploaded_id is None and not dry_run:
            errors.append(("unique", hash_,
                           "upload/verify failed, source preserved"))
            return
        if uploaded_id:
            _register(name_index, target_idx, uploaded_id, marker,
                      item.media_type)

    source = _backend_for(backends, acct)
    item.hash = hash_
    queue_removal(source, item, manifest, log, dry_run, "unique-move")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--dry-run", action="store_true",
                        help="plan only: no uploads or deletions")
    parser.add_argument("--backend", default=None,
                        help="override backend (oauth|rclone); "
                             "env GPC_BACKEND also works")
    parser.add_argument("--accounts", default=None,
                        help="comma-separated account ids to include")
    parser.add_argument("--log-level", default=None)
    args = parser.parse_args()

    cfg = load_config(args.config)
    kind = (args.backend or os.environ.get("GPC_BACKEND")
            or cfg.get("backend", "oauth"))
    if args.log_level:
        cfg["logging"]["log_level"] = args.log_level

    only_accounts = None
    if args.accounts:
        only_accounts = [int(x) for x in args.accounts.split(",") if x.strip()]
    elif os.environ.get("GPC_ACCOUNTS"):
        only_accounts = [int(x) for x in
                         os.environ["GPC_ACCOUNTS"].split(",") if x.strip()]

    log = Logger(
        level=cfg["logging"].get("log_level", "INFO"),
        log_file=cfg["logging"].get("log_file", ""),
    )
    dry_run = args.dry_run
    if dry_run:
        log.info("DRY-RUN mode: no uploads or deletions will happen")

    # -- accounts must ALL be reachable, else abort -----------------------
    try:
        backends = build_backends(kind, cfg, only_accounts)
    except (BackendError, SystemExit) as exc:
        log.error(f"backend init failed: {exc}")
        return 2
    for b in backends:
        try:
            b.check_access()
            caps = b.capabilities()
            log.info(f"account {b.account_id}: reachable, "
                     f"free {free_storage_bytes(b)} bytes")
            log.info(f"account {b.account_id} capabilities: app_created_only="
                     f"{caps.app_created_only} api_delete={True if caps.can_delete_library_items else False}"
                     f" originals_visible={caps.originals_visible}")
        except BackendError as exc:
            log.error(f"account {b.account_id} inaccessible: aborting ({exc})")
            return 2

    # -- removal manifest (Google exposes no API delete) ------------------
    manifest_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        cfg["consolidation"].get("manifest_file",
                                 "removal_manifest.jsonl"),
    )
    manifest = RemovalManifest(manifest_path)
    log.info(f"removal manifest: {manifest_path}")

    # -- choose target (most free space) ----------------------------------
    target_backend = choose_target(backends)
    target_idx = target_backend.account_id
    log.info(f"target account: {target_idx}")

    # -- list + hash everything, group by sha256 --------------------------
    concurrency = cfg["consolidation"].get("concurrency", 4)
    groups = compute_hashes(backends, log, concurrency)
    if not groups:
        log.info("no photos found")
        return 0

    # idempotency index: file_name -> existing target item (groups already
    # contain every item, including marker copies from previous runs).
    name_index: Dict[str, MediaItem] = {}
    for copies in groups.values():
        for acct, item in copies:
            if acct == target_idx:
                name_index.setdefault(item.file_name, item)

    # -- execute ----------------------------------------------------------
    errors: List[Tuple[str, str, str]] = []
    counts = defaultdict(int)
    for hash_ in sorted(groups.keys()):
        copies = groups[hash_]
        if len(copies) > 1:
            handle_duplicate_group(cfg, backends, target_idx, hash_, copies,
                                   name_index, manifest, log, dry_run, errors)
            counts["duplicate_groups"] += 1
        else:
            handle_unique(cfg, backends, target_idx, hash_, copies,
                          name_index, manifest, log, dry_run, errors)
            counts["unique_groups"] += 1

    # -- summary ----------------------------------------------------------
    log.summary("photos_hashed", sum(len(v) for v in groups.values()))
    log.summary("duplicate_groups", counts["duplicate_groups"])
    log.summary("unique_groups", counts["unique_groups"])
    if not dry_run:
        log.summary("queued_for_manual_removal", manifest.count())
    log.summary("errors", len(errors))
    for b in backends:
        try:
            log.summary("storage_after_bytes",
                        f"{b.account_id}={free_storage_bytes(b)}")
        except BackendError:
            log.warning(f"storage after: account {b.account_id} unavailable")
    for err in errors:
        log.error(f"op={err[0]} hash={err[1][:8]} reason={err[2]}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())