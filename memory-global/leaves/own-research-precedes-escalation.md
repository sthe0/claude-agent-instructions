---
name: own-research-precedes-escalation
description: Before a question reaches the user, spend the cheap channels you already hold (memory, repo, docs/web, MCP, a domain-expert subagent) — at every moment a question arises, not only at plan approval.
type: reference
schema: principle/v1
converted_from: leaf/v1
grounded_in: [regularity:a-question-escalated-by-reflex-spends-the-users-attention-a-held-channel-would-have-saved, regularity:a-users-revising-decision-reads-as-settled-though-its-technical-premise-was-never-checked]
grounding_judged: adbc898249119f8dfc9530109f18763ccbb09271124d5161a079e43f5571bf14
created: 2026-09-15
last_verified: 2026-09-15
---

## Difficulty

A question you could have answered yourself spends the user's attention, the scarcest term in the objective function. It is easy to escalate reflexively at the moment a question occurs, without first checking whether a channel already held would have closed it.

## Principle

To spare the user's attention, before a knowledge question reaches the user spend the cheap channels already held (memory, the repo, docs and web search, an MCP, a domain-expert subagent) at every moment a question arises, including mid-execution and at plan review via question-raise, question-research and question-dispose; ask decision, permission and preference questions directly; and when a user decision replaces an earlier one already in the plan, raise its unverified technical premise as a question before recording it into the done criterion or final check.

## Goal class

questions that reach the user only when a held channel cannot close them

## Grounded in

- [[a-question-escalated-by-reflex-spends-the-users-attention-a-held-channel-would-have-saved]]
- [[a-users-revising-decision-reads-as-settled-though-its-technical-premise-was-never-checked]]

## Guidance

Before a question reaches the user, spend the cheap channels you already hold: memory, the repo, docs/web search, an MCP, and any **domain-expert subagent this project defines** — that subagent is a capability you hold, so skipping it is [capability-before-offload](capability-before-offload.md) on the *question* axis.

The engine already codes part of this rule (`question-dispose --to escalated` refuses on empty `own_research`; authority `premise.validate_questions`), but it binds only questions recorded in the `premise` bag and only at the `plan_approval` gate. A question arising **mid-execution, or at plan review**, is bound by nothing but you — so run `question-raise` → `question-research` → `question-dispose` there too, not only at plan-approval time.

**Scope by kind.** A **knowledge** question is what a held channel closes — research it yourself first. A **decision**, **permission**, or **preference** question is the user's to give and carries no research precondition — asking it directly is not a failure of this rule.

**A revising decision can hide its own unverified premise.** When a user's decision *replaces* an earlier one already baked into the plan, the new decision often reads as settled fact because the user decided it — not because its own technical premise was checked against code. Recording it straight into `done_criterion`/`final_check` without first asking "what unverified technical premise does this revision assume?" and raising that premise via `question-raise` lets it ride unexamined through several plan-review rounds, surfacing only when an independent reviewer (or a landed run) contradicts it. (Trigger: an internal project's monitoring-config task, 2026-09-21 — a user-approved architecture revision, superseding an earlier one already reflected in the plan text, was wired into `[meta] done_criterion` and `[[final_check]]` across three plan-review rounds before a fourth review caught that the revision's own premise — a runtime-routing behavior never checked against code — was unverified, leaving a stage assertion built on it.)

This leaf is the elaboration of a `CLAUDE.md` paragraph, extracted 2026-09-15 to keep `CLAUDE.md` under `claude-md-max-chars` while landing the [subagent-destructive-guard](experience/2026-09-15-subagent-self-authorized-destructive-action.md) plan's rule (B).

## See also

- [capability-before-offload.md](capability-before-offload.md)
- [question-provenance-gate.md](question-provenance-gate.md)
