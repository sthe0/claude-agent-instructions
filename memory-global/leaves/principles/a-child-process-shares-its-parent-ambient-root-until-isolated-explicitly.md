---
name: a-child-process-shares-its-parent-ambient-root-until-isolated-explicitly
description: A child process or installed tool that runs with the ambient environment of its parent re-triggers the parent's machinery or collides with the parent's config; isolating by relocating its root works only if what the old root carried in files (credentials, model selection) is supplied to the new one.
type: reference
schema: principle/v1
generality: 2
induced_from: [2026-08-25-unisolated-judge-subprocess-recursion, 2026-07-01-isolate-tool-config-root-per-root-auth]
created: 2026-10-21
last_verified: 2026-10-21
---

# A child process shares its parent ambient root until isolated explicitly

## Principle

When a process is launched by an agent or installed beside a personal setup, decide its root explicitly: relocate it with the tool's own root variable from one single-source seam, then enumerate what the old root held in files and re-supply it; otherwise the unisolated child recurses or clobbers, and the isolated one silently loses its login. Pair the isolation with an alarm, because a caller that tolerates failure shows neither outcome.

## Generality

Level 2 - a rule for any child process or co-installed tool that reads configuration from an ambient location.

## Induced from

- [[2026-08-25-unisolated-judge-subprocess-recursion]] - single-turn judge children loaded the fleet's own hooks and recursed; isolating the config root then severed their file-carried login.
- [[2026-07-01-isolate-tool-config-root-per-root-auth]] - an install into the shared config dir clobbered a personal config; the fix was a separate root, with authentication being per-root.

## Refutation

Refuted by a case where an unisolated child sharing the parent's root caused neither recursion nor collision, or where relocating a root lost nothing the old root carried. Weak on count (two sources); a third independent isolation case would settle it.
