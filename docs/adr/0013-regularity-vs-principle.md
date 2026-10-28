# ADR-0013 — A regularity is knowledge, a principle is a norm grounded in regularities

- **Status:** Accepted (2026-10-27) — implemented in `scripts/lib/principle_provenance.py` (the provenance rules), `scripts/record-experience.py` (`promote`, `promote-scan`, `search --tier regularities`, `link`), `scripts/verify-leaf-structure.py` and `scripts/verify-principle-provenance.py`; the global principle corpus is migrated; the schema is [regularity-leaf-schema.md](../../memory-global/leaves/regularity-leaf-schema.md). Refines [ADR-0001](0001-consensus-architecture.md) (the generality gradient now runs experience → regularity → principle) and keeps [ADR-0004](0004-principle-is-always-a-norm.md) unchanged (a principle is always a norm).
- **Deciders:** system authors (commit-rights holders to this repo)
- **Difficulty removed:** one leaf kind, `principle/v1`, filled two roles. As a **claim about how things are** it was meant to be refuted by a contradicting observation; as a **norm** ("to achieve X, do Y") it can only be shown adequate or not to a goal. Because both roles shared one leaf and one `induced_from` field, an author could not tell what would refute a given principle, the planner could not tell whether a retrieved leaf was a fact to rely on or a rule to follow, and induction from experience had no honest target: a norm is not induced from observations, only the knowledge a norm rests on is.

## Context

[ADR-0001](0001-consensus-architecture.md) introduced a generality gradient: an experience leaf (`difficulty/v1`) is the generality-0 profile, a `principle/v1` leaf the generality-1-and-above profile of one difficulty-record model. [ADR-0004](0004-principle-is-always-a-norm.md) then fixed that the principle of a plan stage (element 7) is always a norm and never a piece of knowledge.

The leaf corpus did not follow. Its `principle/v1` leaves were written as induced generalizations, each with an `induced_from` list of experience leaves and a `## Refutation` that named a contradicting observation, which is the form of a knowledge claim. The same leaves were retrieved at element 7 as the norm a stage rests on. Reading one leaf as both, the memory conflated two things that differ in kind and in how they fail:

- a **regularity** says how things are: a claim about a recurring pattern, true or false, refuted by one contradicting observation;
- a **principle** says what to do for a goal: a norm, never true or false, shown inadequate when the goal it serves is not reached.

## Decision drivers

- **D1** — Keep knowledge and norm in separate leaves, each with its own refutation rule, so that a reader of a leaf knows what would make it wrong.
- **D2** — Give induction an honest target. Experience is generalized into a regularity; a principle is not induced from experience but derived from a regularity plus a goal.
- **D3** — Keep ADR-0004 intact: a principle is always a norm, so the layer split must not reintroduce an is/ought tag on the principle.
- **D4** — Keep the corpus and its tooling consistent. The provenance chain is checkable by machine at commit time, and a corpus migrated in one change leaves no half-converted state.
- **D5** — Do not break the project tier. A project keeps its principles induced from its own leaves; the global tier is where the split is enforced.

## Decision

### 1. Two layers above the experience leaf

The chain is: experience leaf (`difficulty/v1`, generality 0) → **regularity** (`regularity/v1`, knowledge) → **principle** (`principle/v1`, norm).

- A **regularity** leaf lives in `memory-global/leaves/regularities/`, carries `schema: regularity/v1`, a `generality` level and `induced_from: [<slug>, …]`, the experience leaves (or sanitized cases) it generalizes. A regularity with no experience source carries `rule_derived: <reason>` instead. Its sections are `## Regularity`, `## Generality`, `## Refutation` and `## Induced from`. A single contradicting observation refutes it, or forces a broader form (refutation is generalization, as in ADR-0001).
- A **principle** leaf stays in `memory-global/leaves/principles/` with `schema: principle/v1`. It carries `grounded_in: [regularity:<slug>, …]` in place of `induced_from`, and its sections are `## Principle`, `## Goal class`, `## Generality`, `## Grounded in` and `## Refutation`. The statement keeps the "to achieve X, do Y" form: X is the **goal class** the norm serves, and Y follows from a named regularity. It fails not by a contradicting observation but by not reaching its goal class.

### 2. Rules the tooling enforces

`scripts/lib/principle_provenance.py` classifies the leaf areas and adds these finding kinds:

- `ungrounded_principle` — a global principle without a `grounded_in` naming a regularity;
- `unknown_regularity` — a `grounded_in` item that is not `regularity:<slug>` of an existing regularity;
- `principle_induced_from` — a global principle that carries `induced_from`; it must use `grounded_in`;
- `no_experience_source` — a regularity with neither an experience or case source nor a `rule_derived` reason;
- `index_mismatch` — a regularity or principle index pointer that lists other sources than the leaf's own field.

An experience leaf records the regularities it fed in `generalized_into`, kept two-sided with the regularity's `induced_from` by `record-experience.py link`. `record-experience.py promote` writes a regularity skeleton, `promote --principle --grounded-in REG[,REG]` a principle skeleton grounded in existing regularities, and `promote-scan` flags experience clusters that are candidates for a regularity.

### 3. The project tier is unchanged

A project keeps its principles in its own `principles` directory, induced from its own experience leaves with `induced_from`, as before. The global tier cites a project leaf only through a sanitized case (`case/v1`), which a regularity lists in `induced_from`.

### 4. The corpus is migrated once

The global principle leaves were migrated in one change into approved regularities and principles grounded in them, with every old `induced_from` source preserved on a regularity. No leaf is left in the old shape.

## Consequences

- A retrieved leaf is self-describing: a regularity is knowledge to rely on and to test against new observations; a principle is a norm to follow for a goal class.
- Induction has a single target, the regularity. `promote-scan`, the `curator` split and the recording procedure all lift recurring experience into regularities, and a principle is written afterwards for a goal, grounded in the regularities that justify it.
- The planner still retrieves principles at element 7 (`search --tier principles`); `search --tier regularities` retrieves the knowledge those principles rest on.
- A refutation of a regularity propagates: every principle grounded in it is reviewed, which is what the reflexive reconstruction of ADR-0004 asks for.
- The cost is one more leaf kind and one more index. Every consumer that named `principle/v1` as the induction target had to be reworded.
- ADR-0002's revision rule (a principle's `induced_from` points at a recurring cluster or a refuted predecessor) now applies to regularities; a global principle is revised through the regularities in its `grounded_in`.
- A principle's `generality` field still ranks how widely it applies, but a principle has no `## Induced from` section: its provenance is the regularities in `## Grounded in`.

## Alternatives considered

- **Keep one kind and add a `kind:` tag.** Rejected: it types the distinction on the leaf without separating the refutation rules, and ADR-0004 already ruled out an is/ought tag on the principle.
- **Make the regularity an experience leaf of higher generality.** Rejected: an experience leaf records one incident; a regularity is a cross-incident claim with its own refutation condition and sources.
- **Ground project principles in regularities too.** Deferred: the project tier cannot hold global regularities without leaking identifiers, and the case route already covers the cross-tier link.

## Amendment (2026-10-27) — the grounding norm

This amendment keeps every decision above and refines it from primary sources and from the user's decisions of 2026-10-27 (the dossier decisions D-ADR, D-ONTO, D-PRESC, D-SK, D-GIVEN, D-GJ). It states what the code stages of the grounding-norm task enforce, so that a later reader does not undo a distinction as duplication.

> **Language exception:** the sources below are in Russian. This section cites them by URL and renders their position in English; no Russian text is quoted.

### (a) The bundle and its strict correspondence

G.P. Shchedrovitsky, *Philosophy. Science. Methodology*, ch. 5 ([source](https://gtmarket.ru/library/basis/5484/5489)): methodical statements are prescriptions, not knowledge; yet a prescription functions together with knowledge, and the bundle of prescriptions and knowledge forms one whole, the method. The same author, *Programming of scientific research and development*, part II ch. 3 ([source](https://gtmarket.ru/library/basis/6473/6482)): a methodics is a kind of norm; regularities of nature set the conditions of an activity and limit its way, and knowledge enters the content of the way. The same author, *The natural and the artificial in semiotic systems*, 1967 ([source](https://gtmarket.ru/library/basis/3961/3965)): norms keep their constancy although conditions change, which leads to a conflict that is resolved by changing the norm.

Consequence: a prescription is never grounded in another prescription; it rests on knowledge, and the bundle of prescriptions and knowledge is the unit. The engine therefore admits only knowledge in a stage's `[stage.principle].source`, and the link "prescription A rests on knowledge B" is a statement to be judged, not only resolved: the knowledge listed must correspond strictly to the prescription, neither a loose neighbour of it nor a partial cover. A refuted regularity changes the conditions under which the norm was constant, so the norm is revised (the conflict of the 1967 text).

### (b) Three functions of reflection

O.S. Anisimov, *Reflection. Methodology. Science* ([source](http://acmegroup.ru/node/289)): the research function of reflection serves the passage to the critical function and the normative function. Applied here: induction of a regularity from experience is the research function; testing a link and a ground is the critical function; writing or revising the prescription is the normative function. Refuting a prescription is therefore closed in three steps, in this order: (1) the research step establishes what the knowledge under it now says; (2) the critical step names the defect (the link or the ground); (3) the normative step revises or retires the prescription. A change that jumps to step 3 without 1 and 2 is the "if X then Y" thesis, which is rejected: a prescription is not the logical consequence of a fact.

### (c) Experience as singular knowledge

An experience leaf and a sanitized case are singular knowledge: one incident, one observation. They may stand as the source of a plan stage next to regularities, but they do not become a regularity by being cited; a regularity generalizes them (decision 1). An order quote that no regularity covers may be generalized into an order-derived regularity, shown in the plan's essence for the user to see.

### (d) Ontology as a third ground kind

Knowledge is not only regularities. Some leaves state a distinction (what counts as a stage, a norm, a difficulty) and not a claim about recurrence. These become `ontology/v1` leaves, referenced as `ontology:<slug>` (an ADR counts as one as well). Such a ground is refuted not by a contradicting observation but by the inadequacy of the distinction: the grounding judge asks whether the adequacy of the distinction holds for the case at hand.

### (e) leaf/v1 is retired

leaf/v1 is retired as a prescription form. Each existing leaf/v1 leaf is converted in place by a user-approved, judged table: a prescription becomes `principle/v1` grounded in regularities, knowledge becomes `regularity/v1`, a mixed leaf is split, an ontological leaf becomes `ontology/v1`. Every reference that resolved before still resolves, and the size ceilings hold. The decision is recorded here because a leaf kind that tags nothing about its refutation rule is exactly the conflation decision 1 removed.

### (f) The given regularity

System knowledge (facts about systems, processes and structure that the agent did not observe itself) converts to a given regularity: a `regularity/v1` leaf whose currency is presumed suspect. A given is an unverified given until a check has confirmed it; a plan that rests on one carries that status into the link judgment, and a stale or wrong given is a defect of the ground, not of the link.

### (g) The project tier

Decision 3 and the deferred alternative above are amended: the project memory gains a regularity area like the root. Project principles are grounded in project regularities, referenced as `project:regularity:<slug>`, and project experience is cited as singular knowledge. The global tier still reaches a project leaf only through a sanitized case.

### (h) The grounding judge

Resolving a reference proves that B exists, not that A rests on B. A model grounding judge applies a two-way counterfactual test to each link: refuting each listed B must force revising A, and refuting any B that A really depends on must reach A through the list. It names a link defect (irrelevant, insufficient, scope_mismatch, contradicting) apart from a defective ground (stale or wrong B, an unverified given, an inadequate distinction, knowledge that prescribes). The judge fails open, with a recorded reason, as the advisor does; a defect refuses the act unless the link is fixed or an override with a reason is recorded and shown in the essence. This follows the split of rule from perception: existence of the reference is a rule, relevance of the ground is judgment, and a lexical match never stands in for it.
