"""Home blueprint: waterfall listing of all videos (P4)."""

import concurrent.futures
import logging
from typing import Any, List, Optional, Tuple

from flask import Blueprint, current_app, render_template, send_from_directory
from .. import db, drive, storage
from ..auth import login_required

home_bp = Blueprint("home", __name__)
logger = logging.getLogger("simple_video_share.routes.home")

# A page load needs one Drive quota read plus one existence check per
# non-cached video. Drive is the bottleneck, so we fan those out across a
# small pool: N sequential round trips collapse into ~one, so a cold cache no
# longer stalls the page. Kept small so a large library never hammers the API.
_DRIVE_FANOUT_WORKERS = 8


@home_bp.route("/")
@login_required
def index() -> Any:
    videos = db.list_folder_videos(None)
    kept, drive_free = _resolve_drive(videos, current_app.config["VIDEOS_DIR"])
    return _render_gallery(
        videos=kept,
        subfolders=db.list_root_folders(),
        current_folder=None,
        breadcrumbs=[],
        drive_free=drive_free,
    )


@home_bp.route("/folder/<int:folder_id>")
@login_required
def folder(folder_id: int) -> Any:
    from flask import abort
    current_folder = db.get_folder(folder_id)
    if current_folder is None:
        abort(404)
    videos = db.list_folder_videos(folder_id)
    kept, drive_free = _resolve_drive(videos, current_app.config["VIDEOS_DIR"])
    return _render_gallery(
        videos=kept,
        subfolders=db.list_child_folders(folder_id),
        current_folder=current_folder,
        breadcrumbs=_breadcrumbs(current_folder),
        drive_free=drive_free,
    )


def _render_gallery(**kwargs: Any) -> Any:
    # The home page reports *cache* remaining space (from the admin-configured
    # Max cache cap), not raw disk free space. Real disk free space is an
    # admin concern and is shown on the admin page only. The cache total is the
    # real on-disk size, so a migration that did not copy the media reads 0
    # here instead of the phantom upload-time total.
    cap = storage.max_cache_bytes(current_app.config)
    cached = storage.actual_cache_bytes(current_app.config["VIDEOS_DIR"])
    remaining = max(0, cap - cached) if cap > 0 else 0
    drive_free = kwargs.pop("drive_free", None)
    # §bug L67: annotate each video with its tags so the search box can match
    # on them, and expose the tag library for the tag filter + card badges.
    videos = _annotate_tags(kwargs.get("videos") or [])
    # §20: annotate each video with its comment count (replies included) so
    # every rendered card can show the cover badge. Runs on the Drive-filtered
    # list, so the page costs exactly one aggregate query over the cards that
    # are actually shown (home and folder pages alike).
    videos = _annotate_comment_counts(videos)
    return render_template(
        "home.html",
        cached_bytes=cached,
        cache_cap_bytes=cap,
        cache_remaining_bytes=remaining,
        drive_free_bytes=drive_free,
        tags=db.list_tags(),
        **{**kwargs, "videos": videos},
    )


def _annotate_tags(videos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach a ``tags`` list (``[{"id","name"},...]``) to each video."""
    ids = [v["id"] for v in videos]
    mapping = db.bulk_video_tags(ids)
    for v in videos:
        v["tags"] = mapping.get(v["id"], [])
    return videos


def _annotate_comment_counts(videos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach a ``comment_count`` (replies included, default 0) to each video.

    §20: one aggregate query per page covers every rendered card; the home
    card renders the cover comment badge only when the count is non-zero.
    """
    counts = db.comment_counts([v["id"] for v in videos])
    for v in videos:
        v["comment_count"] = counts.get(v["id"], 0)
    return videos


def _breadcrumbs(folder: dict[str, Any]) -> list[dict[str, Any]]:
    """Folders from Root down to (and including) ``folder``."""
    crumbs: list[dict[str, Any]] = []
    seen: set[int] = set()
    cur = folder
    while cur is not None and cur["id"] not in seen:
        seen.add(cur["id"])
        crumbs.append(cur)
        pid = cur.get("parent_id")
        cur = db.get_folder(pid) if pid is not None else None
    crumbs.reverse()
    return crumbs


@home_bp.route("/covers/<path:filename>")
@login_required
def cover(filename: str) -> Any:
    """Serve cover images from the covers directory."""
    covers_dir = str(current_app.config["COVERS_DIR"])
    return send_from_directory(covers_dir, filename)


@home_bp.route("/avatars/<path:filename>")
@login_required
def avatar_file(filename: str) -> Any:
    """Serve user avatars from the avatars directory."""
    avatars_dir = str(current_app.config["AVATARS_DIR"])
    return send_from_directory(avatars_dir, filename)


def _resolve_drive(
    videos: List[dict], videos_dir: Any
) -> Tuple[List[dict], Optional[int]]:
    """Apply the local-cache and Drive-existence filters concurrently.

    Returns ``(kept, drive_free)``. ``kept`` are the videos to show: a video this
    server holds media for - its MP4 copy or a ready HLS tree (§38) - is flagged
    ``is_cached=True`` and needs no Drive call at all; one kept only because a
    Drive copy still exists is flagged ``is_cached=False``. A video with nothing
    on disk whose Drive file is gone (definitive 404) is dropped, while a
    ``None`` from :func:`drive.exists` (API unavailable) keeps it so a transient
    error never hides a valid video. ``drive_free`` is the remaining Drive quota in
    bytes, or ``None`` when the API is unavailable.

    Every Drive call (the per-video existence checks plus the single quota read)
    runs in parallel so the page costs ~one network round trip instead of one
    per video.
    """
    kept: List[dict] = []
    pending: List[Tuple[dict, str]] = []
    for video in videos:
        # §16.4: annotate whether this server holds media for the row so the card
        # can badge the Drive-only ones. §38: "holds media" means its MP4 copy OR a
        # ready HLS tree, because keep_source false deliberately clears
        # local_filename - a repaired video must not depend on a live Drive call
        # to stay on the page, and must not be badged "Remote" while its segments
        # are sitting in this server's own cache.
        if storage.has_local_media(videos_dir, video):
            video["is_cached"] = True
            kept.append(video)
            continue
        drive_id = video.get("google_drive_file_id")
        if not drive_id:
            continue  # neither a local cache nor a Drive reference
        pending.append((video, drive_id))

    drive_free: Optional[int] = None
    workers = max(1, min(_DRIVE_FANOUT_WORKERS, len(pending) + 1))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        quota_fut = pool.submit(drive.get_drive_quota)
        exists_futs = {
            pool.submit(drive.exists, drive_id): (video, drive_id)
            for video, drive_id in pending
        }
        try:
            quota = quota_fut.result()
        except Exception as exc:  # noqa: BLE001 - a failed read is "unknown"
            logger.warning("home: drive quota failed: %s", exc)
            quota = None
        drive_free = quota[2] if quota is not None else None
        for fut in concurrent.futures.as_completed(exists_futs):
            video, drive_id = exists_futs[fut]
            try:
                exists = fut.result()
            except Exception as exc:  # noqa: BLE001 - a failed check is "unknown"
                logger.warning("home: drive exists failed for %s: %s", drive_id, exc)
                exists = None
            if exists is False:
                logger.info(
                    "home: hiding video id=%s title=%r (no local copy, Drive file %s gone)",
                    video.get("id"), video.get("title"), drive_id,
                )
                continue
            # Kept only because a Drive copy still exists -> not cached locally.
            video["is_cached"] = False
            kept.append(video)
    return kept, drive_free
