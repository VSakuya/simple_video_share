"""Admin blueprint: manage all videos/users + global settings (P6).

§16.3: the admin area is a hub (``index``) plus one subpage per feature
(``videos`` / ``users`` / ``rooms`` / ``settings`` / ``logs``). §16.1: the
mutating actions answer with JSON when called via fetch (``X-Requested-With``)
so the client can update the DOM without a reload; otherwise flash + redirect.
"""

import io
import logging
import os
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.security import generate_password_hash

from .. import db, drive, storage, ws
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


def _is_ajax() -> bool:
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _storage_stats() -> dict[str, Any]:
    """Disk / drive / cache numbers shown on the settings subpage (§15.5)."""
    base_dir = os.path.dirname(str(current_app.config["VIDEOS_DIR"]))
    disk_free = storage.free_space_bytes(base_dir)
    quota = drive.get_drive_quota()
    drive_free = quota[2] if quota is not None else None
    cap = storage.max_cache_bytes(current_app.config)
    cached = db.sum_cached_bytes()
    return {
        "cached_bytes": cached,
        "cache_cap_bytes": cap,
        "disk_free_bytes": disk_free,
        "drive_free_bytes": drive_free,
    }


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
    """Hub: one card per admin feature, each linking to its subpage (§16.3)."""
    return render_template(
        "admin.html",
        video_count=len(db.list_all_videos()),
        user_count=len(db.list_users()),
        room_count=len(db.list_live_rooms()),
    )


@admin_bp.route("/videos")
@admin_required
def videos() -> str:
    """All videos: view, pin (§16.6) or delete."""
    return render_template("admin_videos.html", videos=db.list_all_videos())


@admin_bp.route("/users")
@admin_required
def users() -> str:
    """All users + create a new one."""
    return render_template("admin_users.html", users=db.list_users())


@admin_bp.route("/rooms")
@admin_required
def rooms() -> str:
    """Live rooms (created by users; an admin can only delete them)."""
    return render_template("admin_rooms.html", live_rooms=db.list_live_rooms())


@admin_bp.route("/settings")
@admin_required
def settings() -> str:
    """Global transcode settings + disk / drive / cache stats (§15.5)."""
    return render_template("admin_settings.html", settings=db.get_all_settings(), **_storage_stats())


@admin_bp.route("/logs")
@admin_required
def logs() -> str:
    """Log viewer page (§16.3); the log lines come from ``logs_data``."""
    return render_template("admin_logs.html")


@admin_bp.route("/settings", methods=["POST"])
@admin_required
def save_settings() -> Any:
    """Persist the editable settings from the admin form (§16.1: async on AJAX)."""
    for key in _EDITABLE_SETTINGS:
        raw = request.form.get(key)
        if raw is None:
            continue
        value = str(raw).strip()
        if key in ("default_bitrate", "max_resolution", "max_fps", "min_free_space_bytes", "max_cache_mb"):
            try:
                int(value)
            except (TypeError, ValueError):
                if _is_ajax():
                    return jsonify(ok=False, error=f"'{key}' must be a number."), 400
                flash(f"'{key}' must be a number.", "error")
                continue
        if key == "min_free_space_bytes":
            # The form edits the threshold in MB; persist it in bytes.
            value = str(int(value) * 1024 * 1024)
        db.set_setting(key, value)
    if _is_ajax():
        return jsonify(ok=True, message="Settings saved.")
    flash("Settings saved.", "success")
    return redirect(url_for("admin.settings"))


@admin_bp.route("/live-rooms/<int:room_id>/delete", methods=["POST"])
@admin_required
def delete_live_room(room_id: int) -> Any:
    """Admin deletes a room (and its cover file, if any)."""
    room = db.get_live_room(room_id)
    if room is None:
        if _is_ajax():
            return jsonify(ok=False, error="Room not found."), 400
        flash("Room not found.", "error")
        return redirect(url_for("admin.rooms"))
    if room.get("cover_filename"):
        _delete_cover_file(room["cover_filename"])
    db.delete_live_room(room_id)
    if _is_ajax():
        return jsonify(ok=True, message="Live room deleted.", room_id=room_id)
    flash("Live room deleted.", "success")
    return redirect(url_for("admin.rooms"))


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
        if _is_ajax():
            return jsonify(ok=False, error="Username and password are required."), 400
        flash("Username and password are required.", "error")
        return redirect(url_for("admin.users"))
    if len(password) < 6:
        if _is_ajax():
            return jsonify(ok=False, error="Password must be at least 6 characters."), 400
        flash("Password must be at least 6 characters.", "error")
        return redirect(url_for("admin.users"))
    if db.get_user_by_username(username):
        if _is_ajax():
            return jsonify(ok=False, error="Username already taken."), 400
        flash("Username already taken.", "error")
        return redirect(url_for("admin.users"))
    user_id = db.create_user(username, generate_password_hash(password), is_admin=is_admin)
    if _is_ajax():
        row = render_template("_user_row.html", u=db.get_user_by_id(user_id))
        return jsonify(ok=True, message=f"Created user '{username}'.", html=row)
    flash(f"Created user '{username}'.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.route("/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id: int) -> Any:
    """Admin deletes a user. Self-deletion is not allowed."""
    me = current_user()
    if me is not None and user_id == me["id"]:
        if _is_ajax():
            return jsonify(ok=False, error="You cannot delete your own account."), 400
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("admin.users"))
    db.delete_user(user_id)
    if _is_ajax():
        return jsonify(ok=True, message="User deleted.", user_id=user_id)
    flash("User deleted.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.route("/logs/data")
@admin_required
def logs_data() -> Any:
    """JSON log lines for the admin log viewer (filtered by level, newest last).

    ``/admin/logs`` is now the HTML subpage (§16.3); the viewer fetches its lines
    from here.
    """
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


@admin_bp.route("/videos/<int:video_id>/pin", methods=["POST"])
@admin_required
def pin_video(video_id: int) -> Any:
    """Flip a video's pinned flag (§16.6); the Videos table drives this via
    ``data-async`` so the response is always JSON (``{ok, pinned, video_id}``)."""
    video = db.get_video_by_id(video_id)
    if video is None:
        return jsonify(ok=False, error="Video not found."), 400
    pinned = 0 if video.get("is_pinned") else 1
    db.update_video(video_id, is_pinned=pinned)
    return jsonify(ok=True, pinned=pinned, video_id=video_id)


@admin_bp.route("/ws")
@admin_required
def websocket_test() -> Any:
    """Diagnostic: upgrade to WebSocket and echo frames back (§16.7).

    A non-upgrade request (a plain browser GET) gets a 400 so the admin page
    can tell "WS usable" apart from "not". The button on the Admin page opens
    a real WebSocket to this endpoint and reports the result.
    """
    if request.headers.get("Upgrade", "").lower() != "websocket":
        return "WebSocket upgrade required (send Upgrade: websocket).", 400
    key = request.headers.get("Sec-WebSocket-Key", "")
    if not key:
        return "Missing Sec-WebSocket-Key header.", 400
    try:
        ws.run_echo(request.environ, key)
    except ConnectionError as exc:
        # The socket is already gone (the client closed); nothing more to say.
        logger.info("ws test: connection ended (%s)", exc)
        return "", 200
    # The echo loop has taken over the socket. Return an empty direct-passthrough
    # body so Werkzeug writes no further HTTP to the (now WebSocket) socket.
    resp = Response(b"", status=200)
    resp.direct_passthrough = True
    return resp


@admin_bp.route("/export")
@admin_required
def export_bundle() -> Any:
    """Zip the storage directory (excluding the video cache and logs) for
    portable transfer (§16.8). Streams the archive to the client as a download.
    """
    storage_root = Path(os.path.dirname(str(current_app.config["VIDEOS_DIR"])))
    videos_dir = Path(str(current_app.config["VIDEOS_DIR"]))
    logs_dir = storage_root / "logs"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(storage_root.rglob("*")):
            if not path.is_file():
                continue
            if videos_dir in path.parents or logs_dir in path.parents:
                continue  # exclude the video cache and the log directory
            zf.write(path, path.relative_to(storage_root).as_posix())
    buf.seek(0)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"svs-export-{stamp}.zip",
    )


@admin_bp.route("/import", methods=["POST"])
@admin_required
def import_bundle() -> Any:
    """Extract a previously exported storage bundle (§16.8), with verification.

    The uploaded archive is validated before anything is written: it must be a
    readable zip and no entry may escape the storage root (path traversal).
    """
    storage_root = Path(os.path.dirname(str(current_app.config["VIDEOS_DIR"])))
    upload = request.files.get("archive")
    if upload is None or not upload.filename:
        if _is_ajax():
            return jsonify(ok=False, error="No archive uploaded."), 400
        flash("No archive uploaded.", "error")
        return redirect(url_for("admin.settings"))

    data = upload.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(data), "r")
    except zipfile.BadZipFile:
        if _is_ajax():
            return jsonify(ok=False, error="Not a valid zip archive."), 400
        flash("Not a valid zip archive.", "error")
        return redirect(url_for("admin.settings"))

    # Verify every entry stays within the storage root before extracting.
    root_resolved = storage_root.resolve()
    try:
        for info in zf.infolist():
            target = (root_resolved / info.filename).resolve()
            if not target.is_relative_to(root_resolved):
                raise ValueError(f"unsafe path in archive: {info.filename!r}")
    except ValueError as exc:
        if _is_ajax():
            return jsonify(ok=False, error=str(exc)), 400
        flash(str(exc), "error")
        return redirect(url_for("admin.settings"))

    try:
        zf.extractall(storage_root)
    except (zipfile.BadZipFile, OSError) as exc:
        if _is_ajax():
            return jsonify(ok=False, error=f"Extraction failed: {exc}"), 500
        flash(f"Extraction failed: {exc}", "error")
        return redirect(url_for("admin.settings"))

    count = len(zf.namelist())
    if _is_ajax():
        return jsonify(ok=True, files=count)
    flash(f"Imported {count} file(s) from the archive.", "success")
    return redirect(url_for("admin.settings"))
