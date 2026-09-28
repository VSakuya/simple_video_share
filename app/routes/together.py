"""Watch-together routes (§17).

Room list, room view (synchronized player + ephemeral chat), SSE presence
stream, chat, and play-state control. All routes require login.
"""

import os
from typing import Any

from flask import (
    Blueprint,
    Response,
    current_app,
    redirect,
    render_template,
    request,
    stream_with_context,
    url_for,
)

from .. import db, presence
from ..auth import current_user, login_required

together_bp = Blueprint("together", __name__, url_prefix="/together")


def _me_info(user: dict[str, Any]) -> dict[str, Any]:
    """The presence/chat payload for a user: id, name, avatar URL (§16.8)."""
    avatar = user.get("avatar_filename")
    return {
        "id": user["id"],
        "username": user["username"],
        "avatar_url": (
            url_for("home.avatar_file", filename=avatar) if avatar else ""
        ),
    }


def _is_cached(video: dict[str, Any]) -> bool:
    """True when a locally playable copy of ``video`` exists on disk."""
    local = video.get("local_filename")
    if not local:
        return False
    videos_dir = current_app.config.get("VIDEOS_DIR", "")
    return os.path.exists(os.path.join(videos_dir, local))


@together_bp.route("", methods=["GET"])
@together_bp.route("/", methods=["GET"])
@login_required
def index() -> Any:
    """List of active watch rooms (non-empty rooms only)."""
    rooms = presence.active_watch_rooms()
    # Enrich each room with its cover filename for the template.
    enriched = []
    for r in rooms:
        video = db.get_video_by_id(r["video_id"])
        enriched.append({
            "video_id": r["video_id"],
            "title": r["title"],
            "online": r["online"],
            "cover_filename": video.get("cover_filename") if video else None,
        })
    return render_template("together_list.html", rooms=enriched)


@together_bp.route("/<int:video_id>", methods=["GET"])
@login_required
def room(video_id: int) -> Any:
    """Watch-together room page for a cached video."""
    video = db.get_video_by_id(video_id)
    if video is None:
        from flask import abort
        abort(404)

    if not _is_cached(video):
        # Watch Together is only available for locally playable videos.
        return redirect(url_for("watch.page", video_id=video_id))

    key = presence.watch_room_key(video_id)
    settle_seconds = float(current_app.config.get("SEEK_SETTLE_SECONDS", 60.0))
    presence.init_watch_state(key, video_id, video["title"], settle_seconds)
    presence.start_watch_scheduler()

    cover_filename = video.get("cover_filename")
    cover_url = (
        url_for("home.cover", filename=cover_filename) if cover_filename else None
    )

    return render_template(
        "together_view.html",
        video=video,
        video_id=video_id,
        is_cached=True,
        cover_url=cover_url,
    )


@together_bp.route("/<int:video_id>/presence", methods=["GET"])
@login_required
def presence_stream(video_id: int) -> Any:
    """SSE stream for the watch room."""
    user = current_user()
    assert user is not None  # guaranteed by @login_required
    key = presence.watch_room_key(video_id)
    me = _me_info(user)

    def generate():
        yield from presence.stream(key, me)

    resp = Response(
        stream_with_context(generate()), mimetype="text/event-stream"
    )
    resp.headers["Cache-Control"] = "no-cache, no-transform"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@together_bp.route("/<int:video_id>/chat", methods=["POST"])
@login_required
def chat(video_id: int) -> Any:
    """Send an ephemeral chat message to the watch room."""
    user = current_user()
    assert user is not None  # guaranteed by @login_required

    data = request.get_json(silent=True) or {}
    text = (data.get("message") or "").strip()
    if not text:
        return {"ok": False, "error": "empty message"}, 400
    if len(text) > 500:
        return {"ok": False, "error": "message too long (max 500 chars)"}, 400

    key = presence.watch_room_key(video_id)
    presence.broadcast_message(key, _me_info(user), text)
    return {"ok": True}


@together_bp.route("/<int:video_id>/play", methods=["POST"])
@login_required
def play(video_id: int) -> Any:
    """Handle a play/pause/seek action for the watch room."""
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    t = data.get("t")

    if action not in ("play", "pause", "seek"):
        return {"ok": False, "error": f"unknown action: {action!r}"}, 400
    if action == "seek" and (t is None or not isinstance(t, (int, float))):
        return {"ok": False, "error": "seek requires a numeric 't'"}, 400

    user = current_user()
    key = presence.watch_room_key(video_id)
    state = presence.set_watch_state(key, action, t, actor=user["username"])
    if state is None:
        return {"ok": False, "error": "room not found"}, 404
    return {"ok": True, "state": state}


@together_bp.route("/<int:video_id>/position", methods=["POST"])
@login_required
def position(video_id: int) -> Any:
    """Record this viewer's playback position for slowest-viewer pacing (§17.4).

    The client POSTs its ``video.currentTime`` (seconds) every 0.5 s; the
    server derives the shared pace from the minimum fresh position.
    """
    user = current_user()
    assert user is not None  # guaranteed by @login_required

    data = request.get_json(silent=True) or {}
    t = data.get("t")
    if t is None or not isinstance(t, (int, float)):
        return {"ok": False, "error": "'t' (seconds) is required"}, 400

    key = presence.watch_room_key(video_id)
    presence.report_position(key, user["id"], user["username"], float(t))
    return {"ok": True}