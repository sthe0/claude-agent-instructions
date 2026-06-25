# Claude / Cursor agent instructions

This repository turns a stock **Claude Code** (and, through a thin mirror, **Cursor**) into a disciplined **universal manager-actor**: one agent that takes any task and drives it to a verified result — planning, delegating to specialists, checking its own work, and accumulating reusable experience as it goes. The instructions, skills, coordination engine, and memory in this repo are what impose that discipline.

If you just opened this repo and know nothing about it, read the next two sections first — the **core concepts** and the **Claude Code substrate** — then the **architecture** and the **end-to-end walkthrough**. Setup and maintenance are at the bottom.

The repo is the single source of truth for both tools. Edits appear at runtime via symlinks under `~/.claude/` and `~/.cursor/`. The canonical instruction file for both is the same [CLAUDE.md](CLAUDE.md); the Cursor rule (`cursor/rules/claude-code-sync.mdc`) is a thin mirror for the things Cursor cannot do natively (no `Skill` tool, no auto-memory writes).

## Core concepts

The whole system rests on four ideas. Everything else — every rule, skill, hook, and memory file — exists to serve one of them.

1. **Difficulty** — the foundational object: *a divergence between a desired state and the actual state.* The agent's universal job is to remove difficulties. Every rule and component has a **functional ground**: the specific difficulty it removes. If you cannot name the difficulty a piece of this repo removes, treat that as a signal it is noise.

2. **Task** — the form every action takes. Removing a difficulty is framed as a task; all work the system does is tasks. Tasks are classified by weight — **chat** (answer in-thread), **small change** (do it directly), **substantive** (full coordination cycle) — and routing follows from the class.

3. **Universal manager-actor** — the single executor. The main Claude Code dialog *is* the manager; there is no separate manager bot. It resolves a task itself when small, or coordinates specialists (planner, developer, reviewer, …) when large. It is one disciplined actor wearing different hats, not a swarm of disconnected agents.

4. **Memory** — the means of **accumulating experience in overcoming difficulties.** It is not just a fact store: it closes the learning loop *difficulty → overcame it → recorded how → reused it next time.* See § Memory.

### Root + projects

The instructions are layered by scope. The **root** (this repo) defines the **universal** properties of the manager-actor — the ones that hold for every task on the machine. A **project** adds its own properties **on top of** the root; there can be **many projects**, and each one is *root ⊕ project-specific*:

```text
effective agent = root (universal)  ⊕  project A specifics
                                    ⊕  project B specifics
                                    ⊕  …
```

Project-specific runbooks, memory, and skills live in each project's own `<project>/.claude/` tree (committed to that project's git), never in this root repo. The root never embeds project knowledge.

## The Claude Code substrate

The system is built on primitives that Claude Code provides. The terms below recur throughout this repo:

- **Main dialog** — the running Claude Code conversation. It is the manager-actor.
- **`Skill` tool / `/<name>`** — invokes a *skill* (a packaged procedure) **inline**, in the current process, with full context.
- **`claude -p`** — spawns a *fresh* Claude Code process with its own context and budget. Used to run a specialist in isolation.
- **Subagent (`Task`)** — a one-shot worker spawned by the harness; cheap models handle retrieval / monitoring so the main thread stays lean.
- **Hook** — a script the harness runs on an event (prompt submit, before a tool call, on write). Hooks enforce gates deterministically — they can *deny* a tool call.
- **Auto-memory** — Claude Code's built-in mechanism that reads/writes Markdown memory files automatically across sessions.

## Architecture in layers

The repo is organized as seven layers. Each higher layer constrains or drives the one below it; together they make the manager-actor disciplined rather than ad-hoc.

| Layer | What | Role |
|---|---|---|
| **0 — Substrate** | Claude Code CLI (main dialog, `Skill`, `Task`, hooks, auto-memory, `claude -p`) | The runtime the system runs on. |
| **1 — Instruction surface** | [CLAUDE.md](CLAUDE.md) (the constitution, loaded every session), [config.md](config.md) (numeric constants, single source), `memory-global/` (imported via `@`) | What the agent reads at session start. |
| **2 — Skills** | Flat skills (inline) + specializations (spawned) — see § Skills | Packaged procedures and roles. |
| **3 — Coordination engine** | [`scripts/agentctl/`](scripts/agentctl/) — a code state machine | Deterministic control-flow for substantive tasks. |
| **4 — Hooks** | `scripts/hook-*.py` | Enforce the non-skippable gates and reminders. |
| **5 — Memory** | personal auto-memory, global engineering (`memory-global/`), project (`<project>/.claude/agent-memory/`) | Accumulated experience and durable facts. |
| **6 — Distribution** | `setup-symlinks.sh`, the Cursor mirror, `verify-*.py` / `lint-*.py`, githooks | Wires the repo into `~/.claude/` and keeps it consistent. |

### The coordination engine and its state machine

`agentctl` (Layer 3) owns the **deterministic control-flow** of a substantive task, while **prose supplies the cognition** at each step (the classification judgment, the plan content, the marker handling). The canon: *code = deterministic control-flow, prose = cognition.*

```bash
cd scripts && PYTHONPATH=scripts python3 -m agentctl <cmd>
# start → classify → plan → submit-plan → approve → next-stage → dispatch → record-result → verify-final → resolve
```

The state machine a substantive task moves through:

```text
start → CLASSIFIED → ROUTED → PLANNING → PLAN_READY ──■APPROVAL GATE■──→ APPROVED
                       │                                                    │
        small change ──┘                                              PARTITIONED
                       │                                                    │
                       └──────────────────────────────→  EXECUTING  ⇄  VERIFYING
                                                              │             │
       (stage FAILED → DIAGNOSING: declare→investigate→critique → replan ──┘
                          → retry, or PLANNING on a substantive replan)      │
                                                                       RESOLUTION
                                                                            │
                                                            ──■RESOLUTION GATE■──→ RESOLVED
```

The two gates (`■`) are **non-skippable**, enforced by guardian hooks ([hook-state-gate.py](scripts/hook-state-gate.py) hard-denies production edits until the engine reaches an execution node; the resolution gate requires explicit user confirmation). [verify-agentctl.py](scripts/verify-agentctl.py) checks that every gate has its guardian hook and that the schema, transitions, and cognitive leaves stay consistent. State lives at `~/.claude/agentctl/state/<session_id>.json`. Engine modules under `scripts/agentctl/`: `classify`, `config`, `state`, `store`, `machine`, `gates`, `directive`, `cli`, `dispatch`, `partition`, `permissions`, `plan`, `continuations`. The engine has its own README — [scripts/agentctl/README.md](scripts/agentctl/README.md) — covering the command sequence, the `gates.py` purity invariant, and the state file; and the typed **plan model** it builds (the 8-element activity ontology) is documented in [plan-activity-ontology.md](memory-global/leaves/plan-activity-ontology.md).

If the engine is unavailable, the manager walks the same steps by hand in the same order — the engine automates the spine, it does not replace the cognition.

## A task, end to end

What happens when you give the agent a substantive task:

1. **Prompt submitted** → a hook auto-arms the engine (`agentctl start`).
2. **Classify** the task weight (chat / small change / substantive) and **route** accordingly.
3. **Plan** — for substantive work, the agent (often via the `planner` skill) writes a plan with stages, each carrying an *expected result image* and a *done criterion*.
4. **Approval gate** — the plan is shown to you; nothing touches production code until you approve. The `hook-state-gate` enforces this.
5. **Execute** — the manager runs each stage itself (small steps) or **dispatches** a `developer` / other specialist (larger steps).
6. **Difficulty?** If a stage's actual result diverges from its expected image, a FAILED result routes the engine into the `DIAGNOSING` sub-spine: the `overcome-difficulty` skill supplies the cognition while the engine enforces `declare → investigate → critique` and **blocks `replan` until that record is complete**. The replanning task fixes the plan and work resumes.
7. **Verify** each stage against its done criterion, then the whole plan against the overall criterion.
8. **Resolution gate** — the agent recaps and **asks you to confirm** the task is resolved (it does not assume it from silence or thanks).
9. **Record experience** — if the task taught something reusable, the agent writes an experience leaf to memory.

## Memory — accumulating experience

Memory is how the system gets better at removing difficulties over time. Three scopes, picked by purpose (write to the **most specific** one):

| Scope | Where | Purpose |
|---|---|---|
| **Personal (auto-memory)** | `~/.claude/projects/<cwd-hash>/memory/` | User facts, preferences, conversational continuity. |
| **Global engineering** | `memory-global/` (imported into every session) | Cross-project patterns, runbooks, retrospectives. |
| **Project** | `<project>/.claude/agent-memory/` (project's git) | Project-specific runbooks; shared on clone. |

Each scope is a short `MEMORY.md` index plus `leaves/` detail files. The key kind is the **experience leaf** (`difficulty/v1` schema): one recurring *difficulty*, every context it arose in, and the plan that removed it each time. When a task resolves and clears the quality bar, the agent searches existing leaves and **extends** a matching one or creates a new one — so the next similar task starts from accumulated experience, not from scratch.

## Setup

```bash
git clone git@github.com:sthe0/claude-agent-instructions.git ~/claude-agent-instructions
# …no SSH key or no push access? a read-only HTTPS clone works just as well:
#   git clone https://github.com/sthe0/claude-agent-instructions.git ~/claude-agent-instructions
~/claude-agent-instructions/scripts/setup-symlinks.sh        # symlinks + settings + reminder/git hooks (one command does all)
~/claude-agent-instructions/scripts/verify-instructions-sync.sh   # symlink integrity (repo-developer's view)
~/claude-agent-instructions/scripts/doctor.sh                # "am I ready to start?" — run this once, expect all [ OK ]
```

`setup-symlinks.sh` is the single wiring command: besides the symlinks below it merges the policy `settings` and installs the reminder + engine-gate hooks and the git hooks (it calls `apply-settings.sh`, `install-reminder-hooks.sh`, and `install-git-hooks.sh` for you). `doctor.sh` then confirms the runtime is actually ready — the `claude` CLI is on PATH, the constitution is loaded, the engine hooks are armed, and `agentctl` runs; fix any `[FAIL]` line (usually by re-running `setup-symlinks.sh`) before your first task.

You do **not** need push rights to this repo to use the system — it reads the instructions locally and runs fully from a read-only clone. Self-improvement edits land as local commits; the upstream push is always gated behind your explicit confirmation and degrades to a graceful skip when you can't push (your commit stays local, just like Tracker updates are skipped without tracker credentials). To send improvements upstream without push rights, fork `sthe0/claude-agent-instructions`, push to your fork, and open a PR. Full rule: [skills/self-improvement/policy.md](skills/self-improvement/policy.md) § Git sync.

Per-project local setup (from each product repo root; scripts live in that repo's `.claude/scripts/`):

```bash
cd ~/arcadia/robot/deepagent && .claude/scripts/setup-local.sh   # deepagent (Arc)
cd ~/arcadia/logos && .claude/scripts/setup-local.sh             # logos (local only)
```

`setup-local.sh` calls global `setup-project-memory.sh` where applicable and creates Cursor symlinks. See each project's `.claude/scripts/README.md`.

If `verify-layout-contract.sh` fails on a freshly pulled machine (stale directories / dangling symlinks from an old layout), see [docs/migrations/](docs/migrations/README.md) for per-refactor runbooks.

### Symlinks (global from git)

| In repo | Runtime |
|---|---|
| `CLAUDE.md` | `~/.claude/CLAUDE.md` |
| `agents/*.md` | `~/.claude/agents/<name>.md` |
| `skills/<name>/` | `~/.claude/skills/<name>/` |
| `memory-global/` | `~/.claude/memory-global/` |
| `cursor/rules/claude-code-sync.mdc` | `~/.cursor/rules/claude-code-sync.mdc` |
| `cursor/agents/*.md` | `~/.cursor/agents/<name>.md` |

Project-specific Cursor rules live in the project's own `<project>/.claude/rules/` tree (committed in the project's git) and are wired to `<project>/.cursor/rules/` by the project's setup. The deepagent case is automated by `setup-symlinks.sh` when `~/arcadia/robot/deepagent/.claude/rules/` is present. Cursor-only assets live in [`cursor/`](cursor/README.md), isolated from `~/.claude/agents`.

### Scripts

Full inventory (machine-checked against the filesystem by [verify-readme.py](scripts/verify-readme.py)) lives in [scripts/README.md](scripts/README.md).

## Getting started — your first task

Once `doctor.sh` shows all `[ OK ]`, you start a task the same way every time: open `claude` in the directory you want to work in and describe what you need in plain language (English or Russian — the agent replies in the language you write). You do **not** invoke the engine, pick a skill, or write a plan by hand — a hook arms the coordination engine on your first message and the agent routes the work itself. The conceptual walkthrough of what then happens is [§ A task, end to end](#a-task-end-to-end) above; the two flows below are what you actually do.

**Without a ticket — a regular task.** Just describe the task:

```
cd ~/my-project && claude
> Add retry-with-backoff to the HTTP client and cover it with a test.
```

You can also hand the agent the whole bootstrap in one message — point it at this repository, let it set itself up per this README, have it make sure the project you'll work in is checked out and available (and `cd` into it), then describe an arbitrary task in your own words:

```
cd ~/work && claude
> Look at this git repository (~/claude-agent-instructions) — it has a README; set yourself up according to it. Then make sure the order-service project is checked out and start there: add a Prometheus /metrics endpoint with request-latency histograms, and cover it with tests.
```

For anything beyond a quick question or a ≤20-line one-file change, the agent classifies the task as *substantive*, writes a plan, and **stops at the approval gate** — it shows you the plan and changes nothing in your code until you approve (you click `Approve`, or ask for changes). After approval it executes, verifies, and asks you to confirm the task is resolved. Use `/clear` between unrelated tasks so each starts with clean context.

**With a ticket.** Mention the ticket key (e.g. `ABC-123`) anywhere in your request:

```
cd ~/my-project && claude
> Implement ABC-123.
```

On a fresh machine you can even hand the agent the whole bootstrap in one message — point it at this repository, let it set itself up per this README, have it check out the right project and start there, and name the ticket you want to work on:

```
cd ~/work && claude
> Look at this git repository (~/claude-agent-instructions) — it has a README; set yourself up according to it. Then make sure the right project is checked out and start there: I want to work on MYTICKETQUEUE-123.
```

The key triggers the `tracker-management` skill, which loads the ticket's context, and — for a substantive ticket — publishes the plan and posts progress back to the ticket. A ticket task always goes through the plan → approval → execution spine (no in-thread shortcut), so you still approve the plan before any code changes. Posting back to a tracker needs that tracker's credentials configured on your machine; without them the work still proceeds locally, only the ticket updates are skipped — see the `tracker-management` skill for the credential setup.

## Skills

A **skill** is a packaged procedure or role under `skills/`. Flat skills run inline; specializations are spawned as separate processes.

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
| `code-reviewer` | Maintainability / readability / reusability review of a diff (self-review or independent) | [skills/specializations/code-reviewer/SKILL.md](skills/specializations/code-reviewer/SKILL.md) |
| `developer` | Writing, refactoring, debugging, reviewing production code | [skills/specializations/developer/SKILL.md](skills/specializations/developer/SKILL.md) |
| `planner` | Decomposition, stages, dependencies, risks, done criteria | [skills/specializations/planner/SKILL.md](skills/specializations/planner/SKILL.md) |
| `tech-writer` | Russian README / documentation authoring; polishing plans & long comments | [skills/specializations/tech-writer/SKILL.md](skills/specializations/tech-writer/SKILL.md) |
| `thinker` | Independent reasoning check on a non-trivial chain | [skills/specializations/thinker/SKILL.md](skills/specializations/thinker/SKILL.md) |
| `yandex-cloud-expert` | Yandex Cloud / `yc` operations | [skills/specializations/yandex-cloud-expert/SKILL.md](skills/specializations/yandex-cloud-expert/SKILL.md) |
<!-- inventory:specializations:end -->

Full spawn template and return-marker handling: [CLAUDE.md](CLAUDE.md) § Spawning specialists.

### Agents (`agents/`)

None currently. The directory exists with a [README](agents/README.md) describing what it is reserved for; `setup-symlinks.sh` iterates it so future agents are picked up automatically. Machine-local subagents (gitignored) → [`agents-local/`](agents-local/README.md). Project-local subagents → `<project_cwd>/.claude/agents/`.

## Not in this repository

| What | Where |
|---|---|
| Project memory & runbooks | `<project_cwd>/.claude/agent-memory/` (project's git) |
| Extra agents | `~/.claude/agents/` |
| Local scripts | `~/.claude/scripts-local/` |
| Local skills | `~/.claude/skills/` (single-file `skills-local/*.md`, gitignored fallback) |

## Git workflow

```bash
~/claude-agent-instructions/scripts/sync-instructions-repo.sh pull
# edits → commit → push after user confirms
```

`push` to the remote happens **only after explicit user confirmation.** Runbook: [skills/self-improvement/policy.md](skills/self-improvement/policy.md) § Git sync.

## Maintaining this README

When the cooperation model changes — update the affected sections here, [CLAUDE.md](CLAUDE.md), and affected `agents/*.md` or `skills/*/SKILL.md` in **one commit**.

When **directories, scripts, or symlinks** change:

1. Update [skills/self-improvement/policy.md](skills/self-improvement/policy.md) § File structure.
2. Align § Symlinks / § Scripts / § Skills with reality. The inventory sentinels (flat skills / specializations) are machine-checked — run `scripts/verify-readme.py --fix` to reconcile the row sets, then fill in any `TODO` purpose cells by hand.
3. Run `scripts/verify-layout-contract.sh`, `scripts/verify-instructions-sync.sh`, and `scripts/verify-readme.py`.
