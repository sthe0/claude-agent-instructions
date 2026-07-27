---
name: home-dir-arc-fuse-mounts
description: difficulty — a broad find/grep/Grep/Glob rooted at /home/the0, ~, or $HOME silently fans out across several network-backed FUSE mounts of an org-specific VCS tool and is pathologically slow / hammers the mount. Fact — the home dir holds multiple such FUSE mountpoints as direct children; scope every recursive search to a specific repo or subdir, never to the home root.
type: reference
schema: leaf/v1
created: 2026-06-23
last_verified: 2026-06-24
---

# `/home/the0` holds multiple org-VCS FUSE mounts — never root a recursive search there

## Difficulty

A broad `find`/`grep -r`/`rg`/`Grep`/`Glob` rooted at `/home/the0`, `~`, or `$HOME` silently fans out across several network-backed FUSE mounts of an org-specific VCS tool hanging off the home directory — the search becomes pathologically slow and hammers the mount with stat/readdir calls across the entire virtual FS tree.

## Guidance

On this machine the home directory `/home/the0` is **not** a plain local directory: several virtual-filesystem mounts of an org-specific VCS tool (a FUSE-backed monorepo checkout mechanism, its own `fuse.<vcs>` mount type) hang directly off it. Observed 2026-06-23: five such mounts hung directly off the home directory — the org's main monorepo checkout, a second working copy, and three ticket-scoped checkouts, each of type `fuse.<vcs>`.

Each mount is a network-backed virtual FS where `stat`/`readdir` over the full tree is pathologically slow and pressures the mount. A recursive search (`find`, `grep -r`, `rg`, `fd`, or the built-in **Grep/Glob tools**) rooted at `/home/the0`, `~`, `$HOME`, or any ancestor of ≥2 of these mounts fans out across all of them.

**Rule:** scope every recursive search to the specific repository or subdirectory you need — e.g. `~/claude-agent-instructions/` or a single project dir — never the home root. This is especially easy to violate when the session cwd is itself under one of these mounts (e.g. a second working copy several path components deep) and you need files that live elsewhere under `~`: pin the absolute repo path, don't let the search default to `~`/cwd-parent.

A machine-local guard enforces this: `scripts/hook-multi-mount-search-guard.py` (PreToolUse `Bash|Grep|Glob`) reads the live mount table and **denies** a recursive search whose resolved root spans ≥2 mounts of that VCS tool, with a message to re-scope.

> verified by: a `mount` listing filtered to that VCS tool's FUSE type on 2026-06-23 (5 mounts under /home/the0); the guard hook lives in the instructions repo.

## See also

- [[delegatable-work-patterns]] — pin the search root when delegating Pattern-B exploration
