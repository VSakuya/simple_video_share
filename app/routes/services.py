"""Website-side service management, launcher, and one-time gateway handoff."""
import secrets
import sqlite3

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for

from .. import services
from ..auth import admin_required, current_user, login_required

services_bp = Blueprint("services", __name__, url_prefix="/services")
admin_services_bp = Blueprint("admin_services", __name__, url_prefix="/admin/services")


@admin_services_bp.route("/", methods=["GET", "POST"])
@admin_required
def manage():
    if request.method == "POST":
        try:
            slug_before = request.form.get("original_slug", "")
            existing = services.get_service(slug_before) if slug_before else None
            if slug_before and not existing:
                abort(404)
            if request.form.get("action") == "delete":
                if not existing:
                    abort(404)
                services.delete_service(existing["id"])
                flash("Service deleted.", "success")
            else:
                values = services.parse_service(request.form.get("name", ""), request.form.get("slug", ""), request.form.get("address", ""))
                origin = services.service_origin(current_app.config, values[1])
                if origin == values[2]:
                    raise ValueError("Service address cannot point to its own proxy.")
                services.save_service(values, request.form.get("enabled") == "1", existing["id"] if existing else None)
                flash("Service saved.", "success")
        except (ValueError, UnicodeError) as exc:
            flash(str(exc), "error")
        except sqlite3.IntegrityError:
            flash("That service identifier is already in use.", "error")
        return redirect(url_for("admin_services.manage"))
    return render_template("admin_services.html", services=services.list_services())


@services_bp.route("/")
@login_required
def index():
    return render_template("services.html", services=[s for s in services.list_services() if s["enabled"]])


@services_bp.route("/<slug>/")
@login_required
def view(slug):
    service = services.get_service(slug)
    if not service or not service["enabled"]:
        abort(404)
    return render_template("service_view.html", service=service)


@services_bp.route("/<slug>/launch", methods=["POST"])
@login_required
def launch(slug):
    service = services.get_service(slug)
    if not service or not service["enabled"]:
        abort(404)
    login_key = session.get("service_login_key")
    if not login_key:
        login_key = secrets.token_urlsafe(32)
        session["service_login_key"] = login_key
    code = services.issue_code(current_user(), service, login_key)
    response = current_app.make_response(render_template(
        "service_handoff.html", code=code,
        action=services.service_origin(current_app.config, slug) + "/_svs/authorize",
    ))
    response.headers["Cache-Control"] = "no-store"
    # Preserve the parent Origin on the cross-origin handoff POST. The code is
    # in the body, so only the website origin can be disclosed as a referrer.
    response.headers["Referrer-Policy"] = "strict-origin"
    return response
