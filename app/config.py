"""Deployment-level configuration loader (config.json).

Separation of concerns:
- ``config.json``  -> deployment parameters (host, port, Drive folder, secret key).
- SQLite ``settings`` -> runtime/admin parameters (bitrate, codec, caps, thresholds).
"""

import json
import secrets
from pathlib import Path
from typing import Any

# Anchor every path to the project root (the parent of the ``app`` package) so
# the app works no matter which directory it is launched from.
_ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = _ROOT_DIR / "config.json"
CONFIG_EXAMPLE_PATH = _ROOT_DIR / "config.example.json"

DEFAULTS: dict[str, Any] = {
    "host": "0.0.0.0",
    "port": 8080,
    "drive_folder_id": "",
    "secret_key": "",
    "debug": False,
    # Subpath the app is mounted under when behind a reverse proxy (e.g. "/video").
    # Empty string means "serve from the domain root" (local development).
    "base_path": "",
    # Watch-together: max seconds the group stays paused after a seek before
    # resuming even if a client is still landing. The group resumes as soon as
    # every connected client has confirmed arrival at the new point, capped at
    # this value (a slow server serving a large video can need several seconds
    # for a hard-seek). Must be a multiple of 0.5 (the watch tick).
    "seek_settle_seconds": 60.0,
    # Send the session cookie with the Secure flag (28.4). Keep it true: it is
    # correct for the HTTPS deployment, and browsers treat http://127.0.0.1 as a
    # secure context, so local development keeps working with it on. Flip it to
    # false only when testing over plain HTTP from another machine on the LAN.
    "cookie_secure": True,
    # HLS segmentation of cached videos (§31, §32). hls_enabled is the master switch:
    # it gates BOTH the ffmpeg build and offering HLS to a player, so a deploy with
    # it off changes nothing at all. segment_seconds is the target segment length
    # (ffmpeg snaps it to the source GOP). keep_source false means the cache keeps
    # only the segment tree once a stream validates - measured equal in size to the
    # MP4 (§30.2), so keeping both is what doubles the footprint. The repair knobs
    # are the CPU-bound GOP fix an admin can trigger per video (0.65x realtime at
    # ultrafast on the VPS); timeout_seconds is a hard kill for a runaway ffmpeg.
    "hls_enabled": False,
    "hls_repair_enabled": True,
    "hls_segment_seconds": 6,
    "hls_keep_source": False,
    "hls_timeout_seconds": 600,
    "hls_repair_timeout_seconds": 7200,
    "hls_repair_preset": "ultrafast",
    "hls_repair_crf": 26,
    # Empty string means "find ffmpeg on PATH". An absolute path pins the binary.
    "ffmpeg_path": "",
}


def _generate_secret_key() -> str:
    return secrets.token_hex(32)


def load_config() -> dict[str, Any]:
    """Read config.json. If missing, generate a template from the example and
    create a fresh secret key. Always returns a dict with all DEFAULTS present."""
    cfg: dict[str, Any] = dict(DEFAULTS)

    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        cfg.update(data)
    else:
        # Seed from the example template if it exists, else from DEFAULTS.
        if CONFIG_EXAMPLE_PATH.exists():
            with CONFIG_EXAMPLE_PATH.open("r", encoding="utf-8") as fh:
                cfg.update(json.load(fh))
        # Persist a real config so subsequent runs are stable.
        _save_config(cfg)

    # Generate a secret key if it is still a placeholder.
    if not cfg.get("secret_key") or cfg["secret_key"] in (
        "change-me-on-first-run",
        "generate-on-first-run",
    ):
        cfg["secret_key"] = _generate_secret_key()
        _save_config(cfg)

    return cfg


def _save_config(cfg: dict[str, Any]) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
