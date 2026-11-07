"""Host-routed service proxy. Run separately from WSGI; main.py starts it locally.

Only configured origins are accepted. Every request and every live connection
is checked against revocable website grants. No upstream redirects are followed.
"""
import asyncio
import contextlib
import html
import logging
from http.cookies import SimpleCookie
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from aiohttp import web
from multidict import CIMultiDict
from yarl import URL

from . import services

LOG = logging.getLogger(__name__)
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailer", "transfer-encoding", "upgrade"}
COOKIE = "svs_service_access"
SECURE_COOKIE = "__Host-svs_service_access"
CLIENT = web.AppKey("client", aiohttp.ClientSession)
CONFIG = web.AppKey("config", dict)


def _cookie_name(origin):
    return SECURE_COOKIE if origin.startswith("https://") else COOKIE


def _headers(source):
    excluded = HOP | {v.strip().lower() for v in source.get("Connection", "").split(",")}
    return CIMultiDict((k, v) for k, v in source.items() if k.lower() not in excluded)


def _response_headers(source, upstream, origin, parent):
    headers = _headers(source)
    for key in ("Set-Cookie", "X-Frame-Options", "Content-Security-Policy", "Content-Security-Policy-Report-Only"):
        headers.popall(key, None)
    # Keep all upstream CSP restrictions except frame-ancestors, which must
    # allow exactly the website wrapping this service.
    policies = source.getall("Content-Security-Policy", [])
    for policy in policies:
        clauses = [part.strip() for part in policy.split(";") if part.strip() and part.strip().split()[0].lower() != "frame-ancestors"]
        headers.add("Content-Security-Policy", "; ".join(clauses + [f"frame-ancestors {parent}"]))
    if not policies:
        headers["Content-Security-Policy"] = f"frame-ancestors {parent}"
    for raw in source.getall("Set-Cookie", []):
        cookie = SimpleCookie()
        cookie.load(raw)
        for name, morsel in cookie.items():
            if name in (COOKIE, SECURE_COOKIE):
                continue
            morsel["domain"] = ""  # Never let an upstream set parent-domain cookies.
            headers.add("Set-Cookie", morsel.OutputString())
    location = headers.get("Location")
    if location:
        parts = urlsplit(location)
        target = urlsplit(upstream)
        if parts.netloc == target.netloc and (not parts.scheme or parts.scheme == target.scheme):
            public = urlsplit(origin)
            headers["Location"] = urlunsplit((public.scheme, public.netloc, parts.path, parts.query, parts.fragment))
    headers["Referrer-Policy"] = "no-referrer"
    # Service data is authenticated, including static files and error responses.
    headers["Cache-Control"] = "no-store"
    return headers


async def _watch(request, token, slug, task):
    try:
        while True:
            await asyncio.sleep(1)
            if not await asyncio.to_thread(services.authorize, token, slug):
                if request.transport:
                    request.transport.close()
                task.cancel()
                return
    except asyncio.CancelledError:
        pass


async def _relay(source, dest):
    async for msg in source:
        if msg.type == aiohttp.WSMsgType.TEXT:
            await dest.send_str(msg.data)
        elif msg.type == aiohttp.WSMsgType.BINARY:
            await dest.send_bytes(msg.data)
        elif msg.type == aiohttp.WSMsgType.PING:
            await dest.ping(msg.data)
        elif msg.type == aiohttp.WSMsgType.PONG:
            await dest.pong(msg.data)
        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
            break
    await dest.close(code=source.close_code or 1000)


async def _websocket(request, target, headers):
    protocols = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip()]
    for key in list(headers):
        if key.lower().startswith("sec-websocket-"):
            headers.popall(key, None)
    async with request.app[CLIENT].ws_connect(target, headers=headers, protocols=protocols, autoping=False) as upstream:
        downstream = web.WebSocketResponse(protocols=[upstream.protocol] if upstream.protocol else [], autoping=False)
        await downstream.prepare(request)
        tasks = [asyncio.create_task(_relay(upstream, downstream)), asyncio.create_task(_relay(downstream, upstream))]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await downstream.close()
        return downstream


async def handle(request):
    cfg = request.app[CONFIG]
    parent = cfg["services_parent_origin"].rstrip("/")
    candidates = await asyncio.to_thread(services.list_services)
    service = next((s for s in candidates if s["enabled"] and urlsplit(services.service_origin(cfg, s["slug"])).netloc.lower() == request.host.lower()), None)
    if not service:
        raise web.HTTPNotFound()
    slug = service["slug"]
    origin = services.service_origin(cfg, slug)
    cookie_name = _cookie_name(origin)
    if request.path == "/_svs/authorize":
        if request.method != "POST" or request.headers.get("Origin") != parent:
            raise web.HTTPForbidden(text="Open this service from the website.")
        data = await request.post()
        code = data.get("code", "")
        if not isinstance(code, str) or len(code) > 128:
            raise web.HTTPForbidden()
        result = await asyncio.to_thread(services.exchange_code, code, slug)
        if not result:
            raise web.HTTPForbidden(text="Authorization expired. Reload the service from the website.")
        token, grant = result
        response = web.HTTPSeeOther(location=grant["entry_path"])
        response.set_cookie(cookie_name, token, secure=origin.startswith("https://"), httponly=True, samesite="Lax", path="/")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        raise response
    token = request.cookies.get(cookie_name, "")
    grant = await asyncio.to_thread(services.authorize, token, slug)
    if not grant:
        entry = html.escape(parent + cfg.get("base_path", "").rstrip("/") + f"/services/{slug}/", quote=True)
        return web.Response(status=401, text=f'<!doctype html><meta charset="utf-8"><p>Website login required. <a target="_top" href="{entry}">Open from the website</a>.</p>', content_type="text/html", headers={"Cache-Control": "no-store"})
    # Same-origin unsafe calls only; retain upstream CSRF tokens unchanged.
    if request.method not in ("GET", "HEAD", "OPTIONS") or request.headers.get("Upgrade", "").lower() == "websocket":
        if request.headers.get("Origin") not in (None, origin):
            raise web.HTTPForbidden(text="Cross-origin service request rejected.")
        if request.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
            raise web.HTTPForbidden(text="Cross-origin service request rejected.")
    headers = _headers(request.headers)
    for key in list(headers):
        lower = key.lower()
        if lower in {"host", "cookie", "forwarded", "x-real-ip", "remote-user"} or lower.startswith(("x-forwarded-", "x-auth-", "x-authentik-", "x-authelia-")):
            headers.popall(key, None)
    cookies = SimpleCookie()
    cookies.load(request.headers.get("Cookie", ""))
    for name in (COOKIE, SECURE_COOKIE):
        cookies.pop(name, None)
    if cookies:
        headers["Cookie"] = "; ".join(m.OutputString() for m in cookies.values())
    headers["X-Forwarded-Host"] = urlsplit(origin).netloc
    headers["X-Forwarded-Proto"] = urlsplit(origin).scheme
    if headers.get("Origin") == origin:
        headers["Origin"] = grant["upstream"]
    referer = headers.get("Referer", "")
    if referer.startswith(origin + "/"):
        headers["Referer"] = grant["upstream"] + referer[len(origin):]
    # Concatenate a fixed configured origin, never urljoin untrusted // paths.
    target = URL(grant["upstream"] + request.raw_path, encoded=True)
    watcher = asyncio.create_task(_watch(request, token, slug, asyncio.current_task()))
    response = None
    try:
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await _websocket(request, target, headers)
        async with request.app[CLIENT].request(request.method, target, headers=headers,
                data=request.content if request.can_read_body else None, allow_redirects=False) as upstream:
            response = web.StreamResponse(status=upstream.status, reason=upstream.reason,
                headers=_response_headers(upstream.headers, grant["upstream"], origin, parent))
            await response.prepare(request)
            async for chunk in upstream.content.iter_any():
                await response.write(chunk)
            await response.write_eof()
            return response
    except (aiohttp.ClientError, TimeoutError):
        LOG.warning("Service %s is unavailable", slug)
        if response is not None and response.prepared:
            # Headers/body have already reached the browser. A second HTTP
            # response would corrupt the stream; terminate it instead.
            if request.transport:
                request.transport.close()
            return response
        if request.transport and not request.transport.is_closing():
            return web.Response(status=502, text="Service temporarily unavailable. Check its connection, then reload.", headers={"Cache-Control": "no-store"})
        raise
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


async def _client_context(app):
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar(),
            auto_decompress=False, timeout=aiohttp.ClientTimeout(total=None, sock_connect=10),
            skip_auto_headers={"Accept-Encoding", "Content-Type"}) as client:
        app[CLIENT] = client
        yield


def create_gateway(cfg):
    app = web.Application(client_max_size=64 * 1024, handler_args={"auto_decompress": False})
    app[CONFIG] = cfg
    app.cleanup_ctx.append(_client_context)
    app.router.add_route("*", "/{path:.*}", handle)
    return app


def run(cfg):
    web.run_app(create_gateway(cfg), host=cfg.get("services_gateway_host", "127.0.0.1"),
                port=int(cfg.get("services_gateway_port", 8090)), access_log=None)
