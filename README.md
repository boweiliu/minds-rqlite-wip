# minds-rqlite-wip

A private rqlite (SQLite-over-HTTP) database exposed to the internet behind one bearer token: run arbitrary SQL via /api, manage it in a console at /db, try a demo todo app at /demo, and read agent-facing docs at /llms.txt.

This is a self-contained SQL database that lives in your workspace and can be
reached from anywhere on the internet, guarded by a single secret bearer token.
It runs rqlite (SQLite made durable and reachable over HTTP) behind a small Flask
app: external clients and coding agents run arbitrary SQL against `/api`, while a
workspace tab gives you a landing page with the live connection URL and QR code,
a management console at `/db` to browse tables and run SQL and rotate the token,
a drag-and-drop todo `/demo` built on the same API, and agent-facing docs at
`/llms.txt`. There is no imposed schema -- callers create whatever tables they
need -- and the 256-bit token is the whole gate.

This repository is a published **minds inspiration**: a clean, bootable
snapshot of the apps and features a mind built, ready to adapt into your own.
It is NOT the generic workspace template -- it is this specific project.

## Use it

- **Create a new mind from it:** point a new minds workspace at this repo's
  URL. On first boot the mind reads the inspiration and helps you connect your
  own accounts and adapt it.
- **Bring it into an existing mind:** run `/use-inspiration <this repo's URL>`.

## What's inside

- **minds-rqlite-wip** -- [`inspiration-minds-rqlite-wip.md`](inspiration-minds-rqlite-wip.md) (published now)

Each `inspiration-<slug>.md` is the full manifest for that inspiration: what
it is, how it works, the prerequisites it needs, and how to adapt it.
