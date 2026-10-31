---
name: an-answer-composed-before-a-timed-out-ask-is-never-delivered-unless-the-ask-is-retried
description: An answer written mid-turn whose delivering AskUserQuestion times out is lost unless retried, and a question embedded in a reply is not exhausted by the chosen option
type: reference
schema: regularity/v1
generality: 1
induced_from: [unanswered-question-survives-turn]
rule_derived: "lifted from unanswered-question-survives-turn; no experience leaf backs it"
introduced_by: grounding-norm
created: 2026-10-30
last_verified: 2026-10-30
---

# an-answer-composed-before-a-timed-out-ask-is-never-delivered-unless-the-ask-is-retried

## Regularity

When an answer to the user is written mid-turn and the AskUserQuestion that would have delivered it times out, the session continues autonomously and nothing forces a retry, so the answer never reaches the user and the user does not know one was prepared; a question embedded in a user's reply, including inside an Other free-text answer, is likewise not exhausted by the choice of an option.

## Generality

Level 1 — ranges over agent turns that deliver an answer through an AskUserQuestion and over user replies that carry an embedded question. It is deliberately not claimed for answers delivered in the turn's own final text, which does not depend on the ask.

## Induced from

- [[unanswered-question-survives-turn]] — contributes the observed case behind the statement: an answer composed before a timed-out ask, followed by autonomous continuation, that the user never received.

## Refutation

Observing a session where an AskUserQuestion timed out after answer content was written mid-turn, the session continued, and the answer still reached the user without anyone deliberately re-asking, or where an embedded question in an Other answer was answered solely by processing the chosen option, would show the statement false.
