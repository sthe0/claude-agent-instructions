# File structure contract (agent instructions)

**Canonical description.** If disk disagrees — fix **either** this document and related READMEs **or** the file tree and symlinks. Do not leave stale docs.

See also: [runtime-layout.md](runtime-layout.md) (runtime paths), [../../README.md](../../README.md) § Agent cooperation.

## Metadata

| Field | Value |
|------|----------|
| `last_verified` | 2026-05-21 |
| `staleness_triggers` | new directory in git/arc instructions; change to `setup-symlinks.sh`; moving scripts between global/local |
| `revalidate` | `~/claude-agent-instructions/scripts/verify-layout-contract.sh`; `verify-instructions-sync.sh` |

## Layers

| Layer | Versioning | Tree description |
|------|-----------------|-----------------|
| **Global** | git `~/claude-agent-instructions` | this file, § Global tree |
| **Local** | arc (branch on machine) | `~/.claude/memory/INDEX.md` → leaf `the0-agents-mount`; `~/.claude/scripts-local/README.md` |

Global prompts **must not** reference arc junk paths — only runtime (`~/.claude/...`).

## Global tree (`~/claude-agent-instructions/`)

```
CLAUDE.md
README.md
agents/*.md              # global subagents (developer, manager, …)
agents-local/README.md   # pointer to local arc, no *.md agents here
cursor-rules/
  claude-code-sync.mdc
  project-overlay-deepagent.mdc
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
githooks/post-commit
docs/                    # optional
```

**Forbidden in global `scripts/`:** arc scripts (`sync-junk-agents-arc`, `junk-agents-arc-commit`, `setup-the0-agents-mount`, …) — local `scripts/` only.

## Runtime symlinks (after `setup-symlinks.sh`)

| Runtime | Source (logical) |
|---------|------------------------|
| `~/.claude/CLAUDE.md` | `CLAUDE.md` |
| `~/.claude/agents/<global>.md` | `agents/<name>.md` |
| `~/.claude/agents/<local>.md` | local `agents-local/` (arc) |
| `~/.claude/memory-global/` | `memory-global/` |
| `~/.claude/memory/` | local `memory-local/` (arc) |
| `~/.claude/scripts-local/` | local `scripts/` (arc) |
| `~/.cursor/rules/claude-code-sync.mdc` | `cursor-rules/claude-code-sync.mdc` |
| `~/.cursor/agents` | `~/.claude/agents` |

## Local tree (arc, not in instructions git)

On-disk layout on the machine (typical):

```
junk/the0/agents/
  README.md
  agents-local/*.md
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
