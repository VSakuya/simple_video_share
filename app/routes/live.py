"""Live blueprint (§14.6): the live-room list and the DPlayer + flv.js view.

Two layers, mirroring the home gallery:
- ``GET /live``          -> the list of live rooms (gallery of cards).
- ``GET /live/<room_id>``-> the live player page for a single room.

The stream itself (``room.url``) is NOT served by this app: it is a full link to
the media server (Node-Media-Server) behind the reverse proxy — either an
internal ``/live/<code>.flv`` or an external URL. The pages here only render the
UI and drive the player from ``room.url``.

The ON AIR badge is driven by ``GET /live/status`` (§14.7): the server probes
every room's stream and reports which rooms are actually pushing.
"""

import concurrent.futures
import logging
from typing import Any

from flask import Blueprint, abort, jsonify, render_template, request

from .. import db
from ..auth import login_required
from ..live_probe import probe_stream

live_bp = Blueprint("live", __name__, url_prefix="/live")
logger = logging.getLogger("simple_video_share.routes.live")

# Basic-auth for the stream server — temporary (§14.7), used only by the
# on-air probe below and never sent to the browser. To be replaced by a
# config entry later.
_STREAM_USER = "MOYUER"
_STREAM_PASSWORD = "456456"


@live_bp.route("/")
@login_required
def index() -> str:
    """Layer 1: the list of live rooms (gallery)."""
    rooms = db.list_live_rooms()
    return render_template("live_list.html", rooms=rooms)


@live_bp.route("/status")
@login_required
def status() -> Any:
    """On-air status of every room (§14.7).

    Each room is probed in parallel (a video or audio FLV tag must arrive
    within the probe window); the list page polls this endpoint every 15 s.
    """
    rooms = db.list_live_rooms()
    base = request.host_url  # ends with "/"
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, min(8, len(rooms)))
    ) as pool:
        results = list(pool.map(lambda room: _probe(room["url"], base), rooms))
    on_air = {str(room["id"]): on for room, on in zip(rooms, results)}
    return jsonify(on_air=on_air)


def _probe(url: str, base: str) -> bool:
    """Probe one room; a root-relative url is resolved against this host."""
    if url.startswith("/"):
        url = base + url.lstrip("/")
    return probe_stream(url, _STREAM_USER, _STREAM_PASSWORD)


@live_bp.route("/<int:room_id>")
@login_required
def view(room_id: int) -> Any:
    """Layer 2: the live player page for a single room (fetched by its id)."""
    room = db.get_live_room(room_id)
    if room is None:
        abort(404)
    return render_template("live_view.html", room=room)