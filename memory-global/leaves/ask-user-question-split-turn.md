---
name: ask-user-question-split-turn
description: The universal turn-split rule for AskUserQuestion — deliver any preceding artifact as the turn's final message, arm a sleep-2 timer in that same turn, open the ask next turn with zero preceding text; hook enforcement.
schema: leaf/v1
type: feedback
created: 2026-07-14
last_verified: 2026-07-14
---

## Difficulty

Same-message text that precedes a tool call — including an `AskUserQuestion` — may never render **and** is dropped from the transcript. So an ask emitted in a turn that already ran a tool arrives with nothing behind it: the user sees buttons but not the plan / diagnosis / recap they are meant to decide on ("I don't see the plan"). And a prose promise to "ask next turn" that does not actually arm the turn boundary silently strands the ask — no next turn ever fires (observed 2026-07-09).

## Guidance

**The only guaranteed delivery channels are the turn's final message and the ask's own question/option fields.** Everything else in a turn that runs a tool is at risk of being dropped from both render and transcript.

The universal turn-split:

1. Deliver any preceding artifact / recap / decision context as the turn's **final text message** — nothing after it.
2. In that **same** turn, arm a `sleep 2` background timer. **Arming the timer and deferring the ask are one atomic act** — "I'll ask via buttons next message" is never a valid turn-end unless that turn already armed the timer. A prose promise without an armed timer strands the ask because no next turn fires.
3. Open the **next** turn (the timer's completion notification) directly with the `AskUserQuestion` — **zero preceding text**; put any one-line digest inside the question text itself.

This split is **universal**, not only for long artifacts — same-message pre-ask text is dropped from render and transcript regardless of length. The exception shifts *when* the click happens; it never downgrades a defined-set choice to a free-text question.

**Machine enforcement:**
- `hook-ask-text-split.py` denies **every mid-turn ask** — any ask in a turn that already completed a tool call — and any turn-opening ask whose substantive same-turn text exceeds the threshold.
- `hook-plan-delivery-gate.py` additionally guards `PLAN_READY` — a plan must be delivered as the turn's final message before its approval ask.
- `hook-answer-delivery-reminder.py` nudges when a mid-turn answer plus an ask-timeout would otherwise strand an answer (see [CLAUDE.md](../../CLAUDE.md) § Escalation "An unanswered user question survives the turn").

## See also

- `CLAUDE.md` § Escalation to the user — the condensed kernel of this rule and the `AskUserQuestion`-mandatory mandate.
- [outcome-format.md](outcome-format.md) — the main-first shape of a final-message artifact.
