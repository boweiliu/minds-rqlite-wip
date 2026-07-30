"""Secure rqlite-backed JSON document store.

The security model is *structural*, not heuristic. The service runs two separate
apps on two separate ports:

- The **public app** (``PUBLIC_PORT``) exposes only the token-gated data API
  (``/health`` + ``/api/collections/...``). This is the port the dedicated
  tunnel forwards to the internet. It has no viewer and no way to read or rotate
  the token -- so the token cannot be obtained through the public surface at all,
  regardless of which tunnel provider is in front.

- The **admin app** (``ADMIN_PORT``) serves the management viewer plus the
  token-reveal / rotate endpoints. It is registered as the workspace tab and is
  reached only through the local system_interface proxy; the tunnel never
  forwards this port. As defense-in-depth, the token-reveal endpoints
  additionally refuse any request that arrived through Cloudflare (i.e. the
  workspace's own public URL), so the token is only handed to a genuinely local
  request.

Every data endpoint requires ``Authorization: Bearer <token>`` on both apps.
Documents live in rqlite; only the token file and the tunnel's recorded URL live
under ``DATA_DIR``. Both apps bind to loopback; TLS is terminated at the tunnel
edge.
"""

import os
import re
import threading
from pathlib import Path
from typing import Any

from flask import Flask
from flask import Response
from flask import g
from flask import jsonify
from flask import request
from werkzeug.serving import run_simple

from data_store import db
from data_store import security

# Local state this process owns (the API token file, the tunnel's recorded public
# URL) lives under DATA_DIR. The documents themselves live in rqlite.
DATA_DIR = Path(os.environ.get("DATA_STORE_DATA_DIR", "runtime/data-store"))
PUBLIC_URL_FILE = DATA_DIR / "public_url.txt"

# The rqlite HTTP endpoint backing the store.
RQLITE_URL = os.environ.get("DATA_STORE_RQLITE_URL", "http://localhost:4001")

# Presence of this file means the tunnel runs ngrok on a reserved domain, so the
# public URL is permanent; otherwise the fallback tunnel URL is only interim.
NGROK_SECRETS_FILE = Path("runtime/secrets/data_store_ngrok.env")

# Two ports: the public (tunneled) data API and the local-only admin console.
PUBLIC_PORT = int(os.environ.get("DATA_STORE_PORT", "8080"))
ADMIN_PORT = int(os.environ.get("DATA_STORE_ADMIN_PORT", "8084"))

# A single document is capped so one request can't exhaust memory or disk.
MAX_DOCUMENT_BYTES = 1_048_576  # 1 MiB

# Generous per-client request ceiling: throttles floods without impeding a
# legitimately busy agent. The 256-bit token is the real guard.
RATE_LIMIT_MAX_REQUESTS = int(os.environ.get("DATA_STORE_RATE_LIMIT", "300"))
RATE_LIMIT_WINDOW_SECONDS = 60.0

# Pagination bounds for listing documents.
DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 1000
MAX_AUDIT_ENTRIES = 500

# Names are constrained so they are safe as identifiers and can't smuggle
# anything odd through a URL. (Values are always parameterized regardless.)
_COLLECTION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_DOCUMENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

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

    Tunnels (localtunnel, ngrok) and Cloudflare put the true client address in a
    forwarded header; fall back to the socket peer for genuinely local requests.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.headers.get("Cf-Connecting-Ip") or (request.remote_addr or "unknown")


def _error(message: str, status: int) -> Response:
    response = jsonify({"error": message})
    response.status_code = status
    return response


def _require_valid_collection(collection: str) -> Response | None:
    if not _COLLECTION_PATTERN.match(collection):
        return _error("invalid collection name: use 1-64 chars of letters, digits, '-' or '_'", 400)
    return None


def _require_valid_document_id(doc_id: str) -> Response | None:
    if not _DOCUMENT_ID_PATTERN.match(doc_id):
        return _error("invalid document id: use 1-128 chars of letters, digits, '-', '_', '.' or ':'", 400)
    return None


def _parse_json_body() -> tuple[Any, Response | None]:
    """Return (parsed_body, None) or (None, error_response)."""
    raw = request.get_data()
    if len(raw) > MAX_DOCUMENT_BYTES:
        return None, _error(f"document exceeds {MAX_DOCUMENT_BYTES}-byte limit", 413)
    if not raw:
        return None, _error("request body must be non-empty JSON", 400)
    body = request.get_json(silent=True)
    if body is None:
        return None, _error("request body must be valid JSON", 400)
    return body, None


def create_app(mode: str, token_store: _TokenStore, rate_limiter: security.RateLimiter, store_url: str) -> Flask:
    """Build one of the two apps.

    ``mode`` is ``"public"`` (tunneled data API only) or ``"admin"`` (adds the
    viewer and the local-only token endpoints). Dependencies are injected so
    tests can supply their own.
    """
    if mode not in ("public", "admin"):
        raise ConfigurationError(f"unknown app mode: {mode!r}")
    is_admin = mode == "admin"
    application = Flask(f"data_store_{mode}", static_folder=None)
    application.config["MAX_CONTENT_LENGTH"] = MAX_DOCUMENT_BYTES

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

    @application.after_request
    def _after_request(response: Response) -> Response:
        # Only the public app audits: its traffic is the security-relevant,
        # internet-facing access. The admin viewer's own local calls are noise.
        if not is_admin and request.path.startswith("/api/"):
            db.record_audit(
                base_url=store_url,
                method=request.method,
                path=request.path,
                collection=request.view_args.get("collection") if request.view_args else None,
                doc_id=request.view_args.get("doc_id") if request.view_args else None,
                status=response.status_code,
                authed=bool(getattr(g, "authed", False)),
                remote=_client_id(),
            )
        return response

    @application.get("/health")
    def health() -> Response:
        return jsonify({"status": "ok"})

    @application.get("/api/collections")
    def list_collections() -> Response:
        if not _authorize():
            return _error("unauthorized", 401)
        g.authed = True
        return jsonify({"collections": db.list_collections(store_url)})

    @application.get("/api/collections/<collection>")
    def list_documents(collection: str) -> Response:
        if not _authorize():
            return _error("unauthorized", 401)
        g.authed = True
        invalid = _require_valid_collection(collection)
        if invalid is not None:
            return invalid
        limit = min(request.args.get("limit", DEFAULT_PAGE_LIMIT, type=int) or DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT)
        offset = max(request.args.get("offset", 0, type=int) or 0, 0)
        return jsonify(db.list_documents(store_url, collection, limit, offset))

    @application.post("/api/collections/<collection>")
    def create_document(collection: str) -> Response:
        if not _authorize():
            return _error("unauthorized", 401)
        g.authed = True
        invalid = _require_valid_collection(collection)
        if invalid is not None:
            return invalid
        doc_id = request.args.get("id")
        if doc_id is not None:
            invalid_id = _require_valid_document_id(doc_id)
            if invalid_id is not None:
                return invalid_id
        body, error = _parse_json_body()
        if error is not None:
            return error
        try:
            document = db.create_document(store_url, collection, body, doc_id)
        except db.DocumentConflictError as conflict:
            return _error(str(conflict), 409)
        response = jsonify(document)
        response.status_code = 201
        return response

    @application.get("/api/collections/<collection>/<doc_id>")
    def get_document(collection: str, doc_id: str) -> Response:
        if not _authorize():
            return _error("unauthorized", 401)
        g.authed = True
        for invalid in (_require_valid_collection(collection), _require_valid_document_id(doc_id)):
            if invalid is not None:
                return invalid
        document = db.get_document(store_url, collection, doc_id)
        if document is None:
            return _error("document not found", 404)
        return jsonify(document)

    @application.put("/api/collections/<collection>/<doc_id>")
    def put_document(collection: str, doc_id: str) -> Response:
        if not _authorize():
            return _error("unauthorized", 401)
        g.authed = True
        for invalid in (_require_valid_collection(collection), _require_valid_document_id(doc_id)):
            if invalid is not None:
                return invalid
        body, error = _parse_json_body()
        if error is not None:
            return error
        return jsonify(db.put_document(store_url, collection, doc_id, body))

    @application.delete("/api/collections/<collection>/<doc_id>")
    def delete_document(collection: str, doc_id: str) -> Response:
        if not _authorize():
            return _error("unauthorized", 401)
        g.authed = True
        for invalid in (_require_valid_collection(collection), _require_valid_document_id(doc_id)):
            if invalid is not None:
                return invalid
        if not db.delete_document(store_url, collection, doc_id):
            return _error("document not found", 404)
        return jsonify({"deleted": True, "collection": collection, "id": doc_id})

    @application.delete("/api/collections/<collection>")
    def delete_collection(collection: str) -> Response:
        if not _authorize():
            return _error("unauthorized", 401)
        g.authed = True
        invalid = _require_valid_collection(collection)
        if invalid is not None:
            return invalid
        removed = db.delete_collection(store_url, collection)
        return jsonify({"deleted": True, "collection": collection, "documents_removed": removed})

    if is_admin:
        _register_admin_routes(application, token_store, store_url, _authorize)

    return application


def _register_admin_routes(
    application: Flask,
    token_store: _TokenStore,
    store_url: str,
    authorize: Any,
) -> None:
    """Register the viewer and the local-only token endpoints (admin app only)."""

    @application.get("/")
    def viewer() -> Response:
        return Response((_ASSETS_DIR / "viewer.html").read_text(), mimetype="text/html")

    @application.get("/api/audit")
    def audit() -> Response:
        if not authorize():
            return _error("unauthorized", 401)
        g.authed = True
        limit = min(request.args.get("limit", 100, type=int) or 100, MAX_AUDIT_ENTRIES)
        return jsonify({"entries": db.list_audit(store_url, limit)})

    @application.get("/api/local/connection")
    def local_connection() -> Response:
        # Defense-in-depth: even on this non-tunneled port, refuse anything that
        # arrived through the workspace's own Cloudflare tunnel.
        if not security.is_local_request(request.headers):
            return _error("not available", 403)
        public_url = PUBLIC_URL_FILE.read_text().strip() if PUBLIC_URL_FILE.exists() else None
        api_base = f"{public_url}/api" if public_url else None
        return jsonify(
            {
                "token": token_store.get(),
                "public_url": public_url,
                "api_base": api_base,
                "permanent_url": NGROK_SECRETS_FILE.exists(),
                "note": "Send the token as 'Authorization: Bearer <token>'.",
            }
        )

    @application.post("/api/local/rotate")
    def local_rotate() -> Response:
        if not security.is_local_request(request.headers):
            return _error("not available", 403)
        return jsonify({"token": token_store.rotate()})


def _serve(application: Flask, port: int) -> None:
    run_simple("127.0.0.1", port, application, threaded=True, use_reloader=False, use_debugger=False)


def main() -> None:
    db.initialize_database(RQLITE_URL)
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
