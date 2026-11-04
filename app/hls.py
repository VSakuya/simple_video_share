"""HLS segmentation for locally cached videos (§31, §32).

The immutable source of truth is the single MP4 stored in Google Drive. Every byte
this module writes under ``storage/videos/`` is a **derived, regenerable cache**:

    storage/videos/<hash>/stream/master.m3u8    media playlist (EXT-X-MAP -> init.mp4)
    storage/videos/<hash>/stream/init.mp4       ~0.8 kB
    storage/videos/<hash>/stream/seg-000.m4s    fMP4 segment, ``hls_segment_seconds`` long

Two modes, one helper:

- ``copy`` - ``-c copy`` remux only. Measured on the VPS: 1 s of wall time per 30 s
  of video, and the segment set costs +0.03 % over the source. ffmpeg can only cut
  at keyframes, so a segment is always a multiple of the source GOP: the 2 s GOP
  that the browser pipeline now encodes (§30.1) yields exact 6.000 s segments,
  while the pre-30.1 library (GOP 133-167 s) yields one ~150 s segment - valid
  HLS, useless for seeking.
- ``repair`` - re-encode to H.264 with a short GOP, then segment that copy-only.
  Admin-triggered only (§32.2). Measured on the 1.0 GB VPS library file:
  0.65x realtime with ``-preset ultrafast`` (~30 min), 63 % of the source size,
  keyframes every 1.000 s, and the repaired file then segments in under a second.
  ``libsvtav1`` was measured too and the OOM killer ended it, so AV1 re-encoding
  on a 956 MiB box is out.

Invariants this module is responsible for:

- ffmpeg is invoked with an argv **list** (never a shell), fixed flags, a hard
  timeout, ``nice -19`` and ``-threads 1`` on the CPU-bound path, and only from a
  worker thread or an admin action - never from a request handler.
- A stream is built under ``<stream>.tmp/`` and renamed only after validation, so
  a half-built tree is never reported as ``ready``.
- The Drive object and the DB row are never modified here. Dropping the local MP4
  requires a Drive copy to exist.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Iterator, Optional

from . import db

logger = logging.getLogger("simple_video_share.hls")

STREAM_DIR_NAME = "stream"
TMP_SUFFIX = ".tmp"
PLAYLIST_NAME = "master.m3u8"
INIT_NAME = "init.mp4"
REPAIRED_NAME = "repaired.mp4"

#: ``videos.stream_state`` values.
STATE_NONE = "none"
STATE_BUILDING = "building"
STATE_REPAIRING = "repairing"
STATE_READY = "ready"
STATE_FAILED = "failed"

_SEGMENT_RE = re.compile(r"^seg-[0-9]{3,6}\.m4s$")
_EXTINF_RE = re.compile(r"^#EXTINF:([0-9.]+),")
#: An EXT-X-MAP URI must stay a plain file name inside the stream directory.
_INIT_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.mp4$")

#: Mode names accepted by :func:`ensure_stream`.
MODE_COPY = "copy"
MODE_REPAIR = "repair"

#: Fallback frame rate when the row has none, used only to size the repaired GOP.
_DEFAULT_FPS = 30.0

#: A copy-only cut can only break on keyframes that already exist. When the
#: longest segment is this much longer than the target, the source keyframe
#: interval itself is longer than the target (the real library measured
#: 133-167 s), so the stream would be one giant unseekable blob. Those trees
#: are thrown away and the MP4 is kept for an admin repair (32.5).
_GOP_BOUND_RATIO = 1.25

_locks_guard = threading.Lock()
_video_locks: dict[int, threading.Lock] = {}
#: Only one CPU-bound repair may run at a time on a 1 vCPU server.
_repair_slot = threading.Lock()


class StreamError(RuntimeError):
    """A stream build failed. The reason is user-presentable and logged."""


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------
def settings(app_config: Any) -> dict[str, Any]:
    """HLS settings from ``app.config["HLS"]`` with sane fallbacks (§31.8)."""
    raw = dict(app_config.get("HLS") or {})
    try:
        segment = float(raw.get("segment_seconds", 6))
    except (TypeError, ValueError):
        segment = 6.0
    return {
        # Master switch: gates building AND offering HLS to a player, so a deploy
        # with the flag absent does nothing at all.
        "enabled": bool(raw.get("enabled", False)),
        "repair_enabled": bool(raw.get("repair_enabled", True)),
        "segment_seconds": max(1.0, min(60.0, segment)),
        "keep_source": bool(raw.get("keep_source", False)),
        "timeout_seconds": int(raw.get("timeout_seconds", 600)),
        "repair_timeout_seconds": int(raw.get("repair_timeout_seconds", 7200)),
        "repair_preset": str(raw.get("repair_preset") or "ultrafast"),
        "repair_crf": int(raw.get("repair_crf", 26)),
        "ffmpeg_path": str(raw.get("ffmpeg_path") or ""),
    }


def ffmpeg_binary(cfg: dict[str, Any]) -> Optional[str]:
    """Configured ffmpeg path, else ``ffmpeg`` from PATH, else None."""
    return cfg["ffmpeg_path"] or shutil.which("ffmpeg")


def can_build(app_config: Any, mode: str = MODE_COPY) -> bool:
    """True when a build of ``mode`` is allowed right now (config + ffmpeg)."""
    cfg = settings(app_config)
    if not cfg["enabled"]:
        return False
    if mode == MODE_REPAIR and not cfg["repair_enabled"]:
        return False
    return ffmpeg_binary(cfg) is not None


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------
def stream_dir_for(videos_dir: Path | str, video: dict[str, Any]) -> Optional[Path]:
    """Locate the stream tree for ``video``.

    Preference order: the stored ``stream_dir`` (this is what keeps the tree
    findable after ``hls_keep_source`` drops the MP4 and clears
    ``local_filename``), then a path derived from ``local_filename`` - a
    foldered name ``<hash>/<hash>.mp4`` puts the tree at ``videos/<hash>/stream``,
    a legacy flat name ``<hash>.mp4`` at ``videos/<hash>.stream``.
    """
    base = Path(videos_dir)
    stored = video.get("stream_dir")
    if stored:
        return base / str(stored)
    local = str(video.get("local_filename") or "")
    if not local:
        return None
    normalized = Path(local.replace("\\", "/"))
    if normalized.parent != Path("."):
        return base / normalized.parent / STREAM_DIR_NAME
    return base / (normalized.stem + ".stream")


def source_path_for(videos_dir: Path | str, video: dict[str, Any]) -> Optional[Path]:
    """The locally cached MP4 for ``video``, or None when it is not cached."""
    local = str(video.get("local_filename") or "")
    if not local:
        return None
    path = Path(videos_dir) / local
    return path if path.is_file() else None


def _relative(base: Path, path: Path) -> str:
    """POSIX-style path of ``path`` under ``base`` (stored in ``stream_dir``)."""
    return path.relative_to(base).as_posix()


def _video_lock(video_id: int) -> threading.Lock:
    with _locks_guard:
        return _video_locks.setdefault(video_id, threading.Lock())


# --------------------------------------------------------------------------
# validation and reading a built stream
# --------------------------------------------------------------------------
def validate_stream(stream_dir: Path, expected_duration: Optional[float]) -> tuple[bool, str]:
    """Check a built tree without running ffmpeg.

    The playlist must be a VOD-style media playlist that maps ``init.mp4``, list
    at least one segment, name only files that exist in the directory, and add up
    to (roughly) the duration the DB claims for the video. A standalone ``.m4s``
    cannot be probed on its own (§30.2), which is why validation works off the
    playlist.
    """
    playlist = stream_dir / PLAYLIST_NAME
    if not playlist.is_file():
        return False, "playlist missing"
    try:
        lines = playlist.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return False, f"playlist unreadable: {exc}"
    if not any(line.startswith("#EXTM3U") for line in lines):
        return False, "playlist is not an M3U8"
    if "#EXT-X-ENDLIST" not in lines:
        return False, "playlist has no ENDLIST (partial build?)"

    init_uri: Optional[str] = None
    names: list[str] = []
    total = 0.0
    for line in lines:
        if line.startswith("#EXT-X-MAP:"):
            match = re.search(r'URI="([^"]+)"', line)
            init_uri = match.group(1) if match else None
            continue
        if line.startswith("#"):
            continue
        stripped = line.strip()
        if not stripped:
            continue
        names.append(stripped)
    for line in lines:
        match = _EXTINF_RE.match(line)
        if match:
            total += float(match.group(1))

    if init_uri is None:
        return False, "playlist has no EXT-X-MAP (init segment)"
    if not _INIT_NAME_RE.match(init_uri) or ".." in init_uri:
        return False, f"unsafe init-segment URI: {init_uri}"
    if not (stream_dir / init_uri).is_file():
        return False, "init segment missing"
    if not names:
        return False, "playlist lists no segments"
    for name in names:
        if not _SEGMENT_RE.match(name):
            return False, f"unexpected segment name: {name}"
        if not (stream_dir / name).is_file():
            return False, f"segment missing: {name}"

    if expected_duration:
        # Segments are keyframe-snapped, so a short total is the real risk.
        tolerance = max(3.0, float(expected_duration) * 0.10)
        if total < float(expected_duration) - tolerance:
            return False, (
                f"playlist covers {total:.1f}s of a {expected_duration:.1f}s video"
            )
    return True, ""


def longest_segment_seconds(stream_dir: Path) -> float:
    """Longest EXTINF of a built playlist (0.0 when it cannot be read)."""
    try:
        lines = (stream_dir / PLAYLIST_NAME).read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0.0
    values = []
    for line in lines:
        match = _EXTINF_RE.match(line)
        if match:
            values.append(float(match.group(1)))
    return max(values) if values else 0.0


def playlist_init_name(stream_dir: Path) -> Optional[str]:
    """The init-segment file name mapped by EXT-X-MAP, or None.

    ffmpeg names it init.mp4 today, but the name is relative to the playlist,
    so it is read from the playlist instead of trusting INIT_NAME. A URI that
    could escape the stream directory returns None.
    """
    try:
        lines = (stream_dir / PLAYLIST_NAME).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        if not line.startswith("#EXT-X-MAP:"):
            continue
        match = re.search(r'URI="([^"]+)"', line)
        if not match:
            return None
        uri = match.group(1).strip()
        if not _INIT_NAME_RE.match(uri) or ".." in uri:
            return None
        return uri
    return None


def playlist_segment_names(stream_dir: Path) -> list[str]:
    """Segment file names in playlist order (used by the segment route)."""
    try:
        lines = (stream_dir / PLAYLIST_NAME).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    names = []
    for line in lines:
        if line.startswith("#") or not line.strip():
            continue
        name = line.strip()
        if _SEGMENT_RE.match(name):
            names.append(name)
    return names


def iter_stream_bytes(stream_dir: Path, chunk_size: int = 1 << 20) -> Iterator[bytes]:
    """Yield ``init.mp4`` then every segment in playlist order.

    Concatenating an fMP4 init segment with its media segments produces a valid
    fragmented MP4 (§31.7), which is how the download button keeps working when
    the source MP4 was dropped from the cache.
    """
    files: list[Path] = []
    init_name = playlist_init_name(stream_dir)
    if init_name and (stream_dir / init_name).is_file():
        files.append(stream_dir / init_name)
    for name in playlist_segment_names(stream_dir):
        path = stream_dir / name
        if path.is_file():
            files.append(path)
    for path in files:
        try:
            with path.open("rb") as fh:
                while True:
                    block = fh.read(chunk_size)
                    if not block:
                        break
                    yield block
        except OSError as exc:  # noqa: BLE001 - a torn download must not crash
            logger.warning("hls: could not read %s: %s", path, exc)
            return


def stream_bytes_length(stream_dir: Path) -> int:
    """Bytes a reassembled download contains: init + segments, not the playlist.

    Counting glob("*") would include master.m3u8, which iter_stream_bytes never
    emits, so the Content-Length advertised by the download route would not
    match the body it streams.
    """
    names: list[str] = []
    init_name = playlist_init_name(stream_dir)
    if init_name:
        names.append(init_name)
    names.extend(playlist_segment_names(stream_dir))
    total = 0
    for name in names:
        try:
            total += (stream_dir / name).stat().st_size
        except OSError:
            continue
    return total


# --------------------------------------------------------------------------
# ffmpeg invocation
# --------------------------------------------------------------------------
def _nice_prefix() -> list[str]:
    """Run ffmpeg below the interactive load on POSIX (no equivalent on Windows)."""
    if os.name == "posix" and shutil.which("nice"):
        return ["nice", "-n", "19"]
    return []


def _run_ffmpeg(
    argv: list[str], timeout: int, why: str, cwd: Optional[Path] = None
) -> None:
    """Run one ffmpeg job with a hard timeout; raise StreamError on failure.

    cwd is not cosmetic: the hls muxer writes the EXT-X-MAP init segment
    relative to the process working directory, not relative to the playlist.
    Measured on the dev box, an absolute playlist path put init.mp4 in the
    project root, so validate_stream rejected a build whose segments were all
    correct. Running inside the stream directory keeps every output together.
    """
    logger.info("hls: %s: %s", why, " ".join(argv))
    try:
        proc = subprocess.run(
            argv, timeout=timeout, capture_output=True, text=True, errors="replace",
            cwd=str(cwd) if cwd is not None else None,
        )
    except subprocess.TimeoutExpired as exc:
        raise StreamError(f"{why}: timed out after {timeout}s") from exc
    except OSError as exc:
        raise StreamError(f"{why}: could not run ffmpeg: {exc}") from exc
    if proc.returncode != 0:
        tail = ((proc.stderr or "") or (proc.stdout or "")).strip().splitlines()[-4:]
        raise StreamError(
            f"{why}: ffmpeg exit {proc.returncode}: {' | '.join(tail) or 'no output'}"
        )


def _copy_argv(ffmpeg: str, source: Path, playlist: Path, cfg: dict[str, Any]) -> list[str]:
    """Copy-only fMP4/HLS remux (measured +0.03 % over the source, 1 s per 30 s).

    The playlist and segment names are relative because the caller runs ffmpeg
    with cwd set to their directory; the muxer derives the EXT-X-MAP init
    segment from the process CWD rather than from the playlist path.
    """
    seconds = int(round(cfg["segment_seconds"]))
    return [
        *_nice_prefix(),
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source),
        "-c", "copy",
        "-f", "hls",
        "-hls_time", str(seconds),
        "-hls_playlist_type", "vod",
        "-hls_segment_type", "fmp4",
        "-hls_flags", "independent_segments",
        "-hls_segment_filename", "seg-%03d.m4s",
        playlist.name,
    ]


def _repair_argv(
    ffmpeg: str, source: Path, target: Path, cfg: dict[str, Any], fps: float
) -> list[str]:
    """GOP repair: H.264 with a keyframe every ``segment_seconds`` of video.

    ``-g`` counts frames, so the GOP is ``round(segment_seconds * fps)`` (60 at
    60 fps measured exactly 1.000 s keyframes). ``-sc_threshold 0`` plus
    ``-keyint_min`` is what makes the spacing uniform - without it x264 inserts
    extra keyframes on scene cuts. Audio is stream-copied (the library is AAC).
    ``-threads 1`` and ``nice -19`` keep a 1 vCPU server responsive.
    """
    gop = max(1, int(round(cfg["segment_seconds"] * fps)))
    return [
        *_nice_prefix(),
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source),
        "-c:v", "libx264",
        "-preset", cfg["repair_preset"],
        "-crf", str(cfg["repair_crf"]),
        "-g", str(gop),
        "-keyint_min", str(gop),
        "-sc_threshold", "0",
        "-threads", "1",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(target),
    ]


def _fps_of(video: dict[str, Any]) -> float:
    try:
        fps = float(video.get("fps") or 0.0)
    except (TypeError, ValueError):
        return _DEFAULT_FPS
    return fps if 1.0 <= fps <= 240.0 else _DEFAULT_FPS


# --------------------------------------------------------------------------
# the one public entry point
# --------------------------------------------------------------------------
def ensure_stream(
    videos_dir: Path | str,
    video: dict[str, Any],
    app_config: Any,
    *,
    mode: str = MODE_COPY,
    reason: str = "",
) -> str:
    """Build (or reuse) the HLS stream of one video. Returns the new state.

    Call from a worker thread or an admin action. Idempotent: a validated tree
    is left alone. When another job already owns this video the current state is
    returned untouched - two viewers must never spawn two ffmpegs (§31.1).
    """
    cfg = settings(app_config)
    video_id = int(video["id"])
    state_now = str(video.get("stream_state") or STATE_NONE)
    base = Path(videos_dir)

    if not cfg["enabled"]:
        return state_now
    if mode == MODE_REPAIR and not cfg["repair_enabled"]:
        logger.info("hls: id=%s repair requested but hls_repair_enabled is false", video_id)
        return state_now

    ffmpeg = ffmpeg_binary(cfg)
    if ffmpeg is None:
        logger.warning("hls: id=%s ffmpeg not found; stream not built", video_id)
        return state_now

    stream_dir = stream_dir_for(base, video)
    if stream_dir is None:
        # No local MP4 and no stored stream path: nothing to segment yet.
        return state_now

    if state_now == STATE_READY and validate_stream(
        stream_dir, video.get("duration")
    )[0]:
        built = str(video.get("stream_source") or MODE_COPY)
        if built == mode:
            return STATE_READY
        # 32.3: an admin asked for a different mode than what is cached - the
        # copy-only tree of a sparse-GOP video is 'ready' yet useless for seeking,
        # so a repair request must rebuild it instead of being skipped.
        logger.info(
            "hls: id=%s rebuilding a %s stream as %s", video_id, built, mode
        )

    source = source_path_for(base, video)
    if source is None:
        logger.info("hls: id=%s has no local MP4 to segment", video_id)
        return state_now

    busy = STATE_REPAIRING if mode == MODE_REPAIR else STATE_BUILDING
    lock = _video_lock(video_id)
    if not lock.acquire(blocking=False):
        logger.info("hls: id=%s already being built by another job", video_id)
        return state_now
    holding_repair_slot = False
    try:
        if mode == MODE_REPAIR:
            if not _repair_slot.acquire(blocking=False):
                logger.info("hls: id=%s deferred: another repair is running", video_id)
                return state_now
            holding_repair_slot = True

            # 32.4: a repair holds the source, repaired.mp4 and its segments at the
            # same time, so it asks for 1.35x the source ABOVE the free-space reserve
            # (measured output was 63 % of the source, 32.0). Refusing here is much
            # cheaper than dying 25 minutes into a 30-minute encode.
            from . import storage

            source_bytes = source.stat().st_size
            if not storage.ensure_space(
                str(base.parent), source_bytes, app_config, peak_factor=1.35,
            ):
                raise StreamError(
                    "repair refused: needs ~%d bytes of headroom above the reserve"
                    % int(source_bytes * 1.35)
                )

        db.update_video(video_id, stream_state=busy)
        tmp_dir = stream_dir.parent / (stream_dir.name + TMP_SUFFIX)
        _rmtree_quiet(tmp_dir)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        playlist = tmp_dir / PLAYLIST_NAME

        if mode == MODE_REPAIR:
            repaired = tmp_dir / REPAIRED_NAME
            timeout = cfg["repair_timeout_seconds"]
            _run_ffmpeg(
                _repair_argv(ffmpeg, source, repaired, cfg, _fps_of(video)),
                timeout,
                f"repair encode id={video_id} ({reason})",
                tmp_dir,
            )
            _run_ffmpeg(
                _copy_argv(ffmpeg, repaired, playlist, cfg),
                cfg["timeout_seconds"],
                f"segment repaired id={video_id} ({reason})",
                tmp_dir,
            )
        else:
            _run_ffmpeg(
                _copy_argv(ffmpeg, source, playlist, cfg),
                cfg["timeout_seconds"],
                f"segment id={video_id} ({reason})",
                tmp_dir,
            )

        ok, why = validate_stream(tmp_dir, video.get("duration"))
        if not ok:
            _rmtree_quiet(tmp_dir)
            db.update_video(video_id, stream_state=STATE_FAILED, stream_dir=None)
            logger.error("hls: id=%s stream rejected: %s", video_id, why)
            return STATE_FAILED

        longest = longest_segment_seconds(tmp_dir)
        target = float(cfg["segment_seconds"])
        if (
            mode == MODE_COPY
            and state_now != STATE_READY
            and longest > target * _GOP_BOUND_RATIO
        ):
            # 32.5: a sparse-GOP source produces a playlist that is technically
            # valid and useless to play - hls.js would buffer a segment as long
            # as the video with no seeking, while the cache holds a complete
            # duplicate of the MP4. Throwing it away keeps progressive playback
            # (the MP4 stays) and leaves an honest 'none' badge for Repair GOP.
            _rmtree_quiet(tmp_dir)
            db.update_video(
                video_id,
                stream_state=STATE_NONE,
                stream_dir=None,
                stream_source="",
            )
            logger.info(
                "hls: id=%s copy-only tree discarded: longest segment %.1fs against "
                "a %.0fs target means a keyframe interval of the same length; keeping "
                "the MP4 so an admin can run Repair GOP",
                video_id, longest, target,
            )
            return STATE_NONE

        if mode == MODE_REPAIR:
            # 32.1 step 3: repaired.mp4 is scaffolding. Keeping it would leave the
            # cache holding the segments AND a re-encode at 63 % of the source -
            # worse than the MP4 this whole path exists to replace.
            scaffold = tmp_dir / REPAIRED_NAME
            try:
                if scaffold.is_file():
                    scaffold.unlink()
            except OSError as exc:  # noqa: BLE001 - the stream itself is valid
                logger.warning(
                    "hls: id=%s could not delete %s: %s", video_id, scaffold, exc
                )

        if stream_dir.exists():
            _rmtree_quiet(stream_dir)
        tmp_dir.rename(stream_dir)
        db.update_video(
            video_id,
            stream_dir=_relative(base, stream_dir),
            stream_state=STATE_READY,
            stream_source=mode,
            stream_updated_at=_now_text(),
        )
        logger.info(
            "hls: id=%s stream ready (%s, %d segments, longest %.1fs, %d bytes, reason=%s)",
            video_id, mode, len(playlist_segment_names(stream_dir)), longest,
            stream_bytes_length(stream_dir), reason or "-",
        )
        if mode == MODE_REPAIR:
            # A repair is a re-encode, so its size follows hls_repair_crf rather
            # than the source size. Measured on the VPS: 63 % of the source at
            # crf 26. If a particular file comes out bigger than the MP4 it is
            # about to replace, say so in the log while the source is still there.
            built_bytes = stream_bytes_length(stream_dir)
            try:
                source_bytes = source.stat().st_size
            except OSError:
                source_bytes = 0
            if source_bytes and built_bytes > source_bytes * 1.2:
                logger.warning(
                    "hls: id=%s repaired segments are %d%% of the source it replaces "
                    "(%d vs %d bytes); a higher hls_repair_crf would make them smaller",
                    video_id, int(built_bytes * 100.0 / source_bytes),
                    built_bytes, source_bytes,
                )

        if cfg["keep_source"]:
            return STATE_READY
        _drop_local_source(base, db.get_video_by_id(video_id) or video, cfg, reason)
        return STATE_READY
    except StreamError as exc:
        logger.error("hls: id=%s build failed: %s", video_id, exc)
        _rmtree_quiet(stream_dir.parent / (stream_dir.name + TMP_SUFFIX))
        db.update_video(video_id, stream_state=STATE_FAILED)
        return STATE_FAILED
    finally:
        if holding_repair_slot:
            _repair_slot.release()
        lock.release()


def _drop_local_source(
    base: Path, video: dict[str, Any], cfg: dict[str, Any], reason: str
) -> None:
    """Delete the local MP4 once a validated stream exists (§31.1).

    Refuses unless the video has a Drive copy: the local MP4 must never be the
    only remaining copy of the original.
    """
    if not video.get("google_drive_file_id") or video.get("status") == "uploading":
        logger.info(
            "hls: id=%s keeps its local MP4 (no Drive copy yet); segments-only "
            "will apply after the mirror completes", video["id"],
        )
        return
    local = str(video.get("local_filename") or "")
    if not local:
        return
    path = base / local
    try:
        if path.is_file():
            path.unlink()
            logger.info(
                "hls: id=%s dropped local MP4 (%s) - cache is segments-only",
                video["id"], reason or "-",
            )
        # A folder that now holds nothing but the stream tree is left as-is; the
        # cache accounting walks directories, so an empty shell costs nothing.
        db.update_video(video["id"], local_filename=None)
    except OSError as exc:  # noqa: BLE001 - the stream is already usable
        logger.warning("hls: id=%s could not drop local MP4: %s", video["id"], exc)


def mark_stale(video_id: int, reason: str) -> None:
    """Invalidate a stream after its source was replaced or its tree evicted."""
    db.update_video(video_id, stream_state=STATE_NONE, stream_dir=None)
    logger.info("hls: id=%s stream invalidated (%s)", video_id, reason)


def start_build(
    videos_dir: Path | str,
    video: dict[str, Any],
    app_config: Any,
    *,
    mode: str = MODE_COPY,
    reason: str = "",
) -> bool:
    """Run :func:`ensure_stream` on a daemon thread; return True when started.

    Used by the admin actions so a 30-minute repair never blocks a request.
    ``ensure_stream`` owns the per-video lock, so a duplicate request is a no-op
    rather than a second ffmpeg.
    """

    if not can_build(app_config, mode):
        return False
    thread = threading.Thread(
        target=ensure_stream,
        args=(Path(videos_dir), video, app_config),
        kwargs={"mode": mode, "reason": reason},
        name=f"hls-{mode}-{video.get('id')}",
        daemon=True,
    )
    thread.start()
    return True


def _now_text() -> str:
    """UTC timestamp in the same text form SQLite's ``datetime('now')`` writes."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _rmtree_quiet(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def describe(video: dict[str, Any]) -> str:
    """Short human label for the admin table badge."""
    state = str(video.get("stream_state") or STATE_NONE)
    source = str(video.get("stream_source") or "")
    if state == STATE_READY:
        return f"ready ({source or MODE_COPY})"
    return state
