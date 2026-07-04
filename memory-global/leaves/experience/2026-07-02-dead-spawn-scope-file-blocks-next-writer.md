---
name: 2026-07-02-dead-spawn-scope-file-blocks-next-writer
description: A spawned specialist session that dies (budget exhaustion, kill) leaves its scope registration under ~/.claude/agentctl/scopes/<session>.json; hook-scope-conflict.py judges liveness by heartbeat age alone (LIVE_TTL_S=1800), so for up to 30 min every writer overlapping the same files is denied. Bit three times in one task: two dispatched developers hit predecessors' ghost scopes (one burned its whole $3 budget in a denial spiral, zero edits) and the manager's own Edit was denied by a dead spawn's scope. Mitigation: rm dead spawn sessions' scope files before dispatch and before manager edits (encoded as plan conditions in r1). Structural fix LANDED (task ghost-scope-fix, commits c743452+b1413a3): spawn-specialist deregisters the child's scope on every exit path, hook-scope-conflict probes pid liveness, TTL demoted to backstop.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "Fedor (2026-07-02: 'да на все три вопроса')"
refs: [scripts/hook-scope-conflict.py, docs/operations/cross-session-scope-isolation.md]
created: 2026-07-02
last_verified: 2026-07-04
---

# Ghost scope: a dead spawn session's scope file blocks the next writer for the full heartbeat TTL

## Difficulty
Desired: the scope registry reflects only live sessions, so writers are blocked only by genuinely concurrent work. Actual: dead spawn sessions' scope files persist up to LIVE_TTL_S=1800s and deny all overlapping writers; one $3 developer spawn died entirely in the denial spiral before producing a single edit.

## Order & criterion
Execute plan agentctl-gate-quality stage-by-stage via dispatched developer spawns sharing the instructions-repo working tree

**Acceptance check:** Dispatched spawns and manager edits proceed without scope-conflict denials from sessions that are no longer running; no spawn budget burned on denial spirals

## Contexts

### 2026-07-02 — initial
- Where it arose: ~/claude-agent-instructions shared tree; agentctl dispatch -> spawn-specialist.py; hook-scope-conflict.py + scope registry ~/.claude/agentctl/scopes/
- Working plan: Interim (applied): before each dispatch and before manager edits on contested files, rm the dead spawn sessions' scope files under ~/.claude/agentctl/scopes/ (verify the session is dead via its transcript/process first). Structural (planned as follow-up task): (1) spawn-specialist.py deregisters the spawn session's scope on exit, success or failure; (2) hook-scope-conflict.py checks process liveness (pid probe) in addition to heartbeat age before treating a registration as live.


### 2026-07-02 — structural fix landed (task ghost-scope-fix)
- Where it arose: ~/claude-agent-instructions commits c743452 + b1413a3; plan ~/.claude/plans/ghost-scope-fix.toml; docs/operations/cross-session-scope-isolation.md § Liveness
- Working plan: Resolved structurally, three liveness layers: (1) spawn-specialist deregisters the child's scope in the same finally as kill_tree (all exit paths; child id from result-JSON session_id else transcript stem; failures stderr-logged, never alter marker/rc); (2) hook-scope-conflict narrows heartbeat freshness with an os.kill(pid,0) probe (EPERM=alive) — hook-scope-track records the durable session pid once per session via an age-based ancestor walk (first ancestor measurably older than the per-call hook process; verified on a real hook invocation), no-pid records keep heartbeat-only semantics so a false verdict only degrades to old behavior; (3) LIVE_TTL_S demoted to backstop. Sub-difficulty during the fix: [[2026-07-02-spawn-sandbox-excludes-declared-stage-material]] — stage-2 spawn died on budget ($3.23) burning 13 permission denials on plan material outside its scripts/ sandbox; manager salvaged the delivered code, wrote the doc section, verified (147 scope/spawn + 1149 full).


### 2026-07-03 — live parent scope blocks its own spawned specialists (lineage blindness)
- Where it arose: si-mechanize-gates stage 3, session 759b0d4b, 2026-07-03
- Working plan: Inverse direction of the same registry gap: not a DEAD child's ghost scope, but the LIVE parent's own scope. After the manager merged the stage-2 branch in-thread, hook-scope-track put scripts/install-reminder-hooks.sh into the manager session's touched_paths; every subsequently dispatched developer (fresh session id, no lineage link) was denied Edit on that file by hook-scope-conflict and burned its full $3 budget — twice ($6 total, both died error_max_budget_usd with the work committed but the marker unsent). The registry keys scopes by bare CLAUDE_SESSION_ID with no parent-child lineage, so the coordinator blocks its own specialists on exactly the files the plan tells them to edit. Workaround used: complete the conflicting stage in-thread. Structural fix queued for backlog: spawn-specialist passes parent session id; hook-scope-conflict exempts a writer whose scope chain includes the holder.

### 2026-07-04 — session-limit death mode + forensic recovery of a dead spawn's finished work
- Where it arose: fix-agentctl-core-defects stages 4-5, session ce4f6071, 2026-07-04
- Working plan: Two additions confirmed twice this task. (1) Death mode taxonomy: a spawned specialist can die from the SESSION LIMIT (harness kills the process; exit marker absent, transcript ends mid-work) — distinct from budget exhaustion (clean ESCALATE/INCOMPLETE marker). Session-limit death leaves committed work + a live scope claim behind. (2) Forensic recovery beats respawn: before re-spawning, read the dead spawn's transcript tail (~/.claude-agent/ spawn logs / git log in its worktree) — twice the work was actually FINISHED and committed; recovery = verify the commits against the stage's expected result image, release the stale scope claim (agentctl scope release / delete the scope file after backup, e.g. .bak-stage4-release), record-result from the evidence. A respawn would have redone paid-for work and risked conflicting edits.
## Common core & variations
**Common:** Dead-session scope files no longer require manual rm before dispatch: deregistration covers supervised exits, the pid probe covers unsupervised deaths within the same heartbeat window

**Variations:** context 1 (defect) needed the manual-rm mitigation encoded as plan conditions; context 2 (fix) retires that mitigation but surfaced a new dispatch trap ([[2026-07-02-spawn-sandbox-excludes-declared-stage-material]]) — stage material outside the spawn sandbox burns the spawn's budget on permission denials.

## Cost
Task spawn total $11.10 across 4 dispatched stages (agentctl resolve); of that, $3.01 was pure ghost-scope loss (one developer spawn died in the denial spiral with zero edits), plus ~2 manager diagnosis/replan cycles.
