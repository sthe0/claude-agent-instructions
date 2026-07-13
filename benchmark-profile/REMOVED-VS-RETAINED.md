# benchmark-profile: removed vs retained

This is the SHARED, path-agnostic autonomous variant of
`~/claude-agent-instructions/` used for headless SWE-bench execution (internal
arcadia/verified platform, Stage 2+ of `swe-bench-own-agent`, AND the
`public-ablation-benchmark-pilot` plan, which reuses this exact tree — see
`## Canonical artifact` below). The original interactive tree is untouched.

## Removed

- **agentctl entirely** (`scripts/agentctl/*`). Not baked, not invoked at
  runtime — the profile contains no reference to `python3 -m agentctl` and the
  benchmark image has no need for a Python engine driving plan state.
- **All interactive gates**: the plan-approval gate (`PLAN-READY:` +
  `AskUserQuestion`), the resolution gate (`agentctl resolve`), the
  classify-before-edit gate, the `hook-*-gate.py` / `hook-*-reminder.py` family
  registered in the personal `~/.claude/settings.json` (none of those hook
  scripts, nor a reference to them, exist in this profile's `settings.json`,
  which has no `hooks` key at all).
- **Process-spawning execution model**: spawned `claude -p` specialists
  (`spawn-specialist.py`), background `Workflow`, `ScheduleWakeup`,
  `CronCreate`. Explicitly denied in `settings.json` permissions and forbidden
  in `CLAUDE.md` prose. Reason (shared by both consumers of this artifact):
  the platform's `run.sh` kills the parent PID the instant it sees the first
  top-level `{"type":"result"` line in the stream-json output and never waits
  for children — a still-writing spawned child is orphaned and the wrapper
  commits a truncated patch (false green). The pilot's local harness has an
  independent reason: a spawned child's tokens bill outside the parent's
  result-event cost total, breaking `--max-budget-usd` capture.
  This ban is retained unchanged **in this profile**. The pilot's harness now
  also mounts a sibling variant, `benchmark-profile-spawn/`, that conditionally
  un-prohibits `spawn-specialist.py` under a bounded delegation contract — see
  `## Variant: benchmark-profile-spawn` below. It is a **separate tree**
  selected by `AGENT_BENCH_ALLOW_SPAWN=1` in `agent-bench/runner/config_layers.py`,
  not an edit to this one; the arcadia/platform consumer only ever sees this
  directory and is unaffected.
- **Escalation markers** (`CLARIFY:` / `PERMISSION-REQUEST:` / `ESCALATE:` /
  `REPLAN:`): the manager-facing marker protocol assumes a manager process
  reads and answers them; headless there is nobody to. `CLAUDE.md` tells the
  agent to make the call itself instead of emitting these.
- **Tracker / Nirvana / wiki / MCP integration**: the `tracker-management`
  skill, tracker MCP tools, wiki MCP tools are not carried over; the
  prompt.template environment has no network access to them anyway.
  `CLAUDE.md` instructs the agent to no-op past any leftover reference rather
  than fail.
- **Memory system**: `~/.claude-agent/memory-global/` and the auto-memory
  mechanism are not part of this profile. A single-shot benchmark task has no
  future session to benefit from written memory, and no time budget to spend
  reading a large memory tree that mostly encodes org-specific (Yandex)
  process knowledge irrelevant to fixing one Go bug.
- **Non-coding skills**: `ccgram-management`, `instruction-grooming`,
  `overcome-difficulty`, `self-improvement`, `tracker-management`,
  `yandex-cloud-expert`, `tech-writer` — all org/meta-workflow skills with no
  role in a single-shot autonomous code fix.
- **`config.md` numeric constants** (recursion depth, budget tiers, wall-clock
  thresholds): all keyed to the agentctl spine and the spawn/budget machinery
  that no longer exists in this profile.

## Retained

- **Substantive engineering discipline** from `CLAUDE.md`: read before edit,
  no scope creep, no unneeded error handling, no comments unless the *why* is
  non-obvious, match project style, tests accompany a code change (with the
  same named non-testable escape class), run the standard build/test commands,
  self-review the diff before stopping.
- **In-process `Task` subagents** (Explore / general-purpose / Plan) — these
  run and terminate inside this same process's lifetime, so they cannot be
  orphaned by the harness's kill-on-result behavior. Explicitly distinguished
  from spawned `claude -p` processes, which are forbidden (see Removed).
  Skills are invoked **inline**, never spawned.
- **Specialization skill content**: `skills/specializations/{developer,
  planner,thinker,code-reviewer}/` copied verbatim (plus the shared
  `_shared/marker-protocol.md`) from the interactive repo — reusing existing
  instruction content per the plan's method, rather than rewriting it. Their
  own text still describes a "spawn as a separate `claude -p` process" mode
  and manager-facing return markers; those parts are superseded by this
  profile's `CLAUDE.md`, which forbids spawning and escalation markers
  globally. This is a deliberate simplification (override at the top level
  instead of hand-editing every skill file) — flagged here rather than left
  implicit, since a naive reader of the skill file alone would still see the
  spawn instructions.
- **Cost/token-lean output style**: keep responses and diffs minimal — still
  relevant even without an interactive cost dashboard.

## Known gap (not resolved in this pass)

The `developer`/`planner`/`thinker`/`code-reviewer` skill files were copied
as-is rather than individually edited to strip their own spawn/manager-marker
language (see Retained above). The global `CLAUDE.md` override is expected to
dominate, but this has not been verified against a case where the agent
actually invokes one of these skills mid-task and might follow its literal
"spawn a `claude -p` process" instruction instead of the top-level rule. If a
future smoke run shows a skill invocation actually spawning a process, these
files need a direct edit, not just the top-level override.

## Variant: benchmark-profile-spawn (spawn-permitting)

`/home/the0/claude-agent-instructions/benchmark-profile-spawn/` is a sibling
tree, mounted instead of this one only when the harness sets
`AGENT_BENCH_ALLOW_SPAWN=1` (`agent-bench/runner/config_layers.py` picks the
directory; `AGENT_BENCH_LAYER_PROFILE_DIR`, if set explicitly, still wins over
either). It exists to measure the coordination layer's real delegation mode
(spawning `developer`/`thinker`/`code-reviewer`/`planner` via
`spawn-specialist.py`) instead of the fully flat single-process mode this
directory forces.

- **Identical**: `skills/specializations/*` — copied verbatim from this tree
  (same sha256 per file, checked by `MANIFEST.sha256` in both directories).
- **Different `settings.json`**: permits `Bash(*spawn-specialist*)` (the
  sanctioned wrapper only — `Bash(claude -p:*)` and `Bash(*agentctl*)` stay
  denied, `Workflow`/`ScheduleWakeup`/`CronCreate` stay denied) and adds two
  env vars consumed by `spawn-specialist.py` itself:
  `AGENT_BENCH_MAX_CHILDREN=2` (fan-out cap — a 3rd spawn on one instance is
  refused outright) and `AGENT_BENCH_SPAWN_BUDGET_USD=6.00` (SUM-bound: the
  running total across every child on this instance's shared cost ledger —
  actual cost once finished, granted budget cap while in flight — may not
  exceed this). This is a **sum** bound distinct from the pre-existing
  per-spawn `--max-budget-usd` tier cap; a spawn whose own addition would push
  the sum over the cap is refused before it starts. Arithmetic:
  `kind=developer` floors to the medium tier (`budget-medium-usd = $3.00` in
  `config.md`), so 2 children at that tier sum to exactly `$6.00` — sized to
  the worst case for the fan-out cap chosen, not an arbitrary round number
  (checked by `test_spawn_profile_sum_bound_matches_two_medium_tier_children`
  in `agent-bench/tests/test_benchmark_profile_variant.py`, which reads
  `budget-medium-usd` back out of `config.md` so the two never silently
  drift apart).
- **Different `CLAUDE.md`**: the spawn-prohibition bullet is replaced by a
  "Delegation is permitted, bounded" section spelling out both caps, a
  depth ≤ 2 guideline, the same kill-on-first-result-event hazard (a spawned
  child must finish before this process emits its own result, or its edits
  are lost), and instructs the agent to resolve any escalation marker a
  spawned specialist returns itself (no user, no manager one level up).
- **Refusal mechanism** lives in `spawn-specialist.py`
  (`read_ledger_rows`/`committed_spawn_usd` + the `main()` refusal block,
  exit codes 5 = fanout-cap / 6 = spawn-budget-cap), not in the profile
  files — both new env vars are no-ops when unset, so every existing caller
  (including this default profile, which never sets them) is unaffected.
  Covered by `claude-agent-instructions/scripts/tests/test_spawn_specialist_fanout.py`.

## Canonical artifact

- **Canonical path**: `/home/the0/claude-agent-instructions/benchmark-profile/`
- **Content manifest**: `MANIFEST.sha256` in this directory (sha256 of every
  file except itself). Regenerate with:
  `find . -type f ! -name 'MANIFEST.sha256*' | sort | xargs sha256sum > MANIFEST.sha256`
- This tree is reused **verbatim** by the `public-ablation-benchmark-pilot`
  plan's local harness. Any change to the pruning boundary described above
  must be reconciled with that plan before being applied, since both studies
  need to measure the same object.
