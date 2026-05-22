# Migration — 2026-05-22

> Refactor commit: `4671a41` — collapse manager / memory / self-improvement agents into root + skills; rebuild memory on native Claude Code auto-memory.

Read this if you (Claude Code or human) have just pulled the instructions repo on a machine that was set up before this commit. Your `~/.claude/` layer needs to be re-aligned with the new repo state — `setup-symlinks.sh` does most of it, but a few things must be removed manually.

## What changed in the repo (summary)

- **Subagents removed:** `manager`, `memory`, `self-improvement` (`agents/manager.md`, `agents/memory.md`, `agents/self-improvement.md`).
- **Skills introduced:** `skills/overcome-difficulty/SKILL.md` and `skills/self-improvement/SKILL.md` (+ `policy.md`).
- **Root coordinator is the manager.** Coordination cycle, recognition signals, outcome format moved into `CLAUDE.md`.
- **Memory model rebuilt** on native Claude Code auto-memory:
  - **Global memory:** `memory-global/MEMORY.md` + `leaves/`, imported into every session by `@~/.claude/memory-global/MEMORY.md` at the end of `CLAUDE.md`.
  - **Project memory:** `<project_cwd>/.claude/agent-memory/`, symlinked from `~/.claude/projects/<cwd-hash>/memory/` via `scripts/setup-project-memory.sh`.
- **memory-meta/ removed.** Content migrated:
  - `typical-coordinator-pitfalls`, `reasoning-and-task-solving`, `session-retrospective-2026-05` → `memory-global/leaves/`.
  - `instruction-language`, `file-structure-contract`, `instructions-git-sync` → `skills/self-improvement/policy.md`.
  - `runtime-layout` key bits → `CLAUDE.md`.
  - `claude-cursor-instructions` → `docs/deferred/cursor-integration.md` (Cursor wiring rework is a separate follow-up).
- **`memory-global/agent-instructions/` and `memory-global/development/` removed** (content migrated as above).
- **Scripts updated:** `setup-symlinks.sh` now manages `~/.claude/skills/` directory symlinks; `verify-layout-contract.sh` / `verify-instructions-sync.sh` updated for the new layout.
- **Cursor wiring (`cursor-rules/*.mdc`) intentionally not updated** — handled as a separate follow-up.

## What the other machine sees after `git pull`

- `git status` clean, `behind=0`. New repo state is in place.
- On disk under `~/.claude/`:
  - `~/.claude/agents/manager.md`, `~/.claude/agents/memory.md`, `~/.claude/agents/self-improvement.md` — **dangling symlinks** (their targets in the repo are gone).
  - `~/.claude/memory/` — old local-memory directory, possibly with symlinks into the now-removed `memory-meta/` or into the local arc tree. The new layout does **not** use `~/.claude/memory/`.
  - `~/.claude/skills/` — does not exist yet or is empty; the new global skills are not symlinked in.
- `verify-layout-contract.sh` FAILs (it now requires the new structure).

## Migration steps

Run from the machine to be migrated.

### 1. Pull and confirm you are at the refactor commit (or later)

```bash
~/claude-agent-instructions/scripts/sync-instructions-repo.sh pull
cd ~/claude-agent-instructions
git log --oneline -1            # expect 4671a41 or later commit on main
```

### 2. Decide what to do with `~/.claude/memory/` content

The old `~/.claude/memory/` directory is **superseded**. Two cases:

- **Symlinks into the local arc tree** (Yandex/Arcadia: `junk/the0/agents/memory-local/`). The arc tree still exists, but the new model expects project-specific runbooks to live in `<project_cwd>/.claude/agent-memory/`. Plan a follow-up to move each runbook (deepagent, etc.) into the corresponding project, then commit `agent-memory/` to the project's git. For now, you can leave the arc tree alone — only the `~/.claude/memory/` runtime mount goes away.
- **No machine-local content worth keeping.** Just delete the directory.

Either way, the directory itself must go (`verify-layout-contract.sh` insists):

```bash
rm -rf ~/.claude/memory
```

If you have an `~/arcadia_the0-agents/junk/the0/agents/memory-local/` you want to preserve, that lives in arc and is unaffected by this step.

### 3. Re-run `setup-symlinks.sh`

```bash
~/claude-agent-instructions/scripts/setup-symlinks.sh
```

This will:

- Prune dangling agent symlinks (`manager.md`, `memory.md`, `self-improvement.md`).
- Create `~/.claude/skills/` and symlink `overcome-difficulty/` and `self-improvement/` into it.
- Re-link `~/.claude/CLAUDE.md`, `~/.claude/memory-global`, agent files.
- Run the verifiers at the end.

### 4. Verify

```bash
~/claude-agent-instructions/scripts/verify-instructions-sync.sh
```

Expected: `All checks passed.` If it FAILs:

- **"~/.claude/memory exists"** → step 2 was skipped; remove it.
- **"stale agent symlink"** → `manager.md` / `memory.md` / `self-improvement.md` still in `~/.claude/agents/`; delete the symlink and re-run `setup-symlinks.sh`.
- **"~/.claude/skills missing"** → the directory could not be created; check permissions on `~/.claude/`.

### 5. Per-project memory (only where you want shared agent memory)

For each working tree where multiple developers should share project memory:

```bash
cd <project_cwd>
~/claude-agent-instructions/scripts/setup-project-memory.sh
git add .claude/agent-memory
git commit -m "agent memory: bootstrap"
```

The script symlinks `~/.claude/projects/<cwd-hash>/memory/` → `<project_cwd>/.claude/agent-memory/`, so native Claude Code auto-memory reads and writes through the symlink. Existing per-cwd memory under `~/.claude/projects/<cwd-hash>/memory/` is backed up to `memory.bak.<timestamp>` rather than lost — review the backup, salvage any per-project facts into the new location.

The script refuses to run with `$HOME` as the project (home is not a project; its memory stays at `~/.claude/projects/-Users-<you>/memory/`).

### 6. Optional automation

`scripts/migrate-pre-2026-05.sh` performs steps 2 + 3 + 4 idempotently. Run it instead of doing them by hand:

```bash
~/claude-agent-instructions/scripts/migrate-pre-2026-05.sh
```

It does **not** touch per-project memory (step 5) — that requires choosing which projects to set up.

## After migration — what to know in a new session

- The root dialog is the manager. Do not look for a `manager` subagent.
- `Task → memory` / `Task → self-improvement` calls **fail** (those agents are gone). Use the skills instead: invoke `self-improvement` on user feedback, `overcome-difficulty` when stuck.
- Memory writes follow the native auto-memory mechanism (see the `# auto memory` section of your system prompt). The "where to write" routing is global vs project, per `CLAUDE.md` § Memory.
- Cursor configuration is currently stale (`cursor-rules/*.mdc` still references the pre-refactor layout). Until that rework lands, Cursor's view of these instructions will be inconsistent — do not rely on it.
