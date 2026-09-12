# Principles

Generality-graded, provenance-rooted, **refutable** principles induced from recurring difficulties
(ADR-0001 § *Principle as a concept with a generality gradient*). Each leaf carries `schema:
principle/v1` — a statement at its generality level (0 trivial → 3 cross-domain invariant), an
`induced_from` link **down** to the difficulties it generalizes, and a refutation condition
(refutation ≡ generalization). Schema: [principle-leaf-schema.md](../principle-leaf-schema.md).

Sub-index of `memory-global/leaves/principles/`. Pointed at from `memory-global/MEMORY.md`. Not
auto-loaded by the harness. **Consumption:** at a plan's `refutable principle` element the planner
retrieves the relevant principle(s) to ground each stage (retrieval-augmented planning). Leaves MAY
carry an optional `domain:` tag (e.g. `domain: coordination`) consumed by
`record-experience.py search --domain`; untagged leaves match any domain filter (orthogonal to the
Level grouping below).

These principle leaves are the **generality≥1 profile** of one difficulty-record model whose
**generality-0 profile** is the `difficulty/v1` experience leaf ([experience/MEMORY.md](../experience/MEMORY.md));
the two sub-indexes stay physically separate but record two profiles of the same model.

## Level 3 — cross-domain invariants

- [Every result is critiqued against its declared result-image](result-checked-against-its-result-image.md) — the critique primitive (compare expected vs actual, extract the difference) applied to one stage; skipping the check is itself a difficulty. ← `coordinator-pitfalls`, `2026-06-24-gate-exemption-is-category-error-for-result-images`.
- [A complete option space is generated from the functional ground](option-space-spans-axes-from-functional-ground.md) — span orthogonal axes (passive/active, batch/continuous, precedence/synthesis); generate options from the difficulty's ground, not a mechanism catalogue; ask what invariant subsumes them. ← `coordinator-pitfalls`, `2026-06-26-critique-primitive-unifies-conflict-and-principle`.
- [The reflexive exit is the base activity figure over a norm](reflexive-exit-is-base-activity-figure.md) — self-improvement / overcome-difficulty / proactive self-diagnosis are one figure with material=the norm (transform=re-norming, control=achievability of the norm's result, result=norm adequate to the order); its search space is the knowledge space = memory. ← `2026-07-14-smd-principle-norm-category-error-and-budget-exhaustion`, `function-place-difficulty`.

## Level 1 — sibling-context rules

- [A review loop with a non-zero finding floor does not terminate by construction](review-loop-cannot-measure-its-own-convergence.md) — four mechanisms explain why a bounded round count is load-bearing, not a heuristic: unanswerable hypothesis, no verb for "study cannot be done," iatrogenic floor, artifact-to-machinery gap. ← `2026-08-11-review-loop-cannot-measure-its-own-convergence`.

## Level 2 — task-class rules

- [A verdict covers the evidence domain it claims](verdict-covers-the-evidence-domain-it-claims.md) — a gate demanding proof whose PROVER is absent, and a checker enumerating from the git index while reading the working tree, are one fault: a verdict issued over evidence never actually looked at. Escape hatches stay reachable, diagnosed, typed and counted; surfacing an obligation never discharges it. ← `ask-user-question-split-turn`, `2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan`.
- [The coordinator executes through specialists](coordinator-executes-through-specialists.md) — achieve production change by dispatch, not direct root edits; direct Bash/Edit/Write on substantive work is a difficulty signal. ← `coordinator-pitfalls`.
- [A judging artefact is executed before it judges](a-judging-artefact-is-executed-before-it-judges.md) — a plan's controls / CI job / lint rule is run read-only against the real environment before the work it judges is reviewed or approved; every gate on the `submit_plan` path reads a `verify_command` as TEXT, so a control defect surfaces only at `record-result`, downstream of the two most expensive gates. ← `2026-07-09-gate-must-execute-what-it-attests`, `2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan`, `2026-07-20-stage-verify-command-narrower-than-final-check`.
- [A shared resource's health is asserted relative to a pinned base](shared-resource-health-asserted-relative-to-a-pinned-base.md) — a control over co-owned state (trunk's position, trunk's suite) asserts subset-of-base-failures and `passed >= passed_at_base`, never zero-failures or a constant; the base is pinned as a sha AND measured once into a committed artifact. ← `2026-06-30-shared-tree-suite-failure-wrong-ownership-attribution`, `2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan`.
- [A re-norming at principle level sweeps the class](renorming-at-principle-level-sweeps-the-class.md) — when a critique names a *form*, the corrected plan enumerates every site carrying it and fixes-or-exempts each, as an executed step; the engine's replan-coverage gate checks only that each named difference lands somewhere, never that siblings were swept. ← `2026-07-20-agentctl-premise-gate-blocks-venue-refinement-replan`, `2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan`.
- [Determinizing a specialist's behavior: a four-step recipe](specialist-behavior-determinization-recipe.md) — split a specialist's judgment call into a decidable rule half (mechanized as a prefilter/truth-table/typed contract) and an irreducible perception half (kept behind that mechanism, never in front of it), with a named extension seam. ← `regex-not-for-semantic-classification`, `partition-markers`.
