---
name: own-research-precedes-escalation
description: Before a question reaches the user, spend the cheap channels you already hold (memory, repo, docs/web, MCP, a domain-expert subagent) — at every moment a question arises, not only at plan approval.
type: reference
schema: leaf/v1
created: 2026-09-15
last_verified: 2026-09-15
---

## Difficulty

A question you could have answered yourself spends the user's attention, the scarcest term in the objective function. It is easy to escalate reflexively at the moment a question occurs, without first checking whether a channel already held would have closed it.

## Guidance

Before a question reaches the user, spend the cheap channels you already hold: memory, the repo, docs/web search, an MCP, and any **domain-expert subagent this project defines** — that subagent is a capability you hold, so skipping it is [capability-before-offload](capability-before-offload.md) on the *question* axis.

The engine already codes part of this rule (`question-dispose --to escalated` refuses on empty `own_research`; authority `premise.validate_questions`), but it binds only questions recorded in the `premise` bag and only at the `plan_approval` gate. A question arising **mid-execution, or at plan review**, is bound by nothing but you — so run `question-raise` → `question-research` → `question-dispose` there too, not only at plan-approval time.

**Scope by kind.** A **knowledge** question is what a held channel closes — research it yourself first. A **decision**, **permission**, or **preference** question is the user's to give and carries no research precondition — asking it directly is not a failure of this rule.

This leaf is the elaboration of a `CLAUDE.md` paragraph, extracted 2026-09-15 to keep `CLAUDE.md` under `claude-md-max-chars` while landing the [subagent-destructive-guard](experience/2026-09-15-subagent-self-authorized-destructive-action.md) plan's rule (B).

## See also

- [capability-before-offload.md](capability-before-offload.md)
- [question-provenance-gate.md](question-provenance-gate.md)
