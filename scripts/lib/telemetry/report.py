"""The per-Core-version report over the telemetry record, and the readers it is built from.

Every reader opens only the record (read-only): no transcript, no engine state file. The view
`agent-report.py core-version` groups tasks by the Core HEAD their opening session ran on and,
per weight class, reports convergence (replans, review rounds, effort fires), reliability
(verify-final red rate and flakes) and resources (minutes, tokens, spend). Beside the shares it
counts the record's losses and the contours no task owns; per mechanism it joins each event to
the one elementary registry entry holding its source key and rolls the count up to composites.

`--task T` adds the self cost of one task: recorder overhead beside its bound, landing minutes
over per-PR landing intervals and land-branch attempts; `--check-self-cost` fails unless all of
them are present and every self-measurement entry fired over the task's sessions.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from lib import config_root, transcript_cost
from lib.telemetry import importer, live, phases, store, sync, tables

DIAGNOSING = "DIAGNOSING"
DIFFICULTY_EXITS = ("replan", "unblock", "reset")
RECORDER_BOUND_MS = 250.0
REPORT_SOURCE = "telemetry:report"
SELF_COST_ENTRIES = ("telemetry:recorder", "telemetry:sync", "telemetry:report",
                     "land:start", "land:attempt", "land:landed")
UNKNOWN_HEAD = "unknown"


def _fields(raw) -> dict:
    return phases._fields(raw)


def _task_key(row) -> tuple:
    return (row[0], row[1], row[2])


def _is_red(fields: dict) -> bool:
    return fields.get("exit") not in (0, None)


# --- readers ----------------------------------------------------------------------------

def _task_events(conn, key: tuple) -> list:
    """(event_id, session_id, kind, source_key, provenance, time, fields) of the task's events."""
    ids = sync.events_of_task(conn, *key)
    rows = []
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        rows += conn.execute(
            "SELECT event_id, session_id, kind, source_key, provenance, time, fields FROM events "
            f"WHERE event_id IN ({','.join('?' * len(chunk))})", chunk).fetchall()
    return sorted(((r[0], r[1], r[2], r[3], r[4], r[5], _fields(r[6])) for r in rows), key=lambda r: r[5])


def _spend(conn, key: tuple) -> float:
    return sum(transcript_cost.token_cost(
        {"input_tokens": r[0] or 0, "output_tokens": r[1] or 0,
         "cache_creation_input_tokens": r[2] or 0, "cache_read_input_tokens": r[3] or 0}, r[4])
        for r in conn.execute(
            "SELECT input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, model FROM token_usage "
            "WHERE task_origin = ? AND task_id = ? AND task_epoch = ?", key))


def _minutes(events: list) -> float:
    stamps = [phases._ms(e[5]) for e in events if phases._ms(e[5]) is not None]
    return (max(stamps) - min(stamps)) / 60000.0 if stamps else 0.0


def tasks(conn, include_synthetic: bool = False) -> list:
    """One dict per task row: key, weight_class, status (RESOLVED or open), last_event_at,
    minutes, spend_usd (each token row priced by its own model), replans, synthetic."""
    out = []
    for row in conn.execute("SELECT origin, task_id, epoch, status, weight_class, synthetic FROM tasks "
                            "ORDER BY opened_at, task_id").fetchall():
        if row[5] and not include_synthetic:
            continue
        key = _task_key(row)
        events = _task_events(conn, key)
        out.append({
            "task": key, "weight_class": row[4],
            "status": "RESOLVED" if row[3] == "resolved" else "open",
            "last_event_at": events[-1][5] if events else None,
            "minutes": _minutes(events),
            "spend_usd": _spend(conn, key),
            "replans": sum(1 for e in events if e[2] == "command" and e[6].get("command") == "replan"
                           and e[6].get("outcome") == "ok"),
            "synthetic": bool(row[5]),
        })
    return out


def _cause(row) -> dict:
    return {"category": row["cause_category"], "machinery_induced": row["machinery_induced"],
            "mechanism": row["mechanism"], "functional_place": row["functional_place"],
            "mechanism_basis": row["mechanism_basis"], "place_basis": row["place_basis"],
            "interval_unknown": row["interval_unknown"], "imported": row["imported"]}


def _causes_by_slot(conn) -> dict:
    """(session, difficulty index) -> cause, live causes by their own slot, imported ones by
    their alignment."""
    cursor = conn.execute("SELECT * FROM causes")
    names = [d[0] for d in cursor.description]
    slots, unaligned = {}, []
    for values in cursor.fetchall():
        row = dict(zip(names, values))
        if row["imported"]:
            if row["alignment_session"] is None:
                unaligned.append(row)
                continue
            slot = (row["alignment_session"], row["alignment_difficulty_index"])
        else:
            slot = (row["session"], row["difficulty_index"])
        slots[slot] = row
    return {"slots": slots, "unaligned": unaligned}


def _entry_route(prior: list, entry: tuple) -> str:
    if entry[6].get("node_before") == "PLAN_READY":
        return "review_cycle"
    for event in reversed(prior):
        if event[2] == "command":
            break
        if event[3] == "gate:effort_fire" and event[6].get("outcome") != "pass":
            return "effort_fire"
    return "failed_stage"


def difficulties(conn, include_synthetic: bool = False) -> list:
    """One dict per difficulty, read from the command events' node transitions: an entry into
    DIAGNOSING opens one, the next command leaving it closes it. The cause is the one filed for
    the same (session, 0-based difficulty ordinal); an imported cause aligned to no difficulty
    is its own row, keyed by its cause id, with only the cause set."""
    causes = _causes_by_slot(conn)
    out = []
    for task in tasks(conn, include_synthetic):
        events = _task_events(conn, task["task"])
        ordinal: dict = {}
        open_by_session: dict = {}
        for index, event in enumerate(events):
            if event[2] != "command":
                continue
            before, after, session = event[6].get("node_before"), event[6].get("node_after"), event[1]
            if after == DIAGNOSING and before != DIAGNOSING:
                number = ordinal.get(session, 0)
                ordinal[session] = number + 1
                cause = causes["slots"].get((session, number))
                row = {"difficulty": f"{session}#{number}", "task": task["task"], "entry_at": event[5],
                       "entry_route": _entry_route([e for e in events[:index] if e[1] == session], event),
                       "exit_at": None, "exit_kind": None, "cause": _cause(cause) if cause else None}
                open_by_session[session] = row
                out.append(row)
            elif before == DIAGNOSING and after != DIAGNOSING and session in open_by_session:
                row = open_by_session.pop(session)
                row["exit_at"] = event[5]
                command = event[6].get("command")
                row["exit_kind"] = command if command in DIFFICULTY_EXITS else None
    for cause in causes["unaligned"]:
        out.append({"difficulty": cause["cause_id"], "task": None, "entry_at": None, "entry_route": None,
                    "exit_at": None, "exit_kind": None, "cause": _cause(cause)})
    return out


def _task_sessions(conn, key: tuple) -> list:
    sessions = {r[0] for r in conn.execute(
        "SELECT session_id FROM sessions WHERE task_origin = ? AND task_id = ? AND task_epoch = ?", key)}
    opener = conn.execute("SELECT session_id FROM events WHERE event_id = ?", (key[2],)).fetchone()
    if opener:
        sessions.add(opener[0])
    return sorted(sessions)


def sessions(conn, task: tuple) -> list:
    """The task's sessions as dicts of their session-row columns (session_id only when no row)."""
    out = []
    for session in _task_sessions(conn, task):
        cursor = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session,))
        values = cursor.fetchone()
        out.append(dict(zip([d[0] for d in cursor.description], values)) if values else {"session_id": session})
    return out


def events(conn, task: tuple, kinds=None) -> list:
    """The task's events as dicts (fields decoded), oldest first; `kinds` filters by kind."""
    names = ("event_id", "session_id", "kind", "source_key", "provenance", "time", "fields")
    return [dict(zip(names, e)) for e in _task_events(conn, task) if kinds is None or e[2] in kinds]


# --- the core-version view ----------------------------------------------------------------

def _head(conn, key: tuple) -> str:
    opener = conn.execute("SELECT session_id FROM events WHERE event_id = ?", (key[2],)).fetchone()
    candidates = ([opener[0]] if opener else []) + _task_sessions(conn, key)
    for session in candidates:
        row = conn.execute("SELECT core_head FROM sessions WHERE session_id = ?", (session,)).fetchone()
        if row and row[0]:
            return row[0]
    return UNKNOWN_HEAD


def _flakes(checks: list) -> int:
    """(check, plan digest, code ref) groups where a red is later followed by a green, once each."""
    seen_red, flaky = set(), set()
    for fields in checks:
        group = (fields.get("check"), fields.get("plan_digest"), fields.get("code_ref"))
        if _is_red(fields):
            seen_red.add(group)
        elif group in seen_red:
            flaky.add(group)
    return len(flaky)


def _registry() -> "tuple[dict, dict]":
    """(source key -> elementary entry, entry -> its composites, transitively)."""
    members = {n: m for n, m in tables.load("registry").members.items() if m.retired_since is None}
    by_key = {}
    for name, member in members.items():
        if member.attrs.get("kind") == "elementary":
            for key in member.attrs.get("source_keys") or ():
                by_key.setdefault(key, name)
    parents = {}
    for name in members:
        seen, stack = [], list(members[name].attrs.get("part_of") or ())
        while stack:
            parent = stack.pop()
            if parent not in seen and parent in members:
                seen.append(parent)
                stack += members[parent].attrs.get("part_of") or ()
        parents[name] = seen
    return by_key, parents


def _mechanisms(task_events: list, registry) -> dict:
    by_key, parents = registry
    out: dict = {}
    for event in task_events:
        entry = by_key.get(event[3])
        if entry is None:
            continue
        for name in [entry] + parents[entry]:
            slot = out.setdefault(name, {"events": 0, "decisions": 0, "minutes": 0.0})
            slot["events"] += 1
            slot["decisions"] += event[2] == "decision"
            duration = event[6].get("duration_s")
            if isinstance(duration, (int, float)):
                slot["minutes"] += duration / 60.0
    return out


def unresolved_source_keys(conn, registry=None) -> "tuple[dict, dict]":
    """({live source key: count}, {imported source key: count}) of events no elementary entry
    holds."""
    by_key = (registry or _registry())[0]
    live_keys, imported_keys = {}, {}
    for source_key, provenance, count in conn.execute(
            "SELECT source_key, provenance, COUNT(*) FROM events GROUP BY source_key, provenance"):
        if source_key in by_key:
            continue
        target = live_keys if provenance == "live" else imported_keys
        target[source_key] = target.get(source_key, 0) + count
    return live_keys, imported_keys


def _row(group: list, all_tasks: int) -> dict:
    totals = {"tasks": len(group), "replans": 0, "review_rounds": 0, "effort_fires": 0,
              "verify_red": 0, "verify_green": 0, "flakes": 0, "minutes": 0.0, "spend_usd": 0.0,
              "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}}
    for item in group:
        for name in ("replans", "review_rounds", "effort_fires", "verify_red", "verify_green", "flakes"):
            totals[name] += item[name]
        totals["minutes"] += item["minutes"]
        totals["spend_usd"] += item["spend_usd"]
        for name, value in item["tokens"].items():
            totals["tokens"][name] += value
    checks = totals["verify_red"] + totals["verify_green"]
    totals["verify_red_rate"] = totals["verify_red"] / checks if checks else None
    count = len(group)
    totals["share_of_head_tasks"] = count / all_tasks
    totals["per_task"] = {name: totals[name] / count for name in
                          ("replans", "review_rounds", "effort_fires", "flakes", "minutes", "spend_usd")}
    totals["per_task"]["tokens"] = sum(totals["tokens"].values()) / count
    return totals


def _task_measures(conn, task: dict, task_events: list) -> dict:
    commands = [e for e in task_events if e[2] == "command" and e[6].get("outcome") == "ok"]
    checks = [e[6] for e in task_events if e[2] == "check"]
    tokens = conn.execute(
        "SELECT COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0), COALESCE(SUM(cache_read_tokens), 0), "
        "COALESCE(SUM(cache_write_tokens), 0) FROM token_usage WHERE task_origin = ? AND task_id = ? AND task_epoch = ?",
        task["task"]).fetchone()
    return {
        "replans": task["replans"],
        "review_rounds": sum(1 for e in commands if e[6].get("command") == "plan-review"),
        "effort_fires": sum(1 for e in task_events if e[3] == "gate:effort_fire" and e[6].get("outcome") != "pass"),
        "verify_red": sum(1 for f in checks if _is_red(f)),
        "verify_green": sum(1 for f in checks if f.get("exit") == 0),
        "flakes": _flakes(checks),
        "minutes": task["minutes"], "spend_usd": task["spend_usd"],
        "tokens": dict(zip(("input", "output", "cache_read", "cache_write"), tokens)),
    }


def _cause_counts(conn, session_ids: set) -> "tuple[dict, dict]":
    by_category, by_place = {}, {}
    for session, aligned, category, place in conn.execute(
            "SELECT session, alignment_session, cause_category, functional_place FROM causes"):
        if (aligned or session) in session_ids:
            by_category[category] = by_category.get(category, 0) + 1
            by_place[place] = by_place.get(place, 0) + 1
    categories = tables.load("categories").members
    places = tables.load("places").members
    return ({name: by_category.get(name, 0) for name in categories},
            {name: by_place.get(name, 0) for name in places})


def losses(conn) -> dict:
    counted = {mode: 0 for mode in sorted(importer.COUNTED_MODES)}
    for mode, count in conn.execute("SELECT loss_mode, SUM(count) FROM losses GROUP BY loss_mode"):
        if mode in counted:
            counted[mode] += count or 0
    return {"counted": counted, "uncounted_modes": sorted(importer._uncounted_modes())}


def core_version(conn, include_synthetic: bool = False) -> dict:
    registry = _registry()
    heads: dict = {}
    synthetic = 0
    for task in tasks(conn, include_synthetic):
        synthetic += task["synthetic"]
        task_events = _task_events(conn, task["task"])
        head = heads.setdefault(_head(conn, task["task"]), {"tasks": [], "events": [], "sessions": set()})
        head["tasks"].append({**_task_measures(conn, task, task_events), "weight_class": task["weight_class"]})
        head["events"] += task_events
        head["sessions"].update(_task_sessions(conn, task["task"]))
    out_heads = []
    for name, head in sorted(heads.items()):
        classes: dict = {}
        for item in head["tasks"]:
            classes.setdefault(item["weight_class"] or "unclassified", []).append(item)
        by_category, by_place = _cause_counts(conn, head["sessions"])
        out_heads.append({
            "head": name,
            "rows": {wc: _row(group, len(head["tasks"])) for wc, group in sorted(classes.items())},
            "causes_per_category": by_category, "causes_per_place": by_place,
            "mechanisms": _mechanisms(head["events"], registry),
        })
    unresolved, imported_unresolved = unresolved_source_keys(conn, registry)
    return {"heads": out_heads, "losses": losses(conn),
            "unattributed_contours": sync.unattached_contour_count(conn),
            "unresolved_source_keys": unresolved, "imported_unresolved_source_keys": imported_unresolved,
            "synthetic_tasks_counted": synthetic}


# --- one task's self cost -------------------------------------------------------------------

def self_cost(conn, task_id: str) -> dict:
    """Recorder overhead, landing minutes, landing intervals and land attempts of every epoch of
    `task_id`, and the event count of each self-measurement entry over the task's sessions."""
    keys = [_task_key(r) for r in conn.execute(
        "SELECT origin, task_id, epoch FROM tasks WHERE task_id = ?", (task_id,))]
    session_ids = sorted({s for key in keys for s in _task_sessions(conn, key)})
    rows = []
    for start in range(0, len(session_ids), 500):
        chunk = session_ids[start:start + 500]
        marks = ",".join("?" * len(chunk))
        rows += conn.execute(
            f"SELECT kind, source_key, time, fields FROM events WHERE session_id IN ({marks}) "
            f"OR json_extract(fields, '$.session') IN ({marks})", chunk + chunk).fetchall()
    rows = [(r[0], r[1], r[2], _fields(r[3])) for r in rows]
    overhead_ms = sum(f.get("recorder_s") or 0 for k, _, _, f in rows if k == "command") * 1000.0
    intervals = phases.landing_intervals(
        [{"source_key": s, "time": t, "task": f.get("task_epoch"), "branch": f.get("branch")}
         for k, s, t, f in rows if k == "landing"])
    landing_ms = sum(i["end"] - i["start"] for i in intervals if i["start"] is not None and i["end"] is not None)
    entries = {name: sum(1 for _, s, _, _ in rows if s == name) for name in SELF_COST_ENTRIES}
    return {"task_id": task_id, "epochs": len(keys), "sessions": session_ids,
            "recorder_overhead_ms": overhead_ms, "recorder_bound_ms": RECORDER_BOUND_MS,
            "landing_minutes": landing_ms / 60000.0, "landing_intervals": len(intervals),
            "land_attempts": sum(1 for _, s, _, _ in rows if s == "land:attempt"),
            "entry_events": entries}


def self_cost_failures(cost: dict, min_land_attempts: int, min_landing_intervals: int) -> list:
    failures = []
    if not cost["recorder_overhead_ms"] > 0:
        failures.append("no recorder overhead")
    if not cost["landing_minutes"] > 0:
        failures.append("no landing minutes")
    if cost["land_attempts"] < min_land_attempts:
        failures.append(f"land attempts {cost['land_attempts']} < {min_land_attempts}")
    if cost["landing_intervals"] < min_landing_intervals:
        failures.append(f"landing intervals {cost['landing_intervals']} < {min_landing_intervals}")
    failures += [f"no events of {name}" for name, count in cost["entry_events"].items() if not count]
    return failures


# --- check-events: every engine act has its event row ---------------------------------------

EVENT_MATCH_SLACK_S = 2.0


def _read_jsonl(path) -> list:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rows = []
    for raw in text.splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _engine_acts(task_sessions: list) -> list:
    """(session, act, ms) of every engine-history command and gate evaluation of the sessions."""
    acts = []
    for session in task_sessions:
        try:
            data = json.loads((config_root.agentctl_state_dir() / f"{session}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        history = data.get("history") if isinstance(data, dict) else None
        for entry in history if isinstance(history, list) else ():
            if isinstance(entry, dict) and entry.get("event"):
                acts.append((session, f"cli:{entry['event']}", phases._ms(entry.get("ts"))))
    for row in _read_jsonl(config_root.agentctl_gate_log()):
        if row.get("session") in task_sessions and row.get("gate"):
            acts.append((row["session"], f"gate:{row['gate']}", phases._ms(row.get("ts"))))
    return acts


def check_events(conn, task_id: str, window: "dict | None" = None) -> dict:
    """Each engine act of the task's sessions matched to one live event row of the same source key
    and session, recorded at the act or within its command's duration after it. An unmatched act
    inside `window` ({from, to}) is an excused loss; any other unmatched act fails the check."""
    keys = [_task_key(r) for r in conn.execute("SELECT origin, task_id, epoch FROM tasks WHERE task_id = ?", (task_id,))]
    task_sessions = sorted({s for key in keys for s in _task_sessions(conn, key)})
    pool: dict = {}
    for session in task_sessions:
        for source_key, moment, raw in conn.execute(
                "SELECT source_key, time, fields FROM events WHERE session_id = ? AND provenance = 'live'", (session,)):
            duration = _fields(raw).get("duration_s")
            pool.setdefault((session, source_key), []).append(
                (phases._ms(moment), (duration if isinstance(duration, (int, float)) else 0.0) * 1000.0))
    bounds = (phases._ms(window.get("from")), phases._ms(window.get("to"))) if window else None
    slack = EVENT_MATCH_SLACK_S * 1000.0
    missing, excused, matched = [], [], 0
    for session, source_key, ms in sorted(_engine_acts(task_sessions), key=lambda a: a[2] or 0):
        candidates = pool.get((session, source_key), [])
        hit = next((c for c in candidates if ms is not None and c[0] - c[1] - slack <= ms <= c[0] + slack), None)
        if hit is not None:
            candidates.remove(hit)
            matched += 1
            continue
        act = {"session": session, "act": source_key, "time_ms": ms}
        if bounds and ms is not None and bounds[0] <= ms <= bounds[1]:
            excused.append(act)
        else:
            missing.append(act)
    return {"ok": not missing and bool(keys), "task_id": task_id, "sessions": task_sessions, "matched": matched,
            "missing": missing, "excused": excused}


# --- check-latency: each ended session reaches the record within the bound -------------------

def _reserved_spool_sessions(conn) -> "tuple[int, set]":
    """(unreplayed bytes, sessions the unreplayed rows of the reserved sync spool name)."""
    from lib.telemetry import emit
    pending = sync.unreplayed_bytes(conn, sync.SYNC_SESSION)
    if not pending:
        return 0, set()
    path = emit.spool_path(sync.SYNC_SESSION)
    try:
        with open(path, "rb") as handle:
            handle.seek(max(0, path.stat().st_size - pending))
            tail = handle.read()
    except OSError:
        return pending, set()
    named = set()
    for raw in tail.splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        session = (row.get("fields") or {}).get("session") if isinstance(row, dict) else None
        if session:
            named.add(session)
    return pending, named


def _population(conn) -> list:
    """Non-synthetic sessions of the record, the record's own pseudo-sessions excluded."""
    synthetic = {r[0] for r in conn.execute(
        "SELECT s.session_id FROM sessions s JOIN tasks t ON t.origin = s.task_origin AND t.task_id = s.task_id "
        "AND t.epoch = s.task_epoch WHERE t.synthetic = 1")}
    synthetic |= {r[0] for r in conn.execute(
        "SELECT session_id FROM events WHERE kind = 'task' AND json_extract(fields, '$.synthetic') = 1")}
    return [r[0] for r in conn.execute("SELECT DISTINCT session_id FROM events ORDER BY session_id")
            if not r[0].startswith("telemetry:") and r[0] not in synthetic]


def check_latency(conn, max_seconds: float, landed_at: str, now) -> dict:
    """Each non-synthetic session that ended after `landed_at`, by the latency rule over
    sync.latency_marks: on_time, late, waiting (no trigger yet), in_flight (a trigger younger than
    the bound, no commit) or sync_deferred (its rows wait in the reserved spool). The check fails
    on a late session and when no session of the population has appeared."""
    landed = sync.parse_time(landed_at)
    pending, spooled = _reserved_spool_sessions(conn)
    deferred_rows = {r[0] for r in conn.execute(
        "SELECT json_extract(fields, '$.session') FROM events WHERE kind = ?", (sync.SYNC_DEFERRED,))}
    rows = []
    for session in _population(conn):
        end, first, commit = sync.latency_marks(conn, session)
        if end is None or end <= landed:
            continue
        seconds = None
        if commit is None and (session in spooled or (pending and session in deferred_rows)):
            status = "sync_deferred"
        elif first is None and commit is None:
            status = "waiting"
        elif commit is not None:
            seconds = 0.0 if first is None or commit < first else (commit - first).total_seconds()
            status = "late" if seconds > max_seconds else "on_time"
        elif (now - first).total_seconds() <= max_seconds:
            status = "in_flight"
        else:
            status = "late"
        rows.append({"session": session, "E": end, "T1": first, "A": commit, "latency_s": seconds, "status": status})
    late = [r["session"] for r in rows if r["status"] == "late"]
    ok = not late and any(r["A"] for r in rows)
    return {"ok": ok, "max_seconds": max_seconds, "landed_at": landed_at, "late": late,
            "by_status": {s: sum(1 for r in rows if r["status"] == s)
                          for s in ("on_time", "late", "waiting", "in_flight", "sync_deferred")},
            "sessions": rows}


# --- the report's own usage event and the CLI -----------------------------------------------

def emit_usage(view: str) -> None:
    """One `report` event per run of a report or check, so the report path is measured too."""
    session = os.environ.get("CLAUDE_CODE_SESSION_ID") or REPORT_SOURCE
    try:
        live.emit_event(session, "report", REPORT_SOURCE, {"view": view, "session": session})
    except Exception:  # telemetry is fail-open
        pass


def _print(payload: dict, fmt: str) -> None:
    if fmt == "json":
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return
    for head in payload.get("heads", ()):
        print(f"HEAD {head['head']}")
        for wc, row in head["rows"].items():
            print(f"  {wc:<14} tasks={row['tasks']} replans/task={row['per_task']['replans']:.2f} "
                  f"red_rate={row['verify_red_rate']} flakes={row['flakes']} minutes={row['minutes']:.1f} "
                  f"spend=${row['spend_usd']:.2f}")
    for name in ("losses", "unattributed_contours", "unresolved_source_keys",
                 "imported_unresolved_source_keys", "synthetic_tasks_counted", "self_cost"):
        if name in payload:
            print(f"{name}: {json.dumps(payload[name], sort_keys=True, default=str)}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="agent-report.py core-version", description=__doc__.split("\n\n")[0])
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--task", help="add the self cost of this task id")
    parser.add_argument("--include-synthetic", action="store_true")
    parser.add_argument("--check", action="store_true", help="exit 1 on a live source key no registry entry holds")
    parser.add_argument("--check-self-cost", action="store_true", help="with --task: exit 1 on a missing self cost")
    parser.add_argument("--min-land-attempts", type=int, default=1)
    parser.add_argument("--min-landing-intervals", type=int, default=1)
    args = parser.parse_args(argv)
    if args.check_self_cost and not args.task:
        parser.error("--check-self-cost needs --task")
    emit_usage("core-version")
    conn = store.open_readonly()
    if conn is None:
        print("no telemetry record", file=sys.stderr)
        return 2
    try:
        payload = core_version(conn, args.include_synthetic)
        if args.task:
            payload["self_cost"] = self_cost(conn, args.task)
    finally:
        conn.close()
    status = 0
    if args.check and payload["unresolved_source_keys"]:
        status = 1
    if args.check_self_cost:
        payload["self_cost_failures"] = self_cost_failures(
            payload["self_cost"], args.min_land_attempts, args.min_landing_intervals)
        status = 1 if payload["self_cost_failures"] else status
    _print(payload, args.format)
    return status
