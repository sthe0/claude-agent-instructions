# Reapers: stale-residue cleanup under one set of rules

Processes that die hard (SIGKILL, OOM, a crashed harness) leave residue behind: a worktree directory, a state file, a cache. A cleanup that runs only on graceful exit cannot reap it, so a periodic reaper has to. Deleting is irreversible, and the rules that make it safe (do not touch what a live session owns, never act on a guess, say what was removed, survive a bug in one reaper) are the same for every kind of residue. They live **once**, in the runner (`scripts/reaper/runner.py`); a reaper only proves what it can about its own kind of residue.

`scripts/hook-reaper.py` is the SessionStart entry. `scripts/hook-orphan-worktree-sweep.py` is a shim that runs the same runner, so machine-local settings that still name the old sweep keep working.

## The reaper contract

A reaper is a Python module in one of the three layers below, defining:

| Name | Meaning |
|---|---|
| `NAME` | unique string; the identity used for override, `--only`, stamps and the log |
| `THROTTLE_HOURS` | minimum hours between real passes of this reaper; a finite number above 0 (default 24) |
| `scan(ctx) -> list[Verdict]` | one `Verdict(path, action, reason, report=False)` per item it has an opinion on; `action` is `remove` or `keep`. No side effect beyond best-effort bookkeeping that checks `ctx.dry_run` and `ctx.due` |
| `remove(path, ctx)` | delete one item the runner approved. Raise, or return `False`, when it did not happen |
| `summary(verdicts) -> str \| None` | optional; one line for the SessionStart notice (the `git-worktrees` reaper uses it for worktrees kept for over a week) |

`ctx` (`scripts/reaper/contract.py: ReapContext`) carries `now`, `dry_run`, `due`, `project_dir`, `deadletter_dir` and two ownership queries over the session-scope registry: `ctx.owned_path(path)` compares real paths against each record's working directory and repo root, `ctx.owned_session(session_id)` compares session ids (in the sanitized form both stores use as file names). Both count a session as live when its pid is alive or its heartbeat is under 24 hours old.

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

1. **Throttle.** Each reaper has its own stamp, `~/.local/state/claude-reaper/<NAME>.stamp`. A pass starts when at least one selected reaper is due; if none is, the runner prints `throttled: within window` to stderr and does nothing.
2. **Every reaper scans, due or not.** A reaper that is not due cannot remove anything, but its `keep` verdicts still count.
3. **Keep wins.** Verdicts are joined by real path. A path is removed only if no reaper says `keep` for it, for anything inside it, or for a directory containing it (compared by path component, so `/a/item` does not contain `/a/item-2`), a due reaper proposed `remove`, and then **once**, even when two reapers propose it.
4. **Log before removal.** Before each `remove`, one JSON line `{ts, reaper, layer, path, reason}` is appended to `<config root>/reaper/removed.jsonl`.
5. **Error isolation.** A `scan` exception cancels **every** removal of the pass (a reaper that cannot see cannot be outvoted by one that can), prints one stderr line and advances no stamp. The same cancellation applies when a module was skipped at discovery, and when a session-scope record could not be read (an owner may be missing; the registry is loaded strictly). A dry run then prints each `remove` as `KEEP ... (pass cancelled: <why>)`. A `remove` exception, or a `False` return, keeps that path, prints one stderr line and leaves that reaper's stamp where it was; other reapers carry on.
6. **SessionStart mode always exits 0**, whatever a reaper or the runner does.

## Modes

| Invocation | Effect |
|---|---|
| `hook-reaper.py` | SessionStart mode: throttled, real removals, stamps advanced on a clean pass |
| `--dry-run` | Evaluates every selected reaper regardless of stamps and changes nothing (no removal, no log, no stamp, no dead-letter). Prints `<NAME> REMOVE\|KEEP <path> (<reason>)` per verdict; a remove overridden by another reaper prints as `KEEP ... (kept by <other NAME>)` |
| `--force-run` | A real run that ignores stamps, and writes none. No effect with `--dry-run` |
| `--only NAME` | Restricts the run to one reaper: only it is due and can remove, the others still scan and veto, and only its lines print in a dry run. An unknown name exits 2 |
| `--list` | Lists the discovered reapers and exits |

Use `--dry-run` before anything else on a new machine or after writing a reaper.

## The `git-worktrees` reaper

Source: `scripts/reaper/builtin/git_worktrees.py`. It looks at the repository that contains the reaper code and returns exactly one verdict for every `git worktree list --porcelain` entry except the first (the main checkout), with the reason in the verdict (`main branch`, `not under temp root`, `fresh`, `owned`, `missing directory`, `locked`, `dirty`, `unlanded <N>`, ...).

- **Paths are compared by real path**: git prints resolved paths, while `$TMPDIR` or a session's working directory may be spelled through a symlink.
- **Detached worktree**: removed when it is under a temp root (`/tmp/cc-scratch`, `$TMPDIR`), older than 24 hours, unowned, clean, and its `HEAD` is an ancestor of `origin/main` (`git merge-base --is-ancestor`), so no commit is lost; otherwise it is kept with `detached HEAD not in origin/main`. It is removed with plain `git worktree remove`, never `--force`. A dirty one is kept, and a diff snapshot goes to `<config root>/orphan-worktree-deadletter/<name>-<ts>.diff` (not in a dry run, and not when the reaper is not due).
- **Branch worktree** (anywhere, except the main checkout and a worktree on `main`): removed when it is older than 24 hours, unowned, clean (`git status --porcelain` empty, untracked files count) and landed (`git cherry origin/main <branch>` prints no `+` line, so a fast-forward, a merge and a rebase all count). First `<iso-time> <branch> <sha> <path>` is appended to `reaped-branches.log` in the dead-letter directory; then the worktree is removed with plain `git worktree remove` (no `--force`, so git itself refuses one that turned dirty since the scan, and the runner keeps it); only then `git branch -D` runs, so a deleted branch can always be restored from the log line.
- A stale unowned branch worktree with unlanded commits or local changes is **kept** (`unlanded <N>` / `dirty`). Once it is older than 7 days, `summary()` adds one SessionStart line naming how many there are and the command that lists them.
- Any git failure for an entry is a keep for that entry. The main checkout, `refs/heads/main`, the working directory of the running process and the checkout holding the reaper code are never touched.

## The `agentctl-state` reaper

Source: `scripts/reaper/builtin/agentctl_state.py`. agentctl writes `<config root>/agentctl/state/<session>.json` for every session it classifies and never deletes it; almost all of these files belong to sessions that stopped at the first node, `CLASSIFIED` (a chat, a small change), and nothing reads them again.

- It looks only at plain `<session>.json` names (the whole name matches `[0-9A-Za-z_-]+\.json`). Every other file in the directory gets no verdict at all.
- **Removed**: a file whose top-level `node` is `CLASSIFIED`, whose mtime is more than 14 days old (the state has no update-time field, so the mtime is the activity signal), and whose session no live scope record owns (`ctx.owned_session`).
- **Kept**, with the reason: `symlink` (the name is a symbolic link), `unreadable` (not UTF-8 JSON, not an object, or not readable), `node <X>` (any other node), `fresh` (under 14 days), `owned`.
- `remove()` judges the file again before unlinking it, so a session that moved past `CLASSIFIED` between the scan and the removal is kept.

What it deliberately keeps, and why:

| Kept | Why |
|---|---|
| State files at any node other than `CLASSIFIED`, `RESOLVED` included | `agentctl reset --reopen-reason` reopens a resolved session from its file, and reports such as the escape-hatch report read the history of past sessions |
| `plan-approved-<digest>.toml`, `plan-version-*.toml` | approvals and plan versions are accumulators keyed by plan content; they outlive the session that wrote them |
| `<session>.delivery.json` sidecars | the delivery record of a session is read after the session ends |
| `.bak-*`, `.SUPERSEDED.json`, `.PHANTOM-backup.json` and other dotted names | hand-made backups and repair records; whoever made them decides when they go |

## Tests and the mutation catalogue

`scripts/tests/test_reaper_runner.py`, `scripts/tests/test_reaper_git_worktrees.py` and `scripts/tests/test_reaper_agentctl_state.py` are hermetic: throw-away git repositories, and `HOME`, the config root, the plugin dir, the project dir and `TMPDIR` redirected under `tmp_path`. A test never runs the real runner against a real checkout.

`scripts/tests/reaper_mutation_control.py` holds a catalogue of named wrong versions of production lines (`keep-wins`, `error-isolation`, `removal-log-before-remove`, `realpath`, `landed-check`, `state-node-filter`, `state-age-floor`, `state-suffix-filter`, ...). `--mutant NAME` applies one to a scratch copy of `scripts/` and runs only its listed tests; exit **1** means killed (the anchor matched once and every listed test failed in its call phase), **0** survived, **3** anchor miss, **4** collection error, **5** a listed test was missing, errored or skipped, **6** unhandled exception. `--control` runs the same tests on the unmutated copy. The in-suite test runs the whole catalogue (about half a minute); the child runs set `REAPER_MUTATION_CHILD=1` so it skips itself. Run the first trial of the catalogue through `scripts/cap-run.sh`: it spawns a pytest per mutant.

## Adding a reaper

Write the module in the right layer, run `python3 scripts/hook-reaper.py --list` to see it registered, then `--dry-run --only <NAME>` to read its verdicts. Keep `scan` free of side effects, return `keep` with a reason for anything you cannot prove removable, and let the runner own the rest.
