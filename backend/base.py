"""Abstract backend interface shared by both implementations.

IMPORTANT PLATFORM REALITY (verified 2026):
Since 2025-03-31 Google's Library API is strictly app-created-content only:
  * `photoslibrary`, `photoslibrary.readonly` and `.sharing` scopes are removed.
  * Only `appendonly`, `readonly.appcreateddata`, `edit.appcreateddata` remain.
  * There is NO endpoint to delete a media item from a library. The only
    mutation is removing items from albums the app created
    (`albums.batchRemoveMediaItems`), and the API cannot see photos a user
    uploaded through the Photos app/website.
Consequence: the API can never "empty" a source account. This tool therefore
plans consolidations it CAN do (upload, dedupe detection, album management)
and produces a *removal manifest* (private, local, clickable web links) for
the deletions Google reserves for the Photos web UI.

A backend talks to ONE Google account and can:
  * audit what the platform actually allows for the account
  * list app-created media items (all that the API exposes after 2025)
  * stream item bytes for hashing/copying without persisting them
  * create a new media item from a byte stream
  * report free storage and a human-usable removal URL for an item

MediaItem carries an *opaque* media_id so the rest of the program never
needs to know how a backend identifies things. Private data (emails,
filenames, tokens) never travels through this layer beyond what is strictly
needed inside a single backend instance.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import BinaryIO, Iterator, List, Optional


class BackendError(RuntimeError):
    """Raised on any backend-level failure (network, auth, unsupported op)."""


@dataclass
class Capabilities:
    """What Google actually lets this account/app do (probed at runtime)."""

    app_created_only: bool = False        # True for all post-2025 Library API apps
    can_delete_library_items: bool = False  # always False on public Library API
    can_remove_from_albums: bool = False  # True for items app placed in albums
    originals_visible: bool = False       # True only for partner-program apps


@dataclass
class MediaItem:
    """A single photo/video, identified by an opaque media_id."""

    account_id: int          # internal numeric account id (index in config)
    media_id: str            # opaque id: Photos API id, rclone path, ...
    file_name: str           # original file name (used only to derive extension)
    media_type: str = "PHOTO"   # PHOTO | VIDEO
    base_url: str = ""       # used only by the OAuth backend, never logged
    product_url: str = ""    # human-usable web link (OAuth backend only)
    hash: str = ""           # sha256 hex, filled during the hashing phase
    size: int = 0

    @property
    def extension(self) -> str:
        """Lowest-risk extension derived from the original file name."""
        name = self.file_name.rsplit(".", 1)
        if len(name) == 2 and len(name[1]) <= 8:
            return "." + name[1].lower()
        return ""


class BaseBackend(ABC):
    """Interface every backend must implement (one backend per account)."""

    def __init__(self, account_id: int) -> None:
        self.account_id = account_id

    # -- discovery -------------------------------------------------------
    @abstractmethod
    def check_access(self) -> None:
        """Verify the account is reachable; raise BackendError otherwise."""

    @abstractmethod
    def capabilities(self) -> Capabilities:
        """Probe what today's platform allows for this account."""

    @abstractmethod
    def list_all(self) -> Iterator[MediaItem]:
        """Yield every media item visible to the app (post-2025: app-created)."""

    # -- content ---------------------------------------------------------
    @abstractmethod
    def get_hash(self, item: MediaItem) -> str:
        """Stream the item and return its SHA-256 hex digest."""

    @abstractmethod
    def get_bytes(self, item: MediaItem) -> BinaryIO:
        """Return a binary file-like object streaming the item's bytes.

        The caller MUST close() the returned object.
        """

    @abstractmethod
    def create_from_stream(self, stream: BinaryIO, file_name: str) -> str:
        """Create a new media item from a byte stream; return its media_id."""

    # -- mutation --------------------------------------------------------
    @abstractmethod
    def verify_present(self, media_id: str) -> bool:
        """True if the media item still exists (used by self-checks)."""

    @abstractmethod
    def removal_url(self, item: MediaItem) -> Optional[str]:
        """Human-openable web link for the user to delete this item manually.

        Google exposes NO deletion via the Library API; the web UI is the
        only sanctioned channel. Returns None if a URL can't be produced.
        """

    # -- storage ---------------------------------------------------------
    @abstractmethod
    def get_free_storage(self) -> int:
        """Free storage in bytes, or raise BackendError if undeterminable."""


def register_target_copy(
    name_index: dict,
    backend: BaseBackend,
    account_id: int,
    media_id: str,
    file_name: str,
    media_type: str = "PHOTO",
) -> MediaItem:
    """Register a freshly created target copy so later re-runs are idempotent."""
    item = MediaItem(
        account_id=account_id, media_id=media_id,
        file_name=file_name, media_type=media_type,
    )
    name_index[file_name] = item
    return item