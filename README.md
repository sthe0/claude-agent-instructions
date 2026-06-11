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
| **Flat skill** | Directory in `skills/<name>/` with `SKILL.md`. Invoked **inline** by the `Skill` tool or `/<name>`. Runs in the main thread, sees full context. Ships: `overcome-difficulty`, `self-improvement`, `tracker-management`. |
| **Specialization skill** | Directory in `skills/specializations/<name>/` with `SKILL.md`. Symlinked flat into `~/.claude/skills/<name>/`. Spawned as a separate `claude -p` process with `--append-system-prompt-file`. Ships: `planner`, `developer`, `thinker`, `yandex-cloud-expert`, `tech-writer`. |
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

If the machine was set up before a refactor that changed the on-disk layout, `setup-symlinks.sh` alone may not be enough — there can be stale directories or dangling symlinks to remove. See [docs/migrations/](docs/migrations/) for per-refactor migration runbooks (the most recent is [Collapse manager / memory / self-improvement agents into root + skills (2026-05-22)](docs/migrations/2026-05-collapse-manager-memory.md)). The steps are documented manually; the previous automation script (`migrate-pre-2026-05.sh`) has been removed.

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

| Script | Purpose |
|---|---|
| [setup-symlinks.sh](scripts/setup-symlinks.sh) | Apply runtime symlinks for agents, skills, memory-global |
| [setup-project-memory.sh](scripts/setup-project-memory.sh) | Per-project: symlink shared agent memory into the project tree |
| [verify-instructions-sync.sh](scripts/verify-instructions-sync.sh) | Check global symlinks and drift |
| [verify-layout-contract.sh](scripts/verify-layout-contract.sh) | Compare tree to the layout in `skills/self-improvement/policy.md` |
| [verify-all.py](scripts/verify-all.py) | Run all instruction-policy checks (entry point; pre-commit hook uses `--staged`) |
| [verify-language.py](scripts/verify-language.py) | Enforce English-by-default policy with adjacent-exception rule |
| [verify-cross-refs.py](scripts/verify-cross-refs.py) | Catch broken intra-repo Markdown links and inline-code path references |
| [lint-cursor-mirror.py](cursor/scripts/lint-cursor-mirror.py) | Detect structural drift between `skills/` and the cursor mirror (flat-skill parity, specialization parity, trigger markers) |
| [install-cursor-links.sh](cursor/scripts/install-cursor-links.sh) | Apply Cursor-only symlinks (`~/.cursor/rules/*`, `~/.cursor/agents/*`) |
| [link-project-cursor-agents.sh](cursor/scripts/link-project-cursor-agents.sh) | Symlink `<project>/.cursor/agents/*` → `cursor/agents/` (used by deepagent `setup-local.sh`) |
| [migrate-cursor-namespace.sh](cursor/scripts/migrate-cursor-namespace.sh) | Migrate global + all `~/arcadia*/robot/deepagent` mounts (`--all-deepagent-mounts`) |
| [lint-permissions.py](scripts/lint-permissions.py) | Lint `permissions/*.json` schema (structure, fields, dates, duplicates) |
| [permissions-cli.py](scripts/permissions-cli.py) | CLI for workflow-level permissions: `list / check / grant / revoke / digest` |
| [spawn-specialist.py](scripts/spawn-specialist.py) | Wrap `claude -p` spawn: recursion cap, budget tier, permissions digest, marker validation, cost log |
| [coordinate-task.py](scripts/coordinate-task.py) | Drive the coordination cycle: `plan` (spawn planner, verify plan, print approval-gated run cmd) / `run --approved` (spawn developer, parse marker → exit code) |
| [spawn-cursor-specialist.py](scripts/spawn-cursor-specialist.py) | Cursor analogue: wrap `agent -p` specialization spawn — inline SKILL.md, budget→timeout, recursion cap, marker validation, cost log |
| [spawn-cursor-escape.py](scripts/spawn-cursor-escape.py) | Wrap `agent -p` overcome-difficulty escape for Cursor: recursion cap, API key, marker validation, cost log |
| [cost-report.py](scripts/cost-report.py) | Aggregate `~/.local/log/claude-spawn-costs.jsonl` (totals, by kind/tier/day, depth/marker distributions, refused events) |
| [memory-audit.py](scripts/memory-audit.py) | Find orphan / broken / stale memory leaves and frontmatter issues (informational; does not gate) |
| [verify-self-improvement-edit.py](scripts/verify-self-improvement-edit.py) | `commit-msg` gate: require `[self-improvement-reviewed]` in commits that touch `skills/self-improvement/` |
| [lint-prose-length.py](scripts/lint-prose-length.py) | Hard ceiling on instruction-file line counts (`CLAUDE.md`, cursor mirror, skill SKILL.md, policy.md) per `config.md` limits |
| [sync-instructions-repo.sh](scripts/sync-instructions-repo.sh) | `pull` / `push` this repo |
| [install-git-hooks.sh](scripts/install-git-hooks.sh) | Install `pre-commit` (run `verify-all.py --staged`) and `post-commit` (push reminder) |
| [hook-self-critique-reminder.py](scripts/hook-self-critique-reminder.py) | PostToolUse Write: nudge to invoke `self-improvement` after writing an experience leaf with a substantive § Self-critique |
| [hook-tracker-reminder.py](scripts/hook-tracker-reminder.py) | UserPromptSubmit: detect tracker references in the prompt and remind to invoke `tracker-management` |
| [hook-push-confirmation-reminder.py](scripts/hook-push-confirmation-reminder.py) | PreToolUse Bash: nudge to verify user push-confirmation before `git push` / `sync-instructions-repo.sh push` |
| [hook-readme-currency-reminder.py](scripts/hook-readme-currency-reminder.py) | PreToolUse Bash: before `git`/`arc commit`, list READMEs next to changed code that aren't in the changeset — verify currency |
| [hook-resolution-reminder.py](scripts/hook-resolution-reminder.py) | UserPromptSubmit: nudge to ask for explicit resolution when the user's prompt is brief gratitude |
| [hook-context-growth-reminder.py](scripts/hook-context-growth-reminder.py) | UserPromptSubmit: nudge when live context size crosses a band (reads transcript usage); throttled per band per session |
| [hook-prewrite-plan-check.py](scripts/hook-prewrite-plan-check.py) | PreToolUse Edit/Write: after 3 code edits with no plan file, one-time nudge to invoke planner |
| [hook-retry-detector.py](scripts/hook-retry-detector.py) | PreToolUse Bash: same normalized command 3+ times → nudge to invoke overcome-difficulty |
| [install-reminder-hooks.sh](scripts/install-reminder-hooks.sh) | Idempotently wire the canonical reminder-hook set into machine-local `settings.json` (hooks are not merged from `base.json`) |
| [set-context-cap.sh](scripts/set-context-cap.sh) | Set an arbitrary context-size cap (auto-compaction trigger) in tokens — computes `CLAUDE_CODE_DISABLE_1M_CONTEXT` + `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` into `base.json`; max ~830k (83% clamp) |
| [install-sync-cron.sh](scripts/install-sync-cron.sh) | Cron: git pull every 10 min (opt-in; not installed by `setup-symlinks.sh`) |

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

| name | Triggers (summary) | File |
|---|---|---|
| `overcome-difficulty` | Reality diverges from the plan; verification failed; repeated error; missing observable | [skills/overcome-difficulty/SKILL.md](skills/overcome-difficulty/SKILL.md) |
| `self-improvement` | User correction or feedback about agent behavior | [skills/self-improvement/SKILL.md](skills/self-improvement/SKILL.md) |
| `tracker-management` | User mentions a ticket / issue / tracker, or a ticket key like `ABC-123` | [skills/tracker-management/SKILL.md](skills/tracker-management/SKILL.md) |

### Specialization skills (spawned as `claude -p` per plan step)

Canonical path in repo: `skills/specializations/<name>/SKILL.md`. Symlinked flat into `~/.claude/skills/<name>/` by `setup-symlinks.sh`.

| name | Spawns when a plan step calls for | File |
|---|---|---|
| `planner` | Decomposition, stages, dependencies, risks, done criteria | [skills/specializations/planner/SKILL.md](skills/specializations/planner/SKILL.md) |
| `developer` | Writing, refactoring, debugging, reviewing production code | [skills/specializations/developer/SKILL.md](skills/specializations/developer/SKILL.md) |
| `thinker` | Independent reasoning check on a non-trivial chain | [skills/specializations/thinker/SKILL.md](skills/specializations/thinker/SKILL.md) |
| `yandex-cloud-expert` | Yandex Cloud / `yc` operations | [skills/specializations/yandex-cloud-expert/SKILL.md](skills/specializations/yandex-cloud-expert/SKILL.md) |
| `tech-writer` | Russian README / documentation authoring; polishing plans & long comments | [skills/specializations/tech-writer/SKILL.md](skills/specializations/tech-writer/SKILL.md) |

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
2. Align § Symlinks / § Scripts in this README with reality.
3. Run `scripts/verify-layout-contract.sh` and `scripts/verify-instructions-sync.sh`.
