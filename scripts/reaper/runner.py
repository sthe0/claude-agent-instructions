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
     is logged to ``<config root>/reaper/removed.jsonl`` BEFORE it is attempted, and its outcome
     row (``outcome``: ``removed``, ``refused`` or ``failed``, with ``detail``) after; an attempt
     row with no outcome row means the pass died mid-removal. ``RemovalRefused`` from remove()
     keeps the path with its reason and is not an error; any other remove() exception keeps the
     path and leaves that reaper's stamp where it was.
  5. Every removal row carries ``trigger`` (``unit`` when a reaper unit started the pass, else ``pressure``,
     ``manual`` or ``hook``) and, under systemd, ``invocation_id``. After a live pass each due
     reaper's keeps replace the snapshot ``<config root>/reaper/kept/<NAME>.json`` (a snapshot,
     because a timer re-judges the same items every pass): pass time, trigger, the reaper's own
     ``snapshot_extra(verdicts)`` fields and ``kept``, a list of ``{path, reason, detail, bytes}``
     where ``reason`` is the reaper's ``reason=<slug>`` tag (``unclassified`` without one,
     ``removal-failed`` and ``blocked`` set here) and ``detail`` the full text. A dry run prints
     each selected reaper's ``summary`` after its verdict lines, then the lines of its optional
     ``dry_run_notes(verdicts)`` to stderr.
  6. When a reaper unit started the pass (``unit_run``: AGENT_REAPER_UNIT, which only the units set,
     and INVOCATION_ID) every non-dry pass mode also writes the observation
     ``<config root>/reaper/unit-pass/<INVOCATION_ID>.json`` (``write_unit_observation``; the newest
     50 kept): invocation_id, unit, tmpdir and agent_home as seen (resolved), ``credentials_present``
     for SSH_AUTH_SOCK / SSH_AGENT_PID / GIT_ASKPASS / SSH_ASKPASS, ``reapers_run`` (scans that
     completed) and ``errors``. ``scripts/install-reaper-timer.sh --check`` judges a unit run by it.
  7. SessionStart mode (no flag) always exits 0.
  8. A reaper with ``SESSION_SOURCES`` (a session gate) is due in a SessionStart pass only for a fresh
     interactive top-level start of one of those sources, read from the hook payload on stdin
     (``ctx.session``); otherwise it scans with ``ctx.due`` false, prints nothing and keeps its stamp.
     Manual modes ignore the gate.

``--holders PATH...`` is a read-only query, combined with no other flag: it prints
a tab-separated ``<path> <free|held|unknown> <detail>`` line per path (``liveness.path_holders``) and exits 0 when every
path is free, 3 when one is held, 4 when one is unknown. It runs no reaper and writes nothing.

``--upkeep-only`` is a different mode: it skips scan/remove and the throttle stamps and
calls only each reaper's optional ``upkeep(ctx)``, one reaper at a time with its errors
isolated, under an exclusive flock on ``<stamp dir>/upkeep.lock``. It waits up to
``UPKEEP_LOCK_WAIT_S`` for the lock (``--no-wait``: not at all) and exits 0 without
running when another run still holds it. It exits 0 on every path except invalid argv.
``--dry-run`` is passed through to ``upkeep``; ``--only NAME`` restricts it to one reaper.

``--on-pressure`` is a third mode: a reaper that names filesystem paths in its optional
``pressure_paths(ctx)`` is due when the lowest free percentage among them is strictly below
the config.md row ``reaper-pressure-free-percent`` and its cooldown
(``PRESSURE_COOLDOWN_MIN``, stamp ``<NAME>.pressure.stamp``) has elapsed. Only the due
reapers can remove; every other reaper still scans and vetoes, so the pass removes nothing a
forced pass would keep. It never writes the throttle stamp, runs under a no-wait lock on
``<stamp dir>/pressure.lock`` and appends one line per pass to ``<config root>/reaper/pressure.jsonl``.
A missing or invalid row disarms it. It exits 0 on every path except invalid argv and,
with ``--dry-run`` (which prints a status line per reaper and takes no lock), a runner error.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import json
import os
import re
import select
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Sequence, TextIO

from agentctl import config as agentctl_config
from lib import config_root
from reaper import liveness, registry
from reaper.contract import KEEP, REMOVE, ReapContext, RemovalRefused, Verdict, session_sources_problem
from session_scope import registry as scope_registry

THROTTLED_SENTINEL = "nothing due: within the throttle window or outside a gated reaper's session"
UPKEEP_LOCK_WAIT_S = 120.0
UPKEEP_LOCK_POLL_S = 0.25
PRESSURE_THRESHOLD_KEY = "reaper-pressure-free-percent"
# Minimum minutes between two pressured passes of one reaper; the hook fires at every turn end,
# and a pass that frees nothing must not repeat that often.
PRESSURE_COOLDOWN_MIN = 10.0
# The SessionStart payload is a few hundred bytes; a longer one, or one that does not arrive in time, is no payload.
SESSION_PAYLOAD_MAX_BYTES = 65536
SESSION_PAYLOAD_WAIT_S = 2.0
# Values of CLAUDE_CODE_ENTRYPOINT that mean a person is at the terminal; a headless run carries another one.
INTERACTIVE_ENTRYPOINTS = frozenset({"cli"})
HOLDERS_EXIT_HELD = 3
HOLDERS_EXIT_UNKNOWN = 4


def read_session_payload(stream: "TextIO | None") -> "dict[str, object] | None":
    """The SessionStart hook's JSON object from ``stream``, else None.

    None when there is no stream, it is a terminal, nothing readable arrives within
    ``SESSION_PAYLOAD_WAIT_S``, more than ``SESSION_PAYLOAD_MAX_BYTES`` arrive, or the text is not
    one JSON object. A pipe the writer keeps open is not waited on once the object is complete."""
    if stream is None or stream.isatty():
        return None
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        text = stream.read(SESSION_PAYLOAD_MAX_BYTES + 1)
    else:
        buf = b""
        deadline = time.monotonic() + SESSION_PAYLOAD_WAIT_S
        while len(buf) <= SESSION_PAYLOAD_MAX_BYTES:
            try:
                ready = select.select([fd], [], [], max(0.0, deadline - time.monotonic()))[0]
                chunk = os.read(fd, 4096) if ready else b""
            except OSError:
                return None
            if not chunk:
                break
            buf += chunk
            try:
                json.loads(buf)
                break
            except ValueError:
                continue
        text = buf.decode("utf-8", errors="replace")
    if len(text) > SESSION_PAYLOAD_MAX_BYTES:
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def session_of(payload: "dict[str, object] | None", environ: "dict[str, str] | os._Environ[str]") -> "tuple[str, bool, bool] | None":
    """``(source, top_level, interactive)`` for a payload that names a string ``source``, else None.

    ``top_level`` holds when AGENT_RECURSION_DEPTH is unset or ``0`` (a spawned specialist has a
    higher depth); ``interactive`` when CLAUDE_CODE_ENTRYPOINT is in ``INTERACTIVE_ENTRYPOINTS``."""
    source = payload.get("source") if payload else None
    if not isinstance(source, str) or not source:
        return None
    top_level = environ.get("AGENT_RECURSION_DEPTH") in (None, "0")
    return source, top_level, environ.get("CLAUDE_CODE_ENTRYPOINT") in INTERACTIVE_ENTRYPOINTS


def session_admits(sources: "frozenset[str] | None", session: "tuple[str, bool, bool] | None") -> bool:
    """False when a reaper gates on ``sources`` and ``session`` is not a fresh interactive top-level start of one."""
    if sources is None:
        return True
    return session is not None and session[0] in sources and session[1] and session[2]


def stamp_dir() -> Path:
    return Path.home() / ".local" / "state" / "claude-reaper"


def upkeep_lock_path() -> Path:
    return stamp_dir() / "upkeep.lock"


def pressure_lock_path() -> Path:
    return stamp_dir() / "pressure.lock"


def pressure_log_path() -> Path:
    return config_root.agent_home() / "reaper" / "pressure.jsonl"


def removal_log_path() -> Path:
    return config_root.agent_home() / "reaper" / "removed.jsonl"


def kept_snapshot_dir() -> Path:
    return config_root.agent_home() / "reaper" / "kept"


def default_deadletter_dir() -> Path:
    return config_root.agent_home() / "orphan-worktree-deadletter"


def _stamp_path(directory: Path, name: str) -> Path:
    return directory / (re.sub(r"[^A-Za-z0-9._-]", "_", name) + ".stamp")


def _read_stamp(directory: Path, name: str) -> "float | None":
    try:
        return float(_stamp_path(directory, name).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _throttle_elapsed(reaper: "registry.Reaper", stamps: Path, now: float) -> bool:
    prev = _read_stamp(stamps, reaper.name)
    return prev is None or (now - prev) >= reaper.throttle_hours * 3600.0


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
        size = int(getattr(item, "size", 0) or 0)
        verdicts.append(Verdict(str(item.path), action, str(item.reason), bool(getattr(item, "report", False)), tags, size))
    return verdicts


def unit_run() -> "tuple[str, str] | None":
    """``(unit name, invocation id)`` when a reaper unit started this process, else None. Only a unit sets
    AGENT_REAPER_UNIT; INVOCATION_ID alone is inherited by every descendant of a systemd-started process
    (a session under a systemd-started tmux) and proves only ancestry."""
    unit = os.environ.get("AGENT_REAPER_UNIT")
    invocation = os.environ.get("INVOCATION_ID")
    if unit and invocation:
        return unit, invocation
    return None


def _provenance(*, pressured: bool, manual: bool) -> "dict[str, str]":
    """How this pass was started: ``unit`` when a reaper unit started it (``unit_run``; its invocation id is
    also recorded as ``invocation_id``), else ``pressure``, ``manual`` or the SessionStart ``hook``."""
    started = unit_run()
    if started:
        return {"trigger": "unit", "invocation_id": started[1]}
    return {"trigger": "pressure" if pressured else "manual" if manual else "hook"}


def _append_removal_log(
    log_path: Path, now: float, reaper: registry.Reaper, verdict: Verdict, provenance: "dict[str, str]",
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": _iso(now), "reaper": reaper.name, "layer": reaper.layer, "path": verdict.path, "reason": verdict.reason,
        **provenance,
    }
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


OUTCOME_REMOVED = "removed"
OUTCOME_REFUSED = "refused"
OUTCOME_FAILED = "failed"


def _append_outcome_log(
    log_path: Path, now: float, reaper: registry.Reaper, verdict: Verdict, provenance: "dict[str, str]",
    outcome: str, detail: str, err: TextIO,
) -> None:
    """Append the attempt's outcome row; best effort, since the attempt row is already written."""
    record = {
        "ts": _iso(now), "reaper": reaper.name, "layer": reaper.layer, "path": verdict.path,
        "outcome": outcome, "detail": detail, **provenance,
    }
    try:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"reaper {reaper.name}: could not log the outcome for {verdict.path}: {exc}", file=err)


REASON_TAG = "reason="
UNCLASSIFIED = "unclassified"
REMOVAL_FAILED = "removal-failed"
REMOVAL_REFUSED = "removal-refused"
BLOCKED = "blocked"


def _slug(verdict: Verdict) -> str:
    """The reaper's own reason slug (its ``reason=<slug>`` tag), else ``unclassified``."""
    return next((tag[len(REASON_TAG):] for tag in verdict.tags if tag.startswith(REASON_TAG)), UNCLASSIFIED)


def _write_kept(
    kept: Path, name: str, now: float, provenance: "dict[str, str]", keeps: "list[tuple[str, str, str, int]]",
    extra: "dict[str, object] | None" = None,
) -> None:
    """Replace ``<kept>/<name>.json`` atomically with every keep of the pass.

    An entry is ``path``, ``reason`` (a slug), ``detail`` (the full text, any OSError string
    included) and ``bytes``. ``extra`` is the reaper's own ``snapshot_extra`` fields. A snapshot,
    not a log: a timer re-judges the same items every pass. Best effort."""
    record = {
        "reaper": name, "pass_time": _iso(now), **provenance, **(extra or {}),
        "kept": [
            {"path": path, "reason": reason, "detail": detail, "bytes": size} for path, reason, detail, size in keeps
        ],
    }
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    tmp = kept / f".{safe}.json.tmp"
    try:
        kept.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, kept / f"{safe}.json")
    except OSError:
        pass


def _print_summaries(
    selected: "list[registry.Reaper]", due: "set[str]", scanned: "dict[str, list[Verdict]]", out: TextIO, err: TextIO,
) -> None:
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


def _snapshot_extra(reaper: registry.Reaper, verdicts: "list[Verdict]", err: TextIO) -> "dict[str, object]":
    hook = getattr(reaper.module, "snapshot_extra", None)
    if not callable(hook):
        return {}
    try:
        return dict(hook(verdicts))
    except Exception as exc:
        print(f"reaper {reaper.name}: snapshot_extra failed: {type(exc).__name__}: {exc}", file=err)
        return {}


def _print_notes(
    selected: "list[registry.Reaper]", due: "set[str]", scanned: "dict[str, list[Verdict]]", err: TextIO,
) -> None:
    """A dry run's diagnostic lines (``dry_run_notes``) go to stderr, so stdout keeps its closed format."""
    for reaper in selected:
        notes = getattr(reaper.module, "dry_run_notes", None)
        if reaper.name not in due or not callable(notes):
            continue
        try:
            lines = list(notes(scanned[reaper.name]))
        except Exception as exc:
            print(f"reaper {reaper.name}: dry_run_notes failed: {type(exc).__name__}: {exc}", file=err)
            continue
        for line in lines:
            print(line, file=err)


def _nested(real_a: str, real_b: str) -> bool:
    """Same path, or one an ancestor of the other, compared component-wise."""
    return os.path.commonpath([real_a, real_b]) in (real_a, real_b)


def read_pressure_threshold(config_path: "Path | None" = None) -> "float | None":
    """The config.md row as a percentage strictly between 0 and 100; None (disarmed) otherwise."""
    try:
        value = float(agentctl_config.parse_config_md(config_path)[PRESSURE_THRESHOLD_KEY])
    except (OSError, UnicodeDecodeError, KeyError, ValueError):
        return None
    return value if 0.0 < value < 100.0 else None


def free_percent(path: str) -> "float | None":
    """Free percentage (``f_bavail / f_blocks``) of the filesystem holding ``path``, measured at
    its nearest existing ancestor; None when it cannot be read."""
    probe = os.path.abspath(path)
    while not os.path.lexists(probe) and probe != os.path.dirname(probe):
        probe = os.path.dirname(probe)
    try:
        usage = os.statvfs(probe)
    except OSError:
        return None
    return 100.0 * usage.f_bavail / usage.f_blocks if usage.f_blocks else None


@dataclasses.dataclass(frozen=True)
class PressureReading:
    name: str
    free: float
    status: str  # "due" | "ok" | "cooldown"

    def line(self, threshold: float) -> str:
        return f"{self.name} free {self.free:.1f}% threshold {threshold:.1f}% -> {self.status}"


def pressure_readings(
    reapers: "list[registry.Reaper]",
    ctx: ReapContext,
    threshold: float,
    stamps: Path,
    free_fn: "Callable[[str], float | None]" = free_percent,
) -> "list[PressureReading]":
    """One reading per reaper that names readable ``pressure_paths``, judged on its lowest free
    percentage: ``due`` below the threshold once its cooldown has elapsed, ``cooldown`` below it
    before then, ``ok`` otherwise (a value equal to the threshold is ok). A reaper whose paths
    cannot be named or read gets no reading and is never due."""
    readings = []
    for reaper in reapers:
        paths_of = getattr(reaper.module, "pressure_paths", None)
        if not callable(paths_of):
            continue
        try:
            frees = [free_fn(str(path)) for path in paths_of(ctx)]
        except Exception:
            continue
        if not frees or any(free is None for free in frees):
            continue
        free = min(frees)
        if free >= threshold:
            status = "ok"
        else:
            prev = _read_stamp(stamps, f"{reaper.name}.pressure")
            elapsed = prev is None or (ctx.now - prev) >= PRESSURE_COOLDOWN_MIN * 60.0
            status = "due" if elapsed else "cooldown"
        readings.append(PressureReading(reaper.name, free, status))
    return readings


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
    pressured: "frozenset[str] | None" = None,
    kept_dir: "Path | None" = None,
    observe: "dict[str, int] | None" = None,
) -> int:
    """Run one pass over ``reapers`` (every discovered reaper). See the module docstring.

    ``kept_dir`` makes a live pass write each due reaper's kept snapshot there; None writes none.
    ``observe`` receives ``reapers_run`` (scans that completed) and ``errors`` (failed scans,
    failed removals, skipped modules, an unreadable scope registry) for the unit-pass observation.

    ``skipped_modules`` are the discovery warnings for modules that did not load.
    ``pressured`` makes exactly those reapers due and selected (others still scan and veto)
    and, like a manual run, writes no throttle stamp. Returns the number of paths removed.
    """
    now = ctx.now
    manual = dry_run or force_run or only is not None or pressured is not None
    selected = [r for r in reapers if only in (None, r.name) and (pressured is None or r.name in pressured)]
    if pressured is not None:
        ctx = dataclasses.replace(ctx, pressured=frozenset(pressured))
        due = {r.name for r in selected}
    elif dry_run or force_run:
        due = {r.name for r in selected}
    elif only is not None:
        due = {only}
    else:
        due = {
            r.name for r in selected
            if session_admits(getattr(r.module, "SESSION_SOURCES", None), ctx.session)
            and _throttle_elapsed(r, stamps, now)
        }
    if not due:
        print(THROTTLED_SENTINEL, file=err)
        return 0

    cancelled: "list[str]" = []
    if ctx.scope_registry_error:
        cancelled.append(f"scope registry unreadable: {ctx.scope_registry_error}")
    if skipped_modules:
        cancelled.append(f"{len(skipped_modules)} reaper module(s) skipped at discovery")
    scanned: "dict[str, list[Verdict]]" = {}
    failed_scans: "set[str]" = set()
    for reaper in reapers:
        try:
            scanned[reaper.name] = _normalize(reaper.module.scan(dataclasses.replace(ctx, due=reaper.name in due)))
        except Exception as exc:
            scanned[reaper.name] = []
            failed_scans.add(reaper.name)
            cancelled.append(f"scan of {reaper.name} failed")
            print(f"reaper {reaper.name}: scan failed: {type(exc).__name__}: {exc}", file=err)

    if observe is not None:
        observe["reapers_run"] += len(reapers) - len(failed_scans)
        observe["errors"] += len(failed_scans) + len(skipped_modules) + (1 if ctx.scope_registry_error else 0)

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
        _print_summaries(selected, due, scanned, out, err)
        _print_notes(selected, due, scanned, err)
        return 0

    provenance = _provenance(pressured=pressured is not None, manual=manual)
    removed = 0
    failed_removals: "set[str]" = set()
    failed_paths: "set[str]" = set()
    refused_paths: "dict[str, str]" = {}
    removed_paths: "set[str]" = set()
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
        logged = False
        try:
            _append_removal_log(log_path, now, reaper, verdict, provenance)
            logged = True
            if reaper.module.remove(verdict.path, ctx) is False:
                raise RuntimeError("remove() returned False")
        except RemovalRefused as exc:
            refused_paths[verdict.path] = str(exc)
            _append_outcome_log(log_path, now, reaper, verdict, provenance, OUTCOME_REFUSED, str(exc), err)
            print(f"reaper {reaper.name}: refused to remove {verdict.path}: {exc}", file=err)
            continue
        except Exception as exc:
            failed_removals.add(reaper.name)
            failed_paths.add(verdict.path)
            if logged:
                _append_outcome_log(
                    log_path, now, reaper, verdict, provenance, OUTCOME_FAILED, f"{type(exc).__name__}: {exc}", err,
                )
            print(f"reaper {reaper.name}: FAILED to remove {verdict.path}: {type(exc).__name__}: {exc}", file=err)
            continue
        _append_outcome_log(log_path, now, reaper, verdict, provenance, OUTCOME_REMOVED, verdict.reason, err)
        removed += 1
        removed_paths.add(verdict.path)
        print(f"reaper {reaper.name}: removed {verdict.path} ({verdict.reason})", file=err)

    if observe is not None:
        observe["errors"] += len(failed_paths)
    _print_summaries(selected, due, scanned, out, err)

    if kept_dir is not None:
        for reaper in selected:
            if reaper.name not in due or reaper.name in failed_scans:
                continue
            keeps = []
            for verdict in scanned[reaper.name]:
                if verdict.action == KEEP:
                    keeps.append((verdict.path, _slug(verdict), verdict.reason, verdict.size))
                elif verdict.path in failed_paths:
                    keeps.append((verdict.path, REMOVAL_FAILED, f"removal failed: {verdict.reason}", verdict.size))
                elif verdict.path in refused_paths:
                    keeps.append((verdict.path, REMOVAL_REFUSED, refused_paths[verdict.path], verdict.size))
                elif verdict.path not in removed_paths:
                    why = blocker(os.path.realpath(verdict.path))
                    keeps.append((verdict.path, BLOCKED if why else UNCLASSIFIED, why or verdict.reason, verdict.size))
            _write_kept(kept_dir, reaper.name, now, provenance, keeps, _snapshot_extra(reaper, scanned[reaper.name], err))

    if not manual and not cancelled:
        for name in due - failed_removals:
            _write_stamp(stamps, name, now)
    return removed


def execute_pressure(
    reapers: "list[registry.Reaper]",
    ctx: ReapContext,
    *,
    threshold: float,
    stamps: Path,
    log_path: Path,
    pressure_log: Path,
    out: TextIO,
    err: TextIO,
    skipped_modules: "Sequence[str]" = (),
    kept_dir: "Path | None" = None,
    observe: "dict[str, int] | None" = None,
) -> None:
    """One ``--on-pressure`` pass: read the free space of each reaper that names pressure paths,
    run the due ones through ``execute_pass`` and start their cooldown.

    A dry run prints one status line per reaper, then what the pass would do; it writes no
    cooldown stamp and no pressure record. A live run prints only the readings that are not ok.
    """
    readings = pressure_readings(reapers, ctx, threshold, stamps, free_percent)
    for reading in readings:
        if ctx.dry_run or reading.status != "ok":
            print(reading.line(threshold), file=out if ctx.dry_run else err)
    due = frozenset(r.name for r in readings if r.status == "due")
    if not due:
        return
    removed = execute_pass(
        reapers, ctx,
        dry_run=ctx.dry_run, force_run=False, only=None,
        stamps=stamps, log_path=log_path, out=out, err=err,
        skipped_modules=skipped_modules, pressured=due, kept_dir=kept_dir, observe=observe,
    )
    if ctx.dry_run:
        return
    for name in due:
        _write_stamp(stamps, f"{name}.pressure", ctx.now)
    record = {
        "ts": _iso(ctx.now), "threshold": threshold,
        "free": {r.name: round(r.free, 2) for r in readings}, "due": sorted(due), "removed": removed,
    }
    try:
        pressure_log.parent.mkdir(parents=True, exist_ok=True)
        with pressure_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"reaper: pressure log unavailable: {type(exc).__name__}: {exc}", file=err)


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
    """Discovered reapers minus those whose ``SESSION_SOURCES`` is malformed, each skipped with one warning."""
    usable = []
    for reaper in registry.discover(project_dir, warn=warn):
        problem = session_sources_problem(reaper.module)
        if problem:
            warn(f"reaper: skipped {reaper.file}: {problem}")
        else:
            usable.append(reaper)
    return usable


def _one_field(text: str) -> str:
    return text.replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def _holders_report(paths: "list[str]", ctx: ReapContext, out: TextIO) -> int:
    """Print ``<path>\\t<free|held|unknown>\\t<detail>`` per path; exit 0 all free, 4 any unknown, else 3 any held."""
    answers = liveness.path_holders(paths, ctx)
    for path in paths:
        answer = answers[path]
        print(f"{_one_field(path)}\t{answer.status}\t{_one_field(answer.report())}", file=out)
    statuses = {answer.status for answer in answers.values()}
    if liveness.UNKNOWN_STATUS in statuses:
        return HOLDERS_EXIT_UNKNOWN
    return HOLDERS_EXIT_HELD if liveness.HELD_STATUS in statuses else 0


UNIT_PASS_KEEP = 50
CREDENTIAL_VARS = ("SSH_AUTH_SOCK", "SSH_AGENT_PID", "GIT_ASKPASS", "SSH_ASKPASS")


def unit_pass_dir() -> Path:
    return config_root.agent_home() / "reaper" / "unit-pass"


def write_unit_observation(observe: "dict[str, int]", err: TextIO) -> None:
    """When a reaper unit started this pass (``unit_run``) record what it saw, for ``install-reaper-timer.sh
    --check``: unit name, temp root, agent home, which credential variables are present, reapers whose scan
    ran, errors. Atomic; the newest ``UNIT_PASS_KEEP`` files stay. Best effort."""
    started = unit_run()
    if not started or not re.fullmatch(r"[A-Za-z0-9]+", started[1]):
        return
    unit, invocation = started
    record = {
        "invocation_id": invocation,
        "unit": unit,
        "tmpdir": os.path.realpath(tempfile.gettempdir()),
        "agent_home": os.path.realpath(config_root.agent_home()),
        "credentials_present": {name: name in os.environ for name in CREDENTIAL_VARS},
        "reapers_run": observe["reapers_run"],
        "errors": observe["errors"],
    }
    try:
        directory = unit_pass_dir()
        directory.mkdir(parents=True, exist_ok=True)
        tmp = directory / f".{invocation}.json.tmp"
        tmp.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, directory / f"{invocation}.json")
        files = sorted(directory.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
        for stale in files[UNIT_PASS_KEEP:]:
            stale.unlink(missing_ok=True)
    except OSError as exc:
        print(f"reaper: unit-pass observation unavailable: {type(exc).__name__}: {exc}", file=err)


def _run_on_pressure(
    reapers: "list[registry.Reaper]", ctx: ReapContext, skipped: "list[str]", observe: "dict[str, int] | None" = None,
) -> None:
    threshold = read_pressure_threshold()
    if threshold is None:
        print(f"reaper: pressure mode disarmed: config.md row {PRESSURE_THRESHOLD_KEY} is missing or not a percentage above 0 and below 100", file=sys.stderr)
        return

    def run() -> None:
        execute_pressure(
            reapers, ctx, threshold=threshold, stamps=stamp_dir(), log_path=removal_log_path(),
            pressure_log=pressure_log_path(), out=sys.stdout, err=sys.stderr, skipped_modules=skipped,
            kept_dir=kept_snapshot_dir(), observe=observe,
        )

    if ctx.dry_run:
        run()
        return
    with _upkeep_lock(pressure_lock_path(), 0.0, sys.stderr) as acquired:
        if acquired:
            run()


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
    parser.add_argument("--on-pressure", action="store_true", help="run the reapers whose pressure paths are short of free space (config.md row reaper-pressure-free-percent), under a no-wait lock")
    parser.add_argument("--holders", nargs="+", metavar="PATH", help="print who holds each path (free, held or unknown) and exit 0 if all free, 3 if any held, 4 if any unknown; changes nothing")
    args = parser.parse_args(argv)
    if args.holders and (args.dry_run or args.force_run or args.only or args.list or args.upkeep_only or args.no_wait or args.on_pressure):
        parser.error("--holders combines with no other flag")
    if args.no_wait and not args.upkeep_only:
        parser.error("--no-wait needs --upkeep-only")
    if args.on_pressure and (args.upkeep_only or args.only or args.force_run):
        parser.error("--on-pressure combines only with --dry-run")
    fail_open = not (args.dry_run or args.force_run or args.only or args.list or args.holders) or args.upkeep_only
    observe = {"reapers_run": 0, "errors": 0}
    records_pass = not (args.list or args.upkeep_only or args.dry_run or args.holders)
    manual = bool(args.dry_run or args.force_run or args.only or args.on_pressure)

    try:
        now = time.time()
        ctx = build_context(now=now, dry_run=args.dry_run)
        if args.holders:
            return _holders_report(args.holders, ctx, sys.stdout)
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
        if args.on_pressure:
            _run_on_pressure(reapers, ctx, skipped, observe)
            return 0
        if not manual and any(
            hasattr(r.module, "SESSION_SOURCES") and _throttle_elapsed(r, stamp_dir(), now) for r in reapers
        ):
            ctx = dataclasses.replace(ctx, session=session_of(read_session_payload(sys.stdin), os.environ))
        execute_pass(
            reapers, ctx,
            dry_run=args.dry_run, force_run=args.force_run, only=args.only,
            stamps=stamp_dir(), log_path=removal_log_path(), out=sys.stdout, err=sys.stderr,
            skipped_modules=skipped, kept_dir=kept_snapshot_dir(), observe=observe,
        )
    except Exception as exc:
        observe["errors"] += 1
        print(f"reaper: runner error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 0 if fail_open else 1
    finally:
        if records_pass:
            write_unit_observation(observe, sys.stderr)
    return 0
