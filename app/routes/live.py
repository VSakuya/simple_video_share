"""Live blueprint (§14.6): the live-room list and the DPlayer + flv.js view.

Two layers, mirroring the home gallery:
- ``GET /live``          -> the list of live rooms (gallery of cards).
- ``GET /live/<room_id>``-> the live player page for a single room.

The stream itself (``room.url``) is NOT served by this app: it is a full link to
the media server (Node-Media-Server) behind the reverse proxy — either an
internal ``/live/<code>.flv`` or an external URL. The pages here only render the
UI and drive the player from ``room.url``.

The ON AIR badge is probed once, server-side, when the list page renders (§14.7):
each room's stream is checked in parallel and the badge state is baked into the
HTML. There is no client-side polling.
"""

import concurrent.futures
import logging
from pathlib import Path
from typing import Any, Optional

from flask import Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template, request, url_for

from .. import db, presence, storage
from ..auth import current_user, login_required
from ..live_probe import probe_stream

live_bp = Blueprint("live", __name__, url_prefix="/live")
logger = logging.getLogger("simple_video_share.routes.live")

# Basic-auth for the stream server — temporary (§14.7), used only by the
# on-air probe below and never sent to the browser. To be replaced by a
# config entry later.
_STREAM_USER = "MOYUER"
_STREAM_PASSWORD = "456456"


def _probe(url: str, base: str) -> bool:
    """Probe one room; a root-relative url is resolved against this host."""
    if url.startswith("/"):
        url = base + url.lstrip("/")
    return probe_stream(url, _STREAM_USER, _STREAM_PASSWORD)


#: A live-room stream link must be a full URL (``http(s)://...``) or a path on
#: the same host (``/live/<code>.flv``). Anything else is rejected (§14.8).
def _valid_live_url(url: str) -> bool:
    if not url:
        return False
    if url.startswith("/"):
        return True
    return url.startswith("http://") or url.startswith("https://")


#: Max length of one chat message, in characters after trim (§16.8).
_MAX_CHAT_LEN = 500


def _me_info(user: dict[str, Any]) -> dict[str, Any]:
    """The presence/chat payload for a user: id, name, avatar URL (§16.8)."""
    avatar = user.get("avatar_filename")
    return {
        "id": user["id"],
        "username": user["username"],
        "avatar_url": (
            url_for("home.avatar_file", filename=avatar)
            if avatar
            else url_for("static", filename="img/avatar-default.svg")
        ),
    }


def _delete_cover_file(filename: str) -> None:
    """Remove a cover file from disk (best effort)."""
    path = Path(current_app.config["COVERS_DIR"]) / filename
    if path.exists():
        try:
            path.unlink()
        except OSError:
            logger.warning("live: could not delete cover file %s", filename)


def _on_air_map(rooms: list[dict[str, Any]]) -> dict[int, bool]:
    """Probe every room in parallel and return ``{room_id: is_on_air}``.

    A root-relative stream url is resolved against this host (§14.7); each probe
    reads for up to ``PROBE_TIMEOUT`` seconds, so the whole map takes ~one window.
    """
    base = request.host_url  # ends with "/"
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, min(8, len(rooms)))
    ) as pool:
        results = list(pool.map(lambda room: _probe(room["url"], base), rooms))
    return {room["id"]: on for room, on in zip(rooms, results)}


@live_bp.route("/")
@login_required
def index() -> str:
    """Layer 1: the list of live rooms (gallery).

    The page renders **instantly** with every ON AIR badge off (§14.9); the badge
    state is fetched in the background by the client from ``GET /live/onair``.
    Probing here (up to 6 s per room) used to block the whole page load.
    """
    rooms = db.list_live_rooms()
    return render_template("live_list.html", rooms=rooms, on_air={})


@live_bp.route("/onair")
@login_required
def onair() -> Any:
    """Background on-air probe (§14.9): JSON ``{room_id: is_on_air}``.

    Fetched by the list page *after* it loads, so the page renders instantly and
    the badges light up a few seconds later. The probe stays server-side (§14.7)
    because the stream server sits behind Apache Basic auth.
    """
    rooms = db.list_live_rooms()
    on_air = _on_air_map(rooms)
    return jsonify({str(room_id): on for room_id, on in on_air.items()})


@live_bp.route("/<int:room_id>")
@login_required
def view(room_id: int) -> Any:
    """Layer 2: the live player page for a single room (fetched by its id)."""
    room = db.get_live_room(room_id)
    if room is None:
        abort(404)
    return render_template("live_view.html", room=room)


@live_bp.route("/<int:room_id>/presence")
@login_required
def presence_stream(room_id: int) -> Any:
    """SSE feed for the room: join/leave presence + ephemeral chat (§16.8).

    One long-lived ``text/event-stream`` per browser tab. The client gets an
    immediate ``state`` (online count), then live ``join``/``leave``/``message``
    events. Buffering is disabled explicitly: the dev server streams fine, but
    a reverse proxy in front (Apache) would otherwise hold the events back.
    """
    room = db.get_live_room(room_id)
    if room is None:
        abort(404)
    me = current_user()
    assert me is not None
    # The generator never touches the Flask context (me_info is built here),
    # so it can be returned as-is: Werkzeug closes it on client disconnect and
    # its finally block does the presence cleanup.
    resp = Response(presence.stream(room_id, _me_info(me)), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache, no-transform"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Connection"] = "keep-alive"
    return resp


@live_bp.route("/<int:room_id>/chat", methods=["POST"])
@login_required
def chat(room_id: int) -> Any:
    """Post one ephemeral chat message to the room (§16.8).

    JSON ``{"text": "..."}``; the message is broadcast to every open presence
    connection in the room and kept in the room's in-memory ring buffer (capped
    at 50, L62) so a client that enters later can load the recent history.
    """
    room = db.get_live_room(room_id)
    if room is None:
        abort(404)
    me = current_user()
    assert me is not None
    data = request.get_json(silent=True) or {}
    text = str(data.get("text", "")).strip()
    if not text:
        return jsonify(ok=False, error="Message is empty."), 400
    if len(text) > _MAX_CHAT_LEN:
        return jsonify(ok=False, error="Message is too long."), 400
    presence.broadcast_message(room_id, _me_info(me), text)
    return jsonify(ok=True)


@live_bp.route("/room", methods=["POST"])
@login_required
def manage_room() -> Any:
    """Self-service: a user creates/updates their own live room (§14.8).

    One room per user: the first submission creates it, later ones update it.
    """
    me = current_user()
    assert me is not None
    url = (request.form.get("url") or "").strip()
    title = (request.form.get("title") or "").strip()
    description = (request.form.get("description") or "").strip()
    if not _valid_live_url(url):
        flash("Stream link must be a full URL or a path starting with /.", "error")
        return redirect(url_for("auth.account"))
    if not title:
        flash("Room title is required.", "error")
        return redirect(url_for("auth.account"))
    # Cover (optional): a cropped 16:9 image stored under the shared covers dir.
    cover = request.files.get("cover")
    new_cover: Optional[str] = None
    if cover is not None and cover.filename:
        new_cover = storage.save_cover(
            cover, Path(current_app.config["COVERS_DIR"]), current_app.config
        )
    room = db.get_live_room_by_owner(me["id"])
    if room is None:
        db.create_live_room(
            url, title, owner_id=me["id"], description=description, cover_filename=new_cover
        )
        flash("Live room created.", "success")
    else:
        if new_cover and room.get("cover_filename"):
            _delete_cover_file(room["cover_filename"])
        db.update_live_room(
            room["id"],
            url=url,
            title=title,
            description=description,
            cover_filename=new_cover,
        )
        flash("Live room updated.", "success")
    return redirect(url_for("auth.account"))