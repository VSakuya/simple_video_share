"""Admin blueprint: manage all videos/users + global settings (P6)."""

import logging
import re
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
