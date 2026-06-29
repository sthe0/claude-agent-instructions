---
name: spawning-specialists
description: Full mechanics of spawning a specialist via claude -p — spawn template inputs, budget tiers, recursion cap, monitoring a running spawn, after-spawn checks, bypassPermissions discipline, return markers.
type: reference
created: 2026-06-04
last_verified: 2026-06-25
---

# Spawning specialists

A **spawned specialist** is a fresh Claude Code process (`claude -p`) with a specialization skill appended to its system prompt. No parent conversation history, but the same CLAUDE.md, memory, skills, and tools. Use this mode when inline (see `CLAUDE.md` § Invoking specialists) is not sufficient: large scope, fresh-context-as-feature, multi-stage work, or you want the spawn-cost log entry.

## When NOT to spawn — tiny-edit-in-large-file

Means is chosen by *what the work needs to hold in context*, not only by work type. A spawn carries a ~150k autocompact ceiling (`spawn-specialist.py` injects `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`); a developer that full-Reads a large file (>~1000 lines) re-reads it after each autocompact and **thrashes** — the harness emits *"Autocompact is thrashing: context refilled to the limit within N turns"*, the process dies `MALFORMED`, and **no commits land**. This is mis-assigned means: the task is tiny (a few-line edit, or restoring an existing commit) but the executor cannot hold the file context.

To remove this divergence: when the edit is surgical (≤ a few lines) but the target file is large, or the change is "restore an existing commit", **do it in-thread** — `arc cherry-pick <sha>` recovers a commit without dumping its diff into context; a ranged Read (`offset`/`limit`) + Edit touches only the edit region, never the whole file. If a spawn is genuinely unavoidable (multi-file, needs fresh context), the dossier **must** forbid full-file Reads (ranged only) and route large command outputs through `head` / `scripts/offload-large.sh`. Two consecutive `MALFORMED`-with-thrash on the same step is the overcome-difficulty signal — switch means, do not re-spawn a third time.

## Spawn-readiness for gated writes (state-gate)

`hook-state-gate.py` authorizes production Edit/Write by the **acting session's own** engine node — and a spawned specialist runs under a **fresh, unclassified** session that inherits none of the parent's execution authority. So spawning a `developer` for gated writes while the child has no plan/classification → every write is denied and the spawn burns its whole budget. Before such a spawn, ensure the child can reach `EXECUTING` in its own session: a `.toml` plan (markdown plans are structure-verified but do **not** populate `state.stages`, so `next-stage`/`dispatch` never reach an execution node — only `.toml` does). If you can't guarantee that, apply the reviewed code in-thread after driving *your* session to `EXECUTING` instead. See [experience/2026-06-25-state-gate-needs-acting-session-at-executing-via-toml.md](experience/2026-06-25-state-gate-needs-acting-session-at-executing-via-toml.md).

## Spawn template

Use `scripts/spawn-specialist.py` — it handles process concerns (recursion-cap check, budget-tier resolution, permission digest, return-marker validation, cost log). Run `--help` for the flag list; `--dry-run` previews the assembled prompt and command.

Cognitive inputs the manager supplies (mechanics are in `--help`):

- `--kind` — specialization name (must exist at `~/.claude/skills/<kind>/SKILL.md`): `planner` / `developer` / `thinker` / `yandex-cloud-expert` / `tech-writer` / project-local.
- `--plan` — markdown plan with the owned step marked `**<<this step>>**`.
- `--done-criterion` + `--criterion-type` (`measurable` | `acceptance-review`).
- `--context-dossier` — 5–10 line digest of conversation context the specialist cannot read on its own (intent nuances, rejected options, in-session decisions, terminology aliases). Omit if nothing's missable.
- `--budget` (cost ceiling) — see table below. `--complexity` (`low`/`medium`/`high` → haiku/sonnet/opus) sets the sub-agent model by **assessed task difficulty**, overriding the per-kind default; rubric in `--help`. Budget and complexity are distinct axes — a cheap-budget task can still need opus.
- `--project-permissions <project>/.claude/agent-memory/permissions.json` if inside a project tree.

**Budget tiers** (resolve to `budget-*-usd` in `config.md`):

| Tier | Use for |
|---|---|
| `small` | Single-file edit, narrow analysis, short plan refinement |
| `medium` | Multi-file change with tests, scoped refactor, standard plan — default when in doubt |
| `large` | Cross-cutting change, multi-stage plan, full feature, expensive research |

A specialist that hits its cap returns control with whatever it has.

## Recursion cap

`spawn-specialist.py` enforces `max-recursion-depth` (config.md): refuses with exit 3 when the next depth would exceed it. Applies to every `claude -p` invocation, including `overcome-difficulty`'s recursive escape — no exemption.

On refuse — **do not retry**. Stop, summarize for the user (original task, current chain state, what the next spawn would do, why the cap hit), ask whether to continue manually, restart, or accept partial.

## Monitoring a running spawn

`spawn-specialist.py` prints `transcript=<path>` to stderr within ~10s — the freshest jsonl under `~/.claude/projects/<sanitized-cwd>/` that didn't exist before the spawn. Tail that file periodically (~5 min cadence for `developer` spawns) to catch divergence: wrong `cwd`, writes/commits outside the assigned mount, off-scope work (e.g. running someone else's smoke test). **Kill early** — one rescoped re-spawn is cheaper than waiting for a runaway to exhaust its cap.

## After the spawn (kill or completion)

Before deciding the next move (accept, re-spawn, manual takeover), check **both** uncommitted state *and* commit history on the assigned branch:

```bash
arc status      # uncommitted changes only
arc log -n 5    # whether the spawn committed on-scope work before drifting
```

(git equivalents in non-arc repos.) A spawn killed for off-scope behavior may still have committed legitimate on-scope work before drifting — `status` is clean, but `log` shows the commit. Skipping `log` has cost a redundant verification spawn in one observed case.

## `bypassPermissions` for `developer`

The wrapper defaults `kind=developer` to `--permission-mode bypassPermissions` so the child can perform unattended Read / Grep / Write on the assigned mount. The harness no longer prompts on individual writes — that safety is replaced by **prompt-level discipline**:

- The `--constraints` / dossier **must** contain an explicit hard-deny list — no `cd` / no Write / no Edit / no `arc commit` outside `<assigned-mount>`, no `ya package` / `docker push` / smoke tests of other tickets — plus a self-check at session start (`pwd` ⊆ expected mount; if not, return `CLARIFY:`).
- Without this discipline the child treats sibling mounts (referenced as "analogs") as fair game for "understanding through execution".

## Return markers

Each specialist's first non-empty line carries one of these. The wrapper validates and prefixes the output with `MALFORMED:` if the marker is missing.

- `COMPLETED:` — step done; summary + artifacts.
- `PLAN-READY:` — **planner-only.** Plan ready; manager must obtain explicit user approval before next spawn. Hard gate.
- `INCOMPLETE:` — partial; what's done, what's left, blocker.
- `CLARIFY:` — specialist needs one specific fact (path, number, choice between named options) to continue. Manager answers, re-spawns with answer embedded.
- `REPLAN:` — plan-level difficulty; specialist proposes a revision.
- `PERMISSION-REQUEST:` — explicit permission needed for a specific external / irreversible action.
- `ESCALATE:` — other decision (manager or user) affecting plan / scope.

`CLARIFY:` vs `ESCALATE:` — fact vs decision. Prefer `CLARIFY:` when work resumes immediately on the answer.

Handling each marker after the spawn returns: see [handling-escalations.md](handling-escalations.md).
