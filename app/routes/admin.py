"""Admin blueprint: manage all videos/users + global settings (P6)."""

import logging
import re
from pathlib import Path
from typing import Any, Optional

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
from werkzeug.security import generate_password_hash

from .. import db, storage
from ..auth import admin_required, current_user
from ..log import LOG_DIR

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")
logger = logging.getLogger("simple_video_share.routes.admin")

#: Setting keys the admin settings form may write (whitelist). Everything else
#: is ignored so the form can never clobber an unknown/internal key.
_EDITABLE_SETTINGS = (
    "default_bitrate",
    "default_codec",
    "fallback_codec",
    "max_resolution",
    "max_fps",
    "min_free_space_bytes",
    "max_cache_mb",
)


#: Log files the admin viewer may read, keyed by the source select value.
_LOG_FILES = {
    "app": "app.log",
    "client": "client.log",
}

#: Log levels the viewer can filter on (logging's built-in level names).
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

#: Matches a log record's leading "<timestamp> <LEVEL>" so the level can be read
#: back from a line. Continuation lines (e.g. tracebacks) carry no level token
#: and inherit the level of the record they belong to.
_LOG_LINE_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s+([A-Z]+)")

#: Cap on how many matching lines a single request returns (newest last).
_MAX_LOG_LINES = 2000

#: A live-room stream link must be a full URL (``http(s)://...``) or a path on
#: the same host (``/live/<code>.flv``). Anything else is rejected (§14.6).
def _valid_live_url(url: str) -> bool:
    if not url:
        return False
    if url.startswith("/"):
        return True
    return url.startswith("http://") or url.startswith("https://")


def _read_filtered_log(path, level: str) -> list[dict[str, str]]:
    """Read ``path`` and return the lines matching ``level`` (newest last).

    ``level`` is ``"all"`` or one of ``_LOG_LEVELS``. Each returned item is
    ``{"level": <record level>, "text": <line>}``. A line with no level token
    (e.g. a traceback continuation line) inherits the level of the preceding
    record so it stays attached when filtering.
    """
    lines: list[dict[str, str]] = []
    current: Optional[str] = None
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            m = _LOG_LINE_RE.match(raw)
            lvl = m.group(1) if (m and m.group(1) in _LOG_LEVELS) else None
            if lvl is not None:
                current = lvl
            eff = lvl if lvl is not None else current
            if level != "all" and eff != level:
                continue
            lines.append({"level": eff or "", "text": raw.rstrip("\n")})
    return lines


@admin_bp.route("/")
@admin_required
def index() -> str:
    videos = db.list_all_videos()
    users = db.list_users()
    settings = db.get_all_settings()
    return render_template(
        "admin.html",
        videos=videos,
        users=users,
        settings=settings,
        live_rooms=db.list_live_rooms(),
        cached_bytes=db.sum_cached_bytes(),
        cache_cap_bytes=storage.max_cache_bytes(current_app.config),
    )


@admin_bp.route("/settings", methods=["POST"])
@admin_required
def save_settings() -> Any:
    """Persist the editable settings from the admin form."""
    for key in _EDITABLE_SETTINGS:
        raw = request.form.get(key)
        if raw is None:
            continue
        value = str(raw).strip()
        if key in ("default_bitrate", "max_resolution", "max_fps", "min_free_space_bytes", "max_cache_mb"):
            try:
                int(value)
            except (TypeError, ValueError):
                flash(f"'{key}' must be a number.", "error")
                continue
        if key == "min_free_space_bytes":
            # The form edits the threshold in MB; persist it in bytes.
            value = str(int(value) * 1024 * 1024)
        db.set_setting(key, value)
    flash("Settings saved.", "success")
    return redirect(url_for("admin.index"))


@admin_bp.route("/live-rooms", methods=["POST"])
@admin_required
def create_live_room() -> Any:
    """Admin adds a live room (stream link + title + optional cover)."""
    url = (request.form.get("url") or "").strip()
    title = (request.form.get("title") or "").strip()
    cover = request.files.get("cover")
    cover_filename = None
    if cover is not None and cover.filename:
        cover_filename = storage.save_cover(
            cover, Path(current_app.config["COVERS_DIR"]), current_app.config
        )
    if not _valid_live_url(url):
        flash("Stream link must be a full URL or a path starting with /.", "error")
    elif not title:
        flash("Room title is required.", "error")
    else:
        db.create_live_room(url, title, cover_filename)
        flash(f"Live room '{title}' created.", "success")
    return redirect(url_for("admin.index"))


@admin_bp.route("/live-rooms/<int:room_id>", methods=["POST"])
@admin_required
def update_live_room(room_id: int) -> Any:
    """Admin edits a room's stream link, title and/or cover."""
    room = db.get_live_room(room_id)
    if room is None:
        flash("Room not found.", "error")
        return redirect(url_for("admin.index"))
    url = (request.form.get("url") or "").strip()
    title = (request.form.get("title") or "").strip()
    cover = request.files.get("cover")
    new_cover = None
    if cover is not None and cover.filename:
        new_cover = storage.save_cover(
            cover, Path(current_app.config["COVERS_DIR"]), current_app.config
        )
    if not _valid_live_url(url):
        flash("Stream link must be a full URL or a path starting with /.", "error")
        return redirect(url_for("admin.index"))
    if not title:
        flash("Room title is required.", "error")
        return redirect(url_for("admin.index"))
    if new_cover and room.get("cover_filename"):
        _delete_cover_file(room["cover_filename"])
    db.update_live_room(room_id, url=url, title=title, cover_filename=new_cover)
    flash("Live room updated.", "success")
    return redirect(url_for("admin.index"))


@admin_bp.route("/live-rooms/<int:room_id>/delete", methods=["POST"])
@admin_required
def delete_live_room(room_id: int) -> Any:
    """Admin deletes a room (and its cover file, if any)."""
    room = db.get_live_room(room_id)
    if room is None:
        flash("Room not found.", "error")
    else:
        if room.get("cover_filename"):
            _delete_cover_file(room["cover_filename"])
        db.delete_live_room(room_id)
        flash("Live room deleted.", "success")
    return redirect(url_for("admin.index"))


def _delete_cover_file(filename: str) -> None:
    """Remove a cover file from disk (best effort)."""
    path = Path(current_app.config["COVERS_DIR"]) / filename
    if path.exists():
        try:
            path.unlink()
        except OSError:
            logger.warning("admin: could not delete cover file %s", filename)


@admin_bp.route("/users", methods=["POST"])
@admin_required
def create_user() -> Any:
    """Admin creates a new user — the only way to add users after bootstrap."""
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password", "")
    is_admin = 1 if request.form.get("is_admin") else 0
    if not username or not password:
        flash("Username and password are required.", "error")
    elif len(password) < 6:
        flash("Password must be at least 6 characters.", "error")
    elif db.get_user_by_username(username):
        flash("Username already taken.", "error")
    else:
        db.create_user(username, generate_password_hash(password), is_admin=is_admin)
        flash(f"Created user '{username}'.", "success")
    return redirect(url_for("admin.index"))


@admin_bp.route("/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id: int) -> Any:
    """Admin deletes a user. Self-deletion is not allowed."""
    me = current_user()
    if me is not None and user_id == me["id"]:
        flash("You cannot delete your own account.", "error")
    else:
        db.delete_user(user_id)
        flash("User deleted.", "success")
    return redirect(url_for("admin.index"))


@admin_bp.route("/logs")
@admin_required
def logs() -> Any:
    """Serve the admin log viewer: a log file filtered by level (newest last)."""
    source = request.args.get("source", "app")
    level = request.args.get("level", "all")
    if source not in _LOG_FILES:
        source = "app"
    if level != "all" and level not in _LOG_LEVELS:
        level = "all"
    path = LOG_DIR / _LOG_FILES[source]
    lines = _read_filtered_log(path, level) if path.exists() else []
    total = len(lines)
    truncated = total > _MAX_LOG_LINES
    if truncated:
        lines = lines[-_MAX_LOG_LINES:]
    return jsonify(source=source, level=level, total=total, truncated=truncated, lines=lines)
