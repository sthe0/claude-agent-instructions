# ADR-0013 — A regularity is knowledge, a principle is a norm grounded in regularities

- **Status:** Accepted (2026-10-27) — implemented in `scripts/lib/principle_provenance.py` (the provenance rules), `scripts/record-experience.py` (`promote`, `promote-scan`, `search --tier regularities`, `link`), `scripts/verify-leaf-structure.py` and `scripts/verify-principle-provenance.py`; the global principle corpus is migrated; the schema is [regularity-leaf-schema.md](../../memory-global/leaves/regularity-leaf-schema.md). Refines [ADR-0001](0001-consensus-architecture.md) (the generality gradient now runs experience → regularity → principle) and keeps [ADR-0004](0004-principle-is-always-a-norm.md) unchanged (a principle is always a norm). Amended 2026-10-27 by § Amendment — the grounding norm, which also amends decision 3 and D5.
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

This amendment refines decisions 1 and 2 and amends decision 3 and driver D5 (point (h)); every other decision above stands. It records what the code stages of the grounding-norm task enforce, so that a later reader does not undo a distinction as duplication. The rules are this system's decisions, taken by its authors on 2026-10-27 and 2026-10-29; the cited sources motivate them and are quoted where their wording matters.

> **Language exception:** the quotations below are verbatim Russian from the cited sources, each followed by an English rendering; the rest of the section is English.

### (a) Prescription and knowledge: the bundle

G.P. Shchedrovitsky, *Philosophy. Science. Methodology*, ch. 5 ([source](https://gtmarket.ru/library/basis/5484/5489)): «между обеими составляющими связки — знаниями и предписаниями — существуют строгие соответствия: знания вырабатываются для подтверждения или опровержения интуитивно созданных предписаний, а предписания опираются на знания, находят в них своё содержание и оправдание» ("between the two components of the bundle, knowledge and prescriptions, there are strict correspondences: knowledge is produced to confirm or refute intuitively created prescriptions, and prescriptions rest on knowledge and find in it their content and justification").

The same author, *Programming of scientific research and development*, part II ch. 3 ([source](https://gtmarket.ru/library/basis/6473/6482)), p. 3.12: «Во всех своих разнообразных формах оно [знание] включается в содержание способа» ("in all its various forms, knowledge enters the content of the way").

Decision: the bundle of prescriptions and knowledge is the unit, and the relation "prescription A rests on knowledge B", the **grounding** relation, admits only knowledge as B. The engine admits only knowledge references in a stage's `[stage.principle].source` and in a principle's `grounded_in`, and it judges the link, not only resolves it. Because the source asks for a strict correspondence, the knowledge listed must be the knowledge the prescription draws its content and justification from: neither a loose neighbour of it nor a partial cover. Decision 1's "Y follows from a named regularity" is read as "Y rests on a named regularity": a prescription is not the logical consequence of a fact, and the "if X then Y" thesis is rejected.

### (b) Norm over norm is a separate relation

A prescription can stand in relation to another prescription, but that relation is not grounding. The bundle text names it: «понадобится ещё одна группа методических положений — как бы второго порядка, в которых будет отражена в качестве особых предписаний к деятельности сама связь этих двух структур» ("one more group of methodical statements will be needed, of a second order as it were, in which the very link of these two structures is reflected as special prescriptions for the activity"). Shchedrovitsky, *The natural and the artificial in semiotic systems*, 1967 ([source](https://gtmarket.ru/library/basis/3961/3965)): «над каждой из них точно так же надстраивается своя иерархическая система норм, обеспечивающих воспроизводство» ("over each of them, in the same way, its own hierarchical system of norms ensuring reproduction is built"); and of the element that resolves a conflict between a norm and changed conditions: «который, изменив норму [А], разрешил бы конфликт. Функции этого элемента … в управлении нормами … это будет (грубо говоря) научная теория особого типа» ("which, having changed the norm [A], would resolve the conflict. The functions of this element … lie in managing norms … this will be, roughly speaking, a scientific theory of a special type").

Decision: this is the **superstructure** relation. A higher norm manages, orders or revises lower norms, as this ADR governs the leaves it describes or an instruction gates a habit. It is not a ground, and a principle never lists another principle in `grounded_in`; the engine refuses a principle reference as a ground and names the knowledge to cite instead. A general prescription formed from several concrete ones does not rest on those concrete principles either. It rests on the experience of applying them, the experience leaves and cases where they were followed and their outcome observed, generalized into a regularity where that experience recurs.

### (c) Revising a prescription

Above, a principle is shown inadequate, not refuted. The 1967 text: «"нормы" сохраняют своё постоянство, несмотря на все изменения условий. В определённые моменты это приводит к конфликту» ("'norms' keep their constancy despite all changes of conditions; at certain moments this leads to a conflict"). The case here is an analogy, not the same event: when a regularity a prescription rests on is refuted, the conditions it was written for turn out to be other than assumed, and the prescription comes into question.

Decision: the revision is closed in three steps. (1) Establish what the knowledge under the prescription now says. (2) Name the defect, of the link or of the ground. (3) Revise or retire the prescription, or confirm it on its remaining grounds. The three steps correspond to the research function, the critical function and the normative function of reflection in O.S. Anisimov, *Reflection. Methodology. Science* ([archived source](https://web.archive.org/web/2023/http://acmegroup.ru/node/289)). That text could not be retrieved when this amendment was written, so the correspondence rests on secondary readings, and the order of the steps is this system's decision, not a claim about the source. A change that jumps to step 3 is rejected.

### (d) Experience as singular knowledge; facts in an order

An experience leaf and a sanitized case are singular knowledge: one incident, one observation. They may stand as grounds next to regularities, but they do not become a regularity by being cited; a regularity generalizes them (decision 1).

An order, the user's request, is a demand and so a norm; it is never generalized into knowledge. Only a factual statement inside an order (what a system is, how long a run takes) may be lifted into a regularity, and it is recorded as a given (point (f)), unverified until a check confirms it, and shown in the plan's essence.

### (e) Ontology as a third ground kind

Some knowledge states a distinction (what counts as a stage, a norm, a difficulty) rather than a recurring pattern. Such leaves become `ontology/v1` leaves, referenced as `ontology:<slug>`; a distinction an ADR states may be cited the same way, the reference resolving to the ADR file. A distinction is not refuted by one observation: it is shown inadequate when it fails to separate the cases at hand. That is the failure mode of a norm, not of a regularity, and the grounding judge treats it so: it asks about the adequacy of the distinction for the case, not whether the distinction is true.

### (f) The given regularity

System knowledge, facts about systems, processes and structure taken from code, documents or measurements rather than induced from the agent's own incidents, converts to a **given** regularity: a `regularity/v1` leaf with `given: code | doc | measurement` and `given_source` in place of `induced_from`. It states a fact taken from a source, not a pattern induced from incidents, and the given provenance is the source that `no_experience_source` requires. A given is presumed possibly stale, wrong or incomplete, and it stays unverified until a check confirms it: its `last_verified` lies within `given-regularity-max-age-days` (`config.md`), or the citing stage checks the claim against code or documents. Otherwise the judge reports an unverified given, a defect of the ground and not of the link.

### (g) leaf/v1 is retired

leaf/v1 is retired as a prescription form. Each existing leaf/v1 leaf is converted in place by a user-approved, judged table: a prescription becomes `principle/v1` grounded in knowledge, knowledge becomes `regularity/v1`, a mixed leaf is split, an ontological leaf becomes `ontology/v1`. Every reference that resolved before still resolves, and the size ceilings hold. A leaf kind that says nothing about its refutation rule is exactly the conflation decision 1 removed.

### (h) The project tier

Decision 3, driver D5 and the deferred alternative above are amended: the project memory gains a regularity area like the global tier. Project principles are grounded in project regularities, referenced as `project:regularity:<slug>`, and project experience is cited as singular knowledge. The global tier still reaches a project leaf only through a sanitized case.

### (i) The grounding judge

Resolving a reference proves that B exists, not that A rests on B. A model grounding judge applies a two-way counterfactual test to each link. First, refuting a listed B puts A in question: it forces a review of A, not necessarily a change, since A may also stand on other grounds, and such overdetermination is legitimate (the consequence above: every principle grounded in a refuted regularity is reviewed). Second, refuting any B that A really depends on must reach A through the list. The judge names a link defect (irrelevant, insufficient, scope_mismatch, contradicting) apart from a defective ground (stale or wrong B, an unverified given, an inadequate distinction, knowledge that prescribes). It fails open, with a recorded reason, as the advisor does; a defect refuses the act unless the link is fixed or an override with a reason is recorded and shown in the essence. A principle whose `grounded_in` names no knowledge reference at all is still refused by rule (`ungrounded_principle`). This is the split of rule from perception: that the reference exists is a rule; whether the ground is relevant is judgment, and a lexical match never stands in for it.
