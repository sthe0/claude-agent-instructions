# Claude / Cursor agent instructions

Single git repository for **global** instructions for **Claude Code** and **Cursor**. Edits in the repo appear at runtime via symlinks under `~/.claude/` and `~/.cursor/`. The canonical source for both tools is the same `CLAUDE.md`; the Cursor rule (`cursor/rules/claude-code-sync.mdc`) is a thin mirror that handles things Cursor cannot do natively (no `Skill` tool, no auto-memory writes).

File layout, instruction language, and the git workflow live in [skills/self-improvement/policy.md](skills/self-improvement/policy.md).

## Agent cooperation

> Living summary. When roles, mandatory gates, or delegation order change, update this section **in the same commit** as `CLAUDE.md` and the affected `agents/*.md` / `skills/*/SKILL.md`. Details — [CLAUDE.md](CLAUDE.md).

### Concepts

| Concept | Meaning |
|---|---|
| **Root coordinator** | The main Claude Code dialog. Coordinates, decides routing, does not replace specialists. Acts as the manager — there is no separate manager subagent. |
| **Subagent** | Prompt file in `agents/` (or `~/.claude/agents/` for machine-local) — invoked via `Task`, `subagent_type: <name>`. Currently no shipped subagents; infrastructure remains for future use. |
| **Flat skill** | Directory in `skills/<name>/` with `SKILL.md`. Invoked **inline** by the `Skill` tool or `/<name>`. Runs in the main thread, sees full context. Ships: `overcome-difficulty`, `self-improvement`, `tracker-management`, `ccgram-management`. |
| **Specialization skill** | Directory in `skills/specializations/<name>/` with `SKILL.md`. Symlinked flat into `~/.claude/skills/<name>/`. Spawned as a separate `claude -p` process with `--append-system-prompt-file`. Ships: `planner`, `developer`, `code-reviewer`, `thinker`, `yandex-cloud-expert`, `tech-writer`. |
| **Coordination engine** | `scripts/agentctl/` — a code state machine driving substantive-task control-flow (classify → route → plan gate → dispatch → verify → resolution gate); prose supplies the cognition at each step. See § Coordination engine. |
| **Global memory** | `~/.claude/memory-global/MEMORY.md` + `leaves/` — cross-project facts and practices. Imported into every session via `@…` in `CLAUDE.md`. |
| **Project memory** | `<project_cwd>/.claude/agent-memory/` — project-specific runbooks. Symlinked from `~/.claude/projects/<cwd-hash>/memory/` by `scripts/setup-project-memory.sh`, so native auto-memory reads / writes through the symlink. Committed to the project's git. |

### Principles

1. **Root coordinates first.** On a new substantive task, restate goal + done criterion, then decide routing (`Task → planner`, `developer`, `thinker`, …). Do not skip to coding.
2. **Understand → approve → execute.** Non-trivial work → plan with the user, wait for explicit OK unless "do it now".
3. **Ticket code — `developer`** in an isolated VCS copy, not the root in the default tree.
4. **Stuck → `overcome-difficulty` skill** in the same turn (not another blind retry).
5. **Feedback → `self-improvement` skill** in the same turn (including when the user reminds you it was missed).
6. **Org / ticket gates** — canonical runbooks live in project memory; agents point, do not restate.
7. **Runbooks → memory**, not generic agent prompts.
8. **File structure contract** — global tree stays current; after layout changes run `verify-layout-contract.sh`; on mismatch fix doc **or** disk, not both diverging.
9. **Instruction language** — English in this repo and in `.claude/agent-memory/` trees; exceptions need adjacent rationale. User-facing replies use the user's language.
10. **After instructions `pull`** — reconcile active work with new policy (see [skills/self-improvement/policy.md](skills/self-improvement/policy.md) § Git sync).

### Typical flows

```text
New task: root → (planner → approval → developer | thinker | direct answer)
Difficulty: root → Skill overcome-difficulty → (replan → planner | developer | …)
Feedback:  root → Skill self-improvement → (edits in this repo → commit → push after user confirms)
```

Anti-patterns: [memory-global/leaves/coordinator-pitfalls.md](memory-global/leaves/coordinator-pitfalls.md).

## Coordination engine (`scripts/agentctl/`)

`agentctl` is the code-driven coordination state machine. It owns the **deterministic control-flow** of a substantive task — classify → route → plan-approval gate → dispatch → per-stage verify → resolution gate, plus the difficulty/replan loop — while **prose supplies the cognition** at each step (the classification judgment, the plan content, the marker handling). Canon: code = deterministic control-flow, prose = cognition.

```bash
cd scripts && PYTHONPATH=scripts python3 -m agentctl <cmd>
# start → classify → plan → submit-plan → approve → next-stage → dispatch → record-result → verify-final → resolve
```

State lives at `~/.claude/agentctl/state/<session_id>.json`. The plan-approval and resolution gates are non-skippable — enforced by guardian hooks ([hook-state-gate.py](scripts/hook-state-gate.py)); [verify-agentctl.py](scripts/verify-agentctl.py) checks that every gate has its guardian hook and that the schema, transitions, and cognitive leaves stay consistent. Modules under `scripts/agentctl/`: `classify`, `config`, `state`, `store`, `machine`, `gates`, `directive`, `cli`, `dispatch`, `decompose`, `permissions`, `plan`, `continuations`.

## Quick start

```bash
git clone git@github.com:sthe0/claude-agent-instructions.git ~/claude-agent-instructions
~/claude-agent-instructions/scripts/setup-symlinks.sh
~/claude-agent-instructions/scripts/verify-instructions-sync.sh
```

Per-project local setup (from each product repo root; scripts live in that repo's `.claude/scripts/`):

```bash
# deepagent (Arc)
cd ~/arcadia/robot/deepagent && .claude/scripts/setup-local.sh

# logos (local only; logos/.claude is arcignored)
cd ~/arcadia/logos && .claude/scripts/setup-local.sh
```

`setup-local.sh` calls global `setup-project-memory.sh` where applicable and creates Cursor symlinks. See each project's `.claude/scripts/README.md`.

## Migrating a previously set-up machine

If `verify-layout-contract.sh` fails on a freshly pulled machine (stale directories / dangling symlinks from an old layout that `setup-symlinks.sh` alone cannot reconcile), see [docs/migrations/](docs/migrations/README.md) for per-refactor runbooks.

## Symlinks (global from git)

| In repo | Runtime |
|---|---|
| `CLAUDE.md` | `~/.claude/CLAUDE.md` |
| `agents/*.md` | `~/.claude/agents/<name>.md` |
| `skills/<name>/` | `~/.claude/skills/<name>/` |
| `memory-global/` | `~/.claude/memory-global/` |
| `cursor/rules/claude-code-sync.mdc` | `~/.cursor/rules/claude-code-sync.mdc` |
| `cursor/agents/*.md` | `~/.cursor/agents/<name>.md` |

Project-specific Cursor rules live in the project's own `<project>/.claude/rules/` tree (committed in the project's git), and are wired to `<project>/.cursor/rules/` by the project's setup. The deepagent case is automated by `setup-symlinks.sh` when `~/arcadia/robot/deepagent/.claude/rules/` is present.

Cursor-only assets live in [`cursor/`](cursor/README.md) and are intentionally isolated from `~/.claude/agents`.

## Scripts

Full inventory (machine-checked against the filesystem by [verify-readme.py](scripts/verify-readme.py)) lives in [scripts/README.md](scripts/README.md).

## Git workflow

```bash
~/claude-agent-instructions/scripts/sync-instructions-repo.sh pull
# edits → commit → push after user confirms
```

Runbook: [skills/self-improvement/policy.md](skills/self-improvement/policy.md) § Git sync.

## Agents in this repo (`agents/`)

None currently. The directory exists with a [README](agents/README.md) describing what it is reserved for. `setup-symlinks.sh` iterates the directory so future agents are picked up automatically.

Machine-local subagents (gitignored) → [`agents-local/`](agents-local/README.md). Project-local subagents → `<project_cwd>/.claude/agents/`.

## Skills in this repo (`skills/`)

### Flat skills (invoked inline in the current process)

<!-- inventory:skills:begin -->
| name | Triggers (summary) | File |
|---|---|---|
| `ccgram-management` | Manage the CCGram (Telegram) bridge — send / read messages, session mapping | [skills/ccgram-management/SKILL.md](skills/ccgram-management/SKILL.md) |
| `overcome-difficulty` | Reality diverges from the plan; verification failed; repeated error; missing observable | [skills/overcome-difficulty/SKILL.md](skills/overcome-difficulty/SKILL.md) |
| `self-improvement` | User correction or feedback about agent behavior | [skills/self-improvement/SKILL.md](skills/self-improvement/SKILL.md) |
| `tracker-management` | User mentions a ticket / issue / tracker, or a ticket key like `ABC-123` | [skills/tracker-management/SKILL.md](skills/tracker-management/SKILL.md) |
<!-- inventory:skills:end -->

### Specialization skills (spawned as `claude -p` per plan step)

Canonical path in repo: `skills/specializations/<name>/SKILL.md`. Symlinked flat into `~/.claude/skills/<name>/` by `setup-symlinks.sh`.

<!-- inventory:specializations:begin -->
| name | Spawns when a plan step calls for | File |
|---|---|---|
| `planner` | Decomposition, stages, dependencies, risks, done criteria | [skills/specializations/planner/SKILL.md](skills/specializations/planner/SKILL.md) |
| `developer` | Writing, refactoring, debugging, reviewing production code | [skills/specializations/developer/SKILL.md](skills/specializations/developer/SKILL.md) |
| `code-reviewer` | Maintainability / readability / reusability review of a diff (self-review or independent) | [skills/specializations/code-reviewer/SKILL.md](skills/specializations/code-reviewer/SKILL.md) |
| `thinker` | Independent reasoning check on a non-trivial chain | [skills/specializations/thinker/SKILL.md](skills/specializations/thinker/SKILL.md) |
| `yandex-cloud-expert` | Yandex Cloud / `yc` operations | [skills/specializations/yandex-cloud-expert/SKILL.md](skills/specializations/yandex-cloud-expert/SKILL.md) |
| `tech-writer` | Russian README / documentation authoring; polishing plans & long comments | [skills/specializations/tech-writer/SKILL.md](skills/specializations/tech-writer/SKILL.md) |
<!-- inventory:specializations:end -->

Full spawn template and return-marker handling: [CLAUDE.md](CLAUDE.md) § Spawning specialists.

## Not in this repository

| What | Where |
|---|---|
| Project memory | `<project_cwd>/.claude/agent-memory/` (project's git) |
| Extra agents | `~/.claude/agents/` |
| Local scripts | `~/.claude/scripts-local/` |
| Local skills | `~/.claude/skills/` (single-file `skills-local/*.md`, gitignored fallback) |

## Maintaining this README

When the cooperation model changes — update § Agent cooperation, [CLAUDE.md](CLAUDE.md), and affected `agents/*.md` or `skills/*/SKILL.md` in **one commit**.

When **directories, scripts, or symlinks** change:

1. Update [skills/self-improvement/policy.md](skills/self-improvement/policy.md) § File structure.
2. Align § Symlinks / § Scripts in this README with reality. The three inventory sentinels (scripts / flat skills / specializations) are machine-checked — run `scripts/verify-readme.py --fix` to reconcile the row sets, then fill in any `TODO` purpose cells by hand.
3. Run `scripts/verify-layout-contract.sh`, `scripts/verify-instructions-sync.sh`, and `scripts/verify-readme.py`.
