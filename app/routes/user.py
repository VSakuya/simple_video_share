"""User blueprint: manage own videos + folders (P5)."""

import logging
import os
from pathlib import Path
from typing import Any

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from .. import db, drive, storage
from .. import drive_worker
from ..auth import current_user, login_required

user_bp = Blueprint("user", __name__, url_prefix="/user")
logger = logging.getLogger("simple_video_share.routes.user")


@user_bp.route("/")
@login_required
def index() -> str:
    me = current_user()
    videos = db.list_videos_by_owner(me["id"]) if me else []
    folders = db.list_folders(me["id"]) if me else []
    return render_template("user.html", videos=videos, folders=folders)


@user_bp.route("/progress")
@login_required
def progress() -> Any:
    """Return in-flight Drive upload progress (JSON, polled by the UI)."""
    return jsonify(drive_worker.worker.snapshot())


@user_bp.route("/videos/<int:video_id>/retry", methods=["POST"])
@login_required
def retry_video(video_id: int) -> Any:
    """Re-run the background Drive upload for a failed (or stuck) video."""
    me = current_user()
    assert me is not None
    video = db.get_video_by_id(video_id)
    if video is None:
        flash("Video not found.", "error")
        return redirect(url_for("user.index"))
    if video["owner_id"] != me["id"] and not me.get("is_admin"):
        flash("You can only retry your own videos.", "error")
        return redirect(url_for("user.index"))
    local = video.get("local_filename")
    videos_dir = current_app.config["VIDEOS_DIR"]
    local_path = os.path.join(str(videos_dir), local) if local else ""
    if not local_path or not os.path.exists(local_path):
        flash("No local copy to upload — this video cannot be retried.", "error")
        return redirect(url_for("user.index"))
    drive_filename = drive.make_drive_filename(video["title"], int(video["size_bytes"] or 0))
    db.update_video(video_id, status="uploading")
    base_dir = str(os.path.dirname(str(videos_dir)))
    drive_worker.worker.enqueue(
        video_id,
        video["title"],
        local_path,
        drive_filename,
        int(video["size_bytes"] or 0),
        current_app.config.get("DRIVE_FOLDER_ID", ""),
        base_dir,
        str(videos_dir),
        current_app.config,
        resumable_uri=video.get("drive_resumable_uri"),
    )
    flash(f"Retrying upload for '{video['title']}'.", "success")
    return redirect(url_for("user.index"))


@user_bp.route("/videos/<int:video_id>/delete", methods=["POST"])
@login_required
def delete_video(video_id: int) -> Any:
    """Delete one of the owner's videos: Drive file, local cache, cover, row."""
    me = current_user()
    assert me is not None
    video = db.get_video_by_id(video_id)
    if video is None:
        flash("Video not found.", "error")
        return redirect(url_for("user.index"))
    if video["owner_id"] != me["id"] and not me.get("is_admin"):
        flash("You can only delete your own videos.", "error")
        return redirect(url_for("user.index"))
    _purge_video_files(video)
    db.delete_video(video_id)
    flash("Video deleted.", "success")
    return redirect(url_for("user.index"))


@user_bp.route("/videos/<int:video_id>/edit", methods=["POST"])
@login_required
def edit_video(video_id: int) -> Any:
    """Update a video's title and (optionally) its cover image."""
    me = current_user()
    assert me is not None
    video = db.get_video_by_id(video_id)
    if video is None:
        flash("Video not found.", "error")
        return redirect(url_for("user.index"))
    if video["owner_id"] != me["id"] and not me.get("is_admin"):
        flash("You can only edit your own videos.", "error")
        return redirect(url_for("user.index"))

    title = (request.form.get("title") or "").strip()
    if not title:
        flash("Title is required.", "error")
        return redirect(url_for("user.index"))

    description = (request.form.get("description") or "").strip() or None
    updates: dict[str, Any] = {"title": title, "description": description}
    cover_file = request.files.get("cover")
    if cover_file is not None and cover_file.filename:
        covers_dir = Path(current_app.config["COVERS_DIR"])
        new_cover = storage.save_cover(cover_file, covers_dir, current_app.config)
        if new_cover is None:
            flash("Could not save the cover image.", "error")
            return redirect(url_for("user.index"))
        if new_cover != video.get("cover_filename"):
            old_cover = video.get("cover_filename")
            if old_cover:
                _unlink(covers_dir / old_cover)
            updates["cover_filename"] = new_cover

    db.update_video(video_id, **updates)
    flash("Video updated.", "success")
    return redirect(url_for("user.index"))


def _purge_video_files(video: dict[str, Any]) -> None:
    """Best-effort removal of a video's files before dropping its DB row.

    Mirrors the upload flow's cleanup: a failure to remove a file (a locked
    handle, a transient Drive error) logs a warning and does NOT stop the row
    from being deleted, so the UI stays consistent. The Drive file is removed
    first (slowest and most likely to fail), then the local cache and cover.
    """
    drive_id = video.get("google_drive_file_id")
    if drive_id:
        try:
            drive.delete_file(drive_id)
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            logger.warning("purge: could not delete Drive file %s: %s", drive_id, exc)

    videos_dir = str(current_app.config["VIDEOS_DIR"])
    covers_dir = str(current_app.config["COVERS_DIR"])
    local = video.get("local_filename")
    if local:
        _unlink(Path(videos_dir) / local)
    cover = video.get("cover_filename")
    if cover:
        _unlink(Path(covers_dir) / cover)


def _unlink(path: Path) -> None:
    if path.exists():
        try:
            path.unlink()
        except OSError as exc:  # noqa: BLE001 - best-effort cleanup
            logger.warning("purge: could not remove %s: %s", path, exc)
