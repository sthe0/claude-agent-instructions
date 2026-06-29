---
name: home-dir-arc-fuse-mounts
description: difficulty — a broad find/grep/Grep/Glob rooted at /home/the0, ~, or $HOME silently fans out across several network-backed arc FUSE mounts and is pathologically slow / hammers the mount. Fact — the home dir holds multiple fuse.arc mountpoints as direct children; scope every recursive search to a specific repo or subdir, never to the home root.
type: reference
schema: leaf/v1
created: 2026-06-23
last_verified: 2026-06-24
---

# `/home/the0` holds multiple arc FUSE mounts — never root a recursive search there

## Difficulty

A broad `find`/`grep -r`/`rg`/`Grep`/`Glob` rooted at `/home/the0`, `~`, or `$HOME` silently fans out across several network-backed arc FUSE mounts hanging off the home directory — the search becomes pathologically slow and hammers the mount with stat/readdir calls across the entire virtual FS tree.

## Guidance

On this machine the home directory `/home/the0` is **not** a plain local directory: several `arc` virtual-filesystem mounts (`fuse.arc`, the Arcadia `arc vfs`) hang directly off it. Observed 2026-06-23:

```
arc on /home/the0/arcadia                              type fuse.arc
arc on /home/the0/arcadia_claude_local                 type fuse.arc
arc on /home/the0/arcadia_DEEPAGENT-403-train-to-eval-graph  type fuse.arc
arc on /home/the0/arcadia_DEEPAGENT-430-unified-graph  type fuse.arc
arc on /home/the0/arcadia_DEEPAGENT-any                type fuse.arc
```

Each mount is a network-backed virtual FS where `stat`/`readdir` over the full tree is pathologically slow and pressures the mount. A recursive search (`find`, `grep -r`, `rg`, `fd`, or the built-in **Grep/Glob tools**) rooted at `/home/the0`, `~`, `$HOME`, or any ancestor of ≥2 of these mounts fans out across all of them.

**Rule:** scope every recursive search to the specific repository or subdirectory you need — e.g. `~/claude-agent-instructions/` or a single project dir — never the home root. This is especially easy to violate when the session cwd is itself under an arc mount (e.g. `/home/the0/arcadia_claude_local/robot/deepagent`) and you need files that live elsewhere under `~`: pin the absolute repo path, don't let the search default to `~`/cwd-parent.

A machine-local guard enforces this: `scripts/hook-arc-mount-search-guard.py` (PreToolUse `Bash|Grep|Glob`) reads the live mount table and **denies** a recursive search whose resolved root spans ≥2 `fuse.arc` mounts, with a message to re-scope.

> verified by: `mount | grep fuse.arc` on 2026-06-23 (5 mounts under /home/the0); the guard hook lives in the instructions repo.

## See also

- [[delegatable-work-patterns]] — pin the search root when delegating Pattern-B exploration
