---
name: unanswered-question-survives-turn
description: When a user's reply (including an "Other" answer) contains a question, or an AskUserQuestion times out after answer content was already written mid-turn, restate the full answer and re-ask at the next gate rather than letting the question silently drop.
type: reference
schema: principle/v1
converted_from: leaf/v1
grounded_in: [regularity:an-answer-composed-before-a-timed-out-ask-is-never-delivered-unless-the-ask-is-retried]
grounding_judged: 48c1d59ef135c5379764fb2b0032378d014b47e3911f7dc3eb76f95018d9d270
created: 2026-09-27
last_verified: 2026-09-27
---

## Difficulty

A mid-turn answer followed by an `AskUserQuestion` timeout and autonomous continuation can silently never reach the user — the answer was composed, the question that would have delivered it timed out, and nothing forces a retry. The user is left without the answer and without knowing one was ever prepared.

## Principle

To ensure the user receives every answer owed to them, restate the full answer and re-ask at the very next gate when a user's reply (including an Other free-text answer) embeds a question, or when an AskUserQuestion times out after answer content was already written mid-turn.

## Goal class

answers owed to the user actually delivered

## Grounded in

- [[an-answer-composed-before-a-timed-out-ask-is-never-delivered-unless-the-ask-is-retried]]

## Guidance

A user's reply carrying an embedded question (including inside an "Other" free-text answer) is not exhausted by picking an option — the embedded question still needs an answer. And an `AskUserQuestion` timeout after answer content was already written mid-turn is a **non-answer**, not a resolved turn: restate the full answer and re-ask at the very next gate rather than letting the timeout quietly close the loop.

Extracted 2026-09-27 from `CLAUDE.md` § Escalation to the user during instruction grooming (headroom recovery under `claude-md-max-chars`) — the rule sentence stays inline there; this leaf carries the full elaboration.

## See also

- [question-provenance-gate.md](question-provenance-gate.md)
