"""Google Photos Library API backend (native OAuth 2.0 / REST).

PLATFORM FACTS (verified 2026):
  * From 2025-03-31 the scopes `photoslibrary`, `photoslibrary.readonly`
    and `photoslibrary.sharing` are REMOVED by Google. Only these remain:
        photoslibrary.appendonly                (upload)
        photoslibrary.readonly.appcreateddata   (read app-created items)
        photoslibrary.edit.appcreateddata       (edit app-created items)
  * The Library API has NO delete endpoint. The only mutation is removing
    items from app-created albums (`albums.batchRemoveMediaItems`).
  * Photos uploaded via the Photos app/website are invisible to the API.

Consequences implemented here:
  * Up-to-date scopes are requested by default.
  * Uploaded items are placed into an app-created album ("gpc_consolidation")
    so they can later be removed from it (the max the API allows).
  * `removal_url()` returns a human-openable Photos web link; the actual
    deletion is performed by the user there (or by a partner-program app).
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import time
from pathlib import Path
from typing import BinaryIO, Iterator, List, Optional

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from backend.base import (
    BackendError,
    BaseBackend,
    Capabilities,
    MediaItem,
)

UPLOAD_URL = "https://photoslibrary.googleapis.com/v1/uploads"
CREATE_URL = "https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate"
ALBUM_REMOVE_URL = (
    "https://photoslibrary.googleapis.com/v1/albums/{album_id}:batchRemoveMediaItems"
)

GPC_ALBUM_TITLE = "gpc_consolidation"
_SPOOL_LIMIT = 50 * 1024 * 1024  # files larger than this spill to a temp file


class _StreamReader(io.RawIOBase):
    """Expose an HTTP response body as a closable binary stream."""

    def __init__(self, response: requests.Response) -> None:
        self._resp = response
        self._chunks = response.iter_content(chunk_size=1024 * 1024)

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1):  # type: ignore[override]
        if size is None or size < 0:
            size = 1024 * 1024
        try:
            return next(self._chunks)
        except StopIteration:
            return b""

    def close(self) -> None:
        self._resp.close()
        super().close()


class GooglePhotosOAuthBackend(BaseBackend):
    """One OAuth backend per Google account."""

    def __init__(
        self,
        account_id: int,
        token_file: str,
        client_secret_env: str,
        token_dir_env: str,
        token_dir_default: str,
        photos_scopes: List[str],
        storage_scope: str,
        album_title: str = GPC_ALBUM_TITLE,
        retries: int = 5,
        backoff_base: float = 2.0,
    ) -> None:
        super().__init__(account_id)
        self._retries = retries
        self._backoff = backoff_base
        self._album_title = album_title
        self._album_id: Optional[str] = None
        self._caps: Optional[Capabilities] = None

        secret = os.environ.get(client_secret_env, "")
        if not secret:
            raise BackendError(
                f"env '{client_secret_env}' not set (OAuth client secret path)"
            )
        self._client_secret = secret

        tok_dir = os.environ.get(token_dir_env) or token_dir_default
        self._token_path = os.path.join(tok_dir, token_file)
        self._scopes = list(photos_scopes) + [storage_scope]
        self._creds: Optional[Credentials] = None
        self._service = None
        self._drive_service = None

    # -- auth ------------------------------------------------------------
    def _load_or_authorize(self) -> Credentials:
        if self._creds and self._creds.valid:
            return self._creds

        creds = None
        if os.path.exists(self._token_path):
            with open(self._token_path, "r", encoding="utf-8") as fh:
                info = json.load(fh)
            creds = Credentials.from_authorized_user_info(info, self._scopes)

        if creds and creds.has_scopes(self._scopes) and creds.valid:
            self._creds = creds
            return creds

        if creds and creds.expired and creds.refresh_token:
            creds.refresh(GoogleAuthRequest())
            if creds.valid:
                self._creds = creds
                self._persist(creds)
                return creds

        # Interactive browser authorization (local only).
        flow = InstalledAppFlow.from_client_secrets_file(
            self._client_secret, self._scopes
        )
        creds = flow.run_local_server(port=0, open_browser=True)
        self._persist(creds)
        self._creds = creds
        return creds

    def _persist(self, creds: Credentials) -> None:
        path = Path(self._token_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(creds.to_json())

    @property
    def service(self):
        if self._service is None:
            creds = self._load_or_authorize()
            self._service = build(
                "photoslibrary", "v1", credentials=creds,
                cache_discovery=False,
            )
        return self._service

    @property
    def drive_service(self):
        if self._drive_service is None:
            creds = self._load_or_authorize()
            self._drive_service = build(
                "drive", "v3", credentials=creds, cache_discovery=False
            )
        return self._drive_service

    # -- retry wrapper ---------------------------------------------------
    def _call(self, fn, desc: str):
        """Run fn with exponential back-off on transient HTTP errors."""
        last: Optional[Exception] = None
        for attempt in range(self._retries):
            try:
                return fn()
            except HttpError as exc:
                code = exc.resp.status
                if code in (429, 500, 502, 503, 504):
                    last = exc
                    time.sleep(self._backoff ** attempt)
                    continue
                raise BackendError(f"{desc}: Google API HTTP {code}") from exc
            except (requests.RequestException, OSError) as exc:
                last = exc
                time.sleep(self._backoff ** attempt)
        raise BackendError(f"{desc}: transient error persists ({last})")

    # -- discovery -------------------------------------------------------
    def check_access(self) -> None:
        self._call(
            lambda: self.service.mediaItems().list(pageSize=1).execute(),
            "check_access",
        )

    def capabilities(self) -> Capabilities:
        if self._caps is None:
            self._caps = Capabilities(
                app_created_only=True,
                can_delete_library_items=False,
                can_remove_from_albums=True,
                originals_visible=False,
            )
        return self._caps

    def list_all(self) -> Iterator[MediaItem]:
        token = None
        fields = (
            "nextPageToken,mediaItems(id,baseUrl,productUrl,filename,mimeType)"
        )
        while True:
            params = {"pageSize": 100, "fields": fields}
            if token:
                params["pageToken"] = token

            def build_call(p=params):
                return self.service.mediaItems().list(**p).execute()

            resp = self._call(build_call, "mediaItems.list")
            for raw in resp.get("mediaItems", []):
                mime = raw.get("mimeType", "")
                yield MediaItem(
                    account_id=self.account_id,
                    media_id=raw["id"],
                    file_name=raw.get("filename", "item"),
                    media_type="VIDEO" if mime.startswith("video") else "PHOTO",
                    base_url=raw.get("baseUrl", ""),
                    product_url=raw.get("productUrl", ""),
                )
            token = resp.get("nextPageToken")
            if not token:
                break

    # -- app-created album -----------------------------------------------
    def _get_album(self) -> Optional[str]:
        """Find or create the app-created album used to track uploads."""
        if self._album_id is not None:
            return self._album_id if self._album_id else None
        token = None
        while True:
            params = {"pageSize": 50}
            if token:
                params["pageToken"] = token

            def build_call(p=params):
                return self.service.albums().list(**p).execute()

            resp = self._call(build_call, "albums.list")
            for album in resp.get("albums", []):
                if album.get("title") == self._album_title:
                    self._album_id = album["id"]
                    return self._album_id
            token = resp.get("nextPageToken")
            if not token:
                break
        try:
            created = self._call(
                lambda: self.service.albums().create(
                    body={"album": {"title": self._album_title}}
                ).execute(),
                "albums.create",
            )
            self._album_id = created["id"]
        except BackendError:
            self._album_id = ""  # album API refused; uploads still work
        return self._album_id or None

    # -- content ---------------------------------------------------------
    def _download_url(self, item: MediaItem) -> str:
        # `=dv` streams the original video; `=d` the original image.
        return item.base_url + ("=dv" if item.media_type == "VIDEO" else "=d")

    def get_bytes(self, item: MediaItem) -> BinaryIO:
        url = self._download_url(item)
        try:
            resp = requests.get(url, stream=True, timeout=300)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise BackendError(
                f"download failed for media {item.media_id[:24]}"
            ) from exc
        return _StreamReader(resp)

    def get_hash(self, item: MediaItem) -> str:
        from utils.hashing import sha256_stream

        reader = self.get_bytes(item)
        try:
            return sha256_stream(reader)
        finally:
            reader.close()

    def upload_bytes(self, data) -> str:
        """POST raw bytes to the uploads endpoint, return uploadToken."""

        # Spool keeps memory bounded (and is deleted automatically on close).
        with tempfile.SpooledTemporaryFile(max_size=_SPOOL_LIMIT, mode="w+b") as spool:
            with data if hasattr(data, "read") else io.BytesIO(data) as src:
                while True:
                    block = src.read(1024 * 1024)
                    if not block:
                        break
                    spool.write(block)
            spool.seek(0)

            headers = {
                "Content-Type": "application/octet-stream",
                "X-Goog-Upload-File-Name": "photo",
                "X-Goog-Upload-Protocol": "raw",
            }
            resp = requests.post(UPLOAD_URL, data=spool, headers=headers,
                                 timeout=300)
            if resp.status_code != 200:
                raise BackendError(
                    f"upload to uploads endpoint: HTTP {resp.status_code}")
            token = resp.text.strip()
            if not token:
                raise BackendError("upload returned an empty uploadToken")
            return token

    def create_from_stream(self, stream: BinaryIO, file_name: str) -> str:
        token = self._call(
            lambda: self.upload_bytes(stream), "upload bytes"
        )
        body: dict = {
            "newMediaItems": [
                {"simpleMediaItem": {"uploadToken": token, "fileName": file_name}}
            ]
        }
        album_id = self._get_album()
        if album_id:
            body["albumId"] = album_id

        def create_call():
            return self.service.mediaItems().batchCreate(body=body).execute()

        resp = self._call(create_call, "mediaItems.batchCreate")
        results = resp.get("newMediaItemResults", [])
        if not results:
            raise BackendError("batchCreate returned no results")
        item = results[0].get("mediaItem")
        if not item or not item.get("id"):
            raise BackendError(
                "batchCreate failed: "
                + results[0].get("status", {}).get("message", "unknown error")
            )
        return item["id"]

    # -- mutation --------------------------------------------------------
    def verify_present(self, media_id: str) -> bool:
        try:
            self._call(
                lambda mid=media_id: self.service.mediaItems().get(
                    mediaItemId=mid
                ).execute(),
                "mediaItems.get",
            )
            return True
        except BackendError:
            return False

    def removal_url(self, item: MediaItem) -> Optional[str]:
        if item.product_url:
            return item.product_url
        # Fallback: standard per-item Photos web URL.
        return f"https://photos.google.com/photo/{item.media_id}"

    # -- storage ---------------------------------------------------------
    def get_free_storage(self) -> int:
        def about_call():
            return self.drive_service.about().get(
                fields="storageQuota"
            ).execute()

        quota = self._call(about_call, "drive.about.get")["storageQuota"]
        limit = int(quota["limit"])
        usage = int(quota.get("usage", 0))
        return max(limit - usage, 0)