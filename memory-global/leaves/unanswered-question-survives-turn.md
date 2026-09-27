---
name: unanswered-question-survives-turn
description: When a user's reply (including an "Other" answer) contains a question, or an AskUserQuestion times out after answer content was already written mid-turn, restate the full answer and re-ask at the next gate rather than letting the question silently drop.
type: reference
schema: leaf/v1
created: 2026-09-27
last_verified: 2026-09-27
---

## Difficulty

A mid-turn answer followed by an `AskUserQuestion` timeout and autonomous continuation can silently never reach the user — the answer was composed, the question that would have delivered it timed out, and nothing forces a retry. The user is left without the answer and without knowing one was ever prepared.

## Guidance

A user's reply carrying an embedded question (including inside an "Other" free-text answer) is not exhausted by picking an option — the embedded question still needs an answer. And an `AskUserQuestion` timeout after answer content was already written mid-turn is a **non-answer**, not a resolved turn: restate the full answer and re-ask at the very next gate rather than letting the timeout quietly close the loop.

Extracted 2026-09-27 from `CLAUDE.md` § Escalation to the user during instruction grooming (headroom recovery under `claude-md-max-chars`) — the rule sentence stays inline there; this leaf carries the full elaboration.

## See also

- [question-provenance-gate.md](question-provenance-gate.md)
