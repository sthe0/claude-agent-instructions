# ADR-0014: Order agreement — the user agrees the agent's understanding of the order before any stage is written

- Status: Proposed 2026-10-29
- Plan: `order-agreement.toml` (the implementing plan, kept in the agent's plans directory)

## Context

The difficulty: a substantive plan was built, reviewed, approved and run on an understanding of the order that the user never confirmed. `[meta.order]` held the customer, the functional place and the requirements, but nothing made the user look at them before the stages existed; approval then judged the stages and the understanding in one pass, and a misread order surfaced only when a delivery missed it. Two further gaps compounded it: a user's own idea arrived as a requirement (a proposed solution recorded as a need), and a constraint the agent offered could become binding without the user ever accepting it.

The design hypothesis was that a prose rule in `CLAUDE.md` plus a reviewer's judgment would be enough. Its critique: a rule the model must remember is skipped exactly when the plan is large, and an "agreement" the acting agent records itself is no evidence of the user's answer. Hence the split of [CLAUDE.md](../../CLAUDE.md) § Separate rule from perception: the decidable half (was the understanding agreed, does the plan still match it) is enforced by the engine; the perception half (reconstructing the activity, criticizing a proposal) stays with the model.

## Decision

1. **The order carries the understanding.** `[meta.order]` gains `activity`, `difficulty`, `proposals` (each with a verdict `adopt` / `adapt` / `reject`, a critique, and `against`, the requirement ids it is judged against) and `constraints` (offered, binding only through agreement). A user's idea is a criticized proposal, not a requirement. `plan.understanding_digest` hashes these parts and not the stages, so a plan that differs from the agreed understanding differs in a named part.
2. **An order phase precedes the stages.** `agentctl order-submit` records a stage-less draft; `plan-review --scope order`, run by `plan-review-topological.py` over `unit:base`, records the standard base review of it; `present-plan --kind order` shows the draft in the dialogue language with every requirement, proposal and constraint id as an anchor, and the receipt binds the understanding digest as well as the rendering.
3. **The agreement rests on the user's answer.** The presentation ends with an ask whose agree option carries `[order-agree:<digest12>]`. A PostToolUse hook (`hook-ask-answer-record.py`) appends every answered `AskUserQuestion` to the session's answer ledger; `agentctl order-agree` accepts only a ledger row where the user chose the marked option. No transcript is read and no flag overrides the check.
4. **A gate holds the plan to the agreement.** `gates.order_agreement_blockers` refuses `submit-plan`, `approve`, `replan` and the essence and replan-diff presentations while there is no agreement, the plan's understanding differs from the agreed one, or a challenge is open.
5. **A defect in the agreed understanding is a challenge for the user.** A reviewer's blocking concern tagged `understanding:` becomes an `UnderstandingChallenge` that only the user closes (`order-challenge-dismiss`, or a new `order-agree` over a changed understanding). A defect the coordinator can fix in how the plan uses the base stays `order:`.
6. **The gate arms only where its evidence channel exists.** `classify` arms it for a substantive root session on an interactive claude host with the answer hook registered; linked and spawned children, the Cursor host, a headless delivery channel and an unregistered hook are exempt, and the reason is recorded; a session last classified before landing stays unarmed, with no reason, until its next substantive `classify`. A pushed sub-plan runs outside the gate — not held to the parent's agreement and not asked again — and `pop-subplan` restores the parent's agreement.

## Consequences

- A misread order is caught before stages are written; approving the plan no longer doubles as approving the understanding.
- The acting agent cannot satisfy the gate: the evidence is written by a harness hook, not by an `agentctl` verb.
- Stored plans whose order declares none of the new fields keep their digests, and sessions that predate the change load unarmed.
- Where the hook cannot run (Cursor, headless runs, a machine without the hook) the order stays unagreed; the exemption is visible in `classify` and `status`, not silent.
- The design is pinned by mutants (`scripts/tests/order_agreement_mutants.py`) and an end-to-end CLI test.
- Assumption A2: the root reconstructs the order in-thread and writes the order-only draft itself; no new return marker is added to the specialist protocol. A spawned planner authoring the draft would need one.
