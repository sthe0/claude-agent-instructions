---
name: 2026-07-31-second-agent-cli-launcher-seam
description: Adding a second agent CLI (e.g. cursor-agent) to an existing task-entry launcher (claude-personal) by copying the dispatch duplicates enter-task/opening/auth logic and the copies drift. Extract behind a backend descriptor table first; register each CLI as a thin table of values. Also: a live smoke from the Core checkout must pin the workspace backend — ambient detection may pick arc and fail outside task-mounts.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "1"
refs: [scripts/project_entry/agent-dispatch.sh, scripts/cursor-launchers.sh, 2026-07-01-isolate-tool-config-root-per-root-auth]
plan_file: /home/the0/.claude-agent/plans/cursor-agent-launchers.toml
created: 2026-07-31
last_verified: 2026-07-31
---

# Extract a task-entry launcher behind a backend descriptor before adding a second agent CLI

## Difficulty
A second agent CLI needs the same task-entry surface (ticket/name → isolated workspace → opening prompt → launch). Copying the existing launcher duplicates the dispatch; the copies then diverge. Separately, a live smoke run from the Core git checkout can fail because enter-task's ambient workspace backend (arc) does not see the Core tree as a project mount.

## Order & criterion
1) Enumerate every agent-specific site in the existing launcher (command prefix, binary, auth namespace, config-root env, plain-launch twin / See-also). 2) Extract the dispatch into a shared library parameterized by a descriptor of those sites. 3) Re-register the first CLI against it and prove behaviour preservation with the UNCHANGED hermetic suite. 4) Add the second CLI as registration-only + hermetic cases including negatives (no foreign config env leaked; usage prose does not name the other CLI). 5) For a Core-checkout live smoke, pin CLAUDE_WORKSPACE_BACKEND=git (overridable) rather than trusting ambient detection.

**Acceptance check:** Hermetic suite green for both backends with the first CLI's cases unmodified; second launcher file contains no enter-task/opening logic; live -p from Core echoes the resolved workspace path; delivery on origin/main.

## Contexts

### 2026-07-31 — 2026-07-31 — cursor-personal/cursor-task on shared dispatch
- Where it arose: User asked for cursor-personal analogous to claude-personal. Plan cursor-agent-launchers.toml; commits 9e13e31 (extract), c049038 (cursor backend), 382dffe (live-verify git pin). Thinker review of the first plan version found a FIFTH descriptor site the initial four-value claim missed (plain-launch twin in usage See-also).
- Working plan: Backend seam with five descriptors (_backend_prefix/bin/auth_ns/config_env/plain_cmd); agent-dispatch.sh owns the body; claude-launchers.sh and cursor-launchers.sh are registration only; cursor personal profile empty (already-logged-in account); CURSOR_CONFIG_DIR exists but deliberately unused; live-verify defaults CLAUDE_WORKSPACE_BACKEND=git.

## Cost
Cursor Task subagents for developer x2 + code-reviewer x2 + thinker reviews; wall-clock ~2h; user interventions: design Qs, plan approval, two push gates, resolution 5/5.

## Self-critique of the agent system
Initial plan claimed a four-value descriptor; independent thinker review found the fifth (See-also plain twin) — the plan's own confidence=medium was the right signal. Stage-4 verify originally checked only static presence until review forced the live axis into verify_command. Edited the live-verify script briefly in the canon checkout before branching — should have opened a worktree first per canon-readonly.
