"""Central logging for the Simple Video Share app.

Two rotating log files keep the whole upload pipeline auditable end to end:

- ``storage/logs/app.log``    -> server-side activity (requests, Drive, space,
  eviction). Every line also goes to the server console (stderr).
- ``storage/logs/client.log`` -> browser pipeline logs POSTed from ``upload.js``
  (probe, transcode decision, engine attempts, cover, upload).

The point is that a browser-side failure (e.g. a transcode that fell through to
passthrough) can be diagnosed from these files without reopening DevTools.
"""

import logging
import logging.handlers
from pathlib import Path
from typing import Iterable, Optional

# Anchor to the project root so the paths work from any working directory.
_APP_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _APP_DIR.parent
_LOG_DIR = _ROOT_DIR / "storage" / "logs"
#: Public handle to the log directory (used by the admin log viewer).
LOG_DIR = _LOG_DIR

_LOG_FORMAT = "%(asctime)s  %(levelname)-7s  %(name)s: %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
_MAX_BYTES = 5 * 1024 * 1024  # rotate each file at 5 MB
_BACKUPS = 3

#: Set once setup_logging() has wired the handlers (idempotency guard).
_LOGGING_READY = False


def _file_handler(path: Path) -> logging.Handler:
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
    return handler


def setup_logging() -> logging.Logger:
    """Configure the application logger. Idempotent (safe to call twice)."""
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    global _LOGGING_READY
    app_logger = logging.getLogger("simple_video_share")
    if _LOGGING_READY:
        return app_logger

    app_logger.setLevel(logging.INFO)
    app_logger.propagate = False
    app_logger.addHandler(_file_handler(_LOG_DIR / "app.log"))

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
    app_logger.addHandler(console)

    # A dedicated file for the browser pipeline logs. Records also bubble up to
    # the parent, so app.log holds the full picture (server + client) as well.
    client_logger = logging.getLogger("simple_video_share.client")
    client_logger.addHandler(_file_handler(_LOG_DIR / "client.log"))

    _LOGGING_READY = True
    app_logger.info("logging ready (log dir: %s)", _LOG_DIR)
    return app_logger


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Return a child logger under ``simple_video_share`` (or the base logger)."""
    base = logging.getLogger("simple_video_share")
    return base.getChild(name) if name else base


def log_client(lines: Iterable[str]) -> None:
    """Persist already-timestamped browser pipeline log lines to client.log."""
    client = logging.getLogger("simple_video_share.client")
    for line in lines:
        client.info("%s", str(line))
