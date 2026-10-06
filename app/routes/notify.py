"""Notifications blueprint (§21): the per-user SSE feed, the list/read APIs,
and the dedicated /notifications page.

Endpoints:
- ``GET  /notify/stream``  -> per-user SSE feed (``notification`` and
  ``unread-cleared`` events; 15 s heartbeat comments). ``@login_required``.
- ``GET  /notify/api/list`` -> JSON ``{"ok", "unread", "items"}``, newest
  first, ``?limit=`` (default 20, capped at 100). Feeds the dropdown panel.
- ``POST /notify/api/read`` -> marks all of the caller's notifications read
  and broadcasts ``unread-cleared`` on the caller's SSE channel so every
  other open tab drops its dot too.
- ``GET  /notifications``  -> the dedicated page: the 100 newest items, one
  render per page load, marked all-read on load (same rule as the panel).

The item shape is shared by all three surfaces (list API, SSE payload, page)
so the frontend renders whatever ``type`` arrives; a future notification kind
is a new ``type`` string plus one rendering case in ``notify.js``. If a future
type (e.g. live) outgrows a one-line row, its dedicated view should filter this
same table by ``type`` (a query param or small extra route here) rather than
introduce a new table.
"""

import logging
from typing import Any

from flask import (
    Blueprint,
    Response,
    jsonify,
    render_template,
    request,
    url_for,
)

from .. import db, notify
from ..auth import current_user, login_required

logger = logging.getLogger("simple_video_share.routes.notify")

notify_bp = Blueprint("notify", __name__, url_prefix="/notify")
notify_page_bp = Blueprint("notify_page", __name__)

#: How many notifications the dedicated page renders.
_PAGE_LIMIT = 100
#: Hard cap for the panel's ?limit= parameter.
_LIST_LIMIT_MAX = 100


def _avatar_url(filename: str | None) -> str:
    """Avatar URL for a stored filename (the default asset when empty)."""
    if filename:
        return url_for("home.avatar_file", filename=filename)
    return url_for("static", filename="img/avatar-default.svg")


def item_from_parts(
    notification_id: int,
    type: str,
    actor: dict[str, Any],
    video: dict[str, Any],
    comment_id: int | None,
    created_at: str | None = None,
    unread: bool = True,
) -> dict[str, Any]:
    """Shape one notification from the actor/video dicts (trigger path).

    ``created_at`` is None for a just-created event pushed over SSE: the
    client renders "just now" for a null timestamp.
    """
    return {
        "id": int(notification_id),
        "type": type,
        "actor_username": actor.get("username") or "Unknown",
        "actor_avatar_url": _avatar_url(actor.get("avatar_filename")),
        "video_title": (video or {}).get("title") or "",
        "video_id": (video or {}).get("id"),
        "comment_id": comment_id,
        "created_at": created_at,
        "unread": bool(unread),
    }


def item_from_row(row: dict[str, Any]) -> dict[str, Any]:
    """Shape one notification DB row for JSON / page rendering."""
    return {
        "id": int(row["id"]),
        "type": row["type"],
        "actor_username": row.get("actor_username") or "Unknown",
        "actor_avatar_url": _avatar_url(row.get("actor_avatar_filename")),
        "video_title": row.get("video_title") or "",
        "video_id": row.get("video_id"),
        "comment_id": row.get("comment_id"),
        "created_at": row.get("created_at"),
        "unread": row.get("read_at") is None,
    }


def push_new(user_id: int, item: dict[str, Any]) -> None:
    """Push a freshly created notification onto the user's open SSE tabs.

    A no-op when the user is offline (the persisted row drives the baked dot
    on the next page render).
    """
    notify.broadcast(user_id, {"type": "notification", "item": item})


def push_unread_cleared(user_id: int) -> None:
    """Tell the user's open tabs their unread dot may be dropped."""
    notify.broadcast(user_id, {"type": "unread-cleared"})


# ---------------------------------------------------------------------------
# /notify/stream — per-user SSE feed
# ---------------------------------------------------------------------------

@notify_bp.route("/stream")
@login_required
def stream() -> Any:
    """SSE feed for the caller: ``notification`` / ``unread-cleared`` events.

    One long-lived ``text/event-stream`` per browser tab. Buffering is
    disabled explicitly: the dev server streams fine, but a reverse proxy in
    front (Apache) would otherwise hold the events back.
    """
    me = current_user()
    assert me is not None
    # The generator never touches the Flask context (the payload is built by
    # whoever broadcasts), so it can be returned as-is: Werkzeug closes it on
    # client disconnect and its finally block deregisters the queue.
    resp = Response(notify.stream(me["id"]), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache, no-transform"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


# ---------------------------------------------------------------------------
# /notify/api/* — dropdown panel APIs
# ---------------------------------------------------------------------------

@notify_bp.route("/api/list")
@login_required
def api_list() -> Any:
    """The caller's recent notifications, newest first (dropdown panel)."""
    me = current_user()
    assert me is not None
    try:
        limit = int(request.args.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, _LIST_LIMIT_MAX))
    items = [item_from_row(r) for r in db.list_notifications(me["id"], limit)]
    return jsonify(ok=True, unread=db.unread_count(me["id"]), items=items)


@notify_bp.route("/api/read", methods=["POST"])
@login_required
def api_read() -> Any:
    """Mark all of the caller's notifications read; sync the other tabs.

    Opening the panel or the /notifications page calls this; the broadcast
    drops the dot on the caller's other open tabs (their SSE handlers hide
    the dot on ``unread-cleared``).
    """
    me = current_user()
    assert me is not None
    db.mark_all_read(me["id"])
    push_unread_cleared(me["id"])
    return jsonify(ok=True, unread=0)


# ---------------------------------------------------------------------------
# /notifications — dedicated page
# ---------------------------------------------------------------------------

@notify_page_bp.route("/notifications")
@login_required
def page() -> str:
    """The user's 100 newest notifications; marks them all read on load.

    The item list is fetched *before* the mark-all-read so each row keeps its
    original unread flag for the one-time "new" marker, while the baked dot
    (computed from the DB when the template renders) is already cleared and
    the other tabs get the ``unread-cleared`` SSE event.
    """
    me = current_user()
    assert me is not None
    rows = db.list_notifications(me["id"], _PAGE_LIMIT)
    db.mark_all_read(me["id"])
    push_unread_cleared(me["id"])
    items = [item_from_row(r) for r in rows]
    return render_template("notifications.html", items=items)