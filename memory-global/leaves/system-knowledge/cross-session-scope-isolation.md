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
- **Router (Component C)** — `session-isolate.sh` + `project_entry` backends: routes a contended task into its own worktree/mount. **Backend-blind** — resolves a backend *name* through `project_entry/registry.sh` (built-ins first, then `${CLAUDE_PROJECT_PLUGIN_DIR:-$HOME/.claude/project-entry-plugins}/backends/<name>.sh`) and calls one contract (`backend_detect`/`backend_ensure_workspace`/`backend_compose`). Core ships only the built-in `backends/git.sh` (git worktree at `<repo>-<name>`); arc is Yandex-specific and installed as a **machine-local plugin** `backends/arc.sh` (arc mount at `<main-mount>_<name>` sharing the main mount's `--object-store`, per the `using-arc-multiple-mounts` skill) — a Core built-in `arc.sh` would SHADOW that plugin (registry is built-in-first), so Core must not carry one (guarded by the `test_no_core_builtin_arc_backend` test in `scripts/tests/test_backend_arc.py`). On a machine without the arc plugin, `session-isolate.sh` degrades to the git default. Both backends honor `CLAUDE_DRY_RUN` (report the would-be workspace, mutate nothing) via a `*_BIN` stub seam. Two arc mounts are distinct directories, so the same path-prefix overlap logic separates them — the detector needs no arc-specific branch dimension.

The conflict hook's three outcomes: **block** (deny) only for a gated path already held by another live session — the case where a second writer would corrupt an engine-governed path; **warn** (loud stdout advisory, allow) for a non-gated held path; **silent allow** for a single session or two sessions in distinct worktrees/mounts. Liveness = heartbeat within 30 min. Every hook is **fail-open**: malformed stdin / missing registry / any error → allow (exit 0); a hook crash can never wedge a tool call.

Delivery is partitioned: **PR1** = registry (A) + detector (B) + both hooks + docs (the first independently-shippable slice — detection); **PR2** = the git isolation router (C). The arc backend is **not** a Core deliverable — it ships as a machine-local plugin in the Yandex storage tree, not in this repo.

## See also

- Ops doc: `docs/operations/cross-session-scope-isolation.md`
- Reactive playbook this supersedes: `memory-global/leaves/experience/2026-06-29-resume-paused-task-isolated-worktree-pinned-sha.md` and the two `2026-06-30-*shared-tree*` leaves.
