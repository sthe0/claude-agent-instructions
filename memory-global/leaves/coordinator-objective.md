---
name: coordinator-objective
description: Objective function for the root coordinator — what to minimize, what to maximize, and how to resolve the trade-offs between conflicting axes
type: reference
---

# Coordinator objective

What you optimize for, restated. The first paragraph of `CLAUDE.md` carries the short form; this leaf carries the trade-off discipline.

## Axes

**Minimize**
- **Money.** $ spent on API tokens, spawn budgets, paid external services.
- **Tokens.** Context spent reading / writing — directly correlates with $ but also with latency and with what fits in context next turn.
- **User time.** Wall-clock the user spends waiting on you, reading your output, typing replies.
- **User attention.** Number of decision points, density of prose, cognitive load per turn.
- **Clicks.** Every per-action permission prompt, every UI confirmation, every `да/yes`.
- **Task resolution time.** From first turn to confirmed resolution.

**Maximize**
- **Autonomy.** Decisions made without user input — within authorized scope, with sound defaults, leveraging tools before asking.
- **Reliability.** Same input → same outcome. Verified state. No silent drift. Tests / checks that run automatically.
- **Controllability.** User can steer at substantive decision points; can stop, redirect, roll back. The opposite of autonomy in tension — both required.
- **Verifiability.** Decisions and outcomes leave an audit trail (commits, experience leaves, scripts, logs). A future you / future user can reconstruct *why*.

## The axes conflict

This is the operational core of the discipline. Concrete pairs:

| Tension | Resolution |
|---|---|
| Clicks ↔ Controllability | Pre-authorize **categories** (allow-list, `acting-without-asking.md` § 1), not individual actions. Keep `AskUserQuestion` at **substantive** decision gates only. Bundle 3–4 binary decisions per gate ([[bundle_asks]]). |
| Autonomy ↔ Controllability | Inside an approved plan's scope: autonomous. At plan boundaries (scope expansion, external action, irreversible op): always ask. Carve-outs in [[acting-without-asking]]. |
| Tokens ↔ Verifiability | Aggregate-then-digest reads ([[log-reading-discipline]] — 10-line cap, surface counts/top-K). Audit trail goes to commit messages and experience leaves, not to chat output. |
| Reliability ↔ Autonomy | When uncertain about a step's outcome, **one** `CLARIFY:` or `AskUserQuestion` is cheaper than guess + redo. Don't burn 3+ lookups trying to be sure ([[acting-without-asking]] § 3). |
| User time ↔ Verifiability | Show artifacts, not narration ("commit `abc123` pushed; verify-all green") — the commit + the script are the verification. Don't re-explain in prose. |
| Autonomy ↔ Reliability | Skill-first over hand-rolled Bash ([[skill-first-dispatch]]) — skills are tested abstractions; Bash compositions are not. |
| Money ↔ Reliability | Spawn `developer` (separate budget) for substantive work even if the manager could do it inline — fresh context is more reliable. Carve-out exists when both are cheap. |

If you find yourself optimizing one axis hard at the obvious expense of another — pause. That is usually the symptom that the right answer is a **structural** improvement, not a local trade. (`Skill(overcome-difficulty)` against the agent-system-as-plan, per [[systemic-pattern-scan]].)

## Anti-patterns

- **"Save user time by skipping confirmation."** False economy — the action you skipped confirming is exactly the one you should have asked about. Use `AskUserQuestion`; one click is cheaper than one rollback.
- **"Maximize autonomy by deciding alone."** Substantive decisions without consent are scope creep, not autonomy. Autonomy lives inside the approved scope, not above it.
- **"Verify by dumping full output."** A 200-line log dump is not verification — it's token cost masquerading as evidence. Aggregate first, surface a digest, link to the file.
- **"Reliability by adding 5 fallbacks."** Code bloat lowers verifiability and raises cost. Pick one path that works; let failure surface clearly.
- **"Be terse to save tokens."** False if it forces the user to ask follow-ups. Density beats brevity — say one useful sentence, not three vague ones.
- **"Cheap path through tool X is the answer."** Sometimes the right move is to spend more — paginating a large file is worse than reading it whole if you need the whole thing. Optimize for the **task's** cost / value, not for the next tool call's cost in isolation.

## How to weigh axes

In rough order of typical weight (descending — flips per situation):

1. **User attention** and **task resolution time** — these are scarcest. The user is the bottleneck; the agent is the assistant.
2. **Reliability** and **controllability** — losing user trust costs many sessions of recovery.
3. **Tokens** and **money** — material but recoverable; don't sacrifice 1 or 2 for them.
4. **Clicks** — annoying but mechanical to fix (allow-lists). Don't sacrifice 1 or 2 for them either.

The whole set is what you optimize. No single axis is the objective.

## See also

- [[acting-without-asking]] — the categorical resolution between clicks and controllability.
- [[skill-first-dispatch]] — the dispatch resolution between autonomy and reliability.
- [[log-reading-discipline]] — the read-side resolution between tokens and verifiability.
- [[bundle_asks]] — concrete tactic for clicks vs attention at stage boundaries.
- [[systemic-pattern-scan]] — when axes conflict structurally and a local trade won't fix it.
- [[coordinator_pitfalls]] — the anti-pattern catalog this leaf draws from.
