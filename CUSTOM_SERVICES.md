# Custom services

Administrators manage services at `/admin/services/`. Signed-in users open them
through `/services/`, or `/services/<identifier>/`. A service address may include
an initial path, query and fragment, such as `http://192.168.57.238:8096/web/#/home`.
Only its origin is used as the upstream; the full entry path opens after login.
The gateway does not authenticate to the upstream application's own accounts.

## Local review

Install `requirements.txt`, then run `python main.py`. This starts Flask on the
configured app port and a separate asynchronous service gateway on port 8090.
Open **http://video.localhost:8080**, sign in with an existing website account,
and use Admin → Custom services to add a service. The sidebar item sits below
Admin and above the theme toggle. Ordinary users can open services but cannot
manage their definitions.

The default service origin is `http://<identifier>.video.localhost:8090`.
Chromium resolves `.localhost` names to loopback, without a hosts-file edit.
Use `video.localhost` for the website as well: the parent and iframe must be
same-site for the gateway's `SameSite=Lax` cookie. Opening the parent via
`127.0.0.1` while its iframe uses `.localhost` may cause authorization failures.
These names are local to the machine running the browser, not LAN sharing URLs.
The app port in `services_parent_origin` must match the actual Flask port.

## Configuration

Deployment parameters live in `config.json` (defaults in `config.example.json`):

```json
{
  "services_origin_template": "http://{slug}.video.localhost:8090",
  "services_parent_origin": "http://video.localhost:8080",
  "services_gateway_host": "127.0.0.1",
  "services_gateway_port": 8090
}
```

`services_parent_origin` is the exact origin, without any mounted app path.
`base_path`, if used, still controls website routes. Restart both processes after
changing deployment configuration. Editing a service takes effect without a
restart and revokes its existing gateway grants; users reopen it from the site.

## Authentication and forwarding

- The launcher requires website login and a CSRF-protected POST. It sends a
  60-second, single-use opaque code through a hidden form inside the iframe.
  Credentials are not placed in URLs. Codes and grants are stored as SHA-256
  hashes in SQLite; the service-specific session also records the login and
  password version. Gateway cookies are host-only, HttpOnly and SameSite=Lax;
  HTTPS uses a Secure `__Host-` cookie. Browser sessions end when the browser
  expires its session cookies, with a server-side maximum lifetime of 30 days.
- Every upstream request checks the user, service status and grant. Logout or
  replacing the website login revokes that login's grants. Password changes,
  account deletion and service edits/deletion also invalidate access. Active
  streams and WebSockets recheck once per second and close on revocation.
- HTTP bodies and responses stream without whole-file buffering. Range requests,
  compressed bodies, upstream CSRF tokens and WebSocket protocols are preserved.
  Upstream redirects are returned rather than followed, preventing the gateway
  from forwarding credentials to a redirected host. Absolute redirects to the
  upstream are mapped back to the public service origin.
- Website cookies are host-only and stay on the website. Gateway cookies never
  reach the upstream. Upstream cookies are restricted to the service host and
  cannot overwrite the reserved gateway cookie. Spoofable forwarding/SSO headers
  are removed. Service applications retain their own authorization and CSRF
  checks; the gateway also rejects cross-origin unsafe browser requests.
- The gateway replaces upstream framing restrictions with a CSP allowing only
  the configured website as the frame ancestor, preserving other upstream CSP
  directives. Add trusted applications only. Applications that hard-code their
  internal host or use unrelated domains may still require upstream settings.
- A service identifier is a lowercase DNS label of 1–40 characters, beginning
  with a letter. Only administrators can choose destinations. Keep upstreams
  bound to loopback/firewalled when server-local so there is no public bypass.

## Production later (not applied)

Run the existing WSGI server normally and `python service_gateway.py` as a
separately supervised process using the same project directory/configuration and
SQLite database. Keep the gateway listening on loopback. Configure wildcard DNS
and TLS once for the chosen service domain, then have Apache preserve `Host` and
proxy that wildcard virtual host to `127.0.0.1:8090`, including WebSocket upgrades
and streaming. For example, use `https://{slug}.services.vsakuya.xyz` together with
`services_parent_origin: https://video.vsakuya.xyz`. Do not expose the gateway
port directly or broaden the website session cookie to the parent domain.

No production vhost, DNS, certificates, deployment scripts or remote services
are changed by this feature's local implementation.

## Verification

`python -m unittest discover -s testing -p test_services.py -v`

These tests use temporary SQLite databases and loopback upstream servers. They
cover administrator boundaries, CSRF, CRUD, entry URLs, one-time handoff, cookie
isolation, expiry/password changes/logout, HTTP fidelity, Range, compression,
redirects, streaming, WebSockets, active connection revocation and offline errors.
