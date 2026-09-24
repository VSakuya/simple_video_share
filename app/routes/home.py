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
    videos = db.list_public_videos()
    videos = _drop_missing(videos, current_app.config["VIDEOS_DIR"])
    base_dir = os.path.dirname(str(current_app.config["VIDEOS_DIR"]))
    local_free = storage.free_space_bytes(base_dir)
    quota = drive.get_drive_quota()
    drive_free = quota[2] if quota is not None else None
    return render_template(
        "home.html",
        videos=videos,
        local_free_bytes=local_free,
        drive_free_bytes=drive_free,
    )


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
