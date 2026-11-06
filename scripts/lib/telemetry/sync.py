"""The telemetry sync: delivers every spool to the record and files what the events say.

Difficulty removed: a session's events reach the record only once somebody replays its spool,
and a killed session has nobody. A sync does not wait for a session to end: it replays its own
spool and every other spool with unreplayed bytes, whatever its age (replay is exactly-once by
offset and event id, so replaying a live session early is safe). It then materializes, per
replayed session, the rows the events imply (session, task, contour, artifacts) and writes its own
work as events under the reserved source key `telemetry:sync`, through `store.insert_rows`, never
into a visited spool: `trigger.start` when it begins, `sync.commit` per spool it replayed,
`sync.deferred` past the retry budget. A row `insert_rows` could not place goes to the reserved
spool `telemetry:sync.jsonl`, which the next sync replays first; none is lost.

A spool is eligible (its session counts as ended) when it has an end marker, or has none and its
last append is older than LIVE_TTL_S; eligibility decides only prune and whether a drained spool
is considered for a zero-row commit. `latency_marks` is the one function computing a session's
eligibility time E, first trigger T1 and appearance A; it is what the latency check reads.

Hot-path rule: git, the harness version and transcripts are read here, never in `cli.main`.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lib import config_root
from lib.telemetry import emit, live, store, tables

BUDGET_S = 120.0
BACKOFF_FIRST_S = 1.0
BACKOFF_MAX_S = 30.0
LATENCY_S = 600.0
LIVE_TTL_S = live.LIVE_TTL_S
SYNC_SESSION = live.SYNC_SESSION
SOURCE_KEY = "telemetry:sync"
TRIGGER_START = "trigger.start"
SYNC_COMMIT = "sync.commit"
SYNC_DEFERRED = "sync.deferred"
HARNESS_VERSION_ENV = "AGENTCTL_HARNESS_VERSION"
CORE_ROOT = Path(__file__).resolve().parents[3]
GIT_TIMEOUT_S = 5.0
TRANSCRIPT_VERSION_LINES = 50

_sleep = time.sleep


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(timezone.utc)


# --- the sync's own rows -------------------------------------------------------------

def _own_event(kind: str, fields: dict, moment: datetime) -> dict:
    stamp = _iso(moment)
    return {"event_id": emit.uuid7(), "session_id": SYNC_SESSION, "kind": kind,
            "source_key": SOURCE_KEY, "provenance": "live", "time": stamp,
            "fields": {**fields, "source_key": SOURCE_KEY, "time": stamp}}


def _keep_in_reserved_spool(row: dict) -> None:
    """Append `row` to the reserved spool with its own id and original time, as emit frames a line."""
    event = {"event_id": row["event_id"], "session_id": SYNC_SESSION, "kind": row["kind"],
             "time": row["time"], "provenance": "live", "backref": None, "fields": row["fields"]}
    line = b"\n" + json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n"
    try:
        emit.ensure_private_dir(live.spool_dir())
        live.append_line(emit.spool_path(SYNC_SESSION), line)
    except OSError:
        pass


def _write_own(kind: str, fields: dict, moment: "datetime | None" = None) -> str:
    row = _own_event(kind, fields, moment or _now())
    outcome = store.insert_rows("events", [row])
    if outcome["deferred"] or outcome["failed"]:
        _keep_in_reserved_spool(row)
    return row["event_id"]


# --- spools ---------------------------------------------------------------------------

def spool_stems() -> "list[str]":
    """The sessions whose spool file exists, the reserved sync spool excluded."""
    try:
        names = [entry.name for entry in os.scandir(live.spool_dir())]
    except OSError:
        return []
    suffix = emit.SPOOL_SUFFIX
    return sorted(n[:-len(suffix)] for n in names if n.endswith(suffix) and n[:-len(suffix)] != SYNC_SESSION)


def unreplayed_bytes(conn, stem: str) -> int:
    try:
        size = emit.spool_path(stem).stat().st_size
    except OSError:
        return 0
    row = conn.execute("SELECT byte_offset FROM spool_offsets WHERE spool = ?", (stem,)).fetchone()
    return max(0, size - (row[0] if row else 0))


def is_eligible(stem: str, now: "datetime | None" = None) -> bool:
    """The session counts as ended: an end marker exists, or none does and the spool's last
    append is older than LIVE_TTL_S."""
    if live.end_marker_path(stem).exists():
        return True
    try:
        mtime = emit.spool_path(stem).stat().st_mtime
    except OSError:
        return False
    return ((now or _now()).timestamp() - mtime) > LIVE_TTL_S


# --- the latency rule -----------------------------------------------------------------

def _times(conn, sql: str, args: tuple) -> "list[datetime]":
    return sorted(_parse(row[0]) for row in conn.execute(sql, args))


def latency_marks(conn, session_id: str) -> "tuple[datetime | None, datetime | None, datetime | None]":
    """(E, T1, A) of a session by the latency rule.

    E: the time of the session_end trigger.start that names the session, else its last recorded
    event time plus LIVE_TTL_S (the sync's own rows carry the sync session id, so they are
    not events of the session and never move E). T1: the first trigger.start, session_end or
    sweep, at or after E. A: the first sync.commit for the session at or after E."""
    ended = _times(
        conn, "SELECT time FROM events WHERE kind = ? AND json_extract(fields, '$.kind') = 'session_end' "
        "AND json_extract(fields, '$.session') = ?", (TRIGGER_START, session_id))
    if ended:
        end = ended[0]
    else:
        last = _times(conn, "SELECT time FROM events WHERE session_id = ?", (session_id,))
        if not last:
            return None, None, None
        end = last[-1] + timedelta(seconds=LIVE_TTL_S)
    first = next((t for t in _times(conn, "SELECT time FROM events WHERE kind = ?", (TRIGGER_START,)) if t >= end), None)
    commit = next((t for t in _times(
        conn, "SELECT time FROM events WHERE kind = ? AND json_extract(fields, '$.session') = ?",
        (SYNC_COMMIT, session_id)) if t >= end), None)
    return end, first, commit


def latency(conn, session_id: str, now: "datetime | None" = None) -> dict:
    """E, T1, A and what the latency rule makes of them: on time with latency 0 when A falls
    between E and T1, else A minus T1; late iff A exists and A - T1 exceeds S, or no A exists and
    now - T1 exceeds S."""
    end, first, commit = latency_marks(conn, session_id)
    now = now or _now()
    seconds = None
    late = False
    if first is not None:
        if commit is None:
            late = (now - first).total_seconds() > LATENCY_S
        elif commit < first:
            seconds = 0.0
        else:
            seconds = (commit - first).total_seconds()
            late = seconds > LATENCY_S
    elif commit is not None:
        seconds = 0.0
    return {"E": end, "T1": first, "A": commit, "latency_s": seconds, "late": late}


# --- reading events back by task ------------------------------------------------------

def _fields(raw) -> dict:
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _task_key_for(conn, session: str, moment: str, explicit_epoch: "str | None"):
    """(origin, task_id, epoch) the event of `session` at `moment` belongs to: the epoch it
    names, else the session's task row whose opening event is the latest at or before the event,
    else the session row's own task key, else None."""
    if explicit_epoch:
        row = conn.execute("SELECT origin, task_id, epoch FROM tasks WHERE epoch = ?", (explicit_epoch,)).fetchone()
        if row:
            return tuple(row)
    opens = [(_parse(r[1]), r[0]) for r in conn.execute(
        "SELECT event_id, time FROM events WHERE session_id = ? AND kind = 'task' "
        "AND json_extract(fields, '$.action') = 'open'", (session,))]
    stamp = _parse(moment)
    before = [(t, i) for t, i in opens if t <= stamp]
    if before:
        row = conn.execute("SELECT origin, task_id, epoch FROM tasks WHERE epoch = ?", (max(before)[1],)).fetchone()
        if row:
            return tuple(row)
    row = conn.execute("SELECT task_origin, task_id, task_epoch FROM sessions WHERE session_id = ?", (session,)).fetchone()
    if row and row[1] is not None:
        return tuple(row)
    return None


def events_of_task(conn, task_origin: str, task_id: str, epoch: str) -> "list[str]":
    """The ids of the events attributed to one task row: those naming its epoch in their fields,
    and those naming none, whose session's attribution rule lands on this row."""
    target = (task_origin, task_id, epoch)
    chosen = {r[0] for r in conn.execute(
        "SELECT event_id FROM events WHERE json_extract(fields, '$.task_epoch') = ?", (epoch,))}
    sessions = {r[0] for r in conn.execute(
        "SELECT session_id FROM sessions WHERE task_origin = ? AND task_id = ? AND task_epoch = ?", target)}
    opener = conn.execute("SELECT session_id FROM events WHERE event_id = ?", (epoch,)).fetchone()
    if opener:
        sessions.add(opener[0])
    for session in sessions:
        for event_id, moment, raw in conn.execute(
                "SELECT event_id, time, fields FROM events WHERE session_id = ?", (session,)).fetchall():
            if _fields(raw).get("task_epoch") is None and _task_key_for(conn, session, moment, None) == target:
                chosen.add(event_id)
    return sorted(chosen)


def unattached_contour_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM contours WHERE task_id IS NULL").fetchone()[0]


# --- what the sync reads outside the record -------------------------------------------

def _canon_state() -> "tuple[str | None, int | None]":
    """HEAD of the canonical checkout and whether its work tree is dirty."""
    def git(*args):
        done = subprocess.run(["git", "-C", str(CORE_ROOT), *args], capture_output=True, text=True,
                              timeout=GIT_TIMEOUT_S, check=True)
        return done.stdout.strip()
    try:
        return git("rev-parse", "HEAD"), int(bool(git("status", "--porcelain")))
    except (OSError, subprocess.SubprocessError):
        return None, None


def _harness_version(session: str) -> "str | None":
    override = os.environ.get(HARNESS_VERSION_ENV)
    if override:
        return override
    for root in config_root.projects_roots():
        for path in sorted(root.glob(f"*/{session}.jsonl"))[:1]:
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    for _ in range(TRANSCRIPT_VERSION_LINES):
                        line = handle.readline()
                        if not line:
                            break
                        version = _fields(line).get("version")
                        if isinstance(version, str) and version:
                            return version
            except OSError:
                continue
    return None


def _subagent_contours(session: str) -> "list[str]":
    ids = []
    for root in config_root.projects_roots():
        for path in sorted(root.glob(f"*/{session}/subagents/*.jsonl")):
            ids.append(f"{session}/{path.stem}")
    return ids


def _artifact_texts(session: str) -> "list[tuple[str, str]]":
    """Plan text and essence/full renderings of the session's engine state, read off the hot path."""
    path = config_root.resolve_agentctl_state_file(session)
    if path is None:
        return []
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    texts = []
    plan_path = state.get("plan_path")
    if plan_path:
        try:
            texts.append(("plan", Path(plan_path).read_text(encoding="utf-8")))
        except OSError:
            pass
    for presentation in state.get("plan_presentations") or []:
        text = presentation.get("rendering_text")
        if text:
            texts.append((f"{presentation.get('kind', 'plan')}_rendering", text))
    return [(kind, text) for kind, text in texts if text.strip()]


# --- materialization ------------------------------------------------------------------

_SESSION_UPSERT = (
    "INSERT INTO sessions (session_id, origin, parent_session, role, role_table_version, depth, started_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (session_id) DO UPDATE SET "
    "origin = COALESCE(sessions.origin, excluded.origin), "
    "parent_session = COALESCE(sessions.parent_session, excluded.parent_session), "
    "role = COALESCE(sessions.role, excluded.role), "
    "role_table_version = COALESCE(sessions.role_table_version, excluded.role_table_version), "
    "depth = COALESCE(sessions.depth, excluded.depth), "
    "started_at = COALESCE(sessions.started_at, excluded.started_at)")

_CONTOUR_UPSERT = (
    "INSERT INTO contours (contour_id, parent_session, role, role_table_version, weight_class, "
    "task_origin, task_id, task_epoch) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (contour_id) DO UPDATE SET "
    "weight_class = COALESCE(contours.weight_class, excluded.weight_class), "
    "task_origin = CASE WHEN contours.task_id IS NULL THEN excluded.task_origin ELSE contours.task_origin END, "
    "task_epoch = CASE WHEN contours.task_id IS NULL THEN excluded.task_epoch ELSE contours.task_epoch END, "
    "task_id = CASE WHEN contours.task_id IS NULL THEN excluded.task_id ELSE contours.task_id END")


def _put_contour(conn, contour_id: str, parent: "str | None", role: str, key) -> None:
    key = key or (None, None, None)
    weight = None
    if key[2]:
        row = conn.execute("SELECT weight_class FROM tasks WHERE epoch = ?", (key[2],)).fetchone()
        weight = row[0] if row else None
    conn.execute(_CONTOUR_UPSERT, (contour_id, parent, role, tables.validate("roles", role), weight, *key))


def _session_role(sess_event: "dict | None", spawn: "dict | None") -> str:
    if sess_event and sess_event.get("linked"):
        return "linked-child"
    if spawn is not None:
        role = spawn.get("role")
        return role if role in ("linked-child", "spawned-child") else "spawned-child"
    return "root"


def _materialize_session(conn, origin: str, session: str, subagents: "list[str]", artifacts) -> None:
    events = [(r[0], r[1], r[2], _fields(r[3])) for r in conn.execute(
        "SELECT event_id, kind, time, fields FROM events WHERE session_id = ? ORDER BY time, event_id", (session,))]
    if not events:
        return
    sess_event = next((f for _, kind, _, f in events if kind == "session"), None)
    spawn_row = conn.execute(
        "SELECT session_id, time, fields FROM events WHERE kind = 'spawn' AND child_session_id = ? "
        "ORDER BY time LIMIT 1", (session,)).fetchone()
    spawn = _fields(spawn_row[2]) if spawn_row else None
    parent = (sess_event or {}).get("parent_session") or (spawn_row[0] if spawn_row else None)
    role = _session_role(sess_event, spawn)
    depth = (sess_event or {}).get("depth")
    if depth is None:
        depth = 1 if parent else 0
    conn.execute(_SESSION_UPSERT, (session, origin, parent, role, tables.validate("roles", role), depth, events[0][2]))

    for event_id, kind, moment, fields in events:
        if kind == "task" and fields.get("action") == "open" and fields.get("task_id"):
            conn.execute(
                "INSERT OR IGNORE INTO tasks (origin, task_id, epoch, status, synthetic, opened_at) "
                "VALUES (?, ?, ?, 'open', ?, ?)",
                (origin, fields["task_id"], event_id, 1 if fields.get("synthetic") else 0, moment))
        elif kind == "task" and fields.get("action") == "abandon" and fields.get("epoch"):
            conn.execute("UPDATE tasks SET status = 'abandoned', ended_at = ? WHERE epoch = ? AND status = 'open'",
                         (moment, fields["epoch"]))
    for event_id, kind, moment, fields in events:
        epoch = fields.get("task_epoch")
        if kind != "command" or not epoch:
            continue
        if fields.get("weight_class"):
            conn.execute("UPDATE tasks SET weight_class = ? WHERE epoch = ?", (fields["weight_class"], epoch))
        if fields.get("node_after") == "RESOLVED":
            conn.execute("UPDATE tasks SET status = 'resolved', ended_at = ? WHERE epoch = ? AND status = 'open'",
                         (moment, epoch))

    first_open = conn.execute(
        "SELECT t.origin, t.task_id, t.epoch FROM events e JOIN tasks t ON t.epoch = e.event_id "
        "WHERE e.session_id = ? AND e.kind = 'task' ORDER BY e.time LIMIT 1", (session,)).fetchone()
    key = tuple(first_open) if first_open else None
    if key is None and parent:
        stamp = spawn_row[1] if spawn_row else events[0][2]
        key = _task_key_for(conn, parent, stamp, (spawn or {}).get("task_epoch"))
    if key is not None:
        conn.execute(
            "UPDATE sessions SET task_origin = ?, task_id = ?, task_epoch = ? WHERE session_id = ? AND task_id IS NULL",
            (*key, session))
    session_key = conn.execute(
        "SELECT task_origin, task_id, task_epoch FROM sessions WHERE session_id = ?", (session,)).fetchone()
    session_key = tuple(session_key) if session_key and session_key[1] is not None else None

    if parent is None:
        _put_contour(conn, session, None, "root", session_key)
    elif spawn_row is None:
        _put_contour(conn, session, parent, role, session_key)
    for event_id, kind, moment, fields in events:
        if kind == "spawn":
            child_role = fields.get("role") if fields.get("role") in ("linked-child", "spawned-child") else "spawned-child"
            _put_contour(conn, event_id, session, child_role,
                         _task_key_for(conn, session, moment, fields.get("task_epoch")))
        elif kind == "judge":
            _put_contour(conn, event_id, session, "judge", _task_key_for(conn, session, moment, fields.get("task_epoch")))
    for contour_id in subagents:
        _put_contour(conn, contour_id, session, "agent-tool", session_key)
    for kind, text in artifacts:
        _put_artifact(conn, kind, text, session_key, events[-1][2])


def _put_artifact(conn, kind: str, text: str, key, moment: str) -> None:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    key = key or (None, None, None)
    conn.execute("INSERT OR IGNORE INTO artifacts (sha256, kind, task_origin, task_id, task_epoch, text, created_at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)", (digest, kind, *key, text, moment))


def materialize(sessions: "list[str]") -> dict:
    """Idempotently file the rows the replayed events of `sessions` imply, plus every contour
    still without a task (a task row the importer opened since may now be its home). Returns
    {committed, unattached}. The session's HEAD, dirty flag and harness version are captured
    only for a row that has none, so a later sync never rewrites them."""
    conn = store.open_record()
    if conn is None:
        return {"committed": False, "unattached": None}
    try:
        waiting = {r[0] for r in conn.execute(
            "SELECT DISTINCT parent_session FROM contours WHERE task_id IS NULL AND parent_session IS NOT NULL")}
        waiting |= {r[0] for r in conn.execute("SELECT contour_id FROM contours WHERE task_id IS NULL AND role = 'root'")}
        targets = sorted(set(sessions) | waiting)
        needing_head = {s for s in targets if not conn.execute(
            "SELECT core_head FROM sessions WHERE session_id = ? AND core_head IS NOT NULL", (s,)).fetchone()}
    finally:
        conn.close()
    head, dirty = _canon_state() if needing_head else (None, None)
    versions = {s: _harness_version(s) for s in needing_head} if head else {}
    extras = {s: (_subagent_contours(s), _artifact_texts(s)) for s in targets}

    def work(conn):
        origin = conn.execute("SELECT value FROM meta WHERE key = 'origin'").fetchone()[0]
        for session in targets:
            subagents, artifacts = extras[session]
            _materialize_session(conn, origin, session, subagents, artifacts)
            if head and session in needing_head:
                conn.execute(
                    "UPDATE sessions SET core_head = ?, dirty = ?, harness_version = COALESCE(harness_version, ?) "
                    "WHERE session_id = ? AND core_head IS NULL", (head, dirty, versions.get(session), session))
        return unattached_contour_count(conn)

    outcome = store.transact(work)
    return {"committed": outcome["committed"], "unattached": outcome["value"]}


# --- the sync -------------------------------------------------------------------------

def _prune(stem: str) -> None:
    try:
        (live.spool_dir() / (stem + ".lock")).unlink()
    except OSError:
        pass


def _replay_all(stems: "list[str]", budget_s: float) -> dict:
    """First pass one attempt per spool; then only the unsuccessful ones are retried with
    backoff (1 s doubling to 30 s) until each succeeds or the total wait reaches the budget."""
    results = {s: {"inserted": 0, "ok": False} for s in stems}

    def attempt(stem):
        outcome = store.replay(stem)
        results[stem]["inserted"] += outcome["inserted"]
        results[stem]["ok"] = (not outcome["deferred"]) and outcome["drained"] and not outcome["failed"]

    for stem in stems:
        attempt(stem)
    waited, wait = 0.0, BACKOFF_FIRST_S
    while waited + wait <= budget_s and not all(r["ok"] for r in results.values()):
        _sleep(wait)
        waited += wait
        wait = min(wait * 2, BACKOFF_MAX_S)
        for stem in [s for s in stems if not results[s]["ok"]]:
            attempt(stem)
    return results


def run(session: "str | None" = None, budget_s: "float | None" = None) -> dict:
    """One sync. `session` names the session whose SessionEnd started it (kind session_end);
    otherwise it is a sweep. Returns {kind, visited, committed, deferred, unattached}."""
    budget = BUDGET_S if budget_s is None else budget_s
    kind = "session_end" if session else "sweep"
    store.replay(SYNC_SESSION)
    begun = _now()
    _write_own(TRIGGER_START, {"kind": kind, **({"session": session} if session else {})}, begun)

    conn = store.open_record()
    stems = []
    if conn is not None:
        try:
            for stem in spool_stems():
                if stem == session or unreplayed_bytes(conn, stem) > 0:
                    stems.append(stem)
            for stem in spool_stems():
                if stem in stems or not is_eligible(stem, begun):
                    continue
                end, _, commit = latency_marks(conn, stem)
                if end is not None and end <= begun and commit is None:
                    stems.append(stem)
        finally:
            conn.close()
    if session and session not in stems:
        stems.append(session)
    stems = sorted(stems, key=lambda s: s != session)

    results = _replay_all(stems, budget)
    committed, deferred = [], []
    conn = store.open_record()
    try:
        for stem in stems:
            result = results[stem]
            if not result["ok"]:
                left = unreplayed_bytes(conn, stem) if conn is not None else 0
                _write_own(SYNC_DEFERRED, {"session": stem, "unreplayed_bytes": left})
                deferred.append(stem)
                continue
            if result["inserted"] > 0 or stem == session:
                write = True
            else:
                end, _, commit = latency_marks(conn, stem) if conn is not None else (None, None, None)
                write = end is not None and end <= begun and commit is None and is_eligible(stem, begun)
            if write:
                _write_own(SYNC_COMMIT, {"session": stem, "rows_inserted": result["inserted"]})
                committed.append(stem)
    finally:
        if conn is not None:
            conn.close()
    filed = materialize([s for s in stems if s not in deferred])
    for stem in stems:
        if stem not in deferred and is_eligible(stem):
            _prune(stem)
    return {"kind": kind, "visited": stems, "committed": committed, "deferred": deferred,
            "unattached": filed["unattached"]}
