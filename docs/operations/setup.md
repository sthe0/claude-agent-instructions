# Setup and distribution

> How to wire the agent system on a new machine, confirm the runtime is ready, and distribute instructions to developers who can't push to Core.

## Initial setup

```bash
git clone git@github.com:sthe0/claude-agent-instructions.git ~/claude-agent-instructions
# Read-only HTTPS clone works just as well if you have no push access:
#   git clone https://github.com/sthe0/claude-agent-instructions.git ~/claude-agent-instructions
~/claude-agent-instructions/scripts/setup-symlinks.sh
~/claude-agent-instructions/scripts/verify-instructions-sync.sh
~/claude-agent-instructions/scripts/doctor.sh
```

`setup-symlinks.sh` is the single wiring command. It creates the symlinks under `~/.claude/` and `~/.cursor/`, merges the policy settings, and installs the reminder, engine-gate, and git hooks (via `apply-settings.sh`, `install-reminder-hooks.sh`, and `install-git-hooks.sh`). Re-run it whenever the repo layout changes.

`verify-instructions-sync.sh` confirms symlink integrity from the repo-developer's perspective.

`doctor.sh` confirms the runtime is actually ready: the `claude` CLI is on PATH, the constitution is loaded, the engine hooks are armed, and `agentctl` runs. Fix any `[FAIL]` line (usually by re-running `setup-symlinks.sh`) before the first task.

## Symlinks wired by `setup-symlinks.sh`

| In repo | Runtime path |
|---|---|
| `CLAUDE.md` | `~/.claude/CLAUDE.md` |
| `agents/*.md` | `~/.claude/agents/<name>.md` |
| `skills/<name>/` | `~/.claude/skills/<name>/` |
| `memory-global/` | `~/.claude/memory-global/` |
| `cursor/rules/claude-code-sync.mdc` | `~/.cursor/rules/claude-code-sync.mdc` |
| `cursor/agents/*.md` | `~/.cursor/agents/<name>.md` |

Project-specific Cursor rules live in the project's own `<project>/.claude/rules/` tree and are wired to `<project>/.cursor/rules/` by the project's own setup script.

## Per-project local setup

Each product repo has its own `.claude/scripts/setup-local.sh`. Run it from the project root after the global setup above. It calls the global `scripts/setup-project-memory.sh` where applicable and creates any project-level Cursor symlinks.

## Distribution — no push rights required

You do **not** need push rights to use the system. Instructions are read locally from the clone. Self-improvement edits land as local commits; the upstream push is gated behind explicit user confirmation and degrades to a graceful skip when you can't push.

To send improvements upstream without push rights: fork `sthe0/claude-agent-instructions`, push to your fork, and open a PR. Full rule: [skills/self-improvement/policy.md](../../skills/self-improvement/policy.md) § Git sync.

## Troubleshooting a fresh machine

If `verify-layout-contract.sh` fails on a freshly pulled machine (stale directories or dangling symlinks from an old layout), see [docs/migrations/](../migrations/README.md) for per-refactor runbooks.

## See also

- [scripts/setup-symlinks.sh](../../scripts/setup-symlinks.sh) — the wiring script itself.
- [scripts/README.md](../../scripts/README.md) — the machine-checked inventory of every script the setup wires in.
- [Skills (symlinks)](../components/skills-symlinks.md) — how the runtime skills symlink tree is laid out.
- [git-workflow.md](git-workflow.md) — pulling updates and the push confirmation rule.
- [guards.md](guards.md) — the verification-guard suite run after setup and in CI.
- [layer-maintenance.md](layer-maintenance.md) — keeping a Team or Personal layer current as Core evolves.
- [docs/migrations/README.md](../migrations/README.md) — per-refactor migration runbooks.
