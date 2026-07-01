# Cross-session filesystem-scope isolation

Parallel Claude Code sessions share one working tree (a git checkout or an arc
mount). Without coordination, one session's uncommitted edits get attributed to,
staged by, or clobbered by another. Protection used to be purely reactive — a
manual playbook (detect via `git status`, isolate via `git worktree add` off a
pinned SHA, recover via cherry-pick). This subsystem makes the *detection*
deterministic: each live session records the filesystem scope it is touching, and
an online detector fires when two live sessions overlap.

The design law is **isolate, not serialize**: concurrent sessions keep running,
each in its own working tree / mount; integration happens only at the
land/commit point. The mechanism separates the **rule part** (session id, cwd,
touched paths, liveness, path overlap — all decidable from observable inputs, so
they live in code) from the **perception part** (whether to accept an offered
isolation — the model's call).

## Components

| Part | Where | Role |
|---|---|---|
| **A — scope registry** | `scripts/session_scope/registry.py` | One JSON record per session under `~/.claude/agentctl/scopes/<session>.json`: `{heartbeat_ts, cwd, repo_root, vcs, touched_paths[]}`. Pure module; the clock is injected. |
| **A — scope-track hook** | `scripts/hook-scope-track.py` (PostToolUse Edit\|Write + Bash) | Heartbeats the session and accumulates touched paths. Non-blocking. |
| **B — conflict detector** | `scripts/session_scope/detector.py` | Pure path-prefix overlap + severity classification over the registry records. VCS-agnostic. |
| **B — conflict hook** | `scripts/hook-scope-conflict.py` (PreToolUse Edit\|Write) | On a write, asks the detector whether the target overlaps another **live** session's scope, then denies / warns / allows. |
| **C — isolation router** | `session-isolate.sh` (+ `project_entry` backends) | Routes a contended task into its own git worktree / arc mount. *(Later slice — PR2/PR3.)* |

## How the conflict hook decides

`hook-scope-conflict.py` runs on every `Edit`/`Write`, **after** the plan-approval
gate (`hook-state-gate.py`). It resolves the target to a realpath, loads all scope
records, and asks `detector.detect_conflicts` whether the target overlaps a path
already held by **another live session** (liveness = heartbeat within 30 min):

- **block** — the target is a *gated* path (a production file governed by the
  coordination engine, per `agentctl/exempt_paths.is_gated_path`) already held by
  another live session. The hook emits a PreToolUse `permissionDecision: deny`
  naming the holding session and pointing at `session-isolate` for remediation. A
  hard block is reserved for this case, because it is the one where a second
  writer would corrupt a path the engine itself governs.
- **warn** — a non-gated held path (e.g. a data / text file). The hook prints a
  loud advisory offering isolation but **allows** the write. Warning-not-blocking
  is what preserves *isolate-not-serialize*: two sessions that genuinely need the
  same tree isolate into separate worktrees/mounts rather than being serialized.
- **allow (silent)** — no other live session overlaps: a single session, or two
  sessions in distinct worktrees / mounts whose paths do not overlap. The
  single-session flow is completely unchanged.

The detector is **VCS-agnostic** because it reasons over normalized filesystem
paths alone, never a VCS's own diff/status. A git working tree and an arc mount
are both just directories at this layer; two sessions rooted in physically
distinct worktrees / mounts naturally produce non-overlapping paths, which is
exactly why isolating a task stops the detector from firing again.

## Fail-open guarantee

Every hook in this subsystem is strictly fail-open: malformed stdin, a missing
registry, or any internal error falls through to *allow* (exit 0). A hook crash
can never wedge a tool call. The scope-track hook additionally never emits a
`permissionDecision` at all.

## Remediating a conflict — isolate

When the hook blocks or warns, the remedy is to move the contended task into its
own workspace rather than fight over the shared tree:

```bash
scripts/session-isolate.sh <task-name>   # git worktree / arc mount (later slice)
```

Integration back to the shared branch happens at the land point (`land-on-main.sh`
for git). *(The router and the arc backend are subsequent slices; this page will
gain their step-by-step flow when they land.)*

## See also

- Memory leaf: `memory-global/leaves/system-knowledge/cross-session-scope-isolation.md`
- Reactive playbook these leaves capture (superseded by the deterministic path):
  `memory-global/leaves/experience/2026-06-29-resume-paused-task-isolated-worktree-pinned-sha.md`
  and the two `2026-06-30-*shared-tree*` leaves.
