# Migrations

Each entry describes a refactor that changed the on-disk layout (`~/.claude/` and/or `<project>/.claude/`) in a way that `setup-symlinks.sh` alone cannot reconcile — typically because old paths must be removed, content must move, or per-project setup is needed.

Read the migration page if `verify-layout-contract.sh` fails on a freshly pulled machine, or if `~/.claude/` still has artifacts from the old layout (dangling symlinks, removed agents, an old `memory/` directory).

| Date | Commit | Title |
|---|---|---|
| 2026-05-22 | `4671a41` | [Collapse manager / memory / self-improvement agents into root + skills](2026-05-collapse-manager-memory.md) |
