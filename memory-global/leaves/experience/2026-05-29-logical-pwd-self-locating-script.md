---
name: 2026-05-29-logical-pwd-self-locating-script
description: Difficulty — a script that derives its own base dir via logical pwd, invoked through a symlink it then rewrites, self-linked .claude (ELOOP) and broke a ticket worktree. Fix is to invoke by the real path (readlink -f) / pwd -P; recover when the source of truth is intact.
type: reference
schema: difficulty/v1
resolution_confirmed_by_user: "Push и закрыть (Recommended)"
created: 2026-06-11
last_verified: 2026-06-11
---

# Logical-pwd self-locating script footgun (ELOOP)

## Difficulty
`migrate-cursor-namespace.sh --all-deepagent-mounts` invoked `setup-local.sh` through a mount's `.claude` symlink. `setup-local.sh` computes `STORAGE="$(cd "$(dirname "$0")/.." && pwd)"` with a **logical** pwd — by contract it expects to be invoked as `<storage>/scripts/setup-local.sh`. Invoked via the symlink, logical pwd resolved `STORAGE` back to `.claude` itself, and the script's step 1 (`link "$STORAGE" "$ROOT/.claude"`) relinked `.claude` onto itself → `Too many levels of symbolic links`, exit 126, `set -e` aborting the rest. A self-inflicted ELOOP on a ticket worktree.

## Order & criterion
A script that derives its own base dir via logical `pwd` must be invoked **by its real path** — the caller resolves it (`readlink -f`) or the script uses `pwd -P` (physical) — *especially* when a step rewrites the very symlink it was reached through. After a failed approval, re-ask with the new information rather than retrying the broad operation; prefer the narrow tool that achieves the goal. **Acceptance check:** the migration helper resolves its real path before invoking; `set -e` contains the blast radius; recovery is a single re-link because the storage tree is untouched.

## Contexts

### 2026-05-29 — cursor-namespace mount migration
The user-approved "full setup-local via --all-deepagent-mounts" was the dangerous path (approved before the bug was known). After the ELOOP broke one mount: localize via `namei` (self-link + fresh timestamp = self-inflicted) → recover with one `ln -sfn <storage> <mount>/.claude` (storage intact) → re-ask the user → switch to the **narrow** `link-project-cursor-agents.sh` (touches only `.cursor/agents`, no `.claude` self-link risk), which was also the minimal thing achieving the goal → fix the root cause in `migrate-cursor-namespace.sh` (`readlink -f` the real script path). Commit `8176cf0`. `set -e` had limited damage to one of 6 mounts. `overcome-difficulty` discipline (localize before retrying a failed mount/VCS op) applied correctly.

## Cost
In-thread, no spawns, one difficulty cycle fully inline. 4 `AskUserQuestion` gates (do mounts need updating → which wiring → after-failure re-ask → push). Cost driver: the single `setup-local.sh` STORAGE-derivation line was the whole surprise. Optional un-done hardening: `pwd -P` in `setup-local.sh` as defense-in-depth (lives in the separate `arcadia_claude_local` storage VCS).
