---
name: spine-fallback
description: When the agentctl engine is not started or unavailable, walk the substantive-task spine by hand in the same order — restate, classify+route, plan approval, execute against result images, final verification.
type: reference
schema: principle/v1
converted_from: leaf/v1
grounded_in: [regularity:a-recorded-remedy-is-absent-at-the-act-it-governs, regularity:a-step-that-ran-is-not-thereby-a-step-that-worked, regularity:a-controls-defects-surface-only-when-it-is-executed]
grounding_judged: ae4501e03901a2d6b7e49276b7cfc5b875d172ffaf6a86b52a23ad03d479b297
grounding_override: user-approved 2026-11-03: the judge finds part of the rule ungrounded (the five-step hand-walk and the case where the engine is not driving); the remaining grounds cannot be removed and no non-thin regularity covers the gap under the r11 revision rules, so the defect is recorded rather than papered over; the repair is a per-clause split of the leaf or a new regularity, tracked as a follow-up issue.
created: 2026-07-02
last_verified: 2026-07-02
---

# Coordination-spine fallback (engine not started or unavailable)

The short pointer lives in CLAUDE.md § Coordination spine; this leaf carries the five-step hand-walk verbatim.

## Difficulty

When the `agentctl` engine is not driving the session (not started, or unavailable), the deterministic spine is absent — without an explicit hand-walk the manager skips gates (starts coding before approval, declares done without final verification).

## Principle

To keep the coordination spine's gates from being skipped when the agentctl engine is not driving the session, walk the same five steps by hand in the same order: restate the goal and done criterion with its criterion type, classify weight and route, get plan approval before editing production, execute each stage against its Expected result image, and run the plan's Final verification before declaring done.

## Goal class

coordination-spine gates kept when the engine is unavailable

## Grounded in

- [[a-recorded-remedy-is-absent-at-the-act-it-governs]]

## Guidance

Walk the same steps by hand, same order:

1. **restate** goal + **done criterion**, marking *criterion type*;
2. **classify** weight (CLAUDE.md § Classify task weight) and **route** (planner→approval→developer, or thinker / skill / direct answer) — don't start coding on substantive work except under the in-context carve-out;
3. get **plan approval** before editing production;
4. **execute**, comparing each stage's actual to its `Expected result image:`;
5. run the plan's `## Final verification` against the overall done criterion before declaring done.

## See also

- `~/.claude-agent/CLAUDE.md` § Coordination spine — the engine-driven form this fallback mirrors.
- `scripts/agentctl/README.md` § State machine — the engine that drives the spine when available.
