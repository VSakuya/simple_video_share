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

import hashlib
import json
import logging
import os
import re
import shutil
import sys
import time
import uuid
from functools import wraps
from ..filelock import file_lock, LockBusy, replace_file
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

from .. import db, drive, mp4, storage
from .. import drive_worker
from ..auth import login_required
from ..log import log_client

logger = logging.getLogger("simple_video_share.routes.upload")

upload_bp = Blueprint("upload", __name__, url_prefix="/upload")

#: Max tags a single video may carry (§bug L67); enforced in the handler.
MAX_TAGS_PER_VIDEO = 20


@upload_bp.route("/")
@login_required
def index() -> Any:
    from ..auth import current_user
    me = current_user()
    folders = db.folders_with_depth(me["id"]) if me else []
    # The admin "default bitrate" is the client-side transcode bitrate cap.
    # Expose it to the page so upload.js stops using a hard-coded 5000.
    settings = db.get_all_settings()
    try:
        max_bitrate_kbps = int(settings.get("default_bitrate", "5000"))
    except (TypeError, ValueError):
        max_bitrate_kbps = 5000
    return render_template(
        "upload.html",
        folders=folders,
        max_bitrate_kbps=max_bitrate_kbps,
        tags=db.list_tags(),
    )


@upload_bp.route("/edit")
@login_required
def edit() -> Any:
    """Clip/edit sub-page: a single video with preview + trim, then transcode.

    The main /upload page is the multi-file queue (auto-transcode, no clip).
    This page keeps the single-file clip flow and reuses the same shared
    pipeline (pipeline.js via edit.js).
    """
    from ..auth import current_user
    me = current_user()
    folders = db.folders_with_depth(me["id"]) if me else []
    settings = db.get_all_settings()
    try:
        max_bitrate_kbps = int(settings.get("default_bitrate", "5000"))
    except (TypeError, ValueError):
        max_bitrate_kbps = 5000
    return render_template(
        "edit.html", folders=folders, max_bitrate_kbps=max_bitrate_kbps,
        tags=db.list_tags(),
    )


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

    if not _folder_ok(me["id"], bool(me.get("is_admin")), folder_id):
        return _fail("Unknown folder.")

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
        _remove_upload_quiet(os.path.join(str(videos_dir), video_name), videos_dir)
        if cover_name:
            _remove_quiet(os.path.join(str(covers_dir), cover_name))
        return _fail(err or "Could not register the video.", status=500)

    if is_ajax:
        return jsonify({"ok": True, "title": title, "video_id": video_id})
    flash(f"Uploaded '{title}' — now uploading to Google Drive.", "success")
    # My Videos (not home) shows the live Drive upload progress + retry (§13.18).
    return redirect(url_for("user.index"))


# ---------------------------------------------------------------------------
# Segmented upload (resumable chunked path)
# ---------------------------------------------------------------------------

# Upload metadata and completion receipts are persisted under
# videos/.segments/<token>/session.json so interrupted uploads can resume.
SEGMENT_SIZE = 64 * 1024 * 1024  # 64 MB per part to stay well under reverse-proxy limits


def _segment_guard(fn):
    @wraps(fn)
    def guarded(*args, **kwargs):
        base = Path(current_app.config["VIDEOS_DIR"])
        token = request.form.get("token") or request.form.get("resume_token") or "start"
        if token != "start" and not _valid_seg_token(token):
            return jsonify(ok=False, error="Invalid session token"), 400
        try:
            # Serialize space checks and writes across workers, as well as each
            # session's finish/part operations. Lock files are never unlinked.
            with file_lock(base / ".segment-locks" / "intake.lock"):
                with file_lock(base / ".segment-locks" / (token + ".lock")):
                    return fn(*args, **kwargs)
        except LockBusy:
            return jsonify(ok=False, error="Upload busy; retry shortly"), 409
    return guarded


def cleanup_segments(videos_dir: Path) -> None:
    """Expire abandoned uploads and completed receipts after seven idle days."""
    root = videos_dir / ".segments"
    for meta in root.glob("*/session.json"):
        try:
            if time.time() - meta.stat().st_mtime < 7 * 86400:
                continue
            with file_lock(videos_dir / ".segment-locks" / (meta.parent.name + ".lock")):
                _remove_seg_session(videos_dir, meta.parent.name)
        except (OSError, LockBusy):
            continue


@upload_bp.route("/seg/start", methods=["POST"])
@login_required
@_segment_guard
def seg_start() -> Any:
    from ..auth import current_user
    me = current_user()
    assert me is not None

    title = (request.form.get("title") or "").strip() or "Untitled"
    total_parts = _optional_int(request.form.get("total_parts")) or 0
    expected_size = _optional_int(request.form.get("expected_size")) or 0
    folder_id = _optional_int(request.form.get("folder_id"))
    resume_token = (request.form.get("resume_token") or "").strip()
    if not _folder_ok(me["id"], bool(me.get("is_admin")), folder_id):
        return jsonify(ok=False, error="Unknown folder."), 400
    fingerprint = request.form.get("fingerprint", "")
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        return jsonify(ok=False, error="File fingerprint required"), 400
    if expected_size <= 0 or total_parts != (expected_size + SEGMENT_SIZE - 1) // SEGMENT_SIZE or total_parts > 99999:
        return jsonify(ok=False, error="Invalid file size or part count"), 400
    videos_dir = Path(current_app.config["VIDEOS_DIR"])
    cleanup_segments(videos_dir)
    base_dir = str(os.path.dirname(str(videos_dir)))
    ok, msg = _check_space(base_dir, 0)
    if not ok:
        return jsonify(ok=False, error=msg), 400

    if _valid_seg_token(resume_token):
        resumed = _get_seg_session(videos_dir, resume_token)
        if resumed is not None:
            if resumed["user_id"] != me["id"]:
                return jsonify(ok=False, error="Session belongs to another user."), 403
            if resumed.get("fingerprint") == fingerprint and _seg_session_matches(
                resumed, title, total_parts, expected_size, folder_id
            ):
                _save_seg_session(videos_dir, resume_token, resumed)
                uploaded_parts = _uploaded_parts(
                    _seg_dir(videos_dir, resume_token), resumed["total_parts"]
                )
                return jsonify(
                    ok=True,
                    token=resume_token,
                    segment_size=SEGMENT_SIZE,
                    resumed=True,
                    uploaded_parts=uploaded_parts,
                )

    token = uuid.uuid4().hex
    sess = {
        "user_id": me["id"],
        "title": title,
        "total_parts": total_parts,
        "expected_size": expected_size,
        "folder_id": folder_id,
        "created_at": int(time.time()),
        "fingerprint": fingerprint,
    }
    _save_seg_session(videos_dir, token, sess)
    return jsonify(
        ok=True, token=token, segment_size=SEGMENT_SIZE, resumed=False,
        uploaded_parts=[],
    )


@upload_bp.route("/seg/part", methods=["POST"])
@login_required
@_segment_guard
def seg_part() -> Any:
    from ..auth import current_user
    me = current_user()
    assert me is not None
    token = request.form.get("token", "")
    part_index = _optional_int(request.form.get("part_index"))
    videos_dir = Path(current_app.config["VIDEOS_DIR"])
    sess = _get_seg_session(videos_dir, token)
    if sess is None or part_index is None or part_index < 0:
        return jsonify(ok=False, error="Unknown session"), 400
    if sess["user_id"] != me["id"]:
        return jsonify(ok=False, error="Session belongs to another user."), 403
    if part_index >= sess["total_parts"]:
        return jsonify(ok=False, error="part_index out of range"), 400
    part_file = request.files.get("part")
    if part_file is None:
        return jsonify(ok=False, error="No part received"), 400
    seg_dir = _seg_dir(videos_dir, token)
    seg_dir.mkdir(parents=True, exist_ok=True)
    dest = seg_dir / ("part_%05d" % part_index)
    if sess.get("video_id"):
        return jsonify(ok=True, part_index=part_index)
    expected = min(SEGMENT_SIZE, sess["expected_size"] - part_index * SEGMENT_SIZE)
    if _file_size(part_file) != expected:
        return jsonify(ok=False, error="Incorrect part size"), 400
    ok, msg = _check_space(str(videos_dir.parent), expected)
    if not ok:
        return jsonify(ok=False, error=msg), 400
    temp = dest.with_suffix(".tmp")
    digest = hashlib.sha256()
    try:
        with temp.open("wb") as fh:
            for block in iter(lambda: part_file.stream.read(1024 * 1024), b""):
                digest.update(block)
                fh.write(block)
            fh.flush()
            os.fsync(fh.fileno())
        if temp.stat().st_size != expected or digest.hexdigest() != request.form.get("sha256"):
            return jsonify(ok=False, error="Part checksum mismatch"), 400
        os.replace(temp, dest)
        sess.setdefault("checksums", {})[str(part_index)] = digest.hexdigest()
        _save_seg_session(videos_dir, token, sess)
    finally:
        temp.unlink(missing_ok=True)
    return jsonify(ok=True, part_index=part_index)


@upload_bp.route("/seg/finish", methods=["POST"])
@login_required
@_segment_guard
def seg_finish() -> Any:
    from ..auth import current_user
    me = current_user()
    assert me is not None
    token = request.form.get("token", "")
    videos_dir = Path(current_app.config["VIDEOS_DIR"])
    sess = _get_seg_session(videos_dir, token)
    if sess is None:
        return jsonify(ok=False, error="Unknown session"), 400
    if sess["user_id"] != me["id"]:
        return jsonify(ok=False, error="Session belongs to another user."), 403
    if sess.get("video_id"):
        return jsonify(ok=True, title=sess["title"], video_id=sess["video_id"])
    seg_dir = _seg_dir(videos_dir, token)
    parts = sorted(seg_dir.glob("part_[0-9][0-9][0-9][0-9][0-9]")) if seg_dir.exists() else []
    if len(parts) != sess["total_parts"]:
        return (
            jsonify(
                ok=False,
                error="Incomplete upload: %d/%d parts" % (len(parts), sess["total_parts"]),
                uploaded_parts=_uploaded_parts(seg_dir, sess["total_parts"]),
            )
        ), 400

    if _uploaded_parts(seg_dir, sess["total_parts"]) != list(range(sess["total_parts"])):
        return jsonify(ok=False, error="Invalid or incomplete parts"), 400
    fingerprint = hashlib.sha256("".join(sess["checksums"][str(i)] for i in range(sess["total_parts"])).encode()).hexdigest()
    if fingerprint != sess.get("fingerprint"):
        return jsonify(ok=False, error="File fingerprint mismatch"), 400
    ok, msg = _check_space(str(videos_dir.parent), sess["expected_size"])
    if not ok:
        return jsonify(ok=False, error=msg), 400
    cover_file = request.files.get("cover")
    covers_dir = current_app.config["COVERS_DIR"]
    cover_name: str | None = None
    if cover_file is not None:
        cover_name = storage.save_cover(cover_file, covers_dir, current_app.config)

    video_name = sess.setdefault("video_name", f"{token}/{token}.mp4")
    _save_seg_session(videos_dir, token, sess)
    # Recover a committed row if the process died before persisting its receipt.
    existing = next((v for v in db.list_all_videos() if v.get("local_filename") == video_name), None)
    if existing:
        sess["video_id"] = existing["id"]
        _save_seg_session(videos_dir, token, sess)
        return jsonify(ok=True, title=sess["title"], video_id=existing["id"])
    final_path = videos_dir / video_name
    final_path.parent.mkdir(parents=True, exist_ok=True)  # §31.3 foldered name
    try:
        with final_path.open("wb") as out:
            for p in parts:
                with p.open("rb") as fh:
                    shutil.copyfileobj(fh, out)
        size_bytes = final_path.stat().st_size
        if size_bytes != sess["expected_size"]:
            raise OSError("Merged size mismatch")
    except OSError:
        _remove_upload_quiet(str(final_path), videos_dir)
        if cover_name:
            _remove_quiet(os.path.join(str(covers_dir), cover_name))
        return jsonify(ok=False, error="Could not merge video; retry upload completion."), 503

    video_id, err = _register_and_enqueue(
        sess["user_id"], sess["title"], video_name, cover_name, size_bytes,
        sess["folder_id"], _meta_from_form(), videos_dir,
    )
    if video_id is None:
        _remove_upload_quiet(str(final_path), videos_dir)
        if cover_name:
            _remove_quiet(os.path.join(str(covers_dir), cover_name))
        return jsonify(ok=False, error=err or "Could not register the video."), 400

    sess["video_id"] = video_id
    _save_seg_session(videos_dir, token, sess)
    for part in parts:
        part.unlink(missing_ok=True)
    return jsonify(ok=True, title=sess["title"], video_id=video_id)


@upload_bp.route("/seg/cancel", methods=["POST"])
@login_required
@_segment_guard
def seg_cancel() -> Any:
    from ..auth import current_user
    me = current_user()
    base = Path(current_app.config["VIDEOS_DIR"])
    token = request.form.get("token", "")
    sess = _get_seg_session(base, token)
    if sess and (not me or sess["user_id"] != me["id"]):
        return jsonify(ok=False, error="Session belongs to another user."), 403
    if sess and not sess.get("video_id"):
        _remove_seg_session(base, token)
    return jsonify(ok=True)


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


def _tag_ids_from_form() -> list[int]:
    """Valid tag ids from the ``tags`` checkboxes, capped at MAX_TAGS_PER_VIDEO.

    The tag library is admin-managed; a stale/unknown id (a tag deleted between
    page load and submit) is dropped silently rather than erroring the upload.
    """
    raw = request.form.getlist("tags")
    if not raw:
        return []
    valid = {t["id"] for t in db.list_tags()}
    ids: list[int] = []
    for value in raw:
        try:
            tid = int(value)
        except (TypeError, ValueError):
            continue
        if tid in valid and tid not in ids:
            ids.append(tid)
        if len(ids) >= MAX_TAGS_PER_VIDEO:
            break
    return ids


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

    token = request.form.get("token", "")
    if _valid_seg_token(token):
        sess = _get_seg_session(Path(videos_dir), token)
        if sess:
            sess["video_id"] = video_id
            _save_seg_session(Path(videos_dir), token, sess)

    # §bug L67: associate the tags chosen on the upload form. Both the single
    # and segmented flows POST them, so this one spot covers both.
    tag_ids = _tag_ids_from_form()
    if tag_ids:
        db.set_video_tags(video_id, tag_ids)

    local_path = os.path.join(str(videos_dir), video_name)
    # Move the moov atom to the front (faststart) so Range streaming can start
    # playback immediately. The mediabunny path is already faststart; this
    # covers the ffmpeg.wasm fallback and direct-pass uploads. No-op on error.
    mp4.ensure_faststart(local_path)
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
    # Hash the on-disk name (same style as the Drive copy) so two videos with the
    # same title never collide on disk. The per-upload uuid keeps the name unique
    # even for identical title pairs; the human title stays in the DB for display.
    seed = f"{title}|{uuid.uuid4().hex}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    # §31.3: one folder per video, so the HLS segments of this file have an
    # obvious home (videos/<hash>/stream) and eviction can drop the whole entry
    # at once. Old rows keep their flat name and still work.
    return f"{digest[:16]}/{digest[:16]}.mp4"


def _rmtree_quiet(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _rmdir_if_empty(path: Path) -> None:
    """Remove a now-empty video folder (§31.3); never touches videos_dir itself."""
    try:
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    except OSError:  # noqa: BLE001 - cleanup is best-effort
        pass


def _valid_seg_token(token: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{32}", token or ""))


def _seg_dir(videos_dir: Path, token: str) -> Path:
    return videos_dir / ".segments" / token


def _seg_meta_path(videos_dir: Path, token: str) -> Path:
    return _seg_dir(videos_dir, token) / "session.json"


def _save_seg_session(videos_dir: Path, token: str, sess: dict[str, Any]) -> None:
    seg_dir = _seg_dir(videos_dir, token)
    seg_dir.mkdir(parents=True, exist_ok=True)
    meta_path = _seg_meta_path(videos_dir, token)
    temp = meta_path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as fh:
        json.dump(sess, fh)
        fh.flush()
        os.fsync(fh.fileno())
    replace_file(temp, meta_path)


def _load_seg_session(videos_dir: Path, token: str) -> dict[str, Any] | None:
    if not _valid_seg_token(token):
        return None
    meta_path = _seg_meta_path(videos_dir, token)
    if not meta_path.exists():
        return None
    try:
        with meta_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not all(k in data for k in ("user_id", "title", "total_parts", "expected_size", "folder_id")):
        return None
    return data


def _get_seg_session(videos_dir: Path, token: str) -> dict[str, Any] | None:
    if not _valid_seg_token(token):
        return None
    return _load_seg_session(videos_dir, token)


def _seg_session_matches(
    sess: dict[str, Any],
    title: str,
    total_parts: int,
    expected_size: int,
    folder_id: int | None,
) -> bool:
    try:
        sess_total_parts = int(sess.get("total_parts", -1))
        sess_expected_size = int(sess.get("expected_size", -1))
    except (TypeError, ValueError):
        return False
    return (
        str(sess.get("title", "")) == title
        and sess_total_parts == total_parts
        and sess_expected_size == expected_size
        and _optional_int(str(sess.get("folder_id"))) == folder_id
    )


def _uploaded_parts(seg_dir: Path, total_parts: int) -> list[int]:
    out: list[int] = []
    if not seg_dir.exists():
        return out
    sess = _load_seg_session(seg_dir.parent.parent, seg_dir.name)
    for part in seg_dir.glob("part_[0-9][0-9][0-9][0-9][0-9]"):
        m = re.fullmatch(r"part_(\d{5})", part.name)
        if not m:
            continue
        idx = int(m.group(1))
        if sess and 0 <= idx < total_parts and str(idx) in sess.get("checksums", {}):
            expected = min(SEGMENT_SIZE, sess["expected_size"] - idx * SEGMENT_SIZE)
            if part.is_file() and part.stat().st_size == expected:
                with part.open("rb") as fh:
                    if hashlib.file_digest(fh, "sha256").hexdigest() == sess["checksums"][str(idx)]:
                        out.append(idx)
    out.sort()
    return out


def _remove_seg_session(videos_dir: Path, token: str) -> None:
    _rmtree_quiet(_seg_dir(videos_dir, token))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _optional_int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _folder_ok(owner_id: int, is_admin: bool, folder_id: int | None) -> bool:
    """True when ``folder_id`` is Root (None) or one of the owner's folders."""
    if folder_id is None:
        return True
    folder = db.get_folder(folder_id)
    return folder is not None and (folder["owner_id"] == owner_id or is_admin)


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


def _remove_upload_quiet(video_path: str, videos_dir: Path) -> None:
    """Delete a just-written video file plus the empty folder that held it (§31.3).

    A failed registration must not leave an empty videos/<hash>/ behind. The
    parent is only pruned when it is not the videos directory itself.
    """
    _remove_quiet(video_path)
    parent = Path(video_path).parent
    if parent != Path(str(videos_dir)):
        _rmdir_if_empty(parent)


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
