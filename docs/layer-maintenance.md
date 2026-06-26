# Maintaining a layer over a moving Core

> Companion to `docs/instruction-layering.md` (the precedence contract) and ADR-0001. This is the
> operational recipe for keeping a **Team** or **Personal** layer current as the shared **Core**
> evolves. The compose model is **override + rebase**, not merge (see the layering contract).

## Why rebase, not merge

A higher layer (Team, Personal) holds overrides *on top of* Core. When Core advances, the layer's
overrides must be **replayed onto the new Core** so they stay "on top" — that is a rebase. A merge
would instead interleave the histories and repeatedly re-surface the same override-vs-Core conflict.

## The recipe

```bash
# one-time setup on the machine that hosts the layer
git config rerere.enabled true        # remember how each conflict was resolved
git config rerere.autoupdate true     # re-apply a remembered resolution automatically

# routine update of the layer against the moving Core
git pull --autostash --rebase         # stash local work, replay overrides onto new Core, restore
```

`git config --get rerere.enabled || true` confirms the setting is in place (the `|| true` keeps the
check non-fatal in scripts when the key is unset).

## The `rerere` caveat

`git rerere` (**re**use **re**corded **re**solution) records how you resolved a conflict and replays
that resolution when the *identical* conflict recurs on a later rebase — so a personal override
rebased onto a moving Core does not force you to re-resolve the same hunk every time.

**It only auto-resolves *identical* recurring conflicts.** If Core changes the *same* hunk the layer
overrides (a genuinely new conflict, not a recurrence), `rerere` cannot help — a human, or the active
synthesizer of ADR-0001 § *Active synthesizer*, must resolve it. `rerere` reduces toil on repeated
conflicts; it does not decide novel ones.

## See also

- `docs/instruction-layering.md` — the `Core < Team < Personal` precedence + replace-vs-merge contract.
- `docs/personal-layer.md` — scope of a Personal layer (EDIT-in-scope, lives on the developer's machine).
- `memory-global/leaves/team/MEMORY.md` — scope of the Team layer (EDIT, shared via the project repo).
- `docs/adr/0001-consensus-architecture.md` § *Substrate — precedence layers*.
