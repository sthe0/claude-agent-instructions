---
name: 2026-09-03-hygiene-sweep-ownership-check-argument-order-bug
description: Building a daily auto-cleanup sweep (agent-hygiene-sweep.sh) with an ownership guard (is_owned) meant to skip deletion when a candidate path belongs to a live/recently-active session. The guard called a containment helper at_or_inside(container, candidate) with the arguments swapped: at_or_inside(candidate_path, session_dir) instead of at_or_inside(session_dir, candidate_path). This made ownership protection fire only when a session's directory was nested inside the candidate file (essentially never), so in the normal real case -- session cwd is a PARENT of the candidate file -- the guard silently failed open and would have deleted files belonging to a live/recently-heartbeated session.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "fedor.solovyev"
refs: [/home/the0/bin/agent-hygiene-sweep.sh]
created: 2026-09-03
last_verified: 2026-09-03
---

# Acceptance-judge pushback on shallow verification surfaced a swapped-argument ownership-check bug

## Difficulty
A destructive-action ownership/containment guard had its two arguments to a directional helper (container, candidate) passed in swapped order, so the protection only worked in the inverted (near-impossible) case and silently no-oped in the normal case. First-pass verification ("the script ran, A1/A2 counts were 0") could not have caught this -- zero deletions is consistent with both 'no candidates existed' and 'the guard is broken and nothing happened to trigger it'. The bug surfaced only because the acceptance-judge gate (agentctl's fail-closed acceptance_judge on record-result) explicitly rejected a shallow 'it ran and printed counts' observation and demanded proof that grace-period enforcement, registry-based ownership checking, and state pruning were actually exercised -- forcing a genuine synthetic end-to-end test (fake owned + unowned candidates, fake fresh-heartbeat scope record, backdated grace-period timestamps) that immediately showed both the owned and unowned candidate being deleted.

## Order & criterion
User asked for an automated daily hygiene sweep that auto-deletes only unambiguously-safe artifacts (broken symlinks, empty stubs, old scratch files) and NEVER deletes anything whose ownership can't be mechanically confirmed live/recent (must check the session-scope registry, not just 'currently unmounted').

**Acceptance check:** Verified via constructed synthetic candidates for every detector category (not just reading the code or running against real production state, which had 0 hits for A1/A2): an unowned expired candidate must be deleted, an owned candidate (protected via a fresh heartbeat record whose cwd is a real parent of the candidate path) must NOT be deleted. Re-ran end-to-end after the fix to confirm both outcomes.

## Contexts

### 2026-09-03 — initial
- Where it arose: Any script/tool that implements an ownership, ACL, or path-containment guard ("is this path inside/owned-by X") ahead of a destructive action (delete, revoke, overwrite). The generalizable check: when a helper's contract is asymmetric in its arguments (container, candidate), don't just confirm the call exists in code -- construct a concrete case where container is a real ancestor of candidate and confirm the guard actually fires, because a swapped-argument bug produces a guard that is syntactically present, always returns a value, and never audibly errors, yet fails open in the common case.
- Working plan: /home/the0/.claude-agent/plans/agent-hygiene-sweep.toml

## Cost
From the quality ledger (`~/.local/log/claude-task-quality.jsonl`, session `614c0052-3c94-4f3c-8ea0-e2ef26ac3643`): quality 5/5 (user-confirmed); 1 stage, 0 failed stage-results, 0 replans, 0 difficulty records, 0 spawns (in-thread work) — the acceptance-judge pushback (see Difficulty) happened without triggering a formal DIAGNOSING cycle. ~40 min active wall-clock against a 10 min estimate (4x), 7 interactions; no per-spawn dollar cost recorded (no spawns to attribute).

## Self-critique of the agent system
The bug would have shipped if the acceptance-judge gate had not pushed back on a shallow observation -- worth trusting that gate's 'revise' verdicts as genuine content critique before reaching for an override, which is what happened here and is the reusable lesson.
