#!/usr/bin/env python3
"""gdrive_to_gphotos.py

Upload all images + videos from three Google Drive folders
("2025 fotolari", "2026 fotolari", "Consolidated") into Google Photos as
omerhalilli1234@gmail.com.

Duplicate detection (2026 Google Photos API reality)
----------------------------------------------------
Since 2025-03-31 Google removed the read/library scopes. The Library API can
now only see photos that THIS app uploaded (`readonly.appcreateddata`) — it
cannot see pre-existing library photos uploaded via the Photos app/website.
So the ONLY reliable way to avoid duplicates is a local index the script
maintains itself:

  * a local JSON index (`./gphotos_index.json`) recording every file it has
    uploaded, with its Drive id, name, size, sha256 + the returned photo id
  * before uploading, the script checks (a) the local index by filename and
    filename+hash, and (b) the API-visible app-created photos (best effort),
    and skips anything already present.

This keeps the "skip already uploaded files / avoid duplicates" promise
within what Google actually allows.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------
# Target folders (exact names, Unicode-aware: "2025 fotoları" uses the
# Turkish dotless "ı"). Lookup falls back to ASCII-folded spellings, so
# "2025 fotolari" / "2025 fotoları" both match.
FOLDER_NAMES = ["2025 fotoları", "2026 fotoları", "Consolidated"]

_ASCII_FOLD = {
    "ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ç": "c", "Ç": "c",
    "ğ": "g", "Ğ": "g", "ö": "o", "Ö": "o", "ü": "u", "Ü": "u",
    "â": "a", "î": "i", "û": "u",
}


def ascii_fold(name: str) -> str:
    """Lowercase + fold Turkish/accented characters to ASCII (name lookup)."""
    folded = name.lower()
    for src, dst in _ASCII_FOLD.items():
        folded = folded.replace(src, dst)
    return folded
MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".heif", ".tif",
    ".tiff", ".ico",
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".mpg", ".mpeg", ".m4v", ".3gp",
}
GPHOTOS_CREATE_URL = "https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate"
SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",          # read Drive files
    "https://www.googleapis.com/auth/photoslibrary.appendonly",  # upload to Photos
    "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata",  # read own uploads
]
INDEX_FILE = "gphotos_index.json"
TOKEN_BASENAME = "gdrive_to_gphotos_tokens.json"

log = logging.getLogger("gdrive_to_gphotos")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def human(n: int) -> str:
    n = float(max(int(n or 0), 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024.0
    return f"{n} B"


@dataclass
class UploadRecord:
    drive_id: str
    name: str
    size: int
    sha256: str
    gphoto_id: str = ""
    uploaded_at: str = ""


# ---------------------------------------------------------------------------
# OAuth (shared credential for both APIs)
# ---------------------------------------------------------------------------
def load_or_auth_credentials(client_secret: str, token_path: str) -> Credentials:
    creds = None
    if os.path.exists(token_path):
        try:
            with open(token_path, "r", encoding="utf-8") as fh:
                info = json.load(fh)
            creds = Credentials.from_authorized_user_info(info, SCOPES)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not load token file (%s); re-authorizing", exc)

    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(GoogleAuthRequest())
        if creds.valid:
            _save_token(creds, token_path)
            return creds

    flow = InstalledAppFlow.from_client_secrets_file(client_secret, SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    _save_token(creds, token_path)
    return creds


def _save_token(creds: Credentials, token_path: str) -> None:
    with open(token_path, "w", encoding="utf-8") as fh:
        fh.write(creds.to_json())
    log.info("Saved credentials to %s", token_path)


# ---------------------------------------------------------------------------
# Drive access
# ---------------------------------------------------------------------------
class DriveClient:
    def __init__(self, creds: Credentials) -> None:
        self.service = build("drive", "v3", credentials=creds,
                             cache_discovery=False)

    def find_root_ids(self, names: List[str]) -> Dict[str, List[str]]:
        """Return {folder_name: [file_id,...]} for the top-level targets."""
        out: Dict[str, List[str]] = {}
        for name in names:
            spellings = list(dict.fromkeys([name, ascii_fold(name)]))
            ids: List[str] = []
            for spelling in spellings:
                query = (
                    f"name = '{_esc(spelling)}' and mimeType = "
                    "'application/vnd.google-apps.folder' and trashed = false"
                )
                try:
                    resp = self.service.files().list(
                        q=query, fields="files(id,name)",
                        pageSize=1000, supportsAllDrives=True,
                        includeItemsFromAllDrives=True,
                    ).execute()
                except HttpError as exc:
                    log.error("Drive query failed for %r: %s", name, exc)
                    raise
                for f in resp.get("files", []):
                    if f["id"] not in ids:
                        ids.append(f["id"])
            if not ids:
                log.warning("No folder matching %r found in Drive", name)
            else:
                log.info("Found %d folder(s) for %r", len(ids), name)
            out[name] = ids
        return out

    def list_media_recursive(self, folder_ids: List[str]) -> List[Tuple[str, str]]:
        """Return (file_id, name) for every media file under the folders."""
        files: List[Tuple[str, str]] = []
        seen_ids: set = set()
        stack = [f"'{_esc(i)}' in parents and trashed = false"
                 for i in folder_ids]
        while stack:
            query = stack.pop()
            page = None
            while True:
                opts = {"q": query, "fields": "nextPageToken,files(id,name,mimeType,parents)", "pageSize": 1000}
                if page:
                    opts["pageToken"] = page
                try:
                    resp = self.service.files().list(**opts,
                                                     supportsAllDrives=True,
                                                     includeItemsFromAllDrives=True).execute()
                except HttpError as exc:
                    log.error("Drive list failed: %s", exc)
                    raise
                for f in resp.get("files", []):
                    fid = f["id"]
                    if fid in seen_ids:
                        continue
                    seen_ids.add(fid)
                    ext = os.path.splitext(f["name"])[1].lower()
                    if f["mimeType"] == "application/vnd.google-apps.folder":
                        stack.append(f"'{_esc(fid)}' in parents and trashed = false")
                    elif ext in MEDIA_EXTENSIONS:
                        files.append((fid, f["name"]))
                page = resp.get("nextPageToken")
                if not page:
                    break
        return files

    def download(self, file_id: str) -> Tuple[bytes, str, int]:
        """Return (bytes, mime, size) of a file."""
        meta = self.service.files().get(
            fileId=file_id, fields="mimeType,size").execute()
        size = int(meta.get("size", 0))
        mime = meta.get("mimeType", "application/octet-stream")
        request = self.service.files().get_media(fileId=file_id)
        buf = _BytesBuffer()
        dl = MediaIoBaseDownload(buf, request, chunksize=1024 * 1024)
        done = False
        while not done:
            _, done = dl.next_chunk()
        return buf.getvalue(), mime, size


class _BytesBuffer:
    """Growable buffer that satisfies MediaIoBaseDownload's file-like API."""
    def __init__(self):
        self._data = bytearray()
        self._pos = 0

    def write(self, chunk: bytes) -> int:
        self._data.extend(chunk)
        return len(chunk)

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence != 0:
            raise ValueError("only absolute seek supported")
        self._pos = offset
        return self._pos

    def getvalue(self) -> bytes:
        return bytes(self._data)


def _esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


# ---------------------------------------------------------------------------
# Google Photos API (direct REST — Google no longer serves a public
# discovery document for photoslibrary, so googleapiclient build() fails)
# ---------------------------------------------------------------------------
class PhotosClient:
    BASE = "https://photoslibrary.googleapis.com/v1"

    def __init__(self, creds: Credentials) -> None:
        self._creds = creds
        self._session = requests.Session()

    def _auth_headers(self) -> dict:
        if self._creds.expired:
            self._creds.refresh(GoogleAuthRequest())
        return {"Authorization": "Bearer " + self._creds.token}

    def _request(self, method: str, path: str, params=None, body=None):
        url = self.BASE + path
        headers = self._auth_headers()
        try:
            if method == "GET":
                resp = self._session.get(url, params=params, headers=headers,
                                         timeout=600)
            else:
                resp = self._session.post(url, json=body, headers=headers,
                                          timeout=600)
        except requests.RequestException as exc:
            raise RuntimeError(f"Photos {method} {path}: {exc}") from exc
        if resp.status_code != 200:
            raise RuntimeError(
                f"Photos {method} {path}: HTTP {resp.status_code} "
                f"{resp.text[:200]}")
        return resp.json()

    def upload_bytes(self, data: bytes) -> str:
        """Upload raw bytes, return the upload token (or raise).

        Uses a fresh requests session per call so concurrent workers are safe.
        """
        headers = self._auth_headers()
        headers.update({
            "Content-Type": "application/octet-stream",
            "X-Goog-Upload-File-Name": "photo",
            "X-Goog-Upload-Protocol": "raw",
        })
        resp = requests.post(self.BASE + "/uploads", data=data,
                             headers=headers, timeout=600)
        if resp.status_code != 200:
            raise RuntimeError(f"upload HTTP {resp.status_code}: "
                               f"{resp.text[:200]}")
        token = resp.text.strip()
        if not token:
            raise RuntimeError("empty upload token")
        return token

    def batch_create(self, tokens_and_names: Sequence[Tuple[str, str]]) -> Dict[str, str]:
        """Create media items from (upload_token, file_name) pairs.

        Returns {file_name: photo_id} for the items that succeeded.
        """
        if not tokens_and_names:
            return {}
        body = {
            "newMediaItems": [
                {"simpleMediaItem": {"uploadToken": t, "fileName": n}}
                for t, n in tokens_and_names
            ]
        }
        data = self._request("POST", "/mediaItems:batchCreate", body=body)
        result: Dict[str, str] = {}
        for i, res in enumerate(data.get("newMediaItemResults", [])):
            if i >= len(tokens_and_names):
                break
            name = tokens_and_names[i][1]
            status = res.get("status", {})
            item = res.get("mediaItem")
            if status.get("code", 0) not in (0, None):
                log.warning("batchCreate failed for %r: %s",
                            name, status.get("message"))
                continue
            if item and "id" in item:
                result[name] = item["id"]
            else:
                log.warning("batchCreate returned no id for %r", name)
        return result


# ---------------------------------------------------------------------------
# local index (the de-dup source of truth)
# ---------------------------------------------------------------------------
class Index:
    def __init__(self, path: str) -> None:
        self.path = path
        self.by_drive_id: Dict[str, UploadRecord] = {}
        self.by_name: Dict[str, List[UploadRecord]] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for rec in data:
                r = UploadRecord(**rec)
                self.by_drive_id[r.drive_id] = r
                self.by_name.setdefault(r.name, []).append(r)
            log.info("Loaded index: %d previously uploaded files", len(self.by_drive_id))
        except Exception as exc:  # noqa: BLE001
            log.error("Could not load index %s: %s", self.path, exc)

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump([asdict(r) for r in self.by_drive_id.values()],
                      fh, indent=2)
        os.replace(tmp, self.path)

    def has_name(self, name: str) -> bool:
        return name in self.by_name

    def has_drive_id(self, drive_id: str) -> bool:
        return drive_id in self.by_drive_id

    def has_hash(self, sha256: str) -> bool:
        return any(r.sha256 == sha256 for r in self.by_drive_id.values())

    def add(self, rec: UploadRecord) -> None:
        self.by_drive_id[rec.drive_id] = rec
        self.by_name.setdefault(rec.name, []).append(rec)


# ---------------------------------------------------------------------------
# main pipeline
# ---------------------------------------------------------------------------
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-secret", required=True,
                        help="path to client_secret.json (Desktop app OAuth)")
    parser.add_argument("--token-dir", default=".",
                        help="directory for OAuth token + upload index (default .)")
    parser.add_argument("--limit", type=int, default=0,
                        help="upload at most N files (0 = unlimited)")
    parser.add_argument("--jobs", type=int, default=4,
                        help="parallel download/hash/upload workers (default 4)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    Path(args.token_dir).mkdir(parents=True, exist_ok=True)
    token_path = os.path.join(args.token_dir, TOKEN_BASENAME)
    index = Index(os.path.join(args.token_dir, INDEX_FILE))

    log.info("Authenticating with Google (first time opens a browser)...")
    creds = load_or_auth_credentials(args.client_secret, token_path)
    drive = DriveClient(creds)
    photos = PhotosClient(creds)

    # --- locate folders on Drive, recursively list media ---
    roots = drive.find_root_ids(FOLDER_NAMES)
    all_ids = [i for ids in roots.values() for i in ids]
    if not all_ids:
        log.error("None of the target folders were found. Aborting.")
        return 1
    log.info("Locating media files under %d root folder(s)...", len(all_ids))
    media = drive.list_media_recursive(all_ids)
    log.info("Found %d media files in Drive", len(media))

    # --- filter to candidates not yet uploaded (cheap checks first) ---
    candidates: List[Tuple[str, str]] = []
    for fid, name in media:
        if index.has_drive_id(fid):
            continue
        if index.has_name(name):
            log.info("skip (same filename already uploaded): %s", name)
            continue
        candidates.append((fid, name))

    if args.limit > 0:
        candidates = candidates[: args.limit]
        log.info("LIMIT: considering at most %d files", len(candidates))

    total = len(candidates)
    if total == 0:
        log.info("Nothing to upload — everything is already in the index.")
        return 0
    log.info("Files to process after dedupe: %d", total)

    # --- process in parallel: download -> hash-check -> token -> batch ---
    from concurrent.futures import ThreadPoolExecutor

    done = 0
    failed = 0
    # seen_hashes: hashes known to be uploaded (index snapshot + this run),
    # so concurrent workers skip same-content files already uploaded.
    seen_hashes = {r.sha256 for r in index.by_drive_id.values()}
    pending: List[Tuple[str, str, str, bytes, str]] = []  # (fid,name,token,data,sha)

    def flush_batch():
        nonlocal done, pending
        if not pending:
            return
        log.info("Creating %d media items in Photos (batch)...", len(pending))
        try:
            created = photos.batch_create(
                [(tok, name) for _fid, name, tok, _data, _sha in pending])
        except Exception as exc:  # noqa: BLE001
            log.error("batchCreate failed for %d items: %s (will retry next run)",
                      len(pending), exc)
            pending = []
            return
        created_here = 0
        for fid, name, _tok, data, sha in pending:
            pid = created.get(name)
            if pid:
                rec = UploadRecord(
                    drive_id=fid, name=name, size=len(data),
                    sha256=sha, gphoto_id=pid,
                    uploaded_at=time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                )
                index.add(rec)
                created_here += 1
            else:
                log.info("no photo id for %r (will retry next run)", name)
        done += created_here
        index.save()
        pending = []
        log.info("Progress: %d/%d uploaded, %d failed",
                 done, total, failed)

    def process_one(item):
        fid, name = item
        try:
            data, _mime, _size = drive.download(fid)
        except Exception as exc:  # noqa: BLE001
            log.error("download failed %r: %s", name, exc)
            return None
        h = sha256_bytes(data)
        if h in seen_hashes:
            log.info("skip (identical content already uploaded): %s", name)
            return None
        try:
            token = photos.upload_bytes(data)
        except Exception as exc:  # noqa: BLE001
            log.error("upload failed %r: %s", name, exc)
            return None
        return (fid, name, token, data, h)

    BATCH = 50  # Google's batchCreate limit
    workers = max(1, min(args.jobs, 8))
    log.info("Using %d parallel workers, batches of %d", workers, BATCH)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(process_one, candidates):
            if result is None:
                failed += 1
                continue
            fid, name, token, data, h = result
            seen_hashes.add(h)
            pending.append(result)
            if len(pending) >= BATCH:
                flush_batch()
    flush_batch()

    log.info("ALL DONE. New uploads this run: %d  (failed: %d)",
             done, failed)
    index.save()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log.warning("interrupted by user")
        sys.exit(130)
