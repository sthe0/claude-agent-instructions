---
name: verify-right-axis-report-honestly
description: Static checks (imports/tests/build-diff) never substitute for runtime correctness, durable-store coverage, universal-quantification, or provenance/conformance axes — each is its own axis and must be checked on its own terms.
type: reference
schema: principle/v1
converted_from: leaf/v1
grounded_in: [regularity:a-probe-observes-only-the-domain-it-reads, regularity:a-step-that-ran-is-not-thereby-a-step-that-worked, regularity:a-change-outside-the-state-its-consumers-read-is-unseen-by-them, regularity:a-fix-at-the-failing-site-leaves-siblings-of-the-same-form]
grounding_judged: a9a402d134821b0b3a715261a0e5859284ac04d51fd5c4731771add7d7566b44
grounding_override: The leaf bundles several clauses resting on different regularities; across two judge rounds the judge alternately names a missing ground and then an irrelevant one among the added grounds, so no single grounded_in satisfies both; the repair is a per-clause split of the leaf, a content change outside this in-place conversion.
created: 2026-09-15
last_verified: 2026-10-26
---

## Difficulty

Reporting "imports pass / tests green / build-diff identical" reads as "it works", but each of those is a *static* check. A report that stops there silently substitutes a cheap axis for the one that actually matters, and the gap only surfaces later, after the artifact is already relied on.

## Principle

To keep a success report from resting on a cheap axis in place of the one that matters, treat runtime correctness, partial progress, published-versus-posted, durable-store coverage, universally-quantified criteria (mechanical enumeration plus a negative end-state check), provenance for reasoning deliverables, the outcome rather than a proxy for an improvement trial, and conformance to the instructions and plan as separate verification axes, and report success on an axis only after checking that axis on its own terms.

## Goal class

success reports resting on the axis that matters

## Grounded in

- [[a-probe-observes-only-the-domain-it-reads]]
- [[a-step-that-ran-is-not-thereby-a-step-that-worked]]
- [[a-change-outside-the-state-its-consumers-read-is-unseen-by-them]]
- [[a-fix-at-the-failing-site-leaves-siblings-of-the-same-form]]

## Guidance

Treat each of the following as its own verification axis. Passing one does not license reporting success on another:

- **Runtime vs static.** Static verification (imports resolve, tests green, build-diff identical) does not establish *runtime* correctness for code loaded by name from an external artifact — a baked image, a porto/job layer, a serialized graph. Don't report "works / didn't break" until the runtime axis is actually checked for the affected code path.
- **Partial progress is not success.** Never infer success from partial progress — a job that got past block N says nothing about block N+1.
- **"Posted" ≠ "published".** After any outward action (a PR comment, a publish, a push), confirm it actually landed. The action returning without error is not the same as the artifact being live where its consumer will find it.
- **Durable-store coverage is a done-axis.** When capturing worked-out artifacts into a durable store (tracker, backlog, doc), report captured-vs-the-full-set and flag any local-only residue at capture time. A partial capture left to read as complete is the durable-store twin of "posted" ≠ "published" — and the residue's durability then rests on an undisclosed local machine.
- **Universally-quantified criteria need mechanical enumeration.** A done criterion like "all X", "no Y remains" is never established by checking the instances you happened to touch — it needs a mechanical enumeration of the domain plus a negative end-state check of the property itself. See planner `SKILL.md` § Understand the problem first.
- **Provenance for reasoning/research deliverables.** For a reasoning/research deliverable, the right axis is *provenance*, not tests-green: arm `classify --deliverable-kind reasoning` so the `ledger` plugin gates resolution on claim-provenance closure. Ladder + the L3-refusal criterion: [formalization-ladder-l1-l3.md](formalization-ladder-l1-l3.md).
- **Outcome, not proxy, for an improvement trial.** A trial of an agent-system change measures the outcome the order names — the task resolved, and total wall-clock, tokens and user attention to resolution — not the cost of one phase (e.g. planning up to an admissible plan). If the subject cannot be executed safely (a prod order), pick one that can (a past task with tests, replayed in isolation); otherwise the plan's essence states plainly that the outcome axis is unmeasured and the result cannot license landing. *(2026-10-26: a two-arm trial measured planning cost only; the user expected end-to-end resolution cost and the result could not answer "should we land this".)*
- **Conformance is a verification axis too.** Verify your ACTUAL behavior followed the instructions and the plan's expected actions — including an implicit precondition of one — at each step boundary AND at resolution. A divergence is a difficulty to register immediately, not only at delivery. Full rule + the mechanized obligations-ledger slice: [behavioral-conformance-control.md](behavioral-conformance-control.md).

This leaf is the elaboration of a `CLAUDE.md` bullet, extracted 2026-09-15 to keep `CLAUDE.md` under `claude-md-max-chars` while landing the [subagent-destructive-guard](experience/2026-09-15-subagent-self-authorized-destructive-action.md) plan's rule (B).

## See also

- [formalization-ladder-l1-l3.md](formalization-ladder-l1-l3.md)
- [behavioral-conformance-control.md](behavioral-conformance-control.md)
- regularity:a-probe-observes-only-the-domain-it-reads — the regularity this rule rests on.
