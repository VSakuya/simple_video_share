"""In-memory live-room presence + ephemeral chat (§16.8).

Realtime on the live page is Server-Sent Events, not WebSockets: the Werkzeug
dev server rejects a WS upgrade before the route runs, and SSE needs no new
dependency. State lives in process memory only:

- ``members`` is keyed by ``user_id`` (not by connection), so refreshing the
  page never creates a duplicate entry.
- ``subs`` holds one queue per open SSE connection; a user may hold several
  (multiple tabs). A user is only "gone" when their last connection closes.
- Chat messages are ephemeral: broadcast to live connections, never stored.

Leave detection is disconnect-driven: Werkzeug closes the response generator
when the client goes away, and the generator's ``finally`` deregisters. The
15 s heartbeat doubles as the upper bound on how long a dead connection can
linger. This module never touches Flask — the route builds the user payload
and wraps the generator.
"""

import json
import queue
import threading
import time
from typing import Any, Generator

#: Seconds between heartbeat comments. Must stay well below the browser's
#: EventSource reconnect timeout (~60 s) and any proxy idle timeout.
HEARTBEAT_SECONDS = 15


class _Room:
    """Presence state for one room. All access under :data:`_LOCK`."""

    def __init__(self) -> None:
        # user_id -> {"id": int, "username": str, "avatar_url": str}
        self.members: dict[int, dict[str, Any]] = {}
        # user_id -> list of (sub_id, queue); one entry per open connection.
        self.subs: dict[int, list[tuple[int, queue.Queue[Any]]]] = {}


_LOCK = threading.Lock()
_ROOMS: dict[int, _Room] = {}
_NEXT_SUB_ID = 0


def _sse(payload: dict[str, Any]) -> str:
    """Encode one SSE event (a single ``data:`` line, no ``event:`` name)."""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _broadcast(room: _Room, payload: dict[str, Any]) -> None:
    """Push one event onto every open connection in the room.

    Callers must hold :data:`_LOCK`; the queues are unbounded so this never
    blocks.
    """
    for conns in room.subs.values():
        for _sub_id, q in conns:
            q.put(payload)


def online_count(room_id: int) -> int:
    """Number of distinct users in the room (0 if the room is gone)."""
    with _LOCK:
        room = _ROOMS.get(room_id)
        return len(room.members) if room is not None else 0


def stream(room_id: int, me: dict[str, Any]) -> Generator[str, None, None]:
    """Yield the SSE events for one connection of user ``me`` in ``room_id``.

    ``me`` is ``{"id": int, "username": str, "avatar_url": str}`` (built by the
    route). On entry the connection is registered; a brand-new user is added to
    ``members`` and a ``join`` is broadcast (to everyone, including them). The
    current ``state`` (online count, excluding the connecting user) is
    yielded next, followed by any
    ``join``/``leave``/``message`` events, with a ``: hb`` comment every
    ``HEARTBEAT_SECONDS`` of silence.

    On exit (the client disconnected, so the generator is closed) the
    connection is removed; if it was the user's last connection the member
    entry is deleted and a ``leave`` is broadcast. Rooms with no members and
    no connections are dropped from the global map.
    """
    global _NEXT_SUB_ID
    q: queue.Queue[Any] = queue.Queue()
    with _LOCK:
        room = _ROOMS.setdefault(room_id, _Room())
        _NEXT_SUB_ID += 1
        sub_id = _NEXT_SUB_ID
        is_new = me["id"] not in room.members
        if is_new:
            room.members[me["id"]] = me
        room.subs.setdefault(me["id"], []).append((sub_id, q))
        if is_new:
            _broadcast(room, {"type": "join", "user": me})
        # The online count excludes the connecting user: they are already
        # in room.members (added above if new), so subtract one.
        online = len(room.members) - 1
    try:
        yield _sse({"type": "state", "online": online})
        while True:
            try:
                payload = q.get(timeout=HEARTBEAT_SECONDS)
            except queue.Empty:
                # SSE comment line: keeps the connection (and any idle proxy)
                # alive without delivering anything to the client.
                yield ": hb\n\n"
            else:
                yield _sse(payload)
    finally:
        with _LOCK:
            conns = [c for c in room.subs.get(me["id"], []) if c[0] != sub_id]
            if conns:
                room.subs[me["id"]] = conns
            else:
                room.subs.pop(me["id"], None)
                if me["id"] in room.members:
                    del room.members[me["id"]]
                    _broadcast(room, {"type": "leave", "user": me})
            if not room.members and not room.subs:
                _ROOMS.pop(room_id, None)


def broadcast_message(room_id: int, user: dict[str, Any], text: str) -> int:
    """Send a chat ``message`` event to every open connection in the room.

    Returns the number of connections that received it (0 if nobody is
    watching, or the room does not exist).
    """
    with _LOCK:
        room = _ROOMS.get(room_id)
        if room is None:
            return 0
        payload = {"type": "message", "user": user, "text": text, "ts": int(time.time())}
        count = sum(len(conns) for conns in room.subs.values())
        _broadcast(room, payload)
        return count
