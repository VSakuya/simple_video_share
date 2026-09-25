"""Authentication blueprint: login, logout, and access guards.

Also exposes helpers used across other blueprints:
- ``current_user()``  -> the logged-in user dict or None
- ``login_required``  -> decorator that redirects to /login
- ``admin_required``  -> decorator that 403s non-admins
"""

import time
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Optional

from flask import (
    Blueprint,
    flash,
    redirect,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from . import db, storage

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def current_user() -> Optional[dict[str, Any]]:
    """Return the currently logged-in user (dict) or None.

    Called by the request guard, the route decorators and the context
    processor; each call is a single indexed lookup on ``users.id``.
    """
    user_id = session.get("user_id")
    if user_id is None:
        return None
    return db.get_user_by_id(user_id)


def _full_path(path: str) -> str:
    """Return the full mounted path for an app-relative ``path``.

    The reverse proxy strips the mount prefix (e.g. ``/video``), so ``request.path``
    is app-relative. The post-login redirect target must be the full mounted path,
    or the user lands on the domain root instead of ``/video``. ``request.script_root``
    is the recorded ``SCRIPT_NAME`` (empty when served from the root).
    """
    return request.script_root + path


def login_required(view: Callable) -> Callable:
    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if current_user() is None:
            return redirect(url_for("auth.login", next=_full_path(request.path)))
        return view(*args, **kwargs)
    return wrapper


def admin_required(view: Callable) -> Callable:
    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        user = current_user()
        if user is None:
            return redirect(url_for("auth.login", next=_full_path(request.path)))
        if not user.get("is_admin"):
            from flask import abort
            abort(403)
        return view(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

#: Seconds to pause on a failed login attempt (slows brute force).
LOGIN_FAIL_DELAY = 0.5


def _safe_next(default: str, value: str = "") -> str:
    """Return a safe local redirect target (rejects absolute / protocol-relative URLs)."""
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return default


@auth_bp.route("/login", methods=["GET", "POST"])
def login() -> Any:
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        ip = request.remote_addr or "unknown"
        next_url = _safe_next(url_for("home.index"), request.args.get("next", ""))

        # 1. Sliding-window lockout (per username AND per IP).
        remaining = db.login_lockout_seconds(username, ip)
        if remaining > 0:
            flash(
                f"Too many failed attempts. Try again in about "
                f"{max(1, remaining // 60)} minute(s).",
                "error",
            )
            return redirect(url_for("auth.login", next=next_url))

        # 2. Verify credentials.
        user = db.get_user_by_username(username)
        if not (user and check_password_hash(user["password_hash"], password)):
            db.record_failed_login(username, ip)
            time.sleep(LOGIN_FAIL_DELAY)
            flash("Invalid username or password.", "error")
            return redirect(url_for("auth.login", next=next_url))

        # 3. Success — clear failures, then apply the forced-change redirect.
        db.clear_failed_logins(username)
        session["user_id"] = user["id"]
        session.permanent = True
        flash(f"Logged in as {user['username']}.", "success")
        if user.get("must_change_password"):
            return redirect(url_for("auth.change_password"))
        return redirect(next_url)
    return _render(
        "login.html",
        next=_safe_next(url_for("home.index"), request.args.get("next", "")),
    )


@auth_bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password() -> Any:
    """Forced (or voluntary) password change.

    Reached automatically after logging in with an account whose
    ``must_change_password`` flag is set; the flag is cleared on success.
    """
    me = current_user()
    assert me is not None
    forced = bool(me.get("must_change_password"))
    if request.method == "POST":
        current_pw = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm = request.form.get("confirm", "")
        if not check_password_hash(me["password_hash"], current_pw):
            flash("Current password is incorrect.", "error")
        elif forced and new_password == current_pw:
            # Forced change (§13.11): the bootstrap admin/admin account was
            # being "changed" to admin again (browser autofill), which left
            # the default password in place. Block the no-op.
            flash("The new password must be different from the current one.", "error")
        elif len(new_password) < 6:
            flash("New password must be at least 6 characters.", "error")
        elif new_password != confirm:
            flash("Passwords do not match.", "error")
        else:
            db.update_user(
                me["id"],
                password_hash=generate_password_hash(new_password),
                must_change_password=0,
            )
            flash("Password updated.", "success")
            return redirect(url_for("home.index"))
    return _render("change_password.html", forced=forced)


@auth_bp.route("/logout")
def logout() -> Any:
    session.clear()
    flash("Logged out.", "info")
    return redirect(url_for("auth.login"))


@auth_bp.route("/extend")
def extend_session() -> Any:
    """Keep the session alive (called by the frontend periodically)."""
    session.permanent = True
    return "ok"


@auth_bp.route("/account", methods=["GET", "POST"])
@login_required
def account() -> Any:
    """Self-service: a user changes their own password."""
    me = current_user()
    assert me is not None
    if request.method == "POST":
        current_pw = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm = request.form.get("confirm", "")
        if not check_password_hash(me["password_hash"], current_pw):
            flash("Current password is incorrect.", "error")
            return redirect(url_for("auth.account"))
        changed = False
        if new_password:
            if new_password == current_pw:
                flash("New password is the same as the current one.", "error")
            elif len(new_password) < 6:
                flash("New password must be at least 6 characters.", "error")
            elif new_password != confirm:
                flash("Passwords do not match.", "error")
            else:
                db.update_user(
                    me["id"],
                    password_hash=generate_password_hash(new_password),
                )
                changed = True
        if changed:
            flash("Account updated.", "success")
        return redirect(url_for("auth.account"))
    return _render("account.html")


@auth_bp.route("/avatar", methods=["POST"])
@login_required
def avatar() -> Any:
    """Self-service: a user uploads their own avatar."""
    from flask import current_app
    me = current_user()
    assert me is not None
    f = request.files.get("avatar")
    if f is None:
        flash("No avatar file.", "error")
        return redirect(url_for("auth.account"))
    dest = Path(current_app.config["AVATARS_DIR"])
    dest.mkdir(parents=True, exist_ok=True)
    name = storage.save_avatar(f, dest)
    # Remove the previous avatar file (best effort) so UUID names don't orphan.
    old = me.get("avatar_filename")
    if old and old != name:
        old_path = dest / old
        if old_path.exists():
            try:
                old_path.unlink()
            except OSError:
                pass
    db.update_user(me["id"], avatar_filename=name)
    flash("Avatar updated.", "success")
    return redirect(url_for("auth.account"))


def _render(template: str, **context: Any) -> Any:
    from flask import render_template
    return render_template(template, **context)
