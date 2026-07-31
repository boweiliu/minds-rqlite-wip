# data-store — progress & handoff

> **Note (2026-07-30 adaptation):** this doc describes the ORIGINAL design, whose
> public surface was a bespoke `/api/collections` CRUD wrapper. On adoption that
> was scratched: the store now exposes rqlite's **native SQL API** (arbitrary
> SQL) at `/db/query|execute|request`, bearer-token gated, and the public tunnel
> defaults to a Cloudflare quick tunnel. See `README.md` and the manifest's
> Adaptation history for the current state. The security model (two-port split,
> local-only token reveal) below still applies.

**Goal:** expose the workspace's SQLite/rqlite data store on a **stable, permanent
public URL** so external (non-minds) cloud agents can read/write it securely.
Data and code stay in this workspace; only a tunnel forwards to it.

**Status:** the store is built, secure, and running on an *interim* URL. The one
remaining piece is making the URL **permanent**. This doc is the resume point.

---

## What's built and working

A `data-store` app (`system/apps/data_store/`) running as three supervisord services:

| Service | What it is |
|---|---|
| `data-store-rqlite` | `rqlited` on `localhost:4001` (raft `:4002`), data under `data/.apps/data_store/rqlite`. The storage engine. |
| `data-store` | The Flask app. Runs **two** ports (see security below). |
| `data-store-tunnel` | The public tunnel (localtunnel now; ngrok-ready — see below). |

Code map:
- `src/data_store/db.py` — rqlite HTTP storage layer (collections of JSON docs; all statements parameterized; no raw-SQL endpoint).
- `src/data_store/security.py` — 256-bit bearer token (constant-time compare), rate limiter, local-vs-tunnel request detection.
- `src/data_store/runner.py` — builds two apps via `create_app(mode)`.
- `src/data_store/tunnel.py` — dual-mode tunnel: **ngrok** when configured, else **localtunnel** fallback.
- `src/data_store/assets/viewer.html` — the grayscale management console.

Data model: named **collections** of arbitrary-JSON **documents** (string id +
created/updated timestamps), stored verbatim. CRUD API only.

## Security model (structural)

Two ports, so the token is never on the internet-facing surface:
- **Public port `8080`** (the one the tunnel forwards): only `/health` + token-gated
  `/api/collections/...`. No viewer, no token endpoint.
- **Admin port `8084`** (registered as the workspace tab via `forward_port`; **not**
  tunneled): the viewer + `/api/local/connection` + `/api/local/rotate`. Token-reveal
  endpoints additionally refuse any request bearing Cloudflare edge headers
  (defense-in-depth for the workspace's own tunnel).

Other: token file `data/.apps/data_store/api_token` (mode `0600`); 1 MiB body cap;
per-client rate limit; audit log (in the console's Activity tab); loopback binds;
TLS terminated at the tunnel edge.

**Security history:** an earlier version keyed token-hiding off Cloudflare headers
and, after switching to localtunnel, leaked the token publicly. That was found by
active attack and fixed via the two-port split above; re-verified (public
token/rotate/viewer/audit all 404; no/wrong token 401; real token 200).

## Current live state

- Interim public URL via **localtunnel** (written to `data/.apps/data_store/public_url.txt`).
  It is **not** stable — the hostname can change on restart. That's the whole
  remaining problem.
- The live token is shown in the console's **Connection** tab. (Several tokens
  appeared in chat during testing and were rotated away — trust only the console.)

---

## What remains — making the URL permanent

A permanent URL needs an **account-backed tunnel**. Chosen approach: **ngrok**
(free tier includes one permanent static domain). The service is already wired:
`tunnel.py` reads `data/.secrets/data_store_ngrok.env` with:

```
export NGROK_AUTHTOKEN="..."
export NGROK_DOMAIN="your-domain.ngrok-free.app"
```

When that file exists, the tunnel runs ngrok on the reserved domain (permanent);
otherwise it falls back to localtunnel. So finishing =

1. **Get ngrok set up** (needs the user's ngrok **API key** once). Two ways:
   - **Direct:** user provides an ngrok authtoken + reserved domain → write the env
     file → restart `data-store-tunnel`.
   - **Automated (in progress):** use the **printbridge** launcher on the user's Mac
     to run the ngrok/latchkey setup so the user doesn't copy-paste (see below).
2. **Reserve the domain + mint a tunnel authtoken** via ngrok's API
   (`POST /reserved_domains`, `POST /credentials`), then write the env file.
3. **Restart `data-store-tunnel`**, then re-run the attack suite + end-to-end test
   against the permanent URL.

### The automation path (printbridge) — already unblocked

The user has a `launchd` command-runner on their Mac ("printbridge",
`com.minds.printbridge`) installed by another agent (now the `local-print-bridge`
skill). Mechanism: drop a `*.cmd` shell script into
`~/tmp/minds_data/printbridge/cmd/`; it runs within ~15s and writes output to
`printbridge/cmd_done/<name>.cmd.out`. **This agent has been granted file-server
write access to `/Users/bowei/tmp/minds_data`** (WebDAV via
`latchkey curl .../minds-api-proxy/api/v1/files/Users/bowei/tmp/minds_data/...`).

So we can run setup commands on the user's Mac with no copy-paste. The only value
still needed from the user is their ngrok **API key** (dropped into a file, not
pasted as commands).

### latchkey ngrok integration (validated, optional)

`latchkey services register ngrok --base-api-url https://api.ngrok.com/` +
`latchkey auth set ngrok -H "Authorization: Bearer <API_KEY>" -H "Ngrok-Version: 2"`
makes latchkey inject the key into ngrok API calls (validated end-to-end with a
dummy key). **Caveat:** using a service through latchkey also needs a **per-agent
permission**, and custom-registered services are **not** in the permission catalog
— so the grant path for a custom service is unresolved. The printbridge path
avoids this.

## Also outstanding (hardening)

- No unit tests yet for `db.py` / `security.py` / `runner.py` — only the ratchets
  (`test_data_store_ratchets.py`, 14 passing). Verification so far is manual +
  live attack. Add real tests and run the review gate before calling it done.

## How to resume

1. Check services: `supervisorctl status data-store data-store-rqlite data-store-tunnel`.
2. Confirm the store answers: `curl -s localhost:8080/health`, and the console tab.
3. Pick up at "What remains" step 1 — get the ngrok API key from the user and
   drive setup through printbridge.
