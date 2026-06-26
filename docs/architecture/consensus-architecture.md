# The consensus architecture

> The canonical narrative of how one evolving Core is distributed across many developers — the Core < Team < Personal precedence ladder, and how a non-author's improvement reaches the shared Core without anyone editing Core directly.

The repository is consumed by several developers at once. One shared **Core** evolves while each developer keeps personal and project-scoped overrides on top. The reconciliation contract is **override + rebase**, not a blind merge: the layers compose by a higher layer replacing or adding to the lower one, and a layer is kept current against the moving Core by replaying its overrides (a rebase), never by interleaving histories (a merge).

## The precedence ladder

One fixed precedence list, lowest → highest:

```text
Core < Team < Personal
```

- **Core** — the shared, protected instructions ([CLAUDE.md](../../CLAUDE.md), [config.md](../../config.md), `skills/**`, `agents/**`, `memory-global/**`, the `agentctl` engine). Edited only by commit-authorized authors (`CODEOWNERS`); an uncontrolled edit breaks everyone.
- **Team** — project-scoped overrides shared via a project's own git (`<project>/.claude/agent-memory/**`, `<project>/.claude/rules/*.mdc`, `<project>/.claude/skills/**`).
- **Personal** — a single developer's machine-local overrides.

When two layers speak to the same point, the higher (nearer) layer wins. A higher layer may add to Core and may locally override it, but may **not** edit the Core artifact in place. The exact replace-vs-merge rule per artifact class, the tiebreak, and the ordered-list insertion semantics are the **applicable contract**, described in [instruction-layering.md](instruction-layering.md); the rebase / `rerere` recipe for keeping a layer current lives in [layer-maintenance.md](../operations/layer-maintenance.md); the highest-precedence layer's scope is in [personal-layer.md](personal-layer.md).

## How a non-author's improvement reaches the Core

The hard constraint is that **no one edits the protected Core directly**. A recurring difficulty a developer hits is promoted to the shared Core through a four-step channel:

1. **Report, don't edit.** A recurring difficulty is reported to a **difficulty-accumulation channel** (`scripts/difficulty_channel/` — a port with pluggable adapters) rather than as an ad-hoc Core edit.
2. **Cluster and threshold.** [scripts/core-difficulty-digest.py](../../scripts/core-difficulty-digest.py) clusters the reports by root cause. A cluster whose **mass** — the sum of a geometric severity-weight ladder (low 1 / medium 2 / high 4 / critical 8) over its members — reaches `core-difficulty-mass-threshold` (see [config.md](../../config.md) and the [calibration note](core-difficulty-calibration.md)) is flagged for a batched Core change. A single critical report short-circuits the threshold.
3. **Propose, never execute.** [scripts/consensus-synthesizer.py](../../scripts/consensus-synthesizer.py) turns a flagged cluster into a ranked menu of proposed resolutions for a Core author. It **proposes, never executes**: the human author still reviews and commits the change over the normal `planner → approval → developer` spine.
4. **Induce the principle.** The most general, refutable lessons are induced into the fractal **`principles/`** tier ([memory-global/leaves/principles/](../../memory-global/leaves/principles/)), which the `planner` retrieves at a plan's *refutable-principle* element (retrieval-augmented planning).

The flow is deliberately one-directional from report to author: the channel decouples *submission* (anyone, no push rights needed) from *commit* (a Core author only), so an improvement can accumulate evidence and reach the Core without ever bypassing the protected-edit boundary.

## See also

- [docs/adr/README.md](../adr/README.md) — ADR-0001 is the decision this architecture implements; the ADR is the source of truth and this page is its canonical narrative home in the docs tree.
- [instruction-layering.md](instruction-layering.md) — the applicable precedence + replace-vs-merge contract.
- [layer-maintenance.md](../operations/layer-maintenance.md) — the rebase / `rerere` maintenance recipe for Team / Personal layers.
- [personal-layer.md](personal-layer.md) — the highest-precedence layer's scope and authority.
- [The seven-layer model](layers.md) — the single-machine shape this architecture distributes.
