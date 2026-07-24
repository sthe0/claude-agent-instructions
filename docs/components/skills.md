# Skills

> The inventory of skills the agent can invoke: flat skills that run inline in the current process, and specializations that are spawned as separate processes per plan step.

A skill is a packaged procedure or role under [skills/](../../skills/). There are two kinds, split by how they run:

- **Flat skills** run **inline** in the current process via the Skill tool — they share the main dialog's full context. These are the user-invocable procedures and the reflexive ones the manager reaches for (overcome-difficulty, self-improvement, tracker-management).
- **Specialization skills** are **spawned** as separate `claude -p` processes, one per plan step, each with fresh context and its own budget. These are the roles the manager delegates to: planner, developer, code-reviewer, and the rest.

Both tables below are machine-checked against the filesystem by [verify-readme.py](../../scripts/verify-readme.py): every directory under `skills/` (flat) and `skills/specializations/` (specialization) must have a row, and no row may dangle. Run `scripts/verify-readme.py --fix` to reconcile the row sets, then fill any `TODO` purpose cells by hand. The spawn template and return-marker handling for specializations live in [CLAUDE.md](../../CLAUDE.md) and [spawning-specialists.md](../../memory-global/leaves/spawning-specialists.md).

## Flat skills (invoked inline in the current process)

<!-- inventory:skills:begin -->
| name | Triggers (summary) | File |
|---|---|---|
| `instruction-grooming` | An instruction file crosses the `lint-prose-length.py` 90% WARN threshold, flagged directly or via `hook-instruction-grooming-due.py` | [skills/instruction-grooming/SKILL.md](../../skills/instruction-grooming/SKILL.md) |
| `overcome-difficulty` | Reality diverges from the plan; verification failed; repeated error; missing observable | [skills/overcome-difficulty/SKILL.md](../../skills/overcome-difficulty/SKILL.md) |
| `self-improvement` | User correction or feedback about agent behavior | [skills/self-improvement/SKILL.md](../../skills/self-improvement/SKILL.md) |
| `tracker-management` | User mentions a ticket / issue / tracker, or a ticket key like `ABC-123` | [skills/tracker-management/SKILL.md](../../skills/tracker-management/SKILL.md) |
<!-- inventory:skills:end -->

## Specialization skills (spawned as `claude -p` per plan step)

Canonical path in repo: `skills/specializations/<name>/SKILL.md`. Symlinked flat into `~/.claude-agent/skills/<name>/` by `setup-symlinks.sh`.

**Shared marker protocol.** The invocation contract and the `CLARIFY:`/`PERMISSION-REQUEST:` format blocks common to every specialization live once in [skills/specializations/_shared/marker-protocol.md](../../skills/specializations/_shared/marker-protocol.md); each SKILL.md keeps only its role-specific marker subset plus a pointer. `spawn-specialist.py` inlines the shared file into the spawned system prompt (`composed_system_prompt_file`), so a spawned specialist always receives the full protocol. `_`-prefixed directories are support material, not spawnable specializations.

**Project-local specializations.** A project may ship its own domain experts under `<project>/.claude/skills/specializations/<name>/SKILL.md`. `spawn-specialist.py` resolves `--kind` from the global catalog first, then falls back to this project-local path (global wins on a name collision), so project specializations spawn with the same `claude -p` context isolation without entering the global catalog. They are not symlinked flat and so are not offered inline via the Skill tool — the spawn path is their invocation route.

<!-- inventory:specializations:begin -->
| name | Spawns when a plan step calls for | File |
|---|---|---|
| `code-reviewer` | Maintainability / readability / reusability review of a diff (self-review or independent) | [skills/specializations/code-reviewer/SKILL.md](../../skills/specializations/code-reviewer/SKILL.md) |
| `developer` | Writing, refactoring, debugging, reviewing production code | [skills/specializations/developer/SKILL.md](../../skills/specializations/developer/SKILL.md) |
| `planner` | Decomposition, stages, dependencies, risks, done criteria | [skills/specializations/planner/SKILL.md](../../skills/specializations/planner/SKILL.md) |
| `tech-writer` | README / documentation authoring and polishing plans & long comments, in the language of the dialogue | [skills/specializations/tech-writer/SKILL.md](../../skills/specializations/tech-writer/SKILL.md) |
| `thinker` | Independent reasoning check on a non-trivial chain | [skills/specializations/thinker/SKILL.md](../../skills/specializations/thinker/SKILL.md) |
| `yandex-cloud-expert` | Yandex Cloud / `yc` operations | [skills/specializations/yandex-cloud-expert/SKILL.md](../../skills/specializations/yandex-cloud-expert/SKILL.md) |
<!-- inventory:specializations:end -->
