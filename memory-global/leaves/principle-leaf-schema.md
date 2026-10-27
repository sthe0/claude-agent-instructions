---
name: principle-leaf-schema
description: The schema for principle leaves (schema:principle/v1) — the norm layer of the memory: a generality-graded "to achieve X, do Y" statement with a goal class, grounded in the regularities (regularity/v1) it rests on via grounded_in, and refuted by not reaching its goal class. The fractal principles/ tier that ADR-0001 § "Principle as a concept with a generality gradient" mandates; the knowledge layer is regularity-leaf-schema.md. Experience leaves (difficulty/v1) sit below both at generality level 0.
type: reference
schema: leaf/v1
created: 2026-06-26
last_verified: 2026-10-27
---

# Principle leaf schema (`principle/v1`)

## Difficulty

The system accumulates `difficulty/v1` experience leaves — each "this worked once, here", at **generality level 0**. Planning with a level-0 record is literal repetition; nothing lifts a recurring resolution into a broader statement that a *different* task can consume. Two kinds of statement are needed above the experience leaf, and they must not share a leaf: a **regularity** (knowledge: how things are, refuted by a contradicting observation — [regularity-leaf-schema.md](regularity-leaf-schema.md)) and a **principle** (a norm: what to do for a goal, shown inadequate when the goal is not reached). A principle is always a norm ([ADR-0004](../../docs/adr/0004-principle-is-always-a-norm.md)), and a norm is not induced from observations; it follows from the knowledge it rests on and a goal. Without that separation a principle cannot say what would refute it, and the planner cannot tell a fact from a rule when it retrieves a leaf to ground a stage ([ADR-0013](../../docs/adr/0013-regularity-vs-principle.md)). ADR-0001 makes **refutation ≡ generalization**: the *difference* found when repetition fails to reproduce a result is what drives a statement up the generality axis. This schema makes the norm layer machine-recordable.

## Guidance

The chain is: experience leaf (`difficulty/v1`, [experience-leaf-schema.md](experience-leaf-schema.md), generality 0) → regularity (`regularity/v1`, the knowledge layer) → **principle** (`principle/v1`, this schema, the norm layer). The three are physically separate (`experience/`, `regularities/`, `principles/`), with their own sub-indexes, and linked by provenance fields. The `leaf/v1` ordinary-leaf shape ([leaf-schema.md](leaf-schema.md)) is **separate and unrelated** — it is not a point on this chain.

A global principle leaf lives under `memory-global/leaves/principles/` and carries `schema: principle/v1` (grandfathered by `verify-leaf-structure.py`, so it is **not** held to the `leaf/v1` section set — it uses the sections below instead).

**Frontmatter** (in addition to `name` / `description` / `type: reference`):

- `schema: principle/v1`
- `generality: <0–3>` — the generality level (see the ladder below).
- `grounded_in: [regularity:<slug>, …]` — the regularities this norm rests on. A principle is never ungrounded: at least one item, each `regularity:<slug>` of an existing regularity. A global principle does **not** carry `induced_from`.
- `domain: <development | planning | coordination | infra>` — **optional**; the practice domain this principle specializes. **Orthogonal to `generality`**: a level-2 development rule and a level-2 coordination rule are both possible. Absent = cross-domain / unscoped, retrievable under any `--domain` filter (absence is the permissive default, not an error). Consumed by `record-experience.py search --domain`.
- `created` / `last_verified` (required) — the temporal fields common to every leaf, defined in [memory-temporal-frontmatter.md](memory-temporal-frontmatter.md). `last_accessed` is retired.

**Domain specialization (orthogonal to generality).** The optional `domain:` tag lets a specialist (e.g. developer) retrieve domain-specialized principles atop general ones via `record-experience.py search --domain <domain>`. Every leaf without a `domain:` key stays fully valid — absence means "matches any domain filter". `verify-leaf-structure.py` adds no required field for it.

**Body sections** (in order):

- `## Principle` — the norm, phrased at its generality level in "to achieve X, do Y" form: Y is an action that follows from a named regularity, X the goal it serves.
- `## Goal class` — the class of goals or tasks X ranges over, i.e. what the norm is *for*. This is what the principle is judged against.
- `## Generality` — the level number + what it ranges over (the set of contexts it claims to cover).
- `## Grounded in` — the regularities, as `[[slug]]` links with a line each on what the regularity contributes (mirrors the `grounded_in` frontmatter so the graph is readable inline). Provenance is by regularity; the experience below it is reached through the regularity's own `induced_from`.
- `## Refutation` — the concrete observation that would show the principle inadequate: following it does not reach the goal class, or a goal it serves is blocked by it. A principle with no such condition is suspect.
  <!-- Language exception: сущее/должное/знание/норма are the settled SMD source-ontology terms; preserved verbatim for traceability. -->
  A principle **is always a норма** (`должное`) — the most general member of the norm-series (цель→план→программа→метод→подход→принцип) — and a norm is never checked for truth, so a principle carries **no** a-priori знание-vs-норма tag (ADR-0004 dropped the `statement_kind` field as a category error). On the **one reflexive refutation axis**: a `должное` shown inadequate (a goal it serves is blocked) is the discovery that a grounding `сущее` (a `знание`) was false, so `## Refutation` names the goal-blockage and the response is **reflexive reconstruction** of that grounding — which is now a named regularity: if it is refuted, repair or replace it, then re-derive the principle on the corrected ground. Whether a *fault* was a content-fault (`сущее`) or a form-fault (`должное`) is decided **post-hoc** at difficulty closure (`критика`), not fixed on the principle in advance.
- `## See also` — sibling principles, the ADR, related leaves.

**The generality ladder** (shared with regularities):

| Level | Ranges over | Example |
|---|---|---|
| **0** | one context — "worked once, here" | a `difficulty/v1` experience leaf as-recorded |
| **1** | a handful of sibling contexts | "this delegation rule holds across the developer-spawn tasks" |
| **2** | a class of tasks | "the coordinator executes through specialists, not directly" |
| **3** | cross-domain invariant | "every produced result is critiqued against its declared result-image" |

**Consumption.** At a plan's `refutable principle` element, the planner retrieves relevant principles to ground each stage (retrieval-augmented planning; `record-experience.py search --tier principles`); `--tier regularities` retrieves the knowledge a principle rests on. **Promotion:** `record-experience.py promote-scan` computes the recurrence signal — it clusters the experience corpus by functional ground, sums the `### ` context blocks across each cluster's leaves (Σ occurrences), and flags every cluster whose total reaches `principle-promotion-threshold` (config key; default 3, the Rule-of-Three) as a candidate for a **regularity**. Induction itself stays a human step: author a `regularity/v1` leaf with `induced_from` pointing back to the member experience leaves. A principle is then written for a goal class with `record-experience.py promote --principle --grounded-in REG[,REG]`, which refuses a regularity that does not exist.

**Generalization check before induction (held-out sanity pass).** Promotion's recurrence signal proves repetition, not generalization — a regularity authored straight from the cluster that produced it can overfit to those incidents' own specifics. Before authoring the leaf, hold out one member context (or, absent a spare member, a dissimilar past task outside the cluster) and check whether the proposed `## Generality` level still holds for it without a special case. If it only holds by narrowing to exactly the contexts that produced it, state that narrower boundary in `## Generality` rather than inducing at the level the recurrence count alone would suggest, and record the outcome of the check as the opening line of `## Refutation`. *Difficulty removed: recurrence-only induction has no counterpart to a held-out/OOD split, so a statement can both over-generalize and ship unrefuted — the gap this adds is a cheap judgment pass, not a benchmark, since no automated evaluator exists for a statement's correctness.*

### Provenance completeness

It is known in advance which experience was generalized into which regularity, and which regularities ground which principle. Every experience leaf carries a required `generalized_into` list (`[]` = not yet generalized) naming regularities; every regularity names its experience sources in `induced_from` or carries an explicit `rule_derived` reason; every global principle names regularities in `grounded_in`. The experience↔regularity link is two-sided and the writing tools keep it so at write time: `record-experience.py promote` writes the regularity and stamps each source leaf, `record-experience.py link` / `unlink` write or remove every side of one pair, and `record-experience.py backfill` stamps a corpus from the regularities' `induced_from`. The `## Induced from` and `## Grounded in` prose is narrative and is not mirrored; only the frontmatter fields are checked, and the index pointers must list the same sources.

`record-experience.py provenance --of <regularity>` lists exactly the experience to consult when that regularity is revised; `record-experience.py provenance --check` lists unaccounted items and exits 1 on any, and `verify-principle-provenance.py` runs the same check in the pre-commit verifier. The finding kinds for the norm layer are `ungrounded_principle` (no `grounded_in`), `unknown_regularity` (an item that is not an existing `regularity:<slug>`) and `principle_induced_from` (a global principle carrying `induced_from`); `no_experience_source` and `index_mismatch` apply to regularities (and `index_mismatch` also to principles).

How experience unrelated to a generalization became possible, each cause in its own words:

- Seed statements were lifted from a coordinator-pitfalls row and from rule leaves, so they had no experience source to name.
- Induction was hand-written and one-way: the leaf listed its sources, nothing wrote the mark back on the experience leaves.
- `promote-scan` only flags a candidate cluster; nothing recorded which leaves a later regularity consumed.
- Leaves were born without a status field, so "not generalized yet" and "never considered" looked the same.
- `induced_from` accepted non-experience sources (rule leaves, notes), which satisfied the field without any experience behind it.

### Sanitized cases (case/v1)

A project experience leaf reaches a global regularity only through a sanitized case, because Core is public and holds no project identifier. Cases live in `principles/cases/` of the global tier and are not indexed in `principles/MEMORY.md`. A case carries frontmatter `schema: case/v1`, `origin_tier: project`, `source_digest` (a `sha256:` digest of the source leaf's body only, frontmatter excluded, so a stamp change does not stale it) and `generalized_into` (the global regularities); its body has four sections, `## Aspect`, `## Expected`, `## Actual` and a closing `## Mismatch`. `record-experience.py case-add --spec FILE` writes the case and links it to its regularities. `FILE` holds a JSON list of objects with the keys `slug`, `description`, `source` (the path of the project experience leaf), `into` (a non-empty list of global regularity slugs), `aspect`, `expected`, `actual` and `mismatch`; every `source` in one file belongs to one project, so the command runs once per project, and an existing case file is refused. The identifier check on the text is a literal substring match, so a paraphrase of a project name is not caught.

`uncited_case` (no regularity lists the case) is reported on the global tier alone. The cross-tier findings need the project tier, so they come from `record-experience.py provenance --project-dir D --check`, not from the global-only pre-commit verifier: `stale_case` (the source leaf's body changed after the case was written), `orphan_case` (no project leaf cites the case), `case_leak` (the case text contains a project identifier).

### Project principle catalog

A project keeps its own principles in `<project memory dir>/principles`, with the `principle/v1` shape and the **earlier provenance form**: `induced_from` and `## Induced from` instead of `grounded_in` and `## Grounded in`, since a project holds no regularity layer. Its leaves link to them two-sided exactly as in the global tier. `record-experience.py promote` and `link` take `--project-dir` to write there. A project leaf never lists a global regularity or principle directly: that is reported as `project_direct_global`, and the route is a case, which Core holds without any project identifier.

## See also

- `docs/adr/0001-consensus-architecture.md` § *Principle as a concept with a generality gradient*.
- [ADR-0013](../../docs/adr/0013-regularity-vs-principle.md) — the split of knowledge (regularity) from norm (principle).
- [regularity-leaf-schema.md](regularity-leaf-schema.md) — the knowledge layer a principle is grounded in.
- [experience-leaf-schema.md](experience-leaf-schema.md) — the `difficulty/v1` generality-0 leaf a regularity is induced from.
- [leaf-schema.md](leaf-schema.md) — the `leaf/v1` shape ordinary reference leaves use (a separate, unrelated shape).
- [principles/MEMORY.md](principles/MEMORY.md) — the sub-index of principle leaves; [regularities/MEMORY.md](regularities/MEMORY.md) — the sub-index of regularity leaves.
