---
name: 2026-06-25-claude-md-reduction-floor
description: Reducing a bloated always-loaded policy file (CLAUDE.md) toward an aspirational byte target collides with a 'no rule changes meaning' invariant once elaboration is already in leaves; past that floor, further shrink is invariant-extraction (a reliability tradeoff), not dedup.
type: reference
schema: difficulty/v1
resolution_confirmed_by_user: "Решено"
refs: [2026-06-04-non-english-skill-content-language-wall.md]
created: 2026-06-25
last_verified: 2026-06-25
---

# Always-loaded policy shrink hits a dedup floor where size-target meets no-rule-loss

## Difficulty
An always-loaded policy file (CLAUDE.md) is near its byte ceiling. The ask is 'reduce radically (~22KB)', with a co-equal invariant 'no behavioral rule changes meaning'. These conflict at a floor: cheap wins are (a) merging surfaces that describe the same machine 2-3x into one trigger table, and (b) moving per-rule *elaboration* into leaves while the trigger/invariant stays inline. Once both are exhausted, the remaining bytes are irreducible inline invariants; cutting further means moving always-loaded rules into leaves, where they fire less reliably — that is a size-vs-reliability tradeoff, not dedup, and it violates the no-rule-loss invariant. Naive execution gives a false 'target unreachable' or silently guts invariants to hit the number.

## Order & criterion
Distinguish the two regimes before cutting. 1) Dedup regime (free): collapse N overlapping descriptions of one mechanism into one table + pointers; move elaboration (examples, mechanics, implementation trivia) to a leaf with the rule's trigger kept inline. 2) Extraction regime (costs reliability): moving the inline invariant itself to a leaf. Stop at the boundary and surface the floor to the user as an explicit choice (accept current size / light cross-section dedup / extract named invariant blocks with the reliability tradeoff stated) rather than unilaterally gutting or unilaterally stopping short.

**Acceptance check:** measurable: target byte size AND a no-rule-loss invariant; verify both — verify-all 13/13 + byte ceiling for the size axis, and a diff review (every trigger/invariant present inline or in a linked leaf) for the no-loss axis. The invariant wins when they conflict.

## Contexts

### 2026-06-25 — 2026-06-25 CLAUDE.md radical dedup
- Where it arose: global instructions repo, CLAUDE.md radical decomposition (R1-R6)
- Working plan: R1: merge 3 overlapping specialist surfaces (Recognizing-when-to-delegate / Invoking-Spawning-Handling / Available-specializations) into one trigger table + invariant lines + leaf pointers. R2-R6: compress carve-outs; extract 'When to use memory' hygiene to memory-usage.md leaf; tighten Outcome-format and acting-without-asking; light cross-section dedup (difficulty re-definition, Limits recap). Result 39.0->28.8KB (-26%), all rules inline, verify-all 13/13. Surfaced the dedup floor to the user via AskUserQuestion when ~22KB proved to need invariant-extraction; user chose to stop at the floor.

## Cost
in-thread, 3-stage agentctl TOML plan; ~1 session

## Self-critique of the agent system
The 22KB estimate in the original plan was optimistic — it assumed more movable elaboration than remained after Phase 1+R1. Correct response was to surface the floor as a user choice rather than chase the number. No agent-system friction worth a separate self-improvement edit; the mechanism (TOML plan for in-thread multi-stage refactor) worked.
