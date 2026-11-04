"""Space management: free-space checks, cache-capacity enforcement, and LRU
eviction.

Two independent constraints protect local disk:
- **Free-space reserve:** always keep at least ``min_free_space_bytes`` (500 MB)
  free, so the OS and other files are never starved.
- **Cache-capacity cap:** an optional admin-configured ceiling
  (``max_cache_mb``) on the *total* size of locally-cached videos. When the
  total exceeds it, evict LRU videos that already have a copy on Google Drive.

Both evict the same way: oldest-accessed cached videos first, and a video is
only evicted when a Drive copy exists AND it is not mid-upload (status
'uploading' / in-flight), so an in-progress upload is never dropped.

Since §31.4 a cache entry is a folder, not a file: storage/videos/<hash>/ holds
the MP4 copy and its stream/ segment tree. Every size calculation here therefore
stats files and directories alike, and eviction removes a whole tree (a size read
on a directory, or unlink() on one, would both be wrong).
"""

import os
import shutil
import sys
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


def _entry_bytes(base: str, rel: str | None) -> int:
    """Size of one cache entry on disk: a file, or every file under a directory.

    §31.4: a cached video is a folder (``<hash>/`` holding ``<hash>.mp4`` plus the
    ``stream/`` segment tree). os.path.getsize on a directory returns the size of
    the directory entry, not its contents, so directories are walked instead.
    """
    if not rel:
        return 0
    path = os.path.join(base, rel)
    try:
        if os.path.isdir(path):
            total = 0
            for root, _dirs, files in os.walk(path):
                for name in files:
                    try:
                        total += os.path.getsize(os.path.join(root, name))
                    except OSError:
                        continue
            return total
        return os.path.getsize(path)
    except OSError:
        return 0  # entry missing (not migrated / deleted) -> contributes 0


def actual_cache_bytes(videos_dir) -> int:
    """Total size (bytes) of everything cached on disk: MP4 copies and streams.

    Unlike db.sum_cached_bytes (the size recorded at upload time), this stats the
    real files. A row whose file is missing (e.g. the media was not copied during
    a server migration) contributes 0, so the total tracks what is cached now.
    """
    base = str(videos_dir)
    total = 0
    for entry in db.list_cache_paths():
        total += _entry_bytes(base, entry.get("local_filename"))
        total += _entry_bytes(base, entry.get("stream_dir"))
    return total


def ensure_space(
    base_dir: str,
    incoming_bytes: int,
    app_config: dict,
    peak_factor: float | None = None,
    protect: Iterable[int] | None = None,
) -> bool:
    """Ensure ``incoming_bytes`` can be stored while keeping the reserve free.

    Evicts LRU cached videos (already on Google Drive) as needed.
    Returns True if enough space is available, False otherwise (upload should
    be rejected).

    ``peak_factor`` is the multiplier applied to the incoming size. With HLS
    enabled the default is 2.0: an upload briefly holds the MP4 AND its segments
    (measured equal in size, §30.2) until the stream validates and the MP4 is
    dropped, so a 1.0 reservation lets a large upload pass the pre-check and then
    die halfway through segmentation. A caller with a different peak (a repair,
    where the source is already on disk) passes its own factor.

    ``protect`` names the videos the caller is about to write. Without it a reservation
    can evict the very file the caller needs: an HLS build asks for room for the
    segments of a video that is itself a cached, Drive-backed LRU candidate.
    """
    reserve = min_free_space_bytes(app_config)
    factor = peak_factor if peak_factor is not None else _peak_factor(app_config)
    needed = int(incoming_bytes * max(1.0, float(factor)))
    while free_space_bytes(base_dir) < reserve + needed:
        candidate = _next_evict_candidate(base_dir, app_config, protect)
        if candidate is None:
            break
        _evict(candidate, base_dir, app_config)
    return free_space_bytes(base_dir) >= reserve + needed


def _peak_factor(app_config: dict) -> float:
    """Reservation multiplier for a new video: 2.0 while HLS is enabled (§31.4)."""
    try:
        from . import hls

        if hls.settings(app_config)["enabled"]:
            return 2.0
    except Exception:  # noqa: BLE001 - a broken HLS config must not block uploads
        pass
    return 1.0


def _next_evict_candidate(
    base_dir: str, app_config: dict, protected: Optional[Iterable[int]] = None
) -> Optional[dict]:
    """Return the oldest-accessed cached video that has a Drive copy.

    Videos in ``protected`` (in-flight uploads) and any still ``'uploading'``
    are skipped, so an in-progress upload is never evicted. A segments-only video
    (``hls_keep_source`` cleared ``local_filename``, §31.1) is still a cache entry
    and must stay evictable, or the cache cap could never reclaim it.
    """
    protected_set = set(protected or ())
    for video in db.list_lru_cached():
        if not (video.get("local_filename") or video.get("stream_dir")):
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
    # Re-read the real on-disk total on each pass so a deleted file is not
    # over-counted. The loop is bounded by the number of evictable videos.
    while actual_cache_bytes(videos_dir) > cap:
        candidate = _next_evict_candidate(base_dir, app_config, protected)
        if candidate is None:
            break
        _evict(candidate, base_dir, app_config)
        evicted += 1
    return evicted


def _evict(video: dict, base_dir: str, app_config: Optional[dict] = None) -> None:
    """Delete a video local cache - the MP4 copy AND its stream tree (Drive remains).

    A stream tree is a directory, so it needs rmtree (unlink() on a directory
    raises), and the folder that held a foldered upload is pruned once empty.
    stream_state is reset so the watch page stops offering a stream that is no
    longer on disk.
    """
    videos_dir = Path(base_dir) / "videos"
    for rel in (video.get("local_filename"), video.get("stream_dir")):
        if not rel:
            continue
        path = videos_dir / str(rel)
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
        except OSError as exc:  # noqa: BLE001 - the DB flags are cleared regardless
            print(f"warn: could not evict {path}: {exc}", file=sys.stderr)
        # A foldered upload leaves <hash>/ behind; drop it when nothing is left.
        try:
            if path.parent != videos_dir and path.parent.is_dir():
                if not any(path.parent.iterdir()):
                    path.parent.rmdir()
        except OSError:
            pass
    db.update_video(
        video["id"],
        local_filename=None,
        stream_dir=None,
        stream_state="none",
    )


def cache_footprint(base_dir: str, video: dict) -> int:
    """Bytes one cache entry occupies: the local MP4 copy plus its stream tree.

    ``base_dir`` is the storage directory (the parent of ``videos/``), the same
    convention ``_evict`` works from, so the two can never disagree about where an
    entry lives. A row whose file is already gone contributes 0.
    """
    videos_dir = str(Path(base_dir) / "videos")
    return (
        _entry_bytes(videos_dir, video.get("local_filename"))
        + _entry_bytes(videos_dir, video.get("stream_dir"))
    )


def cache_drop_blocker(video: dict) -> Optional[str]:
    """Reason this cache entry must not be dropped, or None when it may be.

    The rule every cache removal already follows, stated once so a button cannot
    invent a looser one: a local copy may only be thrown away when Google Drive
    holds the original, and never while that upload is still in progress.
    """
    if not video.get("google_drive_file_id"):
        return (
            "No Google Drive copy of this video, so its local file is the only "
            "copy. Delete the video instead, or wait for the upload to finish."
        )
    if video.get("status") == "uploading":
        return "The Google Drive upload of this video is still in progress."
    return None


def drop_local_cache(base_dir: str, video: dict) -> int:
    """Delete the local cache of one video (MP4 copy and HLS tree), flags included.

    The admin-facing form of ``_evict``: the same removal and the same flag reset,
    but measured first so the page can report how much disk it just gave back. The
    Drive file and the DB row are untouched, which is exactly what lets the watch
    page pull the video back from Drive.

    Returns the bytes freed (0 when the row had no files on disk).
    """
    freed = cache_footprint(base_dir, video)
    _evict(video, base_dir)
    return freed


def purge_video_files(video: dict, base_dir: str, covers_dir: str) -> int:
    """Delete everything one video occupies: Drive copy, cache files, cover.

    The destructive sibling of :func:`drop_local_cache`: the same local removal
    (MP4 copy, HLS tree, and the folder left empty by a foldered upload) plus the
    Drive file and the cover image. Used by the owner's Delete button and by the
    admin user delete, whose FK cascade drops a user's video ROWS and would
    otherwise strand every one of their files on Drive and on disk (§37.4).

    Best effort, same policy as the upload cleanup and :func:`_evict`: a file that
    will not go (locked handle, transient Drive error) is logged and does not stop
    the caller from dropping the row, so the database stays the source of truth.

    Returns the cache bytes freed (0 when the row had no files on disk).
    """
    from . import drive  # lazy import: storage stays importable without pydrive2

    drive_id = video.get("google_drive_file_id")
    if drive_id:
        try:
            drive.delete_file(drive_id)
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            print(f"warn: could not delete Drive file {drive_id}: {exc}", file=sys.stderr)

    remove_stored_file(Path(covers_dir), video.get("cover_filename"), "cover")
    return drop_local_cache(base_dir, video)


def remove_stored_file(dest_dir: Path, filename: Optional[str], label: str) -> None:
    """Remove one stored image (cover / avatar) by name, best effort.

    Names come from the DB, so a missing file is normal, not an error: it is
    already consistent with what the row claims. Anything else is logged.
    """
    if not filename:
        return
    path = Path(dest_dir) / str(filename)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:  # noqa: BLE001 - the DB row is updated regardless
        print(f"warn: could not remove {label} {path}: {exc}", file=sys.stderr)


def is_locally_playable(videos_dir, video: dict) -> bool:
    """True when a video can be played from this server with no Drive round trip.

    Two shapes count (§31.7 blocker 1): the local MP4 copy, or - once
    ``hls_keep_source`` dropped that copy - a validated HLS tree. The watch page
    and the Watch Together page both key off this predicate.
    """
    base = str(videos_dir)
    local = video.get("local_filename")
    if local and os.path.exists(os.path.join(base, str(local))):
        return True
    from . import hls  # lazy import: storage stays importable on its own
    if str(video.get("stream_state") or "") != hls.STATE_READY:
        return False
    stream_dir = hls.stream_dir_for(Path(base), video)
    if stream_dir is None:
        return False
    return hls.validate_stream(stream_dir, video.get("duration"))[0]


def save_upload_as(uploaded_file, dest_dir: Path, filename: str) -> str:
    """Persist an uploaded file to ``dest_dir`` under a caller-chosen name.

    The caller is responsible for choosing a unique ``filename`` (see
    ``routes.upload._unique_video_name``). Returns ``filename``.
    """
    path = dest_dir / filename
    # §31.3: new uploads live in videos/<hash>/<hash>.mp4, so the folder
    # has to exist before the first write.
    path.parent.mkdir(parents=True, exist_ok=True)
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


def save_avatar(uploaded_file, dest_dir: Path) -> str:
    """Persist an avatar image under a UUID name (never the client's filename).

    Avatars arrive with the user's original filename; storing that on disk would
    leak it and let two users share one file. A UUID stem guarantees uniqueness
    while preserving the image extension. Returns the stored filename.
    """
    ext = os.path.splitext(uploaded_file.filename or "")[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"):
        ext = ".jpg"
    filename = f"avatar_{uuid.uuid4().hex}{ext}"
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
