---
name: improvement-scan
description: TRIGGER when the user asks you to proactively improve the agent system itself, review its own backlog, or look for recurring problems in its own recent work — WITHOUT a specific correction driving it (that's self-improvement's, reactive-only) — in any language, e.g. "scan yourself for improvements" / "what should we fix in the agent" / the Russian trigger «сделай себя лучше». Runs both standing producers (backlog reconciliation against the Triage Board, recent-session telemetry pattern detection) end to end, ranks findings cost-first in one report, and STOPS — never files a difficulty, dispatches a specialist, or auto-selects work. SKIP if the user names one specific item to act on (ordinary task routing, not a scan).
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

1. **Fetch the current board.** `Artifact action:"read"` on the "Triage Board"
   artifact named in `backlog-triage-practice.md` § Reuse across runs. Write its
   JSON body to a session-scratch path (e.g. `board-prior.json`) — this is the
   `--prior` input to phase A, not a fresh derivation from nothing.

2. **Backlog producer, phase A** (collect + diff):
   ```
   python3 scripts/improvement-scan.py backlog --prior board-prior.json \
     --emit-worklist worklist.json
   ```
   Read the printed new/changed/closed/coverage-gap counts.

3. **Classify** (the perception step). For every item in `worklist.json`'s
   `items` (buckets `new`/`changed`), supply, per item ref:
   `breadth` (`narrow`/`shared-mechanism`/`universal`), `cost_to_resolve`
   (a budget tier key), `in_flight` (a readiness coefficient key), and
   `recommended_next_step` (one of `self-improvement`/`planner`/
   `file-difficulty`) — reasoning from the item's own text per
   `backlog-triage-practice.md` § Priority rubric, never guessed. An item with
   no severity label and no cluster-mate gets `no_urgency_signal: true` instead
   of a guessed weight. Write the result plus `worklist.json`'s `closed_refs`
   as `classifications.json` (shape: `{"items": {<ref>: {...}}, "closed_refs":
   [...]}`).

4. **Backlog producer, phase B** (score + merge):
   ```
   python3 scripts/improvement-scan.py backlog --prior board-prior.json \
     --classifications classifications.json --out board-new.json --store <store>
   ```
   An out-of-vocabulary value here is rejected before anything is written —
   fix the classification and rerun rather than loosening the vocabulary.

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
     --board board-new.json --store <store>
   ```
   A ground that matches an existing board item or experience leaf is deduped,
   not double-counted — read the printed dedup outcomes, don't ignore them.

8. **Render the unified report:**
   ```
   python3 scripts/improvement-scan.py report --store <store> --format md
   ```
   The renderer ranks cost-first across both producers and never interleaves
   measured and unmeasured bands — present its output as-is, in the dialogue
   language, with the recommended next step already attached per finding.

9. **Republish the SAME board artifact in place** (`Artifact action:"publish"`
   with the existing `url:` from step 1 — never a new "Triage Board"-titled
   artifact; see `backlog-triage-practice.md` § Reuse across runs for why a
   second one is a standing anti-pattern).

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
