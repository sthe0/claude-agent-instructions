---
name: coordinator-executes-through-specialists
description: The root coordinator achieves change by dispatching specialists, not by editing directly — direct Bash/Edit/Write by the root is a difficulty signal, not the default path.
type: reference
schema: principle/v1
generality: 2
induced_from: [coordinator-pitfalls]
---

# The coordinator executes through specialists

## Principle

To keep the main thread lean, controllable, and verifiable, achieve production change by
**dispatching the work to a specialist** (inline or spawned) rather than editing directly from the
root. Root-issued `Bash`/`Edit`/`Write` on substantive production work is a difficulty signal — the
exceptions (small change ≤ `small-change-max-lines`; gated repo writes that a spawn would be denied)
are named carve-outs, not the default.

## Generality

Level 2 — ranges over the class of coordination tasks: any session where the root is the entry point
and the work exceeds the *small change* bar. It does not claim level 3 because two concrete carve-outs
(small change, gate-denied repo writes applied in-thread) genuinely invert it.

## Induced from

- [[coordinator-pitfalls]] — "Root does most edits via Bash/Edit/Write → `Task → developer` for
  code; invoke `overcome-difficulty` when stuck." The recurring symptom this principle generalizes.

## Refutation

If the gate-denied-repo-writes carve-out (a spawned developer cannot write `scripts/**`, so the
manager applies in-thread) turns out to dominate real sessions — i.e. most substantive work is
instruction-repo work that must run in-thread — the principle is too coarse and must split into
"delegate product code" vs "apply instruction-repo changes in-thread with a read-only reviewer."

## See also

- `~/.claude/CLAUDE.md` § Coordination — you are the manager.
- [[result-checked-against-its-result-image]] — the verification half of the same loop.
- `docs/adr/0001-consensus-architecture.md`.
