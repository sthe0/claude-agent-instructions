---
name: hedge-phrase-not-a-question
description: Ending a turn with a vague invitation ("вы решите", "просто скажите", "let me know if...") instead of a posed AskUserQuestion or explicit open question reads as closure but leaves the user nothing to click on.
type: feedback
schema: leaf/v1
created: 2026-09-02
last_verified: 2026-09-02
---

# A hedge phrase is not a question

The short rule lives in CLAUDE.md § Escalation to the user; this leaf carries the full narrative.

## Difficulty

Ending a turn with "вы решите" / "просто скажите" / "let me know if..." / "just say the word" substitutes a vague invitation for the actual defined-set choice the moment calls for. It reads as closure to the model — the turn ends with something that has the shape of a handoff — but it leaves the user nothing to click and no stated recommended option, so they must reopen the conversation to learn what the choice even was. A hedge phrase feels like deference but functions as silence at a decision point: the same "what's next?" gap a posed-but-unclicked prose question leaves, except a bare invitation carries no question shape at all, so it does not read as a question to catch structurally.

Concrete pattern (found 2026-09-02 across 6 sessions' transcripts, user-reported): the user had to explicitly ask "должен ли я что-то сказать" or "что дальше?" after turns that ended this way — the hedge phrase gave them no defined choice to react to, only an open-ended invitation that reads as if the ball is in their court while giving them nothing concrete to act on.

## Guidance

Treat the urge to reach for a hedge phrase as the same signal as the mandatory `AskUserQuestion` rule (CLAUDE.md § Escalation to the user, "Use `AskUserQuestion` for every confirmation and every defined-set choice"): pose the real choice as an `AskUserQuestion` instead, with a recommended option marked. If the moment is genuinely open-ended (no defined set of choices exists), ask the specific open question directly in prose — but still an actual question, not an invitation to volunteer one.

This is currently a **prose-only backstop**, not a structural gate. `hook-turn-end-gate.py` already implements `prose_binary_ask_blockers` (catches a posed-but-unclicked binary question in prose) and `resolution_turn_blockers` (catches silent resolution-adjacent narration), but neither guardian fires when a turn ends with a hedge phrase specifically, nor — more generally — when a turn ends with **no question posed in any form** at a genuine decision point. That broader mechanism gap (covering silent narration at a decision point, not just hedge phrases) is tracked separately as a substantive `hook-turn-end-gate.py` extension (new guardian + semantic judge), routed through `planner` rather than fixed inline here — a regex/keyword match on "вы решите"-style phrases would be exactly the kind of hard-block-on-semantic-classification anti-pattern [[regex-not-for-semantic-classification]] warns against, so it needs a judge, not a denylist.

## See also

- `~/.claude-agent/CLAUDE.md` § Escalation to the user — the short pointer that loads this leaf.
- [[regex-not-for-semantic-classification]] — why the eventual structural fix needs a semantic judge, not a keyword denylist.
- [[doubt-own-snapshot]] — a neighboring paragraph in the same CLAUDE.md section, same "recognize the substitute for the real check" shape.
