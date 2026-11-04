"""Watch blueprint: Range streaming + Drive download (P3), and comments (§13.16, §13.20)."""

import hashlib
import logging
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Set
from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for
from .. import db
from ..auth import current_user, login_required
from ..routes.notify import item_from_parts, push_new

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
        is_cached=_is_playable_locally(video),
        has_drive=bool(video.get("google_drive_file_id")),
        video_tags=db.get_video_tags(video_id),
    )


def _is_playable_locally(video: dict[str, Any]) -> bool:
    """True when ``video`` can be played from this server without a Drive download.

    §31.7 blocker 1: with ``hls_keep_source: false`` the MP4 is deleted once its
    segments validated, so "cached" means the MP4 **or** a validated stream tree.
    """
    from .. import storage
    return storage.is_locally_playable(app_videos_dir(), video)


def _stream_dir(video: dict[str, Any]) -> Optional[Path]:
    """The stream directory for ``video``, or None when it is not on disk."""
    from .. import hls
    stream_dir = hls.stream_dir_for(Path(app_videos_dir()), video)
    if stream_dir is None or not stream_dir.is_dir():
        return None
    return stream_dir


def _validated_stream_dir(video: dict[str, Any]) -> Optional[Path]:
    """The ``ready`` stream of ``video`` when its files still check out (§31.5).

    Revalidating on read is what turns a tree that was half-deleted out from under
    the row into a clean 404 (and a ``stale`` row) instead of a playlist that names
    missing segments.
    """
    from .. import hls
    if str(video.get("stream_state") or "") not in (hls.STATE_READY, hls.STATE_BUILDING, hls.STATE_REPAIRING):
        return None
    stream_dir = _stream_dir(video)
    if stream_dir is None:
        return None
    ok, why = hls.validate_stream(stream_dir, video.get("duration"))
    if not ok:
        logger.warning("watch: id=%s stream invalid on read: %s", video["id"], why)
        hls.mark_stale(int(video["id"]), "invalid on read")
        return None
    return stream_dir


def _cached_filename(video: dict[str, Any], videos_dir: str) -> str:
    """Pick the on-disk path for a freshly downloaded copy (§31.3).

    Same hash style as the upload/Drive naming so same-title videos never collide.
    A download lands in ``videos/<hash>/<hash>.mp4``; when the row still knows its
    stream tree (``<hash>/stream``), the download reuses that same folder so the
    re-downloaded source and its segments stay together and no orphan folder is
    left behind.
    """
    stored = str(video.get("stream_dir") or "").replace("\\", "/")
    if stored:
        folder = Path(stored).parent
        if folder != Path(".") and re.fullmatch(r"[0-9a-fA-F_-]{4,80}", folder.name):
            return f"{folder.name}/{folder.name}.mp4"
    seed = f"{video.get('title') or 'video'}|{uuid.uuid4().hex}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return f"{digest[:16]}/{digest[:16]}.mp4"


@watch_bp.route("/<int:video_id>/cache", methods=["POST"])
@login_required
def start_cache(video_id: int) -> Any:
    """Begin a background Drive -> local download for a non-cached video (§13.33).

    Only the video owner or an admin can trigger the download (non-owners can
    still watch in-flight progress via cache_status).
    Returns JSON. If the video is already cached (or a download is already
    running) the caller can simply start polling ``cache_status``.
    """
    video = db.get_video_by_id(video_id)
    if video is None:
        return jsonify(ok=False, error="Video not found."), 404
    me = current_user()
    assert me is not None
    if video["owner_id"] != me["id"] and not me.get("is_admin"):
        return jsonify(ok=False, error="Only the uploader (or an admin) can start this download."), 403
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

    # §31.4: reserve room for the MP4 **and** its segments (the peak while a
    # stream is being built), evicting LRU videos first when needed.
    from .. import storage
    incoming = int(video.get("size_bytes") or 0)
    if incoming and not storage.ensure_space(
        os.path.dirname(videos_dir), incoming, current_app.config, protect=[video_id],
    ):
        return (
            jsonify(ok=False, error="Not enough free disk space on this server."),
            507,
        )

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
        filename = _cached_filename(video, videos_dir)
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
    if _is_playable_locally(video):
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
    from .. import drive, hls, storage

    dest = os.path.join(videos_dir, filename)
    # §31.3: cache downloads are foldered (videos/<hash>/<hash>.mp4).
    os.makedirs(os.path.dirname(dest), exist_ok=True)

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
        # §31.5: segment right here - this is the cold-cache equivalent of the
        # upload worker's post-mirror hook: the file is complete and in Drive.
        # hls_keep_source may then delete the MP4 and keep only the segments.
        try:
            row = db.get_video_by_id(video_id)
            if row is not None:
                hls.ensure_stream(
                    Path(videos_dir), row, app_config, reason="post-download",
                )
        except Exception as exc:  # noqa: BLE001 - the MP4 still plays without HLS
            logger.warning("cache: HLS build failed for id=%s: %s", video_id, exc)
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
    # Notification triggers (§21): best-effort — a failure here never breaks
    # the comment post (the comment is already persisted and is returned
    # normally). A user never notifies themselves.
    try:
        if parent_id is None:
            # Top-level comment: notify the video's owner (skip if the author
            # is the owner — commenting on one's own video).
            owner_id = video["owner_id"]
            if owner_id != me["id"]:
                n_id = db.add_notification(
                    owner_id, "video_comment", me, video, comment_id=comment_id,
                )
                push_new(
                    owner_id,
                    item_from_parts(n_id, "video_comment", me, video, comment_id),
                )
        else:
            # Reply: notify the parent comment's author (skip when the replier
            # replies to their own comment). The parent was validated above;
            # re-read it so a concurrent delete simply skips the notification.
            parent = db.get_comment_by_id(parent_id)
            if parent is not None and parent["author_id"] != me["id"]:
                n_id = db.add_notification(
                    parent["author_id"], "comment_reply", me, video,
                    comment_id=comment_id,
                )
                push_new(
                    parent["author_id"],
                    item_from_parts(n_id, "comment_reply", me, video, comment_id),
                )
    except Exception as exc:  # noqa: BLE001 - best-effort contract (§21.5)
        logger.warning("watch: comment notification failed: %s", exc)
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
    """Serve the cached MP4 with HTTP Range support (P3).

    This is the progressive path. A video whose MP4 was replaced by its segments
    (§31.1) is played through ``/hls/playlist.m3u8`` instead - the player helper
    (§31.10) makes that choice, so this route answering 503 for a segments-only
    video is the expected shape, not a failure.
    """
    from flask import send_file, abort
    video = db.get_video_by_id(video_id)
    if video is None:
        abort(404)
    local = video.get("local_filename")
    if not local:
        abort(503)
    path = os.path.join(app_videos_dir(), str(local))
    if not os.path.exists(path):
        abort(503)
    db.touch_video(video_id)
    return send_file(path, conditional=True)


@watch_bp.route("/<int:video_id>/hls/playlist.m3u8")
@login_required
def hls_playlist(video_id: int) -> Any:
    """Serve the media playlist of a validated stream (§31.6).

    Requests never build a stream: segmentation lives in the Drive worker, the
    cache worker, or an explicit admin action, so no viewer ever waits for ffmpeg.
    ``no-cache`` because the tree can be replaced (repair, re-cut) while the URL
    stays the same; send_file still supplies ETag/Last-Modified for cheap revalidation.
    """
    from flask import abort, send_file
    from .. import hls
    video = db.get_video_by_id(video_id)
    if video is None:
        abort(404)
    stream_dir = _validated_stream_dir(video)
    if stream_dir is None:
        abort(404)
    db.touch_video(video_id)
    from flask import Response
    import re
    text = (stream_dir / hls.PLAYLIST_NAME).read_text(encoding="utf-8")
    version = stream_dir.name
    text = re.sub(r'URI="([^"]+)"', lambda m: 'URI="' + m.group(1) + '?v=' + version + '"', text)
    text = "\n".join(line + "?v=" + version if line and not line.startswith("#") else line for line in text.splitlines()) + "\n"
    response = Response(text, mimetype="application/vnd.apple.mpegurl")
    response.headers["Cache-Control"] = "private, no-cache"
    return response


@watch_bp.route("/<int:video_id>/hls/<asset>")
@login_required
def hls_asset(video_id: int, asset: str) -> Any:
    """Serve the init segment or exactly one segment named by the playlist (§31.6).

    The name is matched against the strict segment pattern and against the playlist
    itself, so this route can only ever read a file that the validated stream lists -
    never a path of the caller's choosing. Segments are immutable once built, hence
    the long private max-age. Range requests work (send_file conditional=True).
    """
    from flask import abort, send_file
    from .. import hls
    video = db.get_video_by_id(video_id)
    if video is None:
        abort(404)
    stream_dir = _stream_dir(video)
    if stream_dir is None:
        abort(404)
    version = request.args.get("v", "")
    if version:
        import re
        root = stream_dir.name.split(".gen-")[0]
        if not re.fullmatch(re.escape(root) + r"(?:\.gen-[0-9a-f]{32})?", version):
            abort(404)
        stream_dir = stream_dir.parent / version
    allowed = hls.playlist_segment_names(stream_dir)
    init_name = hls.playlist_init_name(stream_dir)
    if asset != init_name and asset not in allowed:
        abort(404)
    path = stream_dir / asset
    if not path.is_file():
        abort(404)
    db.touch_video(video_id)
    response = send_file(path, mimetype="video/mp4", conditional=True)
    response.headers["Cache-Control"] = "private, max-age=3600"
    return response


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
    if local:
        path = os.path.join(app_videos_dir(), str(local))
        if os.path.exists(path):
            db.touch_video(video_id)
            ext = local.rsplit(".", 1)[-1] if "." in local else ""
            download_name = f"{video['title']}.{ext}" if ext else str(video["title"])
            return send_file(path, as_attachment=True, download_name=download_name)
    # §31.7: the MP4 is gone but the segments are complete. An fMP4 init segment
    # followed by its media segments in playlist order IS a valid fragmented MP4,
    # so the download button keeps working with no Drive round trip. Served as one
    # streamed body (no Range), which is what a save-to-disk needs.
    stream_dir = _validated_stream_dir(video)
    if stream_dir is None:
        abort(503)
    from flask import Response
    from .. import hls
    db.touch_video(video_id)
    response = Response(hls.iter_stream_bytes(stream_dir), mimetype="video/mp4")
    response.headers["Content-Length"] = str(hls.stream_bytes_length(stream_dir))
    response.headers["Content-Disposition"] = _attachment_header(
        f"{video['title']}.mp4"
    )
    return response


def _attachment_header(filename: str) -> str:
    """Build an RFC 6266 attachment header for the reassembled download.

    Werkzeug 3.1 has no ContentDisposition helper, and send_file cannot be used
    for a body assembled from several segment files, so the ASCII fallback and
    the UTF-8 filename* are assembled here. Control characters and quotes are
    stripped - a video title is attacker-visible text.
    """
    from urllib.parse import quote

    ascii_name = filename.encode("ascii", "ignore").decode("ascii")
    ascii_name = "".join(ch for ch in ascii_name if 0x20 <= ord(ch) < 0x7F)
    ascii_name = ascii_name.replace('"', "").strip() or "video.mp4"
    return (
        "attachment; filename=\"" + ascii_name + "\"; "
        "filename*=UTF-8''" + quote(filename)
    )


def app_videos_dir() -> str:
    from flask import current_app
    return str(current_app.config["VIDEOS_DIR"])
