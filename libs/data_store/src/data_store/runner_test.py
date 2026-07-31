"""Unit tests for the two-app SQL store: the served pages, the token gate, the
/api passthrough wiring, and the local-only admin endpoints. rqlite is never
contacted -- the forwarder is stubbed -- so these are fast and hermetic."""

import json
from pathlib import Path

import httpx
import pytest

from data_store import runner
from data_store import security


def _make_apps(tmp_path: Path, forwarder=None):
    token_store = runner._TokenStore(tmp_path)
    rate_limiter = security.RateLimiter(1000, 60.0)
    kwargs = {} if forwarder is None else {"forwarder": forwarder}
    public = runner.create_app("public", token_store, rate_limiter, "http://rqlite.invalid", **kwargs)
    admin = runner.create_app("admin", token_store, rate_limiter, "http://rqlite.invalid", **kwargs)
    return token_store, public, admin


def test_unknown_mode_is_rejected(tmp_path: Path) -> None:
    token_store = runner._TokenStore(tmp_path)
    rate_limiter = security.RateLimiter(10, 60.0)
    with pytest.raises(runner.ConfigurationError):
        runner.create_app("sideways", token_store, rate_limiter, "http://rqlite.invalid")


def test_health_needs_no_auth(tmp_path: Path) -> None:
    _, public, _ = _make_apps(tmp_path)
    response = public.test_client().get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/", "/home", "/db", "/manage", "/demo"])
def test_pages_are_served_as_html(tmp_path: Path, path: str) -> None:
    _, public, _ = _make_apps(tmp_path)
    response = public.test_client().get(path)
    assert response.status_code == 200
    assert response.mimetype == "text/html"


def test_llms_txt_is_agent_docs(tmp_path: Path) -> None:
    _, public, _ = _make_apps(tmp_path)
    response = public.test_client().get("/llms.txt")
    assert response.status_code == 200
    assert response.mimetype == "text/plain"
    body = response.get_data(as_text=True)
    assert "/api/query" in body
    assert "Authorization: Bearer" in body


def test_api_index_lists_endpoints(tmp_path: Path) -> None:
    _, public, _ = _make_apps(tmp_path)
    payload = public.test_client().get("/api").get_json()
    assert set(payload["endpoints"]) == {"query", "execute", "request"}
    assert payload["docs"] == "/llms.txt"


def test_api_query_without_token_is_unauthorized(tmp_path: Path) -> None:
    forwarded = []

    def _spy(*args):
        forwarded.append(args)
        return httpx.Response(200, json={"results": []})

    _, public, _ = _make_apps(tmp_path, forwarder=_spy)
    response = public.test_client().post("/api/query", json=[["SELECT 1"]])
    assert response.status_code == 401
    # The gate must reject before the request ever reaches rqlite.
    assert forwarded == []


def test_api_query_with_token_is_forwarded(tmp_path: Path) -> None:
    captured = {}

    def _spy(base_url, endpoint, method, query_string, body):
        captured.update(base_url=base_url, endpoint=endpoint, method=method, query_string=query_string, body=body)
        return httpx.Response(200, json={"results": [{"rows": []}]}, headers={"content-type": "application/json"})

    token_store, public, _ = _make_apps(tmp_path, forwarder=_spy)
    client = public.test_client()
    response = client.post(
        "/api/query?associative",
        data=json.dumps([["SELECT 1"]]),
        headers={"Authorization": f"Bearer {token_store.get()}", "Content-Type": "application/json"},
    )
    assert response.status_code == 200
    # The Flask endpoint name maps to rqlite's /db/<endpoint> in db.forward.
    assert captured["endpoint"] == "query"
    assert captured["query_string"] == "associative"
    assert json.loads(captured["body"]) == [["SELECT 1"]]


def test_only_sql_endpoints_are_exposed(tmp_path: Path) -> None:
    # Only query/execute/request under /api are routed; the old /db/* paths and
    # non-SQL rqlite endpoints are plain 404s.
    _, public, _ = _make_apps(tmp_path)
    client = public.test_client()
    assert client.post("/api/backup").status_code == 404
    assert client.post("/db/query", json=[["SELECT 1"]]).status_code == 404


def test_connection_never_returns_the_token(tmp_path: Path) -> None:
    _, _, admin = _make_apps(tmp_path)
    client = admin.test_client()
    local = client.get("/admin/connection")
    assert local.status_code == 200
    payload = local.get_json()
    # The token is only readable from the file, never over HTTP.
    assert "token" not in payload
    assert payload["token_file"].endswith("api_token")
    # A request bearing a Cloudflare edge header is treated as non-local.
    through_edge = client.get("/admin/connection", headers={"Cf-Ray": "abc-EWR"})
    assert through_edge.status_code == 403


def test_public_app_has_no_admin_endpoints(tmp_path: Path) -> None:
    _, public, _ = _make_apps(tmp_path)
    assert public.test_client().get("/admin/connection").status_code == 404
    assert public.test_client().post("/admin/rotate").status_code == 404


def test_rotate_regenerates_file_without_revealing(tmp_path: Path) -> None:
    token_store, _, admin = _make_apps(tmp_path)
    before = security.token_path(tmp_path).read_text()
    rotated = admin.test_client().post("/admin/rotate")
    assert rotated.status_code == 200
    body = rotated.get_json()
    # Rotation confirms the deed but never returns the new token.
    assert body["rotated"] is True
    assert "token" not in body
    after = security.token_path(tmp_path).read_text()
    assert after != before
    # The in-memory copy is kept in sync with the file.
    assert token_store.get() == after
