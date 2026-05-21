# File structure contract (agent instructions)

**Canonical description.** If disk disagrees — fix **either** this document and related READMEs **or** the file tree and symlinks. Do not leave stale docs.

See also: [runtime-layout.md](runtime-layout.md) (runtime paths), [../../README.md](../../README.md) § Agent cooperation.

## Metadata

| Field | Value |
|------|----------|
| `last_verified` | 2026-05-21 (skills-local, mcp-local added) |
| `staleness_triggers` | new directory in git/arc instructions; change to `setup-symlinks.sh`; moving scripts between global/local |
| `revalidate` | `~/claude-agent-instructions/scripts/verify-layout-contract.sh`; `verify-instructions-sync.sh` |

## Layers

| Layer | Versioning | Tree description |
|------|-----------------|-----------------|
| **Global** | git `~/claude-agent-instructions` | this file, § Global tree |
| **Local** | arc (branch on machine) | `~/.claude/memory/INDEX.md` → leaf `the0-agents-mount`; `~/.claude/scripts-local/README.md` |

Global prompts (`agents/`, slim `CLAUDE.md`) **must not** embed arc/Arcadia/Tracker procedures — only pointers to `~/.claude/memory/`. Org **gates** live in local `cursor-rules/org-yandex.mdc` (arc tree).

## Global tree (`~/claude-agent-instructions/`)

```
CLAUDE.md
README.md
agents/*.md              # global subagents (developer, manager, …)
agents-local/README.md   # on Arcadia machines: pointer to arc; on non-Arcadia: gitignored *.md here as fallback
skills-local/README.md   # gitignored *.md here → ~/.claude/skills/ (non-Arcadia machine-local skills)
mcp-local/README.md      # gitignored *.json here → applied to settings.local.json via apply-mcp-local.sh
cursor-rules/
  claude-code-sync.mdc
  project-overlay-deepagent.mdc
  # org-yandex.mdc — local arc only (junk/the0/agents/cursor-rules/)
memory-global/
  INDEX.md, README.md
  agent-instructions/    # runtime-layout, file-structure-contract, instruction-language, instructions-git-sync
  development/
memory-meta/README.md    # deprecated, do not add leaves
scripts/
  setup-symlinks.sh
  verify-instructions-sync.sh
  verify-layout-contract.sh
  sync-instructions-repo.sh
  install-git-hooks.sh
  install-sync-cron.sh
  install-sync-systemd-timer.sh
  apply-mcp-local.sh     # merge mcp-local/*.json → ~/.claude/settings.local.json
githooks/post-commit
docs/                    # optional
```

**Forbidden in global `scripts/`:** arc scripts (`sync-junk-agents-arc`, `junk-agents-arc-commit`, `setup-the0-agents-mount`, …) — local `scripts/` only.

## Runtime symlinks (after `setup-symlinks.sh`)

| Runtime | Source (logical) |
|---------|------------------------|
| `~/.claude/CLAUDE.md` | `CLAUDE.md` |
| `~/.claude/agents/<global>.md` | `agents/<name>.md` |
| `~/.claude/agents/<local>.md` | local `agents-local/` (arc on Arcadia; `agents-local/*.md` gitignored in repo on other machines) |
| `~/.claude/skills/<local>.md` | `skills-local/*.md` gitignored in repo (non-Arcadia machine-local skills) |
| `~/.claude/memory-global/` | `memory-global/` |
| `~/.claude/memory/` | local `memory-local/` (arc) |
| `~/.claude/scripts-local/` | local `scripts/` (arc) |
| `~/.cursor/rules/claude-code-sync.mdc` | `cursor-rules/claude-code-sync.mdc` |
| `~/.cursor/rules/org-yandex.mdc` | local `junk/the0/agents/cursor-rules/org-yandex.mdc` |
| `~/.cursor/agents` | `~/.claude/agents` |

## Local tree (arc, not in instructions git)

On-disk layout on the machine (typical):

```
junk/the0/agents/
  README.md
  agents-local/*.md
  cursor-rules/org-yandex.mdc
  memory-local/
    INDEX.md, README.md
    deepagent/, claude-code/, yandex/
  scripts/
    README.md
    setup-the0-agents-mount.sh
    sync-junk-agents-arc.sh
    junk-agents-arc-commit.sh
    install-junk-agents-sync-cron.sh
    verify-the0-agents-sync.sh
```

Runtime only via `~/.claude/memory/`, `~/.claude/scripts-local/`.

## Agent obligations

### On structure change

Any add/move/delete of directory, script, or global/local split:

1. Update **this file** (and `runtime-layout.md` if runtime paths change).
2. Update **README.md** § symlinks/scripts and § Maintaining this README.
3. Local layer — leaf in `~/.claude/memory/` or `scripts/README.md` in arc; arc commit.
4. Global layer — git commit + push.
5. Run `verify-layout-contract.sh` and `verify-instructions-sync.sh`.

### Regular reconciliation

| When | Action |
|-------|----------|
| After edits in `~/claude-agent-instructions/` or local arc | `verify-layout-contract.sh` |
| Every few weeks / on user request | full reconcile: contract ↔ `ls`/`readlink` ↔ INDEX |
| Mismatch | fix document **or** tree; not both diverging |

Parent and **self-improvement** include reconciliation in Definition of Done when refactoring instructions.

### Mismatch: what to fix

| Symptom | Likely fix |
|---------|----------------|
| File exists, not in contract | extend contract (if intentional) or remove extra |
| In contract, missing on disk | restore file or remove from contract |
| Symlink wrong target | `setup-symlinks.sh` |
| arc script in global git | move to local `scripts/` |
