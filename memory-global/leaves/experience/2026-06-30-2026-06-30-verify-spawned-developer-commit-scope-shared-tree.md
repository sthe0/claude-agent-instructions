---
name: 2026-06-30-2026-06-30-verify-spawned-developer-commit-scope-shared-tree
description: Difficulty (manager side) — a spawned developer working in a shared instructions-repo tree that carries another session's untracked WIP runs the FULL-TREE validator (verify-all.py without --staged), which globs the whole tree and fails on the foreign untracked scripts; lacking the foreign-ownership context, the subagent 'fixes' them (chmod + README + git add) and absorbs them into its commit. The executor-side leaves (diagnose-ownership) do not catch this because the subagent never doubts its own change. The manager catch is to diff the developer's committed file set against the plan's declared 'exactly N files' invariant and back out anything foreign before the resolution gate.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "fedor.solovyev (live this session)"
refs: [2026-06-30-shared-tree-suite-failure-wrong-ownership-attribution.md, 2026-06-30-validator-tightening-newly-enforces-latent-violations.md, 2026-06-29-org-portable-core-internal-coupling-opt-in.md]
created: 2026-06-30
last_verified: 2026-06-30
---

# Verify a spawned developer's committed file set against the plan's exactly-N-files invariant in a shared tree

## Difficulty
A spawned developer in a shared tree commits another session's untracked WIP because its full-tree validation failed on foreign files; the plan said 'exactly 9 files' but the commit had 12.

## Order & criterion
After a spawned developer returns COMPLETED with a commit: (1) git show --stat HEAD vs the plan's named file list; (2) any extra file -> is it in my changeset/plan; (3) if foreign, git reset --soft HEAD~1 + mixed reset, re-add ONLY the plan files by explicit path, re-commit through the --staged gate (which, unlike full-tree mode, does not flag foreign untracked files).

**Acceptance check:** git show --stat HEAD lists exactly the plan's N files; git status shows the foreign WIP back to untracked/unstaged; pre-commit verify-all --staged passes.

## Contexts

### 2026-06-30 — initial
- Where it arose: claude-agent-instructions shared tree; si-dev-task-solving self-improvement (9-file plan); a parallel enforce-subdifficulty session's untracked enter-task.sh/claude-launchers.sh/project_entry/ were absorbed by the spawned developer.
- Working plan: Spawned one developer for the whole 6-stage plan; on COMPLETED it reported 12 committed files (9 + 3 foreign). Diffed against the plan invariant, undid the commit, re-staged exactly 9, confirmed the --staged gate passes (foreign untracked files only break the full-tree scan), re-committed with the trailer.

## Cost
~1 detour: OD on the plan-invariant mismatch, hook investigation, reset + re-commit.

## Self-critique of the agent system
The spawn constraints told the developer not to touch the 4 NAMED foreign files but did not forbid running the full-tree validator or committing OTHER untracked files; a tighter constraint ('git add only these explicit paths; never run verify-all without --staged in a shared tree') would have prevented the entanglement at the source.

> Deterministic mechanism (2026-07-01): the shared-tree collision this leaf diagnoses is now excluded up front by the deterministic cross-session scope subsystem — a session-scope registry + online conflict detector (warn/block on a live cross-session overlap) + a backend-blind `session-isolate.sh` router (git worktree / arc mount) that isolates rather than serializes. See `memory-global/leaves/system-knowledge/cross-session-scope-isolation.md` and `docs/operations/cross-session-scope-isolation.md`.
