---
title: minds-rqlite-wip
description: Work-in-progress mind that exposes an rqlite-backed JSON data store on a stable public URL via a token-gated API, with a management console. The permanent-tunnel (ngrok) step is not yet finished.
thumbnail: inspiration-minds-rqlite-wip.svg
format: v1
---

# minds-rqlite-wip

This file is the manifest for the **minds-rqlite-wip** inspiration (slug:
`minds-rqlite-wip`). It is the one document a future agent reads to understand,
present, and adapt this inspiration. If you are an agent in a mind that was
created from this inspiration, this file is your script: read all of it, then
follow "How to adapt it" below.

## What it is

Work-in-progress mind that exposes an rqlite-backed JSON data store on a stable public URL via a token-gated API, with a management console. The permanent-tunnel (ngrok) step is not yet finished.

This is a mind that runs its own private JSON document store and safely exposes
it to the wider internet. The problem it solves: a mind's data normally lives
locked inside its workspace, so an outside program -- for example a cloud agent
running somewhere else -- has no way to read or write it. This inspiration gives
the mind a small database of named **collections** of JSON **documents**, backed
by rqlite (SQLite spoken over HTTP, with Raft durability so writes survive
restarts), and puts a token-gated REST API in front of it (`/api/collections/...`)
so an external caller can do CRUD over the internet using a single secret bearer
token. Alongside the API it produces a plain grayscale **web console**: a
collections browser (click a collection, see its documents as rendered JSON), a
connection panel that reveals the public URL and the current token (with a
one-click rotate), and an activity log of recent API requests. Security is
structural rather than bolted on: the public, tunneled port serves *only* the
token-gated data API, while a separate, non-tunneled admin port serves the
console and the token-reveal endpoints; the 256-bit token is compared in constant
time, request bodies are capped and rate-limited, and every SQL statement is
parameterized (there is deliberately no raw-SQL endpoint). When it is running the
user opens the console tab, copies the connection details, and hands the URL +
token to whatever external agent needs to share state with the mind.

## How it works

The snapshot includes these paths (each is a repo-root-relative path copied
from the original mind onto a clean default-workspace-template base):

- `libs/data_store`

`libs/data_store` is the entire feature -- a single Python lib (workspace member
`data-store`) with all of the code, the console, its README, ratchet tests, and
`PROGRESS.md` (the authoritative status/handoff doc; read it for full detail).
Its source map:

- `src/data_store/db.py` -- the rqlite HTTP storage layer. Talks to `rqlited`
  over `http://localhost:4001`, models collections of arbitrary-JSON documents
  (string id + `created_at`/`updated_at`), and runs only parameterized SQL.
- `src/data_store/security.py` -- the 256-bit bearer token (generated once,
  stored at `runtime/data-store/api_token` mode `0600`, constant-time compared),
  a per-client rate limiter, and the local-vs-tunnel request detection that
  decides whether a caller is allowed to see the token.
- `src/data_store/runner.py` -- builds two Flask apps via `create_app(mode)` and
  serves them on two loopback ports.
- `src/data_store/tunnel.py` -- the public tunnel, dual-mode: ngrok on a reserved
  domain when configured, otherwise a zero-setup localtunnel fallback.
- `src/data_store/assets/viewer.html` -- the self-contained grayscale console.

At runtime three supervisord programs (added to `supervisord.conf`) wire it
together:

- `data-store-rqlite` runs `rqlited` (HTTP on `localhost:4001`, Raft on
  `localhost:4002`, data under `runtime/data-store/rqlite`) -- the storage engine.
- `data-store` runs the Flask app on **two** ports. The **public port `8080`**
  serves only `/health` and the token-gated `/api/collections/...` -- this is the
  port the tunnel forwards. The **admin port `8084`** serves the console plus the
  local-only `/api/local/connection` and `/api/local/rotate` token endpoints; the
  program registers `8084` as the workspace tab through
  `scripts/forward_port.py --url http://localhost:8084 --name data-store`, so
  `8084` (not the public API) is what the user sees in-workspace. The
  token-reveal endpoints additionally refuse any request carrying tunnel/edge
  headers, as defense-in-depth.
- `data-store-tunnel` forwards the public port `8080` to the internet, writing
  the resulting URL to `runtime/data-store/public_url.txt`.

## Prerequisites

Activation requirements: what the adopting agent must SET UP -- and must
INITIATE ITSELF during setup, before asking how to adapt -- for this
inspiration to run against the new user's own accounts/data. One line per
requirement, in this machine-readable form (greppable by `requires_`):

The app itself makes no `latchkey curl` calls to any third-party service, so
there are **no `requires_permission` lines**. The only secret is optional -- it
is what upgrades the impermanent fallback URL to a stable permanent one:

- requires_secret: ngrok authtoken + reserved domain, written as
  `export NGROK_AUTHTOKEN="..."` and `export NGROK_DOMAIN="your-domain.ngrok-free.app"`
  in `runtime/secrets/data_store_ngrok.env`. When present, the tunnel runs ngrok
  on that reserved domain (permanent URL); when absent, it falls back to a
  zero-setup localtunnel URL that is not stable across restarts.

The services also depend on three external binaries that are **not**
pip-installable and must be present on the host at runtime:

- `rqlited` (the rqlite server) -- required by `data-store-rqlite`.
- `lt` (localtunnel, installed via npm: `npm install -g localtunnel`) -- required
  by the default `data-store-tunnel` fallback.
- `ngrok` -- required only if you take the permanent-URL path above.

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

This is deliberately a work-in-progress ("wip") inspiration: the store, the
token-gated API, the console, and the security model are done and verified; the
**permanent public URL is the unfinished part**.

- **The permanent URL is not wired up (the main remaining work).** Out of the
  box the tunnel runs on an impermanent localtunnel hostname that can change on
  every restart, which is useless for anything long-lived. A working replacement
  is an account-backed tunnel: supply an ngrok authtoken + reserved domain (see
  Prerequisites) so `tunnel.py` runs ngrok on a fixed domain, or swap in a
  Cloudflare named tunnel. `tunnel.py` already branches on the ngrok config file,
  so finishing is mostly a matter of provisioning the account and dropping in the
  env file. See `libs/data_store/PROGRESS.md` for the detailed to-do (including an
  automation path via the user's Mac).
- **No unit tests for the core modules.** `db.py`, `security.py`, and `runner.py`
  have only ratchet coverage (`test_data_store_ratchets.py`); verification so far
  is manual plus live attack testing. Real unit/integration tests should be added
  before calling the store production-ready.
- **The store ships empty.** There are no seeded collections or documents -- the
  adopting mind starts with a blank database and decides what to put in it.

## Adaptation history

Each mind that adapts this inspiration appends one dated entry below. Earlier
entries are never rewritten.
