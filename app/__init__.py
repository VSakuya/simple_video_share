"""Application factory for the video-share app.

Layout
------
- ``app/``          -> all Python source (this package) plus ``sql/``, ``templates/``, ``static/``
- ``storage/``      -> runtime data: ``videos/``, ``covers/``, ``avatars/``, ``data/app.db``
- ``credentials/``  -> Google Drive OAuth client (managed at runtime, read-only)

``create_app`` builds the Flask app, loads configuration, initialises the
database, and registers all route blueprints.
"""

import hashlib
import mimetypes
from datetime import timedelta
from pathlib import Path
from typing import Any

from flask import Flask, redirect, request, url_for

from . import config as app_config
from . import db

# Register the ES-module extension with a JavaScript MIME type. Python's
# stdlib ``mimetypes`` maps ".mjs" -> "text/plain", and Flask/Werkzeug serves
# static files via ``mimetypes.guess_type``. Browsers enforce strict MIME
# checking for ES module scripts, so a "text/plain" response makes the browser
# refuse to load the same-origin ``mediabunny.min.mjs`` bundle in upload.js.
mimetypes.add_type("application/javascript", ".mjs")

# Directory anchors (resolve to absolute paths, independent of CWD).
_APP_DIR = Path(__file__).resolve().parent      # .../app
_ROOT_DIR = _APP_DIR.parent                     # project root
_STORAGE_DIR = _ROOT_DIR / "storage"            # runtime data


def create_app() -> Flask:
    """Build and configure the Flask application."""
    from . import log as app_log

    app_logger = app_log.setup_logging()
    app_logger.info("create_app: loading config from %s", app_config.CONFIG_PATH)
    cfg = app_config.load_config()
    videos_dir = _STORAGE_DIR / "videos"
    covers_dir = _STORAGE_DIR / "covers"
    avatars_dir = _STORAGE_DIR / "avatars"
    for d in (_STORAGE_DIR, videos_dir, covers_dir, avatars_dir):
        d.mkdir(parents=True, exist_ok=True)

    app = Flask(
        __name__,
        template_folder=str(_APP_DIR / "templates"),
        static_folder=str(_APP_DIR / "static"),
    )
    app.config["SECRET_KEY"] = cfg["secret_key"]
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
    # Store runtime paths on the app for use by blueprints.
    app.config["VIDEOS_DIR"] = videos_dir
    app.config["COVERS_DIR"] = covers_dir
    app.config["AVATARS_DIR"] = avatars_dir
    app.config["DRIVE_FOLDER_ID"] = cfg.get("drive_folder_id", "")

    # Subpath the app is mounted under behind a reverse proxy (e.g. "/video").
    # Normalize: strip trailing slashes, keep the leading slash. "" (or a lone
    # "/") means "serve from the domain root" and must stay empty — a truthy "/"
    # would leak to the client as SVS_BASE="/" and make hand-written fetch URLs
    # protocol-relative ("//…"), which the browser treats as a different host
    # (§13.29). Exposed to templates as ``svs_base``.
    base_path = str(cfg.get("base_path", "") or "").strip().rstrip("/")
    if base_path and not base_path.startswith("/"):
        base_path = "/" + base_path
    app.config["BASE_PATH"] = base_path

    # Cache-busting version for the app's own static assets (inpage.js and
    # style.css). A content hash computed once at startup lets a browser
    # re-fetch when a file changes, without manual version bumps. Exposed to
    # templates as ``static_version``.
    def _static_version() -> str:
        chunks = []
        for rel in ("static/js/inpage.js", "static/css/style.css"):
            p = _APP_DIR / rel
            if p.is_file():
                chunks.append(p.read_bytes())
        return hashlib.sha256(b"\0".join(chunks)).hexdigest()[:10]
    app.config["STATIC_VERSION"] = _static_version()


    # Initialise the database (idempotent) and ensure the bootstrap admin.
    db.init_db()
    db.ensure_default_admin()

    # Recover from a previous run: resume ``'uploading'`` videos (their local
    # files are still on disk, so the Drive upload continues in the background)
    # and enforce the admin cache-capacity cap on the cached files already on
    # disk.
    from . import drive_worker, storage
    drive_worker.worker.resume_incomplete(videos_dir, app.config)
    try:
        storage.enforce_cache_limit(
            videos_dir, str(videos_dir.parent), app.config,
            drive_worker.worker.in_flight(),
        )
    except Exception as exc:  # noqa: BLE001 - startup cleanup is best effort
        app_logger.warning("create_app: startup cache-cap enforce failed: %s", exc)

    # Register blueprints.
    from .auth import auth_bp
    from .routes.home import home_bp
    from .routes.live import live_bp
    from .routes.upload import upload_bp
    from .routes.watch import watch_bp
    from .routes.user import user_bp
    from .routes.admin import admin_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(home_bp)
    app.register_blueprint(live_bp)
    app.register_blueprint(upload_bp)
    app.register_blueprint(watch_bp)
    app.register_blueprint(user_bp)
    app.register_blueprint(admin_bp)
    app_logger.info("create_app: ready (%d blueprints registered)", len(app.blueprints))

    @app.template_filter("human_size")
    def _human_size(value: int | float) -> str:
        """Render a byte count in human-readable units (KB/MB/GB/TB)."""
        n = float(value)
        units = ("B", "KB", "MB", "GB", "TB")
        i = 0
        while n >= 1024 and i < len(units) - 1:
            n /= 1024
            i += 1
        if i == 0:
            return f"{int(n)} {units[i]}"
        return f"{n:.1f} {units[i]}"

    @app.template_filter("human_duration")
    def _human_duration(value: float | None) -> str:
        """Render a duration in seconds as m:ss (or h:mm:ss for an hour+)."""
        if value is None:
            return ""
        total = int(round(float(value)))
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    @app.template_filter("human_bitrate")
    def _human_bitrate(value: int | float | None) -> str:
        """Render a kbps bitrate as kbps, or Mbps when it reaches a thousand."""
        if value is None:
            return ""
        kbps = float(value)
        if kbps >= 1000:
            return f"{kbps / 1000:.1f} Mbps"
        return f"{kbps:.0f} kbps"

    @app.context_processor
    def inject_globals():
        from .auth import current_user
        user = current_user()
        return {
            "session_username": user["username"] if user else "",
            "user_is_admin": bool(user and user.get("is_admin")),
            "current_user_obj": user,
            # Subpath the app is mounted under behind a reverse proxy ("", or e.g.
            # "/video"). Lets templates build the few hand-written URLs without
            # url_for. url_for() itself already picks this up via SCRIPT_NAME.
            "svs_base": app.config["BASE_PATH"],
            # Cache-buster for the app's own static assets (see create_app).
            "static_version": app.config["STATIC_VERSION"],
        }

    @app.before_request
    def _enforce_forced_password_change() -> Any:
        """Bounce a must-change account to the change-password page until done.

        Static assets and the auth routes themselves are always reachable; every
        other page is blocked until ``must_change_password`` is cleared.
        """
        if request.path.startswith("/static"):
            return None
        from .auth import current_user
        user = current_user()
        if user is None or not user.get("must_change_password"):
            return None
        if request.path in ("/auth/change-password", "/auth/logout", "/auth/login"):
            return None
        return redirect(url_for("auth.change_password"))

    # When mounted under a subpath (e.g. "/video" behind an Apache reverse proxy),
    # tell Flask where it lives so url_for() and the /static handler emit
    # base-prefixed URLs. Apache strips the prefix, so PATH_INFO is already
    # app-relative; we just record the mount point as SCRIPT_NAME.
    if base_path:
        _wsgi_app = app.wsgi_app

        def _base_path_wsgi_app(environ, start_response):
            environ["SCRIPT_NAME"] = base_path
            return _wsgi_app(environ, start_response)

        app.wsgi_app = _base_path_wsgi_app

    return app
