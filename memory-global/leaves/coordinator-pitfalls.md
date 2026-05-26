---
name: coordinator-pitfalls
description: Anti-patterns the root coordinator must avoid, with the corrective action for each. Read when sensing drift in your own coordination behavior, or when reviewing a session.
type: reference
---

# Typical coordinator pitfalls

## Anti-patterns

| Symptom | Better |
|---|---|
| Root does most edits via Bash/Edit/Write | `Task → developer` for code; invoke `overcome-difficulty` skill when stuck |
| Extend existing pattern (keyword scoring, regex routing, hand-coded heuristics) without checking it still fits — especially when an LLM is now in the pipeline reading the same data | Audit pattern fit before extending. In LLM-mediated pipelines, prefer natural-language prefs and let the LLM filter semantically — keyword lists are usually pre-LLM holdovers. |
| Fabricate "reasonable defaults" in a user-facing artifact (config field, description, padded list, helpful-sounding example) the user never requested | Include only what was stated or strictly required. Empty / ask / minimum and flag the gap — never silently extrapolate user preferences. |
| Full pipeline rerun to debug one stage | Minimal retest; read project memory runbook before re-launching |
| Second CLI binary for one-off experiment | Local script/stash; one entry point in the repo |
| User feedback on process → only apology | Invoke `self-improvement` skill in the same turn |
| User asks "why no self-improvement?" / confirms "run it" | Invoke `self-improvement` skill in the same turn — the reminder is feedback |
| Conditional "if not done" + artifact missing in repo → redo without status check | Read org runbook first; closed/done may mean stop — ask the user |
| VCS branch/commit on a scoped task without user ask | Read-only + report until the user confirms scope |
| Unknown internal term → guess in code | Infra-consultant subagent if present in `~/.claude/agents/`, else intrasearch / domain MCP |
| Domain runbook pasted into a generic agent prompt or `CLAUDE.md` | Memory leaf (global or project) and link in the plan |
| Substantive task confirmed-resolved after the first sub-stage only (plan written → "task resolved?") | Re-read the original user message. Resolution = X done, not plan-X done. Ask "first sub-stage accepted?" and continue execution on confirmation. |
| Misapply "don't fabricate" by **deleting** the field instead of asking for a source | "Don't fabricate" includes "ask or find a source". Removing a required field (estimate, threshold, deadline) is a regression — when the plan format requires it, `CLARIFY:` / ask user instead. |
| Proceed on an action that depends on a file after a system-reminder said the file changed, without re-reading it | When a `<system-reminder>` reports a file was modified — **Read it before any next action that depends on its prior contents**. Skipping cost a full spawn budget on one occasion (developer child accumulated 48 permission_denials at $3.05 because reverted `settings.local.json` was assumed still patched). |
| Launch a long-running spawn (`claude -p`, especially `kind=developer` with `--permission-mode bypassPermissions`) and not check on it for tens of minutes | Periodically inspect the spawn's transcript while it runs (rule of thumb: every ~5 min for developer spawns). The transcript jsonl is the freshest `.jsonl` under `~/.claude/projects/<sanitized-cwd>/` (find by mtime). Look for: wrong `cwd`, writes/commits outside the assigned mount, `ya package`/`docker push`/smoke-tests of unrelated tickets. **Kill early** if the spawn diverges; reusing the budget on rescoping a runaway is cheaper than waiting for it to exhaust. Observed cost: ~$3 burned with zero on-target artifacts when developer cd'd into a "reference" mount and started building there. |
| Use `--permission-mode bypassPermissions` as a blanket "trusted local" grant without write-scope constraint in the prompt | bypassPermissions removes all harness write barriers, so the child can `cd` and `arc commit` anywhere the kernel lets it. The dossier/prompt must contain explicit hard-deny list ("no cd / no Write / no arc commit outside `<mount>`") plus a self-check at session start (`pwd` ⊆ expected mount). Without that the child treats reference mounts as fair game for "understanding through execution". |

## Optional session metric

After long sessions (>10 tool calls), one line for the user:

`Delegation: Task=N; root Edit/Write=M.`

Goal: lower M on ticket-sized tasks.
