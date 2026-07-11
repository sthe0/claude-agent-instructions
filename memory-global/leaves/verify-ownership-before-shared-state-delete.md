---
name: verify-ownership-before-shared-state-delete
description: Before deleting shared state (worktrees, mounts, branches, scope files), "no live process foothold" is not "no owner" — a suspended/resumable session still owns its state across idle gaps; check the durable session record (transcripts / scope registry), not only live-process state.
type: feedback
schema: leaf/v1
created: 2026-07-11
last_verified: 2026-07-11
---

# Verify ownership before deleting shared state

## Difficulty

To decide whether a shared, task-scoped resource — a git worktree, a checked-out mount, a personal branch, a `session_scope` record — is abandoned and safe to delete, the tempting oracle is **live-process state**: `readlink /proc/<pid>/cwd`, an open-fd scan, "is any running process sitting inside it?". This oracle is **incomplete**, and its failure is silent and destructive: a paused or resumable agent session owns its worktree/mount/branch across an **idle gap** during which it holds *no* live foothold — no running process, no cwd, no open fd. Judged on live state alone, such a resource reads as ownerless and gets force-removed (`git worktree remove --force`, an `rm`, a scope-file delete) **out from under a still-live owner** that will later resume and find its work destroyed. Ground: this session's user correction **"А ты смотрел транскрипты активных сессий?"** ("did you look at the transcripts of the active sessions?") after the agent judged worktrees abandoned on `/proc/<pid>/cwd` absence alone.

## Guidance

**To avoid destroying state a suspended-but-live session still owns, treat "no live process foothold" as *insufficient* evidence of no owner — cross-check the durable ownership record before any delete.**

- The durable ownership oracle is the **session record**, not `/proc`. Two concrete sources: the `session_scope` registry (`~/.claude-agent/agentctl/scopes/*.json`, whose records carry `cwd` / `repo_root` / `pid` / `heartbeat_ts` / `session_id` / `lineage_ids`), and the **session transcripts** for a resumable owner. A resource is *owned* if a scope record's `cwd` or `repo_root` sits at-or-inside it **and** the session is still live by *either* a confirmed-alive pid *or* a heartbeat within the staleness floor (a live pid is sufficient, but its *absence* is not proof of abandonment — the heartbeat covers the idle gap).
- Bias the classifier **fail-safe**: when ownership cannot be *proven* absent, KEEP. Unknown age → treat as fresh → spare. Cannot prove clean → treat as dirty → never force-remove; dead-letter a diff snapshot instead of discarding uncommitted work.
- This is the ownership arm of the destructive-command discipline in CLAUDE.md § Limits (guard interpolated paths, never collapse to `$HOME`) and the landing/teardown contract in [[landing-discipline]]: a task-scoped resource is torn down **only** at *its own* task's resolution gate by *its own* owner — never reclaimed by a bystander session on a bare live-process check.
- Mechanized form: `scripts/hook-orphan-worktree-sweep.py` (`is_owned()`) — the SessionStart sweeper reaps a worktree only when it is temp-root + detached + stale + **unowned by the registry** + clean, encoding this rule as a gate rather than leaving it to per-session judgement.

## See also

- [[landing-discipline]] — task-scoped resources (worktree, mount, branch, scope file) are torn down at *their* resolution gate by *their* owner.
- CLAUDE.md § Limits — destructive commands built from variables (guard non-emptiness, never collapse to a critical dir).
