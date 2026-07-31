"""Thin passthrough to the local rqlite HTTP API.

The store exposes rqlite's *native* SQL API (arbitrary SQL) to token-authed
callers, rather than a bespoke CRUD wrapper. This module does two things: wait
for ``rqlited`` to come up at startup, and forward a ``/db/<endpoint>`` request
to it verbatim so callers speak rqlite's own request/response format and can use
standard rqlite tooling. No schema is imposed -- clients create whatever tables
they want.
"""

import httpx

_TIMEOUT_SECONDS = 30.0

# The rqlite endpoints we forward: the SQL surface only. Administrative endpoints
# (/db/backup, /db/load, /remove, ...) are deliberately not exposed.
ALLOWED_ENDPOINTS = ("query", "execute", "request")


def wait_for_rqlite(base_url: str) -> None:
    """Block until rqlite is ready, tolerating it still opening its port.

    Uses a connection-retrying client so startup order (this app vs ``rqlited``)
    does not matter; httpx backs off between connect attempts internally, so no
    manual sleep loop is needed.
    """
    transport = httpx.HTTPTransport(retries=10)
    with httpx.Client(transport=transport, timeout=_TIMEOUT_SECONDS) as client:
        client.get(f"{base_url}/readyz").raise_for_status()


def forward(base_url: str, endpoint: str, method: str, query_string: str, body: bytes) -> httpx.Response:
    """Forward one ``/db/<endpoint>`` request to rqlite and return its raw response.

    The caller has already checked ``endpoint`` against ``ALLOWED_ENDPOINTS``.
    Query string and body are passed through untouched so rqlite's own options
    (``?transaction``, ``?level=strong``, ``?associative``, ``?timings``, ...)
    work exactly as documented.
    """
    url = f"{base_url}/db/{endpoint}"
    if query_string:
        url = f"{url}?{query_string}"
    return httpx.request(
        method,
        url,
        content=body,
        headers={"Content-Type": "application/json"},
        timeout=_TIMEOUT_SECONDS,
    )
