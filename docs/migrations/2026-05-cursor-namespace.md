# Cursor namespace split (2026-05-28)

## What changed

Cursor-specific assets moved under `cursor/`:

- `cursor/rules/claude-code-sync.mdc` (was `cursor-rules/claude-code-sync.mdc`)
- `cursor/scripts/lint-cursor-mirror.py` (was `scripts/lint-cursor-mirror.py`)
- new `cursor/agents/*.md` for Cursor-only specialization wrappers
- new `cursor/scripts/install-cursor-links.sh`
- new `cursor/scripts/link-project-cursor-agents.sh`
- new `cursor/scripts/migrate-cursor-namespace.sh`

`~/.cursor/agents` is no longer linked to `~/.claude/agents`.

## Why

Prevent Cursor-only assets from leaking into Claude Code runtime paths and keep ownership boundaries explicit.

## One-machine migration (global only)

From `~/claude-agent-instructions/`:

```bash
scripts/sync-instructions-repo.sh pull
scripts/setup-symlinks.sh
scripts/verify-layout-contract.sh
scripts/verify-instructions-sync.sh
```

This wires **user-level** Cursor paths only (`~/.cursor/rules/`, `~/.cursor/agents/`).

## Project mounts

Cursor also reads **project-local** subagents from `<project_root>/.cursor/agents/`. Where the project root sits inside a VCS monorepo mount, that tree is normally ignored by the monorepo's VCS — a machine-local overlay, not product history. Before this migration, copies of `*-spawn.md` were often left on disk as **regular files** and drifted from `~/claude-agent-instructions/cursor/agents/`.

One project root lives under each working mount. Typical paths:

| Mount | Project root |
|---|---|
| Main trunk | `<trunk_mount>/<project_path>` |
| Ticket / branch mounts | `<ticket_mount>/<project_path>` |

Discover mounts on this machine:

```bash
for root in "$HOME"/<mount_glob>/<project_path>; do
  [[ -d "$root" ]] && echo "$root"
done
```

(Other projects on the same machine are separate — only run their `setup-local.sh` if you use them.)

### Per-mount steps

From `~/claude-agent-instructions/` after pull:

```bash
# Every project root discovered on this machine — `--help` names the
# discovery flag, which an overlay names after its own project:
bash cursor/scripts/migrate-cursor-namespace.sh --help

# Or explicit roots only:
bash cursor/scripts/migrate-cursor-namespace.sh \
  <trunk_mount>/<project_path> \
  <ticket_mount>/<project_path>
```

Each project root runs:

1. `<project>/.claude/scripts/setup-local.sh` (symlinks `.claude`, Cursor rules, permissions, **and** project `.cursor/agents/*` → `cursor/agents/`).
2. Or, if you only need agents fixed:  
   `cursor/scripts/link-project-cursor-agents.sh <project_root>`

### Cleanup if linking refuses

`link-project-cursor-agents.sh` will **not** overwrite a regular file. If you have stale copies:

```bash
cd <project_root>
ls -la .cursor/agents/
# move aside any non-symlink *-spawn.md, then re-run setup-local or link-project-cursor-agents.sh
mv .cursor/agents/developer-spawn.md .cursor/agents/developer-spawn.md.bak.$(date +%Y%m%d)  # example
~/claude-agent-instructions/cursor/scripts/link-project-cursor-agents.sh "$PWD"
```

Optional: remove the backup after `readlink .cursor/agents/developer-spawn.md` points at `~/claude-agent-instructions/cursor/agents/`.

### Overlay storage

Where `setup-local.sh` lives in a machine-local overlay tree rather than in the project itself, that copy must include step 7 (project Cursor agents) too. After updating the overlay on trunk, re-run `setup-local.sh` from **each** mount so every copy picks up the symlinks.

## Expected runtime state after migration

**Global (user):**

- `~/.cursor/rules/claude-code-sync.mdc` → `~/claude-agent-instructions/cursor/rules/claude-code-sync.mdc`
- `~/.cursor/agents/developer-spawn.md` → `~/claude-agent-instructions/cursor/agents/developer-spawn.md`
- `~/.cursor/agents/planner-spawn.md` → `~/claude-agent-instructions/cursor/agents/planner-spawn.md`
- `~/.cursor/agents/thinker-spawn.md` → `~/claude-agent-instructions/cursor/agents/thinker-spawn.md`
- `~/.claude/agents/` remains independent (Claude Code only).

**Per mount (each project root):**

- `<project_root>/.cursor/agents/*-spawn.md` → symlinks to the same `~/claude-agent-instructions/cursor/agents/*.md`
- `<project_root>/.cursor/rules/<project>-project.mdc` → via `.claude/rules/project.mdc` (unchanged)
- No duplicate regular-file copies of spawn agents left in the mount.

## Verify

```bash
cd ~/claude-agent-instructions && ./scripts/verify-all.py
for root in "$HOME"/<mount_glob>/<project_path>; do
  [[ -d "$root" ]] || continue
  echo "== $root =="
  ls -la "$root/.cursor/agents/"*spawn*.md 2>/dev/null || true
done
```

Every `*-spawn.md` should show `-> .../claude-agent-instructions/cursor/agents/...`.
