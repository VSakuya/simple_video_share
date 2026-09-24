"""Background Google Drive upload worker.

Uploads no longer block the HTTP request. A video is saved locally and a DB row
is created immediately with ``status='uploading'``; this worker then pushes the
file to Google Drive on a background thread and flips the row to ``'ready'``
(success) or ``'failed'`` (error). Progress is kept in-memory so the "My
videos" page can poll ``GET /user/progress``.

Videos in flight are registered so the cache-capacity enforcer never evicts a
local copy while its Drive upload is still running. On startup,
:meth:`resume_incomplete` re-enqueues any ``'uploading'`` video left over from a
previous run (its local file is still on disk, and a saved resumable-session
URI lets the transfer continue from the last committed chunk).
"""

import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, Optional, Set

logger = logging.getLogger("simple_video_share.drive_worker")


class DriveUploadWorker:
    """A single process-wide background uploader with a progress store."""

    # Progress samples older than this are dropped from the speed window.
    _SPEED_WINDOW_SECONDS = 4.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # video_id -> {"uploaded_bytes": int, "total_bytes": int, "error": str|None,
        #              "samples": deque[(monotonic_ts, uploaded_bytes)]}
        self._progress: Dict[int, Dict[str, Any]] = {}
        # Video ids whose Drive upload is currently in progress.
        self._in_flight: Set[int] = set()

    # -- public API -------------------------------------------------------
    def enqueue(
        self,
        video_id: int,
        title: str,
        local_path: str,
        drive_filename: str,
        size_bytes: int,
        folder_id_drive: str,
        base_dir: str,
        videos_dir: str,
        app_config: Any,
        resumable_uri: Optional[str] = None,
    ) -> None:
        """Start a background Drive upload for an already-created video row.

        ``resumable_uri`` (optional) resumes a session saved from an earlier
        attempt, so the transfer continues from the last committed chunk.
        """
        min_free = self._min_free(app_config)
        with self._lock:
            if video_id in self._in_flight:
                return
            self._in_flight.add(video_id)
            self._progress[video_id] = {
                "uploaded_bytes": 0,
                "total_bytes": size_bytes,
                "error": None,
                "samples": deque(),
            }
        thread = threading.Thread(
            target=self._run,
            args=(
                video_id, title, local_path, drive_filename, size_bytes,
                folder_id_drive, base_dir, videos_dir, min_free, app_config,
                resumable_uri,
            ),
            name=f"drive-upload-{video_id}",
            daemon=True,
        )
        thread.start()

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Return a copy of the progress store (keys stringified for JSON).

        Each entry also carries ``speed_bps``: the transfer speed measured over
        the recent sample window (``None`` until two samples are >= 1 s apart).
        """
        with self._lock:
            out: Dict[str, Dict[str, Any]] = {}
            for vid, p in self._progress.items():
                entry = {k: v for k, v in p.items() if k != "samples"}
                entry["speed_bps"] = self._speed(p.get("samples"))
                out[str(vid)] = entry
            return out

    def in_flight(self) -> Set[int]:
        """Return the set of video ids whose Drive upload is in progress."""
        with self._lock:
            return set(self._in_flight)

    def resume_incomplete(self, videos_dir: Any, app_config: Any) -> tuple[int, int]:
        """Resume ``'uploading'`` videos left over from a previous run.

        Their local files are still on disk, so the Drive upload is re-enqueued
        (resuming from the last committed chunk when a session URI was saved)
        instead of being failed. Videos whose local file is gone are marked
        ``'failed'``. Returns ``(resumed, failed)``. Safe to call on every boot:
        at that moment no transfer is in flight, so every ``'uploading'`` row is
        a leftover from a previous (crashed or restarted) run.
        """
        from . import db, drive
        resumed = 0
        failed = 0
        base_dir = str(os.path.dirname(str(videos_dir)))
        for video in db.list_uploading_videos():
            local = video.get("local_filename")
            if not local:
                db.update_video(video["id"], status="failed")
                failed += 1
                logger.warning(
                    "resume: video id=%s title=%r had no local file; marked failed",
                    video["id"], video.get("title"),
                )
                continue
            self.enqueue(
                video["id"],
                video.get("title") or "",
                os.path.join(str(videos_dir), local),
                video.get("drive_filename")
                or drive.make_drive_filename(
                    video.get("title") or "", int(video.get("size_bytes") or 0)
                ),
                int(video.get("size_bytes") or 0),
                str(app_config.get("DRIVE_FOLDER_ID", "")),
                base_dir,
                str(videos_dir),
                app_config,
                resumable_uri=video.get("drive_resumable_uri"),
            )
            resumed += 1
            logger.info(
                "resume: re-enqueued video id=%s title=%r",
                video["id"], video.get("title"),
            )
        return resumed, failed

    # -- internals --------------------------------------------------------
    def _min_free(self, app_config: Any) -> int:
        from . import storage
        try:
            return storage.min_free_space_bytes(app_config)
        except Exception:  # noqa: BLE001 - fall back to a sane default
            return 500 * 1024 * 1024

    def _run(
        self,
        video_id: int,
        title: str,
        local_path: str,
        drive_filename: str,
        size_bytes: int,
        folder_id_drive: str,
        base_dir: str,
        videos_dir: str,
        min_free: int,
        app_config: Any,
        resumable_uri: Optional[str] = None,
    ) -> None:
        from . import db, drive, storage
        try:
            drive_id = drive.upload_file(
                local_path,
                title,
                folder_id_drive,
                drive_filename,
                progress_cb=lambda up, total: self._report(video_id, up, total),
                resumable_uri=resumable_uri,
                on_session=lambda uri: self._save_session(video_id, uri),
            )
            drive_path = f"drive://{folder_id_drive}/{drive_id}"
            db.update_video(
                video_id,
                google_drive_file_id=drive_id,
                drive_path=drive_path,
                drive_filename=drive_filename,
                status="ready",
                drive_resumable_uri=None,
            )
            logger.info("worker: video id=%s ready (drive_id=%s)", video_id, drive_id)
            # Free the local copy when the disk is tight (Drive now holds it).
            if storage.free_space_bytes(base_dir) < min_free and os.path.exists(local_path):
                try:
                    os.remove(local_path)
                    db.update_video(video_id, local_filename=None)
                except OSError as exc:  # noqa: BLE001 - best effort
                    logger.warning(
                        "worker: could not drop local copy for id=%s: %s", video_id, exc
                    )
            # Enforce the cache-capacity cap now that this video has a Drive copy.
            self._enforce_cap(videos_dir, base_dir, app_config)
        except Exception as exc:  # noqa: BLE001 - surface a friendly failure
            logger.error("worker: Drive upload failed for video id=%s: %s", video_id, exc)
            self._fail(video_id, str(exc))
        finally:
            with self._lock:
                self._in_flight.discard(video_id)

    def _enforce_cap(self, videos_dir: str, base_dir: str, app_config: Any) -> None:
        from . import storage
        try:
            storage.enforce_cache_limit(
                Path(videos_dir), base_dir, app_config, self.in_flight()
            )
        except Exception as exc:  # noqa: BLE001 - eviction is best effort
            logger.warning("worker: cache-cap enforce failed: %s", exc)

    def _report(self, video_id: int, uploaded: int, total: int) -> None:
        with self._lock:
            p = self._progress.get(video_id)
            if p is not None:
                p["uploaded_bytes"] = uploaded
                p["total_bytes"] = total
                now = time.monotonic()
                samples = p.setdefault("samples", deque())
                samples.append((now, uploaded))
                # Drop samples that fall out of the speed window.
                while samples and now - samples[0][0] > self._SPEED_WINDOW_SECONDS:
                    samples.popleft()

    @staticmethod
    def _speed(samples: Any) -> Optional[float]:
        """Transfer speed (bytes/s) over the sample window, or None if unknown.

        Needs two samples at least 1 s apart, so a fresh (or stalled) upload
        reports ``None`` until real progress is measured.
        """
        if not samples or len(samples) < 2:
            return None
        first_ts, first_bytes = samples[0]
        last_ts, last_bytes = samples[-1]
        span = last_ts - first_ts
        if span < 1.0:
            return None
        return max(0.0, (last_bytes - first_bytes) / span)

    def _save_session(self, video_id: int, uri: str) -> None:
        from . import db
        try:
            db.update_video(video_id, drive_resumable_uri=uri)
        except Exception as exc:  # noqa: BLE001 - resume is best effort
            logger.warning(
                "worker: could not save resumable session for id=%s: %s", video_id, exc
            )

    def _fail(self, video_id: int, error: str) -> None:
        from . import db
        with self._lock:
            p = self._progress.get(video_id)
            if p is not None:
                p["error"] = error
        try:
            db.update_video(video_id, status="failed")
        except Exception as exc:  # noqa: BLE001 - last-resort DB write
            logger.error("worker: could not mark video id=%s failed: %s", video_id, exc)


# Process-wide singleton shared by the routes, ``__init__``, and the worker thread.
worker = DriveUploadWorker()
