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

from .. import db, drive, storage
from ..auth import admin_required, current_user, login_required
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


def _action_answer(endpoint: str, ok: bool, message: str, **extra: Any) -> Any:
    """Answer one admin-table action both ways (16.1).

    ``data-async`` forms get JSON so the table row updates in place; a plain form
    (or a browser without async.js) gets the flash + redirect every other admin
    action uses. ``extra`` carries whatever the page handler needs to render -
    e.g. how many bytes a cache drop just gave back.
    """
    if _is_ajax():
        payload: dict[str, Any] = {"ok": ok, "message": message}
        if not ok:
            # async.js toasts ``error`` (its fallback text is the generic "Something
            # went wrong"), so a refusal that only fills message reaches the admin as
            # that line instead of as a reason. Every refusal in this blueprint goes
            # through here so none of them can regress to that text (§36.1, §37.3).
            payload["error"] = message
        payload.update(extra)
        return (jsonify(payload), 200 if ok else 400)
    flash(message, "success" if ok else "error")
    return redirect(url_for(endpoint))


def _video_action_answer(ok: bool, message: str, **extra: Any) -> Any:
    """Videos-table shorthand for :func:`_action_answer`."""
    return _action_answer("admin.videos", ok, message, **extra)


def _user_action_answer(ok: bool, message: str, **extra: Any) -> Any:
    """Users-table shorthand for :func:`_action_answer` (§37.3).

    The user delete used to answer only with flash + redirect and let the DB error
    escape as a 500, which is why the page said "Something went wrong".
    """
    return _action_answer("admin.users", ok, message, **extra)


def _fmt_mb(n: int) -> str:
    """Bytes as the MB string the admin table speaks in (rounds to 1 decimal)."""
    return "%.1f MB" % (max(0, int(n)) / 1048576.0)


def _storage_stats() -> dict[str, Any]:
    """Disk / drive / cache numbers shown on the settings subpage (§15.5)."""
    base_dir = os.path.dirname(str(current_app.config["VIDEOS_DIR"]))
    disk_free = storage.free_space_bytes(base_dir)
    quota = drive.get_drive_quota()
    drive_free = quota[2] if quota is not None else None
    cap = storage.max_cache_bytes(current_app.config)
    cached = storage.actual_cache_bytes(current_app.config["VIDEOS_DIR"])
    return {
        "cached_bytes": cached,
        "cache_cap_bytes": cap,
        "disk_free_bytes": disk_free,
        "drive_free_bytes": drive_free,
    }


def _hls_status() -> dict[str, Any]:
    """Read-only HLS status for the settings subpage (§31.8).

    The HLS switches deliberately live in config.json rather than this form: one
    repair costs minutes of single-core CPU per video, so it must never be one
    stray POST away. What the page shows is what the server can currently do.
    """
    from .. import hls
    cfg = hls.settings(current_app.config)
    states: dict[str, int] = {}
    for video in db.list_all_videos():
        key = str(video.get("stream_state") or "none")
        states[key] = states.get(key, 0) + 1
    return {
        "enabled": cfg["enabled"],
        "repair_enabled": cfg["repair_enabled"],
        "segment_seconds": cfg["segment_seconds"],
        "keep_source": cfg["keep_source"],
        "ffmpeg": hls.ffmpeg_binary(cfg) or "not found",
        "preset": cfg["repair_preset"],
        "crf": cfg["repair_crf"],
        "states": states,
        "ready_videos": states.get("ready", 0),
        "total_videos": sum(states.values()),
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
        tag_count=len(db.list_tags()),
    )


@admin_bp.route("/videos")
@admin_required
def videos() -> str:
    """All videos: view, pin (§16.6) or delete."""
    from .. import batch, hls

    # §38: "Cached" has to mean what the home cards already mean - this server
    # holds media for the row, either its MP4 copy or a ready HLS tree. A repaired
    # video under hls_keep_source false has no local_filename at all yet still
    # occupies its segments, so the column read "no" beside a Drop cache button
    # that had 1.5 GB to free.
    videos_dir = Path(current_app.config["VIDEOS_DIR"])
    rows = db.list_all_videos()
    for row in rows:
        row["has_cached_media"] = storage.has_local_media(videos_dir, row)
        row["stream_actions"] = hls.stream_actions(videos_dir, row, current_app.config)

    return render_template(
        "admin_videos.html",
        videos=rows,
        hls_batch=batch.status(),
    )


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
    return render_template(
        "admin_settings.html",
        settings=db.get_all_settings(),
        **_storage_stats(),
        hls_info=_hls_status(),
    )


@admin_bp.route("/logs")
@admin_required
def logs() -> str:
    """Log viewer page (§16.3); the log lines come from ``logs_data``."""
    return render_template("admin_logs.html")


@admin_bp.route("/data")
@admin_required
def data() -> str:
    """Data & diagnostics subpage (§16.3): export/import + WebSocket test."""
    return render_template("admin_data.html")


@admin_bp.route("/tags")
@login_required
def tags() -> str:
    """All tags: any user creates, admins delete (§bug L67)."""
    return render_template("admin_tags.html", tags=db.list_tags())


@admin_bp.route("/tags/create", methods=["POST"])
@login_required
def create_tag() -> Any:
    """Create a tag; returns the new row as HTML so the async form appends it."""
    name = (request.form.get("name") or "").strip()
    if not name:
        if _is_ajax():
            return jsonify(ok=False, error="Tag name is required."), 400
        flash("Tag name is required.", "error")
        return redirect(url_for("admin.tags"))
    try:
        tag_id = db.create_tag(name)
    except Exception:  # UNIQUE violation → a tag with this name already exists
        if _is_ajax():
            return jsonify(ok=False, error="A tag with that name already exists."), 409
        flash("A tag with that name already exists.", "error")
        return redirect(url_for("admin.tags"))
    row_html = render_template("_tag_row.html", t={"id": tag_id, "name": name})
    if _is_ajax():
        return jsonify(ok=True, message="Tag created.", html=row_html, tag_id=tag_id, name=name)
    flash("Tag created.", "success")
    return redirect(url_for("admin.tags"))


@admin_bp.route("/tags/<int:tag_id>/delete", methods=["POST"])
@admin_required
def delete_tag(tag_id: int) -> Any:
    """Delete a tag (its video links are removed too)."""
    db.delete_tag(tag_id)
    if _is_ajax():
        return jsonify(ok=True, message="Tag deleted.")
    flash("Tag deleted.", "success")
    return redirect(url_for("admin.tags"))


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


@admin_bp.route("/users/<int:user_id>/role", methods=["POST"])
@admin_required
def set_user_role(user_id: int) -> Any:
    value = request.form.get("is_admin")
    if value not in ("0", "1"):
        return _user_action_answer(False, "Invalid role.")
    me = current_user()
    assert me is not None
    ok, message = db.set_user_admin(me["id"], user_id, value == "1")
    if not ok:
        return _user_action_answer(False, message)
    user = next((u for u in db.list_users() if u["id"] == user_id), None)
    if user is None:
        return _user_action_answer(False, "User no longer exists.")
    role = "administrator" if value == "1" else "regular user"
    logger.info("admin: user_id=%s changed user_id=%s role to %s", me["id"], user_id, role)
    html = render_template("_user_row.html", u=user)
    return _user_action_answer(True, f"{user['username']} is now a {role}.", html=html, user_id=user_id, is_admin=value == "1")


@admin_bp.route("/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id: int) -> Any:
    """Admin deletes a user, everything they own, and every file their videos occupy.

    Files are purged BEFORE the row is dropped: the FK cascade (§37.1/§37.2) is
    what removes their video rows, and after it nothing is left to read the Drive
    ids, cache paths, stream trees and covers from. Skipping that step is how a user
    delete used to strand whole folders of cache on disk (§37.4). The avatar goes too.

    The cascade itself is now answered instead of crashing: an IntegrityError here
    used to escape as a 500 and reach the admin as "Something went wrong".
    """
    me = current_user()
    if me is not None and user_id == me["id"]:
        return _user_action_answer(False, "You cannot delete your own account.")
    user = db.get_user_by_id(user_id)
    if user is None:
        return _user_action_answer(False, "User not found.")

    impact = db.user_impact(user_id)
    freed = _purge_user_files(user_id)
    try:
        db.delete_user(user_id)
    except Exception as exc:  # noqa: BLE001 - a failed cascade must not be a mystery
        logger.exception("admin: deleting user %s failed", user_id)
        return _user_action_answer(
            False,
            "Delete failed in the database: %s. Their files were already removed, so"
            " the rows may need a retry." % (exc,),
        )
    message = "Deleted '%s'." % user["username"]
    owned = _impact_text(impact)
    if owned:
        message += " " + owned
    if freed:
        # The Videos table speaks in MB; a small user delete should not round to
        # "0.0 MB" when it really gave back a few hundred kilobytes.
        size = _fmt_mb(freed) if freed >= 1048576 else "%d KB" % (freed // 1024)
        message += " Freed %s of local cache." % size
    logger.info("admin: %s", message)
    return _user_action_answer(True, message, user_id=user_id)


def _purge_user_files(user_id: int) -> int:
    """Delete every file one user owns on Drive and on disk. Returns cache bytes freed.

    Runs before :func:`db.delete_user` for the reason stated there. Covers owned by
    their live room and their avatar are included; a file that will not go is logged
    by the storage helper and never blocks the delete, same policy as the cache
    evictor.
    """
    videos_dir = Path(current_app.config["VIDEOS_DIR"])
    covers_dir = str(current_app.config["COVERS_DIR"])
    freed = 0
    for video in db.list_videos_by_owner(user_id):
        freed += storage.purge_video_files(video, str(videos_dir.parent), covers_dir)
    room = db.get_live_room_by_owner(user_id)
    if room is not None:
        storage.remove_stored_file(Path(covers_dir), room.get("cover_filename"), "room cover")
    owner = db.get_user_by_id(user_id)
    if owner is not None:
        storage.remove_stored_file(
            Path(current_app.config["AVATARS_DIR"]),
            owner.get("avatar_filename"),
            "avatar",
        )
    return freed


def _impact_text(impact: dict[str, int]) -> str:
    """'69 videos, 7 comments and 1 live room went with them.' - or "" for an empty user.

    Same numbers the Users table shows next to the name, so what the admin was
    warned about and what actually happened are the same sentence.
    """
    parts: list[str] = []
    for key, label in (
        ("video_count", "video"),
        ("comment_count", "comment"),
        ("room_count", "live room"),
        ("folder_count", "folder"),
    ):
        count = int(impact.get(key) or 0)
        if count:
            parts.append("%d %s%s" % (count, label, "s" if count != 1 else ""))
    if not parts:
        return ""
    if len(parts) == 1:
        joined = parts[0]
    else:
        joined = ", ".join(parts[:-1]) + " and " + parts[-1]
    return "%s went with them." % joined


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


def _stream_action(video_id: int, mode: str) -> Any:
    """Start one stream job for one video, or explain why it was refused (32.3).

    Every refusal is a flash + redirect back to the Videos table, so the reason
    shows up next to the badge instead of on a blank page. Nothing here runs ffmpeg
    in the request: hls.start_build hands it to a daemon thread, and ensure_stream
    owns the per-video lock, so a double click is a no-op rather than a second ffmpeg.
    """
    from .. import hls
    video = db.get_video_by_id(video_id)
    if video is None:
        flash("Video not found.", "error")
        return redirect(url_for("admin.videos"))
    videos_dir = Path(current_app.config["VIDEOS_DIR"])
    actions = hls.stream_actions(videos_dir, video, current_app.config)
    blocker = actions["repair_blocker" if mode == hls.MODE_REPAIR else "rebuild_blocker"]
    if blocker:
        return _video_action_answer(False, blocker, video_id=video_id)
    cfg = hls.settings(current_app.config)
    if not cfg["enabled"]:
        flash("HLS is off (hls_enabled = false in config.json).", "error")
        return redirect(url_for("admin.videos"))
    if hls.ffmpeg_binary(cfg) is None:
        flash("ffmpeg was not found on this server, so nothing can be segmented.", "error")
        return redirect(url_for("admin.videos"))
    if mode == hls.MODE_REPAIR and not cfg["repair_enabled"]:
        flash("GOP repair is off (hls_repair_enabled = false in config.json).", "error")
        return redirect(url_for("admin.videos"))
    source = hls.source_path_for(videos_dir, video)
    if source is None:
        # A stream is derived from the MP4, so a cache-evicted video has to be
        # pulled back from Drive (its watch page: 'Download & play') before re-cutting.
        flash(
            "No local copy on this server: open the video and download it from "
            "Drive first, then rebuild.",
            "error",
        )
        return redirect(url_for("admin.videos"))
    # 32.4 + 33.8 pre-check; the hard guard lives in hls.ensure_stream. A job that is
    # going to run out of disk is refused here - in MB, while an admin is watching the
    # badge - rather than 25 minutes into an encode or halfway through a remux.
    from .. import storage

    source_bytes = source.stat().st_size
    factor = 1.35 if mode == hls.MODE_REPAIR else 2.0
    needed = int(source_bytes * factor)
    headroom = storage.free_space_bytes(str(videos_dir.parent)) - storage.min_free_space_bytes(
        current_app.config
    )
    if headroom < needed:
        flash(
            "%s needs about %d MB of free headroom for this file, this server has %d MB: "
            "free cache space first (a build may evict LRU videos to make room)."
            % ("Repair" if mode == hls.MODE_REPAIR else "Rebuild",
               needed // 1048576, max(headroom, 0) // 1048576),
            "error",
        )
        return redirect(url_for("admin.videos"))

    started = hls.start_build(
        videos_dir, video, current_app.config, mode=mode, reason="admin",
    )
    if not started:
        flash("Could not start the stream job.", "error")
    elif mode == hls.MODE_REPAIR:
        flash(
            "GOP repair started - one core, roughly 1 s per second of video. "
            "Progress is shown in the Stream column.",
            "success",
        )
    else:
        flash(
            "Stream rebuild started (copy-only remux). If the source keyframe "
            "interval is longer than the segment target the tree is discarded and "
            "the badge stays 'none' - that video needs Repair GOP.",
            "success",
        )
    return redirect(url_for("admin.videos"))


@admin_bp.route("/videos/<int:video_id>/stream/rebuild", methods=["POST"])
@admin_required
def rebuild_stream(video_id: int) -> Any:
    """Re-cut the HLS stream of one video, copy-only (§31.1, §32.3).

    No re-encode: seconds of wall time per 30 s of video. It only produces useful
    segments when the source already has a short keyframe interval, i.e. anything
    encoded after the 30.1 pipeline fix.
    """
    return _stream_action(video_id, "copy")


@admin_bp.route("/videos/<int:video_id>/stream/repair", methods=["POST"])
@admin_required
def repair_stream(video_id: int) -> Any:
    """GOP repair of one video (32.1): re-encode to H.264 with a short keyframe
    interval, then segment that copy-only.

    Admin-triggered only (32.2) - it costs roughly 1 s of single-core CPU per second
    of video. The Google Drive original and every DB field (title, tags, cover,
    comments, folder) stay untouched, which is exactly what makes this the
    no-re-upload path for the existing library.
    """
    return _stream_action(video_id, "repair")


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


@admin_bp.route("/videos/<int:video_id>/cache/delete", methods=["POST"])
@admin_required
def delete_video_cache(video_id: int) -> Any:
    """Drop the local cache of one video - the MP4 copy and its HLS tree (§35).

    Until now the Videos table had one destructive action: Delete, which removes the
    Drive file, the row and the local copy together. Disk is the scarce resource on
    this server, so freeing one video worth of it has to be possible without deleting
    the video: Drive keeps the original, and the watch page can pull it back again.

    ``storage.cache_drop_blocker`` owns the two rules that protect the original (no
    Drive copy, upload in flight). The guard only this page can know is the third one:
    a stream job writing inside the very tree about to be removed.
    """
    from .. import hls

    video = db.get_video_by_id(video_id)
    if video is None:
        return _video_action_answer(False, "Video not found.", video_id=video_id)
    state = str(video.get("stream_state") or hls.STATE_NONE)
    if state in (hls.STATE_BUILDING, hls.STATE_REPAIRING):
        return _video_action_answer(
            False,
            "A stream job is running on this video: wait for the badge to settle "
            "before dropping its cache.",
            video_id=video_id,
            stream_state=state,
        )
    blocker = storage.cache_drop_blocker(video)
    if blocker:
        return _video_action_answer(False, blocker, video_id=video_id)
    base_dir = str(Path(current_app.config["VIDEOS_DIR"]).parent)
    footprint = storage.cache_footprint(base_dir, video)
    if footprint == 0 and not (video.get("local_filename") or video.get("stream_dir")):
        return _video_action_answer(
            True,
            "Nothing was cached on this server for that video.",
            video_id=video_id,
            cached=False,
        )
    freed = storage.drop_local_cache(base_dir, video)
    logger.info("admin: id=%s local cache dropped (%d bytes freed)", video_id, freed)
    return _video_action_answer(
        True,
        "Cache dropped: %s freed. The Google Drive copy and this row are untouched."
        % _fmt_mb(freed),
        video_id=video_id,
        freed_bytes=freed,
        cached=False,
        stream_state=hls.STATE_NONE,
    )


def _batch_action(mode: str) -> Any:
    """Start one whole-library batch, or explain the refusal (36.2).

    The gates are the ones _stream_action already applies to a single row - HLS off,
    no ffmpeg on this box, repair switched off, nothing to queue - because a batch is
    that same button run in order, not a different kind of job. batch.start hands the
    queue to a daemon thread, so no request here waits on ffmpeg.
    """
    from .. import batch

    started, message = batch.start(
        Path(current_app.config["VIDEOS_DIR"]), current_app.config, mode,
    )
    if started and mode == "repair":
        message += " One video at a time, on one core; the banner tracks it."
    return _video_action_answer(started, message)


@admin_bp.route("/videos/stream/rebuild-all", methods=["POST"])
@admin_required
def rebuild_all_streams() -> Any:
    """Copy-only remux for every locally cached row without a usable stream (§36).

    Cheap: about 30x realtime (31.3), and the candidate rule skips every row already
    ready, so a rebuild-all never re-cuts a working tree. It cannot fix a sparse
    keyframe interval - those rows land on "discarded" with a note naming Repair all.
    """
    return _batch_action("copy")


@admin_bp.route("/videos/stream/repair-all", methods=["POST"])
@admin_required
def repair_all_streams() -> Any:
    """GOP repair for every locally cached row that is not already repaired (§36).

    The expensive button, and the one the existing library actually needs: roughly 1 s
    of single-core per second of video (32.0), so the 14,384.8 s cached on the VPS is
    hours of work. The queue skips rows already ready (repair), so pressing it twice
    does not re-encode what it just finished. Drive originals and DB rows are untouched.
    """
    return _batch_action("repair")


@admin_bp.route("/videos/stream/batch/stop", methods=["POST"])
@admin_required
def stop_stream_batch() -> Any:
    """Stop the batch at the next video (36.0 decision 4).

    The video in progress is not killed: its ffmpeg finishes, validates and lands,
    because a cancelled encode mid-flight is how an abandoned .tmp tree happens.
    """
    from .. import batch

    stopped, message = batch.request_stop()
    return _video_action_answer(stopped, message)


@admin_bp.route("/videos/stream/batch")
@admin_required
def stream_batch_status() -> Any:
    """JSON progress for the banner poller (36.3). Read-only, no state change."""
    from .. import batch

    return jsonify(batch.status())


@admin_bp.route("/videos/stream/progress")
@admin_required
def stream_progress() -> Any:
    from .. import hls
    base = Path(current_app.config["VIDEOS_DIR"])
    jobs = []
    for video in db.list_all_videos():
        job = hls.progress(base, int(video["id"]))
        jobs.append(dict(job, video_id=video["id"], stream_state=video.get("stream_state"),
                         actions=hls.stream_actions(base, video, current_app.config)))
    response = jsonify(jobs=jobs)
    response.headers["Cache-Control"] = "no-store"
    return response


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
