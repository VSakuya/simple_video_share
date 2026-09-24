"""Admin blueprint: manage all videos/users + global settings (P6)."""

import logging
from typing import Any

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.security import generate_password_hash

from .. import db, storage
from ..auth import admin_required, current_user

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")
logger = logging.getLogger("simple_video_share.routes.admin")

#: Setting keys the admin settings form may write (whitelist). Everything else
#: is ignored so the form can never clobber an unknown/internal key.
_EDITABLE_SETTINGS = (
    "default_bitrate",
    "default_codec",
    "fallback_codec",
    "max_resolution",
    "max_fps",
    "min_free_space_bytes",
    "max_cache_mb",
)


@admin_bp.route("/")
@admin_required
def index() -> str:
    videos = db.list_all_videos()
    users = db.list_users()
    settings = db.get_all_settings()
    return render_template(
        "admin.html",
        videos=videos,
        users=users,
        settings=settings,
        cached_bytes=db.sum_cached_bytes(),
        cache_cap_bytes=storage.max_cache_bytes(current_app.config),
    )


@admin_bp.route("/settings", methods=["POST"])
@admin_required
def save_settings() -> Any:
    """Persist the editable settings from the admin form."""
    for key in _EDITABLE_SETTINGS:
        raw = request.form.get(key)
        if raw is None:
            continue
        value = str(raw).strip()
        if key in ("default_bitrate", "max_resolution", "max_fps", "min_free_space_bytes", "max_cache_mb"):
            try:
                int(value)
            except (TypeError, ValueError):
                flash(f"'{key}' must be a number.", "error")
                continue
        if key == "min_free_space_bytes":
            # The form edits the threshold in MB; persist it in bytes.
            value = str(int(value) * 1024 * 1024)
        db.set_setting(key, value)
    flash("Settings saved.", "success")
    return redirect(url_for("admin.index"))


@admin_bp.route("/users", methods=["POST"])
@admin_required
def create_user() -> Any:
    """Admin creates a new user — the only way to add users after bootstrap."""
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password", "")
    is_admin = 1 if request.form.get("is_admin") else 0
    if not username or not password:
        flash("Username and password are required.", "error")
    elif len(password) < 6:
        flash("Password must be at least 6 characters.", "error")
    elif db.get_user_by_username(username):
        flash("Username already taken.", "error")
    else:
        db.create_user(username, generate_password_hash(password), is_admin=is_admin)
        flash(f"Created user '{username}'.", "success")
    return redirect(url_for("admin.index"))


@admin_bp.route("/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id: int) -> Any:
    """Admin deletes a user. Self-deletion is not allowed."""
    me = current_user()
    if me is not None and user_id == me["id"]:
        flash("You cannot delete your own account.", "error")
    else:
        db.delete_user(user_id)
        flash("User deleted.", "success")
    return redirect(url_for("admin.index"))
