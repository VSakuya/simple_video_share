"""Live blueprint (§14.6): the live-room list and the DPlayer + flv.js view.

Two layers, mirroring the home gallery:
- ``GET /live``          -> the list of live rooms (gallery of cards).
- ``GET /live/<room_id>``-> the live player page for a single room.

The stream itself (``room.url``) is NOT served by this app: it is a full link to
the media server (Node-Media-Server) behind the reverse proxy — either an
internal ``/live/<code>.flv`` or an external URL. The pages here only render the
UI and drive the player from ``room.url``.
"""

import logging
from typing import Any

from flask import Blueprint, abort, render_template

from .. import db
from ..auth import login_required

live_bp = Blueprint("live", __name__, url_prefix="/live")
logger = logging.getLogger("simple_video_share.routes.live")


@live_bp.route("/")
@login_required
def index() -> str:
    """Layer 1: the list of live rooms (gallery)."""
    rooms = db.list_live_rooms()
    return render_template("live_list.html", rooms=rooms)


@live_bp.route("/<int:room_id>")
@login_required
def view(room_id: int) -> Any:
    """Layer 2: the live player page for a single room (fetched by its id)."""
    room = db.get_live_room(room_id)
    if room is None:
        abort(404)
    return render_template("live_view.html", room=room)