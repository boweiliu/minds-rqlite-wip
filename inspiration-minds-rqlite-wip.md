---
title: minds-rqlite-wip
description: A private rqlite (SQLite-over-HTTP) database exposed to the internet behind one bearer token: run arbitrary SQL via /api, manage it in a console at /db, try a demo todo app at /demo, and read agent-facing docs at /llms.txt.
thumbnail: inspiration-minds-rqlite-wip.svg
version: v1
format: v1
---

# minds-rqlite-wip

This file is the manifest for the **minds-rqlite-wip** inspiration (slug:
`minds-rqlite-wip`). It is the one document a future agent reads to understand,
present, and adapt this inspiration. If you are an agent in a mind that was
created from this inspiration, this file is your script: read all of it, then
follow "How to adapt it" below.

## What it is

A private rqlite (SQLite-over-HTTP) database exposed to the internet behind one bearer token: run arbitrary SQL via /api, manage it in a console at /db, try a demo todo app at /demo, and read agent-facing docs at /llms.txt.

This gives the user their own small database that lives in the workspace and can
be reached from anywhere on the internet, guarded by a single secret token. The
problem it solves: you (or your own scripts, apps, and coding agents) want a
durable place to keep structured data -- notes, todos, records of any shape --
that you can read and write with plain SQL over HTTP, without standing up and
securing a database server yourself. Under the hood it runs rqlite (SQLite made
durable and reachable over HTTP), fronted by a small web app. When it is running
the user sees a workspace tab with four things: a landing page (`/`) showing the
public connection URL and a scannable QR code; a management console (`/db`, also
reachable as `/manage`) that browses the tables, runs SQL, and rotates the token;
a live demo -- a drag-and-drop todo app (`/demo`) built on the same API to prove
it works end to end; and agent-facing API docs at `/llms.txt`. External clients
hit a separate public URL and run arbitrary SQL against `/api/query`,
`/api/execute`, and `/api/request`, sending `Authorization: Bearer <token>` on
every call. There is no imposed schema -- callers create whatever tables they
want -- and the single 256-bit bearer token is the whole gate.

## How it works

The snapshot includes these paths (each is a repo-root-relative path copied
from the original mind onto a clean default-workspace-template base):

- `system/apps/data_store`
- `system/supervisord.conf`
- `pyproject.toml`
- `uv.lock`
- `system/scripts/env.d/1200-data-store.sh`

What each included path is, and its role:

- `system/apps/data_store` -- the whole feature, a Flask app. `runner.py` builds
  two apps that share one in-memory token: a **public** app and an **admin** app.
  `db.py` is the verbatim rqlite passthrough; `security.py` holds the token
  (256-bit, `secrets.token_urlsafe(32)`), the constant-time compare, the rate
  limiter, and the "is this request genuinely local" check; `tunnel.py` runs the
  public tunnel. `assets/` holds the three static pages (landing, console, demo).
  It ships with unit tests (`runner_test.py`), a ratchets file, and its own
  `README.md` / `PROGRESS.md`.
- `system/supervisord.conf` -- process supervision. It defines the three
  `data-store*` programs described below (plus the rest of the default workspace
  services). The source workspace's private github-sync program has been stripped
  from this published copy.
- `pyproject.toml` + `uv.lock` -- register `data_store` as a workspace member and
  pin its runtime deps (flask, httpx, segno for the QR code).
- `system/scripts/env.d/1200-data-store.sh` -- an env-converge unit that installs
  the two non-pip runtime binaries on boot: `rqlited` (the rqlite server, pinned
  to v10.2.7 with a checksum) and `lt` (localtunnel, via npm). `cloudflared`,
  used for the default tunnel, is already present in the workspace image.

How it wires together at runtime -- three supervisord programs:

- `data-store-rqlite` runs `rqlited`: HTTP API on localhost:4001, Raft on :4002,
  data persisted under `data/.apps/data_store/rqlite`. This is the storage engine.
- `data-store` runs the Flask app. It binds **two loopback ports**: the public
  port 8080 and the admin port 8084. The public port serves `/health` and the
  token-gated SQL passthrough (`/api/query|execute|request`, forwarded verbatim to
  rqlite) -- this is the only port the tunnel forwards to the internet. The admin
  port additionally serves the workspace-tab pages (landing `/`, console `/db`
  alias `/manage`, demo `/demo`, docs `/llms.txt`) and the local-only
  `/admin/connection` + `/admin/rotate` endpoints. The admin port is registered as
  the workspace tab via `forward_port.py` and is never tunneled.
- `data-store-tunnel` runs `tunnel.py`, exposing **only** port 8080 to the public
  internet, isolated from the workspace's own tunnel. It writes the live public
  hostname to `data/.apps/data_store/public_url.txt`, which the landing page, the
  `/admin/connection` JSON, and `/llms.txt` all read back. By default it is a
  zero-setup Cloudflare quick tunnel; localtunnel or ngrok are selectable
  alternatives.

## Recipe

This inspiration is version `v1` (front-matter `version:`).
It is not a fork of the workspace it came from -- it is DERIVED from it by the
recipe below: include these paths, leave these out, apply these
published-version rules. An update re-runs the recipe against the current
workspace and publishes the result as the next version, so anything excluded
here stays excluded even though it still exists in the source workspace. This
block is the durable home of that recipe -- a later update reads it back from
here.

```yaml
version: v1
include:
  - system/apps/data_store
  - system/supervisord.conf
  - pyproject.toml
  - uv.lock
  - system/scripts/env.d/1200-data-store.sh
data_include: []
exclude:
  - the source workspace's private github-sync supervisord program block (stripped from system/supervisord.conf)
modification_rules:
  - remove the workspace's private github-sync supervisord block from the published snapshot
```

## Prerequisites

Activation requirements: what the adopting agent must SET UP -- and must
INITIATE ITSELF during setup, before asking how to adapt -- for this
inspiration to run against the new user's own accounts/data. One line per
requirement, in this machine-readable form (greppable by `requires_`):

This app makes no third-party `latchkey curl` calls and never calls an LLM, so
there are no `requires_permission` and no `requires_llm` lines. The only
requirement is optional, and only for a permanent public URL:

- requires_secret: ngrok authtoken + reserved domain in
  `data/.secrets/data_store_ngrok.env` (as `export NGROK_AUTHTOKEN=...` and
  `export NGROK_DOMAIN=...`). Supplying this makes the public URL a fixed
  `https://<domain>` that survives restarts. Without it the app falls back to a
  zero-setup Cloudflare quick tunnel (a random `*.trycloudflare.com` hostname that
  changes on restart), so nothing has to be set up to get running.

Runtime binaries are handled automatically, not by the adopter: the env-converge
unit `system/scripts/env.d/1200-data-store.sh` installs `rqlited` (the rqlite
server, pinned v10.2.7) and `lt` (localtunnel) on boot; `cloudflared` (used for
the default tunnel) is already in the workspace image.

## How to adapt it

Instructions for the NEXT agent -- the one adapting this inspiration into a
new mind. This is the `use-inspiration` skill's template path; in short:

1. Read this entire file first, especially "Prerequisites" and "Holes"
   below -- Prerequisites are your SETUP agenda, Holes are your ADAPTATION
   agenda.
2. Present the inspiration to the user in plain, non-technical language: what
   it is, what it does, and what it needs from them (name the Prerequisites).
3. Ask whether they want to use the same connectors (e.g. their own Slack).
   If YES: ACTIVATE FIRST -- initiate every `requires_permission` line NOW
   via a latchkey permission request (see the `latchkey` skill; the request
   opens the approval/login flow in the minds app), wire up any
   `requires_secret` values, start the services, and get the app showing
   THE USER'S OWN DATA. Done for a data-backed app means the user can open it
   and see their own data -- NOT that a service starts or an endpoint returns
   200. Then tell them it is live and to take a look.
4. Only AFTER that (or immediately, if they chose different connectors -- the
   swap is then the first adaptation) ask: "How do you want to adapt it?"
5. Work through each hole interactively, one at a time. Translate each into
   plain language, ask for a decision only when you genuinely need one, and
   resolve the obvious ones yourself.
6. When done, append a dated entry to "Adaptation history" below (never
   rewrite earlier entries) and commit.

## Holes

- **Permanent public URL (the main WIP).** Out of the box the public hostname is
  not fixed: the default Cloudflare quick tunnel (and the localtunnel fallback)
  hand out a new URL on every restart, so any client that hardcoded the old URL
  breaks. A working replacement is a stable URL -- supply ngrok creds + a reserved
  domain (see Prerequisites) or wire up a named Cloudflare tunnel -- so the URL
  stays constant across restarts and container moves. Decide with the user which
  they want.
- **The store ships empty.** No tables and no rows are included -- by design,
  since there is no imposed schema. The adapter (or the user's clients) creates
  whatever tables are needed via DDL on `/api/execute`. The bundled `/demo` todo
  app creates its own `todos` table on first use, as a worked example.
- **Test coverage is partial.** The core modules have unit tests for the pages,
  the token gate, and the API wiring, but there is no exhaustive end-to-end
  integration suite against a live rqlite + tunnel. Anyone extending the SQL
  surface or the tunnel logic should add coverage for the new behavior.

## Publication history

This inspiration's changelog: what each published version changed. The PUBLISHER
appends one entry per version (newest last); earlier entries are never rewritten.
This is distinct from "Adaptation history" below, which is the ADOPTERS' log.

### v1 (2026-07-30) -- first published: the 0.3.10 (post-declutter) version of the data-store app.

## Adaptation history

Each mind that adapts this inspiration appends one dated entry below. Earlier
entries are never rewritten.
