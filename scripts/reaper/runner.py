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
  3. Verdicts are joined by realpath. A path is removed only if every verdict on it is
     remove (keep wins), a due selected reaper proposed it, and then once.
  4. A scan exception cancels every removal of the pass and advances no stamp. A removal
     is logged to ``<config root>/reaper/removed.jsonl`` BEFORE it is attempted; a
     remove() exception keeps the path and leaves that reaper's stamp where it was.
  5. SessionStart mode (no flag) always exits 0.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TextIO

from lib import config_root
from reaper import registry
from reaper.contract import KEEP, REMOVE, ReapContext, Verdict
from session_scope import registry as scope_registry

THROTTLED_SENTINEL = "throttled: within window"


def stamp_dir() -> Path:
    return Path.home() / ".local" / "state" / "claude-reaper"


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
        verdicts.append(Verdict(str(item.path), action, str(item.reason), bool(getattr(item, "report", False))))
    return verdicts


def _append_removal_log(log_path: Path, now: float, reaper: registry.Reaper, verdict: Verdict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": _iso(now), "reaper": reaper.name, "layer": reaper.layer, "path": verdict.path, "reason": verdict.reason}
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


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
) -> None:
    """Run one pass over ``reapers`` (every discovered reaper). See the module docstring."""
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

    scanned: "dict[str, list[Verdict]]" = {}
    failed_scans: "list[str]" = []
    for reaper in reapers:
        try:
            scanned[reaper.name] = _normalize(reaper.module.scan(dataclasses.replace(ctx, due=reaper.name in due)))
        except Exception as exc:
            scanned[reaper.name] = []
            failed_scans.append(reaper.name)
            print(f"reaper {reaper.name}: scan failed: {type(exc).__name__}: {exc}", file=err)

    joined: "dict[str, list[tuple[registry.Reaper, Verdict]]]" = {}
    for reaper in reapers:
        for verdict in scanned[reaper.name]:
            joined.setdefault(os.path.realpath(verdict.path), []).append((reaper, verdict))

    def blocker(real: str) -> "str | None":
        if failed_scans:
            return f"pass cancelled: scan of {failed_scans[0]} failed"
        for reaper, verdict in joined[real]:
            if verdict.action == KEEP:
                return f"kept by {reaper.name}"
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

    if not manual and not failed_scans:
        for name in due - failed_removals:
            _write_stamp(stamps, name, now)


def build_context(*, now: float, dry_run: bool) -> ReapContext:
    project = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    try:
        records = scope_registry.load_all(config_root.agentctl_scopes_dir())
    except Exception:
        records = []
    return ReapContext(
        now=now,
        dry_run=dry_run,
        project_dir=Path(project),
        deadletter_dir=default_deadletter_dir(),
        scope_records=records,
    )


def _list(reapers: "list[registry.Reaper]", out: TextIO) -> None:
    for reaper in reapers:
        print(f"{reaper.name} {reaper.layer} {reaper.file}", file=out)


def _discover_default(project_dir: Path) -> "list[registry.Reaper]":
    return registry.discover(project_dir)


def main(
    argv: "list[str] | None" = None,
    *,
    discover: "Callable[[Path], list[registry.Reaper]]" = _discover_default,
) -> int:
    parser = argparse.ArgumentParser(description="Run the registered reapers (see docs/operations/reapers.md).")
    parser.add_argument("--dry-run", action="store_true", help="print one line per verdict; change nothing, touch no stamp")
    parser.add_argument("--force-run", action="store_true", help="run now regardless of throttle stamps, without writing them")
    parser.add_argument("--only", metavar="NAME", help="restrict the run to one reaper (others still veto)")
    parser.add_argument("--list", action="store_true", help="list discovered reapers and exit")
    args = parser.parse_args(argv)
    session_start = not (args.dry_run or args.force_run or args.only or args.list)

    try:
        now = time.time()
        ctx = build_context(now=now, dry_run=args.dry_run)
        reapers = discover(ctx.project_dir)
        if args.list:
            _list(reapers, sys.stdout)
            return 0
        if args.only and args.only not in {r.name for r in reapers}:
            print(f"reaper: no reaper named {args.only!r} (have: {', '.join(r.name for r in reapers) or 'none'})", file=sys.stderr)
            return 2
        execute_pass(
            reapers, ctx,
            dry_run=args.dry_run, force_run=args.force_run, only=args.only,
            stamps=stamp_dir(), log_path=removal_log_path(), out=sys.stdout, err=sys.stderr,
        )
    except Exception as exc:
        print(f"reaper: runner error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 0 if session_start else 1
    return 0
