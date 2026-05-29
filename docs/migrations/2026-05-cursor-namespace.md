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

## Project mounts (`robot/deepagent`)

Cursor also reads **project-local** subagents from `<mount>/robot/deepagent/.cursor/agents/`. That tree is listed in `.arcignore` (machine-local overlay, not product arc history). Before this migration, copies of `*-spawn.md` were often committed only on disk as **regular files** and drifted from `~/claude-agent-instructions/cursor/agents/`.

**Active project today:** `robot/deepagent` under each Arcadia working mount. Typical paths:

| Mount | Project root |
|---|---|
| Main trunk | `~/arcadia/robot/deepagent` |
| Ticket / branch mounts | `~/arcadia_*/robot/deepagent` |

Discover mounts on this machine:

```bash
for root in "$HOME/arcadia/robot/deepagent" "$HOME"/arcadia_*/robot/deepagent; do
  [[ -d "$root" ]] && echo "$root"
done
```

(Other products, e.g. `logos`, are separate — only run their `setup-local.sh` if you use them.)

### Per-mount steps

From `~/claude-agent-instructions/` after pull:

```bash
# All deepagent roots found above:
bash cursor/scripts/migrate-cursor-namespace.sh --all-deepagent-mounts

# Or explicit roots only:
bash cursor/scripts/migrate-cursor-namespace.sh \
  ~/arcadia/robot/deepagent \
  ~/arcadia_MY-TICKET-slug/robot/deepagent
```

Each project root runs:

1. `<project>/.claude/scripts/setup-local.sh` (symlinks `.claude`, Cursor rules, permissions, **and** project `.cursor/agents/*` → `cursor/agents/`).
2. Or, if you only need agents fixed:  
   `cursor/scripts/link-project-cursor-agents.sh <project_root>`

### Cleanup if linking refuses

`link-project-cursor-agents.sh` will **not** overwrite a regular file. If you have stale copies:

```bash
cd <mount>/robot/deepagent
ls -la .cursor/agents/
# move aside any non-symlink *-spawn.md, then re-run setup-local or link-project-cursor-agents.sh
mv .cursor/agents/developer-spawn.md .cursor/agents/developer-spawn.md.bak.$(date +%Y%m%d)  # example
~/claude-agent-instructions/cursor/scripts/link-project-cursor-agents.sh "$PWD"
```

Optional: remove the backup after `readlink .cursor/agents/developer-spawn.md` points at `~/claude-agent-instructions/cursor/agents/`.

### deepagent storage (`arcadia_claude_local`)

`setup-local.sh` in  
`~/arcadia_claude_local/junk/the0/agents/robot/deepagent/scripts/`  
must include step 7 (project Cursor agents). After updating storage on trunk, re-run `setup-local.sh` from **each** mount so every `~/arcadia*` copy picks up symlinks.

## Expected runtime state after migration

**Global (user):**

- `~/.cursor/rules/claude-code-sync.mdc` → `~/claude-agent-instructions/cursor/rules/claude-code-sync.mdc`
- `~/.cursor/agents/developer-spawn.md` → `~/claude-agent-instructions/cursor/agents/developer-spawn.md`
- `~/.cursor/agents/planner-spawn.md` → `~/claude-agent-instructions/cursor/agents/planner-spawn.md`
- `~/.cursor/agents/thinker-spawn.md` → `~/claude-agent-instructions/cursor/agents/thinker-spawn.md`
- `~/.claude/agents/` remains independent (Claude Code only).

**Per mount (`robot/deepagent`):**

- `<mount>/robot/deepagent/.cursor/agents/*-spawn.md` → symlinks to the same `~/claude-agent-instructions/cursor/agents/*.md`
- `<mount>/robot/deepagent/.cursor/rules/deepagent-project.mdc` → via `.claude/rules/project.mdc` (unchanged)
- No duplicate regular-file copies of spawn agents left in the mount.

## Verify

```bash
cd ~/claude-agent-instructions && ./scripts/verify-all.py
for root in "$HOME/arcadia/robot/deepagent" "$HOME"/arcadia_*/robot/deepagent; do
  [[ -d "$root" ]] || continue
  echo "== $root =="
  ls -la "$root/.cursor/agents/"*spawn*.md 2>/dev/null || true
done
```

Every `*-spawn.md` should show `-> .../claude-agent-instructions/cursor/agents/...`.
