# Topological review pair: 3-2

Base: stage 3 (Wire CI). Service: stage 2 (Add tests).

## Order context

*(the plan declares no order)*

## Base: stage 3 (Wire CI)

# Plan: Demonstrate the first-review pair bundle

- **Task id:** golden-pair-bundle
- **Weight class:** small_change
- **Overall done criterion:** all three stages PASSED and resolution confirmed
- **Overall criterion type:** measurable

This is a PROJECTED BRIEF of stage 3 only, out of 3 stage(s) in the plan — the other stages are not shown and are not this step's concern.

## Stage 3: Wire CI

- **Executor:** spawn:developer
- **Expected result image:** CI runs the new tests on push
- **Criterion type:** measurable
- **Done criterion:** CI config references test_mod
- **Output artifacts:** .github/workflows/ci.yml
- **Depends on** (direct dependencies only; see their own stage for detail):
  - Stage 2: Add tests
    - **Its expected result image:** pytest green for the new module
    - **Its output artifacts:** test_mod.py
- **Supplies** (raw provision edges this stage declares):
  - on stage 2
- **Grants (file-access scope):**
  - derived allow: Edit(//./.github/workflows/ci.yml)

## Service declared product: stage 2 (Add tests)

## Stage 2: Add tests

- **Expected result image:** pytest green for the new module
- **Criterion type:** measurable
- **Done criterion:** pytest test_mod.py green
- **Output artifacts:**
  - `test_mod.py`

## Edge

- Edge: stage 3 relies on stage 2 — supplies whole product
- Reliance set of stage 3: stage 2 (supplies)
- Ordering: engine-ordered

## Service file

The service's full node file is one `Read` away in the view directory `/golden/view`:
- `/golden/view/stage-2.md`

## Per-pair procedure

1. Decide `C4:` (the service's declared product covers the part of the base attributed to this edge) from the service's declared product above first.
2. Only when that section cannot decide it, `Read` the one file listed in the service file section. The base is inlined above in full.
3. Report any gap as one `C4:` line.
4. Check the remaining conditions below for this pair only, then reply per the protocol. Judge nothing about other stages.

Plan digest: 0000000000000000000000000000000000000000000000000000000000000000

## Review protocol

Reply with these lines, in this order:
- `REVIEW:` on a line of its own;
- `Verdict: <pass|revise>`;
- `Plan digest: <sha256>` — echo the `Plan digest:` line above verbatim; do not compute it;
- one concern per line, written `blocking: [re:<concern-id>] <marker> <concern>` or `note: ...`, where <marker> is the condition the concern concerns (`C1:`, `C2:`, `C3:`, `C4:`); a condition-4 gap is a `C4:` line. An untagged concern line is refused.
  - `blocking:` keeps the plan from passing; `note:` is recorded and does not. A `Verdict: pass` carries `note:` lines only.
  - Block only on a part that changed since the last review of this pair, or on a part that still carries an unresolved blocker, re-raised as `re:<concern-id>` (the stable id of the earlier concern). A blocking concern on an unchanged part is recorded as advisory.
- after the concerns, a line `Customer questions:` followed by one `Q: <question>` line per question only the customer can decide (a choice or a fact this pair depends on and the plan does not settle), or `Customer questions: none`. A question is not a plan remark: a flaw the coordinator can fix is a concern, not a question.
The REVIEW: block is the last thing in your reply, with no other marker (COMPLETED:, REPLAN:, etc.) after it.

Conditions:
- `C1:` every need of stage 3 (Wire CI) is attributed to a declared edge — it is organized in a non-arbitrary way
- `C2:` stage 3 (Wire CI) is a genuine derivation from the order through this edge
- `C3:` stage 3 (Wire CI) delivers its FULL declared product — the part that depends on stage 2 (Add tests)'s product measured against it, the rest standing on its own
- `C4:` stage 2 (Add tests)'s declared product covers the part of stage 3 (Wire CI) attributed to this edge
