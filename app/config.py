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
