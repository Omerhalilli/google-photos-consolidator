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
from concurrent.futures import ThreadPoolExecutor
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
    """List every account and SHA-256 every item, then group by hash.

    Memory stays bounded on huge libraries: futures are submitted in a
    fixed-size window (back-pressured) instead of one per item, and hashing
    progress is reported every `PROGRESS_EVERY` items.
    """
    backends_by_id = {b.account_id: b for b in backends}

    items: List[MediaItem] = []
    for b in backends:
        for item in b.list_all():
            items.append(item)
    log.count("listed_items", len(items))
    if not items:
        return {}

    groups: Dict[str, List[Tuple[int, MediaItem]]] = defaultdict(list)
    total = len(items)
    done = 0

    def digest(slice_):
        for item in slice_:
            item.hash = _hash_with_retry(
                backends_by_id[item.account_id], item, log)
        return slice_

    batch = max(1, concurrency * 4)
    window = max(2, concurrency * 2)

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        in_flight = []
        for offset in range(0, total, batch):
            fut = pool.submit(digest, items[offset:offset + batch])
            in_flight.append(fut)
            if len(in_flight) >= window:          # back-pressure
                results = in_flight.pop(0).result()
                for item in results:
                    groups[item.hash].append((item.account_id, item))
                done += len(results)
                _log_progress(log, done, total)
        for fut in in_flight:
            results = fut.result()
            for item in results:
                groups[item.hash].append((item.account_id, item))
            done += len(results)
            _log_progress(log, done, total)

    log.count("unique_hashes", len(groups))
    return groups


def _log_progress(log: Logger, done: int, total: int) -> None:
    if done == total or done % 100 == 0:
        log.count("hashed", f"{done}/{total}")


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


def fmt_bytes(n: int) -> str:
    """Human-readable byte count (privacy-safe: numbers only)."""
    n = max(int(n or 0), 0)
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    for unit in units:
        if n < 1024 or unit == units[-1]:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024.0
    return f"{n} B"


def plan_stats(
    groups: Dict[str, List[Tuple[int, MediaItem]]], target_idx: int
) -> Tuple[int, int]:
    """Estimate (bytes to move to target, bytes removable from sources).

    `move_bytes` is what must fit into the target's free space; `freeable`
    is the storage Google could free if the user deletes the manifest items.
    Sizes are always known because get_hash() captures them while streaming.
    """
    move_bytes = 0
    freeable = 0
    for hash_, copies in groups.items():
        if len(copies) == 1:
            acct, item = copies[0]
            if acct != target_idx:
                move_bytes += item.size
                freeable += item.size
            continue
        target_copy = next((c for c in copies if c[0] == target_idx), None)
        size_kept = copies[0][1].size        # identical bytes -> same size
        if target_copy is None:
            move_bytes += size_kept
        keep_key = None
        if target_copy is not None:
            keep_key = (target_copy[0], target_copy[1].media_id)
        for acct, item in copies:
            if (acct, item.media_id) != keep_key:
                freeable += item.size
    return move_bytes, freeable


def preflight_move(
    backends, groups, target_idx, log: Logger, dry_run: bool
) -> Optional[int]:
    """Abort (return 2) if the move cannot fit in the target's free space.

    Returns 0 when the plan is safe, or None to continue normally.
    """
    move_bytes, freeable = plan_stats(groups, target_idx)
    log.info(f"plan: move {fmt_bytes(move_bytes)} to target, "
             f"freeable {fmt_bytes(freeable)} from sources")
    if move_bytes <= 0:
        return 0

    free = free_storage_bytes(_backend_for(backends, target_idx))
    log.info(f"target account {target_idx}: "
             f"{fmt_bytes(free)} free")
    if move_bytes > free:
        msg = (f"plan requires {fmt_bytes(move_bytes)} but target has "
               f"only {fmt_bytes(free)} free")
        if dry_run:
            log.warning(f"preflight: {msg} (dry-run continues)")
        else:
            log.error("preflight ABORT: " + msg)
            return 2
    return 0


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
def _selftest() -> int:
    """Run the project's unit tests in-process (no network)."""
    import unittest

    root = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, root)
    suite = unittest.defaultTestLoader.discover("tests")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


def _connectivity(kind: str, cfg: dict, only_accounts) -> int:
    """--verify: build backends, reach every account, dump first items.

    No uploads, no mutations — just proves your credentials are valid and
    shows what the API actually exposes. Exits 0 on success.
    """
    import traceback

    log = Logger(level=cfg["logging"].get("log_level", "INFO"),
                 log_file=cfg["logging"].get("log_file", ""))
    try:
        backends = build_backends(kind, cfg, only_accounts)
    except (BackendError, SystemExit) as exc:
        log.error(f"backend init failed: {exc}")
        return 2
    ok = True
    for b in backends:
        try:
            b.check_access()
            caps = b.capabilities()
            log.info(f"account {b.account_id}: CONNECTED")
            log.info(f"  free storage: {fmt_bytes(free_storage_bytes(b))}")
            log.info(f"  capabilities: app_created_only={caps.app_created_only} "
                     f"api_delete={caps.can_delete_library_items} "
                     f"originals_visible={caps.originals_visible}")
            shown = 0
            for item in b.list_all():
                if shown < 3:
                    log.info(f"  sample item type={item.media_type} "
                             f"url={'yes' if item.product_url else 'none'}")
                shown += 1
            log.info(f"  items visible to API: {shown}")
            if shown == 0:
                log.warning("  NOTE: the API sees ZERO items. If everything "
                            "was uploaded via the Photos app/website this is "
                            "expected (app-created-content-only scopes).")
        except BackendError as exc:
            log.error(f"account {b.account_id}: FAILED ({exc})")
            traceback.print_exc()
            ok = False
    log.info("verify " + ("OK" if ok else "FAILED — fix credentials, see README"))
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", default=os.environ.get("GPC_CONFIG", DEFAULT_CONFIG),
        help="config file (env GPC_CONFIG also works)")
    parser.add_argument("--dry-run", action="store_true",
                        help="plan only: no uploads or deletions")
    parser.add_argument("--backend", default=None,
                        help="override backend (oauth|rclone); "
                             "env GPC_BACKEND also works")
    parser.add_argument("--accounts", default=None,
                        help="comma-separated account ids to include")
    parser.add_argument("--jobs", type=int, default=None,
                        help="hash workers (default config.concurrency; "
                             "env GPC_JOBS also works)")
    parser.add_argument("--limit", type=int, default=0,
                        help="process at most N photos total (0 = unlimited)")
    parser.add_argument("--self-test", action="store_true",
                        help="run the unit tests, then exit")
    parser.add_argument("--verify", action="store_true",
                        help="test the connection to every account (no "
                             "uploads, no changes) then exit")
    parser.add_argument("--log-level", default=None)
    args = parser.parse_args()

    if args.self_test:
        return _selftest()

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

    if args.verify:
        return _connectivity(kind, cfg, only_accounts)

    log = Logger(
        level=cfg["logging"].get("log_level", "INFO"),
        log_file=cfg["logging"].get("log_file", ""),
    )
    dry_run = args.dry_run
    if dry_run:
        log.info("DRY-RUN mode: no uploads or deletions will happen")
    if args.limit > 0:
        log.info(f"LIMIT: analyzing at most {args.limit} distinct hashes")

    # -- accounts must ALL be reachable, else abort ------------------------
    try:
        backends = build_backends(kind, cfg, only_accounts)
    except (BackendError, SystemExit) as exc:
        log.error(f"backend init failed: {exc}")
        return 2

    try:
        rc = _pipeline(backends, cfg, log, dry_run, args.limit, args.jobs)
    except KeyboardInterrupt:
        log.warning("interrupted by user (Ctrl-C); source photos were never "
                    "deleted automatically, nothing was lost")
        return 130
    return rc


def _pipeline(backends, cfg, log, dry_run, limit, jobs) -> int:
    for b in backends:
        try:
            b.check_access()
            caps = b.capabilities()
            log.info(f"account {b.account_id}: reachable, "
                     f"free {fmt_bytes(free_storage_bytes(b))}")
            log.info(f"account {b.account_id} capabilities: "
                     f"app_created_only={caps.app_created_only} "
                     f"api_delete={caps.can_delete_library_items} "
                     f"originals_visible={caps.originals_visible}")
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
    concurrency = jobs or int(os.environ.get("GPC_JOBS") or 0) or \
        cfg["consolidation"].get("concurrency", 4)
    groups = compute_hashes(backends, log, concurrency)
    if not groups:
        log.info("no photos found")
        return 0

    if limit > 0 and sum(len(v) for v in groups.values()) > limit:
        rest = sorted(groups.items())
        groups = dict(rest[:limit])
        log.warning(f"LIMIT: trimmed analysis to {len(groups)} hashes")

    # -- pre-flight: does the move actually fit? ---------------------------
    rc = preflight_move(backends, groups, target_idx, log, dry_run)
    if rc:
        return rc

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
    move_bytes, freeable = plan_stats(groups, target_idx)
    log.summary("bytes_to_target", fmt_bytes(move_bytes))
    log.summary("bytes_freeable_from_sources", fmt_bytes(freeable))
    log.summary("queued_or_would_queue_removal", manifest.count())
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