---
name: plan-activity-ontology
description: The 8-element activity-structure ontology a plan must cover, mapped to concrete agentctl/plan.py + verify-plan-file.py fields; substantive-only requirement; recursive sub-ordering as the genesis of composite (multi-stage) plans.
type: reference
schema: leaf/v1
---

# Plan activity ontology (the 8 elements a plan must cover)

## Difficulty

A plan stated as a free list of steps silently omits the constituents that decide whether the activity can actually be *performed* and *verified*: what is transformed and into what; by which immutable means and method; under which conditions and which properties must stay unchanged; by which actor holding which capability; and on the strength of which refutable principle the transformation was chosen. The two enforcement surfaces — the machine TOML (`agentctl/plan.py`) and the prose verifier (`verify-plan-file.py`) — historically named only a subset and drifted apart, so an author could satisfy the *shape* while leaving the activity under-specified. Functional ground: **a plan is the image of removing a difficulty; an incomplete image is unrealizable or unverifiable.** This leaf fixes the complete element set and where each element lives in the schema, so neither surface can quietly drop one.

## Guidance

### The 8 elements → schema fields

A plan has a `[meta]` head and one or more `[[stage]]` blocks; each stage is a full *elementary plan* carrying every element below.

| # | Element | Schema field |
|---|---------|--------------|
| 1 | **Order** — what must be done | `meta.goal` |
| 2 | **Material + result** — what is transformed, from what initial state, into what | `stage.material` (initial state + relevant properties) + `stage.expected_result_image` (result) |
| 3 | **Control criterion** — how conformance of result to order is checked | `stage.criterion_type` (measurable \| acceptance_review) + `stage.done_criterion` |
| 4 | **Means** — what is used to carry the material from initial state to result; **immutable during the transformation** | `stage.means` |
| 4'| **Method** — how the means is used | `stage.method` |
| 5 | **Conditions + preserved invariants** — under what conditions the transformation runs, and which properties of the material must remain unchanged | `stage.conditions` + `stage.invariants` |
| 6 | **Actor + capability** — who performs the transformation, with which capability to wield the means in the method | `stage.executor` (`in_thread` \| `spawn:<specialization>`) + `stage.capability_required` |
| 7 | **Principle** — the inference behind choosing this material/method: a pattern of which the chosen transformation is an instance, with a known source, a stated confidence, and treated as refutable | `[stage.principle]` = `statement` + `source` + `confidence` (high\|medium\|low) + `refutation` |
| 8 | **Multi-stage as an acyclic graph** — a plan may be a sequence or DAG of elementary plans, each bearing all the attributes above | `[[stage]]` blocks + `stage.depends_on` (edges) + `stage.output_artifacts` |

<!-- Language exception: the user's source ontology is in Russian; the original terms are preserved once for traceability. -->
> Original terms (user's ontology): заказ = order; материал = material; средство = means; способ = method; деятель = actor.

### Weight gating (substantive-only)

The full element set is **mandatory for substantive plans** (`meta.weight_class = "substantive"`) and optional/lighter for `small_change` / `chat`. When `weight_class` is absent, treat the plan **leniently** (new fields optional) so legacy plans keep parsing — strictness applies only where substantive is declared. This mirrors the `schema:leaf/v1` grandfathering: opt-in enforcement, grandfather the rest. See [[leaf-schema]].

### Element 7 — the refutable-principle discipline

Material and method must be chosen by *inference*, not "from the ceiling". State the principle, its **source** (where the regularity comes from), its **confidence**, and what observation would **refute** it. This is the same epistemics already required of rules and memory ("name the difficulty", "cite the source for version-dependent claims", "any principle is potentially refutable") applied to the choice of transformation. It ties to the planner's existing "no numbers/claims without a source" rule.

### Means immutability (element 4)

The means does not change during the transformation it serves. If the means itself must be built, acquired, or modified, that is **not** part of the consuming stage — it is a separate service stage (see recursive sub-ordering below).

### Recursive sub-ordering — the genesis of composite plans

Element 8 is **not an independent eighth thing**; it is the *consequence* of one rule:

> **Any element of an elementary plan — the material in its required initial state, the means, the method, the conditions, even the actor or its capability — may fail to be a given. When it is not a given, that element itself becomes the ORDER (element 1) for a service activity, hence for a service (sub-)plan that is itself a full elementary plan with all 8 attributes. The service plan's result supplies the missing element to the parent stage.**

A composite / multi-stage plan is therefore the **acyclic closure of this recursion**, not a flat list declared up front. In the schema:

- `stage.depends_on` encodes the **producer → consumer** edge (service stage → the stage that consumes its result).
- The service stage's `output_artifacts` / `expected_result_image` **names the element it supplies** (a produced material, a built means, an established condition, a spawned actor).
- "Capability acquirable as a separate service subtask" (element 6) is just **one instance** of this general rule; so is `executor = "spawn:developer"` (establishing the actor), and "material produced by an earlier stage".

When planning: if any element of a stage is not already a given, do **not** hand-wave it inside the stage — split it out as a service stage and add the `depends_on` edge.

### Actor sees the whole plan (element 6)

The actor must have the **whole** plan before it, to be guided by it — not only its own stage. This is why a spawned specialist receives the working plan with its step marked, not just an isolated task.

### What the two surfaces enforce

- **`agentctl/plan.py`** (machine): for a substantive plan, every stage must carry `material`, `means`, `method`, `conditions`, `invariants`, and `[stage.principle]`.{statement, source, confidence, refutation}; otherwise these are optional. Unknown keys are ignored, so adding the fields is backward-compatible.
- **`verify-plan-file.py`** (prose): a substantive prose plan must additionally carry per-stage subsections **Material**, **Means & method**, **Conditions & invariants**, **Principle** (with Source, Confidence, Refutation), on top of the existing Problem-and-done-criteria / Stages / Final verification / Risks. Both surfaces enforce the same contract so an author cannot satisfy one while violating the other.

## See also

- [[leaf-schema]] — the `leaf/v1` shape this leaf itself follows; the opt-in-enforcement / grandfather pattern reused for weight gating.
- [[experience-leaf-schema]] — the `difficulty/v1` schema; the difficulty graph (cycles allowed) that recursive sub-ordering mirrors (order → plan → difficulty → induced order → …).
- [[decomposition-markers]] — M1–M4 decide whether a substantive plan ships as one PR or several; orthogonal to this element-completeness axis.
- [[coordinator-objective]] — the objective function a plan's choices are weighed against.
