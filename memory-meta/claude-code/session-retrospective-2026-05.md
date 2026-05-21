# Agent session retrospective (2026-05-17 — 2026-05-21)

Source: agent transcripts for project `robot/deepagent` (13 sessions, ~2.9 MB). Do not duplicate deepagent domain facts — they live in `deepagent/*` leaves.

## Metadata

| Field | Value |
|------|----------|
| `last_verified` | 2026-05-21 |
| `staleness_triggers` | mandatory workflow change in CLAUDE.md; new widespread error patterns in transcripts |
| `revalidate` | sample 3 latest parent transcripts; Task vs Edit count; any "plan before code" violations |

## Period summary

| Session (uuid prefix) | Tickets | Type |
|----------------------|--------|-----|
| e7175906 | DEEPAGENT-416 | OOM compute_metrics, Nirvana, Docker |
| 085f3aed | DEEPAGENT-421 | TTL; "14 days" without clarification |
| 593d90aa | DEEPAGENT-419 | cleanup auto_eval; started without mount |
| 9d95383c, 8f0c5353 | DEEPAGENT-402 | FTE-alert; orphan mount → Warming up |
| 6f052210 | 220, 403 | manager, train→eval |
| 8f8b2055 | — | instructions git sync, agents-local |

**Delegation (aggregate, recalc 2026-05-21):** `Task` ~22 vs `Shell` ~1250, `StrReplace` ~370. Subagents in `robot/deepagent` transcripts: self-improvement 12, developer 6, planner 1, **manager 0**. Parent does too much itself and **collapses coordinator role** instead of Task→manager.

## Top mistakes (do not repeat)

| # | Symptom | Correct |
|---|---------|-----------|
| 1 | Code/search in `~/arcadia` before mount and without planner | pull instructions → tracker → **planner** → **approval** → mount → **developer** |
| 2 | Ticket number tied to "similar" constant (421: 14 days) | Source or ask user **before** edits |
| 3 | Full `run_quality` / train→eval when debugging one block | memory: `test-quality-retest.md`, `train-eval-meta-relaunch.md` |
| 4 | New `console_scripts` instead of Fire subcommand | One entry point; one-off — stash, not Arc |
| 5 | Self-improvement only after 2nd correction or apology only | **Task → self-improvement same turn** before reply |
| 6 | WI launched — user asks "are you watching?" | Poll all WI immediately → "monitoring complete" table (`nirvana-wi-watch.md`) |
| 7 | Nirvana/VH3 runbook in `manager.md` / `developer.md` | **memory** leaf + link in plan only |
| 8 | Mount without `--allow-other` → Docker cannot see FUSE | `arc-parallel-mounts.md` |
| 9 | Mount not removed after ticket → Warming up | `arc unmount` on completion |
| 10 | Instruction edits without push / without pull before edit | `instructions-git-sync.md` |
| 11 | Blocker / repeat error / WI-OOM — parent Shell+Grep+transcripts alone | **Task → manager** (§ Overcoming difficulties); do not patch `manager.md` instead of calling |

Incident 416 details: `deepagent/compute-metrics-oom-de416.md`.

## Tracker ticket startup checklist (P0)

Used by **parent**, **manager**, **planner** — do not skip steps.

1. `scripts/sync-instructions-repo.sh pull`
2. Read ticket + comments + links
3. Unclear numbers/deadlines/TTL → source (wiki, code, deepagent MCP) **or ask user**
4. `memory/INDEX.md` — relevant leaves (Nirvana relaunch, retest, mount…)
5. **planner** → markdown plan with "Problem and done criteria"
6. Show plan → **explicit OK** (except "do it now")
7. Parallel mount `~/arcadia_<TICKET>-<slug>` (`--allow-other`)
8. **developer** in mount — not parent
9. After Nirvana launch → WI watch until terminal
10. Close: PR/ticket, **unmount**, if learning — **memory** / **self-improvement**

## Self-check gates (parent)

Before first `Edit`/`Write`/`arc commit` in Arcadia on a ticket:

- [ ] This dialog had a plan message to user and confirmation (or "do it now")
- [ ] cwd is `~/arcadia_<TICKET>-*`, not `~/arcadia`
- [ ] No duplicate full pipeline if goal is single-stage retest

After user correction about agent behavior:

- [ ] **self-improvement** ran in the **same** turn (before final reply)

On blocker, repeat failure, or 2+ process corrections:

- [ ] **manager** ran in the **same** turn (before "one more try" Nirvana/arc)

## Long session metric (optional)

At end of session with >10 tool calls — one line to user:

`Delegation: Task=N; parent Edit/Write=M.`

Goal: lower M on ticket-sized tasks.

## Already covered by instructions (2026-05-21)

- Workflow: understand → plan → approval → mount → developer
- Mandatory **manager** on difficulties (parent must not self-coordinate)
- Mandatory self-improvement same turn
- `agents-local/` for logos-*; git pull/commit/push + hooks
- `nirvana-wi-watch.md`, TTL layers, memory revalidate in `memory.md`

## Improvement priorities

1. **Execute** the checklist (not new rules).
2. Read memory **before** launching Nirvana CLI.
3. Domain → memory leaf with `revalidate`, not agent prompts.
