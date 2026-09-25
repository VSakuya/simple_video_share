"""Home blueprint: waterfall listing of all videos (P4)."""

import logging
import os
from typing import Any

from flask import Blueprint, current_app, render_template, send_from_directory
from .. import db, drive, storage
from ..auth import login_required

home_bp = Blueprint("home", __name__)
logger = logging.getLogger("simple_video_share.routes.home")


@home_bp.route("/")
@login_required
def index() -> Any:
    videos = db.list_folder_videos(None)
    videos = _drop_missing(videos, current_app.config["VIDEOS_DIR"])
    return _render_gallery(
        videos=videos,
        subfolders=db.list_root_folders(),
        current_folder=None,
        breadcrumbs=[],
    )


@home_bp.route("/folder/<int:folder_id>")
@login_required
def folder(folder_id: int) -> Any:
    from flask import abort
    current_folder = db.get_folder(folder_id)
    if current_folder is None:
        abort(404)
    videos = db.list_folder_videos(folder_id)
    videos = _drop_missing(videos, current_app.config["VIDEOS_DIR"])
    return _render_gallery(
        videos=videos,
        subfolders=db.list_child_folders(folder_id),
        current_folder=current_folder,
        breadcrumbs=_breadcrumbs(current_folder),
    )


def _render_gallery(**kwargs: Any) -> Any:
    # The home page reports *cache* remaining space (from the admin-configured
    # Max cache cap), not raw disk free space. Real disk free space is an
    # admin concern and is shown on the admin page only.
    cap = storage.max_cache_bytes(current_app.config)
    cached = db.sum_cached_bytes()
    remaining = max(0, cap - cached) if cap > 0 else 0
    quota = drive.get_drive_quota()
    drive_free = quota[2] if quota is not None else None
    return render_template(
        "home.html",
        cached_bytes=cached,
        cache_cap_bytes=cap,
        cache_remaining_bytes=remaining,
        drive_free_bytes=drive_free,
        **kwargs,
    )


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


def _drop_missing(videos: list[dict[str, Any]], videos_dir: Any) -> list[dict[str, Any]]:
    """Hide videos whose backing file is gone (no local copy AND no Drive file).

    A video stays visible as long as either a local cache or a Drive copy
    exists. When a Drive file is deleted (definitive 404) and there is no local
    cache, the video has nothing to stream and is removed from the listing. A
    ``None`` from :func:`drive.exists` (Drive API unavailable) keeps the video
    so a transient error never hides a valid one.
    """
    kept: list[dict[str, Any]] = []
    base = str(videos_dir)
    for video in videos:
        local = video.get("local_filename")
        if local and os.path.exists(os.path.join(base, local)):
            kept.append(video)
            continue
        drive_id = video.get("google_drive_file_id")
        if not drive_id:
            continue  # neither a local cache nor a Drive reference
        if drive.exists(drive_id) is False:
            logger.info(
                "home: hiding video id=%s title=%r (no local copy, Drive file %s gone)",
                video.get("id"), video.get("title"), drive_id,
            )
            continue
        kept.append(video)
    return kept
