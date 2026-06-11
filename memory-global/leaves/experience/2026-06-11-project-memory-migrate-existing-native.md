---
name: 2026-06-11-project-memory-migrate-existing-native
description: Wiring project-tree memory on a machine that already has populated ~/.claude/projects/<hash>/memory: the stock setup-project-memory.sh stubs the live memory and moves it to .bak, orphaning accumulated content. Migrate content first, then swap the native path to a symlink.
type: reference
schema: difficulty/v1
resolution_confirmed_by_user: "Да, решено"
refs: [2026-06-04-org-specific-vs-global-placement.md, memory-hierarchy.md]
---

# Migrating populated native auto-memory into the project tree

## Difficulty
setup-project-memory.sh is written for the greenfield case: it creates an empty agent-memory/ with a stub MEMORY.md and, finding a non-empty native memory dir, moves it to memory.bak.<ts> and symlinks the (now empty) stub. Run as-is against a project whose native auto-memory is already populated (experience leaves, digests, profile notes), it silently orphans all of it — the live memory becomes an empty stub and stops loading. The script's docstring says 'Existing project memory files are preserved', which is true only of files already inside the project tree, not of the native-location content it is supposed to adopt.

## Order & criterion
Preserve accumulated memory while relocating it: content must end up in <cwd>/.claude/agent-memory/ AND still load via the native path.

**Acceptance check:** diff -r between the pre-migration native memory and the post-migration agent-memory is empty (modulo new empty schema dirs); native path is a symlink into the tree; MEMORY.md readable through it.

## Contexts

### 2026-06-11 — initial
- Where it arose: macOS, ~/projects/{marmaris,the0.fun,yandex-cloud}, none under git. Actualizing project structure per ~/.claude instructions; each had real native auto-memory but no .claude/agent-memory/ in-tree.
- Working plan: 1) mkdir agent-memory; cp -a native/. agent-memory/ (BSD cp copies dotfiles via the trailing /.); 2) mv native -> native.premigrate.bak (keep backup, do not rm yet); 3) ln -s agent-memory native; 4) diff -r backup vs target to prove identity BEFORE removing backup; 5) git init + .gitignore (exclude node_modules/.playwright-mcp/.consolidate-lock/settings.local.json, keep agent-memory tracked); commit local only, no remote/push when memory holds secrets. Do NOT call setup-project-memory.sh against populated native memory. Also: a machine can lack global git identity (commit fails with 'Author identity unknown') — mirror an existing repo's user.name/email per-repo instead of setting it global.

## Cost
~30 min, in-thread, no spawns; 3 projects migrated + git-init'd.

## Self-critique of the agent system
Two agent-system gaps: (1) setup-project-memory.sh has no content-migration path for the populated-native case — it should detect non-empty native memory with an empty/absent in-tree target and OFFER to move content in, not stub+bak. (2) The infra-as-code spawn rule (init/symlink-migration -> spawn developer) would here have REDUCED reliability: a cold developer would re-derive the orphaning trap. The rule's functional ground is irreversibility + cost-log accountability; when the dominant risk is content-preservation that the manager already has full context on, in-thread with backups serves the ground better. Worth a carve-out note in the rule.
