---
name: regularity-leaf-schema
description: The schema for regularity leaves (schema:regularity/v1) — the knowledge layer of the memory: a refutable claim about how things are, induced from experience leaves and refuted by a contradicting observation. A principle (principle/v1) is the norm layer and cites the regularities it rests on in grounded_in.
type: reference
schema: leaf/v1
created: 2026-10-27
last_verified: 2026-10-27
---

# Regularity leaf schema (`regularity/v1`)

## Difficulty

One leaf kind used to serve two roles: a claim about how things are, and a norm for what to do. A claim is refuted by a contradicting observation; a norm (a principle, always a norm per [ADR-0004](../../docs/adr/0004-principle-is-always-a-norm.md)) is shown inadequate when the goal it serves is not reached. With both in one leaf, a reader could not tell what would make it wrong, the planner could not tell a fact from a rule, and induction from experience had no honest target — a norm is not induced from observations, only the knowledge it rests on is. The decision and its alternatives are in [ADR-0013](../../docs/adr/0013-regularity-vs-principle.md). This schema fixes the knowledge layer.

## Guidance

The chain is: experience leaf (`difficulty/v1`, generality 0) → **regularity** (`regularity/v1`, this schema) → **principle** ([principle-leaf-schema.md](principle-leaf-schema.md)). Induction lands in a regularity; a principle is then written for a goal class and cites the regularities that justify it in `grounded_in`.

A regularity lives under `memory-global/leaves/regularities/` and carries `schema: regularity/v1` (checked by `verify-leaf-structure.py`: the four sections below must be present and non-empty; it is **not** held to the `leaf/v1` section set). Index it in [regularities/MEMORY.md](regularities/MEMORY.md) with one pointer line; the sources after the `←` arrow must be the leaf's own `induced_from` (`index_mismatch` otherwise).

**Frontmatter** (in addition to `name` / `description` / `type: reference`):

- `schema: regularity/v1`
- `generality: <1–3>` — how widely the claim ranges (the ladder is in [principle-leaf-schema.md](principle-leaf-schema.md)).
- `induced_from: [<slug>, …]` — provenance **down** to the `difficulty/v1` experience leaves, or the sanitized cases (`case/v1`), this regularity generalizes. The link is two-sided: each experience source lists the regularity in its `generalized_into`, and `record-experience.py link` / `unlink` keep both sides.
- `rule_derived: <reason>` — instead of an experience source, for a regularity written from rules that no experience yet backs. A regularity needs one or the other: neither is `no_experience_source`; both is a stale mark.
- `domain:` — **optional**, the practice domain, orthogonal to `generality` (consumed by `record-experience.py search --domain`).
- `created` / `last_verified` (required) — see [memory-temporal-frontmatter.md](memory-temporal-frontmatter.md).

**Body sections** (in order):

- `## Regularity` — the statement of how things are: a claim about a recurring pattern, true or false. It is not an instruction. "To achieve X, do Y" belongs to a principle, where X is its goal class.
- `## Generality` — the level and the class of tasks the claim ranges over, including what it is deliberately not claimed for.
- `## Refutation` — the concrete observation that would refute the claim, or force a broader form (refutation is generalization, as in [ADR-0001](../../docs/adr/0001-consensus-architecture.md)). A regularity with no refuting observation is suspect.
- `## Induced from` — the sources as `[[slug]]` links with a line each on what the source contributes (mirrors `induced_from`; only the frontmatter is checked).

**Regularity versus principle.** A regularity is refuted by a contradicting observation. A principle fails differently: by not reaching its goal class. When a regularity is refuted, review every principle whose `grounded_in` names it — the norm lost its ground and is re-derived on the corrected knowledge.

**Writing one.** `record-experience.py promote --induced-from SLUG [--induced-from SLUG …]` writes a regularity skeleton and stamps each source (`--rule-derived REASON` instead of an experience source). `record-experience.py promote-scan` flags experience clusters that reach `principle-promotion-threshold` as candidates for a regularity. `record-experience.py search --tier regularities "<keywords>"` retrieves them. A principle is written afterwards with `promote --principle --grounded-in REG[,REG]`.

**Held-out check before induction.** The recurrence count proves repetition, not generalization: a regularity authored from the cluster that produced it can overfit to those incidents. Before writing it, hold out one member context and check that the `## Generality` level still holds for it without a special case; if it holds only by narrowing, state the narrower class and record the outcome of the check as the opening line of `## Refutation`.

**Project tier.** A project keeps its principles induced from its own leaves with `induced_from`; it holds no regularity layer. A project leaf reaches a global regularity only through a sanitized case, which the regularity lists in `induced_from`.

## See also

- [ADR-0013](../../docs/adr/0013-regularity-vs-principle.md) — the decision to separate knowledge from norm.
- [principle-leaf-schema.md](principle-leaf-schema.md) — the norm layer: `principle/v1`, `grounded_in`, the goal class, provenance completeness and cases.
- [experience-leaf-schema.md](experience-leaf-schema.md) — the `difficulty/v1` leaf a regularity is induced from.
- [regularities/MEMORY.md](regularities/MEMORY.md) — the sub-index of regularity leaves.
- [leaf-schema.md](leaf-schema.md) — the `leaf/v1` shape this schema leaf itself uses.
