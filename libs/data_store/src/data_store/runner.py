"""Secure rqlite-backed SQL store exposed to token-authed callers.

The security model is *structural*, not heuristic. The service runs two separate
apps on two separate ports:

- The **public app** (``PUBLIC_PORT``) exposes ``/health``, a demo client at
  ``/``, and the token-gated rqlite SQL passthrough (``/db/query``,
  ``/db/execute``, ``/db/request``). This is the port the tunnel forwards to the
  internet. It has no way to read or rotate the token -- so the token cannot be
  obtained through the public surface at all.

- The **admin app** (``ADMIN_PORT``) serves the management console at ``/`` plus
  the token-reveal / rotate endpoints. It is registered as the workspace tab and
  is reached only through the local system_interface proxy; the tunnel never
  forwards this port. As defense-in-depth, the token-reveal endpoints
  additionally refuse any request that arrived through Cloudflare, so the token
  is only handed to a genuinely local request.

Every ``/db`` endpoint requires ``Authorization: Bearer <token>`` on both apps
and is forwarded to ``rqlited`` verbatim, so callers use rqlite's native
request/response format and standard tooling. This is deliberately an
*arbitrary-SQL* surface -- the bearer token is the whole gate. Both apps bind to
loopback; TLS is terminated at the tunnel edge.
"""

import os
import threading
from pathlib import Path
from typing import Callable

import httpx
import segno
from flask import Flask
from flask import Response
from flask import g
from flask import jsonify
from flask import request
from werkzeug.serving import run_simple

from data_store import db
from data_store import security

# Local state this process owns (the API token file, the tunnel's recorded public
# URL) lives under DATA_DIR. The SQL data itself lives in rqlite.
DATA_DIR = Path(os.environ.get("DATA_STORE_DATA_DIR", "data/.apps/data_store"))
PUBLIC_URL_FILE = DATA_DIR / "public_url.txt"

# The rqlite HTTP endpoint backing the store.
RQLITE_URL = os.environ.get("DATA_STORE_RQLITE_URL", "http://localhost:4001")

# Presence of this file means the tunnel runs ngrok on a reserved domain, so the
# public URL is permanent; otherwise the fallback tunnel URL is only interim.
NGROK_SECRETS_FILE = Path("data/.secrets/data_store_ngrok.env")

# Two ports: the public (tunneled) SQL API and the local-only admin console.
PUBLIC_PORT = int(os.environ.get("DATA_STORE_PORT", "8080"))
ADMIN_PORT = int(os.environ.get("DATA_STORE_ADMIN_PORT", "8084"))

# A single request body is capped so one call can't exhaust memory or disk.
MAX_BODY_BYTES = 1_048_576  # 1 MiB

# Generous per-client request ceiling: throttles floods without impeding a
# legitimately busy client. The 256-bit token is the real guard.
RATE_LIMIT_MAX_REQUESTS = int(os.environ.get("DATA_STORE_RATE_LIMIT", "300"))
RATE_LIMIT_WINDOW_SECONDS = 60.0

_ASSETS_DIR = Path(__file__).parent / "assets"


class ConfigurationError(Exception):
    """Raised when the app is built with an invalid configuration."""


class _TokenStore:
    """Holds the current API token in memory, kept in sync with disk.

    Single-process, so the in-memory copy is authoritative once loaded (both apps
    share this one instance); rotation rewrites the file and updates the copy
    together.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._token = security.load_or_create_token(data_dir)

    def get(self) -> str:
        return self._token

    def rotate(self) -> str:
        self._token = security.rotate_token(self._data_dir)
        return self._token


def _client_id() -> str:
    """Best identifier for the caller: the real client IP when behind a tunnel.

    Tunnels (cloudflare, ngrok, localtunnel) and Cloudflare put the true client
    address in a forwarded header; fall back to the socket peer for genuinely
    local requests.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.headers.get("Cf-Connecting-Ip") or (request.remote_addr or "unknown")


def _error(message: str, status: int) -> Response:
    response = jsonify({"error": message})
    response.status_code = status
    return response


def create_app(
    mode: str,
    token_store: _TokenStore,
    rate_limiter: security.RateLimiter,
    store_url: str,
    forwarder: Callable[[str, str, str, str, bytes], httpx.Response] = db.forward,
) -> Flask:
    """Build one of the two apps.

    ``mode`` is ``"public"`` (SQL API + demo client) or ``"admin"`` (adds the
    console and the local-only token endpoints). Dependencies are injected so
    tests can supply their own -- ``forwarder`` defaults to the real rqlite
    passthrough but a test can pass a stub.
    """
    if mode not in ("public", "admin"):
        raise ConfigurationError(f"unknown app mode: {mode!r}")
    is_admin = mode == "admin"
    application = Flask(f"data_store_{mode}", static_folder=None)
    application.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES

    def _authorize() -> bool:
        """Validate the bearer token against the current one, in constant time."""
        provided = security.extract_bearer_token(request.headers.get("Authorization"))
        if provided is None:
            return False
        return security.token_matches(provided, token_store.get())

    @application.before_request
    def _before_request() -> Response | None:
        g.authed = False
        if request.path == "/health":
            return None
        if not rate_limiter.is_allowed(_client_id()):
            return _error("rate limit exceeded", 429)
        return None

    @application.get("/health")
    def health() -> Response:
        return jsonify({"status": "ok"})

    # --- User-facing pages (static HTML, no auth to load) ---

    @application.get("/")
    @application.get("/home")
    def landing() -> Response:
        return _page("landing.html")

    @application.get("/db")
    @application.get("/manage")
    def manage() -> Response:
        # The database management console: browse tables, run SQL, rotate token.
        # /manage is an alias for /db.
        return _page("manage.html")

    @application.get("/demo")
    def demo() -> Response:
        # A self-contained sample app (a drag-and-drop todo list) that persists
        # to the store through the same token-gated SQL API, to show a realistic
        # client. Its own JS holds the token (from localStorage).
        return _page("demo.html")

    @application.get("/llms.txt")
    def llms_txt() -> Response:
        # Agent-facing documentation: how a coding agent / LLM client talks to
        # the SQL API. Plain text, with the live public URL filled in.
        public_url = PUBLIC_URL_FILE.read_text().strip() if PUBLIC_URL_FILE.exists() else None
        return Response(_render_llms_txt(public_url), mimetype="text/plain")

    # --- The SQL API, token-gated, forwarded verbatim to rqlite ---

    def _forward(endpoint: str) -> Response:
        if not _authorize():
            return _error("unauthorized", 401)
        g.authed = True
        upstream = forwarder(store_url, endpoint, request.method, request.query_string.decode(), request.get_data())
        return Response(
            upstream.content,
            status=upstream.status_code,
            content_type=upstream.headers.get("content-type", "application/json"),
        )

    @application.get("/api")
    def api_index() -> Response:
        return jsonify(
            {
                "endpoints": {
                    "query": "POST /api/query — read statements",
                    "execute": "POST /api/execute — write statements",
                    "request": "POST /api/request — unified read+write",
                },
                "auth": "Authorization: Bearer <token>",
                "format": "rqlite native, e.g. [[\"SELECT ?\", 1]]; add ?associative for object rows",
                "docs": "/llms.txt",
            }
        )

    @application.get("/api/query")
    @application.post("/api/query")
    def api_query() -> Response:
        return _forward("query")

    @application.post("/api/execute")
    def api_execute() -> Response:
        return _forward("execute")

    @application.post("/api/request")
    def api_request() -> Response:
        return _forward("request")

    if is_admin:
        _register_admin_routes(application, token_store)

    return application


def _page(filename: str) -> Response:
    return Response((_ASSETS_DIR / filename).read_text(), mimetype="text/html")


def _render_llms_txt(public_url: str | None) -> str:
    """Agent-facing docs for the SQL API, with the live public URL filled in."""
    base = public_url or "https://<your-public-url>"
    return f"""# data-store

A private rqlite (SQLite-over-HTTP) database exposed to the internet behind one
bearer token. You (an agent or client) can run arbitrary SQL against it.

Base URL: {base}
Auth: every /api request needs the header `Authorization: Bearer <token>`.
The token is secret and is NOT served over HTTP -- the human operator reads it
from the file `data/.apps/data_store/api_token` in the workspace and gives it to
you. Never expect an endpoint to hand you the token.

## Endpoints

- POST {base}/api/query   -- read statements (SELECT, PRAGMA, ...)
- POST {base}/api/execute -- write statements (INSERT, UPDATE, DELETE, DDL)
- POST {base}/api/request -- unified read+write in one call
- GET  {base}/health      -- liveness, no auth

## Request format (rqlite native)

The body is a JSON array of statements. Each statement is an array of
`[sql, ...params]`; use `?` placeholders and pass values as params (never string
-interpolate them). Add `?associative` to get result rows as JSON objects.

Read example:

  curl -H "Authorization: Bearer $TOKEN" \\
       -d '[["SELECT id, title FROM todos WHERE done = ?", 0]]' \\
       '{base}/api/query?associative'

  -> {{"results":[{{"types":{{...}},"rows":[{{"id":1,"title":"..."}}]}}]}}

Write example (multiple statements run in order; add ?transaction to make them atomic):

  curl -H "Authorization: Bearer $TOKEN" \\
       -d '[["INSERT INTO todos(title,done) VALUES(?,0)", "write docs"]]' \\
       '{base}/api/execute'

  -> {{"results":[{{"last_insert_id":1,"rows_affected":1}}]}}

## Notes

- There is no fixed schema; create whatever tables you need with DDL via /api/execute.
- Errors come back per-statement as a "error" key inside that statement's result.
- Request bodies are capped at 1 MiB and there is a per-client rate limit.
- Only these SQL endpoints are exposed; rqlite admin endpoints (backup/load/node
  control) are not reachable.
- Full rqlite API reference: https://rqlite.io/docs/api/
"""


def _register_admin_routes(application: Flask, token_store: _TokenStore) -> None:
    """Register the local-only token / connection endpoints (admin app only)."""

    @application.get("/admin/connection")
    def admin_connection() -> Response:
        # Defense-in-depth: even on this non-tunneled port, refuse anything that
        # arrived through the workspace's own Cloudflare tunnel.
        if not security.is_local_request(request.headers):
            return _error("not available", 403)
        public_url = PUBLIC_URL_FILE.read_text().strip() if PUBLIC_URL_FILE.exists() else None
        qr_svg = segno.make(public_url, error="m").svg_data_uri(scale=4, border=2) if public_url else None
        # The token is deliberately NOT included: it is only readable from the
        # on-disk file (see token_file). This endpoint returns non-secret
        # connection info only.
        return jsonify(
            {
                "public_url": public_url,
                "api_base": f"{public_url}/api" if public_url else None,
                "qr_svg": qr_svg,
                "permanent_url": NGROK_SECRETS_FILE.exists(),
                "token_file": str(security.token_path(DATA_DIR)),
                "note": "Read the token from token_file, then send 'Authorization: Bearer <token>' to /api/query and /api/execute in rqlite's native format.",
            }
        )

    @application.post("/admin/rotate")
    def admin_rotate() -> Response:
        if not security.is_local_request(request.headers):
            return _error("not available", 403)
        # Regenerate the on-disk token but never return it -- the new value is
        # readable only from the file.
        token_store.rotate()
        return jsonify({"rotated": True, "token_file": str(security.token_path(DATA_DIR))})


def _serve(application: Flask, port: int) -> None:
    run_simple("127.0.0.1", port, application, threaded=True, use_reloader=False, use_debugger=False)


def main() -> None:
    db.wait_for_rqlite(RQLITE_URL)
    token_store = _TokenStore(DATA_DIR)
    rate_limiter = security.RateLimiter(RATE_LIMIT_MAX_REQUESTS, RATE_LIMIT_WINDOW_SECONDS)
    public_app = create_app("public", token_store, rate_limiter, RQLITE_URL)
    admin_app = create_app("admin", token_store, rate_limiter, RQLITE_URL)
    # Public (tunneled) app runs in a background thread; the admin app (the
    # workspace tab) runs on the main thread.
    threading.Thread(target=_serve, args=(public_app, PUBLIC_PORT), daemon=True).start()
    _serve(admin_app, ADMIN_PORT)


if __name__ == "__main__":
    main()
