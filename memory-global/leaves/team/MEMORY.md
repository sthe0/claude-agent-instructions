# Team layer

Scope note + sub-index for the **Team** precedence layer (ADR-0001 `Core < Team < Personal`;
`docs/instruction-layering.md`). Pointed at from `memory-global/MEMORY.md`. Not auto-loaded by the
harness.

## Scope and authority

- **Authority: EDIT.** A project's developers may add or override instructions for that project,
  shared via the project's own git (`<project>/.claude/agent-memory/**`, `<project>/.claude/rules/*.mdc`,
  `<project>/.claude/skills/**`). They may **not** edit Core in place — a Team override destined for
  the shared Core is promoted through the difficulty-accumulation channel (ADR-0001 §
  *Difficulty-accumulation mechanism*), never by editing protected Core directly.
- **Precedence: nearest-wins**, below Personal and above Core. Prose overrides replace at leaf
  granularity; `config.md` constants deep-merge at key level.

## Leaves

None yet. Team-scoped global leaves (rare — most Team content lives in a project's own
`.claude/agent-memory/`) are added here with a pointer line, mirroring the other sub-indexes.

## See also

- `docs/instruction-layering.md` — the precedence + replace-vs-merge contract.
- `docs/personal-layer.md` — the Personal layer counterpart.
- `docs/layer-maintenance.md` — the rebase/`rerere` maintenance recipe.
