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
| **A — scope registry** | `scripts/session_scope/registry.py` | One JSON record per session under `~/.claude-agent/agentctl/scopes/<session>.json`: `{heartbeat_ts, cwd, repo_root, vcs, touched_paths[], pid}`. Pure module; the clock and the liveness probe are injected. |
| **A — scope-track hook** | `scripts/hook-scope-track.py` (PostToolUse Edit\|Write + Bash) | Heartbeats the session, accumulates touched paths, and records the durable session process's pid (resolved once per session via an age-based ancestor walk — see `session_pid()`). Non-blocking. |
| **B — conflict detector** | `scripts/session_scope/detector.py` | Pure path-prefix overlap + severity classification over the registry records. VCS-agnostic. |
| **B — conflict hook** | `scripts/hook-scope-conflict.py` (PreToolUse Edit\|Write) | On a write, asks the detector whether the target overlaps another **live** session's scope, then denies / warns / allows. |
| **C — isolation router** | `scripts/session-isolate.sh` (+ `project_entry` backends) | Routes a contended task into its own workspace by reusing `project_entry`'s workspace-backend contract (`backend_ensure_workspace`), then re-registers the session's scope at the new root. The built-in git backend (`backends/git.sh`) and a machine-local plugin backend such as arc (registered at `${CLAUDE_PROJECT_PLUGIN_DIR:-…}/backends/arc.sh`), resolved by name — the router is backend-blind. |

## How the conflict hook decides

`hook-scope-conflict.py` runs on every `Edit`/`Write`, **after** the plan-approval
gate (`hook-state-gate.py`). It resolves the target to a realpath, loads all scope
records, and asks `detector.detect_conflicts` whether the target overlaps a path
already held by **another live session** (liveness — see § Liveness below):

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

## Liveness — three layers, TTL last

A scope record used to be "live" on heartbeat age alone (within `LIVE_TTL_S` =
30 min), so a session that died without cleaning up — most commonly a spawned
specialist killed by budget exhaustion — kept blocking every overlapping writer
for the rest of the window (the *ghost scope* defect, recorded in
`memory-global/leaves/experience/2026-07-02-dead-spawn-scope-file-blocks-next-writer.md`).
Liveness is now decided in three layers, ordered by how directly each observes
the session:

1. **Deregistration at spawn exit** (primary, for supervised spawns).
   `spawn-specialist.py` removes the child session's scope file in the same
   `finally` that tears down the process tree, on every exit path — normal
   completion, budget death, kill. The child's session id comes from the
   result JSON's `session_id`, falling back to the transcript filename stem;
   a deregistration failure is logged to stderr and never alters the spawn's
   own exit code. The common case therefore never reaches the layers below.
2. **Pid probe** (for sessions that die without a supervisor — including the
   supervisor itself). When a record carries a pid, `registry.live_pid_check`
   probes it with `os.kill(pid, 0)` semantics (EPERM counts as alive). The
   probe only **narrows** what heartbeat freshness allows through: a record
   with no pid (legacy, or pid capture unresolved) keeps heartbeat-only
   semantics, so a false verdict degrades to the old behavior rather than
   suppressing detection for a live session.
3. **Heartbeat TTL** (fallback outer bound). `LIVE_TTL_S` still expires
   records whose pid was never captured or was reused by an unrelated
   process — it is no longer the primary liveness signal, just the backstop.

## Fail-open guarantee

Every hook in this subsystem is strictly fail-open: malformed stdin, a missing
registry, or any internal error falls through to *allow* (exit 0). A hook crash
can never wedge a tool call. The scope-track hook additionally never emits a
`permissionDecision` at all.

## Remediating a conflict — isolate

When the hook blocks or warns, the remedy is to move the contended task into its
own workspace rather than fight over the shared tree:

```bash
scripts/session-isolate.sh <task-name>
```

This reuses `project_entry`'s workspace-backend contract — it does **not**
invent a new isolation mechanism:

1. Resolve the workspace backend name (`$CLAUDE_WORKSPACE_BACKEND` override, else
   `project_entry/detect_backend.py`, else the git default) and `source` its
   `backends/<name>.sh` via `project_entry/registry.sh`.
2. Call that backend's `backend_ensure_workspace <name> <branch>` — for git, a
   `git worktree add` at `<repo-parent>/<repo>-<name>` on a new branch, or a
   no-op reuse if that worktree already exists. Under `CLAUDE_DRY_RUN` no
   mutating git call is made; the would-be worktree path is still reported.
3. Re-register this session's scope (`session_scope.registry.set_context`) at
   the new workspace root, read from `$CLAUDE_SESSION_ID`. This is what makes
   the isolation take effect *immediately*: two disjoint worktree roots never
   path-overlap, so `detector.detect_conflicts` stops flagging this session
   against the holder on the very next write — no heartbeat cycle needed to
   wait out. Re-registration is local bookkeeping under
   `~/.claude-agent/agentctl/scopes/`, not a mutation of the task's tree, so it runs
   even under `CLAUDE_DRY_RUN`.
4. Print the new workspace directory (as the final stdout line, so it can be
   captured with `project_dir="$(scripts/session-isolate.sh <task-name>)"`)
   plus a continuation instruction to `cd` there and continue the task.

The router is **backend-blind**: it resolves a backend *name*
(`$CLAUDE_WORKSPACE_BACKEND` override, else `detect_backend.py`, else git) through
`project_entry/registry.sh` and calls the same three contract functions
regardless of which backend answers. The built-in git worktree backend
(`backends/git.sh`, the org-neutral default Core ships) and a machine-local plugin
backend such as the arc mount backend (registered at
`${CLAUDE_PROJECT_PLUGIN_DIR:-$CLAUDE_AGENT_HOME/project-entry-plugins}/backends/arc.sh`)
are drop-in implementations of that one contract, so a new backend attaches with no
change to `session-isolate.sh`.

#### The arc backend (a machine-local plugin)

arc is Yandex-specific, so Core ships no arc backend: it is installed as a
machine-local plugin at `${CLAUDE_PROJECT_PLUGIN_DIR:-…}/backends/arc.sh` and
resolved by name through `registry.sh` (which searches the Core built-ins first,
then the plugin dir). On a machine without that plugin, `session-isolate.sh`
degrades to the git default. Where the plugin is installed, arc has no `worktree`
command; its equivalent is a second `arc mount` that shares the main mount's
`--object-store` (the `using-arc-multiple-mounts` skill). So on arc,
`backend_ensure_workspace <name> <branch>`:

1. Reads `arc mount --list --json`, finds the mounted entry that is an ancestor of
   the anchor directory (`$CLAUDE_WORKSPACE_ROOT` when set, else `$PWD`), and
   extracts its `mount` path (`MAIN_MOUNT`) and `object-store`.
2. Targets a new mount at `<MAIN_MOUNT>_<name>` — reused as-is if a mount already
   exists there, never recreated.
3. Otherwise creates it: `mkdir` the path, `arc mount -m <path> --object-store
   <store> --override-object-store`, then `arc checkout -b <branch>` inside it.

Every `arc` call goes through the `ARC_BIN` seam (default `arc`) so the tests can
stub it, and under `CLAUDE_DRY_RUN` no mount-creating command runs and no
directory is made — the would-be mount path is still reported so the detector can
be shown the isolated root. Two arc mounts are physically distinct directories, so
the same path-prefix overlap logic that separates two git worktrees separates two
arc mounts — the detector needs no arc-specific branch dimension. The mount, like a
git worktree, already carries the repo's `.claude` tree, so `backend_compose` is a
no-op.

### Landing back

Integration back to the shared branch happens at the land point, **not** by
merging the isolated worktree back in place. Once the isolated task is done and
its result is staged there:

```bash
cd <isolated-project-dir>
git add <files>
scripts/land-on-main.sh -C <isolated-project-dir> -m "<message>"
```

`land-on-main.sh` applies the staged patch onto `origin/main` through its own
isolated detached worktree and pushes (retrying on a rebase if the remote
moved) — it never touches the *caller's* branch, worktree, or index, so this
step is itself safe to run from inside the isolated workspace `session-isolate`
created.

## The canon-read-only guard

A generalization of the earlier serving-checkout-off-main guard. Both
canonical checkouts on a machine — the Core repo's primary/serving checkout
(`~/claude-agent-instructions`) and, where installed, the arc anchor mount
(`~/task-mounts/main`) — are read-only to a session's own edits. They are
mutated **only** by `pull`; all code/memory edits flow through a separate
linked worktree (Core) or second arc mount, then land back.

`scripts/hook-guard-canon-readonly.py` (PreToolUse, renamed from
`hook-guard-serving-checkout-offmain.py`) enforces this. Its predicate: the
target of an `Edit`/`Write`/`git commit` resolves (realpath-normalized) under
a canonical root **on any branch** — there is no on-`main` carve-out and no
`memory-global/` exemption (both existed on the old serving-checkout guard
and are dropped here, decision D1). Canon roots are read from a
machine-local list, `~/.claude-agent/canon-roots.local` (via
`config_root.canon_roots_file()`), so the Core repo stays org-neutral — arc
canon is opt-in per install, not hardcoded. The guard still fails open for a
linked worktree, a second arc mount, personal auto-memory under
`~/.claude-agent`, `/tmp`, and `git pull` itself; its deny message points at
`session-isolate.sh` to relocate.

**Operational consequence:** recording an experience or self-improvement leaf
into `memory-global/` now requires a worktree — the primary Core checkout
denies that write same as any other — then land, per § Landing back above.

Two once-per-session nudges layer on top (offers, never automatic):

- **D3 — worktree-rebase-onto-canon offer.** When a linked worktree is behind
  the canonical root, the session is offered a rebase onto canon. The check
  fetches first, then counts commits behind, so the offer reflects the true
  remote state rather than a stale local view.
- **D2/R4 — in-canon relocation offer.** When a session's cwd resolves inside
  a canonical root, it is offered relocation via `session-isolate.sh` (the
  same router § Remediating a conflict — isolate uses) rather than left to
  hit the guard's deny on its first edit.

## See also

- Memory leaf: `memory-global/leaves/system-knowledge/cross-session-scope-isolation.md`
- Reactive playbook these leaves capture (superseded by the deterministic path):
  `memory-global/leaves/experience/2026-06-29-resume-paused-task-isolated-worktree-pinned-sha.md`
  and the two `2026-06-30-*shared-tree*` leaves.
