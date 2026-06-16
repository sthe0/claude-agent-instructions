---
name: tracker-write-token
description: Yandex Tracker (Startrek) writes need ~/.tracker-token, not $OAUTH_TOKEN; MCP tracker tools are read-only.
type: reference
---

# Tracker (Startrek) write path on this machine

Creating / editing / linking Yandex Tracker issues from the shell:

- **Use the token in `~/.tracker-token`** (a dedicated file, OAuth scheme).
  ```bash
  TT=$(cat ~/.tracker-token | tr -d '\n\r ')
  curl -s -H "Authorization: OAuth $TT" ...  https://st-api.yandex-team.ru/v2/...
  ```
- **`$OAUTH_TOKEN` (env) is read-only** — it returns 200 on `/v2/myself` and GETs but **403 on writes** with
  `"startrek:write OAuth scope is required"`. Do not use it for POST/PATCH.
- **MCP `mcp__tracker__*` tools are read-only** (Get/Search only — no Create/Comment/Link). Use them for reads;
  for writes fall back to `~/.tracker-token` + REST.

API quick refs:
- Create issue: `POST /v2/issues` body `{"queue":"DEEPAGENT","type":{"key":"task"},"assignee":"the0","summary":..,"description":..}` → 201, returns `key`.
- Link issues: `POST /v2/issues/<KEY>/links` body `{"relationship":"relates","issue":"<OTHER-KEY>"}`. 422 with
  "уже связаны" means the link already exists (idempotent-safe to ignore).

> verified by: this session 2026-06-14 (created DEEPAGENT-430, $OAUTH_TOKEN 403'd, ~/.tracker-token 201'd).
