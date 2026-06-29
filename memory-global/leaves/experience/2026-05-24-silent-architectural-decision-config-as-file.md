---
name: 2026-05-24-silent-architectural-decision-config-as-file
description: Difficulty — making an architectural/scope decision silently inside the implementation turn (a consequence-of-a-change, or a design fork like "config" placement) instead of surfacing it for turn-1 approval. Plus the embedded-prompt literal-drift trap.
type: reference
schema: difficulty/v1
resolution_confirmed_by_user: "<retroactive: rule introduced 2026-05-26; original confirmation not captured at write time>"
refs: [2026-05-26-agent-system-plan-vs-reality-drift]
created: 2026-06-11
last_verified: 2026-06-11
---

# Silent architectural decision + "config" means a file

## Difficulty
While implementing an approved change, the agent makes a *second* decision without surfacing it: a consequence-of-the-change that is itself a choice (e.g. rescaling `loop-sensitivity-depth` from 5→2 and the OD budget 5.00→3.00 as a "natural" side-effect of capping max-depth), or a non-obvious design fork resolved by guess (putting a constants table *inside* CLAUDE.md after the user said "вынести в конфиг", then arguing back instead of proposing both options). The user catches each as "you changed X without telling me."

## Order & criterion
Any consequence-of-a-change that is itself a decision, and any non-obvious design fork, gets a turn-1 proposal before it lands — even mid-session, even when the user's message said "do it differently" (that is not pre-approval for the *specific* design). Default "config" to a **separate file** under git, symlinked into runtime, imported by CLAUDE.md (same pattern as `memory-global` and the cursor rule). **Acceptance check:** the user signs off on the decision before it ships; an `rg <key>` sweep confirms no literal constant was left un-keyed (including embedded heredoc prompts).

## Contexts

### 2026-05-24 — coordination-machinery refactor (two silent decisions + embedded drift)
Extended the coordination machinery (task-weight triage, CLARIFY/PLAN-READY markers, depth cap, tiered budgets, two-turn self-improvement). Two silent architectural decisions (the budget/depth rescale; constants-as-section vs file) each cost an extra round-trip — and embarrassingly violated the *just-written* two-turn rule. Also missed the embedded recursive-escape prompt in `overcome-difficulty/SKILL.md` line 118 still hardcoding `>= 5` when the constants changed; caught later. Resolution: extracted constants to a top-level `config.md` (symlink `~/.claude/config.md`, `@`-import at end of CLAUDE.md), every literal now references a `config.md` key by name so updates propagate. Commits `06e1f11`, `65619e6`, `e1cd7d0`.

## Cost
In-thread, $0 spawn. The cost was the two extra correction round-trips — the avoidable kind. Same shape as [[2026-05-26-agent-system-plan-vs-reality-drift]] (a rule existed — the two-turn self-improvement rule — and didn't fire), but here the failure was application discipline, not a missing mechanism, so no new machinery was warranted.
