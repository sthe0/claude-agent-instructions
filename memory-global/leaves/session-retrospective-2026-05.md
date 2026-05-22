---
name: session-retrospective-2026-05
description: Retrospective from the 2026-05-17 → 2026-05-21 deepagent sessions — top mistakes, ticket startup checklist, self-check gates. Read before starting a new Arcadia ticket session or after a process correction.
type: project
---

# Agent session retrospective (2026-05-17 — 2026-05-21)

Source: agent transcripts for project `robot/deepagent` (13 sessions, ~2.9 MB). Domain facts for deepagent live in that project's `.claude/agent-memory/`.

## Period summary

| Session (uuid prefix) | Tickets | Type |
|---|---|---|
| e7175906 | DEEPAGENT-416 | OOM compute_metrics, Nirvana, Docker |
| 085f3aed | DEEPAGENT-421 | TTL; "14 days" without clarification |
| 593d90aa | DEEPAGENT-419 | cleanup auto_eval; started without mount |
| 9d95383c, 8f0c5353 | DEEPAGENT-402 | FTE-alert; orphan mount → Warming up |
| 6f052210 | 220, 403 | coordination, train → eval |
| 8f8b2055 | — | instructions git sync, agents-local |

**Delegation (aggregate, 2026-05-21):** `Task` ~22 vs `Bash` ~1250 + `Edit/Write` ~370. Root coordinator did too much itself instead of delegating.

## Top mistakes (do not repeat)

| # | Symptom | Correct |
|---|---|---|
| 1 | Code/search in `~/arcadia` before mount and without planner | Pull instructions → tracker → `planner` → approval → mount → `developer` |
| 2 | Ticket number tied to "similar" constant (421: 14 days) | Source it or ask the user **before** edits |
| 3 | Full `run_quality` / train → eval when debugging one block | Project memory: `test-quality-retest`, `train-eval-meta-relaunch` |
| 4 | New `console_scripts` instead of Fire subcommand | One entry point; one-off → stash, not Arc |
| 5 | `self-improvement` only after 2nd correction or apology only | Invoke the skill **in the same turn**, before final reply |
| 6 | WI launched — user asks "are you watching?" | Poll all WI immediately → "monitoring complete" table |
| 7 | Nirvana/VH3 runbook pasted into a generic agent prompt | Memory leaf + link in the plan only |
| 8 | Mount without `--allow-other` → Docker cannot see FUSE | `arc-parallel-mounts` runbook |
| 9 | Mount not removed after ticket → Warming up | `arc unmount` on completion |
| 10 | Instruction edits without push / without pull before edit | `self-improvement/policy.md` § Git sync |
| 11 | Blocker / repeat error / WI-OOM — root keeps trying Bash + Grep alone | Invoke the `overcome-difficulty` skill |

## Tracker ticket startup checklist

1. `scripts/sync-instructions-repo.sh pull`.
2. Read ticket + comments + links.
3. Unclear numbers/deadlines/TTL → source (wiki, code, project MCP servers) **or** ask the user.
4. Project memory — relevant leaves (Nirvana relaunch, retest, mount, …).
5. `Task → planner` → markdown plan with "Problem and done criteria".
6. Show plan → **explicit OK** (except "do it now").
7. Parallel mount `~/arcadia_<TICKET>-<slug>` (`--allow-other`).
8. `Task → developer` in the mount — not root.
9. After Nirvana launch → WI watch until terminal state.
10. Close: PR/ticket, **unmount**, if learning emerged — global memory leaf and/or `self-improvement` skill.

## Self-check gates (root)

Before first `Edit` / `Write` / `arc commit` in Arcadia on a ticket:

- [ ] This dialog had a plan message to the user and an explicit confirmation (or "do it now").
- [ ] cwd is `~/arcadia_<TICKET>-*`, not `~/arcadia`.
- [ ] No duplicate full pipeline if the goal is single-stage retest.

After user correction about agent behavior:

- [ ] `self-improvement` skill ran in the **same** turn (before final reply).

On blocker, repeat failure, or 2+ process corrections:

- [ ] `overcome-difficulty` skill ran in the **same** turn (before "one more try" Nirvana / arc).
