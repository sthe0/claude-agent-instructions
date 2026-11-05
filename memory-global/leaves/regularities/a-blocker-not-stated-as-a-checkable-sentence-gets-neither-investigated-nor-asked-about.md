---
name: a-blocker-not-stated-as-a-checkable-sentence-gets-neither-investigated-nor-asked-about
description: A blocker in self-devised workaround work never named as a checkable sentence gets neither investigated nor put as a question to the user
type: reference
schema: regularity/v1
generality: 1
induced_from: [mini-od-self-devised-workaround]
rule_derived: "lifted from mini-od-self-devised-workaround.md (Difficulty and Guidance); no experience leaf backs it"
introduced_by: grounding-norm
created: 2026-10-30
last_verified: 2026-10-30
---

# a-blocker-not-stated-as-a-checkable-sentence-gets-neither-investigated-nor-asked-about

## Regularity

A blocker met while building or running a workaround the agent invented itself (a credential exchange, a wrapper script, a one-off build) is not covered by the declare-before-retry discipline that covers a failed orchestrated job. If it is never named as a concrete sentence of what is missing or blocking, the response drifts into successive improvised attempts or into silent stalling; the blocker has no content to investigate and none to put as a question, so it sits until the user notices and names each next step themselves.

## Generality

Level 1 — ranges over blockers met while building or running a workaround that the agent devised itself, outside any orchestrated job. It is deliberately not claimed for failed orchestrated jobs, where the declare-before-retry discipline already applies.

## Induced from

- [[mini-od-self-devised-workaround]] — contributes the observed case of a control-run subtask whose local blockers were never declared, so the user had to name each next concrete step one at a time.

## Refutation

Blockers in self-devised workaround work found to be resolved or escalated as early when no Mismatch was declared as when one was; or users found not to need to direct the next step after an unnamed blocker.
