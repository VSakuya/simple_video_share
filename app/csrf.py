"""CSRF protection: one per-session token plus one request guard (no new dependency).

Contract
--------
- :func:`get_token` returns the session token, creating it with
  ``secrets.token_hex(32)`` on first use. Templates embed it as a hidden field
  through ``{{ csrf_field() }}``, and ``base.html`` also publishes it as
  ``<meta name="csrf-token">``.
- ``static/js/csrf.js`` reads that meta tag and attaches ``X-CSRF-Token`` to
  every same-origin unsafe ``fetch`` / ``XMLHttpRequest``, so individual call
  sites never have to remember it. Native (non-AJAX) form posts carry the
  hidden ``_csrf`` field instead, so they need nothing from the JS side.
- :func:`protect` is an app-level ``before_request`` hook: every unsafe method
  (POST / PUT / PATCH / DELETE) must present the token in either place. Safe
  methods are never checked, so page loads and the SSE streams are untouched.

The header is read *before* the form body: for the segmented upload a 64 MB
part would otherwise force a multipart parse in the guard before the route
parses it anyway (Werkzeug caches the parsed body once, so header-first costs
nothing on the ordinary endpoints either).

Rejections
----------
An AJAX request (``X-Requested-With: XMLHttpRequest``, or a JSON-only
``Accept``) gets a ``400`` JSON body; a plain form post gets a flash plus a
redirect back to the page it came from.

Test environment
----------------
Enforcement is skipped when ``app.config["TESTING"]`` is true. That flag marks a
test environment in Flask (it also makes the app re-raise errors) and the
regression probes drive POSTs through ``app.test_client()`` with no browser in
the loop. ``testing/probe_csrf.py`` is the probe that exercises this guard, so
the exemption is itself covered by a test.
"""

import hmac
import secrets
from typing import Any, Optional
from urllib.parse import urlsplit

from flask import current_app, flash, jsonify, redirect, request, session, url_for
from markupsafe import Markup

from . import log

#: Session key holding the token.
TOKEN_SESSION_KEY = "_csrf"
#: Form field the guard reads.
FIELD_NAME = "_csrf"
#: Header the guard reads (attached by ``static/js/csrf.js``).
HEADER_NAME = "X-CSRF-Token"
#: ``<meta name=...>`` the token is published in, read by ``csrf.js``.
META_NAME = "csrf-token"
#: Methods that can change state, and therefore must carry the token.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_MESSAGE = "Session security check failed. Reload the page and try again."


def get_token() -> str:
    """Return this session's CSRF token, creating it on first use."""
    token = session.get(TOKEN_SESSION_KEY)
    if not token:
        token = secrets.token_hex(32)
        session[TOKEN_SESSION_KEY] = token
    return token


def csrf_field() -> Markup:
    """Render the hidden input that every POST form must carry.

    Use as ``{{ csrf_field() }}`` directly inside a ``<form method="post">``;
    the return value is ``Markup`` (already escaped) so no ``| safe`` is needed.
    """
    return Markup(
        '<input type="hidden" name="%s" value="%s">' % (FIELD_NAME, get_token())
    )


def _wants_json() -> bool:
    """True when the caller expects JSON (so the rejection should too)."""
    if request.headers.get("X-Requested-With", "").lower() == "xmlhttprequest":
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


def _local_target(referrer: Optional[str]) -> Optional[str]:
    """Return the path of ``referrer`` only when it points at this app.

    Used to send a rejected form post back to the page it came from. An
    off-host referer is ignored (redirecting there would be an open redirect).
    """
    if not referrer:
        return None
    parts = urlsplit(referrer)
    if not parts.path or (parts.netloc and parts.netloc != request.host):
        return None
    return parts.path


def protect() -> Any:
    """``before_request`` hook enforcing the token on every unsafe request.

    Returns ``None`` to let the request through, or a rejection response.
    """
    if request.method not in UNSAFE_METHODS:
        return None
    if current_app.config.get("TESTING"):
        return None

    expected = session.get(TOKEN_SESSION_KEY)
    reason = ""
    if not expected:
        # A session with no token cannot be validated, so an unsafe request
        # arriving on it is rejected rather than trusted.
        reason = "no session token"
    else:
        provided = request.headers.get(HEADER_NAME) or request.form.get(FIELD_NAME)
        if provided and hmac.compare_digest(str(expected), str(provided)):
            return None
        reason = "token missing or mismatched"

    log.get_logger("csrf").warning(
        "csrf: rejected %s %s (%s)", request.method, request.path, reason
    )
    if _wants_json():
        return jsonify(ok=False, error=_MESSAGE), 400
    flash(_MESSAGE, "error")
    return redirect(_local_target(request.referrer) or url_for("home.index"))
