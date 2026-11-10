"""Substrate adequacy: difficulty mass per functional place, read from the telemetry record.

A difficulty is a rework interval: it opens at an entry into DIAGNOSING (a failed stage, an
effort-divergence firing or a review-cycle limit) and closes at the next replan, unblock or
reset. Its severity comes from severity.toml: the band of the interval's length, `never_closed`
when its task ended with the interval still open, `interval_unknown` for an imported cause no
interval was found for; an open interval of a running task weighs nothing yet. A task has ended
when it is RESOLVED or has been silent longer than the abandonment window.

Each difficulty is attributed to the place of the registry entry its cause names (the cause's
own place when it names no entry) and rolled up to the place of every composite reachable from
that entry through part_of. A place's mass is the sum of the severities of the difficulties
credited to it, each difficulty at most once per place, keyed by (task, difficulty id): one
session may feed several task rows, each restarting its difficulty ordinals. Place `none`
(not machinery-induced) is never ranked. Inputs come only through report.tasks_with_events()
and report.difficulties(); this module reads the record and writes nothing.
"""
from __future__ import annotations

from datetime import timedelta

from lib.telemetry import report, sync, tables

NONE_PLACE = "none"
UNKNOWN_MECHANISM = "unknown"
CATEGORY_DEFAULT = "category_default"
INCLUDE_SYNTHETIC = False


# --- tables -------------------------------------------------------------------------------

def load_registry() -> dict:
    """{entry: {kind, part_of, place}} of the registry's live (unretired) entries."""
    return {name: {"kind": m.attrs.get("kind"), "part_of": list(m.attrs.get("part_of") or ()),
                   "place": m.attrs.get("place")}
            for name, m in tables.load("registry").members.items() if m.retired_since is None}


def load_severity() -> dict:
    """{"bands": [(from_minutes, below_minutes or None, mass)], case: mass or None}."""
    members = tables.load("severity").members
    bands = sorted((m.attrs["from_minutes"], m.attrs.get("below_minutes"), m.attrs["mass"])
                   for m in members.values() if m.attrs.get("kind") == "band")
    cases = {name: m.attrs.get("mass") for name, m in members.items() if m.attrs.get("kind") == "case"}
    return {"bands": bands, **cases}


# --- one difficulty -----------------------------------------------------------------------

def task_ended(task: "dict | None", now, abandon_days: float) -> bool:
    if task is None or task["status"] == "RESOLVED":
        return True
    last = task["last_event_at"]
    return last is None or now - sync.parse_time(last) > timedelta(days=abandon_days)


def band_mass(minutes: float, severity: dict) -> int:
    for start, below, mass in severity["bands"]:
        if minutes >= start and (below is None or minutes < below):
            return mass
    raise ValueError(f"no severity band holds {minutes} minutes")


def severity_of(difficulty: dict, task: "dict | None", now, abandon_days: float, severity: dict):
    """(case, mass) of one difficulty; mass None when it weighs nothing (open, task running)."""
    if difficulty["entry_at"] is None:
        return "interval_unknown", severity["interval_unknown"]
    if difficulty["exit_at"] is not None and difficulty["exit_kind"] in report.DIFFICULTY_EXITS:
        minutes = (sync.parse_time(difficulty["exit_at"]) - sync.parse_time(difficulty["entry_at"])).total_seconds() / 60.0
        return "closed", band_mass(minutes, severity)
    if task_ended(task, now, abandon_days):
        return "never_closed", severity["never_closed"]
    return "open_running", severity["open_running"]


def _rollup(registry: dict, entry: str) -> list:
    """`entry` and every composite reachable from it through part_of, once per path."""
    out = [entry]
    for parent in registry[entry]["part_of"]:
        if parent in registry:
            out += _rollup(registry, parent)
    return out


def places_of(cause: "dict | None", registry: dict) -> list:
    """Places a cause is credited to, most specific first; a place reached by several entries
    or paths appears once per reach."""
    if cause is None:
        return []
    mechanism = cause["mechanism"]
    if cause["machinery_induced"] and mechanism in registry:
        return [registry[entry]["place"] for entry in _rollup(registry, mechanism)]
    return [cause["functional_place"]]


def _closures(registry: dict) -> dict:
    return {name: set(_rollup(registry, name)) - {name} for name in registry}


def related_places(registry: dict) -> dict:
    """{place: places part_of-related to it}: an entry at one is an ancestor or descendant of
    an entry at the other."""
    related: dict = {}
    for name, ancestors in _closures(registry).items():
        for ancestor in ancestors:
            low, high = registry[name]["place"], registry[ancestor]["place"]
            if low != high:
                related.setdefault(low, set()).add(high)
                related.setdefault(high, set()).add(low)
    return related


# --- mass ---------------------------------------------------------------------------------

def _task_label(key) -> str:
    return "/".join(str(part) for part in key)


def compute(tasks: list, difficulties: list, now, abandon_days: float,
            registry: "dict | None" = None, severity: "dict | None" = None) -> dict:
    """Per place: mass, the machinery-induced part of it and the credited difficulties; plus the
    record-wide counts of difficulties by severity case, the uncaused ones and the caused ones
    credited to no ranked place (`none` or no place)."""
    registry = load_registry() if registry is None else registry
    severity = load_severity() if severity is None else severity
    by_task = {t["task"]: t for t in tasks}
    credit: dict = {}
    counts = {"difficulties": 0, "uncaused": 0, "unplaced": 0,
              "cases": {"closed": 0, "never_closed": 0, "interval_unknown": 0, "open_running": 0}}
    for difficulty in difficulties:
        counts["difficulties"] += 1
        case, mass = severity_of(difficulty, by_task.get(difficulty["task"]), now, abandon_days, severity)
        counts["cases"][case] += 1
        cause = difficulty["cause"]
        ranked = [p for p in places_of(cause, registry) if p not in (None, NONE_PLACE)]
        if cause is None:
            counts["uncaused"] += 1
        elif not ranked:
            counts["unplaced"] += 1
        if mass is None:
            continue
        key = (difficulty["task"], difficulty["difficulty"])
        for place in ranked:
            slot = credit.setdefault(place, {})
            slot[key] = (mass, difficulty, case)
    places = {}
    for place, slot in credit.items():
        credited = [(d, case, m) for m, d, case in slot.values()]
        places[place] = {"mass": sum(m for _, _, m in credited),
                         "machinery_mass": sum(m for d, _, m in credited if d["cause"]["machinery_induced"]),
                         "credited": credited}
    composite_places = {e["place"] for e in registry.values() if e["kind"] == "composite"}
    return {"places": places, "counts": counts, "by_task": by_task,
            "composite_places": composite_places, "related": related_places(registry)}


def ranking(result: dict) -> list:
    """Every credited place once, heaviest first, ties by name."""
    return [{"place": place, "mass": data["mass"], "composite": place in result["composite_places"]}
            for place, data in sorted(result["places"].items(), key=lambda kv: (-kv[1]["mass"], kv[0]))]


def ties(rows: list) -> list:
    groups: dict = {}
    for row in rows:
        groups.setdefault(row["mass"], []).append(row["place"])
    return [{"mass": mass, "places": names} for mass, names in groups.items() if len(names) > 1]


def finding(place: str, result: dict) -> dict:
    data = result["places"][place]
    keys = []
    for difficulty, _, _ in data["credited"]:
        if difficulty["task"] is not None and difficulty["task"] not in keys:
            keys.append(difficulty["task"])
    owned = [result["by_task"][k] for k in keys if k in result["by_task"]]
    causes = [d["cause"] for d, _, _ in data["credited"]]
    return {
        "place": place, "mass": data["mass"], "composite": place in result["composite_places"],
        "difficulties": len(data["credited"]),
        "tasks": [_task_label(k) for k in keys],
        "minutes": sum(t["minutes"] for t in owned),
        "spend_usd": sum(t["spend_usd"] for t in owned),
        "replans": sum(t["replans"] for t in owned),
        "interval_unknown": sum(1 for _, case, _ in data["credited"] if case == "interval_unknown"),
        "unknown_mechanism": sum(1 for c in causes if c["mechanism"] == UNKNOWN_MECHANISM),
        "category_default_place": sum(1 for c in causes if c.get("place_basis") == CATEGORY_DEFAULT),
        "recommended_next_step": "planner",
    }


def findings(result: dict, threshold: float) -> list:
    return [finding(row["place"], result) for row in ranking(result) if row["mass"] >= threshold]


def calibrate(result: dict, places: int) -> dict:
    """Count `places` places down the ranking, skipping one part_of-related to a higher-ranked
    counted place; report the Nth counted place's mass, the ties at it and the shares."""
    rows, counted = [], []
    for row in ranking(result):
        nested = sorted(result["related"].get(row["place"], set()) & set(counted))
        if nested:
            rows.append({**row, "status": "skipped", "nested_with": nested})
            continue
        status = "counted" if len(counted) < places else "beyond"
        if status == "counted":
            counted.append(row["place"])
        rows.append({**row, "status": status, "nested_with": []})
    total = sum(d["mass"] for d in result["places"].values())
    counted_mass = sum(result["places"][p]["mass"] for p in counted)
    machinery = sum(d["machinery_mass"] for d in result["places"].values())
    nth = counted[places - 1] if len(counted) >= places else None
    nth_mass = result["places"][nth]["mass"] if nth else None
    tied = [r["place"] for r in rows if nth and r["status"] != "skipped" and r["mass"] == nth_mass and r["place"] != nth]
    return {"places": places, "nth_place": nth, "nth_mass": nth_mass, "ties_at_nth": tied,
            "counted": counted, "rows": rows, "total_mass": total,
            "counted_share": counted_mass / total if total else None,
            "machinery_induced_share": machinery / total if total else None}


def load(conn, now, abandon_days: float, registry: "dict | None" = None) -> dict:
    """compute() over the record's readers, synthetic tasks left out; one pass over the tasks
    and their events feeds both readers."""
    task_events = report.tasks_with_events(conn, INCLUDE_SYNTHETIC)
    return compute([task for task, _ in task_events], report.difficulties(conn, task_events=task_events),
                   now, abandon_days, registry)
