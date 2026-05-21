# Global agent memory

Cross-project facts and **how to think / work**, not tied to one repository or Yandex product.

**Path:** `~/.claude/memory-global/` (symlink to this directory in `~/claude-agent-instructions`).

## Use

1. Start with **[INDEX.md](INDEX.md)**.
2. Read only relevant leaf files.
3. For machine- or product-specific runbooks → **`~/.claude/memory/INDEX.md`** (local tree; source path is machine setup, not this git repo).

## What belongs here

- Reasoning, decomposition, plan approval habits
- Generic development pitfalls and retest discipline
- Delegation and coordinator patterns (without Tracker/arc command details)
- Instruction-repo sync workflow (git, not arc)

## What does not

- Prod pipeline paths, Nirvana block names, YT table layouts → local memory
- Secrets, tokens, one-off session plans

## Freshness

Same rules as local memory: runbook leaves need `## Метаданные` with `last_verified`, `staleness_triggers`, `revalidate`. Agent **memory** maintains both trees.
