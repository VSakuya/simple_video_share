"""Upload blueprint: receive client-side transcode/clip results and store them.

Flow (see project_requirements.md, Scenario 1):
1. Receive the video + optional cover + metadata from the browser.
2. Ensure enough free space (Drive quota + local disk reserve).
3. Save the video (unique name) to ``videos/`` and the cover (unique name) to
   ``covers/``.
4. Create the video record immediately with ``status='uploading'`` so the owner
   can stream the cached copy and watch progress; the shared home gallery only
   shows ``status='ready'`` videos.
5. Push the file to Google Drive on a background worker
   (``app.drive_worker``), which flips the row to ``'ready'`` or ``'failed'``,
   drops the local copy if disk is tight, and enforces the cache-capacity cap.
"""

import logging
import os
import shutil
import sys
import uuid
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
from ..auth import login_required
from ..log import log_client

logger = logging.getLogger("simple_video_share.routes.upload")

upload_bp = Blueprint("upload", __name__, url_prefix="/upload")


@upload_bp.route("/")
@login_required
def index() -> Any:
    from ..auth import current_user
    me = current_user()
    folders = db.list_folders(me["id"]) if me else []
    return render_template("upload.html", folders=folders)


@upload_bp.route("/log", methods=["POST"])
@login_required
def client_log() -> Any:
    """Persist browser pipeline logs (from upload.js) to storage/logs/client.log.

    The lines arrive already timestamped by the client; this is a diagnostic
    sink only and never affects the upload itself.
    """
    data = request.get_json(silent=True)
    lines = data.get("lines") if isinstance(data, dict) else None
    if isinstance(lines, str):
        lines = [lines]
    if not isinstance(lines, list):
        return jsonify(ok=False)
    cleaned = [str(x) for x in lines[:500] if x is not None]  # cap per request
    if cleaned:
        log_client(cleaned)
        logger.info("client log: %d line(s) received", len(cleaned))
    return jsonify(ok=True)


@upload_bp.route("/submit", methods=["POST"])
@login_required
def submit() -> Any:
    from ..auth import current_user
    me = current_user()
    assert me is not None
    # The upload page posts via fetch() and expects a JSON response; a plain
    # form submission (no header) gets the usual flash + redirect instead.
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    video_file = request.files.get("video")
    cover_file = request.files.get("cover")
    title = (request.form.get("title") or "").strip()
    if not title and video_file is not None:
        title = os.path.splitext(video_file.filename or "")[0] or "Untitled"
    folder_id = _optional_int(request.form.get("folder_id"))
    logger.info(
        "submit: user_id=%s title=%r size=%s cover=%s",
        me["id"],
        title,
        video_file.content_length if video_file is not None else 0,
        cover_file is not None,
    )

    def _fail(message: str, status: int = 400) -> Any:
        if is_ajax:
            return jsonify(ok=False, error=message), status
        flash(message, "error")
        return redirect(url_for("upload.index"))

    if video_file is None:
        return _fail("No video file received.")

    videos_dir = current_app.config["VIDEOS_DIR"]
    covers_dir = current_app.config["COVERS_DIR"]
    base_dir = str(os.path.dirname(str(videos_dir)))

    # --- 1. Space checks (Drive + local) ---
    incoming = _file_size(video_file)
    ok, msg = _check_space(base_dir, incoming)
    if not ok:
        return _fail(msg)

    # --- 2. Save video (unique name) + cover (unique name) ---
    video_name = _unique_video_name(title, videos_dir)
    storage.save_upload_as(video_file, videos_dir, video_name)
    cover_name: str | None = None
    if cover_file is not None:
        cover_name = storage.save_cover(cover_file, covers_dir, current_app.config)

    # --- 3. Create the DB row (status='uploading') + start the Drive upload ---
    video_id, err = _register_and_enqueue(
        me["id"], title, video_name, cover_name, incoming, folder_id,
        _meta_from_form(), videos_dir,
    )
    if video_id is None:
        _remove_quiet(os.path.join(str(videos_dir), video_name))
        if cover_name:
            _remove_quiet(os.path.join(str(covers_dir), cover_name))
        return _fail(err or "Could not register the video.", status=500)

    if is_ajax:
        return jsonify({"ok": True, "title": title, "video_id": video_id})
    flash(f"Uploaded '{title}' — now uploading to Google Drive.", "success")
    # My Videos (not home) shows the live Drive upload progress + retry (§13.18).
    return redirect(url_for("user.index"))


# ---------------------------------------------------------------------------
# Segmented upload (final MP4 > 1 GB) — project_requirements.md R4
# ---------------------------------------------------------------------------

# In-memory upload sessions (a restart mid-upload drops the session; the
# orphaned part directory is removed by the next finish, or by hand).
_seg_sessions: dict[str, dict[str, Any]] = {}
SEGMENT_SIZE = 512 * 1024 * 1024  # 512 MB per part (client-side policy)


@upload_bp.route("/seg/start", methods=["POST"])
@login_required
def seg_start() -> Any:
    from ..auth import current_user
    me = current_user()
    assert me is not None

    title = (request.form.get("title") or "").strip() or "Untitled"
    total_parts = _optional_int(request.form.get("total_parts")) or 0
    expected_size = _optional_int(request.form.get("expected_size")) or 0
    folder_id = _optional_int(request.form.get("folder_id"))
    if total_parts <= 0:
        return jsonify(ok=False, error="total_parts is required"), 400

    base_dir = str(os.path.dirname(str(current_app.config["VIDEOS_DIR"])))
    ok, msg = _check_space(base_dir, expected_size)
    if not ok:
        return jsonify(ok=False, error=msg), 400

    token = uuid.uuid4().hex
    _seg_sessions[token] = {
        "user_id": me["id"],
        "title": title,
        "total_parts": total_parts,
        "expected_size": expected_size,
        "folder_id": folder_id,
    }
    return jsonify(ok=True, token=token, segment_size=SEGMENT_SIZE)


@upload_bp.route("/seg/part", methods=["POST"])
@login_required
def seg_part() -> Any:
    token = request.form.get("token", "")
    part_index = _optional_int(request.form.get("part_index"))
    sess = _seg_sessions.get(token)
    if sess is None or part_index is None or part_index < 0:
        return jsonify(ok=False, error="Unknown session"), 400
    part_file = request.files.get("part")
    if part_file is None:
        return jsonify(ok=False, error="No part received"), 400
    seg_dir = Path(current_app.config["VIDEOS_DIR"]) / ".segments" / token
    seg_dir.mkdir(parents=True, exist_ok=True)
    dest = seg_dir / ("part_%05d" % part_index)
    with dest.open("wb") as fh:
        shutil.copyfileobj(part_file.stream, fh)
    return jsonify(ok=True, part_index=part_index)


@upload_bp.route("/seg/finish", methods=["POST"])
@login_required
def seg_finish() -> Any:
    token = request.form.get("token", "")
    sess = _seg_sessions.get(token)
    if sess is None:
        return jsonify(ok=False, error="Unknown session"), 400
    seg_dir = Path(current_app.config["VIDEOS_DIR"]) / ".segments" / token
    parts = sorted(seg_dir.glob("part_*")) if seg_dir.exists() else []
    if len(parts) != sess["total_parts"]:
        _rmtree_quiet(seg_dir)
        _seg_sessions.pop(token, None)
        return (
            jsonify(ok=False, error="Incomplete upload: %d/%d parts" % (len(parts), sess["total_parts"]))
        ), 400

    cover_file = request.files.get("cover")
    covers_dir = current_app.config["COVERS_DIR"]
    cover_name: str | None = None
    if cover_file is not None:
        cover_name = storage.save_cover(cover_file, covers_dir, current_app.config)

    videos_dir = Path(current_app.config["VIDEOS_DIR"])
    video_name = _unique_video_name(sess["title"], videos_dir)
    final_path = videos_dir / video_name
    with final_path.open("wb") as out:
        for p in parts:
            with p.open("rb") as fh:
                shutil.copyfileobj(fh, out)
    size_bytes = final_path.stat().st_size

    _rmtree_quiet(seg_dir)
    _seg_sessions.pop(token, None)

    video_id, err = _register_and_enqueue(
        sess["user_id"], sess["title"], video_name, cover_name, size_bytes,
        sess["folder_id"], _meta_from_form(), videos_dir,
    )
    if video_id is None:
        _remove_quiet(str(final_path))
        if cover_name:
            _remove_quiet(os.path.join(str(covers_dir), cover_name))
        return jsonify(ok=False, error=err or "Could not register the video."), 400

    return jsonify(ok=True, title=sess["title"], video_id=video_id)


def _check_space(base_dir: str, incoming: int) -> tuple[bool, str]:
    """Check both Google Drive quota and local disk space.

    Returns (True, "") when the upload fits, else (False, message).
    """
    quota = drive.get_drive_quota()
    logger.info(
        "space check: incoming=%d drive_remaining=%s",
        incoming, quota[2] if quota is not None else None,
    )
    if quota is not None and quota[2] < incoming:
        return False, "Not enough free space on Google Drive for this video."
    if not storage.ensure_space(base_dir, incoming, current_app.config):
        return False, "Not enough free disk space (reserve must stay at or above 500 MB)."
    return True, ""


def _register_and_enqueue(
    owner_id: int,
    title: str,
    video_name: str,
    cover_name: str | None,
    size_bytes: int,
    folder_id: int | None,
    meta: dict[str, Any],
    videos_dir: Path,
) -> tuple[int | None, str]:
    """Create the video row (``status='uploading'``) and start the Drive upload.

    The local file is already on disk under ``video_name``; the row is written
    immediately so the owner can stream it and watch progress while the Drive
    upload runs in the background. Returns ``(video_id, "")`` on success or
    ``(None, reason)`` if the row could not be created.
    """
    base_dir = str(os.path.dirname(str(videos_dir)))
    drive_filename = drive.make_drive_filename(title, size_bytes)
    logger.info("register: title=%r local=%s size=%d", title, video_name, size_bytes)
    try:
        video_id = db.create_video(
            title=title,
            owner_id=owner_id,
            folder_id=folder_id,
            local_filename=video_name,
            cover_filename=cover_name,
            size_bytes=size_bytes,
            status="uploading",
            **meta,
        )
    except Exception as exc:  # noqa: BLE001 - surface a friendly error
        logger.error("register: could not create video row: %s", exc)
        return None, "Could not create the video record."

    local_path = os.path.join(str(videos_dir), video_name)
    drive_worker.worker.enqueue(
        video_id,
        title,
        local_path,
        drive_filename,
        size_bytes,
        current_app.config.get("DRIVE_FOLDER_ID", ""),
        base_dir,
        str(videos_dir),
        current_app.config,
    )
    # Enforce the admin cache-capacity cap now that a new local copy exists.
    try:
        storage.enforce_cache_limit(
            videos_dir, base_dir, current_app.config, drive_worker.worker.in_flight()
        )
    except Exception as exc:  # noqa: BLE001 - eviction is best effort
        logger.warning("register: cache-cap enforce failed: %s", exc)
    return video_id, ""


def _meta_from_form() -> dict[str, Any]:
    return {
        "duration": _optional_float(request.form.get("duration")),
        "resolution": request.form.get("resolution") or None,
        "codec": request.form.get("codec") or None,
        "bitrate": _optional_int(request.form.get("bitrate")),
        "fps": _optional_float(request.form.get("fps")),
        "description": request.form.get("description") or None,
    }


def _unique_video_name(title: str, videos_dir: Path) -> str:
    base = storage._safe_name(title + ".mp4")
    name = base
    counter = 0
    while (videos_dir / name).exists():
        stem, ext = os.path.splitext(base)
        name = f"{stem}_{counter}{ext}"
        counter += 1
    return name


def _rmtree_quiet(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _optional_int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _optional_float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _file_size(file_storage: Any) -> int:
    """Return the size of an uploaded file without consuming the stream."""
    if getattr(file_storage, "content_length", 0):
        return int(file_storage.content_length)
    try:
        file_storage.stream.seek(0, os.SEEK_END)
        size = file_storage.stream.tell()
        file_storage.stream.seek(0)
        return size
    except Exception:  # noqa: BLE001 - fall back to 0 if unreadable
        return 0


def _remove_quiet(path: str) -> None:
    """Best-effort delete of a leftover file.

    On Windows the file may still be locked (e.g. by the pydrive2 handle left
    open during a failed upload, or by AV), so a failure here must NOT mask the
    original error that triggered the cleanup. The file is collected by LRU
    eviction later, once the lock is released.
    """
    if not path or not os.path.exists(path):
        return
    try:
        os.remove(path)
    except OSError as exc:  # noqa: BLE001 - cleanup is best-effort
        print(f"warn: could not remove {path!r}: {exc}", file=sys.stderr)
