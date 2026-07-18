---
name: ask-user-question-split-turn
description: The universal turn-split rule for AskUserQuestion — deliver any preceding artifact as the turn's final message, arm a sleep-2 timer in that same turn, open the ask next turn with zero preceding text; hook enforcement.
schema: leaf/v1
type: feedback
created: 2026-07-14
last_verified: 2026-07-17
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

**Two version-sensitivity caveats (measured 2026-07-15), so a denial here is not mistaken for a hook bug:**
- A **Stop-hook block does not open a fresh turn** — it *continues* the current one. So a tool call made before the block is legitimately "this turn", and a subsequent ask is denied **correctly**, not as a false positive. To open a real boundary, the `sleep 2` timer route above is still required.
- The sleep-2 remedy is itself version-sensitive: the background command's own completion can land a `tool_result` between the turn boundary and the ask, re-tripping the mid-turn rule. When a denial is suspected of being a false positive, the entry-shape tail behind it is appended to `~/.local/log/claude-ask-gate-denials.jsonl` (`hook-ask-text-split.py`; path overridable via `CLAUDE_ASK_GATE_DENIAL_LOG`) — read the tail to confirm the true transcript shape rather than assuming the gate misfired.

**Machine enforcement — what each mechanism actually checks (and what it does not):**

- **Turn-boundary check** (`hook-ask-text-split.py`): denies **every mid-turn ask** — any ask in a turn that already completed a tool call — and any turn-opening ask whose substantive same-turn text exceeds the threshold. This is a TURN-BOUNDARY guarantee, nothing more: it proves the ask did not share a turn with a tool call, not that any plan was shown.
- **Delivery check** (`hook-plan-delivery-gate.py` + the `PlanPresentation` receipt): at `PLAN_READY`, the registered essence rendering must have landed as a COMPLETED turn's **FINAL** assistant message — terminal at BLOCK granularity, strictly after the receipt that registered it — bound to the current `plan_sha256`. On positive verification, and **only** there, the hook stamps a `source=hook` delivery receipt; `cmd_approve` requires that stamp. This is a DELIVERY guarantee. *(Before this mechanism existed the leaf claimed a plan "must be delivered as the turn's final message before its approval ask" as though enforced — it was not; the hook guarded only the turn boundary. That overclaim is the proximate cause of the task that added the mechanism: a corrected claim may be stated only now that the check backing it exists.)*
- **NOT machine-checked (perception):** that the rendering faithfully renders the plan, and that the essence is genuinely self-contained. tech-writer authors these; the thinker's plan-review checks them. Describing either as enforced is exactly the defect above — do not repeat it.

**What the two-prover split DOES close** (least obvious, easiest to dismantle by accident): delivery is observable ONLY by the hook — only hooks receive a `transcript_path`; agentctl never sees a transcript. So the hook PRODUCES the proof (a stamp bound to the plan version + rendering) and `cmd_approve` CONSUMES it (`plan_presentation_blockers` requires both a bound receipt and a bound stamp). Therefore `present-plan` → never emit → `approve --by user` now FAILS: no ask fires, so the hook never runs, so nothing stamps, so approve refuses. Two properties hold it up, both easy to erode: (i) **ALLOW != VERIFIED** — the hook stamps only on positive verification, never on a fail-open ALLOW, or it would manufacture the proof it exists to demand; (ii) the only remaining path to approval-without-delivery is the explicit, audit-logged, per-plan-version `confirm-delivery --by --note` escape (it exists because a refusal with no reachable exit trains bypasses — experience leaf `2026-07-09-gate-must-execute-what-it-attests`). An escape used routinely rather than exceptionally is the signal to fix the hook, never to widen the escape.

**Two residuals the mechanism does NOT close — real limits, but not silent ones:**
1. **The essence has no lower bound.** `full` renderings get anchor-completeness checking; `essence` gets none, correctly — a summary is a summary. So registering "план готов" as the essence, emitting it, and asking for approval passes every machine check. The gate proves that AN ACT OF PRESENTATION OCCURRED and those exact bytes landed, bound to this plan version — **not** that anything meaningful was shown. It raises the floor from "nothing" to "whatever you registered"; that is a real gain and is not "the user saw the plan". Adequacy of the essence is perception (tech-writer authors, thinker plan-review checks).
2. **Delivery is not reading.** Terminal position in a completed turn proves the harness rendered the bytes, not that the user read them. That is the ceiling of any mechanical check available here.

- `hook-answer-delivery-reminder.py` nudges when a mid-turn answer plus an ask-timeout would otherwise strand an answer (see [CLAUDE.md](../../CLAUDE.md) § Escalation "An unanswered user question survives the turn").

## See also

- `CLAUDE.md` § Escalation to the user — the condensed kernel of this rule and the `AskUserQuestion`-mandatory mandate.
- [outcome-format.md](outcome-format.md) — the main-first shape of a final-message artifact.
