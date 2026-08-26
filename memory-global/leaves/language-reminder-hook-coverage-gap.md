---
name: language-reminder-hook-coverage-gap
description: hook-language-reminder.py fires only on UserPromptSubmit — autonomous continuation turns (resuming after tool-only/task-notification turns, no fresh user prompt) get no language nudge and can silently lapse into English
type: feedback
schema: leaf/v1
created: 2026-08-25
last_verified: 2026-08-25
---

## Difficulty

`hook-language-reminder.py` (registered under `UserPromptSubmit` in `~/.claude-agent/settings.json`) reminds the model to reply in the dialogue's detected language, but only fires when a **new user prompt** arrives. A turn generated as a pure continuation — resuming after a batch of tool-result or `<task-notification>` turns with no fresh user text in between — gets no reminder at all, because no `UserPromptSubmit` event fired for it.

Confirmed 2026-08-25 in a project session conducted entirely in Russian: after a long autonomous investigation (tool calls + task-notification turns only, no fresh user prompt in between), the first user-facing reply on resuming was written in English — the rule itself (CLAUDE.md § Instruction language) was correct and known, but the mechanized nudge that operationalizes it never fired for that specific turn shape. The user caught it immediately; the very next turn's reminder fired normally and the lapse self-corrected in one round.

## Guidance

Before writing user-facing text on a turn that opened with tool results / task notifications rather than a fresh user message, explicitly re-check the dialogue's established language yourself — no automated reminder will do it for you on that turn shape. This is a personal-habit mitigation, not a fix: a real fix (e.g. a `Stop`-hook that inspects the model's own about-to-ship text against the session's detected dialogue language) was considered and deliberately **not** built yet — the cost of a new mechanism (design + false-positive risk on code/identifiers/proper nouns embedded in an otherwise-correct-language reply + review) currently outweighs the benefit of a single low-severity, self-correcting lapse. Per the Rule of Three (see `principle-promotion-threshold` in `~/.claude-agent/config.md`), one occurrence does not clear the bar for mechanization — a second independent lapse on this exact turn shape is the trigger to actually build the `Stop`-hook extension.

## See also

- CLAUDE.md § Instruction language — the underlying rule this hook operationalizes.
- `~/claude-agent-instructions/scripts/hook-language-reminder.py` — the existing mechanism to extend, if this recurs.
