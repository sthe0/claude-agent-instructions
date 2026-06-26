# Personal layer — scaffold and scope

> Part of the `Core < Team < Personal` substrate (ADR-0001; `docs/architecture/instruction-layering.md`). The
> Personal layer is the **highest-precedence** layer and is **not** shared through this repository —
> it lives on a single developer's machine.

## Scope and authority

- **Authority: EDIT-in-scope.** A developer may freely add or override instructions *for their own
  machine*. They may **not** edit Core in place — a Personal override that should become shared is
  promoted through the difficulty-accumulation channel (ADR-0001 § *Difficulty-accumulation
  mechanism*), never by editing the protected Core directly.
- **Precedence: nearest-wins.** A Personal leaf/constant overrides the same-named Team or Core one
  (replacement at leaf granularity for prose; key-level deep-merge for `config.md` constants).

## Where it lives

The Personal layer is the developer's own `~/.claude/` tree (the live install), maintained against
the moving Core with `git pull --autostash --rebase` + `git rerere` — see `docs/operations/layer-maintenance.md`.
Because it is per-machine, this repository carries only this scope note, **not** the personal
overrides themselves; committing personal overrides into the shared Core would defeat the layering.

## See also

- `docs/architecture/instruction-layering.md` — the precedence + replace-vs-merge contract.
- `docs/operations/layer-maintenance.md` — the rebase/`rerere` maintenance recipe.
- `memory-global/leaves/team/MEMORY.md` — the Team layer counterpart.
