"""Service configuration and revocable, one-time login handoff credentials."""
import hashlib
import re
import secrets
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

from . import db


def sql(name):
    return db._sql("services", name)


@contextmanager
def connection():
    conn = db.get_db()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def list_services():
    with connection() as conn:
        return [dict(row) for row in conn.execute(sql("list"))]


def get_service(slug):
    with connection() as conn:
        row = conn.execute(sql("get"), (slug,)).fetchone()
        return dict(row) if row else None


def parse_service(name, slug, address):
    name, slug, address = name.strip(), slug.strip(), address.strip()
    if not name or len(name) > 80:
        raise ValueError("Name must contain 1–80 characters.")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,38}[a-z0-9]|[a-z]", slug):
        raise ValueError("Identifier must contain 1–40 lowercase letters, digits or hyphens, starting with a letter.")
    if "://" not in address:
        address = "http://" + address
    try:
        parts = urlsplit(address)
        port = parts.port
    except ValueError as exc:
        raise ValueError("Invalid service address or port.") from exc
    if (parts.scheme not in ("http", "https") or not parts.hostname
            or parts.username or parts.password or "\\" in address
            or any(ord(c) <= 32 for c in address)):
        raise ValueError("Use an HTTP(S) service address without credentials or whitespace.")
    host = parts.hostname.encode("idna").decode("ascii")
    if ":" in host:
        host = f"[{host}]"
    upstream = f"{parts.scheme}://{host}" + (f":{port}" if port else "")
    entry = parts.path or "/"
    if not entry.startswith("/") or entry.startswith("//"):
        raise ValueError("Invalid entry path.")
    if parts.query:
        entry += "?" + parts.query
    if parts.fragment:
        entry += "#" + parts.fragment
    return name, slug, upstream, entry


def save_service(values, enabled, service_id=None):
    with connection() as conn:
        if service_id is None:
            conn.execute(sql("insert"), (*values, int(enabled)))
        else:
            conn.execute(sql("update"), (*values, int(enabled), service_id))
            conn.execute(sql("revoke_service"), (service_id,))


def delete_service(service_id):
    with connection() as conn:
        conn.execute(sql("delete"), (service_id,))


def revoke_login(login_key):
    if login_key:
        with connection() as conn:
            conn.execute(sql("revoke_login"), (login_key,))


def _digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _issue(conn, user_id, service_id, login_key, stamp, kind, expires):
    token = secrets.token_urlsafe(32)
    conn.execute(sql("issue"), (_digest(token), user_id, service_id, login_key, stamp, kind, expires))
    return token


def issue_code(user, service, login_key):
    with connection() as conn:
        conn.execute(sql("prune"), (time.time(),))
        return _issue(conn, user["id"], service["id"], login_key,
                      user["password_hash"], "code", time.time() + 60)


def exchange_code(code, slug):
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(sql("grant"), (_digest(code), "code", time.time())).fetchone()
        if not row or row["slug"] != slug:
            return None
        conn.execute(sql("consume"), (_digest(code),))
        # Match the website's 30-day login lifetime, with explicit logout revocation.
        token = _issue(conn, row["user_id"], row["service_id"], row["login_key"],
                       row["password_stamp"], "session", time.time() + 30 * 86400)
        return token, dict(row)


def authorize(token, slug):
    if not token or len(token) > 128:
        return None
    with connection() as conn:
        row = conn.execute(sql("grant"), (_digest(token), "session", time.time())).fetchone()
        return dict(row) if row and row["slug"] == slug else None


def service_origin(cfg, slug):
    template = cfg.get("services_origin_template", "http://{slug}.video.localhost:8090")
    origin = template.format(slug=slug).rstrip("/")
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.path or parts.query or parts.fragment or parts.username:
        raise ValueError("services_origin_template must be an HTTP(S) origin containing {slug}.")
    if "{slug}" not in template or parts.hostname == urlsplit(cfg.get("services_parent_origin", "")).hostname:
        raise ValueError("Services must use separate hosts from the website.")
    return origin
