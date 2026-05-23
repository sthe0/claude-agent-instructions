# Agents (`agents/`)

Reserved for future subagent definitions invoked via the `Task` tool.

The current architecture is **manager + skills** — there are no shipped subagents. Specialist work (planning, development, reasoning verification, domain consultation) is implemented as **specialization skills** under [`skills/specializations/`](../skills/specializations/), spawned by the manager as separate `claude -p` processes (see [CLAUDE.md](../CLAUDE.md) § Spawning specialists).

If a future need calls for a true `Task`-spawned subagent (one-shot, isolated, fan-out research, etc.), add the agent definition here. `setup-symlinks.sh` already iterates this directory.

Machine-local subagent definitions go in [`agents-local/`](../agents-local/) (gitignored fallback) or under the project's own `.claude/agents/` tree.
