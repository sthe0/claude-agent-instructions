---
name: instruction-grooming
description: TRIGGER when an instruction file crosses the lint-prose-length.py WARN threshold (>=90% of its ceiling — CLAUDE.md, README.md, cursor mirror, or any SKILL.md/policy.md) either flagged directly by the linter or via the hook-instruction-grooming-due.py OFFER, or when the user asks to slim / groom / consolidate / deduplicate a bloated instruction file. Also usable proactively before a file is expected to grow further. Work through measure -> survey -> verify -> plan; the actual edit still rides the standard plan-approval spine. SKIP when the file is comfortably under 90% of its ceiling and no user request references grooming.
---

# Instruction grooming

Recovers headroom in an instruction file **without losing meaning** — content moves to a leaf or a sibling `policy.md`, it is never deleted. This is the repeatable procedure behind the CLAUDE.md 99.6%-to-33000-byte recovery: measure the bloat, survey it for duplication / mechanizable rules / prose-vs-mechanism drift, verify nothing depends on the exact text before moving it, then let the edit itself go through the normal plan-approval gate like any other production change.

## 1. Measure

Run the linter and get a per-section breakdown of the offending file(s):

```
python3 scripts/lint-prose-length.py
```

Any `WARN` line names a file and its `%` of ceiling (`WARN_FRACTION = 0.90` in `lint-prose-length.py`). For the flagged file, break it down by top-level (`## `) section to find where the bytes actually live — a one-off `awk`/`python` byte-count per section is enough; do not guess from memory which section is heaviest.

## 2. Survey

Spawn cheap `Explore`/`general-purpose` agents (never inline on opus — see `delegatable-work-patterns.md`) to classify the bloat into three buckets, each with a different fix:

| Bucket | Signal | Fix |
|---|---|---|
| **Duplication** | The same rule or block appears in >=2 files (e.g. a protocol block copy-pasted across several `SKILL.md`) | Centralize at the layer that composes the duplicates (a shared file the consumer includes/appends), strip the copies to a pointer |
| **Mechanizable-but-still-prose** | The rule's *rule part* (order, gate, dispatch, validation) is already delivered by a runtime mechanism (an `agentctl` Directive, a hook nudge, a guard script) but the file still spells out the mechanics in prose | Compress to the *perception* the model still owns + a pointer to the mechanism; the engine/hook already re-delivers the rest at the decision point |
| **Extractable narrative** | A rule with a clear "why" and no runtime re-delivery point, but not needed on every load | Extract full text to a `memory-global/leaves/<slug>.md` (or sibling `policy.md` for a skill body), leave a 1-3 line rule + pointer in place |

## 3. Verify consumers and guards — before moving anything

A move without checking who reads the exact text breaks silently. Before extracting or centralizing a block:

- `grep` for any script that anchors on that text: `verify-*` guards (`verify-cross-refs.py`, `verify-doc-concepts.py`, `lint-cursor-mirror.py`), `spawn-specialist.py`'s prompt-composition logic.
- If a guard pins the moved content (line count, a specific phrase, a required cross-reference), update the guard **in the same commit** as the move — a move and its guard drift apart otherwise (experience: `2026-06-26-guard-coupled-doc-relocation`).
- If a spawn template composes the file as a system prompt (e.g. specialization `SKILL.md`s appended at `claude -p` spawn time), confirm the spawned process still receives the full moved text — either inline it at compose time or leave a real markdown link a human/agent can follow; a dangling relative link a spawned process can't resolve is silent information loss (experience: `2026-06-24-prose-to-code-migration-consumer-and-superset`).

## 4. Plan and execute on the normal spine

Grooming edits are production changes to the agent's own instructions — they do **not** bypass the plan-approval gate. Turn the survey into an edit plan (per-file: what moves where, what pointer replaces it) and route it through `planner` -> user approval -> `developer` like any other substantive change; a *small change* (single file, <= `small-change-max-lines`) may run in-thread per the usual carve-out.

Two invariants the plan must preserve:

- **Meaning-preserving.** Every extracted block's full text is present in its new home; the replacement in the source file is a rule-sentence + one-line difficulty + pointer, not a summary that drops detail.
- **Shrink past the dedup floor needs a delivery-point compensator.** Once duplication and dead weight are gone, further shrinkage is invariant extraction — a reliability tradeoff (experience: `memory-global/leaves/experience/2026-06-25-claude-md-reduction-floor.md`). Only extract a rule with no runtime re-delivery mechanism if you're prepared to accept it is re-read less often; prefer extracting rules a hook/gate/engine node already re-delivers at their decision point — landing discipline via `hook-resolution-reminder.py` is the canonical example.

## 5. Confirm

Re-run `python3 scripts/lint-prose-length.py` (expect headroom, no new WARN elsewhere) and `python3 scripts/verify-all.py` (guards + cross-refs green) before considering the grooming done.
