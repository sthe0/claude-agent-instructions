# Claude / Cursor agent instructions

Single git repository for **global** instructions for **Claude Code** and **Cursor**. Edits in the repo appear at runtime via symlinks under `~/.claude/` and `~/.cursor/`. The canonical source for both tools is the same `CLAUDE.md`; the Cursor rule (`cursor-rules/claude-code-sync.mdc`) is a thin mirror that handles things Cursor cannot do natively (no `Skill` tool, no auto-memory writes).

File layout, instruction language, and the git workflow live in [skills/self-improvement/policy.md](skills/self-improvement/policy.md).

## Agent cooperation

> Living summary. When roles, mandatory gates, or delegation order change, update this section **in the same commit** as `CLAUDE.md` and the affected `agents/*.md` / `skills/*/SKILL.md`. Details — [CLAUDE.md](CLAUDE.md).

### Concepts

| Concept | Meaning |
|---|---|
| **Root coordinator** | The main Claude Code dialog. Coordinates, decides routing, does not replace specialists. Acts as the manager — there is no separate manager subagent. |
| **Subagent** | Prompt file in `agents/` (or `~/.claude/agents/` for machine-local) — invoke via `Task`, `subagent_type: <name>`. |
| **Skill** | Directory in `skills/<name>/` with `SKILL.md` (+ optional supporting files). Invoked via the `Skill` tool or `/<name>` from the user. Runs in the main thread, sees full context. |
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
Feedback:  root → Skill self-improvement → (edits in this repo → commit → push)
```

Anti-patterns: [memory-global/leaves/coordinator-pitfalls.md](memory-global/leaves/coordinator-pitfalls.md).

## Quick start

```bash
git clone git@github.com:sthe0/claude-agent-instructions.git ~/claude-agent-instructions
~/claude-agent-instructions/scripts/setup-symlinks.sh
~/claude-agent-instructions/scripts/verify-instructions-sync.sh
```

Per-project memory (run inside each project where you want shared agent memory):

```bash
~/claude-agent-instructions/scripts/setup-project-memory.sh
git add .claude/agent-memory && git commit -m "agent memory: bootstrap"
```

## Migrating a previously set-up machine

If the machine was set up before a refactor that changed the on-disk layout, `setup-symlinks.sh` alone may not be enough — there can be stale directories or dangling symlinks to remove. See [docs/migrations/](docs/migrations/) for per-refactor migration runbooks. The most recent is [Collapse manager / memory / self-improvement agents into root + skills (2026-05-22)](docs/migrations/2026-05-collapse-manager-memory.md); the automated form is `scripts/migrate-pre-2026-05.sh`.

## Symlinks (global from git)

| In repo | Runtime |
|---|---|
| `CLAUDE.md` | `~/.claude/CLAUDE.md` |
| `agents/*.md` | `~/.claude/agents/<name>.md` |
| `skills/<name>/` | `~/.claude/skills/<name>/` |
| `memory-global/` | `~/.claude/memory-global/` |
| `cursor-rules/claude-code-sync.mdc` | `~/.cursor/rules/claude-code-sync.mdc` |
| — | `~/.cursor/agents` → `~/.claude/agents` (so Cursor sees the same subagents) |
| `cursor-rules/project-overlay-deepagent.mdc` | `~/arcadia/robot/deepagent/.cursor/rules/deepagent-project.mdc` (Arcadia machines only) |

## Scripts

| Script | Purpose |
|---|---|
| [setup-symlinks.sh](scripts/setup-symlinks.sh) | Apply runtime symlinks for agents, skills, memory-global |
| [setup-project-memory.sh](scripts/setup-project-memory.sh) | Per-project: symlink shared agent memory into the project tree |
| [verify-instructions-sync.sh](scripts/verify-instructions-sync.sh) | Check global symlinks and drift |
| [verify-layout-contract.sh](scripts/verify-layout-contract.sh) | Compare tree to the layout in `skills/self-improvement/policy.md` |
| [sync-instructions-repo.sh](scripts/sync-instructions-repo.sh) | `pull` / `push` this repo |
| [install-git-hooks.sh](scripts/install-git-hooks.sh) | post-commit → push |
| [install-sync-cron.sh](scripts/install-sync-cron.sh) | Cron: git pull every 10 min (opt-in; not installed by `setup-symlinks.sh`) |

## Git workflow

```bash
~/claude-agent-instructions/scripts/sync-instructions-repo.sh pull
# edits → commit → push (post-commit hook)
```

Runbook: [skills/self-improvement/policy.md](skills/self-improvement/policy.md) § Git sync.

## Agents in this repo (`agents/`)

| name | File |
|---|---|
| developer | [agents/developer.md](agents/developer.md) |
| planner | [agents/planner.md](agents/planner.md) |
| thinker | [agents/thinker.md](agents/thinker.md) |
| yandex-cloud-expert | [agents/yandex-cloud-expert.md](agents/yandex-cloud-expert.md) |

Additional subagents — only files in `~/.claude/agents/` not listed in this repo's `agents/` (machine-local, gitignored).

## Skills in this repo (`skills/`)

| name | Triggers (summary) | File |
|---|---|---|
| `overcome-difficulty` | Verification failed, blocker, repeated error, plan mismatch, 2+ corrections | [skills/overcome-difficulty/SKILL.md](skills/overcome-difficulty/SKILL.md) |
| `self-improvement` | User correction or feedback about agent behavior | [skills/self-improvement/SKILL.md](skills/self-improvement/SKILL.md) |

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
2. Align § Symlinks / § Scripts in this README with reality.
3. Run `scripts/verify-layout-contract.sh` and `scripts/verify-instructions-sync.sh`.
