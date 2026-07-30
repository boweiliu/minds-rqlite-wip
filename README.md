# minds-rqlite-wip

Work-in-progress mind that exposes an rqlite-backed JSON data store on a stable public URL via a token-gated API, with a management console. The permanent-tunnel (ngrok) step is not yet finished.

It runs a private JSON document store -- named collections of arbitrary-JSON
documents backed by rqlite (SQLite over HTTP, with Raft durability) -- and
exposes it to the internet through a token-gated REST API so an outside program,
like a cloud agent running elsewhere, can read and write the mind's data with a
single secret bearer token. A plain grayscale web console lets you browse
collections, reveal and rotate the connection token, and watch an activity log
of API requests. Security is structural: a public port serves only the
token-gated data API while a separate, non-tunneled admin port serves the
console and token, the 256-bit token is constant-time checked, and all SQL is
parameterized (no raw-SQL endpoint). It is a work in progress -- the store, API,
console, and security are done and verified, but making the public URL permanent
(via ngrok or a named tunnel) is still the remaining work.

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
