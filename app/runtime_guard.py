"""Deployment guard: this app is single-process by construction.

Everything realtime lives in one process's memory - ``presence.py`` rooms and
their SSE subscription queues, ``drive_worker``'s upload-progress table, the
watch-room tick scheduler and the in-memory segmented-upload sessions. A
multi-worker WSGI server (``gunicorn -w N``, uWSGI with processes, a Kubernetes
replica set sharing a volume) splits that state, and it shows up as what looks
like a data bug rather than a deployment mistake: online counts change depending
on which worker answers, upload progress disappears between polls, Watch
Together rooms never converge.

Two independent checks run once at startup, through :func:`check`:

1. **Launcher markers.** ``WEB_CONCURRENCY`` / ``UVICORN_WORKERS`` above 1, or a
   ``GUNICORN_CMD_ARGS`` asking for more than one worker, means the process tree
   was built for horizontal scaling.
2. **A heartbeat lock file** (``storage/.app.lock``, JSON holding our PID, mtime
   refreshed every :data:`HEARTBEAT_SECONDS`). If another process refreshed it
   moments ago under a different PID, a second live instance is sharing this
   ``storage/`` directory.

Neither check aborts startup on purpose: a lock left behind by a crashed process
must never block a deploy, and the second check is advisory by nature. Both log
(WARNING / CRITICAL) and every message is returned so the caller can surface it
in the log and, if needed, in the admin page. PID liveness is judged by heartbeat
freshness rather than ``os.kill``, which has no usable signal-0 semantics on
Windows.
"""

import atexit
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

from . import log

#: Name of the heartbeat file written under ``storage/``.
LOCK_NAME = ".app.lock"
#: How often the owning process refreshes the heartbeat (seconds).
HEARTBEAT_SECONDS = 10.0
#: A heartbeat older than this belongs to a process that is gone (seconds).
STALE_SECONDS = 45.0

#: Environment variables a multi-process launcher sets.
WORKER_ENV_VARS = ("WEB_CONCURRENCY", "UVICORN_WORKERS")

logger = log.get_logger("runtime_guard")


def _worker_env_warnings() -> List[str]:
    """Warn when the environment asks for more than one process."""
    warnings: List[str] = []
    for name in WORKER_ENV_VARS:
        raw = os.environ.get(name)
        if not raw:
            continue
        try:
            count = int(raw)
        except ValueError:
            continue
        if count > 1:
            warnings.append(
                "%s=%d: this app keeps room presence, upload progress and the "
                "watch scheduler in process memory, so multiple workers will "
                "disagree about all of it." % (name, count)
            )
    gunicorn_args = os.environ.get("GUNICORN_CMD_ARGS", "")
    if re.search(r"(--workers\b|--threads\b|workers\s*=)", gunicorn_args):
        warnings.append(
            "GUNICORN_CMD_ARGS requests gunicorn process workers: run "
            "waitress (single process, many threads) instead, see start.sh."
        )
    return warnings


def _read_lock(path: Path) -> Dict[str, Any]:
    """Read the heartbeat file, or an empty dict when it is unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_lock(path: Path) -> None:
    """Write (or reclaim) the heartbeat file atomically."""
    payload = json.dumps(
        {"pid": os.getpid(), "started_at": int(time.time())}, sort_keys=True
    )
    tmp = path.with_suffix(".lock.tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(str(tmp), str(path))
    except OSError as exc:  # read-only volume / concurrent takeover: advisory only
        logger.warning("runtime_guard: could not write %s: %s", path, exc)


def _heartbeat_loop(path: Path) -> None:
    """Keep the heartbeat fresh and shout if another process reclaimed it."""
    reclaimed_logged = False
    while True:
        time.sleep(HEARTBEAT_SECONDS)
        data = _read_lock(path)
        if data.get("pid") not in (None, os.getpid()):
            if not reclaimed_logged:
                logger.critical(
                    "runtime_guard: another process (pid %s) took over %s. Two "
                    "instances sharing one storage/ directory will lose presence "
                    "and upload-progress state.", data.get("pid"), path,
                )
                reclaimed_logged = True
            _write_lock(path)
        else:
            reclaimed_logged = False
            _write_lock(path)


def check(storage_dir: Path) -> List[str]:
    """Run the startup checks and start the heartbeat. Never raises.

    Returns the human-readable warnings found (empty when the deployment looks
    right). The caller logs them; the heartbeat thread keeps running for the
    life of the process and removes the lock file at exit.
    """
    warnings: List[str] = []
    try:
        warnings.extend(_worker_env_warnings())

        storage_dir.mkdir(parents=True, exist_ok=True)
        path = storage_dir / LOCK_NAME
        now = time.time()
        if path.exists():
            data = _read_lock(path)
            try:
                age = now - path.stat().st_mtime
            except OSError:
                age = None
            other_pid = data.get("pid")
            if (
                age is not None
                and age < STALE_SECONDS
                and other_pid is not None
                and other_pid != os.getpid()
            ):
                warnings.append(
                    "another instance is alive (pid %s, heartbeat %.0fs ago) and "
                    "shares %s. Presence, upload progress and Watch Together "
                    "state are per-process, so run exactly one."
                    % (other_pid, age, storage_dir)
                )

        _write_lock(path)
        atexit.register(_release_lock, path)
        threading.Thread(
            target=_heartbeat_loop, args=(path,),
            name="runtime-guard-heartbeat", daemon=True,
        ).start()
    except Exception as exc:  # noqa: BLE001 - a guard must never break startup
        logger.warning("runtime_guard: check failed: %s", exc)
    return warnings


def _release_lock(path: Path) -> None:
    """Delete the heartbeat file if it is still ours."""
    if _read_lock(path).get("pid") == os.getpid():
        try:
            path.unlink()
        except OSError:
            pass
