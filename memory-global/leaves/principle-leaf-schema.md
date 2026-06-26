---
name: principle-leaf-schema
description: The schema for principle leaves (schema:principle/v1) — a generality-graded, provenance-rooted, refutable principle induced from one or more difficulties. The fractal principles/ tier that ADR-0001 § "Principle as a concept with a generality gradient" mandates. Experience leaves (difficulty/v1) are principles at generality level 0.
type: reference
schema: leaf/v1
---

# Principle leaf schema (`principle/v1`)

## Difficulty

The system accumulates `difficulty/v1` experience leaves — but each is a principle at **generality
level 0**: "this worked once, here." Planning with a level-0 principle is literal repetition; nothing
lifts a recurring resolution into a broader rule that a *different* task can consume. Without a tier
that records principles **at their generality level**, with a link **down** to the difficulties they
generalize and a **refutation condition** that says when they stop holding, two failures follow:
(1) the planner cannot retrieve a principle to ground a new stage (it only has raw difficulties), and
(2) a principle stated too broadly is never refuted because no one wrote down what would refute it.
ADR-0001 makes **refutation ≡ generalization**: the *difference* found when repetition fails to
reproduce a result is exactly what drives the principle up the generality axis. This schema makes
that machine-recordable.

## Guidance

A principle leaf lives under `memory-global/leaves/principles/` and carries `schema: principle/v1`
(grandfathered by `verify-leaf-structure.py`, so it is **not** held to the `leaf/v1` section set —
it uses the sections below instead).

**Frontmatter** (in addition to `name` / `description` / `type: reference`):

- `schema: principle/v1`
- `generality: <0–3>` — the generality level (see the ladder below).
- `induced_from: [<slug>, …]` — provenance **down** to the difficulty / experience / pitfall leaves
  this principle was induced from. A principle is never rootless; every one cites what it generalizes.

**Body sections** (in order):

- `## Principle` — the statement, phrased at its generality level in "to achieve X, do Y" form.
- `## Generality` — the level number + what it ranges over (the set of contexts it claims to cover).
- `## Induced from` — the provenance, as `[[slug]]` links down to the difficulties it generalizes
  (mirrors the `induced_from` frontmatter so the graph is readable inline).
- `## Refutation` — the concrete observation that would refute the principle **or** drive it to a
  broader form (refutation ≡ generalization). A principle with no refutation condition is suspect.
- `## See also` — sibling principles, the ADR, related leaves.

**The generality ladder:**

| Level | Ranges over | Example |
|---|---|---|
| **0** | one context — "worked once, here" | a `difficulty/v1` experience leaf as-recorded |
| **1** | a handful of sibling contexts | "this delegation rule holds across the developer-spawn tasks" |
| **2** | a class of tasks | "the coordinator executes through specialists, not directly" |
| **3** | cross-domain invariant | "every produced result is critiqued against its declared result-image" |

**Consumption.** At a plan's `refutable principle` element, the planner retrieves relevant principles
to ground each stage (retrieval-augmented planning). Promotion: when a difficulty recurs across
contexts (an experience leaf accumulates contexts), its commonality is lifted into a principle leaf
here at the appropriate level, with `induced_from` pointing back down.

## See also

- `docs/adr/0001-consensus-architecture.md` § *Principle as a concept with a generality gradient*.
- [experience-leaf-schema.md](experience-leaf-schema.md) — `difficulty/v1`; principles at level 0.
- [leaf-schema.md](leaf-schema.md) — the `leaf/v1` shape ordinary reference leaves use.
- [principles/MEMORY.md](principles/MEMORY.md) — the sub-index of principle leaves.
