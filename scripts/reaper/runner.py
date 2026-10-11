"""The reaper runner: throttle, ownership, arbitration, log and error isolation.

Difficulty removed: a deletion is irreversible, so every rule that makes it safe must
be identical for every kind of residue and cannot be left to each reaper. The runner
owns that part; a reaper (``reaper.contract``) only proves what it can about its own
kind of residue.

One pass:
  1. A pass starts when at least one selected reaper is due (its per-reaper stamp under
     ``~/.local/state/claude-reaper/<NAME>.stamp`` is missing or older than its
     THROTTLE_HOURS). ``--dry-run`` and ``--force-run`` make every selected reaper due;
     ``--only NAME`` selects one reaper and makes it due. Neither writes a stamp.
  2. Every discovered reaper's ``scan`` runs, due or not, so its KEEP verdicts always count.
  3. Verdicts are joined by realpath. A path is removed only if no KEEP verdict names it,
     a directory inside it, or a directory containing it (keep wins), a due selected
     reaper proposed it, and then once.
  4. The pass is cancelled — nothing removed, no stamp advanced, lines still printed —
     when a scan raises, when a reaper module was skipped at discovery (its KEEP vetoes
     are missing), or when a session-scope record could not be read (an owner may be
     missing). A removal
     is logged to ``<config root>/reaper/removed.jsonl`` BEFORE it is attempted; a
     remove() exception keeps the path and leaves that reaper's stamp where it was.
  5. SessionStart mode (no flag) always exits 0.

``--upkeep-only`` is a different mode: it skips scan/remove and the throttle stamps and
calls only each reaper's optional ``upkeep(ctx)``, one reaper at a time with its errors
isolated, under an exclusive flock on ``<stamp dir>/upkeep.lock``. It waits up to
``UPKEEP_LOCK_WAIT_S`` for the lock (``--no-wait``: not at all) and exits 0 without
running when another run still holds it. It exits 0 on every path except invalid argv.
``--dry-run`` is passed through to ``upkeep``; ``--only NAME`` restricts it to one reaper.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Sequence, TextIO

from lib import config_root
from reaper import registry
from reaper.contract import KEEP, REMOVE, ReapContext, Verdict
from session_scope import registry as scope_registry

THROTTLED_SENTINEL = "throttled: within window"
UPKEEP_LOCK_WAIT_S = 120.0
UPKEEP_LOCK_POLL_S = 0.25


def stamp_dir() -> Path:
    return Path.home() / ".local" / "state" / "claude-reaper"


def upkeep_lock_path() -> Path:
    return stamp_dir() / "upkeep.lock"


def removal_log_path() -> Path:
    return config_root.agent_home() / "reaper" / "removed.jsonl"


def default_deadletter_dir() -> Path:
    return config_root.agent_home() / "orphan-worktree-deadletter"


def _stamp_path(directory: Path, name: str) -> Path:
    return directory / (re.sub(r"[^A-Za-z0-9._-]", "_", name) + ".stamp")


def _read_stamp(directory: Path, name: str) -> "float | None":
    try:
        return float(_stamp_path(directory, name).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _write_stamp(directory: Path, name: str, now: float) -> None:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        _stamp_path(directory, name).write_text(str(now), encoding="utf-8")
    except OSError:
        pass


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _normalize(raw: object) -> "list[Verdict]":
    verdicts = []
    for item in raw:  # type: ignore[attr-defined]
        action = item.action
        if action not in (REMOVE, KEEP):
            raise ValueError(f"verdict action {action!r} is neither remove nor keep")
        tags = tuple(str(tag) for tag in getattr(item, "tags", ()))
        verdicts.append(Verdict(str(item.path), action, str(item.reason), bool(getattr(item, "report", False)), tags))
    return verdicts


def _append_removal_log(log_path: Path, now: float, reaper: registry.Reaper, verdict: Verdict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": _iso(now), "reaper": reaper.name, "layer": reaper.layer, "path": verdict.path, "reason": verdict.reason}
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _nested(real_a: str, real_b: str) -> bool:
    """Same path, or one an ancestor of the other, compared component-wise."""
    return os.path.commonpath([real_a, real_b]) in (real_a, real_b)


def execute_pass(
    reapers: "list[registry.Reaper]",
    ctx: ReapContext,
    *,
    dry_run: bool,
    force_run: bool,
    only: "str | None",
    stamps: Path,
    log_path: Path,
    out: TextIO,
    err: TextIO,
    skipped_modules: "Sequence[str]" = (),
) -> None:
    """Run one pass over ``reapers`` (every discovered reaper). See the module docstring.

    ``skipped_modules`` are the discovery warnings for modules that did not load.
    """
    now = ctx.now
    manual = dry_run or force_run or only is not None
    selected = [r for r in reapers if only in (None, r.name)]
    if dry_run or force_run:
        due = {r.name for r in selected}
    elif only is not None:
        due = {only}
    else:
        due = {
            r.name for r in selected
            if (prev := _read_stamp(stamps, r.name)) is None or (now - prev) >= r.throttle_hours * 3600.0
        }
    if not due:
        print(THROTTLED_SENTINEL, file=err)
        return

    cancelled: "list[str]" = []
    if ctx.scope_registry_error:
        cancelled.append(f"scope registry unreadable: {ctx.scope_registry_error}")
    if skipped_modules:
        cancelled.append(f"{len(skipped_modules)} reaper module(s) skipped at discovery")
    scanned: "dict[str, list[Verdict]]" = {}
    for reaper in reapers:
        try:
            scanned[reaper.name] = _normalize(reaper.module.scan(dataclasses.replace(ctx, due=reaper.name in due)))
        except Exception as exc:
            scanned[reaper.name] = []
            cancelled.append(f"scan of {reaper.name} failed")
            print(f"reaper {reaper.name}: scan failed: {type(exc).__name__}: {exc}", file=err)

    joined: "dict[str, list[tuple[registry.Reaper, Verdict]]]" = {}
    for reaper in reapers:
        for verdict in scanned[reaper.name]:
            joined.setdefault(os.path.realpath(verdict.path), []).append((reaper, verdict))
    keepers = [
        (os.path.realpath(v.path), r.name) for r in reapers for v in scanned[r.name] if v.action == KEEP
    ]

    def blocker(real: str) -> "str | None":
        if cancelled:
            return f"pass cancelled: {cancelled[0]}"
        for held, name in keepers:
            if _nested(real, held):
                return f"kept by {name}"
        return None

    if dry_run:
        for reaper in selected:
            for verdict in scanned[reaper.name]:
                reason = blocker(os.path.realpath(verdict.path)) if verdict.action == REMOVE else None
                if verdict.action == REMOVE and reason is None:
                    print(f"{reaper.name} REMOVE {verdict.path} ({verdict.reason})", file=out)
                else:
                    print(f"{reaper.name} KEEP {verdict.path} ({reason or verdict.reason})", file=out)
        return

    failed_removals: "set[str]" = set()
    for real, entries in joined.items():
        if blocker(real) is not None:
            continue
        executor = next(
            ((r, v) for r, v in entries if v.action == REMOVE and r.name in due and only in (None, r.name)),
            None,
        )
        if executor is None:
            continue
        reaper, verdict = executor
        try:
            _append_removal_log(log_path, now, reaper, verdict)
            if reaper.module.remove(verdict.path, ctx) is False:
                raise RuntimeError("remove() returned False")
        except Exception as exc:
            failed_removals.add(reaper.name)
            print(f"reaper {reaper.name}: FAILED to remove {verdict.path}: {type(exc).__name__}: {exc}", file=err)
            continue
        print(f"reaper {reaper.name}: removed {verdict.path} ({verdict.reason})", file=err)

    for reaper in selected:
        summarize = getattr(reaper.module, "summary", None)
        if reaper.name not in due or not callable(summarize):
            continue
        try:
            line = summarize(scanned[reaper.name])
        except Exception as exc:
            print(f"reaper {reaper.name}: summary failed: {type(exc).__name__}: {exc}", file=err)
            continue
        if line:
            print(line, file=out)

    if not manual and not cancelled:
        for name in due - failed_removals:
            _write_stamp(stamps, name, now)


@contextlib.contextmanager
def _upkeep_lock(path: Path, wait_s: float, err: TextIO) -> "Iterator[bool]":
    """Yield True under the exclusive lock, False when another run holds it past ``wait_s``.

    A lock file that cannot be created yields True unlocked: upkeep is idempotent, so a
    second concurrent run is wasteful, not wrong.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a")
    except OSError as exc:
        print(f"reaper: upkeep lock unavailable ({type(exc).__name__}: {exc}); running unlocked", file=err)
        yield True
        return
    try:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(UPKEEP_LOCK_POLL_S)
        yield True
    finally:
        handle.close()


def execute_upkeep(
    reapers: "list[registry.Reaper]", ctx: ReapContext, *, only: "str | None", out: TextIO, err: TextIO,
) -> None:
    """Call each selected reaper's ``upkeep``; one failing reaper never stops the others."""
    for reaper in reapers:
        step = getattr(reaper.module, "upkeep", None)
        if only not in (None, reaper.name) or not callable(step):
            continue
        try:
            lines = [str(line) for line in step(ctx) or ()]
        except Exception as exc:
            print(f"reaper {reaper.name}: upkeep failed: {type(exc).__name__}: {exc}", file=err)
            continue
        for line in lines:
            print(line, file=out)


def build_context(*, now: float, dry_run: bool) -> ReapContext:
    project = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    records: "list[scope_registry.ScopeRecord]" = []
    error = None
    try:
        records = scope_registry.load_all(config_root.agentctl_scopes_dir(), strict=True)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    return ReapContext(
        now=now,
        dry_run=dry_run,
        project_dir=Path(project),
        deadletter_dir=default_deadletter_dir(),
        scope_records=records,
        scope_registry_error=error,
    )


def _list(reapers: "list[registry.Reaper]", out: TextIO) -> None:
    for reaper in reapers:
        print(f"{reaper.name} {reaper.layer} {reaper.file}", file=out)


def _discover_default(project_dir: Path, warn: "Callable[[str], None]") -> "list[registry.Reaper]":
    return registry.discover(project_dir, warn=warn)


def main(
    argv: "list[str] | None" = None,
    *,
    discover: "Callable[[Path, Callable[[str], None]], list[registry.Reaper]]" = _discover_default,
) -> int:
    parser = argparse.ArgumentParser(description="Run the registered reapers (see docs/operations/reapers.md).")
    parser.add_argument("--dry-run", action="store_true", help="print one line per verdict; change nothing, touch no stamp")
    parser.add_argument("--force-run", action="store_true", help="run now regardless of throttle stamps, without writing them")
    parser.add_argument("--only", metavar="NAME", help="restrict the run to one reaper (others still veto)")
    parser.add_argument("--list", action="store_true", help="list discovered reapers and exit")
    parser.add_argument("--upkeep-only", action="store_true", help="run only each reaper's upkeep (e.g. the branch backup push), under a lock")
    parser.add_argument("--no-wait", action="store_true", help="with --upkeep-only: exit 0 at once if another upkeep run holds the lock")
    args = parser.parse_args(argv)
    if args.no_wait and not args.upkeep_only:
        parser.error("--no-wait needs --upkeep-only")
    fail_open = not (args.dry_run or args.force_run or args.only or args.list) or args.upkeep_only

    try:
        now = time.time()
        ctx = build_context(now=now, dry_run=args.dry_run)
        skipped: "list[str]" = []

        def warn(line: str) -> None:
            skipped.append(line)
            print(line, file=sys.stderr)

        reapers = discover(ctx.project_dir, warn)
        if args.list:
            _list(reapers, sys.stdout)
            return 0
        if args.only and args.only not in {r.name for r in reapers}:
            print(f"reaper: no reaper named {args.only!r} (have: {', '.join(r.name for r in reapers) or 'none'})", file=sys.stderr)
            return 2
        if args.upkeep_only:
            wait_s = 0.0 if args.no_wait else UPKEEP_LOCK_WAIT_S
            with _upkeep_lock(upkeep_lock_path(), wait_s, sys.stderr) as acquired:
                if acquired:
                    execute_upkeep(reapers, ctx, only=args.only, out=sys.stdout, err=sys.stderr)
            return 0
        execute_pass(
            reapers, ctx,
            dry_run=args.dry_run, force_run=args.force_run, only=args.only,
            stamps=stamp_dir(), log_path=removal_log_path(), out=sys.stdout, err=sys.stderr,
            skipped_modules=skipped,
        )
    except Exception as exc:
        print(f"reaper: runner error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 0 if fail_open else 1
    return 0
