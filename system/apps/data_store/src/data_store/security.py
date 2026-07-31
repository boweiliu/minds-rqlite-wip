"""Security primitives for the data-store service.

The whole external attack surface is guarded by one thing: a high-entropy bearer
token. This module owns it (generation, on-disk storage with strict permissions,
rotation) plus the two checks that back it up: a constant-time comparison so the
token can't be recovered by timing, and a lightweight per-client rate limiter so
a flood of guesses is throttled and logged.

It also distinguishes *local* requests (from the workspace UI, reaching the app
directly through the loopback proxy) from *public* requests (arriving through the
Cloudflare tunnel, which stamps ``Cf-Connecting-Ip``/``Cf-Ray`` headers the
client cannot forge). That distinction lets the private viewer read the token
locally while a public caller never can -- the token is only ever revealed to a
request that did not come off the internet.
"""

import hmac
import os
import secrets
import threading
import time
from collections import defaultdict
from collections import deque
from pathlib import Path

# Cloudflare stamps these on every request it proxies and strips any
# client-supplied value, so their presence is a reliable "came from the public
# internet" signal and their absence means the request reached us locally.
_CLOUDFLARE_HEADERS = ("Cf-Connecting-Ip", "Cf-Ray")

_TOKEN_FILENAME = "api_token"
# 32 bytes -> 256 bits of entropy; brute force is infeasible, which is why the
# rate limiter below is defense-in-depth rather than the primary guard.
_TOKEN_NUM_BYTES = 32


def token_path(data_dir: Path) -> Path:
    """On-disk location of the API token. This file is the ONLY place the token
    is exposed -- it is never served over HTTP. Read it to obtain the token."""
    return data_dir / _TOKEN_FILENAME


def load_or_create_token(data_dir: Path) -> str:
    """Return the persisted API token, generating and storing one on first use.

    The token file is written with owner-only (0600) permissions so it is not
    world-readable on the host.
    """
    path = token_path(data_dir)
    if path.exists():
        return path.read_text().strip()
    return rotate_token(data_dir)


def rotate_token(data_dir: Path) -> str:
    """Generate a fresh token, persist it with strict permissions, and return it."""
    data_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(_TOKEN_NUM_BYTES)
    path = token_path(data_dir)
    # Create with 0600 from the start rather than widening then narrowing.
    file_descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(file_descriptor, "w") as handle:
        handle.write(token)
    return token


def token_matches(provided: str, expected: str) -> bool:
    """Constant-time comparison so a valid token can't be recovered via timing."""
    return hmac.compare_digest(provided, expected)


def extract_bearer_token(authorization_header: str | None) -> str | None:
    """Pull the token out of an ``Authorization: Bearer <token>`` header."""
    if not authorization_header:
        return None
    parts = authorization_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()


def is_local_request(headers: object) -> bool:
    """True when the request did not arrive through the Cloudflare tunnel.

    ``headers`` is any mapping exposing ``.get`` (a Flask/Werkzeug headers
    object). A request is treated as local only when none of the Cloudflare
    edge headers are present.
    """
    get = headers.get  # type: ignore[attr-defined]
    return all(get(name) is None for name in _CLOUDFLARE_HEADERS)


class RateLimiter:
    """A thread-safe fixed-window request limiter keyed by client identifier.

    Bounds how many requests one client may make per window; used to throttle
    and surface token-guessing floods. Kept in memory -- the service is
    single-instance and this is a deterrent, not an accounting system.
    """

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def is_allowed(self, client_id: str, now: float | None = None) -> bool:
        """Record a hit for ``client_id`` and return whether it is under the limit."""
        current = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits[client_id]
            cutoff = current - self._window_seconds
            while hits and hits[0] < cutoff:
                hits.popleft()
            if len(hits) >= self._max_requests:
                return False
            hits.append(current)
            return True
