---
name: function-place-difficulty
description: A difficulty is a symptom on the `морфология` (form) layer; its cause is a broken `функциональное место` (functional place) in the `организованность` (function layer) that place serves — the two are not 1:1, so reconstruct the functional place before optimizing the literal order.
type: reference
schema: leaf/v1
created: 2026-07-13
last_verified: 2026-07-13
---

# The functional-place layer beneath a difficulty

The short pointer lives in CLAUDE.md's opening root-primitive paragraph; this leaf carries the triad, its SMD pedigree, and a worked example.

## Difficulty

<!-- Language exception: морфология/функциональное место/организованность are the settled SMD source-ontology terms this leaf types; preserved verbatim for traceability. -->
CLAUDE.md's root primitive names a difficulty as "a divergence between a desired and an actual state" — a natural-psychological framing that localizes the problem in the stated wish (the literal order). Taken at face value, this pulls the solver toward patching the морфология (the observable symptom) directly: satisfy the order as given. But an order is itself an object filling a **функциональное место** (functional place) — a position defined by the организованность (organized whole, function layer) it serves, not by its current filler. When that place is emptied or misfilled, the divergence a person reports is the symptom; the actual cause is the broken functional place, and the map between symptom and cause is **not 1:1** — the same symptom can mask different broken places, and the same broken place can surface as different symptoms. Optimizing the symptom without reconstructing the place risks solving the wrong problem well.

## Guidance

Three layers, in SMD terms (Shchedrovitsky's `ММК`; Anisimov's `ММПК`):

<!-- Language exception: ММК/ММПК/морфология/функция/организованность/функциональное место/потребность are the settled SMD source-ontology terms (Shchedrovitsky/Anisimov) this leaf types; preserved verbatim for traceability. -->
- **морфология** (morphology / form) — the observable material: the stated order, the literal wish, the surface divergence.
- **функция** (function) — the организованность (organized whole, cooperative structure) the morphology is a filling for; a **функциональное место** is a position in that organizedness that survives a change of its filler.
- **потребность** (need) — per Anisimov, a need is a functional place that has lost its object-filling; per Shchedrovitsky, a need is constituted by a place in the cooperative structure, not by the subject's psychology.

<!-- Language exception: проблематизация/морфология/функция/потребность/организованность are the settled SMD source-ontology terms this passage types; preserved verbatim for traceability. -->
Before optimizing the literal order, run the beat: **order → проблематизация (problematization) → reconstruction of the functional place → (possibly redefined) task.** Problematization asks what organizedness this order is a filling for, and whether the stated order actually fills the functional place correctly. If it does not, the task to solve is the reconstructed one — which may differ from the literal order — not the literal one taken at face value.

### Worked example

A user asks: "add a retry loop around this flaky API call." Taken as a morphology-level order, the fix is a `for` loop with backoff. Problematizing it: the functional place is "this call must not silently drop data on transient failure" — a place in the larger organizedness of the pipeline's reliability contract. If the actual cause is that the *downstream* consumer has no idempotency key (so a retry would double-write), the literal order (retry loop) fills the wrong place — patching morphology without reconstructing the function reproduces the divergence one layer down. The reconstructed task is "make the call safely retryable," which may mean adding an idempotency key first.

### Relation to R3's провал-нормы / перенормирование cycle

<!-- Language exception: провал нормы/перенормирование/норма/знание are the settled SMD source-ontology terms this passage types; preserved verbatim for traceability. -->

A divergence surfaced by a blocked goal (a норма shown inadequate) is, in this vocabulary, a **провал нормы** — a signal that some grounding знание was false. It is only a signal, one layer up from a functional-place break: the functional place is the standing organizedness a task serves; a провал нормы is what that place's breakage looks like when the broken norm is discovered through a blocked goal rather than through directly missing an object-filling. See [recording-experience.md](recording-experience.md) for the signal(провал)→act(перенормирование) mechanization.

## See also

<!-- Language exception: функция/морфология are the settled SMD source-ontology terms referenced below; preserved verbatim for traceability. -->

- `~/.claude-agent/CLAUDE.md` § the opening root-primitive paragraph — the byte-tight pointer that loads this leaf.
- [[doubt-own-snapshot]] — the snapshot-freshness twin: refresh not only your view of the order but the functional place behind it.
<!-- Language exception: функция/морфология are the settled SMD source-ontology terms referenced below; preserved verbatim for traceability. -->
- [[plan-activity-ontology]] — element 2 (organizedness: form-from-order, content-from-knowledge) is the same функция/морфология distinction applied to a plan's goal.
