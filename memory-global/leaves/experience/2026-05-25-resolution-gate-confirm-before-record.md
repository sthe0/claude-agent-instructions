---
name: 2026-05-25-resolution-gate-confirm-before-record
description: Recurring difficulty — at task resolution the agent treats a pushed diff / "thanks" as session-end and skips the confirm→record gate (records on assumption, never writes the leaf, or accepts verbal assent as empirical success). Fix is a mechanically-nudged gate shaped to the criterion type.
type: reference
schema: difficulty/v1
resolution_confirmed_by_user: "<migrated 2026-06-11 to difficulty/v1; per-context confirmations preserved inline>"
refs: [2026-05-26-agent-system-plan-vs-reality-drift]
---

# Resolution gate: confirm before recording

## Difficulty
When the work is done and the diff pushed, the agent treats the user's "thanks" (or its own "пауза") as session-end instead of as the cue to run the resolution gate: ask "resolved?" → on explicit confirmation write the experience leaf → trigger self-improvement. The failure has three surfaces: the leaf is never written; the leaf is written on assumed resolution; or verbal assent is accepted as evidence of *empirical* success when the user has not actually observed the fix work.

## Order & criterion
An explicit, mechanically-nudged resolution gate. Ask via `AskUserQuestion` **shaped to the criterion type** — *measurable*: a generic "решено?"; *acceptance-review*: ask the user to confirm a **specific observation they have just performed** ("ran X, saw Y?"), not a belief on the strength of the explanation. Record only after explicit confirmation. **Acceptance check:** every experience leaf carries a non-empty `resolution_confirmed_by_user` quote (hard-blocked by `verify-experience-leaf.py`), and a hook nudges when the user's reply is brief gratitude.

## Contexts

### 2026-05-25 — leaf almost never written (first recorded instance)
Confirmed: retroactive (rule introduced 2026-05-26). Substantive task resolved; user said "let's pause"; agent proposed pause too and nearly skipped the leaf. Origin of the `missed-leaf-at-resolution` pattern. Fix shipped soon after: the `resolution_confirmed_by_user` frontmatter sentinel + `verify-experience-leaf.py` (PreToolUse `Write` hook + `verify-all`).

### 2026-05-26 — third instance; prose rule keeps being skipped → needs code
Confirmed: "Да, резолвнута". After a push the agent said "На здоровье! 🙂" and stopped; user had to prompt "Почему ты не спрашиваешь решена ли задача?". Recognized as the 3rd instance (links to 2026-05-25 and a cron-tz project leaf). Key insight: `hook-self-critique-reminder.py` only fires *after* a leaf is written, so it cannot catch the leaf-never-written case — the gate itself needed a UserPromptSubmit nudge, not more prose.

### 2026-05-29 — assent without observation + a hook word-cap miss
Confirmed: "Да, решена — после второго захода на закрытие". Two distinct failures in one task: (a) wrote a final summary without putting the `AskUserQuestion` in the *same* reply; the 7-word reminder slipped past `hook-resolution-reminder.py`'s ≤6-word cap → added an in-thread carve-out (the summary reply *is* the gate) + a meta-question predicate (gratitude + meta-keyword in ≤20 words). (b) Asked "resolved?", user said "yes" on the strength of the explanation without having retried mosh; the next turn revealed a real Touch-ID hang → for acceptance-review, require a *specific observed* outcome, not assent. Commit `dbdf071`.

## Common core & variations
**Common:** make the gate mechanical (UserPromptSubmit nudge on brief gratitude + the required-frontmatter hard block) and shape the ask to the criterion type. The underlying shape is the same as [[2026-05-26-agent-system-plan-vs-reality-drift]]: a prose rule that keeps being skipped must become a firing mechanism.

**Variations:**
- *Leaf-never-written* (2026-05-25/26) — caught by a UserPromptSubmit nudge, since a post-write hook is structurally too late.
- *Assent ≠ observation* (2026-05-29) — for acceptance-review criteria the ask must name the empirical check the user performed; "yes" to "did the explanation sound right?" lets a regression through.
- *Hook coverage gaps* — the word-cap (≤6 → meta-keyword in ≤20) shows nudge heuristics start permissive and tighten when a real miss surfaces.

## Cost
- All occurrences in-thread (small-change carve-out), $0 spawn cost.
- 2026-05-29 was the most expensive (~3 h, ~12 turns) precisely *because* of a premature "resolved" gate — the post-confirmation hang report cost an extra full iteration plus a self-improvement turn. The cost is the direct argument for requiring an observed check before closing acceptance-review tasks.
