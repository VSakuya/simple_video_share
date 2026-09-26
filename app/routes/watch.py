"""Watch blueprint: Range streaming + Drive download (P3), and comments (§13.16, §13.20)."""

import hashlib
import logging
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, Set
from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for
from .. import db
from ..auth import current_user, login_required

watch_bp = Blueprint("watch", __name__, url_prefix="/watch")

logger = logging.getLogger("simple_video_share.routes.watch")

#: Max comment length in characters (plain text, kaomoji allowed).
MAX_COMMENT_LENGTH = 2000

# In-process progress store for Drive -> local cache downloads (§13.33). The
# watch page polls ``/watch/<id>/cache/status`` while a background thread pulls
# the file from Drive. ``video_id -> {"transferred", "total", "error", "done"}``.
_cache_progress: Dict[int, Dict[str, Any]] = {}
_cache_lock = threading.Lock()
_cache_in_flight: Set[int] = set()

#: Kaomoji offered by the quick-insert bar on the watch page (§13.16).
KAOMOJI_SET = [
    "(≧▽≦)",
    "(≧◡≦)",
    "(≖‿≖)✧",
    "(´｡• ᵕ •｡`) ♡",
    "(•ω•)",
    "(≧ω≦)",
    "(・ω・ )",
    "(¬‿¬)",
    "(=^・ω・^=)",
    "( ˘ω˘ )",
    "o(≧v≦)o",
    "(ノ°▽°)ノ",
    "^_^",
    ">:3",
    "(T_T)",
    "(；＿；)",
]


@watch_bp.route("/<int:video_id>")
@login_required
def page(video_id: int) -> str:
    video = db.get_video_by_id(video_id)
    if video is None:
        from flask import abort
        abort(404)
    comments = db.list_comments(video_id)
    for c in comments:
        c["is_reply"] = False
        for r in c["replies"]:
            r["is_reply"] = True
    return render_template(
        "watch.html",
        video=video,
        comments=comments,
        kaomoji_set=KAOMOJI_SET,
        max_comment_length=MAX_COMMENT_LENGTH,
        is_cached=_is_cached(video),
        has_drive=bool(video.get("google_drive_file_id")),
    )


def _is_cached(video: dict[str, Any]) -> bool:
    """True when a locally playable copy of ``video`` exists on disk."""
    local = video.get("local_filename")
    if not local:
        return False
    return os.path.exists(os.path.join(app_videos_dir(), local))


def _cached_filename(title: str, videos_dir: str) -> str:
    """Pick a unique, hash-based on-disk filename for a freshly downloaded copy.

    Same style as the upload/Drive naming: a short SHA-256 digest so same-title
    videos never collide on disk. The uuid keeps it unique per download.
    """
    seed = f"{title}|{uuid.uuid4().hex}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return f"{digest[:16]}.mp4"


@watch_bp.route("/<int:video_id>/cache", methods=["POST"])
@login_required
def start_cache(video_id: int) -> Any:
    """Begin a background Drive -> local download for a non-cached video (§13.33).

    Returns JSON. If the video is already cached (or a download is already
    running) the caller can simply start polling ``cache_status``.
    """
    video = db.get_video_by_id(video_id)
    if video is None:
        return jsonify(ok=False, error="Video not found."), 404
    drive_id = video.get("google_drive_file_id")
    if not drive_id:
        return (
            jsonify(ok=False, error="This video has no Drive copy to download."),
            400,
        )

    videos_dir = app_videos_dir()
    local = video.get("local_filename")
    if local and os.path.exists(os.path.join(videos_dir, local)):
        # Already playable.
        return jsonify(ok=True, cached=True, transferred=0, total=0)

    with _cache_lock:
        if video_id in _cache_in_flight:
            # Attach to the in-flight download; the page keeps polling.
            p = _cache_progress.get(video_id, {})
            return jsonify(
                ok=True,
                cached=False,
                transferred=p.get("transferred", 0),
                total=p.get("total", 0),
                error=p.get("error"),
            )
        filename = _cached_filename(video.get("title") or "video", videos_dir)
        _cache_in_flight.add(video_id)
        _cache_progress[video_id] = {
            "transferred": 0,
            "total": 0,
            "error": None,
            "done": False,
        }

    base_dir = os.path.dirname(videos_dir)
    app_config = current_app.config
    thread = threading.Thread(
        target=_run_cache,
        args=(video_id, drive_id, videos_dir, filename, base_dir, app_config),
        name=f"drive-cache-{video_id}",
        daemon=True,
    )
    thread.start()
    return jsonify(ok=True, cached=False, transferred=0, total=0)


@watch_bp.route("/<int:video_id>/cache/status")
@login_required
def cache_status(video_id: int) -> Any:
    """Poll the progress of a cache download for ``video_id`` (§13.33)."""
    video = db.get_video_by_id(video_id)
    if video is None:
        return jsonify(cached=False, active=False, error="Video not found.")
    if _is_cached(video):
        return jsonify(cached=True, active=False, transferred=0, total=0, error=None)
    with _cache_lock:
        p = dict(_cache_progress.get(video_id, {}))
        active = video_id in _cache_in_flight
    total = p.get("total") or 0
    transferred = p.get("transferred") or 0
    return jsonify(
        cached=False,
        active=active,
        transferred=transferred,
        total=total,
        progress=(transferred / total) if total else 0.0,
        error=p.get("error"),
    )


def _run_cache(
    video_id: int,
    drive_id: str,
    videos_dir: str,
    filename: str,
    base_dir: str,
    app_config: Any,
) -> None:
    """Background worker: pull ``drive_id`` from Drive into ``videos_dir`` (§13.33)."""
    from .. import drive, storage

    dest = os.path.join(videos_dir, filename)

    def _report(transferred: int, total: int) -> None:
        with _cache_lock:
            p = _cache_progress.get(video_id)
            if p is not None:
                p["transferred"] = transferred
                p["total"] = total

    try:
        drive.download_file(drive_id, dest, progress_cb=_report)
        db.update_video(video_id, local_filename=filename)
        db.touch_video(video_id)
        with _cache_lock:
            p = _cache_progress.get(video_id)
            if p is not None:
                p["done"] = True
        logger.info("cache: video id=%s now cached as %s", video_id, filename)
        # Enforce the admin cache-capacity cap now that a new local copy exists
        # (protect the video we just downloaded from eviction).
        try:
            storage.enforce_cache_limit(
                Path(videos_dir), base_dir, app_config, {video_id}
            )
        except Exception as exc:  # noqa: BLE001 - eviction is best effort
            logger.warning("cache: cap enforce failed for id=%s: %s", video_id, exc)
    except Exception as exc:  # noqa: BLE001 - surface a friendly failure
        logger.error("cache: Drive download failed for video id=%s: %s", video_id, exc)
        try:
            if os.path.exists(dest):
                os.remove(dest)
        except OSError:  # noqa: BLE001 - cleanup is best effort
            pass
        with _cache_lock:
            p = _cache_progress.get(video_id)
            if p is not None:
                p["error"] = str(exc)
    finally:
        with _cache_lock:
            _cache_in_flight.discard(video_id)


@watch_bp.route("/<int:video_id>/comments", methods=["POST"])
@login_required
def add_comment(video_id: int) -> Any:
    """Create a comment on the video (plain text; kaomoji are just characters).

    A normal form POST gets a redirect; an AJAX request (``X-Requested-With``)
    gets JSON plus the rendered comment so the UI updates without a reload
    (§13.26).
    """
    from flask import abort
    video = db.get_video_by_id(video_id)
    if video is None:
        abort(404)
    me = current_user()
    assert me is not None
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    body = (request.form.get("body") or "").strip()
    if not body:
        if is_ajax:
            return jsonify(ok=False, error="Comment cannot be empty."), 400
        flash("Comment cannot be empty.", "error")
        return redirect(url_for("watch.page", video_id=video_id))
    if len(body) > MAX_COMMENT_LENGTH:
        if is_ajax:
            return (
                jsonify(
                    ok=False,
                    error=f"Comment too long (max {MAX_COMMENT_LENGTH} characters).",
                ),
                400,
            )
        flash(f"Comment too long (max {MAX_COMMENT_LENGTH} characters).", "error")
        return redirect(url_for("watch.page", video_id=video_id))
    # Optional parent_id for two-level replies (§13.20).
    parent_id: int | None = None
    parent_raw = request.form.get("parent_id")
    if parent_raw:
        try:
            parent_id = int(parent_raw)
        except (TypeError, ValueError):
            parent_id = None
        if parent_id is not None:
            # Validate: the parent must be a top-level comment on this video.
            parent = db.get_comment_by_id(parent_id)
            if (
                parent is None
                or parent["video_id"] != video_id
                or parent.get("parent_id") is not None
            ):
                if is_ajax:
                    return jsonify(ok=False, error="Invalid reply target."), 400
                flash("Invalid reply target.", "error")
                return redirect(url_for("watch.page", video_id=video_id))
    comment_id = db.add_comment(video_id, me["id"], body, parent_id=parent_id)
    if not is_ajax:
        return redirect(url_for("watch.page", video_id=video_id))
    # Render just the new comment for the client to insert into the DOM.
    fetched = db.get_comment_by_id(comment_id)
    assert fetched is not None
    comment: dict[str, Any] = dict(fetched)
    comment["username"] = me["username"]
    comment["avatar_filename"] = me.get("avatar_filename")
    comment["is_reply"] = parent_id is not None
    comment["replies"] = []
    html = render_template(
        "_comment.html",
        c=comment,
        video_id=video_id,
        max_comment_length=MAX_COMMENT_LENGTH,
    )
    return jsonify(ok=True, html=html, id=comment_id, parent_id=parent_id)


@watch_bp.route("/<int:video_id>/comments/<int:comment_id>/delete", methods=["POST"])
@login_required
def delete_comment(video_id: int, comment_id: int) -> Any:
    """Delete a comment: the author or an admin only (AJAX-aware, §13.26)."""
    me = current_user()
    assert me is not None
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    comment = db.get_comment_by_id(comment_id)
    if comment is None or comment["video_id"] != video_id:
        if is_ajax:
            return jsonify(ok=False, error="Comment not found."), 400
        flash("Comment not found.", "error")
        return redirect(url_for("watch.page", video_id=video_id))
    if comment["author_id"] != me["id"] and not me.get("is_admin"):
        if is_ajax:
            return (
                jsonify(ok=False, error="You can only delete your own comments."),
                400,
            )
        flash("You can only delete your own comments.", "error")
        return redirect(url_for("watch.page", video_id=video_id))
    db.delete_comment(comment_id)
    if is_ajax:
        return jsonify(ok=True)
    flash("Comment deleted.", "success")
    return redirect(url_for("watch.page", video_id=video_id))


@watch_bp.route("/<int:video_id>/stream")
@login_required
def stream(video_id: int) -> Any:
    """Serve the cached video with HTTP Range support (P3)."""
    from flask import send_file, abort
    video = db.get_video_by_id(video_id)
    if video is None:
        abort(404)
    if not video.get("local_filename"):
        # Not cached locally; P3 will download from Drive first.
        abort(503)
    import os
    path = os.path.join(app_videos_dir(), video["local_filename"])
    if not os.path.exists(path):
        abort(503)
    db.touch_video(video_id)
    return send_file(path, conditional=True)


@watch_bp.route("/<int:video_id>/download")
@login_required
def download(video_id: int) -> Any:
    """Serve a cached video as a download (§16.5).

    Mirrors ``stream``'s checks (video exists, cached locally) but serves the
    file with ``as_attachment=True`` and a Range-free response so the browser
    saves ``<title>.<ext>`` instead of streaming it.
    """
    from flask import send_file, abort
    video = db.get_video_by_id(video_id)
    if video is None:
        abort(404)
    local = video.get("local_filename")
    if not local:
        abort(503)
    path = os.path.join(app_videos_dir(), local)
    if not os.path.exists(path):
        abort(503)
    db.touch_video(video_id)
    ext = local.rsplit(".", 1)[-1] if "." in local else ""
    download_name = f"{video['title']}.{ext}" if ext else str(video["title"])
    return send_file(path, as_attachment=True, download_name=download_name)


def app_videos_dir() -> str:
    from flask import current_app
    return str(current_app.config["VIDEOS_DIR"])
