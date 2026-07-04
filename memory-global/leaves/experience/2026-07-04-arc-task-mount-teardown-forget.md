---
name: 2026-07-04-arc-task-mount-teardown-forget
description: Per-task arc mounts were torn down with plain arc unmount, which is a detach (store under ~/.arc/stores/ + registry entry in ~/.arc/mount-points persist by design) — 11 stale WARN registry entries and 4 unused stores (~6.3G) accumulated silently. Full teardown is arc unmount --forget; stale WARN entries (store already gone) additionally need --force. Before forgetting a materialized store, check unpushed state: arc branch -v --json (remote field = tracking ref), arc log --oneline <remote>..<branch> per branch, arc status -s. Sweep mechanized as junk/the0/agents/common/scripts/arc-mounts-gc.sh (PR 14261429).
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user"
refs: [memory-global/leaves/system-knowledge/landing-changes-core-git-and-arc.md, https://a.yandex-team.ru/review/14261429]
created: 2026-07-04
last_verified: 2026-07-04
---

# Arc task-mount lifecycle must end with unmount --forget; GC mechanized

## Difficulty
Desired: task-mount teardown leaves no residue. Actual: plain arc unmount kept stores+registry entries; 11 stale WARN lines and 6.3G of dead stores accumulated across weeks, discovered only when the user ran arc mount -l. Root cause: the arc backend workflow creates mounts but has no teardown verb (git side has land-branch.py deleting worktree+branch; arc side had nothing), and no mechanism invoked cleanup at any lifecycle point.

## Order & criterion
1) Inspect: parse ~/.arc/mount-points + arc mount -l; classify garbage into stale registry entries / unused stores / orphan dirs. 2) For each unused store: remount, check unpushed branches (arc log <remote>..<branch>) + arc status -s; keep-and-report dirty ones. 3) Forget clean ones (arc unmount --forget; --force --forget for stale entries). 4) For the kept dirty store: verify its unique content against trunk byte-for-byte before deciding — the unpushed commit and uncommitted files can be fully superseded. 5) Mechanize: GC script + wiring into lifecycle (resolution-gate hook nudge; backend teardown verb).

**Acceptance check:** arc mount -l shows only live mounts: zero WARN lines, zero stores without a mount, orphan dirs removed; every deletion preceded by a persisted inspection log proving the store was clean or superseded.

## Contexts

### 2026-07-04 — initial
- Where it arose: Machine with arc task-mount workflow (~/task-mounts anchor model); any session that creates per-task arc mounts or reviews arc mount -l output.
- Working plan: ~/.claude-agent/plans/arc-mount-store-cleanup-v1.toml (5 stages, all PASSED)

## Cost
~2h wall-clock; 2 developer spawns (~$5.85, first hit $3 budget cap and returned INCOMPLETE — continuation via spawn-specialist.py --context-dossier because agentctl dispatch has no continuation channel); thinker plan review x2

## Self-critique of the agent system
SI stopped at tool+knowledge (GC script + leaf) without proposing the invocation point (hook wiring, backend teardown verb) — the named CLAUDE.md failure mode 'propose the structural form yourself'; user had to push twice ('why no autonomous cleanup', 'why text not code'). Quality rated 3/5 by user for exactly this gap.
