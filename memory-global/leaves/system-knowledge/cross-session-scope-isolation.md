---
name: cross-session-scope-isolation
description: "Deterministic cross-session filesystem-scope isolation: a session-scope registry + online conflict detector + PreToolUse hook that denies/warns when two LIVE sessions overlap the same tree, replacing the reactive git-status/worktree playbook. Isolate, not serialize."
type: reference
schema: leaf/v1
created: 2026-07-01
last_verified: 2026-07-01
---

# Cross-session filesystem-scope isolation

## Difficulty

Desired — parallel Claude Code sessions that share one working tree (git checkout or arc mount) never collide over it. Actual — one session's uncommitted edits get attributed to, staged by, or clobbered by another, and the only protection is a reactive, perception-heavy manual playbook (detect via `git status`, isolate via `git worktree add` off a pinned SHA, recover via cherry-pick) captured in three experience leaves. There was no session→scope registry, no liveness signal, and no online conflict detector — so a collision was caught only after it had already happened.

## Guidance

The subsystem determinizes the **detection** half. Each live session records the filesystem scope it touches; an online detector fires when two *live* sessions overlap. Design law: **isolate, not serialize** — concurrent sessions keep running, each in its own worktree/mount; integration happens only at the land/commit point.

Parts (all under `scripts/`):

- **Registry (Component A)** — `session_scope/registry.py`: one JSON record per session at `~/.claude/agentctl/scopes/<session>.json` = `{heartbeat_ts, cwd, repo_root, vcs, touched_paths[]}`. Pure module, injected clock, atomic writes; a missing/corrupt file is treated as absent. Wired by `hook-scope-track.py` (PostToolUse Edit|Write + Bash) — heartbeat + touched-path accumulation, non-blocking.
- **Detector (Component B)** — `session_scope/detector.py`: `path_overlaps` (normalized ancestor-or-equal prefix test — **VCS-agnostic**, works identically for a git tree path and an arc mount path), `detect_conflicts` (over OTHER live sessions only; distinct roots never overlap → isolate-not-serialize is automatic), `classify_severity` (`is_gated_path` from `agentctl/exempt_paths` is the ONLY block designation — no hardcoded repo path). Wired by `hook-scope-conflict.py` (PreToolUse Edit|Write, registered AFTER `hook-state-gate.py`).
- **Router (Component C)** — `session-isolate.sh` + `project_entry` git/arc backends: routes a contended task into its own worktree/mount. *(Later slice.)*

The conflict hook's three outcomes: **block** (deny) only for a gated path already held by another live session — the case where a second writer would corrupt an engine-governed path; **warn** (loud stdout advisory, allow) for a non-gated held path; **silent allow** for a single session or two sessions in distinct worktrees/mounts. Liveness = heartbeat within 30 min. Every hook is **fail-open**: malformed stdin / missing registry / any error → allow (exit 0); a hook crash can never wedge a tool call.

Delivery is partitioned: **PR1** = registry (A) + detector (B) + both hooks + docs (the first independently-shippable slice — detection); **PR2** = the git isolation router (C); **PR3** = the arc backend.

## See also

- Ops doc: `docs/operations/cross-session-scope-isolation.md`
- Reactive playbook this supersedes: `memory-global/leaves/experience/2026-06-29-resume-paused-task-isolated-worktree-pinned-sha.md` and the two `2026-06-30-*shared-tree*` leaves.
