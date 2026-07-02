# Instruction layering — the precedence contract

> Operational distillation of ADR-0001 § *Substrate — precedence layers*. The ADR is the source of
> truth; this document paraphrases it into a contract a tool or a developer can apply, and must never
> contradict it. It changes **no** behavioural rule in `CLAUDE.md` or `skills/` — it only records how
> the existing layers compose.

The agent's instructions are distributed to several developers. A shared core evolves while each
developer keeps personal and project overrides on top. The correct compose operation is
**override + rebase**, not a blind merge — and "override" has to state *what unit* it replaces.

## The ladder

One explicit, fixed precedence list, lowest → highest:

```
Core < Team < Personal
```

- **Core** — the shared, protected instructions (`CLAUDE.md`, `config.md`, `skills/**`, `agents/**`,
  `memory-global/**`, the `scripts/agentctl` engine). Edited only by commit-authorized authors
  (`CODEOWNERS`). Everyone consumes it; an uncontrolled edit breaks everyone.
- **Team** — project-scoped overrides shared via a project's own git
  (`<project>/.claude/agent-memory/**`, `<project>/.claude/rules/*.mdc`, `<project>/.claude/skills/**`).
- **Personal** — a single developer's overrides on their own machine.

**Tiebreak: nearest-wins / last-wins.** When two layers speak to the same point, the higher
(nearer) layer wins; within one layer, the later-loaded value wins. This mirrors cascading
`CLAUDE.md`/`AGENTS.md` proximity resolution and the OpenAI Model Spec authority tiers.

A higher layer may **add** to Core and may **locally override** it, but may not edit the Core
artifact in place — Core changes go through the `planner → approval → developer` spine and
`CODEOWNERS`.

## Replace vs. merge — chosen explicitly per artifact class

Every surveyed config system defaults to **replacing** a complex value wholesale rather than
field-merging it (Viper "entirely replaced"; Dynaconf last-wins; Helm deep-merges only map leaves).
A layering scheme therefore **must choose** replace-vs-merge per class of artifact — it cannot assume
merge.

| Artifact class | Unit | Compose operation |
|---|---|---|
| **Prose** (`CLAUDE.md` sections, memory leaves, skill `SKILL.md`/`policy.md`) | the **leaf / file** ("one fact = one file") | **Replacement** at leaf granularity — a higher layer's leaf replaces the same-named Core leaf wholesale; prose is never line-merged across layers. |
| **Structured constants** (`config.md` key/value table) | the **key** | **Deep-merge** (Helm-leaf style) — a higher layer overrides individual keys; unspecified keys fall through to Core. |

Rationale: prose carries meaning that a line-merge silently corrupts (the meaning is in the whole
leaf, not the line); structured constants are independent scalars where key-level override is exactly
what an operator wants. The leaf being the unit of replacement is *why* the "one fact = one file"
memory convention exists.

## Ordered layers — insertion semantics

Some layers are **ordered collections** rather than key/value maps: the `skills/` list, and the leaf
ordering inside a `MEMORY.md` index. For these, pin the insertion operation and its stability:

- **Insertion: append.** A higher layer's additional skills / leaves are **appended** after the Core
  entries, preserving Core's relative order. Core ordering is never reshuffled by an override.
- **Override-by-name still replaces.** An appended entry that shares a Core entry's name replaces it
  in place (per the prose rule above) rather than duplicating it.
- **Version-stability caveat.** Array/list merge semantics are notoriously version-dependent (the
  Kustomize array-merge gotcha, where the merge strategy changed across schema versions and silently
  altered results). Pin the strategy explicitly and treat any change to it as a breaking change to
  the layering contract, not a transparent upgrade.

## Maintaining a layer over a moving Core

A Team or Personal layer is kept current against the evolving Core with `git pull --autostash
--rebase` + `git rerere` — the full recipe, the one-time setup, and the `rerere` identical-conflict
caveat live in `docs/operations/layer-maintenance.md` (kept in one place to avoid drift).

## Staying current: the daily refresh offer

Both a moving Core and a moving Team layer need pulling, but a *silent* background pull risks
stashing/rebasing over uncommitted local work with no one watching. Instead, `hook-instructions-refresh-due.py`
(a `UserPromptSubmit` hook) checks once per calendar day, on the day's first prompt, whether the
Core repo and — if the current project carries a git-tracked `.claude/` Team layer distinct from
Core — that project's own repo are behind their upstream (`git fetch` + `rev-list --count`, bounded
and fail-open: any git error is treated as not-behind and stays silent). When a layer is behind, it
prints a nudge naming the layer and its exact pull command; the agent must **offer** the pull via
`AskUserQuestion` before running it — the hook itself never pulls. This supersedes the older silent
10-min cron/systemd timer (`install-sync-cron.sh` / `install-sync-systemd-timer.sh`), which is now
deprecated in favor of this explicit, opt-in cadence.

## See also

- `docs/adr/0001-consensus-architecture.md` — the decision this contract implements.
- `docs/operations/layer-maintenance.md` — the rebase/`rerere` maintenance recipe for Team/Personal layers.
- `docs/architecture/personal-layer.md` / `memory-global/leaves/team/MEMORY.md` — the Personal and Team layer scopes.
- `memory-global/leaves/memory-usage.md` — the "one fact = one file" leaf convention that makes the
  leaf the unit of replacement.
