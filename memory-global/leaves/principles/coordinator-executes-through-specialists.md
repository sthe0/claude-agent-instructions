---
name: coordinator-executes-through-specialists
description: The root coordinator achieves change by dispatching specialists, not by editing directly — direct Bash/Edit/Write by the root is a difficulty signal, not the default path.
type: reference
schema: principle/v1
generality: 2
domain: coordination
grounded_in: [regularity:root-direct-edits-accumulate-context-and-skip-review]
created: 2026-06-26
last_verified: 2026-06-26
rule_derived: "Seed principle lifted from a coordinator-pitfalls row (ADR-0001 S2.5); no experience leaf recorded."
---

# The coordinator executes through specialists

## Principle

Because root-direct edits accumulate context and skip review ([[root-direct-edits-accumulate-context-and-skip-review]]), for the goal of a lean, controllable and verifiable main thread, achieve production change by **dispatching the work to a specialist** (inline or spawned) rather than editing directly from the root. Root-issued `Bash`/`Edit`/`Write` on substantive production work is a difficulty signal — the exceptions (small change ≤ `small-change-max-lines`; gated repo writes that a spawn would be denied) are named carve-outs, not the default.
## Goal class

A root coordinator thread that stays lean, controllable and verifiable across a long task: low per-turn context cost and every production change reviewed and checkable through a return marker.

## Generality

Level 2 — ranges over the class of coordination tasks: any session where the root is the entry point
and the work exceeds the *small change* bar. It does not claim level 3 because two concrete carve-outs
(small change, gate-denied repo writes applied in-thread) genuinely invert it.

## Grounded in

- [[root-direct-edits-accumulate-context-and-skip-review]] — Production work done directly by the root coordinator accumulates file reads and tool output in the one context every later turn pays for, and passes through no separate actor who could review it.

## Refutation

The principle fails its goal if sessions that dispatch substantive production work end with root contexts no leaner, or with no fewer unreviewed defects, than sessions that edit from the root.

## See also

- `~/.claude-agent/CLAUDE.md` § Coordination — you are the manager.
- [[result-checked-against-its-result-image]] — the verification half of the same loop.
- `docs/adr/0001-consensus-architecture.md`.
