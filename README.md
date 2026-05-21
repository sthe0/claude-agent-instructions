# Claude / Cursor agent instructions

Single git repository for **global** instructions for **Claude Code** and **Cursor**. Edits in the repo appear in both IDEs via symlinks under `~/.claude/` and `~/.cursor/`.

File structure contract: [memory-global/agent-instructions/file-structure-contract.md](memory-global/agent-instructions/file-structure-contract.md). Runtime paths: [runtime-layout.md](memory-global/agent-instructions/runtime-layout.md).

## Agent cooperation

> **This section is a living summary of how agents cooperate.** When roles, mandatory gates, or delegation order change, update it **in the same commit** as `CLAUDE.md`, `agents/*.md`, and `cursor-rules/claude-code-sync.mdc`. Details — [CLAUDE.md](CLAUDE.md).

### Concepts

| Concept | Meaning |
|---------|--------|
| **Parent agent** | Dialog in Cursor / Claude Code: delegates, does not replace specialists |
| **Subagent** | Prompt in `~/claude-agent-instructions/agents/` or extra file in `~/.claude/agents/`; invoke `Task`, `subagent_type: <name>` |
| **Memory (global)** | `~/.claude/memory-global/` — how to think, coordination, git sync |
| **Memory (local)** | `~/.claude/memory/INDEX.md` — product and environment runbooks (outside this git) |
| **Instructions** | This repo → `~/.claude/CLAUDE.md` |

### Principles

1. **Mandatory beats "prefer".** New task → **manager** first; plan approval; self-improvement on feedback; **manager** again on difficulty ([CLAUDE.md](CLAUDE.md)).
2. **New task → manager → specialists.** Parent does not call **planner** / **developer** before **manager** on a new goal.
3. **Understand → approve → execute.** **manager** routes **planner**; then **developer**, except explicit "do it now".
4. **Ticket code — developer** in isolated VCS copy, not parent in shared default copy ([CLAUDE.md](CLAUDE.md)).
5. **Difficulty — manager** again in the same turn.
6. **Feedback — self-improvement** in the same turn (including when user reminds agent forgot).
7. **Org/ticket gates** — canonical runbooks in local memory + `org-yandex.mdc`; global agents point, do not restate ([tracker-ticket-workflow.md](~/.claude/memory/claude-code/tracker-ticket-workflow.md)).
8. **Runbooks — memory INDEX**, not generic agent prompts.
9. **File structure contract** — global/local tree docs stay current; after changes run `verify-layout-contract.sh`; on mismatch fix docs or disk.
10. **Instruction language** — English in this git repo and in `~/.claude/memory/`; exceptions need adjacent rationale ([instruction-language.md](memory-global/agent-instructions/instruction-language.md)). User replies — same language as the request.
11. **After instructions `pull`** — reconcile active work with new policy ([instructions-git-sync.md](memory-global/agent-instructions/instructions-git-sync.md) § After pull).

### Typical flows

```text
New task: manager → (memory | planner → approval → developer | thinker | optional ~/.claude/agents/)
Difficulty (same task): manager → (replan → planner | developer | memory | …)
```

Global anti-patterns: [memory-global/development/typical-coordinator-pitfalls.md](memory-global/development/typical-coordinator-pitfalls.md).

## Quick start

```bash
git clone git@github.com:sthe0/claude-agent-instructions.git ~/claude-agent-instructions
~/claude-agent-instructions/scripts/setup-symlinks.sh
~/claude-agent-instructions/scripts/verify-instructions-sync.sh
```

Optional local configuration (extra agents, memory, scripts): `~/.claude/memory/INDEX.md` after `setup-symlinks.sh`.

## Symlinks (global from git)

| In repo | Runtime |
|--------|---------|
| `CLAUDE.md` | `~/.claude/CLAUDE.md` |
| `agents/*.md` | `~/.claude/agents/<name>.md` |
| `memory-global/` | `~/.claude/memory-global/` |
| `cursor-rules/claude-code-sync.mdc` | `~/.cursor/rules/` |
| — | `~/.cursor/agents` → `~/.claude/agents` |
| — | `~/.cursor/rules/org-yandex.mdc` ← local arc (`cursor-rules/org-yandex.mdc`) |

Local `~/.claude/memory/`, `org-yandex.mdc`, and `~/.claude/scripts-local/` are **not** in this git — configured on the machine (`setup-symlinks.sh`).

## Scripts (global, git)

| Script | Purpose |
|--------|---------|
| [setup-symlinks.sh](scripts/setup-symlinks.sh) | Symlinks for Claude + Cursor (+ local runtime paths) |
| [verify-instructions-sync.sh](scripts/verify-instructions-sync.sh) | Check global symlinks; delegates local verify |
| [verify-layout-contract.sh](scripts/verify-layout-contract.sh) | Compare tree to file-structure-contract.md |
| [sync-instructions-repo.sh](scripts/sync-instructions-repo.sh) | `pull` / `push` this repo |
| [install-git-hooks.sh](scripts/install-git-hooks.sh) | post-commit → push |
| [install-sync-cron.sh](scripts/install-sync-cron.sh) | Cron: git pull every 10 min |

Local scripts: `~/.claude/scripts-local/` (see README in that directory after `setup-symlinks.sh`).

## Git workflow

```bash
~/claude-agent-instructions/scripts/sync-instructions-repo.sh pull
# edits → commit → push (post-commit hook)
```

Runbook: [memory-global/agent-instructions/instructions-git-sync.md](memory-global/agent-instructions/instructions-git-sync.md).

## Agents in this repo (`agents/`)

| name | File |
|------|------|
| manager | [agents/manager.md](agents/manager.md) |
| planner | [agents/planner.md](agents/planner.md) |
| developer | [agents/developer.md](agents/developer.md) |
| thinker | [agents/thinker.md](agents/thinker.md) |
| memory | [agents/memory.md](agents/memory.md) |
| self-improvement | [agents/self-improvement.md](agents/self-improvement.md) |
| yandex-cloud-expert | [agents/yandex-cloud-expert.md](agents/yandex-cloud-expert.md) |

Additional subagents — only files in `~/.claude/agents/` not listed in this repo's `agents/`.

## Not in this repository

| What | Where |
|-----|------------|
| Local memory | `~/.claude/memory/INDEX.md` |
| Extra agents | `~/.claude/agents/` |
| Local scripts | `~/.claude/scripts-local/` |
| Skills | `~/.claude/skills/` |

## Maintaining this README

When the cooperation model changes — update § Agent cooperation, [CLAUDE.md](CLAUDE.md), and affected `agents/*.md` in **one commit**.

When **directories, scripts, or symlinks** change (global or local):

1. Update [file-structure-contract.md](memory-global/agent-instructions/file-structure-contract.md) and if needed [runtime-layout.md](memory-global/agent-instructions/runtime-layout.md).
2. Align § symlinks/scripts in this README with reality.
3. Run `scripts/verify-layout-contract.sh` and `verify-instructions-sync.sh`.
4. Local layer — per runbook in `~/.claude/memory/INDEX.md` and `~/.claude/scripts-local/`.
