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
python3 scripts/improvement-scan.py backlog \
  --emit-worklist <worklist.json> [--channel <name> ...]
```

Phase A collects raw backlog records (from `--channel`, repeatable, or the configured default
channels) and diffs them against the board state file. Emits only new/changed items plus
`closed_refs`, not a full re-derivation.

The durable board is a **local state file** — `$IMPROVEMENT_SCAN_BOARD_STATE`, default
`~/.local/state/improvement-scan/board.json`; the env var moves it for all three subcommands,
`backlog --board-state` for `backlog` only. Phase B writes it atomically (nothing under
`--dry-run`); phase A, `telemetry --board` and `report --board` read it by default. With no state
file phase A cold-starts from an empty board and prints one stderr note; an unreadable or
other-schema state file is moved to `board.json.bak` first, so the next write cannot clobber the
only copy (one rotation: an earlier `.bak` is overwritten). An explicit `--prior` or `--board`
overrides the default; `--out` additionally writes a copy of the board.

A published artifact is a view of that file, never an input: `Artifact action:"read"` returned
HTTP 451 for an artifact minutes after the same session published it, so an artifact cannot be
read back as the store.

```
python3 scripts/improvement-scan.py backlog \
  --worklist <worklist.json> --classifications <classifications.json> \
  --store <store-path>
```

Phase B takes a classifications file (`{"items": {<ref>: {breadth, cost_to_resolve, in_flight,
recommended_next_step, blocked_by?, addresses?}}, "closed_refs": [...]}`) and merges each item's metadata
(title, functional_ground, severity, severity_labeled, reporter, evidence, cost_estimate,
source_digest) from `--worklist`; a field the classification names wins over the worklist's.
Without `--worklist` the classification must carry title, functional_ground, severity and
source_digest itself. An unknown ref, a missing field, or a worklist ref left unclassified
exits 2 naming the ref, as does an `items` that is not an object. `closed_refs` falls back to the
worklist's list when the classifications file carries none. It validates every field against its
closed vocabulary (rejecting the whole call on the first out-of-vocabulary value), scores and
ranks via `score(item) = breadth_weight × recurrence_mass / cost_to_resolve`, applies the hard
partial order from any explicit `blocked_by` edges, writes the new board JSON to the state file,
and stores `Finding` rows.

`addresses` is an optional list of 12-hex telemetry store keys (the `Store key` the report
prints) naming the measured cluster(s) a backlog item removes. Phase B checks only the shape
(anything else exits 2 naming the ref) and persists it on the board; whether a key still
resolves is decided at report time, because clusters resolve out between runs. A classification
carrying only `addresses` for a ref already on the board (and not in the worklist) amends that
item: it replaces its `addresses` (`[]` clears) and changes nothing else; any other field on
such an entry, or a ref on neither the worklist nor the board, exits 2.

## `telemetry` — session pattern detection

Two modes:

```
python3 scripts/improvement-scan.py telemetry --emit-evidence <evidence.json> --days <window>
```

Scan mode reads the policy ledger and spawn rows since the last cursor position (an
mtime-gated `LedgerCursor`, so a rerun only rescans sessions that grew) and emits an evidence
bundle of candidate friction patterns. The ledger refresh that precedes the scan is bounded at
900 s (a catch-up refresh after a long gap rescans hundreds of sessions); set
`IMPROVEMENT_SCAN_REFRESH_TIMEOUT_S` to a positive integer to override. A refresh that fails or
exceeds the bound still makes the run DEGRADED (exit 1). The `--emit-evidence` and report outputs
are plain overwrites with no `.bak`; only the board state file is backed up.

```
python3 scripts/improvement-scan.py telemetry --grounds <grounds.json> \
  [--board <board.json>] --store <store-path>
```

Grounds mode takes a list of ground records (`detector`, `functional_ground`, `title`,
`evidence_refs`, optional `cost_signal`, optional `recommended_next_step`), dedups each against the
given board and against existing experience leaves, and stores survivors as `Finding` rows —
logging every dedup outcome, not just the ones it keeps.

The experience-leaf dedup is a two-step join. `record-experience.py search` only nominates
candidate leaves (ranked by term overlap, no cut-off); a model judge
(`agentctl/advisor.py::judge_same_difficulty`) alone decides whether a ground is the same
difficulty as a candidate. The run prints one summary line, `dedup outcomes: no-match=N
dedup-match=M board-match=B search-failed=K judge-unavailable=J`. `judge-unavailable` means no
verdict was obtained (the `AGENTCTL_ADVISOR=0` killswitch, a timeout, an unparseable answer) and
the finding is stored anyway; `search-failed` means the search subprocess itself failed.

A ground is judged against the board's open items the same way (word overlap nominates, the
judge decides; identical text joins outright), so a reworded ground is a `board-match`, not a
new finding. `file-difficulty.py` — the step after the scan — checks open records before filing:
on a judged match it files nothing and comments the evidence on the matched record (exit 0),
or under `--no-comment-on-match` refuses naming the ref (exit 3).
`record-experience.py new` likewise asks a judge before refusing in favour of `extend`.

## `report` — unified ranked output

```
python3 scripts/improvement-scan.py report --store <store-path> --format md|json
```

Renders every stored `Finding` from both producers into one cost-first-ranked report, never
interleaving measured and unmeasured cost bands.

At report time each backlog item's `addresses` keys are looked up among the open telemetry rows
of the same report. The highest measured cluster cost among them (or the item's own measured
cost, if larger) becomes the item's cost, shown with a `via <key>` note when the cluster's is the
larger; the item then follows its cluster's own row at equal cost, by proxy score. The cost is
copied, never summed across items sharing a cluster. Keys matching no open row are listed as
`dangling` (markdown and JSON) even when another key resolved; an item whose keys match only
unmeasured rows, or none, stays in the unmeasured band. Items without a rank
(no-urgency-signal, unjudged) are not joined.

## Flags common to all subcommands

- `--store <path>` — the durable findings store (`self_diagnose_store.py`); defaults to
  `CLAUDE_SELF_DIAGNOSE_STORE` or `~/.local/state/claude-self-diagnose-findings.jsonl`.
- `--dry-run` (top-level, before the subcommand) — run the subcommand's logic without writing to
  the store or advancing any cursor.

## The due-hook

`scripts/hook-improvement-scan-due.py` (SessionStart, throttled weekly) never invokes this CLI. It
only counts already-stored open findings and, if any are open, prints a reminder to invoke the
`improvement-scan` skill — running the producers themselves remains a live session's job.
