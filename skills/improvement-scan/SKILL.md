---
name: improvement-scan
description: TRIGGER when the user asks you to proactively improve the agent system itself, review its own backlog, or look for recurring problems in its own recent work — WITHOUT a specific correction driving it (that's self-improvement's, reactive-only) — in any language, e.g. "scan yourself for improvements" / "what should we fix in the agent" / the Russian trigger «сделай себя лучше». Runs both standing producers (backlog reconciliation against the local board state, recent-session telemetry pattern detection) end to end, ranks findings cost-first in one report, and STOPS — never files a difficulty, dispatches a specialist, or auto-selects work. SKIP if the user names one specific item to act on (ordinary task routing, not a scan).
---

# Improvement scan

You run `scripts/improvement-scan.py`'s two producers to completion and hand the
user one ranked, cost-first report — the standing, mechanized form of what
`backlog-triage-practice.md` and `systemic-pattern-scan.md` otherwise ask you to
redo from prose every time. The script deliberately does not do the perception
half (classification, functional-ground statement) — that is what this skill
supplies, against the script's own closed vocabularies, which it validates and
rejects on the first out-of-vocabulary value.

> **Language exception:** «сделай себя лучше» in the description above is the
> settled Russian trigger phrase for this skill, preserved verbatim per
> CLAUDE.md § Instruction language.

## Boundary — read this before running anything

This skill **reports and recommends only**. Never, at any step below: file a
difficulty, dispatch a specialist, open a PR, or pick which finding to work on.
The output is an ordered report; starting work on any one item is a separate,
explicit, later appeal naming that item — same invariant as backlog-triage's
"never auto-select" (`memory-global/leaves/backlog-triage-practice.md` §
Procedure step 6).

## Procedure

1. **The prior board is the local state file** (`$IMPROVEMENT_SCAN_BOARD_STATE`,
   default `~/.local/state/improvement-scan/board.json`), which phase B writes
   and phase A reads automatically — nothing to fetch. No file means a cold
   start (one stderr note, not an error). Never rebuild the prior from the
   published artifact: that is a view, and `Artifact action:"read"` returned
   HTTP 451 minutes after the same session published it.

2. **Backlog producer, phase A** (collect + diff against the state file):
   ```
   python3 scripts/improvement-scan.py backlog --emit-worklist worklist.json
   ```
   Read the printed new/changed/closed/coverage-gap counts.

3. **Classify** (the perception step). For every item in `worklist.json`'s
   `items` (buckets `new`/`changed`/`rescore` — `rescore` is a prior unscored
   item whose record now carries a severity label), supply only the judgment,
   per item ref: `breadth` (`narrow`/`shared-mechanism`/`universal`),
   `cost_to_resolve` (a budget tier key), `in_flight` (a readiness coefficient
   key), `recommended_next_step` (one of `self-improvement`/`planner`/
   `file-difficulty`), and optionally `blocked_by` and `addresses` (a list of
   12-hex telemetry store keys, copied from the report's telemetry rows, naming
   the measured cluster the item removes; the join to that cluster's cost
   happens at report time). `{"<ref>": {"addresses": [...]}}` alone also amends
   an item already on the board (`[]` clears it). Changed/rescore items arrive
   with their existing `addresses` already filled in, so re-supply the key only
   to change it. Reason from the item's own
   text per `backlog-triage-practice.md` § Priority rubric, never guessed.
   Do not copy the item's title, ground, severity, `severity_labeled`, evidence
   or digest: phase B merges them from the worklist. An item with no severity
   label is still placed in a lane (below), never dropped. Supply the loss
   fields too: `signatures` (observable strings its failure leaves in a
   transcript), `minutes_per_occurrence` with `minutes_basis`, `family` (a
   shared id for items describing one failure), and for an item with no
   observable signature a `silent_estimate` instead. An item already on the
   board takes an amendment-only entry carrying just these keys (and
   `addresses`); phase A's `loss_unclassified` lists board items that still
   have neither signatures nor a silent estimate. Write
   `classifications.json` as `{"items": {<ref>: {...}}, "closed_refs": [...]}`
   with `worklist.json`'s `closed_refs`.

4. **Backlog producer, phase B** (score + merge):
   ```
   python3 scripts/improvement-scan.py backlog \
     --worklist worklist.json --classifications classifications.json \
     --store <store>
   ```
   The merged board replaces the state file atomically (`--out <path>` also
   writes a copy). Without `--worklist`, each classification must itself carry
   `title`, `functional_ground`, `severity` and `source_digest`. A ref missing
   from the worklist, a worklist ref left unclassified, or a missing field exits
   2 naming the ref; an out-of-vocabulary
   value is rejected before anything is written — fix the classification and
   rerun rather than loosening the vocabulary.

4a. **Loss step** (count, then judge precision):
   ```
   python3 scripts/improvement-scan.py loss --emit-samples samples.json
   ```
   Counts, per item with signatures, the distinct sessions whose transcripts
   hit a signature in the window, and ranks the board by min/week. For each
   item in `samples.json` (precision missing or judged on another signature
   set), judge the excerpts as real occurrence or not and amend the item with
   `precision` and `precision_sample` `{n, true}`; rerun phase B with that
   amendment, then `loss` again. Until judged, precision is 1 and shown as an
   upper bound. Rule and edge cases: `docs/operations/improvement-scan.md`.

5. **Telemetry producer, scan mode:**
   ```
   python3 scripts/improvement-scan.py telemetry \
     --emit-evidence evidence.json --days <window>
   ```
   This mechanizes `systemic-pattern-scan.md`'s manual cross-session friction
   scan — the resume unit is a session, gated on its transcript's own mtime
   (`LedgerCursor`), so a rerun only rescans sessions that grew.

6. **State each candidate's functional ground.** Read `evidence.json`'s items.
   For each, write one ground record: `detector`, `functional_ground` (the
   `функция`-layer cause, not the symptom — see
   `memory-global/leaves/function-place-difficulty.md`), `title`,
   `evidence_refs`, and a `cost_signal` (`usd_per_week`/`attention_per_week`/
   `stability_per_week`, `basis`, `measured`) if one is derivable from the
   evidence — leave it absent rather than inventing a figure. Collect these as
   a JSON list, `grounds.json`.

7. **Telemetry producer, store mode** (dedup + persist):
   ```
   python3 scripts/improvement-scan.py telemetry --grounds grounds.json \
     --store <store>
   ```
   A ground that a judge finds the same as an existing board item or experience leaf is
   deduped, not double-counted — read the printed dedup outcomes, don't ignore them.
   The next step, `file-difficulty.py`, itself checks open records before filing: a judged
   match is commented on (or refused with exit 3 under `--no-comment-on-match`), and
   `record-experience.py new` asks a judge before creating a leaf.

8. **Render the unified report:**
   ```
   python3 scripts/improvement-scan.py report --store <store> --format md
   ```
   The report opens with the board lanes: measured loss (min/week,
   sessions/window, min/occurrence, basis; a family is one row), the silent lane
   (qualitative estimate, no signature) and awaiting loss classification;
   the stored findings follow, ranked cost-first and never interleaving
   measured and unmeasured bands. Severity and fix cost are tie-break
   inputs only. Present its output as-is, in the dialogue
   language, with the recommended next step already attached per finding. A
   backlog item ranked at an addressed cluster's cost carries a `via <key>`
   note; keys matching no open telemetry row are listed as dangling. Items
   without a rank (no-urgency-signal, unjudged) are not joined.

9. **Optionally republish the board as a human-readable view** rendered from
   the state file (`Artifact action:"publish"`, the current `url:` named in
   `backlog-triage-practice.md` § Reuse across runs). The artifact is output
   only — never read it back as input.

10. **Present the ranked report to the user**, in the dialogue language, and
    stop. Do not file, dispatch, or pre-select an item — that is a separate,
    later, explicit appeal.

## What this is not

- Not a replacement for `self-improvement` (reactive, fires the same turn as a
  stated user correction — CLAUDE.md § When the user corrects agent behavior).
  This skill is proactive and runs on its own trigger, never as a substitute
  for that reactive obligation.
- Not the due-hook (`scripts/hook-improvement-scan-due.py`). The hook only
  nudges — it counts already-stored open findings and prints a reminder to run
  this skill; it never runs the producers itself.

## See also

- `memory-global/leaves/improvement-scan.md` — the two producers' resume
  schemes and why each differs, and the report-only boundary in leaf form.
- `memory-global/leaves/backlog-triage-practice.md` — the priority rubric and
  classification vocabulary this skill's step 3 applies.
- `memory-global/leaves/systemic-pattern-scan.md` — the manual scan step 5-7
  mechanize.
- `docs/operations/improvement-scan.md` — command-line reference for
  `scripts/improvement-scan.py` outside this skill's orchestration.
