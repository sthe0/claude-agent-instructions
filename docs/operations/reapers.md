# Reapers: stale-residue cleanup under one set of rules

Processes that die hard (SIGKILL, OOM, a crashed harness) leave residue behind: a worktree directory, a state file, a cache. A cleanup that runs only on graceful exit cannot reap it, so a periodic reaper has to. Deleting is irreversible, and the rules that make it safe (do not touch what a live session owns, never act on a guess, say what was removed, survive a bug in one reaper) are the same for every kind of residue. They live **once**, in the runner (`scripts/reaper/runner.py`); a reaper only proves what it can about its own kind of residue.

`scripts/hook-reaper.py` is the SessionStart entry.

## The reaper contract

A reaper is a Python module in one of the three layers below, defining:

| Name | Meaning |
|---|---|
| `NAME` | unique string; the identity used for override, `--only`, stamps and the log |
| `THROTTLE_HOURS` | minimum hours between real passes of this reaper; a finite number above 0 (default 24) |
| `scan(ctx) -> list[Verdict]` | one `Verdict(path, action, reason, report=False, tags=(), size=0)` per item it has an opinion on; `action` is `remove` or `keep`; `size` is the item's disk bytes when the reaper measured them (recorded in the kept snapshot). No side effect beyond best-effort bookkeeping that checks `ctx.dry_run` and `ctx.due` |
| `remove(path, ctx)` | delete one item the runner approved. Raise `RemovalRefused(reason)` (`reaper.contract`) when, judged again, the item must stay; raise anything else, or return `False`, when the removal failed |
| `summary(verdicts) -> str \| None` | optional; the text of the SessionStart notice, printed after each pass in which the reaper was due, and after a dry run (the `git-worktrees` reaper uses it for worktrees kept for over a week, and for branches origin lacks; `temp-residue` prints a multi-line report) |
| `upkeep(ctx) -> list[str]` | optional; work that is not a removal and must run on every trigger rather than on the throttle (the `git-worktrees` reaper backs up unpushed branches). Returns report lines; honours `ctx.dry_run`. Called **only** by `--upkeep-only`, never by a plain pass |
| `pressure_paths(ctx) -> list[str]` | optional; the paths whose free space this reaper can relieve. A reaper without it is never triggered by disk pressure (see [Disk pressure](#disk-pressure)) |
| `SESSION_SOURCES` | optional; a non-empty `frozenset` of SessionStart `source` values (`startup`, `resume`, `clear`, `compact`). A reaper that has it is due in a SessionStart pass only for a fresh interactive top-level start whose source is in the set (see [Session gate](#session-gate)). A module whose `SESSION_SOURCES` is not a non-empty frozenset of non-empty strings is skipped at discovery like any other malformed module |

`ctx` (`scripts/reaper/contract.py: ReapContext`) carries `now`, `dry_run`, `due`, `pressured`, `session`, `project_dir`, `deadletter_dir` and two ownership queries over the session-scope registry: `ctx.owned_path(path)` compares real paths against each record's working directory and repo root, `ctx.owned_session(session_id)` compares session ids (in the sanitized form both stores use as file names). Both count a session as live when its pid is alive or its heartbeat is under 24 hours old.

A plugin or project module is executed under a synthetic package name, so it must use **absolute** imports (`from reaper.contract import Verdict`).

## Layers and registration

Discovery reads three layers in this order; a later layer's reaper with the **same `NAME` replaces** the earlier one (a project can retune a Core reaper; a plugin can replace a built-in).

| Layer | Where | Who installs it |
|---|---|---|
| Core built-in | `scripts/reaper/builtin/*.py` | Core |
| Machine-local plugin | `${CLAUDE_REAPER_PLUGIN_DIR:-<config root>/reaper-plugins}/reapers/*.py` | a higher layer (an org, a personal machine); listed as the `reaper-plugins` seam in [org-portability.md](org-portability.md) |
| Project | `<project>/.claude/reapers/*.py`, project = `$CLAUDE_PROJECT_DIR`, else the working directory | the project, shared through its git |

Files starting with `_` are not reapers. A module that fails to import, calls `sys.exit`, or lacks `NAME` / `scan` / `remove` / a finite positive `THROTTLE_HOURS` is skipped with one `reaper: skipped <file>: <why>` line on stderr. The rest still scan and print, but that pass removes nothing: the skipped module's `keep` verdicts are missing. `python3 scripts/hook-reaper.py --list` prints `NAME layer file` for each reaper actually in force.

**Trust.** A project-layer reaper (`.claude/reapers/`) is Python executed at SessionStart from the project directory, with the same trust level as the project's own `.claude` hooks. A project reaper with the same `NAME` as a built-in replaces it, and with it the built-in's safety rules for that kind of residue; the runner's rules below still apply.

## What the runner guarantees

1. **Throttle.** Each reaper has its own stamp, `~/.local/state/claude-reaper/<NAME>.stamp`. A pass starts when at least one selected reaper is due; if none is, the runner prints `nothing due: within the throttle window or outside a gated reaper's session` to stderr and does nothing.
2. **Every reaper scans, due or not.** A reaper that is not due cannot remove anything, but its `keep` verdicts still count.
3. **Keep wins.** Verdicts are joined by real path. A path is removed only if no reaper says `keep` for it, for anything inside it, or for a directory containing it (compared by path component, so `/a/item` does not contain `/a/item-2`), a due reaper proposed `remove`, and then **once**, even when two reapers propose it.
4. **Log before removal.** Before each `remove`, one JSON line `{ts, reaper, layer, path, reason, trigger[, invocation_id]}` is appended to `<config root>/reaper/removed.jsonl`. `trigger` says how the pass started: `unit` when a reaper unit started the pass (`AGENT_REAPER_UNIT` and `INVOCATION_ID` both set; the latter is also recorded as `invocation_id`, which is absent otherwise), else `pressure` (`--on-pressure`), `manual` (`--dry-run`, `--force-run` or `--only`) or `hook` (SessionStart). After the attempt a second line `{ts, reaper, layer, path, outcome, detail, trigger[, invocation_id]}` records what happened: `outcome` is `removed` (`detail` is the verdict's reason), `refused` (the reaper raised `RemovalRefused`; `detail` is its reason) or `failed` (`detail` is the error). So the first kind of line counts attempts, not deletions; count deletions by `outcome == "removed"`. An attempt line with no outcome line means the pass died during that removal. The outcome line is best effort: when it cannot be written, the runner prints one stderr line and carries on.
5. **Kept snapshot.** After each live pass, for every reaper that was due and whose scan did not fail, `<config root>/reaper/kept/<NAME>.json` is replaced atomically (temporary file, then `os.replace`) with `{reaper, pass_time, trigger[, invocation_id], kept: [{path, reason, detail, bytes}][, <fields from the reaper's optional snapshot_extra()>]}`: every keep of the pass, including a remove another reaper vetoed and excluding what was removed. `reason` is a fixed slug, so a reader can count and group entries: the keep verdict's `reason=<slug>` tag when it has one, else `unclassified`; a remove another reaper vetoed is `blocked`, one the reaper refused when it judged the item again at removal time is `removal-refused` (`detail` is the refusal's reason) and one whose removal failed is `removal-failed`. `detail` is the human text (the verdict's reason, `kept by <other NAME>`, the error). `temp-residue` adds `unreadable_helpers_skipped`. It is a snapshot, not a log: a timer re-judges the same items on every pass. A dry run writes none. Writing it is best effort.
6. **Error isolation.** A `scan` exception cancels **every** removal of the pass (a reaper that cannot see cannot be outvoted by one that can), prints one stderr line and advances no stamp. The same cancellation applies when a module was skipped at discovery, and when a session-scope record could not be read (an owner may be missing; the registry is loaded strictly). A dry run then prints each `remove` as `KEEP ... (pass cancelled: <why>)`. A `remove` exception, or a `False` return, keeps that path, prints one stderr line, counts as an error of the pass and leaves that reaper's stamp where it was; other reapers carry on. `RemovalRefused` is not an error: the reaper judged the item again and found it must stay (it became held, fresh or no longer a candidate), so the path is kept with that reason and the stamp advances.
7. **SessionStart mode always exits 0**, whatever a reaper or the runner does.

## Modes

| Invocation | Effect |
|---|---|
| `hook-reaper.py` | SessionStart mode: throttled, real removals, stamps advanced on a clean pass |
| `--dry-run` | Evaluates every selected reaper regardless of stamps and changes nothing (no removal, no log, no stamp, no dead-letter). Prints `<NAME> REMOVE\|KEEP <path> (<reason>)` per verdict; a remove overridden by another reaper prints as `KEEP ... (kept by <other NAME>)`. After the verdict lines it prints each due reaper's `summary()`, so the report of what a live pass would act on is readable without a live pass, then to stderr each reaper's optional `dry_run_notes()` (`temp-residue` prints the first incomplete-sweep cause: pid, status name, argv0, errno) |
| `--force-run` | A real run that ignores stamps, and writes none. No effect with `--dry-run` |
| `--only NAME` | Restricts the run to one reaper: only it is due and can remove, the others still scan and veto, and only its lines print in a dry run. An unknown name exits 2 |
| `--list` | Lists the discovered reapers and exits |
| `--upkeep-only` | Runs only each reaper's `upkeep`: no scan, no removal, no stamp. Holds an exclusive lock on `~/.local/state/claude-reaper/upkeep.lock` and waits for it up to 120 s, then gives up. Prints each reaper's report lines; one `reaper <NAME>: upkeep failed: <why>` stderr line per raising `upkeep`, the others still run. Exits 0 on every path except invalid argv (exit 2). Honours `--dry-run` and `--only`. If the lock file cannot be created the run goes ahead unlocked |
| `--no-wait` | Only with `--upkeep-only` (alone it is invalid argv): exit 0 at once when another upkeep run holds the lock |
| `--on-pressure` | Disk-pressure pass: makes due, ahead of its throttle, each reaper whose `pressure_paths` are short of free space. Combines only with `--dry-run`; with `--upkeep-only`, `--only` or `--force-run` it is invalid argv. See [Disk pressure](#disk-pressure) |

| `--holders PATH...` | Read-only holder query, see [Holder query](#holder-query). Prints one `<path>\t<free\|held\|unknown>\t<detail>` line per path. Exits 0 when every path is free, 3 when a path is held and none is unknown, 4 when any path is unknown. Combines with no other flag (exit 2); runs no reaper, reads no stdin, writes no stamp or log |

Use `--dry-run` before anything else on a new machine or after writing a reaper.

## Session gate

SessionStart runs the hook for every session source, so a reaper whose notice is only worth showing at a fresh interactive start (not on `resume` or `compact`, and not in a spawned specialist or an SDK run) declares `SESSION_SOURCES`. The runner then reads the hook payload, one JSON object on stdin, only when some discovered reaper has the attribute, its throttle has elapsed and the pass is not manual (a start inside every gated reaper's throttle never touches stdin, so it cannot wait on it). It never waits on a terminal or on a pipe that stays silent (it gives up after 2 seconds) and ignores a payload over 64 KiB, input that is not JSON or not an object, and a payload without a string `source`. The result is `ctx.session = (source, top_level, interactive)`, or `None`:

| Part | Holds when |
|---|---|
| `source` | the payload's `source` field |
| `top_level` | `AGENT_RECURSION_DEPTH` is unset or `0` |
| `interactive` | `CLAUDE_CODE_ENTRYPOINT` is `cli`; any other value (`sdk-cli` and the like) is not interactive |

A gated reaper is due only when `source` is in its `SESSION_SOURCES` and `top_level` and `interactive` both hold, and its throttle has elapsed. Otherwise it is not due: its `scan` still runs with `ctx.due` false and its keep verdicts still veto, but it prints no summary and its stamp is not advanced, so a start that showed nothing does not spend the throttle of a later one. A pass with no payload (a timer, a pressure pass, a terminal) never makes a gated reaper due. `--dry-run`, `--force-run` and `--only` ignore the gate. A reaper without `SESSION_SOURCES` behaves exactly as before. The interactive entrypoint value is one constant (`INTERACTIVE_ENTRYPOINTS` in `scripts/reaper/runner.py`).

## Holder query

`python3 scripts/hook-reaper.py --holders PATH...` answers whether anything still uses a path, for a caller that wants to know before it removes one itself. The same answer is `liveness.path_holders(paths, ctx)` in `scripts/reaper/liveness.py`. Per path, the first source that holds it names it in the detail:

| Source | A path is held when | Detail |
|---|---|---|
| `session-scope` | a live scope record's working directory or repo root is at or inside it (the liveness rule of `ctx.owned_path`) | `session-scope: session=<id> cwd=<dir>` |
| `engine-task` | an agentctl session state under the engine state directory, whose `node` is not `RESOLVED`, has its `repo_root` or `delivery_worktree` at or inside it | `engine-task: session=<id> task=<task id>` |
| `process` | a process has its cwd, exe, an open fd or a mapped file at or inside it (one `/proc` sweep, the same one `temp-residue` uses) | `process: <cwd\|exe\|fd\|maps>`, the kind of the first hold found |

The answer is `unknown`, never `free`, when a source could not be read in full: the scope registry failed to load, the engine state directory or a state file in it could not be read or parsed (a zero-byte state file records nothing, so it is skipped and does not count; a non-empty file that does not parse does), or the `/proc` sweep was incomplete (the detail names the cause). `held` outranks `unknown`. A missing engine state directory holds nothing. The filesystem root is always `unknown`.

Containment is decided on normalized absolute strings (`normpath` of `abspath`) plus one resolution of the queried path itself: nothing under a queried path is stat-ed, resolved or walked, so the query cannot hang on a dead mount. A queried path that is a symlink is also tried by its resolved spelling, and a held path (a scope record's cwd or repo root, an engine task's root) that is not inside a queried path is resolved too, so a symlinked spelling on either side still gives `held`. A held path inside a queried path is compared as recorded. Process holders are matched by the kernel's own symlink-free paths, so only the queried spellings are tried.
Detail text and path are printed on one line: a tab, newline or carriage return in either is escaped as `\t`, `\n`, `\r`.

`scripts/hook-branch-backup.py` is the Stop and SessionStart trigger of the upkeep and of the pressure check. It spawns `hook-reaper.py --upkeep-only --no-wait` and, independently, `hook-reaper.py --on-pressure`, each in its own session, appends their output to `~/.local/state/claude-reaper/upkeep.log` and `pressure.log`, and returns without waiting, printing nothing and exiting 0 whatever happens (a push can wedge on the network, and a Stop hook must not); one spawn failing does not stop the other. It names its own repository, so the session's working directory is irrelevant. The pass that runs on SessionStart through `hook-reaper.py` never calls `upkeep`.

## Disk pressure

Difficulty removed: the 24-hour throttle lets residue fill a disk long before the next pass. The pressure mode changes **when** a reaper runs, never what it may delete.

- **Contract.** A reaper may define `pressure_paths(ctx)`. `ctx.pressured` is the set of reaper names made due by pressure in this pass (empty in every other pass), so a reaper whose removal is costly may restrict that removal to pressure passes. Plugin-layer reapers may declare `pressure_paths` too.
- **Trigger.** For each reaper with `pressure_paths`, the runner takes the lowest free percentage, `100 * f_bavail / f_blocks` from `statvfs` at the nearest existing ancestor of each path. The reaper is **due** when that is strictly below the threshold and its cooldown has elapsed. An unreadable path gives no reading, so the reaper is not due.
- **Threshold.** The `config.md` row `reaper-pressure-free-percent`, read from the config.md of the checkout the runner lives in. A missing row, a non-number, or a value not above 0 and below 100 disarms the mode: one `reaper: pressure mode disarmed: ...` stderr line, exit 0, no scan.
- **Cooldown.** After a live pass a reaper's `<NAME>.pressure.stamp` (beside the throttle stamps) holds back the next pressure pass for `PRESSURE_COOLDOWN_MIN` (10) minutes. The throttle stamp is never written or read by this mode.
- **Pass.** Exactly the due reapers may remove; every other reaper still scans and vetoes, so keep-wins, ownership, the removal log and error isolation are unchanged. When nothing is due there is no scan and the run exits 0.
- **Lock.** A no-wait exclusive lock on `~/.local/state/claude-reaper/pressure.lock`: a second run exits 0 at once. A dry run takes no lock.
- **Record.** A live pass appends `{ts, threshold, free, due, removed}` to `<config root>/reaper/pressure.jsonl` (also when nothing was removed), the source for recalibrating the threshold. A live run prints only readings that are not `ok`, to stderr; the hook's `pressure.log` therefore stays empty on a healthy disk.
- **Dry run.** Prints `<NAME> free <p>% threshold <t>% -> due|ok|cooldown` per pressure-aware reaper, then the usual verdict lines with `ctx.pressured` set; no lock, no stamp, no log. Always exits 0 except on invalid argv (a dry run exits 1 on a runner error).

Residuals, named rather than hidden: only reapers that name pressure paths can trigger a pass; a disk full of residue no reaper may remove is only visible in `pressure.log`; the trigger needs `hook-branch-backup.py` installed; the detached run inherits the spawning session's environment, so the pytest reaper sees that session's `TMPDIR`; a pressure pass can run beside a normal pass (the locks differ), so a pressure-aware reaper treats a vanished path as success.

### The systemd timer (`scripts/install-reaper-timer.sh`)

Difficulty removed: the hook trigger fires only while a Claude session ends or starts, so a machine with no session fills its disk unwatched. A user timer runs the pressure mode about once a minute, with no session and, with linger, after logout.

- **Units** (systemd `--user`, written to `~/.config/systemd/user`): `agent-reaper-pressure.timer` (`OnCalendar=minutely`, `Persistent=false`) starts `agent-reaper-pressure.service` (`hook-reaper.py --on-pressure`); `agent-reaper-pass.service` (`--force-run`, every reaper due; what `--check` starts); template `agent-reaper-only@.service` (`--only %i`, e.g. `@temp-residue`). A unit holds environment and `ExecStart` only, no removal logic and no flag the runner does not define. The 10-minute per-reaper pressure cooldown is unchanged.
- **Frozen environment.** Every unit gets the same `Environment=TMPDIR=`, `CLAUDE_CONFIG_DIR=` and `CLAUDE_AGENT_HOME=` (both the agent home that `lib/config_root.py` resolves, `CLAUDE_CONFIG_DIR` first) and the line `UnsetEnvironment=SSH_AUTH_SOCK SSH_AGENT_PID GIT_ASKPASS SSH_ASKPASS`. A unit sees only what is written into it, so without `TMPDIR` the temp reapers would read `/tmp` while the sessions write elsewhere. `TMPDIR` is `--tmpdir PATH`, else the installer's `TMPDIR`, else the platform default with a warning that names it; resolved with realpath. A path with a character a unit cannot carry unquoted is refused (exit 2).
- **Commands.** `install-reaper-timer.sh [--dry-run] [--uninstall] [--status] [--check] [--tmpdir PATH]`. `--dry-run` prints the units and the commands and makes no systemctl call. `--status` exits 0 (installed, timer state printed) or 3. Install, `--uninstall`, `--status` and `--check` exit 4, changing nothing, when no user manager answers `systemctl --user is-system-running`. Seams for tests: `SYSTEMD_USER_DIR`, `SYSTEMCTL_BIN`, `LOGINCTL_BIN`, `JOURNALCTL_BIN`; the stubs are `scripts/tests/fixtures/reaper_timer_stubs/`.
- **`--check`** starts `agent-reaper-pass.service` through systemd and judges that run, never the manager's own environment. It names the first failed clause (`check failed: <clause>: <detail>`, exit 1), in order: `units` (all four files installed), `environment` (the `@` unit's `Environment`/`UnsetEnvironment` lines equal the pass unit's), `start`, `result` (`Result=success`, `ExecMainStatus=0`), `invocation` (the run's id read from the pass unit's journal since the start, retrying up to 5 s; none or several distinct ids fail: systemd clears `InvocationID` of a finished oneshot, so `systemctl show` no longer has it), `journal` (no `reaper: runner error` line under `_SYSTEMD_INVOCATION_ID=<id>`, the runner's own lines; an unreadable journal is not a failure, the observation's `errors` covers it), `observation` (the file for that id exists and names `unit` = `agent-reaper-pass.service`), `tmpdir` and `agent_home` (observed equal to the frozen values), `credentials` (all four absent), `reapers_run` (at least 1) and `errors` (0), then `linger` (Linger=yes).
- **The observation.** Every unit sets `AGENT_REAPER_UNIT=%n` (its own name). `INVOCATION_ID` alone is no proof of a unit run: every descendant of a systemd-started process inherits it, so a session under a systemd-started tmux carries one. The runner treats a pass as started by a unit only when both are set; then a non-dry pass is tagged `trigger=unit` in the removal log and kept snapshot, and the runner writes `<agent home>/reaper/unit-pass/<INVOCATION_ID>.json` (atomic, newest 50 kept): `invocation_id`, `unit`, `tmpdir`, `agent_home` (resolved, as seen), `credentials_present` (a boolean per variable), `reapers_run` (scans that completed) and `errors`. `--force-run` makes every reaper due, so the pass unit cannot pass with nothing run.

Residuals: the live clauses (a run started by the real systemd, the real environment, linger) are discharged only by running `--check` on the machine; `ExecStart` freezes the installer's `python3` path, so reinstall after moving it; the check does not read what the manager would otherwise have passed on (`SSH_AUTH_SOCK` in the manager's environment is removed by the unset line, and the observation is what proves it).

## The `git-worktrees` reaper

Source: `scripts/reaper/builtin/git_worktrees.py`. It looks at the repository that contains the reaper code and returns exactly one verdict for every `git worktree list --porcelain` entry except the first (the main checkout), with the reason in the verdict (`main branch`, `not under temp root`, `fresh`, `owned`, `missing directory`, `locked`, `dirty`, `unlanded <N>`, ...).

- **Paths are compared by real path**: git prints resolved paths, while `$TMPDIR` or a session's working directory may be spelled through a symlink.
- **Detached worktree**: removed when it is under a temp root (`/tmp/cc-scratch`, `$TMPDIR`), older than 24 hours, unowned, clean, and its `HEAD` is an ancestor of `origin/main` (`git merge-base --is-ancestor`), so no commit is lost; otherwise it is kept with `detached HEAD not in origin/main`. It is removed with plain `git worktree remove`, never `--force`. A dirty one is kept, and a diff snapshot goes to `<config root>/orphan-worktree-deadletter/<name>-<ts>.diff` (not in a dry run, and not when the reaper is not due).
- **Branch worktree** (anywhere, except the main checkout and a worktree on `main`): removed when it is older than 24 hours, unowned, clean (`git status --porcelain` empty, untracked files count) and landed (`git cherry origin/main <branch>` prints no `+` line, so a fast-forward, a merge and a rebase all count). First `<iso-time> <branch> <sha> <path>` is appended to `reaped-branches.log` in the dead-letter directory; then the worktree is removed with plain `git worktree remove` (no `--force`, so git itself refuses one that turned dirty since the scan, and the runner keeps it); only then `git branch -D` runs, so a deleted branch can always be restored from the log line.
- A stale unowned branch worktree with unlanded commits or local changes is **kept** (`unlanded <N>` / `dirty`). Once it is older than 7 days, `summary()` adds one SessionStart line naming how many there are and the command that lists them.
- Any git failure for an entry is a keep for that entry. The main checkout, `refs/heads/main`, the working directory of the running process and the checkout holding the reaper code are never touched.

### Backup duty (`upkeep`)

Difficulty removed: a worktree branch holds the only copy of its commits until someone pushes it, and the reaper above is the one place that already knows which branches those are.

Every `--upkeep-only` run pushes each worktree branch of this repository (the main checkout's branch included; detached, bare and branchless entries skipped) that is **not trunk** (`main`, `master`, `release-*`, `release/*`), has commits `git cherry origin/main <branch>` marks `+` (a branch landed by rebase is not resurrected), and whose tip origin does not already have: `refs/remotes/origin/<branch>` is missing or the tip is not its ancestor. It runs only local ref queries before the push; it never fetches.

- **Plain push only.** `git push origin refs/heads/<b>:refs/heads/<b>`: no force flag, no leading `+`, no lease, no hook bypass, no working-tree command, so uncommitted and untracked files never leave the machine. A remote that moved on is a `rejected` outcome, not an overwrite. A branch whose origin tip is neither an ancestor nor a descendant of the local tip is **diverged**: reported, never pushed.
- **Org-neutral check first.** The outgoing patch (`git log -p` of the commits origin lacks) is piped to `scripts/check-org-neutral.py -`; exit 1 skips the push as `blocked` / `org-neutral`, any other failure of the checker (missing, crash, timeout of 30 s) as `blocked` / `checker error`. The repository is public, so an unchecked push is not an option.
- **Bounded and non-interactive.** `GIT_TERMINAL_PROMPT=0`, `LC_ALL=C`, ssh `BatchMode=yes` and `ConnectTimeout=10` (appended to any existing `GIT_SSH_COMMAND`, else `core.sshCommand`, else `ssh`), and a 60 s kill of the whole push process group (`timeout` outcome; the 60 s is an assumption, not a measured figure).
- **Idempotent.** A successful push updates `refs/remotes/origin/<b>`, so the next run sees the branch as backed up and does nothing.
- **Fail-open.** One branch failing (no push rights, rejection, timeout, exception) is logged and the loop goes on; nothing raises out of `upkeep`.
- **Report and log.** One line per branch, `git-worktrees <WORD> <branch> <sha> [detail]` with `WORD` one of `PUSHED`, `REJECTED`, `BLOCKED`, `TIMEOUT`, `ERROR`, and `git-worktrees DIVERGED <branch>`. A dry run prints `WOULD-PUSH`, `BLOCKED` or `DIVERGED` and writes nothing. A live run appends one JSON line `{ts, branch, sha, outcome, detail}` per branch to `<config root>/reaper/branch-backup.jsonl`.
- **Visible at session start.** `summary()` adds a line naming how many linked worktree branches hold commits origin lacks.

Residuals, named rather than hidden: the checker runs before the push, so a branch that moves in between is pushed unchecked for its newest commits; `branch-backup.jsonl` and `upkeep.log` gain one line per unresolved `blocked` or `diverged` branch per run, with no deduplication or rotation; only this repository is covered; a machine without push rights logs an `error` per branch per run; the session-start count covers linked worktrees while the backup also covers the main checkout's branch.

## The `agentctl-state` reaper

Source: `scripts/reaper/builtin/agentctl_state.py`. agentctl writes `<config root>/agentctl/state/<session>.json` for every session it classifies and never deletes it; almost all of these files belong to sessions that stopped at the first node, `CLASSIFIED` (a chat, a small change), and nothing reads them again.

- It looks only at plain `<session>.json` names (the whole name matches `[0-9A-Za-z_-]+\.json`). Every other file in the directory gets no verdict at all.
- **Removed**: a file whose top-level `node` is `CLASSIFIED`, whose mtime is more than 30 days old (the state has no update-time field, so the mtime is the activity signal), and whose session no live scope record owns (`ctx.owned_session`).
- **Kept**, with the reason: `symlink` (the name is a symbolic link), `unreadable` (not UTF-8 JSON, not an object, or not readable), `node <X>` (any other node), `fresh` (under 30 days), `owned`.
- `remove()` judges the file again before unlinking it, so a session that moved past `CLASSIFIED` between the scan and the removal is kept.

What it deliberately keeps, and why:

| Kept | Why |
|---|---|
| State files at any node other than `CLASSIFIED`, `RESOLVED` included | `agentctl reset --reopen-reason` reopens a resolved session from its file, and reports such as the escape-hatch report read the history of past sessions |
| `plan-approved-<digest>.toml`, `plan-version-*.toml` | approvals and plan versions are accumulators keyed by plan content; they outlive the session that wrote them |
| `<session>.delivery.json` sidecars | the delivery record of a session is read after the session ends |
| `.bak-*`, `.SUPERSEDED.json`, `.PHANTOM-backup.json` and other dotted names | hand-made backups and repair records; whoever made them decides when they go |

## The `pytest-basetemp` reaper

Source: `scripts/reaper/builtin/pytest_basetemp.py`. Every pytest run creates `<temp dir>/pytest-of-<user>/pytest-N`; pytest prunes old ones only when a later run reaches its own cleanup, so killed runs and busy hosts leave directories that nothing reclaims. The reaper proves a directory dead instead of guessing from its age: pytest 8.3.5 writes the creating pid into `pytest-N/.lock` and removes the file at exit.

- The root is `<tempfile.gettempdir()>/pytest-of-<current user>`, resolved at call time. Only direct children named `pytest-<digits>` get a verdict; `pytest-current`, `garbage-*` and every other name get none.
- **Removed**: a real directory of the current uid, whose mtime and `.lock` mtime (the newer one) are over 2 hours old (`SHORT_FLOOR_HOURS`), with no `.lock` or a `.lock` holding a pid that is dead on this host, with no process of this host having its cwd at or inside it (one `/proc` scan per call), and that no live scope record owns (`ctx.owned_path`).
- **Kept**, with the reason: `symlink`, `not a directory`, `foreign owner`, `fresh`, `live lock (pid N)` (including a pid owned by another user, which `kill(pid, 0)` reports as permission denied), `unreadable lock` (empty, non-numeric, non-positive, or not a regular file), `process cwd inside`, `cwd scan unavailable` (no `/proc`), `owned`, and `error: <type>` for any exception while judging one directory.
- `remove()` refuses a path that is not a direct child of the root, judges the directory again with a fresh `/proc` scan, and deletes it with `liveness.remove_tree` (through the parent's directory fd, never following a symlink, retrying after `chmod u+rwx` on a permission error; shared with `temp-residue`). An already-vanished path counts as removed. The runner throttle is 24 hours; its `pressure_paths` is the root, so a [pressure pass](#disk-pressure) makes it due earlier under the same rules.

pytest's own 3-day lock timeout is not used as the age: it is too long to bound the stock on a busy host. The 2-hour floor is only a backstop; the lock pid, the cwd scan and scope ownership are the proof.

Residuals, named rather than hidden: a pid written by a process in another PID namespace that shares this temp root looks dead here, and its directory is removed once it is over 2 hours idle with no cwd visible; a run started with an explicit `--basetemp` lives outside the root and is never seen; `garbage-*` and other non-numbered leftovers in the root are not reaped; a live run that has touched nothing for over 2 hours and whose lock was removed is protected only by the cwd scan.

## The `temp-residue` reaper

Source: `scripts/reaper/builtin/temp_residue.py`, proof in `scripts/reaper/liveness.py`, switch in `scripts/reaper/opt_in.py`. Test and session runs leave directories in shared temp roots that nothing reclaims; on a busy machine they fill the volume. The reaper removes one only on a proof of death, never on age alone.

**Where it looks.** Three kinds, each a direct child of its root, matched by full name (`<prefix>.[A-Za-z0-9_-]+`):

| Kind | Root |
|---|---|
| `instruction-sandbox.*` | `/tmp` and the session temp root (`tempfile.gettempdir()` at pass time, so `TMPDIR` decides) |
| `tmp.*` | the session temp root |
| `sbx.*` | `<agent home>/jobs/<job>/tmp` for each job |

Each root is resolved with `os.path.realpath` and scanned once however many ways it is named; the walk and its device reference come from the resolved root. The roots are the reaper's `pressure_paths`, so a [pressure pass](#disk-pressure) makes it due ahead of its 24-hour throttle, under the same rules.

**Proof of death (`liveness.judge_residue`).** A candidate is removable only when all of these hold; the first that fails decides the keep slug:

| Slug | Keeps the candidate when |
|---|---|
| `not-a-directory` | it is a symlink or a plain file (lstat, never followed) |
| `foreign-owner` | its uid is not the current user's |
| `mount-inside` | it is on another device than its root (checked first, before the walk) |
| `session-owned` | a live scope record owns it (`ctx.owned_path`) |
| `held` | a process has its cwd, exe, an open fd or a mapped file (`/proc/<pid>/maps`) at or inside it; the keep text names the holder kinds |
| `holder-sweep-incomplete` | a holder cannot be ruled out: `/proc` could not be listed, a process's `status` could not be read (EACCES/EPERM) or has no `Uid:` line, or a process of this uid had a cwd, exe, fd or maps entry that could not be read (EACCES/EPERM) and is not an [allowlisted helper](#unreadable-helpers). A process whose `/proc/<pid>` or `status` is gone (ENOENT/ESRCH) vanished and holds nothing; an ENOENT on one link of a process whose `status` is still present skips only that link. Only a process whose `status` `Uid:` line (real, effective, saved, filesystem) names no uid of this user is skipped, and counted |
| `os-error` | `/proc/self/mountinfo` could not be read, or any other exception while judging; the text goes to the kept snapshot only |
| `mount-inside` | a mount point from `mountinfo` is at or inside it, or the walk meets an entry on another device |
| `nested-git` | a `.git` entry is inside it, unless the [sandbox clone exemption](#the-sandbox-clone-exemption) vouches for it |
| `idle-floor` | the newest `max(mtime, ctime)` of anything in the tree is younger than the floor |

A process is identified by the `Uid:` line of `/proc/<pid>/status`, never by the owner of `/proc/<pid>`: the kernel shows root there for a process whose memory is not dumpable (after `prctl(PR_SET_DUMPABLE, 0)`, or after a setuid binary or one with file capabilities), and such a process of this user can hold a candidate. A long-lived non-dumpable process of this user whose links cannot be read therefore makes every pass `holder-sweep-incomplete` and blocks all removals unless it is one of the helpers below; that is the price of not guessing. A process of this uid in state `Z` or `X` on its `status` `State:` line (exited, not yet reaped) has released its cwd, fds and mappings and holds nothing: it is skipped before any read and never makes the sweep incomplete.

#### Unreadable helpers

A real machine has about a dozen same-uid system processes that are non-dumpable and so cannot be read (the user manager, `(sd-pam)`, the ssh daemon's per-login children, the agents). Treating each as an unknown holder blocked every removal. A process of this uid whose links cannot be read is exempt, and counted in `unreadable_helpers_skipped`, only when its identity matches **one exact pair**: the `Name:` line of its `status` **and** its argv0 token (the first word of `/proc/<pid>/cmdline`, cut at the first `:` or space, reduced to its basename) must both name the same row:

| `status` Name | argv0 token | extra condition |
|---|---|---|
| `systemd` | `systemd` | the pid is the `MainPID` of `user@<uid>.service` (`systemctl show -p MainPID`) |
| `(sd-pam)` | `(sd-pam)` | its `PPid` is that same `MainPID` |
| `sshd` | `sshd` | none |
| `sshd-session` | `sshd-session` | none |
| `gpg-agent` | `gpg-agent` | none |
| `ssh-agent` | `ssh-agent` | none |
| `fusermount` / `fusermount3` | `fusermount` / `fusermount3` | none |

A name that matches one row and an argv0 that matches another is not exempt; neither is an unreadable or empty `cmdline`, nor any failure of the `systemctl` query (missing binary, timeout, non-zero exit, a unit not running, `MainPID=0`): the manager and `(sd-pam)` then stay unknown holders. The query runs at most once per sweep, and only when a candidate needs it. A helper whose links *are* readable is swept like any process, and its holds count.

Residuals of the allowlist, all bounded by the 3-day idle floor: an allowlisted helper that holds a temporary directory is not seen; any process of this uid can rename itself (`prctl(PR_SET_NAME)` and a rewritten argv0) into a listed pair and stay exempt; a non-dumpable process that is not on the list still keeps every pass `holder-sweep-incomplete` (the dry run's stderr names the first one); a unix socket a process holds in a candidate is not a cwd, exe, fd link into the tree and is not seen; an `(sd-pam)` orphaned and adopted by another parent is no longer exempt.

The walk never follows a symlink and does not cross a filesystem boundary. The floor, `IDLE_FLOOR_SECONDS`, is 3 days and a constant of the module: no environment variable or config key moves it.

### The sandbox clone exemption

`scripts/instruction-sandbox.sh` makes `<sandbox>/core` a `git clone --shared --no-checkout` of the canonical checkout (then `checkout --detach`, `remote remove origin`, and `home/claude-agent-instructions` links to `core`), so every `/tmp/instruction-sandbox.*` holds a `.git` and would be kept as `nested-git` for ever. For kind `instruction-sandbox` only, `temp_residue.disposable_shared_clone` lifts that clause when all of these hold; any failed check, git failure or timeout keeps the candidate as `nested-git`:

- the only `.git` entry in the whole candidate is `core/.git`, a directory;
- `core/.git/objects/info/alternates` exists and each entry is the `objects` directory of an existing repository outside the candidate;
- `core/.git/worktrees` is absent or empty (no linked worktree);
- `core/.git/modules` is absent or empty (a submodule's commits live in its own object store there, which the clone's `count-objects` does not count);
- none of `rebase-apply`, `rebase-merge`, `sequencer`, `MERGE_HEAD`, `CHERRY_PICK_HEAD`, `REVERT_HEAD`, `BISECT_LOG` exists in `core/.git` (a stopped `git am` keeps its patch only in `rebase-apply`, which no object and no `status` line shows);
- the clone's **own** object store is empty: `git count-objects -v` reports `count: 0`, `in-pack: 0` and `garbage: 0` (a `.pack` without its `.idx` is garbage, yet holds objects). A commit made in the clone is an own object however it is later hidden (reset, a detached checkout, a deleted branch, a dropped stash, a reflog entry), so with an empty store `HEAD` and every ref can only name objects the alternates hold;
- `git status --porcelain -z --untracked-files=all --ignored=matching` of `core` lists no modified, staged or untracked file and no ignored file, except ignored `*.pyc` files and ignored `__pycache__/` directories that hold only `*.pyc` files and no symlink (`.env`, `secrets/`, `agents-local/*` and anything in `.git/info/exclude` therefore keep the candidate);
- `git ls-files -v` shows no entry tagged assume-unchanged (lowercase) or skip-worktree (`S`), which would hide an edit from `status`.

What the exemption proves is exactly this list: nothing in `core` that is not in the alternates, uncommitted, untracked, or ignored beyond bytecode, and no other `.git` entry in the candidate. It says nothing about the rest of the candidate (the `home` side, files outside `core`), which the other clauses and the idle floor judge.

Git runs with a scrubbed environment (no `GIT_*` from the caller, `HOME=/nonexistent`, no system or global config, `GIT_OPTIONAL_LOCKS=0`, `core.fsmonitor=false`, `core.hooksPath=/dev/null`; `status` also runs with `core.untrackedCache=false`, `core.checkStat=default`, `core.trustctime=true`, so neither the index's untracked-cache extension nor the clone's own stat settings decide what it reports), no network and a 20-second timeout, so judging a sandbox writes nothing into it. A sandbox where `setup-symlinks.sh` left the working tree dirty is still kept; the dry run's `reason=nested-git` count shows how many.

**Opt-in.** The reaper deletes only on a machine whose owner wrote `temp-residue = true` in `<agent home>/reaper/opt-in.toml` (the boolean `true` only; a missing file, a missing key, a string or `1` all mean off). With it off, a live pass keeps every candidate it would have removed (`opt-in-off-would-remove`), `remove()` refuses, and a dry run still shows those as `REMOVE`, so the owner sees what enabling would do. The summary and the kept snapshot show the same candidates.

**Removal.** `remove()` re-judges the candidate with a fresh `/proc` sweep and then deletes it through the parent's directory fd (`liveness.remove_tree`: `chmod u+rwx` and retry on an unwritable directory, never following a symlink; a vanished name is success).

**Report.** `summary()` prints one `temp-residue root=<path>` line per scanned root, one `temp-residue kind=<kind> removable=<n> removable_bytes=<b> kept=<n> kept_bytes=<b>` line per kind (zero counts included) and one `temp-residue reason=<slug> count=<n> bytes=<b>` line per keep slug present. Reasons are slugs only; paths and OS error text are in the kept snapshot. The runner prints it after a live pass in which the reaper was due and after a dry run.

Residuals, named rather than hidden, each covered only by the idle floor: a process in another PID namespace sharing the temp root is invisible to the sweep; a process in another *mount* namespace (a bwrap or rootless container that bind-mounts a candidate elsewhere) reports its cwd, fd and maps paths as seen there, so they do not match; `liveness.remove_tree` does not check `st_dev`, so a mount that appears between the re-judgment and the deletion would be entered. A process of another user (by its `Uid:` line) is not examined, and a candidate of this user that such a process, or root, holds is not seen as held. A candidate on another device than its root is kept as `mount-inside` rather than judged. `git status` in the sandbox clone may run a clean or smudge filter that the candidate's own `.git/config` or `info/attributes` defines on a stat-dirty file: the reaper neutralizes `core.fsmonitor` and `core.hooksPath` but cannot list the arbitrary `filter.<name>.*` keys, so that program, run as this user under the 20-second timeout, remains; "judging writes nothing" holds for what git itself writes, not for such a program. A bare repository not named `.git` (for example `stuff/mine`) is invisible to the `nested-git` clause, so a candidate holding one is judged as if it were not there. Work kept only in `core/.git/hooks/*` (a hand-written hook script) or in `core/.git/config` (a hand-set key or remote) is not examined: neither is an object, a ref or a status line.

## Cleanup at `resolve`, not by a reaper

A reaper handles residue nobody reclaimed. The workspaces of a task that finished normally are reclaimed at its end instead: `agentctl resolve` removes the clean, landed workspaces the task registered (`scripts/agentctl/workspace_cleanup.py`, report in `data['workspace_cleanup']`). Whatever it keeps (dirty, unlanded, protected, a failed teardown) stays with a stated reason and remains for the `git-worktrees` reaper above. The rule, the registry and the controls are in `scripts/agentctl/README.md` § Workspace cleanup on `resolve`. A task that never reaches `resolve` registers its workspaces and loses nothing: they stay for the reaper.

## Tests and the mutation catalogue

`scripts/tests/test_reaper_runner.py`, `scripts/tests/test_reaper_holders.py`, `scripts/tests/test_reaper_git_worktrees.py`, `scripts/tests/test_reaper_agentctl_state.py`, `scripts/tests/test_reaper_pytest_basetemp.py`, `scripts/tests/test_reaper_pressure.py`, `scripts/tests/test_reaper_liveness.py`, `scripts/tests/test_reaper_temp_residue.py` and `scripts/tests/test_hook_branch_backup.py` are hermetic: throw-away git repositories, and `HOME`, the config root, the plugin dir, the project dir and `TMPDIR` redirected under `tmp_path`. A test never runs the real runner against a real checkout.

`scripts/tests/reaper_mutation_control.py` holds a catalogue of named wrong versions of production lines (`keep-wins`, `error-isolation`, `removal-log-before-remove`, `removal-outcome-logged`, `removal-refused-not-an-error`, `realpath`, `landed-check`, `state-node-filter`, `state-age-floor`, `state-suffix-filter`, `basetemp-live-lock`, `basetemp-age-floor`, `basetemp-name-filter`, `basetemp-cwd-holder`, `basetemp-symlink`, `basetemp-foreign-uid`, `basetemp-owned-scope`, `basetemp-remove-rejudge`, `backup-push-duty`, `backup-never-force`, `backup-trunk-excluded`, `backup-skip-backed-up`, `backup-dry-run`, `backup-fail-open`, `pressure-threshold`, `pressure-keeps-veto`, `pressure-cooldown`, `pressure-no-throttle-stamp`, `pressure-disarm-on-bad-row`, `pressure-hook-spawn`, `pressure-hook-stop-only`, `residue-idle-floor`, `residue-holder-cwd`, `residue-holder-fd`, `residue-holder-maps`, `residue-holder-exe`, `residue-session-owned`, `residue-owner-uid`, `residue-proc-unreadable-keeps`, `residue-mount-keep`, `residue-nested-git`, `residue-skip-unreadable-same-uid`, `residue-skip-every-process`, `residue-skip-no-process`, `residue-status-uid-first-only`, `residue-owner-unreadable-keeps`, `residue-no-uid-line-keeps`, `residue-helper-allowlist-empty`, `residue-helper-allowlist-open`, `residue-helper-name-or-argv0`, `residue-helper-manager-not-mainpid`, `residue-helper-manager-query-fails-open`, `residue-helper-sdpam-parent`, `residue-helper-readable-exempt`, `residue-vanished-incomplete`, `residue-proc-owner-from-dir-stat`, `residue-vanished-fd-skips-process`, `residue-snapshot-reason-slug`, `residue-snapshot-helpers-count`, `residue-dry-run-cause`, `residue-clone-own-objects-unchecked`, `residue-clone-loose-objects-unchecked`, `residue-clone-packed-objects-unchecked`, `residue-clone-linked-worktrees-unchecked`, `residue-clone-garbage-unchecked`, `residue-clone-modules-unchecked`, `residue-clone-operation-unchecked`, `residue-clone-status-cache-settings`, `residue-clone-dirty-ignored`, `residue-clone-status-defaults`, `residue-clone-ignored-unseen`, `residue-clone-ignored-any`, `residue-clone-pycache-contents`, `residue-clone-pycache-symlink`, `residue-clone-assume-unchanged`, `residue-clone-skip-worktree`, `residue-git-fsmonitor`, `residue-clone-no-alternates`, `residue-clone-alternate-inside`, `residue-clone-second-git`, `residue-clone-any-kind`, `residue-git-failure-keeps`, `residue-git-exceptions`, `residue-git-env-scrubbed`, `residue-git-timeout`, `residue-remove-candidate-check`, `residue-failed-scan-no-snapshot`, `residue-refused-removal-reason`, `residue-symlink`, `residue-one-fs`, `residue-root-realpath`, `residue-opt-in`, `residue-opt-in-remove`, `residue-remove-rejudge`, `residue-error-keeps`, `residue-dry-run-no-write`, `residue-summary-format`, `kept-snapshot-written`, `residue-pressure-paths`, `session-gate-ignored`, `session-gate-stamps`, `session-gate-child`, `session-gate-noninteractive`, `session-gate-manual`, `holders-scope-ignored`, `holders-engine-ignored`, `holders-engine-resolved-held`, `holders-process-ignored`, `holders-unknown-as-free`, `holders-empty-state-unknown`, `holders-realpath`, `holders-zombie-unknown`, `holders-symlink-queried`, `holders-symlink-holder`, `holders-resolve-inside`, `holders-detail-unescaped`, `session-payload-read-always`, ...). `--mutant NAME` applies one to a scratch copy of `scripts/` and runs only its listed tests; exit **1** means killed (the anchor matched once and every listed test failed in its call phase), **0** survived, **3** anchor miss, **4** collection error, **5** a listed test was missing, errored or skipped, **6** unhandled exception. `--control` runs the same tests on the unmutated copy. The in-suite test (a slow test: it runs when a guarded path changed or with `--run-slow`, see `scripts/tests/slow_tests.toml`) runs the whole catalogue with up to 6 mutants at a time (`MUTANT_WORKERS`; one scratch copy and one serial pytest per mutant, about 14 minutes run one after the other, 3.6 minutes with six workers on 12 CPUs); the child runs set `REAPER_MUTATION_CHILD=1` so it skips itself. The catalogue spawns a pytest per mutant, so it fans out to six workers only under `scripts/cap-run.sh`: that script exports its cap as `CAP_RUN_NPROC`, and the test uses six workers only when that value equals the process's own `RLIMIT_NPROC` soft limit (`mutant_workers` in `test_reaper_runner.py`); without it the catalogue runs one mutant at a time. Run it as `scripts/cap-run.sh -- python3 -m pytest -q -p no:xdist scripts/tests/test_reaper_runner.py --run-slow`.

## Adding a reaper

Write the module in the right layer, run `python3 scripts/hook-reaper.py --list` to see it registered, then `--dry-run --only <NAME>` to read its verdicts. Keep `scan` free of side effects, return `keep` with a reason for anything you cannot prove removable, and let the runner own the rest.
