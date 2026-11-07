"""Integration coverage: authorization boundaries and transparent HTTP/WS proxying."""
import asyncio
import gzip
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import ClientSession, WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL
from flask import Flask
from werkzeug.security import generate_password_hash

from app import csrf, db, services
from app.auth import auth_bp
from app.routes.services import admin_services_bp, services_bp
from app.service_gateway import COOKIE, create_gateway


class DatabaseMixin:
    def setup_database(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_patch = patch.object(db, "DB_PATH", Path(self.temp.name) / "test.db")
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        db.init_db()
        self.uid = db.create_user("tester", generate_password_hash("test-only-password"), is_admin=1, must_change_password=0)
        self.user = db.get_user_by_id(self.uid)


class ServiceRoutesTest(DatabaseMixin, unittest.TestCase):
    def setUp(self):
        self.setup_database()
        self.app = Flask(__name__, template_folder="../app/templates")
        self.app.secret_key = "test-only"
        self.app.config.update(services_origin_template="http://{slug}.video.localhost:8090", services_parent_origin="http://video.localhost:8080")
        self.app.register_blueprint(auth_bp)
        self.app.register_blueprint(services_bp)
        self.app.register_blueprint(admin_services_bp)
        self.app.before_request(csrf.protect)
        self.app.add_url_rule("/", "home.index", lambda: "home")
        self.client = self.app.test_client()

    def login(self, user_id=None):
        with self.client.session_transaction() as session:
            session["user_id"] = user_id or self.uid
            session["_csrf"] = "test-token"

    def post(self, data):
        return self.client.post("/admin/services/", data=data, headers={"X-CSRF-Token": "test-token"})

    def test_access_controls_and_crud(self):
        self.assertEqual(self.client.get("/services/").status_code, 302)
        self.assertEqual(self.client.get("/admin/services/").status_code, 302)
        member = db.create_user("member", "hash", is_admin=0, must_change_password=0)
        self.login(member)
        self.assertEqual(self.client.get("/admin/services/").status_code, 403)
        self.login()
        self.assertEqual(self.client.post("/admin/services/", data={}).status_code, 302)
        self.assertEqual(services.list_services(), [])  # Missing CSRF never writes.
        data = dict(name="Media", slug="media", address="http://192.168.57.238:8096/web/#/home", enabled="1")
        self.assertEqual(self.post(data).status_code, 302)
        service = services.get_service("media")
        self.assertEqual(service["upstream"], "http://192.168.57.238:8096")
        self.assertEqual(service["entry_path"], "/web/#/home")
        self.post(dict(data, name="Duplicate"))
        self.assertEqual(len(services.list_services()), 1)
        code = services.issue_code(self.user, service, "login")
        token, _ = services.exchange_code(code, "media")
        self.post(dict(data, original_slug="media", name="Updated", enabled="0"))
        self.assertIsNone(services.authorize(token, "media"))
        self.assertEqual(self.client.get("/services/media/").status_code, 404)
        self.post(dict(original_slug="media", action="delete"))
        self.assertEqual(services.list_services(), [])

    def test_handoff_requires_post_csrf_and_logout_revokes(self):
        services.save_service(services.parse_service("Demo", "demo", "http://127.0.0.1:9000"), True)
        self.login()
        self.assertEqual(self.client.get("/services/demo/launch").status_code, 405)
        response = self.client.post("/services/demo/launch", headers={"X-CSRF-Token": "test-token"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Referrer-Policy"], "strict-origin")
        import re
        code = re.search(r'name="code" value="([^"]+)"', response.text).group(1)
        token, _ = services.exchange_code(code, "demo")
        self.assertIsNotNone(services.authorize(token, "demo"))
        self.client.get("/auth/logout")
        self.assertIsNone(services.authorize(token, "demo"))

    def test_validation_and_expiry(self):
        for address in ("file:///etc/passwd", "http://user:password@localhost", "http://localhost:bad", "http://localhost/\r\nX:1"):
            with self.assertRaises(ValueError):
                services.parse_service("Demo", "demo", address)
        for slug in ("../admin", "Demo", "demo_1", "-demo", "demo-", "a" * 41):
            with self.assertRaises(ValueError):
                services.parse_service("Demo", slug, "localhost:9000")
        services.save_service(services.parse_service("Demo", "demo", "localhost:9000"), True)
        service = services.get_service("demo")
        code = services.issue_code(self.user, service, "login")
        self.assertIsNone(services.exchange_code(code, "other"))
        with patch("app.services.time.time", return_value=time.time() + 61):
            self.assertIsNone(services.exchange_code(code, "demo"))
        token, _ = services.exchange_code(code, "demo")
        self.assertIsNone(services.exchange_code(code, "demo"))
        self.assertIsNone(services.authorize(token, "other"))
        db.update_user(self.uid, password_hash="changed")
        self.assertIsNone(services.authorize(token, "demo"))


class GatewayTest(DatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup_database()
        self.received = []

        async def upstream(request):
            self.received.append((request.raw_path, dict(request.headers), await request.read()))
            if request.path == "/redirect":
                return web.Response(status=302, headers={"Location": str(self.upstream.make_url("/web/#/home"))})
            if request.path == "/compressed":
                return web.Response(body=gzip.compress(b"compressed payload"), headers={"Content-Encoding": "gzip"})
            if request.path == "/cookie":
                return web.Response(text="ok", headers=[("Set-Cookie", "upstream=one; Domain=localhost; Path=/; HttpOnly"), ("Set-Cookie", f"{COOKIE}=evil; Path=/"), ("X-Frame-Options", "SAMEORIGIN"), ("Content-Security-Policy", "default-src 'self'; frame-ancestors 'self'")])
            if request.path == "/stream":
                response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
                await response.prepare(request)
                await response.write(b"data: first\n\n")
                await asyncio.sleep(5)
                with self.assertRaises((ConnectionError, RuntimeError)):
                    await response.write(b"data: second\n\n")
                return response
            if request.path == "/ws":
                ws = web.WebSocketResponse(protocols=["test"])
                await ws.prepare(request)
                async for message in ws:
                    if message.type == WSMsgType.TEXT:
                        await ws.send_str(message.data)
                return ws
            return web.Response(body=b"echo:" + self.received[-1][2], headers={"Content-Range": "bytes 0-3/20"} if request.headers.get("Range") else {}, status=206 if request.headers.get("Range") else 200)

        app = web.Application()
        app.router.add_route("*", "/{path:.*}", upstream)
        self.upstream = TestServer(app)
        await self.upstream.start_server()
        services.save_service(services.parse_service("Demo", "demo", str(self.upstream.make_url("/web/#/home"))), True)
        self.service = services.get_service("demo")
        self.cfg = dict(services_origin_template="http://{slug}.video.localhost:8090", services_parent_origin="http://video.localhost:8080")
        self.client = TestClient(TestServer(create_gateway(self.cfg)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.addAsyncCleanup(self.upstream.close)
        self.host = {"Host": "demo.video.localhost:8090"}
        code = services.issue_code(self.user, self.service, "login")
        self.token, _ = services.exchange_code(code, "demo")
        self.auth = dict(self.host, Cookie=f"{COOKIE}={self.token}; upstream=two")

    async def test_anonymous_all_paths_and_unknown_host(self):
        for path in ("/", "/web/", "/api/private", "/ws"):
            response = await self.client.get(path, headers=self.host)
            self.assertEqual(response.status, 401)
        response = await self.client.get("/", headers={"Host": "attacker.example"})
        self.assertEqual(response.status, 404)
        self.assertEqual(self.received, [])

    async def test_bootstrap_is_one_time_and_origin_bound(self):
        code = services.issue_code(self.user, self.service, "login")
        response = await self.client.post("/_svs/authorize", data={"code": code}, headers=dict(self.host, Origin="http://evil.example"))
        self.assertEqual(response.status, 403)
        headers = dict(self.host, Origin=self.cfg["services_parent_origin"])
        response = await self.client.post("/_svs/authorize", data={"code": code}, headers=headers, allow_redirects=False)
        self.assertEqual(response.status, 303)
        self.assertEqual(response.headers["Location"], "/web/#/home")
        self.assertIn("HttpOnly", response.headers["Set-Cookie"])
        self.assertNotIn("Domain=", response.headers["Set-Cookie"])
        response = await self.client.post("/_svs/authorize", data={"code": code}, headers=headers)
        self.assertEqual(response.status, 403)

    async def test_http_body_range_headers_cookies_and_redirects(self):
        response = await self.client.post(URL("/api/echo?q=a%2Fb", encoded=True), data=b"payload", headers=dict(self.auth, Origin="http://demo.video.localhost:8090", **{"X-CSRF-Token": "upstream-token", "X-Forwarded-User": "spoof", "Remote-User": "spoof"}))
        self.assertEqual(await response.read(), b"echo:payload")
        path, headers, body = self.received[-1]
        self.assertEqual(path, "/api/echo?q=a%2Fb")
        self.assertEqual(headers["Cookie"], "upstream=two")
        self.assertEqual(headers["X-CSRF-Token"], "upstream-token")
        self.assertNotIn("X-Forwarded-User", headers)
        self.assertNotIn("Remote-User", headers)
        response = await self.client.get("/file", headers=dict(self.auth, Range="bytes=0-3"))
        self.assertEqual(response.status, 206)
        self.assertEqual(response.headers["Content-Range"], "bytes 0-3/20")
        response = await self.client.get("/cookie", headers=self.auth)
        self.assertNotIn("X-Frame-Options", response.headers)
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors http://video.localhost:8080", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers.getall("Set-Cookie"), ["upstream=one; HttpOnly; Path=/"])
        response = await self.client.get("/redirect", headers=self.auth, allow_redirects=False)
        self.assertEqual(response.headers["Location"], "http://demo.video.localhost:8090/web/#/home")
        response = await self.client.get("/compressed", headers=self.auth)
        self.assertEqual(await response.read(), b"compressed payload")
        response = await self.client.post("/api/echo", headers=dict(self.auth, Origin="http://evil.example"))
        self.assertEqual(response.status, 403)

    async def test_websocket_and_logout_close_active_connection(self):
        ws = await self.client.ws_connect("/ws", headers=dict(self.auth, Origin="http://demo.video.localhost:8090"), protocols=["test"])
        self.assertEqual(ws.protocol, "test")
        await ws.send_str("hello")
        self.assertEqual((await ws.receive(timeout=2)).data, "hello")
        services.revoke_login("login")
        message = await ws.receive(timeout=3)
        self.assertIn(message.type, (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR))
        response = await self.client.get("/api/private", headers=self.auth)
        self.assertEqual(response.status, 401)

    async def test_stream_arrives_before_completion_and_disable_closes_it(self):
        response = await self.client.get("/stream", headers=self.auth)
        self.assertEqual(await asyncio.wait_for(response.content.readuntil(b"\n\n"), 1), b"data: first\n\n")
        services.save_service(("Demo", "demo", self.service["upstream"], "/"), False, self.service["id"])
        with self.assertRaises(Exception):
            await asyncio.wait_for(response.read(), 3)

    async def test_unreachable_upstream(self):
        await self.upstream.close()
        response = await self.client.get("/web/", headers=self.auth)
        self.assertEqual(response.status, 502)


if __name__ == "__main__":
    unittest.main()
