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
| After killing a spawn for off-scope behavior, check `arc log` / `git log` on the assigned branch, not just `arc status` / `git status` | `status` only shows uncommitted changes; a spawn that committed on-scope work and then drifted to off-scope work shows clean status but has a new commit. Skipping this cost a redundant $3.80 spawn that re-verified work the prior killed session had already committed. After kill: `arc log -n 5 --oneline` (or git equiv) on the assigned branch is mandatory before deciding the next move. |
| Advanced to the next plan stage (or reported the task complete) without comparing the actual outcome to the stage's `Expected result image:` | CLAUDE.md § Coordination cycle § Verification — verification has two mandatory layers: after each stage against its result image, and after the last stage against the plan's `## Final verification`. On mismatch → `overcome-difficulty`. Treat a skipped check as a difficulty itself: stop and run it before advancing. |
| Treated bare gratitude (`thanks` / `спасибо` / `perfect` alone) as resolution confirmation, or waited for user gratitude before asking | CLAUDE.md § On task resolution § Closing protocol — close the loop **proactively** the moment Final verification passes (recap + explicit ask), do not wait for gratitude. When gratitude comes, treat it as a prompt to ask, not as the answer. `scripts/hook-resolution-reminder.py` (UserPromptSubmit) emits a nudge on brief gratitude as a safety net. |
| Confirmation / apply-skip / push / resolution question asked as free text ("Применять?", "Так и оставим?", "Запушить?", "Считаем резолвнутой?") at end of turn | CLAUDE.md § Escalation to the user — `AskUserQuestion` is **mandatory** whenever the answer is binary or one-of-N you can list. "Apply this?" is not open-ended; it's exactly what the structured UI exists for. Bundle multiple end-of-turn binary decisions into a single `AskUserQuestion` call rather than splitting structured asks + free-text sign-off across the turn. Failure mode: user has to type "да" where one click would suffice, and the closing protocol's structured-confirm affordance is lost. |
| Asked the user for permission on a side-effect-free action (Read / Grep / WebFetch / `--help` / `--dry-run` / MCP `get_*` `list_*` `search_*`) or on an action explicitly declared in the approved plan's scope | CLAUDE.md § Acting without asking — side-effect-free classes and plan-scope-declared actions are pre-authorized. Re-asking erodes the "structured confirm = something I actually need to decide" affordance and wastes the user's time. Carve-outs and full class list in `memory-global/leaves/acting-without-asking.md`. |
| Burned 3+ tool calls "to be sure" about a new MCP/CLI's side-effect class | Budget is **1 lookup** (`--help` / `ToolSearch` / `Read` SKILL.md). If still unclear → `PERMISSION-REQUEST:` immediately. One click for the user beats minutes of agent thrashing. |
| Quietly expanded the approved plan's scope mid-execution ("while I was at it I fixed X in the adjacent file") | CLAUDE.md § Acting without asking — silent scope expansion is a substantive plan change, not a refinement. Stop, state the diff (was → now → why), confirm via `AskUserQuestion`. Adding a file to `Reference files` is substantive even if the added work feels obviously beneficial. |

## Optional session metric

After long sessions (>10 tool calls), one line for the user:

`Delegation: Task=N; root Edit/Write=M.`

Goal: lower M on ticket-sized tasks.
