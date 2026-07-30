# data-store

A secure, SQLite-backed JSON document store with a token-protected HTTP API so
an external (non-minds) cloud agent can read and write it over the internet, plus
a private in-workspace viewer.

## Model

Named **collections**, each holding **documents** that are arbitrary JSON with a
string id and `created_at` / `updated_at` timestamps. No fixed schema; the JSON is
stored verbatim. There is no raw-SQL endpoint -- only safe, parameterized CRUD --
which keeps the external attack surface small.

## Security

- Every `/api/...` endpoint requires `Authorization: Bearer <token>`. The token
  is 256-bit, compared in constant time, and stored at
  `runtime/data-store/api_token` with `0600` permissions.
- The token can be read (and rotated) only from a **local** request -- one that
  did not arrive through the Cloudflare tunnel (Cloudflare stamps
  `Cf-Connecting-Ip`, which a public caller cannot forge). Public callers get 403
  from the token endpoints.
- The app binds to loopback only; TLS is terminated at the Cloudflare edge.
- Request bodies are capped (1 MiB) and there is a per-client rate limiter.
- Every API request is recorded in an audit log (visible in the viewer).

## External access

The `data-store-tunnel` service runs a dedicated Cloudflare tunnel for this port,
isolated from the workspace's own tunnel. Its public hostname is written to
`runtime/data-store/public_url.txt`. Being an account-less quick tunnel, the
hostname changes if the tunnel restarts; swap in a named tunnel for a permanent
URL.

## API

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness (no auth) |
| GET | `/api/collections` | List collections + counts |
| GET | `/api/collections/<c>?limit=&offset=` | List documents in a collection |
| POST | `/api/collections/<c>` (body: JSON, optional `?id=`) | Create a document |
| GET | `/api/collections/<c>/<id>` | Read a document |
| PUT | `/api/collections/<c>/<id>` (body: JSON) | Create or replace a document |
| DELETE | `/api/collections/<c>/<id>` | Delete a document |
| DELETE | `/api/collections/<c>` | Delete a whole collection |
| GET | `/api/audit?limit=` | Recent request log |

The viewer is served at `/` (workspace tab `/service/data-store/`).
