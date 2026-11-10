"""Read-only snapshot of the live agentctl tasks, with per-phase time and cost.

Difficulty removed: the engine's state, the spawn cost ledger and the session transcripts
each hold part of the picture of a running task, and nothing joined them without writing
to one of them. This module only reads. A state file is written by another process with no
lock, so a parse error may be a writer mid-save: it is re-read once, and a file that stays
unparsable becomes a visible row with ``readable`` false, never a dropped row and never an
exception. A value inferred rather than recorded is flagged ``estimate``; a missing
transcript gives a null root cost, never 0, because 0 would read as "free".
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from agentctl import child_runs, effort
from lib import config_root, engine_session_binding, transcript_cost
from plan_dashboard import snapshot

SCHEMA = "plan-dashboard/v1"
LEDGER_ANCHOR_BYTES = 64
ACTIVE_WINDOW = dt.timedelta(minutes=30)
HISTORY_TAIL = 20
RETRY_PAUSE_S = 0.05
DELIVERY_SUFFIX = ".delivery"
DONE_STATUSES = frozenset({"PASSED", "SKIPPED"})
REVIEW_EVENTS = frozenset({"plan_review", "plan_pair_review", "plan_review_compose"})
_UNLINKED_SPAWN_TASK_PREFIX = "agent-recursion-depth-"

CACHE_LOCK = threading.Lock()
_LEDGER: dict = {}
_SUMS: dict = {}
_TRANSCRIPTS: dict = {}
_CHILDREN: dict = {}


class UnknownSession(KeyError):
    """A well-formed session id with no state file."""


def clear_cache() -> None:
    with CACHE_LOCK:
        _LEDGER.clear()
        _SUMS.clear()
        _TRANSCRIPTS.clear()
        _CHILDREN.clear()
    snapshot.STATES.clear()


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _parse(value) -> dt.datetime | None:
    return transcript_cost.parse_ts_or_none(value)


def _iso(moment: dt.datetime | None) -> str | None:
    return moment.astimezone(dt.timezone.utc).isoformat(timespec="seconds") if moment else None


def _epoch(value) -> dt.datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return dt.datetime.fromtimestamp(value, dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _seconds(start: dt.datetime | None, end: dt.datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return round((end - start).total_seconds(), 3)


def to_json(obj) -> str:
    return json.dumps(obj, sort_keys=True)


def content_digest(snapshot: dict) -> str:
    body = {key: value for key, value in snapshot.items() if key != "generated_at"}
    return hashlib.sha256(to_json(body).encode("utf-8")).hexdigest()


def _read_state(path: Path) -> tuple[dict | None, str | None]:
    """The engine saves with a plain truncate-then-write, so a torn read is a writer mid-save:
    the pause gives it time to finish before the second attempt."""
    reason = None
    for attempt in range(2):
        if attempt:
            time.sleep(RETRY_PAUSE_S)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            reason = f"{type(exc).__name__}: {exc}"
            continue
        if isinstance(data, dict):
            return data, None
        reason = "not a JSON object"
    return None, reason


def _ledger_path(cost_log) -> Path:
    if cost_log is not None:
        return Path(cost_log)
    from agentctl import cost

    return cost.COST_LOG


def _stat_ledger(path: Path):
    """``(stat, None)``; ``(None, True)`` for an absent ledger (nothing was ever spent); ``(None, False)`` for
    one that exists but cannot be examined (its figures are unknown)."""
    try:
        return path.stat(), None
    except FileNotFoundError:
        return None, True
    except OSError:
        return None, False


def _rows_of(data: bytes):
    for line in data.split(b"\n"):
        if not line.strip():
            continue
        snapshot.check_stop()
        try:
            row = json.loads(line.decode("utf-8"))
        except ValueError:
            continue
        if isinstance(row, dict):
            yield row


def _ledger(cost_log) -> dict:
    """Every row of the ledger by owner, for a task's detail. ``unread`` marks a ledger that exists but
    could not be read: its rows are unknown, not none."""
    path = _ledger_path(cost_log)
    st, absent = _stat_ledger(path)
    if st is None:
        return {"rows": [], "by_session": {}, "by_plan": {}, "unread": not absent}
    ident = snapshot.identity_of(st)
    snapshot.check_stop()
    with CACHE_LOCK:
        entry = _LEDGER.get("entry")
        if entry is not None and entry["path"] == str(path) and snapshot.same_identity("ledger", entry["ident"], ident):
            return entry
        read_ns = snapshot.now_ns()
        try:
            rows = list(_rows_of(path.read_bytes()))
        except OSError:
            return {"rows": [], "by_session": {}, "by_plan": {}, "unread": True}
        snapshot.OBSERVED.note_read(str(path))
        by_session: dict = {}
        by_plan: dict = {}
        for row in rows:
            if row.get("spawner_session"):
                by_session.setdefault(row["spawner_session"], []).append(row)
            elif row.get("plan_path"):
                by_plan.setdefault(row["plan_path"], []).append(row)
        entry = {"path": str(path), "ident": ident, "rows": rows, "by_session": by_session, "by_plan": by_plan,
                 "unread": False}
        if snapshot.settled(st, read_ns):
            _LEDGER["entry"] = entry
        else:
            _LEDGER.pop("entry", None)
        return entry


def _owned_spawn_rows(ledger: dict, session_id: str, plan_path) -> list[dict]:
    rows = list(ledger["by_session"].get(session_id, []))
    if plan_path:
        rows.extend(ledger["by_plan"].get(plan_path, []))
    return rows


def _anchor(path: Path, offset: int) -> str:
    with open(path, "rb") as fh:
        fh.seek(max(offset - LEDGER_ANCHOR_BYTES, 0))
        return hashlib.sha256(fh.read(min(LEDGER_ANCHOR_BYTES, offset))).hexdigest()


def _extend_sums(path: Path, base: dict | None) -> dict:
    """The spend sums of the ledger: ``base``'s plus the complete lines appended since its offset (the
    ledger is append-only), or all of it. Reads only the new bytes."""
    offset = base["offset"] if base else 0
    by_session = dict(base["by_session"]) if base else {}
    by_plan = dict(base["by_plan"]) if base else {}
    with open(path, "rb") as fh:
        fh.seek(offset)
        data = fh.read()
    complete = data[: data.rfind(b"\n") + 1]
    for row in _rows_of(complete):
        amount = row.get("cost_usd")
        amount = amount if isinstance(amount, (int, float)) and not isinstance(amount, bool) else 0.0
        if row.get("spawner_session"):
            by_session[row["spawner_session"]] = by_session.get(row["spawner_session"], 0.0) + amount
        elif row.get("plan_path"):
            by_plan[row["plan_path"]] = by_plan.get(row["plan_path"], 0.0) + amount
    offset += len(complete)
    return {"offset": offset, "anchor": _anchor(path, offset), "by_session": by_session, "by_plan": by_plan}


def _ledger_sums(cost_log) -> dict:
    """What each session and plan spent, ``unread`` when the ledger exists and cannot be read. A change
    that only appends is read from where the last read stopped; any other change reads all of it again."""
    path = _ledger_path(cost_log)
    st, absent = _stat_ledger(path)
    if st is None:
        return {"by_session": {}, "by_plan": {}, "unread": not absent}
    ident = snapshot.identity_of(st)
    snapshot.check_stop()
    with CACHE_LOCK:
        entry = _SUMS.get("entry")
        mine = entry is not None and entry["path"] == str(path)
        same = mine and snapshot.same_identity("ledger", entry["ident"], ident)
        if same and entry["settled"]:
            return entry
        # the ledger is append-only: a read inside the mtime step that left the identity as it was is
        # finished from its offset (an append changes the size), not read from the start
        appended = (mine and entry["ident"][1] == st.st_ino and entry["ident"][2] <= st.st_size
                    and (st.st_size > entry["offset"] or same))
        read_ns = snapshot.now_ns()
        try:
            base = entry if appended and _anchor(path, entry["offset"]) == entry["anchor"] else None
            sums = _extend_sums(path, base)
        except OSError:
            _SUMS.pop("entry", None)
            return {"by_session": {}, "by_plan": {}, "unread": True}
        snapshot.OBSERVED.note_read(str(path))
        entry = _SUMS["entry"] = {"path": str(path), "ident": ident, **sums, "unread": False,
                                  "settled": snapshot.settled(st, read_ns)}
        return entry


def export_sums(identities) -> dict | None:
    """The kept ledger sums for the snapshot, when the ledger's recorded identity is the one they were read
    under (a spawn may have appended between the read and the stat the snapshot records).

    Takes no lock: a read holds ``CACHE_LOCK`` across its file I/O, and the snapshot at exit must not wait for
    it. The entry is replaced whole, never changed in place, so the last one published is read as it is."""
    entry = _SUMS.get("entry")
    if (entry is None or not entry["settled"] or entry["path"] not in identities
            or tuple(entry["ident"]) != tuple(identities[entry["path"]])):
        return None
    return {key: entry[key] for key in ("path", "offset", "anchor", "by_session", "by_plan")}


def seed_sums(data, identities: dict[str, tuple]) -> None:
    """Adopt snapshot ledger sums under the identity the snapshot recorded for the ledger; a malformed
    entry is skipped."""
    if not isinstance(data, dict) or data.get("path") not in identities:
        return
    try:
        entry = {"path": str(data["path"]), "ident": tuple(identities[data["path"]]), "offset": int(data["offset"]),
                 "anchor": str(data["anchor"]), "by_session": dict(data["by_session"]),
                 "by_plan": dict(data["by_plan"]), "unread": False, "settled": True}
    except (KeyError, TypeError, ValueError):
        return
    with CACHE_LOCK:
        _SUMS.setdefault("entry", entry)


def _spend(sums: dict, session_id: str, plan_path) -> float | None:
    """What a task's spawns cost; None when the ledger could not be read."""
    if sums["unread"]:
        return None
    return sums["by_session"].get(session_id, 0.0) + (sums["by_plan"].get(plan_path, 0.0) if plan_path else 0.0)


def _read_from(path: Path, offset: int) -> bytes:
    with open(path, "rb") as fh:
        fh.seek(offset)
        return fh.read()


def _read_transcript(path: Path) -> list:
    """Advance the cached read of one transcript; the one place a transcript is read.

    Caller holds CACHE_LOCK: the offset, the seen set and the rows move together."""
    try:
        st = os.stat(path)
    except OSError:
        return []
    key = str(path)
    entry = _TRANSCRIPTS.get(key)
    if entry is not None and (
        entry["ino"] != st.st_ino
        or st.st_size < entry["size"]
        or (st.st_size == entry["size"] and st.st_mtime_ns != entry["mtime_ns"])
    ):
        entry = None
    if entry is not None and st.st_size == entry["size"]:
        return entry["rows"]
    if entry is None:
        entry = {"ino": st.st_ino, "size": 0, "mtime_ns": 0, "offset": 0, "seen": set(), "rows": []}
    data = _read_from(path, entry["offset"])
    complete = data[: data.rfind(b"\n") + 1]
    objects = []
    for raw in complete.split(b"\n"):
        if b'"usage"' not in raw:
            continue
        snapshot.check_stop()
        try:
            obj = json.loads(raw.decode("utf-8"))
        except ValueError:
            continue
        if isinstance(obj, dict):
            objects.append(obj)
    # rows_from dedups only inside the chunk it is given, and numbers anonymous rows from zero
    # for each call; the cache feeds it one chunk at a time, so the seen set is kept here.
    for row in transcript_cost.rows_from(objects):
        anonymous = row.message_id.startswith("_anonymous:")
        if not anonymous and row.message_id in entry["seen"]:
            continue
        entry["seen"].add(row.message_id)
        entry["rows"].append(row)
    entry["offset"] += len(complete)
    entry["size"], entry["mtime_ns"] = st.st_size, st.st_mtime_ns
    _TRANSCRIPTS[key] = entry
    return entry["rows"]


def transcript_usage(path) -> list:
    snapshot.check_stop()
    with CACHE_LOCK:
        return list(_read_transcript(Path(path)))


def _transcript_paths(session_id: str, transcripts_root, *, linked_child: bool = False) -> list[Path]:
    """The transcripts of the conversations that ran a task. A linked child's own harness id
    is its engine id, and its parent drives it with verbs of its own from the parent's
    transcript, so a child reads only its own (the rule of
    ``engine_session_binding.transcript_for_session``)."""
    names = [config_root.sanitize_session_id(session_id)]
    if not linked_child:
        names += [config_root.sanitize_session_id(h)
                  for h in snapshot.harness_sessions_driving(session_id)]
    found: dict[str, Path] = {}
    for name in dict.fromkeys(names):
        if transcripts_root is not None:
            matches = sorted(Path(transcripts_root).glob(f"*/{name}.jsonl"))
        else:
            matches = config_root.iter_transcripts(f"*/{name}.jsonl")
        for match in matches:
            found.setdefault(str(match.resolve()), match)
    return list(found.values())


def _is_linked_child(state: dict) -> bool:
    return isinstance(state.get("parent_link"), dict)


def _usage_rows(session_id: str, transcripts_root, *, linked_child: bool = False) -> list | None:
    """Usage rows of every transcript of the task, deduplicated across them by message id
    (the same turn can sit in two transcripts of one conversation chain)."""
    paths = _transcript_paths(session_id, transcripts_root, linked_child=linked_child)
    if not paths:
        return None
    seen: set[str] = set()
    rows = []
    for path in paths:
        for row in transcript_usage(path):
            if row.message_id.startswith("_anonymous:") or row.message_id not in seen:
                seen.add(row.message_id)
                rows.append(row)
    return rows


def _events(state: dict) -> list[tuple[dt.datetime, dict]]:
    out = []
    for event in _list(state.get("history")):
        if isinstance(event, dict) and (moment := _parse(event.get("ts"))) is not None:
            out.append((moment, event))
    return out


def _window(kind, label, start, end, *, wall_estimate=False, **extra) -> dict:
    return {
        "kind": kind, "label": label, "_start": start, "_end": end, "_wall_estimate": wall_estimate,
        "spawn_cost_usd": 0.0, "spawn_count": 0, "_root": 0.0, "children": [], **extra,
    }


def _holds(window: dict, moment: dt.datetime) -> bool:
    start, end = window["_start"], window["_end"]
    return start is not None and moment >= start and (end is None or moment < end)


def attribute_spawn(row: dict, windows: list[dict]) -> dict:
    """The window a ledger row belongs to: its stage, else the plan holding its review, else
    the latest-starting window holding its ts, else the 'unattributed' window."""
    unattributed = windows[-1]
    stage_index = row.get("stage_index")
    if stage_index is not None:
        for window in windows:
            if window["kind"] == "stage" and window["_stage"] == stage_index:
                return window
    moment = _parse(row.get("ts"))
    if moment is None:
        return unattributed
    if row.get("review_pair"):
        for window in windows:
            if window["kind"] == "plan" and _holds(window, moment):
                return window
    holding = [w for w in windows[:-1] if _holds(w, moment)]
    return max(holding, key=lambda w: w["_start"]) if holding else unattributed


def _planning_and_plans(events) -> list[dict]:
    submits = [(m, e) for m, e in events if e.get("event") == "submit_plan"]
    out = []
    if events:
        first = events[0][0]
        out.append(_window("planning", "planning", first, submits[0][0] if submits else None,
                           wall_estimate=True))
    for number, (start, _) in enumerate(submits, 1):
        later = [m for m, e in events if m > start and e.get("event") in ("approve", "submit_plan")]
        end = min(later) if later else None
        plan = _window("plan", f"plan v{number}", start, end)
        boundary = start
        for round_number, moment in enumerate(
            [m for m, e in events if e.get("event") in REVIEW_EVENTS
             and m >= start and (end is None or m < end)], 1,
        ):
            plan["children"].append(_window("review round", f"review round {round_number}",
                                            boundary, moment, wall_estimate=True))
            boundary = moment
        out.append(plan)
    return out


def _stage_windows(events, state) -> list[dict]:
    """One window per stage, from its first ``next_stage`` to its last ``record_result``. A
    retry opens the stage again (``end`` back to None), so the gap between a failed result and
    the next ``next_stage`` is inside the window."""
    runs = [r for r in _list(state.get("effort_child_runs")) if isinstance(r, dict)]
    out = []
    numbers = []
    for _, event in events:
        number = event.get("stage")
        if event.get("event") == "next_stage" and number not in numbers:
            numbers.append(number)
    for number in numbers:
        start = end = None
        for moment, event in events:
            if event.get("stage") != number:
                continue
            if event.get("event") == "next_stage":
                start = start or moment
                end = None
            elif event.get("event") == "record_result" and start is not None:
                end = moment
        attempts = 1 + sum(1 for _, e in events
                           if e.get("event") == "record_result_attempt" and e.get("stage") == number)
        window = _window("stage", f"stage {number}", start, end, attempts=attempts, _stage=number)
        for run in runs:
            if run.get("stage_index") == number and (began := _epoch(run.get("started"))) is not None:
                window["children"].append(_window(
                    "child run", run.get("child_session") or "child run", began, _epoch(run.get("ended"))))
        out.append(window)
    return out


def _difficulty_windows(events) -> list[dict]:
    out = []
    opened: list[tuple] = []
    for moment, event in events:
        if event.get("event") == "difficulty_open":
            window = _window("difficulty", f"difficulty {event.get('uid') or len(out) + 1}", moment, None)
            out.append(window)
            opened.append((event.get("uid"), window))
        elif event.get("event") == "difficulty_close" and opened:
            index = next((i for i in range(len(opened) - 1, -1, -1) if opened[i][0] == event.get("uid")),
                         len(opened) - 1)
            opened.pop(index)[1]["_end"] = moment
    return out


def _resolution_window(events) -> list[dict]:
    finals = [m for m, e in events if e.get("event") == "verify_final"]
    if not finals:
        return []
    start = finals[-1]
    ends = [m for m, e in events if e.get("event") == "resolve" and m >= start]
    return [_window("resolution", "resolution", start, min(ends) if ends else None)]


def _slice_root(windows, usage_rows, events, resolved) -> None:
    first = events[0][0] if events else None
    resolve_at = max((m for m, e in events if e.get("event") == "resolve"), default=None) if resolved else None
    unattributed = windows[-1]
    for row in usage_rows:
        moment = row.ts
        if moment is None:
            unattributed["_root"] += row.usd
            continue
        if (first is not None and moment < first) or (resolve_at is not None and moment > resolve_at):
            continue
        holding = [w for w in windows[:-1] if _holds(w, moment)]
        target = max(holding, key=lambda w: w["_start"]) if holding else unattributed
        target["_root"] += row.usd
        for child in target["children"]:
            if _holds(child, moment):
                child["_root"] += row.usd


def _finish(window: dict, counted: bool, *, nested: bool = False) -> dict:
    """A nested window (review round, child run) has null spawn figures: spawn rows are
    attributed to top-level windows only, so 0 there would read as "free" rather than "not
    attributed"."""
    start, end = window["_start"], window["_end"]
    item = {
        "kind": window["kind"], "label": window["label"], "start": _iso(start), "end": _iso(end),
        "wall_clock_s": _seconds(start, end),
        "spawn_cost_usd": None if nested else round(window["spawn_cost_usd"], 6),
        "spawn_count": None if nested else window["spawn_count"],
        "root_cost_usd": round(window["_root"], 6) if counted else None,
        "estimate": {"wall_clock_s": window["_wall_estimate"], "spawn_cost_usd": False,
                     "root_cost_usd": True},
        "children": [_finish(child, counted, nested=True) for child in window["children"]],
    }
    if "attempts" in window:
        item["attempts"] = window["attempts"]
    return item


def phases(state: dict, spawn_rows: list[dict], usage_rows: list | None) -> list[dict]:
    """Phase tree of one task from its recorded history; pure, reads no clock.

    ``usage_rows`` None means no transcript was found: every root cost is null."""
    events = _events(state)
    windows = _planning_and_plans(events)
    windows += _stage_windows(events, state) + _difficulty_windows(events) + _resolution_window(events)
    windows.sort(key=lambda w: w["_start"])
    windows.append(_window("unattributed", "unattributed", None, None))
    for row in spawn_rows:
        target = attribute_spawn(row, windows)
        if isinstance(row.get("cost_usd"), (int, float)):
            target["spawn_cost_usd"] += row["cost_usd"]
        target["spawn_count"] += 1
    if usage_rows is not None:
        _slice_root(windows, usage_rows, events, state.get("node") == "RESOLVED")
    return [_finish(w, usage_rows is not None) for w in windows]


def totals(*, wall_clock_s, spawn_cost_usd, root_cost_usd, root_counted: bool, shared_with=None) -> dict:
    """``root_shared_with`` lists the other tasks that ran in the same conversation(s) as the
    task, whose transcript rows may then sit in this task's last open window; null when no
    transcript was read."""
    spawn_known = spawn_cost_usd is not None
    cost_usd = (spawn_cost_usd or 0.0) + (root_cost_usd or 0.0) if spawn_known or root_counted else None
    return {
        "wall_clock_s": wall_clock_s,
        "spawn_cost_usd": round(spawn_cost_usd, 6) if spawn_known else None,
        "root_cost_usd": round(root_cost_usd, 6) if root_counted else None,
        "cost_usd": round(cost_usd, 6) if cost_usd is not None else None,
        "estimate": {"wall_clock_s": False, "spawn_cost_usd": False,
                     "root_cost_usd": root_counted, "cost_usd": root_counted},
        "partial": not (root_counted and spawn_known),
        "root_counted": root_counted,
        "root_shared_with": sorted(shared_with) if root_counted and shared_with is not None else None,
    }


def _spent(rows: list[dict]) -> float:
    return sum(r["cost_usd"] for r in rows if isinstance(r.get("cost_usd"), (int, float)))


def _title(record: dict) -> str | None:
    declaration = _dict(record.get("declaration"))
    return declaration.get("mismatch") or declaration.get("actual") or None


def _activity(state: dict, mtime: dt.datetime):
    stamps = [m for m, _ in _events(state)]
    return (min(stamps), max(stamps)) if stamps else (None, mtime)


def _age_band(age: dt.timedelta, window: dt.timedelta) -> str:
    if age <= ACTIVE_WINDOW:
        return "active"
    return "idle" if age <= window else "stale"


def _liveness(digest: dict, last: dt.datetime, now: dt.datetime, window: dt.timedelta) -> str:
    if digest["node"] == "RESOLVED":
        return "resolved"
    if digest["node"] == "CLASSIFIED" and not digest["weight_class"] and not digest["plan_path"]:
        return "stub"
    live_run = any(child_runs.pid_alive(pid) for pid in digest["open_run_pids"])
    return "active" if live_run else _age_band(now - last, window)


def _digest(stem: str, state: dict, mtime: dt.datetime) -> dict:
    """What a state file contributes to its row, apart from the clock, the processes and the ledger: JSON
    all through, so it can sit in the snapshot."""
    first, last = _activity(state, mtime)
    stages = [s for s in _list(state.get("stages")) if isinstance(s, dict)]
    open_record = _dict(state.get("difficulty"))
    return {
        "owner": state.get("session_id"), "plan_path": state.get("plan_path"), "node": state.get("node"),
        "weight_class": state.get("weight_class"),
        "open_run_pids": [run.get("dispatcher_pid") for run in _list(state.get("effort_child_runs"))
                          if isinstance(run, dict) and run.get("ended") is None],
        "first": first.isoformat() if first else None, "last": last.isoformat(),
        "row": {
            "session_id": state.get("session_id") or stem, "task_id": state.get("task_id"),
            "goal": state.get("goal"), "node": state.get("node"), "weight_class": state.get("weight_class"),
            "current_stage": state.get("current_stage"),
            "stages_done": sum(1 for s in stages if _dict(s.get("outcome")).get("status") in DONE_STATUSES),
            "stages_total": len(stages),
            "open_difficulty": _title(open_record) if open_record else None,
            "difficulties": {"open": 1 if open_record else 0,
                             "stacked": len(_list(state.get("difficulty_stack"))),
                             "closed": len(_list(state.get("closed_difficulties")))},
            # replan_count reads only ``.history``; a state file is not rebuilt into a SessionState
            # (a legacy file with missing keys would not load), so a stand-in carries the history.
            "replans": effort.replan_count(SimpleNamespace(history=_list(state.get("history")))),
            "review_rounds": state.get("review_rounds") or 0,
            "plan_review_rounds": state.get("plan_review_rounds") or 0,
            "effort": {"estimate": state.get("effort_estimate"), "actuals": _dict(state.get("effort_actuals"))},
            "parent_session": _dict(state.get("parent_link")).get("parent_session"),
        },
    }


def _load_digest(path, mtime: dt.datetime) -> dict:
    """``{"digest", "reason"}`` of one state file: the digest None, with the reason, when it stays unparsable."""
    state, reason = _read_state(Path(path))
    return {"digest": None if state is None else _digest(Path(path).stem, state, mtime), "reason": reason}


def _summary(stem: str, entry: dict, mtime: dt.datetime, now: dt.datetime, window, sums: dict) -> dict:
    digest = entry["digest"]
    if digest is None:
        return {
            "session_id": stem, "task_id": None, "goal": None, "node": None, "weight_class": None,
            "liveness": _age_band(now - mtime, window),
            "current_stage": None, "stages_done": None, "stages_total": None,
            "open_difficulty": None, "difficulties": None, "replans": None, "review_rounds": None,
            "plan_review_rounds": None, "effort": None, "parent_session": None,
            "last_activity": _iso(mtime), "readable": False, "reason": entry["reason"], "totals": None,
        }
    first = dt.datetime.fromisoformat(digest["first"]) if digest["first"] else None
    last = dt.datetime.fromisoformat(digest["last"])
    row = digest["row"]
    return {
        **row, "liveness": _liveness(digest, last, now, window), "last_activity": _iso(last),
        "readable": True, "reason": None,
        "totals": totals(wall_clock_s=_seconds(first, last),
                         spawn_cost_usd=_spend(sums, row["session_id"], digest["plan_path"]),
                         root_cost_usd=None, root_counted=False),
    }


def _is_sidecar(stem: str, entry: dict) -> bool:
    """A JSON file in the state directory that is not a task: the engine's delivery record
    (``<session>.delivery.json``) or any file naming another session than its own."""
    if stem.endswith(DELIVERY_SUFFIX):
        return True
    owner = (entry["digest"] or {}).get("owner")
    return isinstance(owner, str) and config_root.sanitize_session_id(owner) != stem


def _mtime(path: Path) -> dt.datetime | None:
    try:
        return dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc)
    except OSError:
        return None


def _is_unlinked_spawn(row: dict) -> bool:
    """A spawned child that recorded no parent: the spawn wrapper's first prompt line became its task id."""
    return not row["parent_session"] and str(row["task_id"] or "").startswith(_UNLINKED_SPAWN_TASK_PREFIX)


def _listed_stems(summaries: dict, wanted: set, pinned: set, include_all: bool) -> set:
    """The listing's rows: the root of each wanted task and each pinned task itself. A child shows only in
    its root's detail; a child whose parent has no task to show stays listed, so its work never vanishes.
    An unlinked spawn is a child with no root to show it, so only `--all` and `--session` list it."""
    listed = pinned & summaries.keys()
    for stem in wanted:
        seen = {stem}
        while (parent := summaries[stem]["parent_session"]):
            parent = config_root.sanitize_session_id(parent)
            if parent not in summaries or parent in seen:
                break
            seen.add(parent)
            stem = parent
        if include_all or not _is_unlinked_spawn(summaries[stem]):
            listed.add(stem)
    return listed


def _qualifies_alone(state, row: dict, windowed) -> bool:
    """Whether a task is listed for itself. Once the open windows are known they account for the work
    a person left open, so beyond them only a task active right now earns a row."""
    if windowed is None:
        return state is None or row["liveness"] in ("active", "idle")
    return row["liveness"] == "active"


def collect(state_root, *, now: dt.datetime, window_hours=24, include_all=False, sessions=None,
            cost_log=None, windowed=None) -> dict:
    """``windowed`` names the task each open window shows (``attention.windowed_engines``); each is
    listed whatever its age or state. ``None`` means the windows are unknown."""
    root = Path(state_root)
    window = dt.timedelta(hours=window_hours)
    pinned = {config_root.sanitize_session_id(s) for s in (*(sessions or ()), *(windowed or ()))}
    listing = dict(sorted(snapshot.listed("state", root, ".json").items()))
    snapshot.STATES.prune({path for path, _ in listing.values()})
    mtimes = {stem: (path, st, dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc))
              for stem, (path, st) in listing.items()}
    queue = [stem for stem, (_, _, moment) in mtimes.items()
             if include_all or stem in pinned or now - moment <= window]
    loaded: dict = {}
    while queue:
        stem = queue.pop()
        if stem in loaded or stem not in mtimes:
            continue
        path, st, moment = mtimes[stem]
        entry = snapshot.STATES.fetch(path, st, lambda p, moment=moment: _load_digest(p, moment))
        loaded[stem] = (entry, moment)
        parent = ((entry["digest"] or {}).get("row") or {}).get("parent_session")
        if parent:
            queue.append(config_root.sanitize_session_id(parent))
    snapshot.OBSERVED.used(mtimes[stem][0] for stem in loaded)
    sums = _ledger_sums(cost_log)
    snapshot.OBSERVED.ledger(_ledger_path(cost_log))
    summaries, wanted = {}, set()
    for stem, (entry, moment) in loaded.items():
        if _is_sidecar(stem, entry):
            continue
        row = summaries[stem] = _summary(stem, entry, moment, now, window, sums)
        if include_all or stem in pinned or _qualifies_alone(entry["digest"], row, windowed):
            wanted.add(stem)
    tasks = [summaries[stem] for stem in _listed_stems(summaries, wanted, pinned, include_all)]
    tasks.sort(key=lambda t: (t["last_activity"] or "", t["session_id"]), reverse=True)
    return {"schema": SCHEMA, "generated_at": _iso(now), "hostname": socket.gethostname(), "tasks": tasks}


_DETAIL_KEYS = (
    "session_id", "goal", "node", "current_stage", "stages", "open_difficulties", "closed_difficulties",
    "children", "history", "plan_path", "tracker_key", "repo_root", "harness_sessions", "readable",
    "reason", "totals", "phases",
)


def _child_link(path: Path) -> tuple[str, dict] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if '"parent_link"' not in text:
        return None
    try:
        state = json.loads(text)
    except ValueError:
        state, _ = _read_state(path)
    state = _dict(state)
    link = _dict(state.get("parent_link"))
    if not link.get("parent_session"):
        return None
    return link["parent_session"], {
        "session_id": state.get("session_id") or path.stem, "stage_index": link.get("stage_index"),
        "node": state.get("node"), "goal": state.get("goal")}


def _prune_children(state_root: Path, present: set) -> None:
    """Drop the cached links of ``state_root``'s state files that are gone."""
    with CACHE_LOCK:
        for path in [p for p in _CHILDREN if p.parent == state_root and p not in present]:
            del _CHILDREN[path]


def child_index(state_root, *, since: float | None = None) -> dict[str, list[dict]]:
    """Parent session id → its linked children. Each state file's link is cached by path,
    mtime and size, so a call re-reads only changed or new files. With ``since`` (epoch
    seconds) an uncached file older than ``since`` minus a minute is skipped: a linked child's
    state file is created after its parent starts and later saves only raise its mtime."""
    floor = None if since is None else since - 60
    index: dict[str, list[dict]] = {}
    paths = sorted(Path(state_root).glob("*.json"))
    _prune_children(Path(state_root), set(paths))
    for path in paths:
        try:
            st = path.stat()
        except OSError:
            continue
        key = snapshot.identity_of(st)
        with CACHE_LOCK:
            cached = _CHILDREN.get(path)
        if cached is not None and snapshot.same_identity("state", cached[0], key):
            link = cached[1]
        elif cached is None and floor is not None and st.st_mtime < floor:
            continue
        else:
            snapshot.check_stop()
            read_ns = snapshot.now_ns()
            link = _child_link(path)
            with CACHE_LOCK:
                if snapshot.settled(st, read_ns):
                    _CHILDREN[path] = (key, link)
                else:
                    _CHILDREN.pop(path, None)
        if link is not None:
            index.setdefault(link[0], []).append(dict(link[1]))
    return index


def _co_driven(root: Path, harness_sessions, session_id: str) -> list[str]:
    """Other tasks bound to the same conversations, linked children excluded (they run from
    transcripts of their own)."""
    others = {s for harness in harness_sessions for s in engine_session_binding.driven_sessions(harness)}
    others.discard(config_root.sanitize_session_id(session_id))
    return sorted(s for s in others if not _is_linked_child(_dict(_read_state(root / f"{s}.json")[0])))


def _stage_row(stage: dict) -> dict:
    return {
        "index": stage.get("index"), "title": stage.get("title"),
        "actor": _dict(stage.get("actor")).get("executor"),
        "criterion_type": _dict(stage.get("criterion")).get("criterion_type"),
        "status": _dict(stage.get("outcome")).get("status"),
    }


def _difficulty_row(record: dict) -> dict:
    return {**record, "title": _title(record)}


def detail(state_root, session_id, *, cost_log=None, transcripts_root=None, children=None) -> dict:
    """``children`` is a ``child_index`` result; omitted, the index is built for this call."""
    root = Path(state_root)
    path = root / f"{config_root.sanitize_session_id(session_id)}.json"
    if not path.exists():
        raise UnknownSession(session_id)
    state, reason = _read_state(path)
    if state is None:
        return {**dict.fromkeys(_DETAIL_KEYS), "session_id": session_id, "stages": [],
                "open_difficulties": [], "closed_difficulties": [], "children": [], "history": [],
                "harness_sessions": [], "phases": [], "readable": False, "reason": reason}
    sid = state.get("session_id") or session_id
    mtime = _mtime(path) or dt.datetime.fromtimestamp(0, dt.timezone.utc)
    first, last = _activity(state, mtime)
    ledger = _ledger(cost_log)
    spawn_rows = _owned_spawn_rows(ledger, sid, state.get("plan_path"))
    linked = _is_linked_child(state)
    usage_rows = _usage_rows(sid, transcripts_root, linked_child=linked)
    tree = phases(state, spawn_rows, usage_rows)
    root_cost = sum(item["root_cost_usd"] for item in tree) if usage_rows is not None else None
    harness_sessions = snapshot.harness_sessions_driving(sid)
    shared = [] if linked else _co_driven(root, harness_sessions, sid)
    if children is None:
        children = child_index(root)
    stack = [d for d in _list(state.get("difficulty_stack")) if isinstance(d, dict)]
    open_record = _dict(state.get("difficulty"))
    return {
        "session_id": sid, "goal": state.get("goal"), "node": state.get("node"),
        "current_stage": state.get("current_stage"),
        "stages": [_stage_row(s) for s in _list(state.get("stages")) if isinstance(s, dict)],
        "open_difficulties": [_difficulty_row(d) for d in [*stack, *([open_record] if open_record else [])]],
        "closed_difficulties": [_difficulty_row(d) for d in _list(state.get("closed_difficulties"))
                                if isinstance(d, dict)],
        "children": children.get(sid, []),
        "history": _list(state.get("history"))[-HISTORY_TAIL:],
        "plan_path": state.get("plan_path"), "tracker_key": state.get("tracker_key"),
        "repo_root": state.get("repo_root"),
        "harness_sessions": sorted(harness_sessions),
        "readable": True, "reason": None,
        "totals": totals(wall_clock_s=_seconds(first, last), spawn_cost_usd=None if ledger["unread"] else _spent(spawn_rows),
                         root_cost_usd=root_cost, root_counted=usage_rows is not None, shared_with=shared),
        "phases": tree,
    }
