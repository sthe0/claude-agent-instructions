---
name: language-reminder-hook-coverage-gap
description: hook-language-reminder.py fires only on UserPromptSubmit — autonomous continuation turns (resuming after tool-only/task-notification turns, no fresh user prompt) get no language nudge and can silently lapse into English
type: feedback
schema: leaf/v1
created: 2026-08-25
last_verified: 2026-08-26
---

## Difficulty

`hook-language-reminder.py` (registered under `UserPromptSubmit` in `~/.claude-agent/settings.json`) reminds the model to reply in the dialogue's detected language, but only fires when a **new user prompt** arrives. A turn generated as a pure continuation — resuming after a batch of tool-result or `<task-notification>` turns with no fresh user text in between — gets no reminder at all, because no `UserPromptSubmit` event fired for it.

Confirmed 2026-08-25 in a project session conducted entirely in Russian: after a long autonomous investigation (tool calls + task-notification turns only, no fresh user prompt in between), the first user-facing reply on resuming was written in English — the rule itself (CLAUDE.md § Instruction language) was correct and known, but the mechanized nudge that operationalizes it never fired for that specific turn shape. The user caught it immediately; the very next turn's reminder fired normally and the lapse self-corrected in one round.

**Second occurrence, 2026-08-26, same project, same turn shape:** a long autonomous git/bash cleanup sequence (no fresh user message in between) ended with a full outcome report composed entirely in English. This is exactly the second independent lapse this leaf's own Guidance named as the trigger to stop deferring and actually build the `Stop`-hook extension.

## Guidance

Before writing user-facing text on a turn that opened with tool results / task notifications rather than a fresh user message, explicitly re-check the dialogue's established language yourself — no automated reminder will do it for you on that turn shape. This remains true as a fallback, but is no longer the only mitigation: the second occurrence above fired the Rule-of-Three trigger this leaf itself set, and a concrete `Stop`-hook extension (a new pure guardian in `hook-turn-end-gate.py`, deterministic script-ratio check, no model call) has been proposed and filed as [claude-agent-instructions#190](https://github.com/sthe0/claude-agent-instructions/issues/190) (`backlog`/`layer:core`/`severity:medium`) — the user chose to park it in the backlog rather than implement immediately. Once #190 lands, this leaf's Guidance should be re-verified against the shipped mechanism and this personal-habit fallback demoted to a backstop.

## See also

- CLAUDE.md § Instruction language — the underlying rule this hook operationalizes.
- `~/claude-agent-instructions/scripts/hook-language-reminder.py` — the existing mechanism, to be extended by #190.
- [claude-agent-instructions#190](https://github.com/sthe0/claude-agent-instructions/issues/190) — the filed `Stop`-hook extension proposal (backlog, not yet implemented).
