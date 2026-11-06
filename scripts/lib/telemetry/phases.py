"""The one phase-boundary definition: where a task's time moves from one phase to the next.

Difficulty removed: time-profile replayed engine nodes from state files while the record's
event-derived shares needed phase boundaries too; two definitions would drift. Both now build a
list of (time, node) points and read it through the functions here: `dwell_from_points` (per-node
dwell, what time-profile reports), `phase_intervals` (the phases table's members over time) and
`phase_at` (the phase a session was in at a moment, the function the token-share stage calls).

A session's points come from its events when it has live events, else from its reconstruction:
the engine-history commands imported for it, else the agentctl commands found in its transcript.
Landing is a phase of its own, one interval per pull request: from the first land-branch start
after the previous landed event of the same task and branch to the next landed event (events),
or, for imported history without landing events, from the first land-branch Bash call after the
previous successful one to the next one that exited 0.
"""
from __future__ import annotations

import json
from pathlib import Path

from lib.telemetry import tables

NODE_PHASE = {
    "CLASSIFIED": "order",
    "ROUTED": "order",
    "PLANNING": "planning",
    "PLAN_READY": "plan_review",
    "APPROVED": "execution",
    "PARTITIONED": "execution",
    "EXECUTING": "execution",
    "VERIFYING": "verification",
    "DIAGNOSING": "difficulty",
    "RESOLUTION": "resolution",
    "RESOLVED": "resolution",
}
OTHER = "other"
LANDING = "landing"

# The node an agentctl verb leaves the session in, for a transcript that records the verb but
# not the node: the engine's own transition table names events, not verbs, so only verbs whose
# target node is fixed are listed.
VERB_NODE = {
    "classify": "ROUTED",
    "plan": "PLANNING",
    "submit-plan": "PLAN_READY",
    "approve": "APPROVED",
    "partition": "PARTITIONED",
    "execute": "EXECUTING",
    "record-result": "VERIFYING",
    "verify-final": "RESOLUTION",
    "resolve": "RESOLVED",
}
LAND_PROGRAM = "land-branch.py"


def phase_of_node(node) -> str:
    """The phases-table member a node belongs to; a node the engine does not name is `other`."""
    phase = NODE_PHASE.get(node, OTHER)
    tables.validate("phases", phase)
    return phase


def _ms(value) -> "int | None":
    from lib import time_profile
    return time_profile._epoch_or_iso_ms(value)


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def transition_targets() -> dict:
    try:
        from agentctl.machine import TRANSITIONS
    except ImportError:
        return {}
    return {event: to for event, (_, to) in TRANSITIONS.items()}


# --- points: (time in ms or None, node after the act) -------------------------------------

def history_points(history) -> list:
    """Points of an engine state history: the node comes from the entry, else the engine's
    transition table replayed from CLASSIFIED."""
    targets = transition_targets()
    node = "CLASSIFIED"
    points = []
    for event in history:
        if not isinstance(event, dict):
            continue
        node = event["node"] if isinstance(event.get("node"), str) else targets.get(event.get("event"), node)
        points.append((_ms(event.get("ts")), node))
    return points


def event_points(rows) -> list:
    """Points of recorded command events: (time, fields) pairs, `node_after` naming the node."""
    points = []
    for moment, fields in sorted(rows, key=lambda r: r[0]):
        node = fields.get("node_after")
        if isinstance(node, str):
            points.append((_ms(moment), node))
    return points


def bash_points(rows) -> list:
    """Points of agentctl commands found in a transcript: (time, fields) of `bash` events."""
    points = []
    for moment, fields in sorted(rows, key=lambda r: r[0]):
        node = VERB_NODE.get(fields.get("verb")) if fields.get("program") == "agentctl-cli.py" else None
        if node is not None:
            points.append((_ms(moment), node))
    return points


def dwell_from_points(points, window: "tuple | None" = None) -> dict:
    """Per-node dwell between consecutive timed points. A gap with an untimed point inside it is
    dropped rather than guessed. `window` is (lo_ms, hi_ms): each gap is clipped to it."""
    dwell: dict = {}
    timed = untimed = 0
    prev_ts = prev_node = None
    gap_untimed = False
    for ts, node in points:
        if ts is None:
            untimed += 1
            gap_untimed = True
            continue
        timed += 1
        if prev_ts is not None and not gap_untimed and ts >= prev_ts:
            lo, hi = prev_ts, ts
            if window is not None:
                lo, hi = max(lo, window[0]), min(hi, window[1])
            if hi > lo:
                dwell[prev_node] = dwell.get(prev_node, 0) + hi - lo
        prev_ts, prev_node, gap_untimed = ts, node, False
    return {"dwell": dwell, "timed": timed, "untimed": untimed}


def node_dwell(state_path, window: "tuple | None" = None) -> "dict | None":
    """Per-node dwell from consecutive history `ts` of one agentctl state file.

    Events carry no node, so the node is replayed through the engine's transition table.
    `window` is (lo_ms, hi_ms): each gap is clipped to it, so a harness session counts only the
    dwell that fell inside its own transcript."""
    data = _read_json(state_path)
    history = data.get("history") if isinstance(data, dict) else None
    if not isinstance(history, list):
        return None
    return dwell_from_points(history_points(history), window)


def phase_intervals(points) -> list:
    """[(start_ms, end_ms or None, phase)]: consecutive points of one phase merge; the last
    interval is open."""
    out: list = []
    for ts, node in sorted((p for p in points if p[0] is not None), key=lambda p: p[0]):
        phase = phase_of_node(node)
        if out and out[-1][2] == phase:
            continue
        if out:
            out[-1] = (out[-1][0], ts, out[-1][2])
        out.append((ts, None, phase))
    return out


# --- landing --------------------------------------------------------------------------------

def landing_intervals(events) -> list:
    """Landing intervals from landing events: dicts with `source_key` (land:start / land:landed),
    `time`, `task` and `branch`. One interval per PR: from the first start after the previous
    landed event of the same (task, branch) to the next landed event."""
    intervals = []
    started: dict = {}
    for event in sorted(events, key=lambda e: e["time"]):
        key = (event.get("task"), event.get("branch"))
        if event["source_key"] == "land:start":
            started.setdefault(key, _ms(event["time"]))
        elif event["source_key"] == "land:landed" and key in started:
            intervals.append({"task": key[0], "branch": key[1], "start": started.pop(key), "end": _ms(event["time"])})
    return intervals


def call_landing_intervals(calls) -> list:
    """Landing intervals from transcript Bash calls of land-branch.py: (time, exit code) pairs.
    One interval per PR: from the first call after the previous successful one to the next call
    that exited 0."""
    intervals = []
    start = None
    for moment, code in sorted(calls, key=lambda c: c[0]):
        if start is None:
            start = _ms(moment)
        if code == 0:
            intervals.append({"task": None, "branch": None, "start": start, "end": _ms(moment)})
            start = None
    return intervals


def _in(intervals, ts) -> bool:
    return any(i["start"] is not None and i["start"] <= ts < i["end"] for i in intervals)


# --- the record's reading side -------------------------------------------------------------

def _fields(raw) -> dict:
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def has_live_events(conn, session_id: str) -> bool:
    return conn.execute("SELECT 1 FROM events WHERE session_id = ? AND provenance = 'live' LIMIT 1",
                        (session_id,)).fetchone() is not None


def _rows(conn, session_id: str, kind: str, provenance: "str | None" = None) -> list:
    sql = "SELECT time, fields, source_key FROM events WHERE session_id = ? AND kind = ?"
    args = [session_id, kind]
    if provenance is not None:
        sql += " AND provenance = ?"
        args.append(provenance)
    return [(r[0], _fields(r[1]), r[2]) for r in conn.execute(sql, args)]


def session_points(conn, session_id: str) -> "tuple[list, list]":
    """(points, landing intervals) of a session: from its events when it has live events, else
    from its reconstruction (the imported engine-history commands, else its transcript's calls)."""
    if has_live_events(conn, session_id):
        points = event_points([(t, f) for t, f, _ in _rows(conn, session_id, "command", "live")])
        events = [{"source_key": s, "time": t, "task": f.get("task_epoch"), "branch": f.get("branch")}
                  for t, f, s in _rows(conn, session_id, "landing", "live")]
        return points, landing_intervals(events)
    command_rows = [(t, f) for t, f, _ in _rows(conn, session_id, "command")]
    bash_rows = [(t, f) for t, f, _ in _rows(conn, session_id, "bash")]
    points = event_points(command_rows) or bash_points(bash_rows)
    calls = [(t, f.get("exit")) for t, f in bash_rows if f.get("program") == LAND_PROGRAM]
    return points, call_landing_intervals(calls)


def phase_at(session_id: str, timestamp, *, conn=None) -> str:
    """The phase `session_id` was in at `timestamp` (epoch or ISO string): landing inside a
    landing interval, else the phase of the node the session's latest earlier point names;
    `other` before its first point or when the record holds nothing for it."""
    ts = _ms(timestamp)
    own = conn is None
    if own:
        from lib.telemetry import store
        conn = store.open_readonly()
    if conn is None or ts is None:
        return OTHER
    try:
        points, landing = session_points(conn, session_id)
    finally:
        if own:
            conn.close()
    if _in(landing, ts):
        return LANDING
    for start, end, phase in phase_intervals(points):
        if start <= ts and (end is None or ts < end):
            return phase
    return OTHER


def events_node_dwell(conn, session_id: str, window: "tuple | None" = None) -> "dict | None":
    """Per-node dwell of a session from the record's command events, None when it has none."""
    points = event_points([(t, f) for t, f, _ in _rows(conn, session_id, "command")])
    return dwell_from_points(points, window) if points else None
