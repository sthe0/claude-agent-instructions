---
name: user-ask-calibration-opencode-gates
description: "Standing user grant (the0): memory edits never warrant an AskUserQuestion — apply silently, mention in one line; and the two-layer distinction: a repeating permission dialog is OpenCode's native edit/write/patch gate (default ask), not the agent asking — the fix is the user allowlist, not an apology"
type: feedback
created: 2026-11-28
last_verified: 2026-11-28
---

Two corrections from the0 on 2026-11-28, one per prompt layer:

**Agent layer (AskUserQuestion).** Standing grant, all projects: never compose an ask whose subject is a memory edit — which leaf to update, whether to publish it, whether the edit is OK. Apply the edit, mention it in one line in the reply. Memory writes are exempt from the plan-approval spine by design (CLAUDE.md), so an ask adds user friction with zero risk reduction. The project-level "ask less" calibration lives in the deepagent project leaf `feedback_fewer_autonomous_asks.md`.

**Harness layer (native dialogs).** OpenCode's tool-permission dialogs are NOT agent asks. The Core runtime config (`/tmp/core-oc-*/runtime/config.json`) defaults `edit|write|patch *` → `ask`, and the user allowlist initially carried only `shell` rules — so every file edit outside an explicit allow produced a native confirm the user correctly read as "the agent asking again". Fix: extend the allowlist at `~/.config/opencode/opencode.json`; 2026-11-28 added 21 rules (edit/write/patch × 7 path globs: the robot/deepagent workspace, both task-mounts junk trees, `~/.claude-agent`, core-opencode state, `/place/vartmp`, `/tmp`). Rules of thumb: check WHICH layer is speaking (model-composed ask vs native dialog) before answering a "why do you keep asking" question; when a native prompt repeats for an operation the user has sanctioned, extend the allowlist within the sanctioned scope only — never self-widen beyond what the user asked.

OpenCode permission syntax (from the `https://opencode.ai/config.json` schema): each rule is `{action, resource, effect}` with `resource` a glob; `permissions` is a plain array; `edit`/`write`/`patch` are separate actions from `shell`.

Deferred (2026-11-28, cost vs benefit): a Core `CLAUDE.md` line "memory edits never warrant asks" — the spine exemption already implies it, the failure mode is a once-corrected user preference, and an always-loaded line costs every session for it; re-raise only if the ask recurs in a project that has read this leaf.