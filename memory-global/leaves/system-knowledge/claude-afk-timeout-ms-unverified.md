---
name: claude-afk-timeout-ms-unverified
description: CLAUDE_AFK_TIMEOUT_MS (set to 2e9 in settings/base.json to disable the ~60s AskUserQuestion no-response auto-proceed) is NOT documented in any Claude Code primary source (docs, settings.md, CHANGELOG.md, GitHub) — treat as an unverified/possible-dead-config env var; its only asserted purpose comes from the repo's own commit cbe290f, not from authoritative docs. Verify empirically before relying on it.
type: reference
schema: leaf/v1
created: 2026-07-02
last_verified: 2026-07-02
---

# CLAUDE_AFK_TIMEOUT_MS is an unverified (possibly dead) config env var

## Difficulty

Desired: every env var pinned in `settings/base.json` has a known, verified effect. Actual: `CLAUDE_AFK_TIMEOUT_MS` (set to `2000000000` — see the `env` block, intent per commit `cbe290f` "disable AskUserQuestion AFK auto-resolve timeout") cannot be confirmed to do anything. When AskUserQuestion goes unanswered the harness auto-proceeds after ~60s ("No response after 60s — the user may be away from keyboard…"); the var is meant to lengthen/disable that window, but nothing verifies it works.

Two facts establish the risk:
- **No consumer in the repo.** `CLAUDE_AFK_TIMEOUT_MS` appears ONLY in `settings/base.json`. No script/hook reads it (`hook-answer-delivery-reminder.py` merely *detects* the harness's "No response after" marker; it does not produce or control the timeout). So the var only matters if the Claude Code harness itself honors it.
- **No documentation anywhere.** Not in the official docs map, `settings.md` (which lists ~25+ other env vars incl. `BASH_DEFAULT_TIMEOUT_MS`), `hooks.md`, the `anthropics/claude-code` `CHANGELOG.md` (4679 lines, zero "afk" matches), or web/GitHub search for the literal string. The closest feature request, [issue #30740](https://github.com/anthropics/claude-code/issues/30740) ("configurable AskUserQuestion timeout"), was closed **not planned** — suggesting no such knob shipped.

So its purpose is asserted only by the repo's own commit message + the variable name, not by an independent source. It may be (a) real-but-undocumented/internal, (b) an experimental-build artifact, or (c) a no-op. The 60s auto-proceed itself is a Claude Code **harness** behavior, not ccgram (ccgram's autoclose only closes Telegram topics, `AUTOCLOSE_DEAD/DONE_MINUTES` 10/30 min — a different mechanism).

> verified by: claude-code-guide subagent web research 2026-07-02 (session 5108f108) — checked docs.claude.com map, settings.md, hooks.md, GitHub CHANGELOG.md, issues #30740/#70294; no primary source mentions the var. Also `grep -rn CLAUDE_AFK_TIMEOUT_MS` across the repo → only `settings/base.json`.

## Guidance

- Do NOT treat a live-session AFK timeout as "fixed" just because this var is set. Env is read at session start, so any change only takes effect in a **new** session; and the var may be inert.
- **Empirical test:** in a fresh session, pose an AskUserQuestion and leave it unanswered. Still "No response after 60s" ⇒ the var is a no-op ⇒ it is dead config. Then either remove it from `settings/base.json` (cleanup) or file a doc-gap via `/feedback`. If the timeout is genuinely gone ⇒ it works; update this leaf with the confirming client version.
- When reporting on any env var pinned in `base.json`, state the reliability tier: repo-asserted (commit/comment) vs your training data vs live-doc-verified. `CLAUDE_AFK_TIMEOUT_MS` is repo-asserted only.
- The user chose (2026-07-02) to keep the var pending an empirical check rather than remove it now.

## See also

- [[2026-07-01-additive-settings-merge-cannot-prune-install-time-invariant]] — same `base.json` env-propagation path; this var was absent from the live file until `apply-settings.sh` was re-run (add-when-absent merge).
- [[claude-code-drops-pre-tool-call-text]] — another empirically-established Claude Code harness behavior not in the changelog.
