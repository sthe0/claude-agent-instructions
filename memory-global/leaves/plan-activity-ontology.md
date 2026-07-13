---
name: plan-activity-ontology
description: The 8-element activity-structure ontology a plan must cover, mapped to concrete agentctl/plan.py + verify-plan-file.py fields; substantive-only requirement; recursive sub-ordering as the genesis of composite (multi-stage) plans.
type: reference
schema: leaf/v1
created: 2026-06-25
last_verified: 2026-06-25
---

# Plan activity ontology (the 8 elements a plan must cover)

## Difficulty

A plan stated as a free list of steps silently omits the constituents that decide whether the activity can actually be *performed* and *verified*: what is transformed and into what; by which immutable means and method; under which conditions and which properties must stay unchanged; by which actor holding which capability; and on the strength of which refutable principle the transformation was chosen. The two enforcement surfaces — the machine TOML (`agentctl/plan.py`) and the prose verifier (`verify-plan-file.py`) — historically named only a subset and drifted apart, so an author could satisfy the *shape* while leaving the activity under-specified. Functional ground: **a plan is the image of removing a difficulty; an incomplete image is unrealizable or unverifiable.** This leaf fixes the complete element set and where each element lives in the schema, so neither surface can quietly drop one.

## Guidance

### Source of truth: the typed code model, not this prose

The **canonical** definition and enforcer of plan structure is the typed model in **`scripts/agentctl/state.py` + `scripts/agentctl/plan.py`**: the grouped dataclasses (`Subject`, `Means`, `Actor`, `Criterion`, `Principle`, `Supply`, `Outcome`) and the algorithms over them — substantive-field validation, the `Confidence` enum check, and `_validate_graph` (dangling `Supply.on`, unknown substantive `Supply.element`, acyclicity). This leaf and `verify-plan-file.py` are a **secondary human-readable mirror** that must track the code; on any divergence, **the code wins**. The author-written TOML stays in a flat per-stage schema (top-level `executor` / `done_criterion` / `criterion_type` / `depends_on`); `parse_plan` maps those flat keys onto the grouped objects, so the grouping is a property of the in-memory model, not of the file syntax.

### The 8 elements → grouped typed fields

A plan has a `[meta]` head and one or more `[[stage]]` blocks; each stage is a full *elementary plan* carrying every element below. The "Code field" column names the grouped dataclass attribute that actually carries the element (the immutable DECLARATION lives in `subject`/`means`/`actor`/`criterion`/`principle`/`conditions`; the mutable execution RECORD lives in `outcome`).

| # | Element | Code field (canonical) |
|---|---------|--------------|
| 1 | **Order** — what must be done | `meta.goal` |
| 2 | **Material + result** — what is transformed, from what initial state, into what | `stage.subject.material` (initial state + relevant properties) + `stage.subject.result` |
| 3 | **Control criterion** — how conformance of result to order is checked | `stage.criterion.criterion_type` (measurable \| acceptance_review) + `stage.criterion.done_criterion` |
| 4 | **Means** — what is used to carry the material from initial state to result; **immutable during the transformation** | `stage.means.means` |
| 4'| **Method** — how the means is used | `stage.means.method` |
| 5 | **Conditions + preserved invariants** — under what conditions the transformation runs, and which properties of the material must remain unchanged | `stage.conditions` + `stage.subject.invariants` |
| 6 | **Actor + capability** — who performs the transformation, with which capability to wield the means in the method | `stage.actor.executor` (`in_thread` \| `spawn:<specialization>`) + `stage.actor.capability_required` |
| 7 | **Principle** — the inference behind choosing this material/method: a pattern of which the chosen transformation is an instance, with a known source, a stated confidence, treated as refutable, and **typed** by category (знание \| норма) | `stage.principle` = `Principle`(`statement` + `source` + `confidence` (high\|medium\|low, a `Confidence` enum) + `refutation` + optional `statement_kind` (`сущее` \| `должное`, a `StatementKind` enum — grandfathered when absent)) |
| 8 | **Multi-stage as an acyclic graph** — a plan may be a sequence or DAG of elementary plans, each bearing all the attributes above | `[[stage]]` blocks + typed `Supply`(on/element/artifact) in `stage.supplies` (the SOLE edge source); `stage.depends_on` is a derived projection; `stage.outcome` records each stage's result |

<!-- Language exception: the user's source ontology is in Russian; the original terms are preserved once for traceability. -->
> Original terms (user's ontology): заказ = order; материал = material; средство = means; способ = method; деятель = actor.

### Weight gating (substantive-only)

The full element set is **mandatory for substantive plans** (`meta.weight_class = "substantive"`) and optional/lighter for `small_change` / `chat`. When `weight_class` is absent, treat the plan **leniently** (new fields optional) so legacy plans keep parsing — strictness applies only where substantive is declared. This mirrors the `schema:leaf/v1` grandfathering: opt-in enforcement, grandfather the rest. See [[leaf-schema]].

### Element 3 — a machine-enforced instance: review of a developer-actor stage

The control criterion (element #3) is normally a per-plan text the manager checks by hand. One instance is enforced by the engine: when the **actor** (element #6, the `executor`) is `spawn:developer`, the criterion's value is **code review**, and `record-result` refuses to record that stage PASSED without a non-empty general `--control` attestation (`Stage.needs_control()` in `state.py`, the precondition in `cmd_record_result`). Review is not a separate obligation bolted on — it is the value element #3 takes when the result is delegated code, with the reviewer a special case of the controller and the developer (element #6) a special case of the executor. The engine enforces the general structural fact (a developer-produced result needs a control attestation); the content stays free-text cognition. There is deliberately **no** review-specific command — the obligation rides the general `record-result --control`. See `scripts/agentctl/README.md` § *Control attestation*.

### Element 7 — the refutable-principle discipline

Material and method must be chosen by *inference*, not "from the ceiling". State the principle, its **source** (where the regularity comes from), its **confidence**, and what observation would **refute** it. This is the same epistemics already required of rules and memory ("name the difficulty", "cite the source for version-dependent claims", "any principle is potentially refutable") applied to the choice of transformation. It ties to the planner's existing "no numbers/claims without a source" rule.

**The two kinds of principle (`statement_kind`) and the one reflexive refutation axis.** A principle is not one undifferentiated "refutable" thing. There are exactly **two** categories, and `statement_kind` types which one a given principle is:

- **`сущее`** — знание (*is* / descriptive): a claim about how the world is, **refuted by the world** when observation contradicts it. Its `refutation` names the observation that would falsify it.
- **`должное`** — норма (*ought* / prescriptive): a claim about what should be done to reach a goal, never true-or-false but **adequate-or-not** to that goal. It is not refuted by the world; it is shown **inadequate** when a goal it serves is blocked.

<!-- Language exception: сущее/должное/знание/норма/принцип are the settled SMD source-ontology terms this field types; preserved verbatim for traceability (the enum values are literally сущее/должное). -->
There is **one** refutation axis, not two symmetric ones — and it is *reflexively* structured. Only знание is refuted (by the world). A норма shown inadequate is, by the reflexive figure, the discovery that some grounding знание (possibly one reflexive level up) was false: a blocked goal signals that the descriptive premise the norm rested on does not hold. So a `должное` principle's `refutation` describes the goal-blockage that would expose it, and the response is **reflexive reconstruction** — repair the grounding знание, then re-derive (renorm) the норма on the corrected ground. `принцип` in the norm-series (цель→план→программа→метод→подход→принцип) is the *most general* member of that series, **not** a third category beside знание and норма.

Typing this lets the engine demand the *right* refutation for each kind instead of accepting a category error (a norm "refuted by the world", or a knowledge-claim defended by appeal to a goal). The field is **optional and grandfathered**: a plan predating it omits `statement_kind` and still validates; when present, its value must be `сущее` or `должное`.

<!-- Language exception: сущее/должное/знание-о-материале/целеполагание/синолон are the settled SMD source-ontology terms this passage types; preserved verbatim for traceability. -->
**The same `сущее`/`должное` root over the *goal* (`failure_address`).** `statement_kind` applies this root over the **means/principle** (element 7). Its twin over the **goal** lives in the difficulty record. The goal is itself an **organizedness** (a синолон): its **form** is given by the order (`form-from-order`) and its **content** by the знание-о-материале (`content-from-knowledge`) — one form/content unity, superseding the earlier coined goal-homonymy framing. So when a goal-failure closes a difficulty, `Critique.failure_address` ROUTES that failure to the constituent that broke — a content-fault (`сущее`: the `content-from-knowledge`, the model of the material, was wrong — form was right) or a form-fault (`должное`: the `form-from-order`, the целеполагание, was wrong), or explicitly `not_applicable`. This is exactly the reflexive figure above read at closure: a blocked goal is either a false grounding знание (route `сущее`) or an inadequate норма (route `должное`). It **reuses the `StatementKind` values verbatim** (`FAILURE_ADDRESS_VALUES` = the two enum values + the `not_applicable` sentinel) — no second enum is coined — and is enforced by `gates.failure_address_blockers` as a `replan` precondition at DIAGNOSING closure (a bare `None` blocks so the routing is DECIDED; an explicit `not_applicable` is a legal opt-out that clears), mirroring the normalization gate over the renorming act. Optional and grandfathered like `statement_kind`: a critique predating it omits `failure_address` and loads unchanged.

### Means immutability (element 4)

The means does not change during the transformation it serves. If the means itself must be built, acquired, or modified, that is **not** part of the consuming stage — it is a separate service stage (see recursive sub-ordering below).

### Documentation projection is a preserved invariant (element 5)

When an activity transforms an object that has bound documentation, that documentation is a **projection of the object that must stay consistent with it** — i.e. a *preserved invariant* (element 5) of the activity, not an optional follow-up. **Code is a special case of a documented object; a README / concept doc is a special case of documentation.** Changing the object without updating its documentation leaves the invariant violated.

Two currencies, two enforcement grains:

- **Structural currency** — the doc still names code landmarks (sections, symbols) that still exist — is *mechanically* guaranteeable at the **concept grain** (not per-symbol, which rots). The registry [`scripts/doc-bindings.json`](../../scripts/doc-bindings.json) binds each foundational concept to a doc section + a few representative code anchors; [`verify-doc-concepts.py`](../../scripts/verify-doc-concepts.py) (in `verify-all`) asserts the section is present and the anchors resolve.
- **Semantic currency** — the prose *meaning* matches the code *intent* — is **not** mechanically guaranteeable; it is carried by the actor rule (developer SKILL § While developing) and a commit-time soft reminder (`hook-readme-currency-reminder.py`, registry-driven) that names which concept's doc to review when its code changes.

So documentation currency is the same element-5 discipline as any preserved invariant: the control criterion (element 3) at final verification should treat "the changed object's bound documentation is still consistent" as part of conformance, with the registry making *which* documentation explicit.

### Recursive sub-ordering — the genesis of composite plans

Element 8 is **not an independent eighth thing**; it is the *consequence* of one rule:

> **Any element of an elementary plan — the material in its required initial state, the means, the method, the conditions, even the actor or its capability — may fail to be a given. When it is not a given, that element itself becomes the ORDER (element 1) for a service activity, hence for a service (sub-)plan that is itself a full elementary plan with all 8 attributes. The service plan's result supplies the missing element to the parent stage.**

A composite / multi-stage plan is therefore the **acyclic closure of this recursion**, not a flat list declared up front. In the typed model:

- A typed `Supply(on, element, artifact)` in the **consumer's** `stage.supplies` encodes the **producer → consumer** edge: `on` = the producing stage, `element` = which missing element it supplies, `artifact` = the named result. `supplies` is the **SOLE** edge source; `stage.depends_on` is a derived projection (`sorted({s.on for s in supplies})`) — ordering *is* the projection of provision, never a parallel hand-maintained list.
- `Supply.element` / `Supply.artifact` **names the element supplied** (a produced material, a built means, an established condition, a spawned actor). `_validate_graph` rejects a `Supply.on` that points to no stage, a substantive `Supply.element` that is not a known element name, and any cycle in the derived graph.
- "Capability acquirable as a separate service subtask" (element 6) is just **one instance** of this general rule; so is `executor = "spawn:developer"` (establishing the actor), and "material produced by an earlier stage".

When planning: if any element of a stage is not already a given, do **not** hand-wave it inside the stage — split it out as a service stage and add a `Supply` edge naming the element it provides.

### Actor sees the whole plan (element 6)

The actor must have the **whole** plan before it, to be guided by it — not only its own stage. This is why a spawned specialist receives the working plan with its step marked, not just an isolated task.

### What the two surfaces enforce (one canonical, one mirror)

- **`agentctl/plan.py` + `state.py`** (machine, **canonical**): `parse_plan` builds the grouped typed objects from the flat TOML keys and enforces, *in code*, for a substantive plan — every stage carries `subject`(material/result), `means`(means/method), `conditions`, `subject.invariants`, and a `Principle`(statement/source/confidence/refutation, plus an optional `statement_kind`); the `confidence` is a valid `Confidence` enum value; the `statement_kind`, when present, is a valid `StatementKind` enum value (`сущее`\|`должное`) — absent, it grandfathers; and `_validate_graph` holds (no dangling `Supply.on`, no unknown substantive `Supply.element`, no cycle). For non-substantive / weight-absent plans these stay optional. Unknown keys are ignored.
  - **Principle (element 7) anti-template** is also mechanically enforced for substantive plans: `_validate_substantive_stage` rejects placeholder subfield values (`todo`/`tbd`/`n/a`/…), a principle whose `refutation` echoes its `statement`, and a `statement` that echoes the stage's `method` (all after normalization) — so a Principle can't be satisfied by restating the stage. The shared normalization/placeholder-set logic lives in `agentctl/text_shape.py`, reused from `gates.py`'s difficulty-record anti-template.
- **`verify-plan-file.py`** (prose, **mirror**): a substantive prose plan must additionally carry per-stage subsections **Material**, **Means & method**, **Conditions & invariants**, **Principle** (with Source, Confidence, Refutation), on top of the existing Problem-and-done-criteria / Stages / Final verification / Risks. This surface is a human-readable reflection of the code contract — it must track `plan.py`/`state.py`, which win on any divergence.

## See also

- [[leaf-schema]] — the `leaf/v1` shape this leaf itself follows; the opt-in-enforcement / grandfather pattern reused for weight gating.
- [[experience-leaf-schema]] — the `difficulty/v1` schema; the difficulty graph (cycles allowed) that recursive sub-ordering mirrors (order → plan → difficulty → induced order → …).
- [[partition-markers]] — M1–M4 decide whether a substantive plan ships as one PR or several (delivery partition); orthogonal to this element-completeness axis.
- [[coordinator-objective]] — the objective function a plan's choices are weighed against.
