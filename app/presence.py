"""In-memory live-room presence + ephemeral chat (§16.8).

Realtime on the live page is Server-Sent Events, not WebSockets: the Werkzeug
dev server rejects a WS upgrade before the route runs, and SSE needs no new
dependency. State lives in process memory only:

- ``members`` is keyed by ``user_id`` (not by connection), so refreshing the
  page never creates a duplicate entry.
- ``subs`` holds one queue per open SSE connection; a user may hold several
  (multiple tabs). A user is only "gone" when their last connection closes.
- Chat messages are ephemeral but kept in a per-room ring buffer (``messages``,
  capped at ``_MAX_MESSAGES``, L62): a newly-connected client is replayed the
  recent history, then live messages are broadcast. Nothing is persisted to disk.

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

#: Max chat messages kept per room (L62); the oldest is dropped when exceeded.
#: A newly-connected client is replayed this recent history on entry.
_MAX_MESSAGES = 50


class _Room:
    """Presence state for one room. All access under :data:`_LOCK`."""

    def __init__(self) -> None:
        # user_id -> {"id": int, "username": str, "avatar_url": str}
        self.members: dict[int, dict[str, Any]] = {}
        # user_id -> list of (sub_id, queue); one entry per open connection.
        self.subs: dict[int, list[tuple[int, queue.Queue[Any]]]] = {}
        # Last chat messages (oldest first), capped at _MAX_MESSAGES (L62).
        self.messages: list[dict[str, Any]] = []


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
    current ``state`` (the online count, i.e. the total number of users in the
    room) is yielded next, followed by the room's recent chat history (the
    messages kept in the ring buffer, L62), then any ``join``/``leave``/
    ``message`` events. ``join``/``leave`` events carry the updated online count
    so clients stay in sync (L61), with a ``: hb`` comment every
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
            _broadcast(room, {"type": "join", "user": me, "online": len(room.members)})
        # The online count is the total number of distinct users in the room,
        # including the connecting user (added to room.members above if new).
        # join/leave events carry the same total so clients stay in sync (L61).
        online = len(room.members)
        # Snapshot the recent chat so this connection can replay it (L62).
        history = list(room.messages)
    try:
        yield _sse({"type": "state", "online": online})
        for msg in history:
            yield _sse(msg)
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
                    _broadcast(room, {"type": "leave", "user": me, "online": len(room.members)})
            if not room.members and not room.subs:
                _ROOMS.pop(room_id, None)


def broadcast_message(room_id: int, user: dict[str, Any], text: str) -> int:
    """Send a chat ``message`` event to every open connection in the room.

    The message is also appended to the room's ring buffer (capped at
    ``_MAX_MESSAGES``, dropping the oldest, L62) so a client that connects later
    can load the recent history. Returns the number of connections that
    received it (0 if nobody is watching, or the room does not exist).
    """
    with _LOCK:
        room = _ROOMS.get(room_id)
        if room is None:
            return 0
        payload = {"type": "message", "user": user, "text": text, "ts": int(time.time())}
        room.messages.append(payload)
        if len(room.messages) > _MAX_MESSAGES:
            room.messages.pop(0)  # drop the oldest, keep at most _MAX_MESSAGES
        count = sum(len(conns) for conns in room.subs.values())
        _broadcast(room, payload)
        return count
