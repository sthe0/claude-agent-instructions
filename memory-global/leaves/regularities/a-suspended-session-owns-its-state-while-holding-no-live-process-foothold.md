---
name: a-suspended-session-owns-its-state-while-holding-no-live-process-foothold
description: A paused resumable session owns its worktree, mount, branch and scope record while holding no process, cwd or fd; a live-process check reads it as ownerless
type: reference
schema: regularity/v1
generality: 1
induced_from: [verify-ownership-before-shared-state-delete]
rule_derived: "lifted from verify-ownership-before-shared-state-delete; no experience leaf backs it (the leaf cites a user correction in its own session)"
introduced_by: grounding-norm
created: 2026-10-30
last_verified: 2026-10-30
---

# a-suspended-session-owns-its-state-while-holding-no-live-process-foothold

## Regularity

A paused or resumable agent session owns its worktree, checked-out mount, personal branch and session_scope record across an idle gap during which it holds no running process, no working directory and no open file descriptor, so a live-process check such as `readlink /proc/<pid>/cwd` or an open-fd scan reads that resource as ownerless while the owner will later resume and need it; the durable ownership record is the session_scope registry (records carrying cwd, repo_root, pid, heartbeat_ts, session_id, lineage_ids) and the session transcripts.

## Generality

Level 1 — ranges over task-scoped resources (worktrees, mounts, branches, scope files) owned by a resumable agent session across an idle gap. It is deliberately not claimed for resources owned by a process that is still running, nor for sessions that can never resume.

## Induced from

- [[verify-ownership-before-shared-state-delete]] — contributes the observed practice behind the statement: a user correction in its own session that a resource with no live process foothold still had an owner who later resumed.

## Refutation

Observing a resumable session that owns a worktree or mount across an idle gap and whose absence of any live process, cwd or open fd coincides with the session_scope registry and transcripts also showing no owner, or finding that a resumed session never needs the state it owned before the gap, would show the statement false.
