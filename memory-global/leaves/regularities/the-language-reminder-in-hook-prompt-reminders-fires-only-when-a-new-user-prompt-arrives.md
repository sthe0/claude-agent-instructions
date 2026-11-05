---
name: the-language-reminder-in-hook-prompt-reminders-fires-only-when-a-new-user-prompt-arrives
description: The language check in hook-prompt-reminders.py fires only on a new user prompt, so pure continuation turns get no language reminder
type: reference
schema: regularity/v1
generality: 1
induced_from: [language-reminder-hook-coverage-gap]
rule_derived: "lifted from language-reminder-hook-coverage-gap.md; no experience leaf backs it (the incidents are recorded in the leaf itself)"
introduced_by: grounding-norm
created: 2026-10-30
last_verified: 2026-10-30
---

# the-language-reminder-in-hook-prompt-reminders-fires-only-when-a-new-user-prompt-arrives

## Regularity

The language check in scripts/hook-prompt-reminders.py (check_language) is registered under UserPromptSubmit, so it fires only when a new user prompt arrives. A turn generated as a pure continuation, resuming after tool-result or task-notification turns with no fresh user text, gets no reminder, and its user-facing text can lapse into English in a session conducted in another language. This was observed on 2026-08-25, 2026-08-26 and 2026-09-14 in Russian-language sessions; the next turn with a real user prompt got its reminder and self-corrected. A Stop-hook extension (a deterministic script-ratio guardian in hook-turn-end-gate.py) was filed as claude-agent-instructions#190 and was not implemented as of the leaf's last verification.

## Generality

Level 1 — ranges over agent turns in a session conducted in a non-English language that resume after tool-result or task-notification turns without fresh user text. It is deliberately not claimed for turns that open with a user prompt, which do receive the reminder.

## Induced from

- [[language-reminder-hook-coverage-gap]] — contributes the three dated lapses into English on continuation turns in Russian-language sessions, each self-corrected on the next real user prompt.

## Refutation

A continuation turn with no fresh user prompt is observed to receive the language reminder from hook-prompt-reminders.py, or scripts/hook-prompt-reminders.py is found registered under an event that also fires on such turns.
