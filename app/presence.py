"""In-memory room presence + ephemeral chat (§16.8, §17).

Realtime on the live and watch-together pages is Server-Sent Events, not
WebSockets: the Werkzeug dev server rejects a WS upgrade before the route
runs, and SSE needs no new dependency. State lives in process memory only:

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

Rooms are keyed by a namespaced string (``"live:<id>"`` or ``"watch:<id>"``)
to avoid collisions between live rooms and watch rooms that share the same
numeric id. See §17.
"""

import json
import queue
import threading
import time
from typing import Any, Generator, Optional

#: Seconds between heartbeat comments. Must stay well below the browser's
#: EventSource reconnect timeout (~60 s) and any proxy idle timeout.
HEARTBEAT_SECONDS = 15

#: Max chat messages kept per room (L62); the oldest is dropped when exceeded.
#: A newly-connected client is replayed this recent history on entry.
_MAX_MESSAGES = 50

#: How often (seconds) the background scheduler broadcasts ``time`` ticks to
#: every active watch room (§17.6). Must stay well under 1 s so that client
#: drift is corrected within the 1 s SLA.
_WATCH_TICK_SECONDS = 0.5

#: A member's reported position is treated as stale (disconnected) after this
#: many seconds without a fresh report; stale members are excluded from the pace.
_POSITION_STALE_SECONDS = 3.0


def live_room_key(room_id: int) -> str:
    """Namespaced key for a live room (§17.3)."""
    return f"live:{room_id}"


def watch_room_key(video_id: int) -> str:
    """Namespaced key for a watch-together room (§17.3)."""
    return f"watch:{video_id}"


class _Room:
    """Presence state for one room. All access under :data:`_LOCK`."""

    def __init__(self) -> None:
        # user_id -> {"id": int, "username": str, "avatar_url": str}
        self.members: dict[int, dict[str, Any]] = {}
        # user_id -> list of (sub_id, queue); one entry per open connection.
        self.subs: dict[int, list[tuple[int, queue.Queue[Any]]]] = {}
        # Last chat messages (oldest first), capped at _MAX_MESSAGES (L62).
        self.messages: list[dict[str, Any]] = []
        # Watch-together fields (§17); live rooms leave these at their defaults.
        self.video_id: int = 0
        self.title: str = ""
        # Slowest-viewer pacing (§17.4). ``play_state`` carries ``playing``,
        # the current pace ``target_t`` (the slowest fresh position), who paused
        # it (``paused_by``), and the last seek (``seek_seq``/``seek_t``) so a
        # client that missed the one-shot seek broadcast hard-seeks on the next
        # tick. The per-member positions that derive the pace live in
        # ``positions``.
        self.play_state: dict[str, Any] | None = None
        # user_id -> {"t": float, "at": float, "username": str}; the last
        # reported playback position (seconds) and its wall-clock timestamp.
        self.positions: dict[int, dict[str, Any]] = {}


_LOCK = threading.Lock()
_ROOMS: dict[str, _Room] = {}
_NEXT_SUB_ID = 0
_watch_scheduler_started: bool = False


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


def _pace(room: _Room) -> tuple[float | None, str | None]:
    """Compute the shared pace from the members' reported positions.

    The pace is the minimum position among members with a fresh report (less
    than ``_POSITION_STALE_SECONDS`` old); that member is the slowest one.
    Returns ``(None, None)`` when nobody has reported a fresh position yet.
    Callers must hold :data:`_LOCK`.
    """
    now = time.time()
    slowest_t: float | None = None
    slowest_name: str | None = None
    for _uid, pos in room.positions.items():
        if now - float(pos["at"]) > _POSITION_STALE_SECONDS:
            continue  # stale (disconnected) member: exclude from the pace.
        if slowest_t is None or float(pos["t"]) < slowest_t:
            slowest_t = float(pos["t"])
            slowest_name = str(pos.get("username", ""))
    return (slowest_t, slowest_name)


def online_count(key: str) -> int:
    """Number of distinct users in the room (0 if the room is gone)."""
    with _LOCK:
        room = _ROOMS.get(key)
        return len(room.members) if room is not None else 0


def stream(
    key: str,
    me: dict[str, Any],
    history: Optional[list[dict[str, Any]]] = None,
) -> Generator[str, None, None]:
    """Yield the SSE events for one connection of user ``me`` in room ``key``.

    ``me`` is ``{"id": int, "username": str, "avatar_url": str}`` (built by the
    route). On entry the connection is registered; a brand-new user is added to
    ``members`` and a ``join`` is broadcast (to everyone, including them). The
    current ``state`` (the online count plus the full member list, so the client
    can render the room's avatars, §17.10) is yielded next, followed by the
    room's recent chat history (the
    messages kept in the ring buffer, L62), then any ``join``/``leave``/
    ``message`` events. For watch rooms the state payload also includes
    ``playing``, ``target_t``, ``paused_by``, ``seek_t``, and ``seek_seq``
    (§17.4). ``join``/``leave``
    events carry the updated online count so clients stay in sync (L61), with
    a ``: hb`` comment every ``HEARTBEAT_SECONDS`` of silence.

    On exit (the client disconnected, so the generator is closed) the
    connection is removed; if it was the user's last connection the member
    entry is deleted and a ``leave`` is broadcast. Rooms with no members and
    no connections are dropped from the global map.
    """
    global _NEXT_SUB_ID
    q: queue.Queue[Any] = queue.Queue()
    with _LOCK:
        room = _ROOMS.setdefault(key, _Room())
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
        online = len(room.members)
        # Replay the recent chat (L62): live rooms pass a DB-backed history;
        # watch rooms use the in-memory ring buffer.
        replay = history if history is not None else list(room.messages)
        # Snapshot the member list so the client can render the avatar stack
        # immediately (§17.10); includes the connecting user, added above.
        members = list(room.members.values())
        # For watch rooms, include the play state + the current pace in the
        # initial payload (the new joiner seeks to the pace, §17.4).
        play_state = dict(room.play_state) if room.play_state is not None else None
        pace: tuple[float | None, str | None] = (None, None)
        if play_state is not None:
            pace = _pace(room)
    try:
        state_payload: dict[str, Any] = {
            "type": "state",
            "online": online,
            "members": members,
        }
        if play_state is not None:
            state_payload["playing"] = play_state["playing"]
            state_payload["target_t"] = (
                pace[0] if pace[0] is not None else play_state["target_t"]
            )
            state_payload["paused_by"] = play_state.get("paused_by")
            state_payload["seek_t"] = play_state.get("seek_t", 0.0)
            state_payload["seek_seq"] = play_state.get("seek_seq", 0)
            if pace[1] is not None:
                state_payload["slowest"] = pace[1]
        yield _sse(state_payload)
        for msg in replay:
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
                    room.positions.pop(me["id"], None)
                    _broadcast(room, {"type": "leave", "user": me, "online": len(room.members)})
            if not room.members and not room.subs:
                _ROOMS.pop(key, None)


def broadcast_message(key: str, user: dict[str, Any], text: str) -> int:
    """Send a chat ``message`` event to every open connection in the room.

    The message is also appended to the room's ring buffer (capped at
    ``_MAX_MESSAGES``, dropping the oldest, L62) so a client that connects later
    can load the recent history. Returns the number of connections that
    received it (0 if nobody is watching, or the room does not exist).
    """
    with _LOCK:
        room = _ROOMS.get(key)
        if room is None:
            return 0
        payload = {"type": "message", "user": user, "text": text, "ts": int(time.time())}
        room.messages.append(payload)
        if len(room.messages) > _MAX_MESSAGES:
            room.messages.pop(0)  # drop the oldest, keep at most _MAX_MESSAGES
        count = sum(len(conns) for conns in room.subs.values())
        _broadcast(room, payload)
        return count
def init_watch_state(key: str, video_id: int, title: str) -> None:
    """Create or re-initialise the play state for a watch room.

    Called from the room-page route before rendering, so the play state exists
    by the time the first SSE connection opens. Idempotent: if the room already
    has a play state, it is not overwritten.
    """
    with _LOCK:
        room = _ROOMS.setdefault(key, _Room())
        room.video_id = video_id
        room.title = title
        if room.play_state is None:
            room.play_state = {
                "playing": True,
                "target_t": 0.0,
                "paused_by": None,
                "seek_seq": 0,
                "seek_t": 0.0,
            }


def set_watch_state(
    key: str,
    action: str,
    t: float | None = None,
    actor: str | None = None,
) -> dict[str, Any] | None:
    """Handle a play/pause/seek action and broadcast the updated state.

    ``action`` is ``"play"``, ``"pause"``, or ``"seek"``. For ``"seek"``, ``t``
    is the new video position in seconds — every member's reported position is
    reset to it so the group stays together, ``seek_t`` is set to ``t``, and
    ``seek_seq`` is bumped so a client that missed this one-shot broadcast
    hard-seeks to ``seek_t`` on the next tick. When ``actor`` is given and the
    action actually changes the shared state, the payload also carries ``actor``
    + ``action`` so clients can show a "XXX paused / seeked / resumed" notice. A
    pause records ``paused_by`` (the username) in the play state so every client
    can show an "XXX paused" overlay while the group is stopped; playback clears
    it. Returns the updated play state dict, or ``None`` if the room is gone.
    """
    with _LOCK:
        room = _ROOMS.get(key)
        if room is None or room.play_state is None:
            return None
        ps = room.play_state
        seek = False
        changed = False

        if action == "play" and not ps["playing"]:
            ps["playing"] = True
            ps["paused_by"] = None
            changed = True
        elif action == "pause" and ps["playing"]:
            ps["playing"] = False
            ps["paused_by"] = actor
            changed = True
        elif action == "seek" and t is not None:
            # Reset every member's position to the seek target so the group
            # stays together (the playing state is unchanged by a seek).
            for pos in room.positions.values():
                pos["t"] = float(t)
            ps["target_t"] = float(t)
            ps["seek_t"] = float(t)
            ps["seek_seq"] = ps.get("seek_seq", 0) + 1
            seek = True
            changed = True

        target_t, slowest = _pace(room)
        if target_t is not None:
            ps["target_t"] = target_t
        payload: dict[str, Any] = {
            "type": "time",
            "playing": ps["playing"],
            "target_t": ps["target_t"],
            "paused_by": ps.get("paused_by"),
            "seek_t": ps.get("seek_t", 0.0),
            "seek_seq": ps.get("seek_seq", 0),
        }
        if seek:
            payload["seek"] = True
        if slowest is not None:
            payload["slowest"] = slowest
        if changed and actor is not None:
            payload["actor"] = actor
            payload["action"] = action
        _broadcast(room, payload)
        return dict(ps)


def report_position(
    key: str, user_id: int, username: str, t: float
) -> dict[str, Any] | None:
    """Record a member's current playback position (seconds) for pacing.

    The position is stored with the username and a wall-clock timestamp; the
    pace is recomputed on every watch tick from the freshest positions (§17.4).
    Returns the updated play state dict, or ``None`` if the room is gone.
    """
    with _LOCK:
        room = _ROOMS.get(key)
        if room is None or room.play_state is None:
            return None
        room.positions[user_id] = {
            "t": float(t),
            "at": time.time(),
            "username": username,
        }
        return dict(room.play_state)


def get_watch_state(key: str) -> dict[str, Any] | None:
    """Return the play state for a watch room, or ``None`` if the room is gone."""
    with _LOCK:
        room = _ROOMS.get(key)
        if room is None or room.play_state is None:
            return None
        return dict(room.play_state)


def active_watch_rooms() -> list[dict[str, Any]]:
    """Return a list of active watch rooms (rooms with at least one member).

    Each entry is ``{"video_id": int, "title": str, "online": int}``.
    """
    with _LOCK:
        return [
            {
                "video_id": room.video_id,
                "title": room.title,
                "online": len(room.members),
            }
            for key, room in _ROOMS.items()
            if key.startswith("watch:")
            and room.members
            and room.play_state is not None
        ]


def _watch_tick_loop() -> None:
    """Background thread: broadcast ``time`` ticks to all active watch rooms.

    Each tick recomputes the shared pace (the slowest fresh position, §17.4)
    and broadcasts it with ``target_t``, ``slowest``, ``seek_t``, and
    ``seek_seq`` (the latter two let a client that missed the one-shot seek
    broadcast hard-seek on the next tick).
    """
    while True:
        time.sleep(_WATCH_TICK_SECONDS)
        with _LOCK:
            for key, room in list(_ROOMS.items()):
                if (
                    not key.startswith("watch:")
                    or room.play_state is None
                    or not room.members
                ):
                    continue
                ps = room.play_state
                target_t, slowest = _pace(room)
                if target_t is not None:
                    ps["target_t"] = target_t
                payload: dict[str, Any] = {
                    "type": "time",
                    "playing": ps["playing"],
                    "target_t": ps["target_t"],
                    "paused_by": ps.get("paused_by"),
                    "seek_t": ps.get("seek_t", 0.0),
                    "seek_seq": ps.get("seek_seq", 0),
                }
                if slowest is not None:
                    payload["slowest"] = slowest
                _broadcast(room, payload)


def start_watch_scheduler() -> None:
    """Start the background thread that broadcasts time ticks to watch rooms.

    Idempotent: calling it multiple times has no effect after the first call.
    """
    global _watch_scheduler_started
    if _watch_scheduler_started:
        return
    _watch_scheduler_started = True
    threading.Thread(target=_watch_tick_loop, daemon=True).start()
