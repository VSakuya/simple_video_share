"""Per-user SSE fan-out for message notifications (§21).

Strictly per-user realtime delivery: a user's open tabs each hold one
``queue.Queue`` (the browser opens one ``EventSource`` per tab); ``broadcast``
enqueues a JSON payload onto all of them. Queues are unbounded so a broadcast
never blocks, and an entry is dropped when its generator closes (client
disconnect, the same disconnect-driven leave detection as ``presence.py``).
The 15 s heartbeat doubles as the upper bound on how long a dead connection
can linger.

Unlike ``presence.py`` there is no room/membership state: the registry is
flat ``user_id -> list[queue.Queue]``. Persistence lives in the
``notifications`` table, so a user with no open tabs still sees the red dot
on the next page render (the baked ``unread_notifications`` count).

Future types (e.g. "your live has started") reuse the same table and delivery:
a type-specific payload goes in the ``data`` JSON column, and unknown types
degrade to a generic line on the surfaces. If a future type outgrows that
(e.g. live notifications warranting their own page), the dedicated view should
filter this same table by ``type`` (a query param or a small extra route on
/notifications) rather than introducing a new table.
"""

import json
import queue
import threading
from typing import Any, Generator

#: Seconds between heartbeat comments. Must stay well below the browser's
#: EventSource reconnect timeout (~60 s) and any proxy idle timeout — the
#: same convention as presence.py.
HEARTBEAT_SECONDS = 15

_LOCK = threading.Lock()
#: user_id -> list of open SSE connection queues (one per connection/tab).
_SUBS: dict[int, list[queue.Queue[dict[str, Any]]]] = {}


def _sse(payload: dict[str, Any]) -> str:
    """Encode one SSE event (a single ``data:`` line, no ``event:`` name)."""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def online_count(user_id: int) -> int:
    """Number of open SSE connections for ``user_id`` (0 if nobody is online)."""
    with _LOCK:
        return len(_SUBS.get(user_id, []))


def broadcast(user_id: int, payload: dict[str, Any]) -> int:
    """Push ``payload`` onto every open connection of ``user_id``.

    A no-op when the user has no open connections (persistence covers offline
    users: the next page render bakes the unread dot from the DB). Returns
    the number of connections that received the event. The queues are
    unbounded, so this never blocks; callers hold no state of their own.
    """
    with _LOCK:
        conns = _SUBS.get(user_id)
        if not conns:
            return 0
        for q in conns:
            q.put(payload)
        return len(conns)


def stream(user_id: int) -> Generator[str, None, None]:
    """Yield the SSE events for one connection of ``user_id``.

    On entry the connection's queue is registered; the generator then yields
    every ``broadcast`` payload as an SSE event (``{"type": "notification",
    ...}`` or ``{"type": "unread-cleared"}``), with a ``: hb`` comment line
    every ``HEARTBEAT_SECONDS`` of silence so idle proxies do not drop the
    connection. On exit (the client went away, so the generator is closed)
    the queue is removed from the registry; the last connection's removal
    drops the user's registry entry entirely.
    """
    q: queue.Queue[dict[str, Any]] = queue.Queue()
    with _LOCK:
        _SUBS.setdefault(user_id, []).append(q)
    try:
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
            conns = [c for c in _SUBS.get(user_id, []) if c is not q]
            if conns:
                _SUBS[user_id] = conns
            else:
                _SUBS.pop(user_id, None)