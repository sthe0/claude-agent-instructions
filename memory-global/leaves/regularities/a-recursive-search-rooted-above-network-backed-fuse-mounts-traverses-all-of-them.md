---
name: a-recursive-search-rooted-above-network-backed-fuse-mounts-traverses-all-of-them
description: The home directory carries several network-backed VCS FUSE mounts; a recursive traversal rooted at or above them descends into all and is very slow
type: reference
schema: regularity/v1
generality: 1
induced_from: [home-dir-arc-fuse-mounts]
rule_derived: "lifted from home-dir-arc-fuse-mounts; no experience leaf backs it"
introduced_by: grounding-norm
created: 2026-10-30
last_verified: 2026-10-30
---

# a-recursive-search-rooted-above-network-backed-fuse-mounts-traverses-all-of-them

## Regularity

On the user's machine the home directory /home/the0 is not a plain local directory: several network-backed virtual-filesystem mounts of an org-specific VCS tool (mount type fuse.<vcs>; five were observed on 2026-06-23, the org monorepo checkout, a second working copy and three ticket-scoped checkouts) hang directly off it. A recursive traversal (find, grep -r, rg, fd, ls -R, du, or the Grep and Glob tools) rooted at the home directory, ~, $HOME or any ancestor of two or more of these mounts descends into every mount, and stat/readdir over a mount's full tree is pathologically slow and loads the mount. A traversal rooted strictly inside one mount is not affected the same way; the machine-local guard scripts/hook-multi-mount-search-guard.py denies the first kind by reading the live mount table.

## Generality

Level 1 — ranges over recursive file traversals on this user's machine rooted at the home directory or any ancestor of two or more network-backed VCS FUSE mounts. It is deliberately not claimed for traversals rooted strictly inside one mount, nor for machines without such mounts.

## Induced from

- [[home-dir-arc-fuse-mounts]] — contributes the observed state behind the statement: five fuse.<vcs> mounts under the home directory on 2026-06-23 and the cost of traversing them.

## Refutation

A mount listing of the machine shows no fuse.<vcs> mounts under /home/the0, or a recursive find/grep rooted at the home directory completes at ordinary local-disk speed without stat/readdir load on those mounts.
