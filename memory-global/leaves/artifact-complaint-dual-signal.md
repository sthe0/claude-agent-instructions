---
name: artifact-complaint-dual-signal
description: A user complaint about a published artifact's readability/format/density ("watery", "unreadable", "too formal") is a dual signal -- fix the artifact AND diagnose why the publish-time gate let it through -- run self-improvement in the same turn, even when the content fix alone looks like a small change.
type: feedback
created: 2026-09-15
last_verified: 2026-09-15
---

A user complaint about a **published** artifact's readability, formality, or
density is not a single task. It is two signals at once: (a) fix the artifact
-- ordinary task work -- and (b) diagnose why the publish-time gate that
exists specifically to prevent this (a tech-writer pass, the
`published-text-writer-gate`) let the bad text through in the first place --
a self-improvement trigger. Run (b) in the same turn as (a), not deferred,
and not skipped because the content fix itself looked small.

**Why:** self-caught 2026-09-15 on a project on this fleet (session
`f65204f0-...`). The user's complaint about two "watery"/formal ticket
comments ("куча воды и каких-то формальных утверждений... не видна суть") was
handled purely as a content-fix task: draft a clearer TL;DR, tech-writer-polish
it, publish, then (on a follow-up message) delete the old comments. No parallel
Beat-1 diagnosis ran on *why* the gate missed the bad text the first time --
until the user asked directly, "why didn't my remarks trigger self-improvement?"
Investigation then found a plausible structural root cause independent of
whether a tech-writer pass ran on those specific bytes: `tracker-management`'s
"Experience record lives in the ticket" convention instructs posting a
structured Difficulty/Order/Context/Working-plan record as the final ticket
comment, but never required that record to *open* with a plain-language lead
stating the practical bottom line -- so even a correctly polished instance of
that template kept its bureaucratic macro-shape, because tech-writer polishes
lexicon inside a structure the skill itself fixes, not the macro-ordering
relative to that structure.

**How to apply:** the moment a message reads as "this published text is
unclear / too formal / full of water", split it explicitly into two branches
before acting: (1) the content fix itself, and (2) `Skill(self-improvement)`
Beat 1 in the same turn -- even when branch (1) alone is a `small-change`.
Task-weight classification and the self-improvement trigger are orthogonal
axes; a small fix does not suppress the diagnostic branch.

## See also
- [published-text-writer-gate.md](published-text-writer-gate.md) -- the gate this complaint means investigating
