"""Batch stream builds: rebuild or repair every locally cached video (§36).

One button, one background thread, one video at a time. This module owns no ffmpeg
logic: every item goes through :func:`app.hls.ensure_stream`, the same entry point a
viewer's first request and the per-row admin buttons already use. What a batch adds
is the three decisions a per-row button cannot make for 86 rows:

- which rows are candidates (:func:`plan`, 36.0 decision 2), so a rebuild never
  destroys a working stream and a repair never re-encodes a video it already repaired;
- whether this item fits on this disk *right now* (32.4 + 33.8, re-checked per item),
  so a 1 GB outlier is skipped instead of starving the rest of the run;
- when to stop (36.0 decision 4): between videos, never mid-encode.

There is exactly one batch slot per process. On a 1 vCPU / 956 MiB server a batch of
parallel ffmpegs is the OOM story 32.0 already measured with libsvtav1, so the queue
is strictly sequential and :func:`start` refuses while a batch runs. The queue lives
in process memory only: a restart abandons it and leaves the row that was mid-build at
``building`` / ``repairing``; startup recovery releases those stale states.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import db, hls, storage

logger = logging.getLogger("simple_video_share.batch")

#: The two batch modes are the two stream modes hls already defines.
MODES = (hls.MODE_COPY, hls.MODE_REPAIR)

#: Wall seconds per second of video, from the measurements 31.3 / 32.0: a copy remux
#: runs at roughly 30x realtime, a single-core ultrafast repair at roughly 1x. Used
#: only for the estimate the admin reads before committing to a batch.
_SPEED_FACTOR = {hls.MODE_COPY: 1.0 / 30.0, hls.MODE_REPAIR: 1.0}

#: How many per-item skip / failure notes the banner keeps. The full list is in app.log.
_MAX_NOTES = 12

_guard = threading.Lock()
_state: Optional[dict[str, Any]] = None
_cancel = threading.Event()


def _label(mode: Optional[str]) -> str:
    """Button-shaped name for a mode, so the banner and the flash say the same words."""
    if mode == hls.MODE_REPAIR:
        return "Repair"
    if mode == hls.MODE_COPY:
        return "Rebuild"
    return "Stream"


def _now_text() -> str:
    """UTC timestamp in the text form SQLite writes (same as hls._now_text)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _mb(n: int) -> str:
    """Bytes as whole MB, the unit the admin headroom messages speak in."""
    return str(max(0, int(n)) // 1048576)


def _fmt_duration(seconds: int) -> str:
    """Seconds as the coarse span a human plans a batch against."""
    if seconds < 90:
        return "%d s" % seconds
    minutes = int(round(seconds / 60.0))
    if minutes < 60:
        return "%d min" % minutes
    hours, rest = divmod(minutes, 60)
    return "%d h" % hours if rest == 0 else "%d h %d min" % (hours, rest)


def _estimate(mode: str, duration_seconds: float) -> tuple[int, str]:
    """(seconds, human text) for one sequential pass over ``duration_seconds`` of video.

    The two answers this cannot give are distinguished on purpose: no durations on
    these rows at all (unknown) against a real queue whose wall time rounds to under
    a second (a copy remux at ~30x realtime).
    """
    if duration_seconds <= 0.0:
        return 0, "unknown (these rows have no recorded duration)"
    seconds = int(round(max(0.0, float(duration_seconds)) * _SPEED_FACTOR.get(mode, 1.0)))
    if seconds < 1:
        return 0, "under a second on one core"
    return seconds, "about " + _fmt_duration(seconds) + " on one core"

# --------------------------------------------------------------------------
# candidate rules (36.0 decision 2)
# --------------------------------------------------------------------------
def blocker_for(
    videos_dir: Path | str, video: dict[str, Any], mode: str, app_config: Any = None
) -> Optional[str]:
    """Use the same eligibility as single-video admin actions."""
    actions = hls.stream_actions(videos_dir, video, app_config)
    return actions["repair_blocker" if mode == hls.MODE_REPAIR else "rebuild_blocker"]


def plan(
    videos_dir: Path | str,
    mode: str,
    videos: Optional[list[dict[str, Any]]] = None,
    app_config: Any = None,
) -> dict[str, Any]:
    """Split the library into candidate rows and skipped rows (36.0 decision 2).

    ``videos`` is injectable so a probe can plan a fixed set of rows instead of the
    live library. Returns the candidate row dicts (in list_all_videos order), the
    skipped rows as (id, reason) pairs and the summed duration the estimate is built
    from.
    """
    base = Path(videos_dir)
    candidates: list[dict[str, Any]] = []
    skipped: list[tuple[int, str]] = []
    duration_seconds = 0.0
    for video in (videos if videos is not None else db.list_all_videos()):
        reason = blocker_for(base, video, mode, app_config)
        if reason is not None:
            skipped.append((int(video["id"]), reason))
            continue
        candidates.append(video)
        try:
            duration_seconds += float(video.get("duration") or 0.0)
        except (TypeError, ValueError):
            continue
    return {
        "candidates": candidates,
        "skipped": skipped,
        "duration_seconds": duration_seconds,
    }


def _headroom_blocker(
    videos_dir: Path, source_bytes: int, mode: str, app_config: Any
) -> Optional[str]:
    """32.4 + 33.8 for one batch item: does this file fit on this disk right now?

    The same factors the single-video path uses: 1.35 for a repair, which holds the
    source, repaired.mp4 and its segments at once, and 2.0 for a copy, which briefly
    holds the MP4 and a near-complete duplicate of it as segments. Skipping here is
    what keeps a batch moving; the hard reservation still happens inside ensure_stream,
    which may evict LRU rows to make room.
    """
    factor = 1.35 if mode == hls.MODE_REPAIR else 2.0
    needed = int(source_bytes * factor)
    headroom = storage.free_space_bytes(str(videos_dir.parent)) - storage.min_free_space_bytes(
        app_config
    )
    if headroom < needed:
        return ("needs ~%s MB of headroom, this server has %s MB: drop a cache first"
                % (_mb(needed), _mb(max(headroom, 0))))
    return None

# --------------------------------------------------------------------------
# the one batch slot
# --------------------------------------------------------------------------
def _blank(mode: Optional[str] = None) -> dict[str, Any]:
    """A status dict with nothing queued (what status() returns before any batch)."""
    return {
        "running": False,
        "mode": mode,
        "total": 0,
        "done": 0,
        "ready": 0,
        "none": 0,
        "failed": 0,
        "skipped": 0,
        "current_video_id": None,
        "current_title": "",
        "started_at": None,
        "finished_at": None,
        "stopped": False,
        "estimated_seconds": 0,
        "estimate_text": "",
        "message": "",
        "notes": [],
    }


def status() -> dict[str, Any]:
    """Snapshot of the running or last finished batch (36.3 renders this dict)."""
    with _guard:
        state = _state
        if state is None:
            return _blank()
        snapshot = dict(state)
        snapshot["notes"] = list(state["notes"])
        return snapshot


def start(videos_dir: Path | str, app_config: Any, mode: str) -> tuple[bool, str]:
    """Queue a batch. Returns (started, message); the message is the refusal reason.

    Refuses while another batch is running (36.0 decision 1) and refuses a queue that
    can only produce zeros - a button that starts nothing and reports success is how
    an admin discovers the candidate rules 86 videos late.
    """
    global _state  # the one batch slot: start owns creating it (36.1)

    if mode not in MODES:
        return False, "Unknown batch mode: %s" % mode
    base = Path(videos_dir)
    cfg = hls.settings(app_config)
    if not cfg["enabled"]:
        return False, "HLS is off (hls_enabled = false in config.json)."
    if mode == hls.MODE_REPAIR and not cfg["repair_enabled"]:
        return False, "GOP repair is off (hls_repair_enabled = false in config.json)."
    if hls.ffmpeg_binary(cfg) is None:
        return False, "ffmpeg was not found on this server, so nothing can be segmented."

    planned = plan(base, mode, app_config=app_config)
    candidates = planned["candidates"]
    if not candidates:
        return False, (
            "Nothing to queue: %d row(s) were skipped - already ready, mid-build or "
            "with no local copy on this server." % len(planned["skipped"])
        )
    estimate_seconds, estimate_text = _estimate(mode, planned["duration_seconds"])
    message = "%s batch queued: %d video(s), %d row(s) skipped, %s." % (
        _label(mode), len(candidates), len(planned["skipped"]), estimate_text,
    )
    with _guard:
        running = _state
        if running is not None and running["running"]:
            return False, (
                "A %s batch is already running: %d of %d video(s) done. Stop it first."
                % (_label(running["mode"]), running["done"], running["total"])
            )
        _cancel.clear()
        _state = _blank(mode)
        _state["running"] = True
        _state["total"] = len(candidates)
        _state["started_at"] = _now_text()
        _state["estimated_seconds"] = estimate_seconds
        _state["estimate_text"] = estimate_text
        _state["message"] = message
    logger.info(
        "batch: %s queued %d video(s), skipped %d, estimated %s",
        mode, len(candidates), len(planned["skipped"]), estimate_text,
    )
    thread = threading.Thread(
        target=_run,
        args=(base, app_config, mode, [int(video["id"]) for video in candidates]),
        name="hls-batch-" + mode,
        daemon=True,
    )
    thread.start()
    return True, message


def request_stop() -> tuple[bool, str]:
    """Ask the worker to stop after the video in progress (36.0 decision 4).

    An ffmpeg already running is never killed: its tree finishes, validates and
    lands, and the queue ends at the next item. Mid-encode cancellation is how a
    .tmp tree gets abandoned.
    """
    message = "Stop requested: the video in progress finishes, then the batch ends."
    with _guard:
        state = _state
        if state is None or not state["running"]:
            return False, "No batch is running."
        state["message"] = message
    _cancel.set()
    logger.info("batch: stop requested")
    return True, message

# --------------------------------------------------------------------------
# the worker thread
# --------------------------------------------------------------------------
def _run(videos_dir: Path, app_config: Any, mode: str, video_ids: list[int]) -> None:
    """Walk the queued ids one at a time. A daemon-thread body, never a request body.

    Each item re-reads its own row: the queue is a decision made at start time and
    the disk, the cache and the viewers have had since then to change their minds
    (blocker_for re-runs, then the headroom pre-check re-runs).
    """
    for video_id in video_ids:
        if _cancel.is_set():
            _finish(stopped=True)
            return
        video = db.get_video_by_id(video_id)
        if video is None:
            _record(video_id, "skipped", "row no longer exists")
            continue
        blocker = blocker_for(videos_dir, video, mode, app_config)
        if blocker is not None:
            _record(video_id, "skipped", blocker)
            continue
        from . import repair_sync
        sync_only = mode == hls.MODE_REPAIR and repair_sync.needs_sync(videos_dir, video)
        if not sync_only:
            source = hls.source_path_for(videos_dir, video)
            if source is None:
                # Raced with an eviction between blocker_for and this stat.
                _record(video_id, "skipped", "no local copy on this server")
                continue
            try:
                source_bytes = source.stat().st_size
            except OSError:
                source_bytes = 0
            headroom = _headroom_blocker(videos_dir, source_bytes, mode, app_config)
            if headroom is not None:
                _record(video_id, "skipped", headroom)
                logger.warning("batch: id=%s skipped: %s", video_id, headroom)
                continue
        _current(video_id, str(video.get("title") or ""))
        try:
            state = hls.ensure_stream(
                videos_dir, video, app_config, mode=mode, reason="batch " + mode,
            )
        except Exception as exc:  # noqa: BLE001 - one bad row must not end the queue
            logger.exception("batch: id=%s raised", video_id)
            _record(video_id, "failed", "crashed: %s" % exc)
            continue
        job = hls.progress(videos_dir, video_id)
        if job.get("error"):
            _record(video_id, "failed", str(job["error"]))
        elif state == hls.STATE_READY:
            _record(video_id, "ready", "")
        elif state == hls.STATE_NONE:
            # 32.5: a copy cut of a sparse-GOP source is valid HLS and useless to
            # seek in, so ensure_stream throws that tree away. Name the button that
            # fixes it instead of reporting a silent zero.
            _record(video_id, "none", "copy-only tree discarded - needs Repair GOP")
        elif state == hls.STATE_FAILED:
            _record(video_id, "failed", "build failed - see the hls lines in app.log")
        else:
            _record(video_id, "skipped", "left at state %s" % state)
    _finish(stopped=False)


def _current(video_id: int, title: str) -> None:
    """Point the banner at the video ffmpeg is about to work on."""
    with _guard:
        state = _state
        if state is None:
            return
        state["current_video_id"] = video_id
        state["current_title"] = title


def _record(video_id: int, bucket: str, note: str) -> None:
    """Close one item out: bump its counter, keep the note, log the line."""
    with _guard:
        state = _state
        if state is None:
            return
        state[bucket] = int(state.get(bucket, 0)) + 1
        state["done"] = int(state["done"]) + 1
        state["current_video_id"] = None
        state["current_title"] = ""
        if note:
            state["notes"].append("id %d: %s" % (video_id, note))
            state["notes"] = state["notes"][-_MAX_NOTES:]
    logger.info("batch: id=%s -> %s (%s)", video_id, bucket, note or "-")


def _summary(state: dict[str, Any], stopped: bool) -> str:
    """The line a batch ends on, in the banner, in the flash and in the log."""
    return (
        "%s batch %s: %d of %d video(s) processed - %d ready, %d skipped, "
        "%d failed, %d discarded (a skip or failure reason is listed above and in app.log)"
        % (
            _label(state["mode"]),
            "stopped" if stopped else "finished",
            state["done"], state["total"], state["ready"], state["skipped"],
            state["failed"], state["none"],
        )
    )


def _finish(stopped: bool) -> None:
    """Release the batch slot, leaving the summary visible for the next page render."""
    message = ""
    with _guard:
        state = _state
        if state is None:
            return
        state["running"] = False
        state["stopped"] = stopped
        state["finished_at"] = _now_text()
        state["current_video_id"] = None
        state["current_title"] = ""
        message = _summary(state, stopped)
        state["message"] = message
    if message:
        logger.info("batch: %s", message)
