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
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from flask import Flask, redirect, request, session, url_for
from markupsafe import Markup

from . import config as app_config
from . import csrf as app_csrf
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
    # Session cookie hardening (28.4). These flags were never set explicitly, so
    # SameSite was whatever the browser defaulted to. ``cookie_secure`` comes from
    # config.json and defaults to True; browsers treat http://127.0.0.1 as a
    # secure context, so plain-HTTP local development still gets the session
    # cookie. Set it to false only when testing over plain HTTP on a LAN address.
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "lax"
    app.config["SESSION_COOKIE_SECURE"] = bool(cfg.get("cookie_secure", True))
    # Store runtime paths on the app for use by blueprints.
    app.config["VIDEOS_DIR"] = videos_dir
    app.config["COVERS_DIR"] = covers_dir
    app.config["AVATARS_DIR"] = avatars_dir
    # Multipart parsing precedes route-level space checks. Keep its temporary
    # files on the storage disk instead of a small/quota-limited system tmpfs.
    from .upload_request import UploadRequest
    app.request_class = UploadRequest
    app.config["UPLOAD_TEMP_DIR"] = _STORAGE_DIR / "tmp" / "uploads"

    @app.errorhandler(OSError)
    def upload_storage_error(error):
        import errno
        from flask import jsonify
        if request.path.startswith('/upload/') and error.errno in (errno.ENOSPC, errno.EDQUOT):
            app_logger.error('Upload temporary/storage space unavailable: %s', error)
            return jsonify(ok=False, error='Upload storage is full or its disk quota is exceeded. Free server storage before retrying.'), 507
        raise error
    app.config["DRIVE_FOLDER_ID"] = cfg.get("drive_folder_id", "")
    # Watch-together max seek wait (§17.4): max seconds the group stays paused
    # after a seek before resuming even if a client is still landing. The group
    # resumes as soon as every connected client confirms arrival, capped here.
    app.config["SEEK_SETTLE_SECONDS"] = float(cfg.get("seek_settle_seconds", 60.0))
    # HLS segmentation of the local cache (§31/§32). One dict so hls.py reads the
    # feature config in one place; every key falls back to config.py DEFAULTS.
    app.config["HLS"] = {
        "enabled": bool(cfg.get("hls_enabled", False)),
        "repair_enabled": bool(cfg.get("hls_repair_enabled", True)),
        "segment_seconds": float(cfg.get("hls_segment_seconds", 6)),
        "keep_source": bool(cfg.get("hls_keep_source", False)),
        "timeout_seconds": int(cfg.get("hls_timeout_seconds", 600)),
        "repair_timeout_seconds": int(cfg.get("hls_repair_timeout_seconds", 7200)),
        "repair_preset": str(cfg.get("hls_repair_preset", "ultrafast")),
        "repair_crf": int(cfg.get("hls_repair_crf", 26)),
        "ffmpeg_path": str(cfg.get("ffmpeg_path", "") or ""),
    }

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
    for key in ("services_origin_template", "services_parent_origin"):
        app.config[key] = cfg[key]

    # Cache-busting version for shared and live-player scripts plus style.css.
    # A content hash computed once at startup lets a browser
    # re-fetch when a file changes, without manual version bumps. Exposed to
    # templates as ``static_version``.
    def _static_version() -> str:
        chunks = []
        for rel in (
            "static/js/inpage.js", "static/js/csrf.js", "static/js/notify.js",
            "static/js/live-player.js", "static/css/style.css",
            # The upload pipeline scripts joined the hash list (30.1): their encoder
            # settings are part of what a client must not keep cached.
            "static/js/edit.js", "static/js/upload.js", "static/js/pipeline.js",
            "static/vendor/mpegts/mpegts.min.js",
            # 31.6/31.10: the HLS picker and the vendored hls.js it depends on.
            "static/js/stream-player.js", "static/vendor/hls/hls.min.js",
        ):
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
    from . import drive_worker, storage, hls
    hls.recover_jobs(videos_dir)
    from .routes.upload import cleanup_segments
    cleanup_segments(videos_dir)
    drive_worker.worker.resume_incomplete(videos_dir, app.config)
    from . import repair_sync
    repair_sync.resume_pending(videos_dir, app.config)
    try:
        storage.enforce_cache_limit(
            videos_dir, str(videos_dir.parent), app.config,
            drive_worker.worker.in_flight(),
        )
    except Exception as exc:  # noqa: BLE001 - startup cleanup is best effort
        app_logger.warning("create_app: startup cache-cap enforce failed: %s", exc)

    # The ffmpeg.wasm Stage-2 fallback asset is deliberately out of git (28.7):
    # 32 MB in every clone and in every boot-time git pull, while mediabunny
    # (WebCodecs) is the primary engine. Missing means the fallback fails, not
    # the upload. See README ("Run") for the one-time copy.
    if not (_APP_DIR / "static" / "js" / "ffmpeg" / "ffmpeg-core.wasm").is_file():
        app_logger.warning(
            "create_app: ffmpeg.wasm fallback asset missing "
            "(static/js/ffmpeg/ffmpeg-core.wasm). mediabunny transcode still "
            "works; the ffmpeg.wasm fallback will fail. See README (Run).",
        )

    # Room presence, upload progress and the watch scheduler live in ONE process,
    # so a second live instance sharing storage/ would silently break them (28.8).
    # Advisory: a failed check never blocks startup.
    from . import runtime_guard
    for warning in runtime_guard.check(_STORAGE_DIR):
        app_logger.critical("create_app: %s", warning)

    # Register blueprints.
    from .auth import auth_bp
    from .routes.home import home_bp
    from .routes.live import live_bp
    from .routes.upload import upload_bp
    from .routes.watch import watch_bp
    from .routes.together import together_bp
    from .routes.user import user_bp
    from .routes.admin import admin_bp
    from .routes.notify import notify_bp, notify_page_bp
    from .routes.services import services_bp, admin_services_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(home_bp)
    app.register_blueprint(live_bp)
    app.register_blueprint(upload_bp)
    app.register_blueprint(watch_bp)
    app.register_blueprint(together_bp)
    app.register_blueprint(user_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(notify_bp)
    app.register_blueprint(notify_page_bp)
    app.register_blueprint(services_bp)
    app.register_blueprint(admin_services_bp)
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

    @app.template_filter("tojson_attr")
    def _tojson_attr(value: Any) -> "Markup":
        """Serialize ``value`` to JSON and escape it for use inside a
        double-quoted HTML attribute (e.g. ``data-tags``).

        Returns a ``Markup`` (treated as already-safe) so the browser's
        ``getAttribute`` -> ``JSON.parse`` round-trips cleanly. ``ensure_ascii``
        keeps the JSON pure-ASCII, and ``html.escape(..., quote=True)`` turns
        ``"`` into ``&quot;`` and any ``&`` into ``&amp;`` with single escaping.
        Use as ``data-...="{{ value | tojson_attr }}"`` (no ``| safe`` needed).
        """
        import json
        import html
        payload = json.dumps(value, ensure_ascii=True)
        return Markup(html.escape(payload, quote=True))


    @app.context_processor
    def inject_globals():
        from .auth import current_user
        user = current_user()
        return {
            # CSRF (28.1/28.2): ``csrf_field`` is a callable used as
            # {{ csrf_field() }} inside every POST form; ``csrf_token`` and
            # ``csrf_meta_name`` are values, used in base.html as
            # <meta name="{{ csrf_meta_name }}" content="{{ csrf_token }}">.
            "csrf_token": app_csrf.get_token(),
            "csrf_field": app_csrf.csrf_field,
            "csrf_meta_name": app_csrf.META_NAME,
            "session_username": user["username"] if user else "",
            "user_is_admin": bool(user and user.get("is_admin")),
            "current_user_obj": user,
            # Subpath the app is mounted under behind a reverse proxy ("", or e.g.
            # "/video"). Lets templates build the few hand-written URLs without
            # url_for. url_for() itself already picks this up via SCRIPT_NAME.
            "svs_base": app.config["BASE_PATH"],
            # Cache-buster for the app's own static assets (see create_app).
            "static_version": app.config["STATIC_VERSION"],
            # Unread notification count baked at render time (§21): base.html
            # bakes the red-dot state into the sidebar user chip from it. One
            # cheap indexed query per page render — the same trade as the
            # presence snapshots baked on the live list.
            "unread_notifications": db.unread_count(user["id"]) if user else 0,
        }

    # Registered first: a request rejected for a missing token should cost no
    # session-driven DB work (the two hooks below each read the session, and one
    # of them writes).
    app.before_request(app_csrf.protect)

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

    @app.before_request
    def _track_last_seen() -> Any:
        """Record the user's most recent visit so the admin "Last seen" column
        reflects activity, not just the most recent login. Throttled to at most
        one DB write per 60 s per session (checked via the session, so a
        request only costs a session read until the throttle lapses).
        """
        if request.path.startswith("/static"):
            return None
        from .auth import current_user
        user = current_user()
        if user is None:
            return None
        now = time.time()
        last = session.get("last_seen_db")
        if last is not None and now - last < 60:
            return None
        db.update_user(
            user["id"],
            last_login_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        )
        session["last_seen_db"] = now
        return None

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

