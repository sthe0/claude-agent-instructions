# Improvement scan

> Command-line reference for `scripts/improvement-scan.py` — the standing self-improvement
> producer/report CLI. For the orchestration that ties these commands into one live-session flow,
> see `skills/improvement-scan/SKILL.md`; for the underlying model, see
> `memory-global/leaves/improvement-scan.md`.

## Overview

`scripts/improvement-scan.py` provides three subcommands. Each does only the mechanizable rule
part of its responsibility — collection, diffing, scoring, deduplication, rendering — and leaves
every judgment call (classification, functional-ground statement, which candidate is worth acting
on) to whichever live session drives it via the `improvement-scan` skill. The CLI never files a
difficulty, dispatches a specialist, or picks work; it only reports.

## `backlog` — Core+Org backlog reconciliation

Two phases, run in sequence with a classification step in between:

```
python3 scripts/improvement-scan.py backlog --prior <prior-board.json> \
  --emit-worklist <worklist.json> [--channel <name> ...]
```

Phase A collects raw backlog records (from `--channel`, repeatable, or the configured default
channels) and diffs them against `--prior` (a previously-published board JSON). Emits only
new/changed items plus `closed_refs`, not a full re-derivation.

```
python3 scripts/improvement-scan.py backlog --prior <prior-board.json> \
  --classifications <classifications.json> --out <new-board.json> --store <store-path>
```

Phase B takes a classifications file (`{"items": {<ref>: {breadth, cost_to_resolve, in_flight,
recommended_next_step}}, "closed_refs": [...]}`), validates every field against its closed
vocabulary (rejecting the whole call on the first out-of-vocabulary value), scores and ranks via
`score(item) = breadth_weight × recurrence_mass / cost_to_resolve`, applies the hard partial order
from any explicit `blocked_by` edges, writes the new board JSON, and stores `Finding` rows.

## `telemetry` — session pattern detection

Two modes:

```
python3 scripts/improvement-scan.py telemetry --emit-evidence <evidence.json> --days <window>
```

Scan mode reads the policy ledger and spawn rows since the last cursor position (an
mtime-gated `LedgerCursor`, so a rerun only rescans sessions that grew) and emits an evidence
bundle of candidate friction patterns.

```
python3 scripts/improvement-scan.py telemetry --grounds <grounds.json> \
  --board <board.json> --store <store-path>
```

Grounds mode takes a list of ground records (`detector`, `functional_ground`, `title`,
`evidence_refs`, optional `cost_signal`, optional `recommended_next_step`), dedups each against the
given board and against existing experience leaves, and stores survivors as `Finding` rows —
logging every dedup outcome, not just the ones it keeps.

## `report` — unified ranked output

```
python3 scripts/improvement-scan.py report --store <store-path> --format md|json
```

Renders every stored `Finding` from both producers into one cost-first-ranked report, never
interleaving measured and unmeasured cost bands.

## Flags common to all subcommands

- `--store <path>` — the durable findings store (`self_diagnose_store.py`); defaults to
  `CLAUDE_SELF_DIAGNOSE_STORE` or `~/.local/state/claude-self-diagnose-findings.jsonl`.
- `--dry-run` (top-level, before the subcommand) — run the subcommand's logic without writing to
  the store or advancing any cursor.

## The due-hook

`scripts/hook-improvement-scan-due.py` (SessionStart, throttled weekly) never invokes this CLI. It
only counts already-stored open findings and, if any are open, prints a reminder to invoke the
`improvement-scan` skill — running the producers themselves remains a live session's job.
