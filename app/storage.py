"""Space management: free-space checks, cache-capacity enforcement, and LRU
eviction.

Two independent constraints protect local disk:
- **Free-space reserve:** always keep at least ``min_free_space_bytes`` (500 MB)
  free, so the OS and other files are never starved.
- **Cache-capacity cap:** an optional admin-configured ceiling
  (``max_cache_mb``) on the *total* size of locally-cached videos. When the
  total exceeds it, evict LRU videos that already have a copy on Google Drive.

Both evict the same way: oldest-accessed cached videos first, and a video is
only evicted when a Drive copy exists AND it is not mid-upload (``status``
``'uploading'`` / in-flight), so an in-progress upload is never dropped.
"""

import os
import shutil
import uuid
from pathlib import Path
from typing import Iterable, Optional

from . import db


def free_space_bytes(path: str) -> int:
    """Return free disk space (bytes) for the filesystem holding ``path``."""
    return shutil.disk_usage(path).free


def min_free_space_bytes(app_config: dict) -> int:
    """Read the minimum-free-space threshold from settings (or app config)."""
    raw = db.get_setting("min_free_space_bytes")
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            pass
    return int(app_config.get("min_free_space_bytes", 500 * 1024 * 1024))


def max_cache_bytes(app_config: dict) -> int:
    """Read the total cache-capacity ceiling (bytes) from settings.

    ``0`` (or a missing/invalid value) means unlimited. The value is stored in
    MB in the admin UI and converted to bytes here.
    """
    raw = db.get_setting("max_cache_mb")
    if raw is not None:
        try:
            return int(raw) * 1024 * 1024
        except ValueError:
            pass
    return int(app_config.get("max_cache_mb", 0)) * 1024 * 1024


def ensure_space(
    base_dir: str, incoming_bytes: int, app_config: dict
) -> bool:
    """Ensure ``incoming_bytes`` can be stored while keeping the reserve free.

    Evicts LRU cached videos (already on Google Drive) as needed.
    Returns True if enough space is available, False otherwise (upload should
    be rejected).
    """
    reserve = min_free_space_bytes(app_config)
    while free_space_bytes(base_dir) < reserve + incoming_bytes:
        candidate = _next_evict_candidate(base_dir, app_config)
        if candidate is None:
            break
        _evict(candidate, base_dir, app_config)
    return free_space_bytes(base_dir) >= reserve + incoming_bytes


def _next_evict_candidate(
    base_dir: str, app_config: dict, protected: Optional[Iterable[int]] = None
) -> Optional[dict]:
    """Return the oldest-accessed cached video that has a Drive copy.

    Videos in ``protected`` (in-flight uploads) and any still ``'uploading'``
    are skipped, so an in-progress upload is never evicted.
    """
    protected_set = set(protected or ())
    for video in db.list_lru_cached():
        if not video.get("local_filename"):
            continue
        if video["id"] in protected_set:
            continue
        if video.get("status") == "uploading":
            continue
        return video
    return None


def enforce_cache_limit(
    videos_dir: Path,
    base_dir: str,
    app_config: dict,
    in_flight_ids: Optional[Iterable[int]] = None,
) -> int:
    """Evict LRU cached videos until the total cache size is under the cap.

    Returns the number of videos evicted. No-op when the cap is unlimited
    (``0``). A video is evicted only if it has a local copy AND a Drive copy
    AND is not in ``in_flight_ids`` (a video whose Drive upload is still in
    progress is never dropped, per the cache-capacity policy).
    """
    cap = max_cache_bytes(app_config)
    if cap <= 0:
        return 0
    protected = set(in_flight_ids or ())
    evicted = 0
    # Re-read the total on each pass so a partially-deleted file does not
    # over-count. The loop is bounded by the number of cached videos.
    while db.sum_cached_bytes() > cap:
        candidate = _next_evict_candidate(base_dir, app_config, protected)
        if candidate is None:
            break
        _evict(candidate, base_dir, app_config)
        evicted += 1
    return evicted


def _evict(video: dict, base_dir: str, app_config: dict) -> None:
    """Delete the local cached file (Drive copy remains) and clear the flag."""
    local = video.get("local_filename")
    if local:
        path = Path(base_dir) / "videos" / local
        if path.exists():
            path.unlink()
    db.update_video(video["id"], local_filename=None)


def save_upload_as(uploaded_file, dest_dir: Path, filename: str) -> str:
    """Persist an uploaded file to ``dest_dir`` under a caller-chosen name.

    The caller is responsible for choosing a unique ``filename`` (see
    ``routes.upload._unique_video_name``). Returns ``filename``.
    """
    path = dest_dir / filename
    with path.open("wb") as fh:
        shutil.copyfileobj(uploaded_file.stream, fh)
    return filename


def save_cover(uploaded_file, dest_dir: Path, app_config: dict) -> str:
    """Persist a cover image under a guaranteed-unique name.

    Covers are sent by the browser with a fixed name (``cover.jpg``); naming
    them by the client filename would make every video share one file. A UUID
    stem guarantees uniqueness while preserving the image extension. Returns
    the stored filename.
    """
    ext = os.path.splitext(uploaded_file.filename or "")[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"):
        ext = ".jpg"
    filename = f"cover_{uuid.uuid4().hex}{ext}"
    path = dest_dir / filename
    with path.open("wb") as fh:
        shutil.copyfileobj(uploaded_file.stream, fh)
    return filename


def _safe_name(name: str) -> str:
    """Reduce ``name`` to its basename with an alphanumeric-safe stem, keeping
    the original extension. Uniqueness is not guaranteed here; callers may
    suffix to avoid collisions."""
    base = os.path.basename(name) or "video"
    stem, ext = os.path.splitext(base)
    stem = "".join(ch for ch in stem if ch.isalnum() or ch in "-_")[:80] or "video"
    return f"{stem}{ext.lower()}"
