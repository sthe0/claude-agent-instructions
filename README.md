# Claude / Cursor agent instructions

This repository turns a stock **Claude Code** (and, through a thin mirror, **Cursor**) into a disciplined **universal manager-actor**: one agent that takes any task and drives it to a verified result — planning, delegating to specialists, checking its own work, and accumulating reusable experience as it goes. The instructions, skills, coordination engine, and memory in this repo are what impose that discipline.

The repo is the single source of truth for both tools. Edits appear at runtime via symlinks under `~/.claude/` and `~/.cursor/`. The canonical instruction file for both is the same [CLAUDE.md](CLAUDE.md); the Cursor rule (`cursor/rules/claude-code-sync.mdc`) is a thin mirror for the things Cursor cannot do natively (no Skill tool, no auto-memory writes).

This README is the minimal entry point — what the system is, the core mental model, and how to start. Everything below that lives in [docs/](docs/README.md), the full documentation tree organized general → specific.

## Core concepts

The whole system rests on four ideas. Everything else — every rule, skill, hook, and memory file — exists to serve one of them.

1. **Difficulty** — the foundational object: *a divergence between a desired state and the actual state.* The agent's universal job is to remove difficulties; every rule and component has a **functional ground**, the specific difficulty it removes. Full concept: [docs/concepts/difficulty.md](docs/concepts/difficulty.md).

2. **Task** — the form every action takes. Removing a difficulty is framed as a task, classified by weight — **chat**, **small change**, **substantive** — with routing following from the class. Full concept: [docs/concepts/task.md](docs/concepts/task.md).

3. **Universal manager-actor** — the single executor. The main Claude Code dialog *is* the manager; it resolves a task itself when small, or coordinates specialists when large. One disciplined actor wearing different hats, not a swarm. Full concept: [docs/concepts/manager-actor.md](docs/concepts/manager-actor.md).

4. **Memory** — the means of **accumulating experience in overcoming difficulties.** It closes the learning loop *difficulty → overcame it → recorded how → reused it next time.* Full concept: [docs/concepts/memory-model.md](docs/concepts/memory-model.md).

### Root + projects

The instructions are layered by scope. The **root** (this repo) defines the **universal** properties of the manager-actor — the ones that hold for every task on the machine. A **project** adds its own properties on top of the root, so the effective agent is *root ⊕ project-specifics*. Project-specific runbooks, memory, and skills live in each project's own `<project>/.claude/` tree (committed to that project's git), never in this root repo.

Several projects can be grouped into a shared **workspace** (a storage tree outside Core, with its own onboarding) that composes on top of the root — see [Multi-project workspaces](docs/operations/setup.md#multi-project-workspaces-optional).

## Documentation map

[docs/README.md](docs/README.md) is **the** documentation index — read it top-to-bottom for a guided path, or jump to the section you need.

| Section | What it covers |
|---|---|
| [Concepts](docs/concepts/) | The four foundational ideas above, in full. |
| [Architecture](docs/architecture/) | The layered build, the coordination engine and its state machine, and the consensus architecture for distributing instructions across a team. |
| [Processes](docs/processes/) | The task lifecycle and its sub-processes: planning, difficulty/replan, self-improvement, resolution, partition. |
| [Components](docs/components/) | The parts and where they live: skills, agents, hooks, scripts, memory scopes, settings, the Cursor mirror. |
| [Operations](docs/operations/) | Running and maintaining the repo: setup and distribution, the git workflow, the verification guards, layer maintenance. |
| [Decisions](docs/adr/README.md) | The architecture decision records for significant, hard-to-reverse choices. |

## Setup

### Fastest path — one command

```bash
git clone git@github.com:sthe0/claude-agent-instructions.git ~/claude-agent-instructions
echo 'source ~/claude-agent-instructions/scripts/claude-launchers.sh' >> ~/.bashrc && source ~/.bashrc
onboard   # setup-symlinks + readiness check + any machine-local onboard.d/*.sh hooks
```

Core already cloned: just `onboard`. On a machine with org-specific workspace storage,
a machine-local `onboard.d/` hook mounts storage and wires project configs automatically
(see [Multi-project workspaces](docs/operations/setup.md#multi-project-workspaces-optional)).

After setup, `claude-task` self-initializes: it probes `onboard.d/` hooks with
`--needs-init` and runs `onboard` automatically when initialization is needed (e.g.
after a reboot that dropped a storage mount).

### Equivalent steps

```bash
git clone git@github.com:sthe0/claude-agent-instructions.git ~/claude-agent-instructions
# …no SSH key or no push access? a read-only HTTPS clone works just as well:
#   git clone https://github.com/sthe0/claude-agent-instructions.git ~/claude-agent-instructions
~/claude-agent-instructions/scripts/setup-symlinks.sh        # symlinks + settings + reminder/git hooks (one command does all)
~/claude-agent-instructions/scripts/doctor.sh                # "am I ready to start?" — run this once, expect all [ OK ]
```

`setup-symlinks.sh` is the single wiring command: it lays the symlinks, merges the policy settings, and installs the reminder + engine-gate hooks and the git hooks. `doctor.sh` then confirms the runtime is ready — fix any `[FAIL]` line (usually by re-running `setup-symlinks.sh`) before your first task.

You do **not** need push rights to use the system — it runs fully from a read-only clone; self-improvement edits land as local commits and the upstream push is always gated behind your explicit confirmation. The full procedure (symlink table, per-machine settings merge, per-project local setup, and how the root and project trees compose) is in [docs/operations/setup.md](docs/operations/setup.md).

### Using this in another organization

Core is org-neutral by default — only **internal-only** Yandex facilities (Arcadia `arc`, Startrek) are couplings, and they are **opt-in**, not assumed. Publicly-reachable services stay available (e.g. `yandex-cloud-expert`, since `yandex.cloud` is public). Onboarding in a non-Yandex org is three commands:

```bash
~/claude-agent-instructions/scripts/setup-symlinks.sh   # symlinks + settings + hooks
~/claude-agent-instructions/scripts/setup-org.sh        # detects the difficulty channel (→ github off-corp), writes per-machine identity
~/claude-agent-instructions/scripts/doctor.sh           # expect all [ OK ]
```

Git itself needs no special setup — Claude uses `git`/`gh` natively. Org-specific runbooks go in each project's `<project>/.claude/`, never in Core. The opt-in surface and what stays Yandex-flavored (and why it's harmless) are in [docs/operations/org-portability.md](docs/operations/org-portability.md).

## Getting started — your first task

Once `doctor.sh` shows all `[ OK ]`, start a task the same way every time: open `claude` in the directory you want to work in and describe what you need in plain language (English or Russian — the agent replies in the language you write). You do **not** invoke the engine, pick a skill, or write a plan by hand — a hook arms the coordination engine on your first message and the agent routes the work itself.

```
cd ~/my-project && claude
> Add retry-with-backoff to the HTTP client and cover it with a test.
```

For anything beyond a quick question or a small one-file change, the agent classifies the task as *substantive*, writes a plan, and **stops at the approval gate** — it shows you the plan and changes nothing in your code until you approve. After approval it executes, verifies, and asks you to confirm the task is resolved. Use `/clear` between unrelated tasks.

Mention a ticket key (e.g. `ABC-123`) anywhere in your request and the `tracker-management` skill loads the ticket's context and posts progress back. A ticket task always goes through the plan → approval → execution spine. The full walkthrough of what happens end to end is the task lifecycle in [docs/processes/task-lifecycle.md](docs/processes/task-lifecycle.md).

### Task-entry wrappers (optional)

Instead of `cd`-ing into a working copy by hand, `claude-task` does the whole entry in one step — resolves the issue, makes an isolated working copy, `cd`s into it, and launches `claude`:

```
claude-task DEEPAGENT-123        # resolve a tracker issue → isolated git worktree + launch
claude-team DEEPAGENT-123        # same, on the "team" auth profile
claude-task --new "title"        # create an issue, then enter
claude-task <name>               # named scratch workspace (no tracker)
```

The bare `cd ~/my-project && claude` flow still works exactly as before — `claude-task` is an optional shortcut.

It selects a **workspace** backend (a `git` worktree by default; an `arc` mount where `ya`+`arc` are present) and a **tracker** backend (GitHub Issues by default) — auto-detected and overridable via the `project_backend` / `tracker_backend` keys in `~/.claude/agent-identity.local`. Auth variants `claude-<profile>` (e.g. `claude-team`, `claude-personal`) are the same entry on a machine-local auth profile. Core ships only the `git`/`github` defaults and the `default` profile; specialized backends install from workspace storage — see [docs/operations/org-portability.md](docs/operations/org-portability.md).

## Pointers

- **Skills** — a packaged procedure or role under `skills/`. Flat skills run inline; specializations are spawned per plan step. The full, machine-checked inventory of both kinds lives in [docs/components/skills.md](docs/components/skills.md).
- **Agents** — the specialization roles the manager delegates to, and how each is spawned: [docs/components/agents.md](docs/components/agents.md). No shipped Task-spawned subagents currently; machine-local ones go in `agents-local/`, project-local ones in `<project_cwd>/.claude/agents/`.
- **Git workflow** — pull before editing, commit after a change, push only after explicit confirmation: [docs/operations/git-workflow.md](docs/operations/git-workflow.md).

### Not in this repository

| What | Where |
|---|---|
| Project memory & runbooks | `<project_cwd>/.claude/agent-memory/` (project's git) |
| Extra agents | `~/.claude/agents/` |
| Local scripts | `~/.claude/scripts-local/` |
| Local skills | `~/.claude/skills/` (single-file `skills-local/*.md`, gitignored fallback) |
