---
name: improvement-scan
description: The standing, mechanized producer/skill pair for proactive self-improvement scanning — two independent producers (backlog reconciliation, session telemetry pattern detection), their two distinct resume schemes, and the report-only boundary the CLI deliberately does not cross.
type: reference
schema: leaf/v1
created: 2026-08-28
last_verified: 2026-08-28
---

# Improvement scan

## Difficulty

Two standing self-improvement responsibilities were previously done from prose,
by hand, re-derived on every visit: [[backlog-triage-practice]]'s cross-source
priority digest (gap #2, now closed) and [[systemic-pattern-scan]]'s manual
cross-session friction scan. Both are decidable-from-observable-inputs
collection/scoring work wearing a judgment-only task's clothes — the
mechanizable rule part (collect, diff, score, rank, dedup) was tangled up with
the genuinely-judgment part (classification, functional-ground statement,
never-auto-select). `scripts/improvement-scan.py` separates the two: the
script owns the rule part; the `improvement-scan` skill
(`skills/improvement-scan/SKILL.md`) supplies the perception part at each seam
the script's own closed vocabularies gate.

## Guidance

### The two producers

- **`backlog` subcommand** — reconciles the Core+Org backlog against a
  published "Triage Board" artifact (see
  [[backlog-triage-practice]] § Reuse across runs). Two phases: phase A
  (`--emit-worklist`) collects raw records and diffs them against a `--prior`
  board JSON, emitting only new/changed items plus `closed_refs`; phase B
  (`--classifications --out`) takes a live session's classifications (breadth,
  cost_to_resolve, in_flight, recommended_next_step — validated against closed
  vocabularies, rejecting the whole call on the first out-of-vocabulary value),
  scores and ranks via
  `score(item) = breadth_weight × recurrence_mass / cost_to_resolve`, and
  writes both a new board JSON and `Finding` rows to the durable store.
- **`telemetry` subcommand** — scans recent session ledgers for recurring
  friction patterns. Two modes: scan mode (`--emit-evidence`) reads the policy
  ledger and spawn rows since the last cursor position and emits an evidence
  bundle; grounds mode (`--grounds`) takes a live session's stated
  <!-- Language exception: симптом/функция are the settled SMD source-ontology terms [[function-place-difficulty]] types; preserved verbatim for traceability. -->
  `functional_ground` per candidate (the симптом-vs-функция distinction — see
  [[function-place-difficulty]]), dedups against the current board and against
  existing experience leaves, and stores survivors as `Finding` rows.
- **`report` subcommand** — renders every stored `Finding` from both producers
  into one cost-first-ranked report (measured cost bands never interleaved
  with unmeasured ones).

### Why two different resume schemes, not one

- **Backlog** uses `PriorBoard`: a frozen baseline JSON snapshot, diffed
  against fresh collection on each run. Right for it because the Core+Org
  backlog is a live, externally-mutated list (items close, get relabeled,
  gain cluster-mates) — the diff has to be against the *board's last known
  state*, not against a point in time, or a closed-then-reopened item and a
  merely-untouched item become indistinguishable.
- **Telemetry** uses `LedgerCursor`: an mtime-gated position per session
  transcript. Right for it because session ledgers only ever grow (a
  transcript is append-only within a session, and past sessions are immutable
  once closed) — a position cursor is cheaper and sufficient, and a
  baseline-diff scheme would force re-reading every historical session on
  every run for no benefit.

A single unified resume scheme was considered and rejected: forcing the
externally-mutable backlog through a position cursor would silently miss
external edits between runs; forcing the append-only ledger through a
baseline-diff would re-read strictly more data for the same result.

### The report-only boundary

Neither producer, nor the `report` renderer, ever files a difficulty,
dispatches a specialist, or picks an item to work on — asserted by
`scripts/tests/test_improvement_scan.py` via `ast_purity.impure_names`. The
`improvement-scan` skill inherits this boundary explicitly (its own §
Boundary): it hands the user one ranked report and stops; starting work on any
one finding is a separate, later, explicit appeal naming that finding — the
same invariant [[backlog-triage-practice]] states for its own procedure step 6.

### The due-hook is a nudge, not a trigger

`scripts/hook-improvement-scan-due.py` (SessionStart, throttled 7 days) never
runs either producer. It only counts already-stored, still-open findings
carrying the improvement-scan source tag and, if any are open, prints a
reminder naming the skill. Running the producers, supplying classifications,
and rendering the report all remain a live session's work, invoked via the
skill.

## See also

- [[backlog-triage-practice]] — the priority rubric and classification
  vocabulary the backlog producer's phase B applies; § Identified engine gaps
  #2 records this producer as its closure.
- [[systemic-pattern-scan]] — the manual cross-session friction scan the
  telemetry producer mechanizes the collection half of.
<!-- Language exception: симптом/функция are the settled SMD source-ontology terms [[function-place-difficulty]] types; preserved verbatim for traceability. -->
- [[function-place-difficulty]] — the симптом/функция distinction behind
  `functional_ground`, required at the telemetry producer's grounds-intake
  step.
- `docs/operations/improvement-scan.md` — command-line reference for
  `scripts/improvement-scan.py` outside the skill's own orchestration.
