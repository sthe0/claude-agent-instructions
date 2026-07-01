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

`setup-symlinks.sh` is the single wiring command. It creates the symlinks under the isolated system root (`$CLAUDE_AGENT_HOME`, default `~/.claude-agent`) and `~/.cursor/`, merges the policy settings, and installs the reminder, engine-gate, and git hooks (via `apply-settings.sh`, `install-reminder-hooks.sh`, and `install-git-hooks.sh`). Re-run it whenever the repo layout changes.

`verify-instructions-sync.sh` confirms symlink integrity from the repo-developer's perspective.

`doctor.sh` confirms the runtime is actually ready: the `claude` CLI is on PATH, the constitution is loaded, the engine hooks are armed, and `agentctl` runs. Fix any `[FAIL]` line (usually by re-running `setup-symlinks.sh`) before the first task.

## Isolated config root

The system installs into and runs from its **own config root**, separate from your personal Claude config, so installing it never clobbers a pre-existing `~/.claude` and your personal settings never interfere with the system:

- **`CLAUDE_AGENT_HOME`** — the system root, `~/.claude-agent` by default; override the variable to relocate it. Single source of truth: `scripts/lib/config-root.sh`, sourced by every setup script and launcher.
- **Bare `claude`** uses your personal `~/.claude` and is never modified — install, re-run, and self-improvement edits all land under `~/.claude-agent`.
- **`claude-task` / `claude-agent`** run the system: the launchers export `CLAUDE_CONFIG_DIR=$CLAUDE_AGENT_HOME` around the `claude` exec (scoped to that process via `env`, so it never leaks into your interactive shell). `claude-agent` is a plain launch on the system root for scripted `-p`/`-c` use; `claude-task` adds workspace management.

### One-time login (auth is per-root)

`CLAUDE_CONFIG_DIR` relocates the entire config root, and **auth is per-root** — a fresh `~/.claude-agent` starts unauthenticated. By design **no credential is copied** out of `~/.claude` and no API key is written anywhere (an API key would lose the claude.ai subscription tier); instead you log the system root in once:

```bash
CLAUDE_CONFIG_DIR=~/.claude-agent claude auth login   # or: claude-agent /login
```

The token then lives in your OS keychain (encrypted). `setup-symlinks.sh` and `doctor.sh` print this command when the system root isn't logged in yet.

### Migrating an older in-place install

Machines set up before isolation have the system symlinks directly in `~/.claude`. Move them into the isolated root with:

```bash
scripts/migrate-to-isolated.sh            # preview — lists what would move, changes nothing
scripts/migrate-to-isolated.sh --apply    # back up, then move
```

It relocates **only** system-owned entries (repo-pointing symlinks + `agent-identity.local`), leaves your personal files and the merged `settings.json` in place, backs up first, and is idempotent. `doctor.sh` warns while a legacy in-place layout is detected.

## Symlinks wired by `setup-symlinks.sh`

`$AGENT` below is `$CLAUDE_AGENT_HOME` (default `~/.claude-agent`).

| In repo | Runtime path |
|---|---|
| `CLAUDE.md` | `$AGENT/CLAUDE.md` |
| `agents/*.md` | `$AGENT/agents/<name>.md` |
| `skills/<name>/` | `$AGENT/skills/<name>/` |
| `memory-global/` | `$AGENT/memory-global/` |
| `cursor/rules/claude-code-sync.mdc` | `~/.cursor/rules/claude-code-sync.mdc` |
| `cursor/agents/*.md` | `~/.cursor/agents/<name>.md` |

Project-specific Cursor rules live in the project's own `<project>/.claude/rules/` tree and are wired to `<project>/.cursor/rules/` by the project's own setup script.

## Per-project local setup

Each product repo has its own `.claude/scripts/setup-local.sh`. Run it from the project root after the global setup above. It calls the global `scripts/setup-project-memory.sh` where applicable and creates any project-level Cursor symlinks.

## Multi-project workspaces (optional)

When you maintain agent configs for several projects, you can group them in a single **workspace storage tree** layered *outside* Core. A workspace holds a shared layer (hooks, org rules, shared permissions, scaffolding scripts) plus one thin tree per project; each project's tree carries its own `<project>/.claude/` (a `CLAUDE.md` contract, `agent-memory/`, and an optional project `setup-local.sh`). At a working mount, the project's `.claude/` is **composed** on top of Core, so the effective agent is *Core ⊕ workspace-shared ⊕ project*. Adding a project is one scaffolding step from the shared layer's template.

A workspace is self-describing: it ships its **own onboarding README that slots under the Core quick-start** — bring up Core first (the setup above), then run the workspace's onboarding to mount its storage and wire each project. Core stays unaware of any particular workspace; this is purely an additive composition pattern on top of the root.

## Distribution — no push rights required

You do **not** need push rights to use the system. Instructions are read locally from the clone. Self-improvement edits land as local commits; the upstream push is gated behind explicit user confirmation and degrades to a graceful skip when you can't push.

To send improvements upstream without push rights: fork `sthe0/claude-agent-instructions`, push to your fork, and open a PR. Full rule: [skills/self-improvement/policy.md](../../skills/self-improvement/policy.md) § Git sync.

## Starting a task with `claude-task`

Once setup is complete, the fastest way to start working on a tracker issue is:

```
claude-task DEEPAGENT-123        # resolve issue → isolated git worktree → launch claude
claude-team DEEPAGENT-123        # same, using the "team" auth profile
claude-task --init <name>        # create a NEW local git repo, register it, then enter
```

The wrapper resolves the issue, creates an isolated working copy (`git worktree` by default), `cd`s into it, and launches `claude`. The bare `cd <dir> && claude` flow still works and is not replaced.

`--init <name>` creates a brand-new local git repository (git init + project memory scaffold + initial commit), registers it in the project registry, and enters it. The target directory defaults to `$PWD/<name>`; override the base with `CLAUDE_PROJECT_INIT_BASE=/some/dir`. You may also pass an absolute path or a path containing a slash as the name argument (e.g. `--init /path/to/myproject` or `--init projects/foo`) — in that case the path is used verbatim as the target and the last component becomes the project name.

`claude-task` selects backends automatically. To override, add these keys to `$CLAUDE_AGENT_HOME/agent-identity.local` (default `~/.claude-agent/agent-identity.local`):
- `project_backend` — workspace isolation mechanism (`git` by default; `arc` where `ya`+`arc` are present)
- `tracker_backend` — issue source (`github` by default)

Core ships the `git`/`github` defaults and the `default` auth profile; specialized backends install automatically from workspace storage. See [org-portability.md](org-portability.md) for the opt-in surface.

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
