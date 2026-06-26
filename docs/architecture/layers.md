# The seven-layer model

> The canonical overview of how this repo is organized into layers — each higher layer constrains or drives the one below it, and together they make the manager-actor disciplined rather than ad-hoc.

The repository is built as **seven layers, numbered 0–6**. Layer 0 is the runtime the system stands on; each layer above adds a band of structure that the layers beneath it obey. Reading them bottom-to-top is reading the system from "what it runs on" up to "how it is distributed to many machines".

## The layers

| Layer | What it is | Role |
|---|---|---|
| **0 — Substrate** | The Claude Code CLI itself: the main dialog, the `Skill` tool, the `Task` subagent mechanism, hooks, auto-memory, and `claude -p` for spawning a fresh process | The runtime everything else runs on. Layer 0 is given — the rest of the repo is what disciplines it. |
| **1 — Instruction surface** | [CLAUDE.md](../../CLAUDE.md) (the constitution, loaded every session), [config.md](../../config.md) (numeric constants, single source of truth), and `memory-global/` (imported via `@`) | What the agent reads at the start of every session. |
| **2 — Skills** | Flat skills, run inline, and specializations, spawned as separate processes | Packaged procedures and the roles the actor wears. |
| **3 — Coordination engine** | `scripts/agentctl/` — a code state machine — see [the coordination engine](coordination-engine.md) | Deterministic control-flow for substantive tasks. |
| **4 — Hooks** | `scripts/hook-*.py` | Enforce the non-skippable gates and the reminders deterministically — a hook can deny a tool call. |
| **5 — Memory** | Personal auto-memory, global engineering memory (`memory-global/`), and project memory (`<project>/.claude/agent-memory/`) — see [memory model](../concepts/memory-model.md) | Accumulated experience and durable facts. |
| **6 — Distribution** | `setup-symlinks.sh`, the Cursor mirror, the `verify-*.py` / `lint-*.py` guard suite, and the git hooks | Wires the repo into `~/.claude/` and `~/.cursor/` and keeps the runtime consistent with the source of truth. |

## How the layers compose

The dependency runs downward: the **instruction surface** (Layer 1) tells the actor which **skills** (Layer 2) to invoke; the **coordination engine** (Layer 3) sequences a substantive task and is policed by the **hooks** (Layer 4); every layer reads from and writes to **memory** (Layer 5); and the **distribution** layer (Layer 6) is what makes the whole stack appear at runtime under `~/.claude/` and stay verifiably in sync with the repo.

The seven-layer split is itself an instance of the system's own discipline: each layer has a single functional ground (the difficulty it removes) and one canonical home. The layers describe the *single-machine* shape of the system. How that shape is shared across many developers — one evolving Core with per-developer overrides on top — is a separate axis, described in [the consensus architecture](consensus-architecture.md).

## See also

- [The coordination engine and its state machine](coordination-engine.md) — Layer 3 in depth.
- [The consensus architecture](consensus-architecture.md) — how the layers are distributed across a team.
- [Memory model](../concepts/memory-model.md) — Layer 5, the three scopes.
