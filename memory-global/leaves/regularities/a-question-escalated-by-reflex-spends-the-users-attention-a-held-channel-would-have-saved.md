---
name: a-question-escalated-by-reflex-spends-the-users-attention-a-held-channel-would-have-saved
description: Escalating a question to the user is the easiest act, and checking whether a held channel would close it is skipped unless something forces the check
type: reference
schema: regularity/v1
generality: 1
induced_from: [own-research-precedes-escalation]
rule_derived: "lifted from own-research-precedes-escalation.md (Difficulty and Guidance); no experience leaf backs it"
introduced_by: grounding-norm
created: 2026-10-30
last_verified: 2026-10-30
---

# a-question-escalated-by-reflex-spends-the-users-attention-a-held-channel-would-have-saved

## Regularity

When a question occurs, escalating it to the user is the easiest act, and checking whether a channel already held (memory, the repo, docs, an MCP, a domain-expert subagent) would close it is skipped unless something forces the check. The engine forces it only for questions recorded in the premise bag and only at the plan-approval gate; a question arising mid-execution or at plan review is bound by nothing. A question the agent could have answered itself consumes the user's attention, the scarcest term in the objective function.

## Generality

Level 1 — ranges over knowledge questions that arise to an agent during planning, plan review or execution while it holds channels able to answer them. It is deliberately not claimed for decision, permission or preference questions, which only the user can give.

## Induced from

- [[own-research-precedes-escalation]] — contributes the observed practice behind the statement: only premise-bag questions at plan approval are forced through research by the engine, so questions arising elsewhere are escalated by reflex.

## Refutation

Mid-execution questions escalated without prior research found to be, in a sample, as rarely answerable from a held channel as researched ones; or no attention cost found to attach to a question the user must answer.
