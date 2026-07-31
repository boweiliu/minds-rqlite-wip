# data-store

A private rqlite (SQLite-over-HTTP) database exposed to the internet behind a
single bearer token, so an external client or agent can run **arbitrary SQL**
against it, plus a management console served as a workspace tab.

## Model

There is no imposed schema and no CRUD wrapper: callers talk to rqlite's own
SQL API and create whatever tables they want. rqlite gives SQLite HTTP access
with Raft durability, so writes survive restarts. This is deliberately an
arbitrary-SQL surface -- the bearer token is the whole gate -- intended for your
own clients/agents, not anonymous public use.

## Security

- Every `/db/...` endpoint requires `Authorization: Bearer <token>`. The token
  is 256-bit, compared in constant time, and stored at
  `data/.apps/data_store/api_token` with `0600` permissions.
- **The token is never served over HTTP.** It is readable only from the on-disk
  file `data/.apps/data_store/api_token` (mode `0600`). Read that file to get the
  token; paste it into the console or into your client. No endpoint (public or
  admin) returns it.
- **Two-port split.** The public (tunneled) port serves only `/health`, the demo
  page, and the token-gated `/db/*` passthrough. The admin port (the workspace
  tab, never tunneled) additionally serves the console and a local-only endpoint
  that returns non-secret connection info (public URL, QR) plus a rotate action
  that regenerates the file without echoing the new value.
- The connection / rotate endpoints are refused (403) for any request that
  arrived through the workspace's Cloudflare tunnel (`Cf-Connecting-Ip` / `Cf-Ray`).
- **Rotation:** the console's Rotate button (local only) regenerates the file;
  re-read the file for the new token. It never appears on screen or on the wire.
- Both apps bind to loopback only; TLS is terminated at the tunnel edge.
- Request bodies are capped (1 MiB) and there is a per-client rate limiter.
- Only rqlite's SQL endpoints (`query`, `execute`, `request`) are forwarded;
  administrative endpoints (`/db/backup`, `/db/load`, node control) are not.

## External access

The `data-store-tunnel` service runs a dedicated public tunnel for the API port,
isolated from the workspace's own tunnel. Its public hostname is written to
`data/.apps/data_store/public_url.txt`. Default is a Cloudflare quick tunnel
(zero-setup, but the hostname changes on restart -- a stopgap); set
`DATA_STORE_TUNNEL_PROVIDER=localtunnel` to use localtunnel instead, or supply an
ngrok authtoken + reserved domain in `data/.secrets/data_store_ngrok.env` for a
permanent fixed URL.

## Paths

| Path | Audience | What |
|------|----------|------|
| `/`, `/home` | user | Landing page: overview, connection URL + QR, links |
| `/db` (alias `/manage`) | user | Management console: browse tables, run SQL, rotate token |
| `/demo` | user | POC client — a drag-and-drop todo app on the same API |
| `/api/*` | client/agent | The token-gated SQL API (see below) |
| `/llms.txt` | agent | Plain-text API docs for an LLM client, live URL filled in |
| `/admin/*` | local only | Non-secret connection info + token rotation (admin port) |

The workspace tab opens `/` (the landing page); from there, Manage / Demo / API docs.

## The `/api` SQL surface

The `/api/*` endpoints are a verbatim passthrough to rqlite, so its own options
(`?transaction`, `?level=strong`, `?associative`, `?timings`, ...) all work. See
the [rqlite API docs](https://rqlite.io/docs/api/).

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness (no auth) |
| GET | `/api` | JSON index of endpoints |
| GET/POST | `/api/query` | Run read statements (SELECT/PRAGMA/...) |
| POST | `/api/execute` | Run write statements (INSERT/UPDATE/DDL/...) |
| POST | `/api/request` | Unified read+write in one call |

Example:

```bash
curl -H "Authorization: Bearer <token>" \
     -d '[["CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)"]]' \
     https://<public-url>/api/execute
curl -H "Authorization: Bearer <token>" \
     -d '[["SELECT * FROM notes"]]' \
     'https://<public-url>/api/query?associative'
```
