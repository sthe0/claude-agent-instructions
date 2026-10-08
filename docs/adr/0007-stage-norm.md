# ADR-0007: StageNorm — one object behind a stage's review, interface and carry identities

- Status: Accepted 2026-10-08
- Plan: `stagenorm-319.toml` (Core issue #319)

## Context

Three digests each answered "did this stage's norm change?" from their own hand-maintained field list: `stage_question_key` (review and question dispositions), `stage_carry_key` (carrying a PASSED verdict across a plan edit) and `stage_interface_digest` (what a consumer of the stage relies on). Their overlap was uncontrolled, and three defects followed:

- the carry key held a supplier only as an index, so retyping an edge (its element, artifact or delivery) at an unchanged index carried a stale PASSED verdict;
- a refinement owed review to every moved stage with no rule for consumers, and a consumer pair's currency followed `service_key` / `service_file` instead of the product the service declares;
- an edge could not say how its provision arrives. A stage needed only for its working tree looked identical to one needed for a file, and `_continuation_worktree` could only infer from `depends_on`.

Core #319 also reports that stage edges are not held to the base-service principle (28% of 515 measured plans had bare edges, the legacy population).

## Decision

1. **`StageNorm`** (`scripts/agentctl/stage_norm.py`) holds a stage's norm once, as plain serializable data: the order a spawned specialist, a depth n+1 manager, would receive. Three digests are projections of it:
   - `review_digest` is byte-identical to the former `stage_question_key`; `delivery` joins only as a conditional trailing splice, so every persisted `Question.disposed_at_key` and every legacy plan hashes as before;
   - `interface_digest` equals the former `stage_interface_digest`;
   - `carry_digest` replaces the `depends_on` slot with sorted `(on, element, artifact, delivery)` tuples.
2. **Carry** (`stage_carried`): a PASSED stage is carried iff its `carry_digest` and every direct supplier's `interface_digest` are unchanged, at both carry sites (approve-time refresh and substantive replan). Anything else is reset to PENDING.
3. **Review scope** (`gates.review_scope`): moved stages plus the direct consumers of interface-moved stages. A legacy baseline without interface digests widens to the consumers, never narrows. Pair currency follows `interface_digest`.
4. **Typed edges.** `delivery` is an optional field of `[[stage.supplies]]` with the closed vocabulary `artifact | continuation | report`. `artifact` must name a supplier `output_artifacts` entry; `continuation` needs a `spawn` supplier; an unknown value is a `PlanError`. `_continuation_worktree` follows an explicit delivery per edge and falls back to the `depends_on` inference only where an edge says nothing. A bare edge (no `element`) in a new substantive plan is refused by `submit-plan`, as before; legacy plans load.

## Consequences

- No persisted key changes: review digests, question dispositions and legacy plans are untouched. A plan that adds its first `delivery` moves that stage's `review_digest`, which is intended.
- `delivery` stays optional, so no plan in flight breaks. `element` stays mandatory; the "element and/or artifact" widening proposed in #319 is declined, because an artifact names a file but not the activity place it fills.
- The first carry decision reads supplier interfaces, so a construction-only change to a supplier no longer resets its consumers.
- Stage identity is still the index. Reordering stages resets them.

## Not decided here

The order's corrections to the original plan are folded into this ADR: (a) the delivery vocabulary must contain `continuation`, because #319's example is that case and `_continuation_worktree` was gated on `depends_on`; (b) a `restructure` pair verdict belongs to a plan-level difficulty cycle (failure address: normative) that reviews only the changed structure and counts as an approach replan, not a review round; (c) bare-edge refusal was already live for `element`, so the stage reduced to pinning tests and the delivery checks.

The `restructure` verdict is a follow-up that needs a new pair-verdict value, a route into DIAGNOSING and a round-accounting decision. `StageNorm` leaves room for it: edge fields are already in `carry_digest`, the edge list is data, and the structure-change test stays in `plan._structural_signature`.
