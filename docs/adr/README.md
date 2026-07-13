# Architecture Decision Records

Significant, hard-to-reverse design decisions for the agent system. An ADR is immutable once **Accepted**; a later decision that changes it is a **new** ADR that supersedes the old one (never an in-place rewrite).

| ADR | Title | Status |
|---|---|---|
| [0001](0001-consensus-architecture.md) | Consensus architecture for a distributed agent system | Accepted (2026-06-26) — implemented in slices S1–S4 |
| [0002](0002-dialectical-transition.md) | Dialectical transition: the σ operator and principle revision | Accepted (2026-06-29) — σ-sentinel slice implemented; σ deferred behind the build-trigger |
| [0003](0003-statement-kind-typed-principle.md) | Typed principle: `statement_kind` (`знание`/`сущее` vs `норма`/`должное`) | Accepted (2026-07-13) — implemented across plan.py/state.py/verify-plan-file.py + docs |

The operational distillations of an ADR (the contracts a tool or developer applies) live alongside it under [docs/](../): e.g. [instruction-layering.md](../architecture/instruction-layering.md), [layer-maintenance.md](../operations/layer-maintenance.md), [personal-layer.md](../architecture/personal-layer.md), [core-difficulty-calibration.md](../architecture/core-difficulty-calibration.md) all distil ADR-0001; [sigma-build-trigger.md](../sigma-build-trigger.md) distils ADR-0002.
