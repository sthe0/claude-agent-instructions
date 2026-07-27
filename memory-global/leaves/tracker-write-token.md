---
name: tracker-write-token
description: A tracker's write path may need a dedicated write-scoped token distinct from a general-purpose read-only env token; a tracker's MCP integration may expose reads only.
type: reference
created: 2026-06-16
last_verified: 2026-06-16
---

# Tracker write path — separate write token from read-only env token

Creating / editing / linking tracker issues from the shell, when the tracker distinguishes scopes:

- **Prefer a dedicated write-scoped token file** over a general-purpose env token when one exists — some tracker deployments provision a narrower-scoped credential specifically for write operations.
- **A general-purpose env token may be read-only** — it can return success on reads (`GET`/`whoami`-style calls) but **403 on writes** with a scope-required error. Do not assume read success implies write capability.
- **A tracker's MCP integration may be read-only** (Get/Search only — no Create/Comment/Link) even when a REST API supports writes. Use MCP for reads; fall back to the dedicated write-scoped token + the tracker's REST API for writes.

> verified by: a past session — the general-purpose env token 403'd on a write while the dedicated write-scoped token file succeeded on the same call.
