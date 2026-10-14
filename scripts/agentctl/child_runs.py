"""Locating the linked child of a recorded dispatch, and ending a run its dispatcher left open.

`cmd_dispatch` records each linked spawn as a `{child_session, stage_index, started, ended,
dispatcher_pid}` interval in `effort_child_runs`. The child's session id is not known before
the spawn, so an open run carries `child_session: None` until it is attributed. The CLI (at
close, and when it finds a dead dispatcher) and the prompt hook (when a gap straddles a dead
dispatcher's run) must agree on which session that is and when the run ended, or the same
interval is booked twice — so both go through the functions below, which read the state
directory directly (plain JSON, stdlib only: the hook has to survive a broken checkout).

A dead dispatcher's run is closed IN the state it belongs to, by whoever notices first, so a
later reader sees the same `ended` instead of a child file that moved on.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
from pathlib import Path

# History timestamps carry whole seconds, so a child born in the second a run started in
# can read as up to a second earlier than `started`.
BIRTH_TOLERANCE_S = 2.0

# A state file that cannot be parsed is read as half-written (a writer is mid-save) for this
# long after its last write; past it, it is just a broken file and no longer holds a run open.
UNREADABLE_GRACE_S = 60.0


def pid_alive(pid) -> bool:
    """Whether a process with this pid exists (a reused pid reads as alive)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _born(data: dict, mtime: float) -> float:
    """When the session came into being: its earliest history timestamp, else its last write."""
    stamps = []
    for event in data.get("history") or ():
        ts = event.get("ts") if isinstance(event, dict) else None
        if isinstance(ts, str):
            try:
                stamps.append(dt.datetime.fromisoformat(ts).timestamp())
            except ValueError:
                continue
    return min(stamps) if stamps else mtime


def last_write(state_dir: Path, session) -> float | None:
    """When `session`'s state file was last written, None when it has none."""
    return _mtime(Path(state_dir) / f"{session}.json") if session else None


def is_child_of(state_dir: Path, parent: str, session: str, stage_index) -> bool:
    """Whether `session` has a state file whose `parent_link` names `parent` and the stage."""
    data = _read(Path(state_dir) / f"{session}.json") if session else None
    link = data.get("parent_link") if data else None
    return (isinstance(link, dict) and link.get("parent_session") == parent
            and link.get("stage_index") == stage_index)


def discover_child(state_dir: Path, parent: str, run: dict, runs) -> str | None:
    """The linked child session `run` started, found from the state directory.

    A candidate has a `parent_link` naming `parent` and the run's stage, is not already
    attributed to another of `runs` (open or closed), was last written at or after the run
    began AND came into being at or after it — a session left over from an earlier dispatch
    whose process is still writing is therefore never taken for this child. The earliest
    born wins. None when there is none: the spawn was unlinked."""
    return _scan(state_dir, parent, run, runs, time.time())[0]


def _scan(state_dir: Path, parent: str, run: dict, runs, now: float) -> tuple[str | None, bool]:
    """`(child, uncertain)`: the linked session `run` started, and whether a state file that
    could have been it was unreadable at the moment (half-written, see UNREADABLE_GRACE_S)."""
    started = run.get("started")
    if not isinstance(started, (int, float)):
        return None, False
    claimed = {r.get("child_session") for r in runs if r is not run and isinstance(r, dict)}
    best: tuple[float, str] | None = None
    uncertain = False
    try:
        files = sorted(Path(state_dir).glob("*.json"))
    except OSError:
        return None, False
    for path in files:
        if path.stem == parent or path.stem in claimed:
            continue
        written = _mtime(path)
        if written is None or written < started:
            continue
        data = _read(path)
        if data is None:
            uncertain = uncertain or now - written <= UNREADABLE_GRACE_S
            continue
        link = data.get("parent_link")
        if not (isinstance(link, dict) and link.get("parent_session") == parent
                and link.get("stage_index") == run.get("stage_index")):
            continue
        born = _born(data, written)
        if born + BIRTH_TOLERANCE_S < started:
            continue
        if best is None or born < best[0]:
            best = (born, path.stem)
    return (best[1] if best else None), uncertain


def child_end(state_dir: Path, child) -> float | None:
    """When `child` last did anything: its state file's last write, or the end of one of its
    own child runs if that is later. None when it has no state file."""
    end = last_write(state_dir, child)
    data = _read(Path(state_dir) / f"{child}.json") if child else None
    for run in (data or {}).get("effort_child_runs") or ():
        ended = run.get("ended") if isinstance(run, dict) else None
        if isinstance(ended, (int, float)) and end is not None:
            end = max(end, float(ended))
    return end


def close_dead_runs(runs, state_dir: Path, parent: str, *, alive=pid_alive,
                    now: float | None = None) -> bool:
    """Close, in `runs`, each open run whose dispatcher process is gone: it ended when its
    child last did anything (`child_end`), and never before it began. A run whose child
    cannot be found ends where it started, so it adds nothing — unless a state file that
    could have been the child is unreadable just now, when the run stays open for the next
    read rather than being closed on a guess. Attributes the child on the way.
    True when any run changed."""
    changed = False
    now = time.time() if now is None else now
    for run in runs or ():
        if not isinstance(run, dict) or run.get("ended") is not None:
            continue
        started = run.get("started")
        if not isinstance(started, (int, float)) or alive(run.get("dispatcher_pid")):
            continue
        if not run.get("child_session"):
            child, uncertain = _scan(state_dir, parent, run, runs, now)
            if child is None and uncertain:
                continue
            run["child_session"] = child
        end = child_end(state_dir, run["child_session"])
        run["ended"] = max(float(started), end if end is not None else float(started))
        changed = True
    return changed


RUN_KEY = ("started", "dispatcher_pid", "stage_index")


def note_gap_loss(runs, lo: float, hi: float, minutes_without, *, alive=pid_alive) -> None:
    """Record on the open run of a live dispatcher that overlaps the prompt gap `[lo, hi]`
    most how many minutes the hook booked less because of that run alone
    (`minutes_without(other_runs)` is the loss were the run absent, the other runs staying).
    The dispatcher books them back if the run turns out to have no linked child and is
    dropped (`gap_loss_min`); the part of the gap other runs cover is theirs, not noted."""
    best, best_overlap = None, 0.0
    for run in runs or ():
        started = run.get("started") if isinstance(run, dict) else None
        if (not isinstance(started, (int, float)) or run.get("ended") is not None
                or not alive(run.get("dispatcher_pid"))):
            continue
        overlap = hi - max(lo, float(started))
        if overlap > best_overlap:
            best, best_overlap = run, overlap
    if best is None:
        return
    minutes = minutes_without([r for r in runs if r is not best])
    if minutes > 0:
        best["gap_loss_min"] = float(best.get("gap_loss_min") or 0.0) + minutes


def gap_loss_min(saved_runs, run: dict) -> float:
    """The `gap_loss_min` the hook noted in the saved copy of `run` (found by its identity
    fields), 0 when there is none."""
    for saved in saved_runs or ():
        if isinstance(saved, dict) and all(saved.get(k) == run.get(k) for k in RUN_KEY):
            value = saved.get("gap_loss_min")
            return float(value) if isinstance(value, (int, float)) else 0.0
    return 0.0


def parent_cover(state_dir: Path, parent: str, session: str, *, alive=pid_alive,
                 now: float | None = None) -> list[tuple[float, float]] | None:
    """The intervals of `parent`'s state during which `session` ran as the child of a dispatch
    `parent` recorded — what `parent`'s own wall-clock already counts of `session`. A run still
    open under a live dispatcher has no end yet and is returned open-ended (`inf`); one whose
    dispatcher died is closed in a copy (nothing is written). A run not yet attributed counts
    when `session` is the child discovery would give it. Empty when the parent has no state
    file, a long-unreadable one, or no run for `session`; None (unknown) when the file cannot
    be parsed and was written within `UNREADABLE_GRACE_S` — a writer is mid-save and the
    answer would be a guess."""
    path = Path(state_dir) / f"{parent}.json"
    data = _read(path)
    now = time.time() if now is None else now
    if data is None:
        written = _mtime(path)
        if written is not None and now - written <= UNREADABLE_GRACE_S:
            return None
    runs = [dict(r) for r in (data or {}).get("effort_child_runs") or () if isinstance(r, dict)]
    close_dead_runs(runs, state_dir, parent, alive=alive, now=now)
    cover = []
    for run in runs:
        started = run.get("started")
        if not isinstance(started, (int, float)):
            continue
        child = run.get("child_session")
        if child is None and run.get("ended") is None:
            child = _scan(state_dir, parent, run, runs, now)[0]
        if child != session:
            continue
        ended = run.get("ended")
        end = float(ended) if isinstance(ended, (int, float)) else float("inf")
        cover.append((float(started), end))
    return cover
