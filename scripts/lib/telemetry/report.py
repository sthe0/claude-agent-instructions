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

def task_event_rows(conn, key: tuple) -> list:
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


def tasks_with_events(conn, include_synthetic: bool = False) -> "list[tuple[dict, list]]":
    """(tasks() row, its task_event_rows) per task: one pass over the record that a caller
    needing both tasks() and difficulties() hands to difficulties(task_events=...)."""
    out = []
    for row in conn.execute("SELECT origin, task_id, epoch, status, weight_class, synthetic FROM tasks "
                            "ORDER BY opened_at, task_id").fetchall():
        if row[5] and not include_synthetic:
            continue
        key = _task_key(row)
        events = task_event_rows(conn, key)
        out.append(({
            "task": key, "weight_class": row[4],
            "status": "RESOLVED" if row[3] == "resolved" else "open",
            "last_event_at": events[-1][5] if events else None,
            "minutes": _minutes(events),
            "spend_usd": _spend(conn, key),
            "replans": sum(1 for e in events if e[2] == "command" and e[6].get("command") == "replan"
                           and e[6].get("outcome") == "ok"),
            "synthetic": bool(row[5]),
        }, events))
    return out


def tasks(conn, include_synthetic: bool = False) -> list:
    """One dict per task row: key, weight_class, status (RESOLVED or open), last_event_at,
    minutes, spend_usd (each token row priced by its own model), replans, synthetic."""
    return [task for task, _ in tasks_with_events(conn, include_synthetic)]


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


def difficulties(conn, include_synthetic: bool = False, task_events: "list | None" = None) -> list:
    """One dict per difficulty, read from the command events' node transitions: an entry into
    DIAGNOSING opens one, the next command leaving it closes it. The cause is the one filed for
    the same (session, 0-based difficulty ordinal); an imported cause aligned to no difficulty
    is its own row, keyed by its cause id, with only the cause set. `task_events` is a
    tasks_with_events() result already read (include_synthetic is then not consulted)."""
    causes = _causes_by_slot(conn)
    out = []
    if task_events is None:
        task_events = tasks_with_events(conn, include_synthetic)
    for task, events in task_events:
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


def task_session_ids(conn, key: tuple) -> list:
    sessions = {r[0] for r in conn.execute(
        "SELECT session_id FROM sessions WHERE task_origin = ? AND task_id = ? AND task_epoch = ?", key)}
    opener = conn.execute("SELECT session_id FROM events WHERE event_id = ?", (key[2],)).fetchone()
    if opener:
        sessions.add(opener[0])
    return sorted(sessions)


def sessions(conn, task: tuple) -> list:
    """The task's sessions as dicts of their session-row columns (session_id only when no row)."""
    out = []
    for session in task_session_ids(conn, task):
        cursor = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session,))
        values = cursor.fetchone()
        out.append(dict(zip([d[0] for d in cursor.description], values)) if values else {"session_id": session})
    return out


def events(conn, task: tuple, kinds=None) -> list:
    """The task's events as dicts (fields decoded), oldest first; `kinds` filters by kind."""
    names = ("event_id", "session_id", "kind", "source_key", "provenance", "time", "fields")
    return [dict(zip(names, e)) for e in task_event_rows(conn, task) if kinds is None or e[2] in kinds]


# --- the core-version view ----------------------------------------------------------------

def _head(conn, key: tuple) -> str:
    opener = conn.execute("SELECT session_id FROM events WHERE event_id = ?", (key[2],)).fetchone()
    candidates = ([opener[0]] if opener else []) + task_session_ids(conn, key)
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
        task_events = task_event_rows(conn, task["task"])
        head = heads.setdefault(_head(conn, task["task"]), {"tasks": [], "events": [], "sessions": set()})
        head["tasks"].append({**_task_measures(conn, task, task_events), "weight_class": task["weight_class"]})
        head["events"] += task_events
        head["sessions"].update(task_session_ids(conn, task["task"]))
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
    session_ids = sorted({s for key in keys for s in task_session_ids(conn, key)})
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


def agentctl_command_keys() -> frozenset:
    """The act key of every agentctl verb, read from the CLI's own parser."""
    from agentctl import cli
    subparsers = next(a for a in cli.build_parser()._actions if isinstance(a, argparse._SubParsersAction))
    return frozenset(live.act_key(verb) for verb in subparsers.choices)


def _engine_acts(task_sessions: list, command_keys: "frozenset | None" = None) -> list:
    """(session, act, ms) of every engine-history command and gate evaluation of the sessions. A
    history entry is a command act when its event names an agentctl verb; the entries a verb logs
    about its own steps (an attempt, a verdict, a carried review) are not separate acts."""
    command_keys = agentctl_command_keys() if command_keys is None else command_keys
    acts = []
    for session in task_sessions:
        try:
            data = json.loads((config_root.agentctl_state_dir() / f"{session}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        history = data.get("history") if isinstance(data, dict) else None
        for entry in history if isinstance(history, list) else ():
            if isinstance(entry, dict) and entry.get("event") and live.act_key(entry["event"]) in command_keys:
                acts.append((session, live.act_key(entry["event"]), phases._ms(entry.get("ts"))))
    for row in _read_jsonl(config_root.agentctl_gate_log()):
        if row.get("session") in task_sessions and row.get("gate"):
            acts.append((row["session"], f"gate:{row['gate']}", phases._ms(row.get("ts"))))
    return acts


def task_sessions_of(conn, task_id: str) -> list:
    keys = [_task_key(r) for r in conn.execute("SELECT origin, task_id, epoch FROM tasks WHERE task_id = ?", (task_id,))]
    return sorted({s for key in keys for s in task_session_ids(conn, key)})


class KnownLossError(ValueError):
    """A known-loss file that cannot excuse anything as written: the check refuses it."""


def read_known_losses(entries, bound_ms: "int | None") -> list:
    """The entries of a known-loss file, each naming one act (session, act key, ts) lost for a
    stated cause before `bound_ms`. Every entry must hold all four, and its ts must fall before the
    bound, so the file names only losses that happened before the fix that closed them."""
    if bound_ms is None:
        raise KnownLossError("known losses need a bound (--known-losses-until or --known-losses-until-landing)")
    if not isinstance(entries, list):
        raise KnownLossError("known losses: `entries` is not a list")
    losses = []
    for entry in entries:
        entry = entry if isinstance(entry, dict) else {}
        cause = entry.get("cause")
        ms = phases._ms(entry.get("ts"))
        if not isinstance(cause, str) or not cause.strip():
            raise KnownLossError(f"known loss without a cause: {entry}")
        if not entry.get("session") or not entry.get("act") or ms is None:
            raise KnownLossError(f"known loss without session, act or ts: {entry}")
        if ms >= bound_ms:
            raise KnownLossError(f"known loss at or after the bound: {entry}")
        losses.append({"session": entry["session"], "act": live.normalized_source_key(entry["act"]),
                       "time_ms": ms, "cause": cause.strip()})
    return losses


def _known_loss_for(losses: list, session: str, source_key: str, ms) -> "dict | None":
    slack = EVENT_MATCH_SLACK_S * 1000.0
    return next((loss for loss in losses if loss["session"] == session and loss["act"] == source_key
                 and ms is not None and abs(loss["time_ms"] - ms) <= slack), None)


def check_events(conn, task_id: str, window: "dict | None" = None, command_keys: "frozenset | None" = None,
                 known_losses: "list | None" = None) -> dict:
    """Each engine act of the task's sessions matched to one live event row of the same source key
    and session, recorded at the act or within its command's duration after it; both sides read
    their keys through live.normalized_source_key, so a row stored in either spelling matches. An
    act before the first live event of a session that has live events is a counted loss
    (lost_before_first_live); an unmatched act inside `window` ({from, to}) is an excused loss; an
    act before `window` of a session with no live event was made before any emitter existed, so
    it is a loss named under lost_before_emitter; an unmatched act that one entry of
    `known_losses` (read_known_losses) names by session, act key and a ts within the match slack is
    the counted loss known_loss:<cause>, each entry excusing one act, and an entry naming no act of
    the task is refused; any other unmatched act fails, every later act of a session with no live
    event among them."""
    keys = [_task_key(r) for r in conn.execute("SELECT origin, task_id, epoch FROM tasks WHERE task_id = ?", (task_id,))]
    task_sessions = sorted({s for key in keys for s in task_session_ids(conn, key)})
    pool: dict = {}
    first_live: dict = {}
    for session in task_sessions:
        for source_key, moment, raw in conn.execute(
                "SELECT source_key, time, fields FROM events WHERE session_id = ? AND provenance = 'live'", (session,)):
            duration = _fields(raw).get("duration_s")
            ms = phases._ms(moment)
            if ms is not None:
                first_live[session] = min(ms, first_live.get(session, ms))
            pool.setdefault((session, live.normalized_source_key(source_key)), []).append(
                (ms, (duration if isinstance(duration, (int, float)) else 0.0) * 1000.0))
    bounds = (phases._ms(window.get("from")), phases._ms(window.get("to"))) if window else None
    slack = EVENT_MATCH_SLACK_S * 1000.0
    missing, excused, lost, before_emitter, known, matched = [], [], [], [], [], 0
    acts = [(session, live.normalized_source_key(source_key), ms) for session, source_key, ms
            in sorted(_engine_acts(task_sessions, command_keys), key=lambda a: a[2] or 0)]
    unused = list(known_losses or ())
    for loss in unused:
        if not any(_known_loss_for([loss], *act) for act in acts):
            raise KnownLossError(f"known loss matches no history act of the task: {loss}")
    for session, source_key, ms in acts:
        candidates = pool.get((session, source_key), [])
        hit = next((c for c in candidates if ms is not None and c[0] is not None
                    and c[0] - c[1] - slack <= ms <= c[0] + slack), None)
        if hit is not None:
            candidates.remove(hit)
            matched += 1
            continue
        act = {"session": session, "act": source_key, "time_ms": ms}
        start = first_live.get(session)
        if start is not None and ms is not None and ms < start - slack:
            lost.append(act)
        elif bounds and ms is not None and bounds[0] <= ms <= bounds[1]:
            excused.append(act)
        elif start is None and bounds and ms is not None and bounds[0] is not None and ms < bounds[0]:
            before_emitter.append(act)
        elif (loss := _known_loss_for(unused, session, source_key, ms)) is not None:
            unused.remove(loss)
            known.append({**act, "loss": "known_loss:" + loss["cause"]})
        else:
            missing.append(act)
    return {"ok": not missing and bool(keys), "task_id": task_id, "sessions": task_sessions, "matched": matched,
            "missing": missing, "excused": excused, "lost_before_first_live": len(lost),
            "lost_before_emitter": before_emitter, "known_losses": known}


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


SYNC_OUTAGE = "sync_outage"
PRE_FIX_LATE = "pre_fix_late"
IMPORT_ONLY = "import_only"
PRE_FIX_CAUSE_PREFIX = "pre_fix_cause_"
PRE_EMITTER = "pre_emitter"
IMPORT_SESSION_PREFIX = "import:"
LANDED_SOURCE_KEY = "land:landed"
LANDED_AT_MAX_LAG_S = 600.0
# The rows the record writes about itself (the sync, the report, the importer, a pre-commit run, a
# landing); a session holding nothing else did no work of its own.
RECORD_OWN_SOURCE_KEYS = (*SELF_COST_ENTRIES, importer.SELF_SOURCE_KEY, "precommit")
SYNTHETIC_TASK = "synthetic_task"
ONLY_RECORD_OWN_ROWS = "only_record_own_rows"


class LatencyInputError(ValueError):
    """An outage window or a pre-fix-late entry check-latency refuses (exit 2)."""


def outage_end_from_landing(conn, landing: dict):
    """The `landed_at` of a landing record, bound to the record's land:landed event whose tip or
    pushed head is the record's head (the earliest when several are): it may not precede that event
    nor follow it by more than LANDED_AT_MAX_LAG_S. Only landing rows are read."""
    head, claimed = landing.get("head"), sync.parse_time(landing.get("landed_at"))
    if not head or claimed is None:
        raise LatencyInputError("the landing record needs head and landed_at")
    events = []
    for moment, raw in conn.execute("SELECT time, fields FROM events WHERE kind = 'landing' AND source_key = ?",
                                    (LANDED_SOURCE_KEY,)):
        fields = _fields(raw)
        if head in (fields.get("tip"), fields.get("pushed_head")) and sync.parse_time(moment) is not None:
            events.append(sync.parse_time(moment))
    if not events:
        raise LatencyInputError(f"no {LANDED_SOURCE_KEY} event of head {head} in the record")
    landed_event = min(events)
    if claimed < landed_event or (claimed - landed_event).total_seconds() > LANDED_AT_MAX_LAG_S:
        raise LatencyInputError(f"landed_at {claimed.isoformat()} is not within {LANDED_AT_MAX_LAG_S:.0f} s "
                                f"after the {LANDED_SOURCE_KEY} event at {landed_event.isoformat()}")
    return claimed


def _synthetic_proofs(conn) -> dict:
    """{session: proof} of the sessions whose task rows mark them synthetic."""
    proofs = {r[0]: SYNTHETIC_TASK for r in conn.execute(
        "SELECT s.session_id FROM sessions s JOIN tasks t ON t.origin = s.task_origin AND t.task_id = s.task_id "
        "AND t.epoch = s.task_epoch WHERE t.synthetic = 1")}
    proofs.update({r[0]: SYNTHETIC_TASK for r in conn.execute(
        "SELECT session_id FROM events WHERE kind = 'task' AND json_extract(fields, '$.synthetic') = 1")})
    return proofs


def _holds_only_record_own_rows(conn, session: str) -> bool:
    marks = ",".join("?" * len(RECORD_OWN_SOURCE_KEYS))
    return conn.execute(f"SELECT 1 FROM events WHERE session_id = ? AND source_key NOT IN ({marks}) LIMIT 1",
                        (session, *RECORD_OWN_SOURCE_KEYS)).fetchone() is None


def _population(conn) -> "tuple[list, list]":
    """(sessions judged, [{session, proof}] excluded): a session leaves the population only when its
    rows prove it synthetic (its task is synthetic, or it holds nothing but the record's own rows)."""
    proofs = _synthetic_proofs(conn)
    judged, excluded = [], []
    for (session,) in conn.execute("SELECT DISTINCT session_id FROM events ORDER BY session_id").fetchall():
        proof = proofs.get(session) or (ONLY_RECORD_OWN_ROWS if _holds_only_record_own_rows(conn, session) else None)
        if proof:
            excluded.append({"session": session, "proof": proof})
        else:
            judged.append(session)
    return judged, excluded


def is_real_session(conn, session: str) -> bool:
    """The session ran here: it is not an import:* key and the record holds a row it wrote while it
    ran (a live row) or a transcript row of it."""
    if session.startswith(IMPORT_SESSION_PREFIX):
        return False
    return conn.execute("SELECT 1 FROM events WHERE session_id = ? AND provenance IN ('live', 'transcript') LIMIT 1",
                        (session,)).fetchone() is not None


def is_import_only(conn, session: str) -> bool:
    """No row of the session came through the live path (every row is imported or from a transcript;
    the sync's rows naming it are the sync's own) and no spool of it exists, so a sync never had
    anything of it to commit."""
    from lib.telemetry import emit
    if conn.execute("SELECT 1 FROM events WHERE session_id = ? AND provenance NOT IN ('imported', 'transcript') "
                    "LIMIT 1", (session,)).fetchone() is not None:
        return False
    stem = emit.session_file_stem(session)
    return stem is None or not emit.spool_path(stem).exists()


def _earliest_time(conn, session: str):
    texts = [r[0] for r in conn.execute("SELECT time FROM events WHERE session_id = ?", (session,))]
    return min(sync.known_times(texts), default=None)


def _pre_fix_causes(conn, entries, rows: dict, landed, span_end) -> dict:
    """{session: status} of the pre-fix-late entries, each checked against the record."""
    statuses = {}
    for entry in entries:
        entry = entry if isinstance(entry, dict) else {}
        cause, session = entry.get("cause"), entry.get("session")
        if not isinstance(cause, str) or not cause.startswith(PRE_FIX_CAUSE_PREFIX):
            raise LatencyInputError(f"entry {entry!r}: the cause must start with {PRE_FIX_CAUSE_PREFIX}")
        if not isinstance(session, str) or not session:
            raise LatencyInputError(f"entry {entry!r}: no session")
        import_only = is_import_only(conn, session)
        if not import_only and not is_real_session(conn, session):
            raise LatencyInputError(f"{session}: neither a real session nor import-only")
        row = rows.get(session)
        if row is None or row["status"] != "late":
            raise LatencyInputError(f"{session}: not late by the rule")
        if not landed <= row["T1"] < span_end:
            raise LatencyInputError(f"{session}: T1 {row['T1'].isoformat()} is outside [{landed.isoformat()}, "
                                    f"{span_end.isoformat()})")
        statuses[session] = f"{IMPORT_ONLY if import_only else PRE_FIX_LATE}:{cause}"
    return statuses


def check_latency(conn, max_seconds: float, landed_at: str, now, outage: "tuple | None" = None,
                  pre_fix_entries: "list | None" = None) -> dict:
    """Each session of the population that ended after `landed_at`, by the latency rule over
    sync.latency_marks: on_time, late, waiting (no trigger yet), in_flight (a trigger younger than
    the bound, no commit) or sync_deferred (its rows wait in the reserved spool). A late session is
    instead a counted named loss: sync_outage when its T1 is in `outage` ([from, until)), the
    status its pre-fix-late entry names (pre_fix_late:<cause>, or import_only:<cause> for an
    import-only session), or import_only:pre_emitter for an import-only session whose earliest
    timed row precedes `landed_at`. The check fails on a late session and when no session of the
    population has appeared; it raises LatencyInputError on an entry it refuses."""
    landed = sync.parse_time(landed_at)
    pending, spooled = _reserved_spool_sessions(conn)
    deferred_rows = {r[0] for r in conn.execute(
        "SELECT json_extract(fields, '$.session') FROM events WHERE kind = ?", (sync.SYNC_DEFERRED,))}
    rows = []
    skipped = {sync.NO_TIME_ROWS: 0}
    judged, excluded = _population(conn)
    starts = sync.trigger_start_times(conn, skipped)
    for session in judged:
        end, first, commit = sync.latency_marks(conn, session, skipped, starts)
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
        if status == "late" and outage and outage[0] <= first < outage[1]:
            status = SYNC_OUTAGE
        rows.append({"session": session, "E": end, "T1": first, "A": commit, "latency_s": seconds, "status": status})
    by_session = {r["session"]: r for r in rows}
    named = _pre_fix_causes(conn, pre_fix_entries or (), by_session, landed, outage[0] if outage else now)
    for row in rows:
        if row["session"] in named:
            row["status"] = named[row["session"]]
        elif row["status"] == "late" and is_import_only(conn, row["session"]):
            earliest = _earliest_time(conn, row["session"])
            if earliest is not None and earliest < landed:
                row["status"] = f"{IMPORT_ONLY}:{PRE_EMITTER}"
    late = [r["session"] for r in rows if r["status"] == "late"]
    ok = not late and any(r["A"] for r in rows)
    by_status = {s: 0 for s in ("on_time", "late", "waiting", "in_flight", "sync_deferred")}
    for row in rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
    losses = {s: n for s, n in by_status.items() if s == SYNC_OUTAGE or ":" in s}
    return {"ok": ok, "max_seconds": max_seconds, "landed_at": landed_at, "late": late, "by_status": by_status,
            "losses": losses, "excluded": excluded, "excluded_count": len(excluded),
            sync.NO_TIME_ROWS: skipped[sync.NO_TIME_ROWS], "sessions": rows}


# --- the report's own usage event and the CLI -----------------------------------------------

def task_opener_session(conn, task_id: str) -> "str | None":
    """The session that opened the latest epoch of `task_id`: the task's engine root."""
    row = conn.execute(
        "SELECT e.session_id FROM tasks t JOIN events e ON e.event_id = t.epoch WHERE t.task_id = ? "
        "ORDER BY t.opened_at DESC LIMIT 1", (task_id,)).fetchone()
    return row[0] if row else None


def usage_session(conn=None, task_id: "str | None" = None) -> str:
    """The session a report run is charged to: the harness session, else the engine root of the
    task the run reports on, else the report's own pseudo-session."""
    env_session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if env_session:
        return env_session
    if conn is not None and task_id:
        try:
            return task_opener_session(conn, task_id) or REPORT_SOURCE
        except Exception:
            return REPORT_SOURCE
    return REPORT_SOURCE


def emit_usage(view: str, conn=None, task_id: "str | None" = None) -> None:
    """One `report` event per run of a report or check, so the report path is measured too; a run
    charged to a real session replays that session's spool so the next read of the record sees it."""
    session = usage_session(conn, task_id)
    try:
        live.emit_event(session, "report", REPORT_SOURCE, {"view": view, "session": session})
        if session != REPORT_SOURCE:
            store.replay(session)
    except Exception:  # telemetry is fail-open
        pass


def unstamped_row_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM events WHERE time = ''").fetchone()[0]


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
                 "imported_unresolved_source_keys", "synthetic_tasks_counted", sync.NO_TIME_ROWS, "self_cost"):
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
    conn = store.open_readonly()
    if conn is None:
        emit_usage("core-version")
        print("no telemetry record", file=sys.stderr)
        return 2
    try:
        emit_usage("core-version", conn, args.task)
        payload = core_version(conn, args.include_synthetic)
        payload[sync.NO_TIME_ROWS] = unstamped_row_count(conn)
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
