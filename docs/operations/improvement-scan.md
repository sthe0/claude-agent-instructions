# Improvement scan

> Command-line reference for `scripts/improvement-scan.py` — the standing self-improvement
> producer/report CLI. For the orchestration that ties these commands into one live-session flow,
> see `skills/improvement-scan/SKILL.md`; for the underlying model, see
> `memory-global/leaves/improvement-scan.md`.

## Overview

`scripts/improvement-scan.py` provides four subcommands (`backlog`, `loss`, `telemetry`,
`report`). Each does only the mechanizable rule
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
`~/.local/state/improvement-scan/board.json`; the env var moves it for every subcommand,
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
computes the old rubric score (`breadth_weight`, `recurrence_mass` and `cost_to_resolve`, kept
as `old_score`), which is now a tie-break input only; the rank itself is min/week from the
`loss` subcommand below (until `loss` has run, the board still carries an old-formula `rank`,
which the report ignores). It applies the hard partial order from any explicit `blocked_by`
edges, writes the new board JSON to the state file, and stores `Finding` rows.

A classification may also carry the loss fields: `signatures` (list of non-empty strings),
`minutes_per_occurrence` (number > 0) with `minutes_basis`, `precision` (0..1) with
`precision_sample` `{n, true}`, `family` (string) and `silent_estimate` (non-empty string).
They are validated on shape; a bad one exits 2 naming the ref. A re-classified item inherits
its previous loss fields. An amendment-only entry for a carried item may hold any subset of
these keys plus `addresses`. Phase A's worklist also lists `loss_unclassified`: open board items
with neither signatures nor a silent estimate.

Migration: a board written before the loss fields existed loads without loss. Every item keeps
its old classification, score and `severity_labeled`, and gains empty signature and precision
fields. Old boards never stored `severity`, `cost_to_resolve` or `old_score`, so these load
empty: fix cost sorts as unknown and the old-score tie-break falls back to the legacy score
until the item is re-classified. Such items sit under *Awaiting loss classification* until classified; none is dropped.

`addresses` is an optional list of 12-hex telemetry store keys (the `Store key` the report
prints) naming the measured cluster(s) a backlog item removes. Phase B checks only the shape
(anything else exits 2 naming the ref) and persists it on the board; whether a key still
resolves is decided at report time, because clusters resolve out between runs. A classification
carrying only `addresses` for a ref already on the board (and not in the worklist) amends that
item: it replaces its `addresses` (`[]` clears) and changes nothing else; any other field on
such an entry, or a ref on neither the worklist nor the board, exits 2.

## `loss` — measured-loss ranking

```
python3 scripts/improvement-scan.py loss [--board-state <path>] [--days 14] [--until <iso>] \
  [--projects-root <dir> ...] [--hits-cache <path>] [--emit-samples <samples.json>] \
  [--sample-size 10] [--store <store-path>]
```

Reads the board state file, counts for every item with `signatures` the distinct sessions
whose transcripts hit a signature inside the window, and rewrites the board with each item's
`loss_measurement` and lane `rank`. Rank is min/week = est sessions/window × min/occurrence /
(days/7). Counting surfaces are `tool_result` blocks, `system` entries and attachments whose
type starts with `hook`; assistant text, thinking, tool_use inputs and user text never count.
Known limit: `async_hook_response` attachments are not counted as hits. A `tool_result` whose
originating call was discussing or filing (input holds the signature, `#N` / `issues/N` of the
item, or `gh issue` / `file-difficulty.py` / `improvement-scan.py`) is excluded.

Hit cache: `~/.local/state/improvement-scan/loss-hits.json` (`--hits-cache`), keyed per
transcript by mtime and size, holding timestamps of hits per signature. A transcript is reopened
only when it grew or a signature it has not scanned yet is wanted; a transcript last modified
before the window start is skipped. A corrupt or absent cache is a full rescan. `--dry-run`
writes neither cache nor board.

`--emit-samples` writes seeded excerpts (one hit per session, up to `--sample-size`) for the
items whose `precision` is missing or was judged on a different signature set. The model judges
them and amends `precision` and `precision_sample` through phase B. Unjudged precision counts as
1 and the row says so (upper bound).

Report lanes, in order:

- **Measured loss**: columns min/week, sessions/window raw → estimated, min/occurrence, basis,
  fix cost, members.
- **Silent lane**: items without a signature, with their qualitative estimate.
- **Awaiting loss classification**: items not yet rankable or silent-qualified: no signature
  and no silent estimate; or signatures but no `minutes_per_occurrence`; or no `loss`
  measurement yet, or one older than the current signature set (run `loss`). A signature
  without measurement is never moved into the silent lane.

Every open item is in exactly one row. A family (shared `family` id) is one row: its loss is
computed once over the union of its members' hit sessions, each shared session charged at the
per-session max of p × minutes, so a family is never the sum of its members. In the loss lane,
rows sort by min/week descending, then tie-break inputs: severity (labelled first, higher mass
first), fix cost ascending, old score descending, ref. The silent and awaiting lanes sort by the
tie-break inputs alone. `blocked_by` is a hard order only within a lane; an edge crossing lanes
imposes none.

`report --board` JSON carries `findings`, `loss`, `silent` and `awaiting`; a `loss` row has
`ref`, `family`, `members`, `min_per_week`, `sessions_hit`, `sessions_est`, `min_per_occurrence`,
`basis`, `fix_cost`, `window_days`.

### Design choices

`loss` enumerates transcripts through `config_root.iter_transcripts` (subagents included) over
the same `projects_roots()` root set that `policy-scorecard` globs, so both see the same sessions. The hit cache is a new mtime-gated file
instead of `LedgerCursor`: the cursor stores one position per session for one fixed question,
while the loss scan asks a different question per signature, and a changed signature set must
rescan without resetting anything else. A per-transcript cache keyed by signature does that.

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
interleaving measured and unmeasured cost bands. With a readable `--board` (default: the board
state file) the loss lane opens the report; the silent and awaiting lanes follow the measured
telemetry findings.

At report time each backlog item's `addresses` keys are looked up among the open telemetry rows
of the same report. The highest measured cluster cost among them (or the item's own measured
cost, if larger) becomes the item's cost, shown with a `via <key>` note when the cluster's is the
larger; the item then follows its cluster's own row at equal cost, by proxy score. The cost is
copied, never summed across items sharing a cluster. Keys matching no open row are listed as
`dangling` (markdown and JSON) even when another key resolved; an item whose keys match only
unmeasured rows, or none, stays in the unmeasured band.

## Flags common to all subcommands

- `--store <path>` — the durable findings store (`self_diagnose_store.py`); defaults to
  `CLAUDE_SELF_DIAGNOSE_STORE` or `~/.local/state/claude-self-diagnose-findings.jsonl`.
- `--dry-run` (top-level, before the subcommand) — run the subcommand's logic without writing to
  the store or advancing any cursor.

## The due-hook

`scripts/hook-improvement-scan-due.py` (SessionStart, throttled weekly) never invokes this CLI. It
only counts already-stored open findings and, if any are open, prints a reminder to invoke the
`improvement-scan` skill — running the producers themselves remains a live session's job.
