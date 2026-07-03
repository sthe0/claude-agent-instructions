---
name: spine-fallback
description: When the agentctl engine is not started or unavailable, walk the substantive-task spine by hand in the same order — restate, classify+route, plan approval, execute against result images, final verification.
type: reference
schema: leaf/v1
created: 2026-07-02
last_verified: 2026-07-02
---

# Coordination-spine fallback (engine not started or unavailable)

The short pointer lives in CLAUDE.md § Coordination spine; this leaf carries the five-step hand-walk verbatim.

## Difficulty

When the `agentctl` engine is not driving the session (not started, or unavailable), the deterministic spine is absent — without an explicit hand-walk the manager skips gates (starts coding before approval, declares done without final verification).

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
