---
name: verify-right-axis-report-honestly
description: Static checks (imports/tests/build-diff) never substitute for runtime correctness, durable-store coverage, universal-quantification, or provenance/conformance axes — each is its own axis and must be checked on its own terms.
type: reference
schema: leaf/v1
created: 2026-09-15
last_verified: 2026-09-15
---

## Difficulty

Reporting "imports pass / tests green / build-diff identical" reads as "it works", but each of those is a *static* check. A report that stops there silently substitutes a cheap axis for the one that actually matters, and the gap only surfaces later, after the artifact is already relied on.

## Guidance

Treat each of the following as its own verification axis. Passing one does not license reporting success on another:

- **Runtime vs static.** Static verification (imports resolve, tests green, build-diff identical) does not establish *runtime* correctness for code loaded by name from an external artifact — a baked image, a porto/job layer, a serialized graph. Don't report "works / didn't break" until the runtime axis is actually checked for the affected code path.
- **Partial progress is not success.** Never infer success from partial progress — a job that got past block N says nothing about block N+1.
- **"Posted" ≠ "published".** After any outward action (a PR comment, a publish, a push), confirm it actually landed. The action returning without error is not the same as the artifact being live where its consumer will find it.
- **Durable-store coverage is a done-axis.** When capturing worked-out artifacts into a durable store (tracker, backlog, doc), report captured-vs-the-full-set and flag any local-only residue at capture time. A partial capture left to read as complete is the durable-store twin of "posted" ≠ "published" — and the residue's durability then rests on an undisclosed local machine.
- **Universally-quantified criteria need mechanical enumeration.** A done criterion like "all X", "no Y remains" is never established by checking the instances you happened to touch — it needs a mechanical enumeration of the domain plus a negative end-state check of the property itself. See planner `SKILL.md` § Understand the problem first.
- **Provenance for reasoning/research deliverables.** For a reasoning/research deliverable, the right axis is *provenance*, not tests-green: arm `classify --deliverable-kind reasoning` so the `ledger` plugin gates resolution on claim-provenance closure. Ladder + the L3-refusal criterion: [formalization-ladder-l1-l3.md](formalization-ladder-l1-l3.md).
- **Conformance is a verification axis too.** Verify your ACTUAL behavior followed the instructions and the plan's expected actions — including an implicit precondition of one — at each step boundary AND at resolution. A divergence is a difficulty to register immediately, not only at delivery. Full rule + the mechanized obligations-ledger slice: [behavioral-conformance-control.md](behavioral-conformance-control.md).

This leaf is the elaboration of a `CLAUDE.md` bullet, extracted 2026-09-15 to keep `CLAUDE.md` under `claude-md-max-chars` while landing the [subagent-destructive-guard](experience/2026-09-15-subagent-self-authorized-destructive-action.md) plan's rule (B).

## See also

- [formalization-ladder-l1-l3.md](formalization-ladder-l1-l3.md)
- [behavioral-conformance-control.md](behavioral-conformance-control.md)
