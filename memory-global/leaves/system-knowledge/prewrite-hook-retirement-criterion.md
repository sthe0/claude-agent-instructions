---
name: prewrite-hook-retirement-criterion
description: Difficulty — `hook-prewrite-plan-check.py` is the OLD non-agentctl fallback plan-check; retiring it on a guess could drop plan-gate coverage for sessions still on the prose fallback. Fact — it now logs every firing to a JSONL ledger; retire only when the report shows zero firings for ≥ a full window after engine auto-start ships.
type: reference
---

# When to retire `hook-prewrite-plan-check.py`

**Difficulty.** `scripts/hook-prewrite-plan-check.py` is the legacy, entirely-fallback (non-agentctl) plan-check nudge: on `PreToolUse` Edit/Write it counts consecutive production-file edits without a plan file and emits a one-time nudge. Once the agentctl engine auto-starts every session, this hook is redundant with the `EXECUTING`-node plan gate (`hook-state-gate.py`). But retiring it on a guess is unsafe: any session still running the prose fallback (engine not started) would silently lose its plan-gate nudge. We need evidence, not a guess.

**Fact / criterion.** The hook now appends one JSON line per firing to
`~/.claude/agentctl/prewrite-fallback.jsonl` (best-effort, inside `try/except`, never breaks the hook — commit `d9972a3`). `scripts/prewrite-fallback-report.py [--days N] [--ledger PATH]` aggregates it (total firings, unique sessions, per-cwd breakdown).

**Retire `hook-prewrite-plan-check.py` (drop it from `install-reminder-hooks.sh` DESIRED and delete the script) only when ALL hold:**
1. Engine auto-start has shipped and been live for ≥ one full reporting window (so sessions default to agentctl, not the prose fallback).
2. `prewrite-fallback-report.py --days <window>` shows **0 firings** across that window.
3. The `EXECUTING`-node gate (`hook-state-gate.py`) is confirmed to cover the same production-edit-without-approved-plan case for auto-started sessions.

Until then the hook stays as the safety net for any non-agentctl session. The ledger is the data that turns "probably safe to remove" into a measurable check.

> verified by: commit d9972a3 (ledger instrumentation); plan `~/.claude/plans/task8-hygiene.md` Stage A; depends on the auto-start enabler (roadmap `steady-riding-dragonfly.md` cross-cutting enabler).
