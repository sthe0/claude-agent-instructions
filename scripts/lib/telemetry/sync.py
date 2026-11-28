"""The telemetry sync: delivers every spool to the record and files what the events say.

Difficulty removed: a session's events reach the record only once somebody replays its spool,
and a killed session has nobody. A sync does not wait for a session to end: it replays its own
spool and every other spool with unreplayed bytes, whatever its age (replay is exactly-once by
offset and event id, so replaying a live session early is safe). It then materializes, per
replayed session, the rows the events imply (session, task, contour, artifacts) and writes its own
work as events under the reserved source key `telemetry:sync`, through `store.insert_rows`, never
into a visited spool: `trigger.start` when it begins, `sync.commit` per spool it replayed,
`sync.deferred` past the retry budget, and `sync.end` as its last act (the trigger it answers and
the longest write transaction it held). A spool still deferred is then retried by a detached
process of its own (`run_retry`) until it commits, another sync replays it, or RETRY_CEILING_S
after the trigger, so it is not left to the next unrelated trigger. A row `insert_rows` could not place goes to the reserved
spool `telemetry:sync.jsonl`, which the next sync replays first; none is lost.

A spool is eligible (its session counts as ended) when it has an end marker, or has none and its
last append is older than LIVE_TTL_S; eligibility decides only prune and whether a drained spool
is considered for a zero-row commit. `latency_marks` is the one function computing a session's
eligibility time E, first trigger T1 and appearance A; it is what the latency check reads.

Hot-path rule: git, the harness version and transcripts are read here, never in `cli.main`.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lib import config_root
from lib.telemetry import cause, emit, live, store, tables

LATENCY_S = 600.0
# A deferred spool is retried until the latency bound of the session that started the sync, less
# the time its commit, materialize and token collection take once the write lock is free.
COMMIT_MARGIN_S = 60.0
BUDGET_S = LATENCY_S - COMMIT_MARGIN_S
BACKOFF_FIRST_S = 1.0
BACKOFF_MAX_S = 30.0
LIVE_TTL_S = live.LIVE_TTL_S
END_GRACE_S = 30.0
SYNC_SESSION = live.SYNC_SESSION
SOURCE_KEY = "telemetry:sync"
TRIGGER_START = "trigger.start"
SYNC_COMMIT = "sync.commit"
SYNC_DEFERRED = "sync.deferred"
SYNC_END = "sync.end"
RETRY_CEILING_S = 3600.0
RETRY_LOCK_SUFFIX = ".retry-lock"
WAITING_PER_SYNC = 50
CURSOR_KEY = "materialize_cursor"
# meta key prefix of a waiting session's last-tried mark (see materialize)
WAITING_MARK_PREFIX = "waiting_mark:"
HARNESS_VERSION_ENV = "AGENTCTL_HARNESS_VERSION"
CORE_ROOT = Path(__file__).resolve().parents[3]
GIT_TIMEOUT_S = 5.0
TRANSCRIPT_VERSION_LINES = 50

_sleep = time.sleep
_monotonic = time.monotonic


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


NO_TIME_ROWS = "no_time_rows"


def parse_time(text: "str | None") -> "datetime | None":
    """A stored event time as an aware UTC datetime; None for the empty time of an imported row
    whose source line carried no time."""
    if not text:
        return None
    return datetime.fromisoformat(text).astimezone(timezone.utc)


_UNTIMED_ROW_IDS = "_untimed_row_ids"


def known_times(texts) -> "list[datetime]":
    """The parsed times of `texts`, sorted, the empty ones left out."""
    return sorted(moment for moment in map(parse_time, texts) if moment is not None)


def tally_untimed(skipped: dict, event_ids) -> None:
    """Count each row with an empty time once in skipped[NO_TIME_ROWS], however many reads meet it."""
    seen = skipped.setdefault(_UNTIMED_ROW_IDS, set())
    seen.update(event_ids)
    skipped[NO_TIME_ROWS] = len(seen)


# --- the sync's own rows -------------------------------------------------------------

def _own_event(kind: str, fields: dict, moment: datetime) -> dict:
    stamp = _iso(moment)
    return {"event_id": emit.uuid7(), "session_id": SYNC_SESSION, "kind": kind,
            "source_key": SOURCE_KEY, "provenance": "live", "time": stamp,
            "fields": {**fields, "source_key": SOURCE_KEY, "time": stamp}}


def _keep_in_reserved_spool(row: dict) -> None:
    """Append `row` to the reserved spool with its own id and original time, as emit frames a line."""
    event = {"event_id": row["event_id"], "session_id": SYNC_SESSION, "kind": row["kind"],
             "time": row["time"], "provenance": "live", "backref": None,
             "fields": emit.stamp_writer_marker(row["fields"])}
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
    """Bytes of the spool file now at the stem's path that the record has not replayed. The stored
    offset counts only for the generation it was taken in (and while it fits the file), exactly
    as replay reads it: a spool recreated after a prune is unreplayed whole."""
    state = store.spool_state(stem)
    if state is None:
        return 0
    size, generation = state
    row = conn.execute("SELECT generation, byte_offset FROM spool_offsets WHERE spool = ?", (stem,)).fetchone()
    if row is None or row[0] != generation or row[1] > size:
        return size
    return size - row[1]


def is_eligible(stem: str, now: "datetime | None" = None) -> bool:
    """The session counts as ended: its end marker is current, or the spool's last append is
    older than LIVE_TTL_S. A marker is current while the spool has not been appended to more than
    END_GRACE_S after the marker's last write (the SessionEnd hooks themselves still record a
    decision event after it); a resumed session appends later, and its stale marker is ignored."""
    try:
        spooled = emit.spool_path(stem).stat().st_mtime
    except OSError:
        spooled = None
    try:
        marked = live.end_marker_path(stem).stat().st_mtime
    except OSError:
        marked = None
    if marked is not None and (spooled is None or spooled <= marked + END_GRACE_S):
        return True
    return spooled is not None and ((now or _now()).timestamp() - spooled) > LIVE_TTL_S


def prune_spool(stem: str, deadline_s: "float | None" = None) -> bool:
    """Remove an ended, fully replayed spool together with its offset row, end marker and epoch
    sidecar, in one step under the spool's replay lock; True when it was removed. Both
    conditions are re-read under the lock, so a session that resumed or appended since the caller
    looked keeps its spool, and no replay can interleave. An emitter that opened the file before
    the lock and writes after the unlink is not excluded (emitters take no lock); the window is
    one open-to-write interval of a session that is by the conditions idle. Lock files stay."""
    def work(conn):
        if unreplayed_bytes(conn, stem) > 0 or not is_eligible(stem):
            return False
        conn.execute("DELETE FROM spool_offsets WHERE spool = ?", (stem,))
        for path in (emit.spool_path(stem), live.end_marker_path(stem), live.epoch_path(stem)):
            try:
                path.unlink()
            except OSError:
                pass
        return True

    with store.spool_lock(stem, deadline_s) as held:
        if not held:
            return False
        outcome = store.transact(work, deadline_s)
    return bool(outcome["committed"] and outcome["value"])


# --- the latency rule -----------------------------------------------------------------

def _times(conn, sql: str, args: tuple, skipped: "dict | None" = None) -> "list[datetime]":
    """`sql` selects (event_id, time); the untimed rows are tallied in `skipped` when one is passed."""
    rows = conn.execute(sql, args).fetchall()
    if skipped is not None:
        tally_untimed(skipped, (event_id for event_id, moment in rows if not moment))
    return known_times(moment for _, moment in rows)


def trigger_start_times(conn, skipped: "dict | None" = None) -> "list[datetime]":
    """Every timed trigger.start, sorted: what latency_marks searches for T1."""
    return _times(conn, "SELECT event_id, time FROM events WHERE kind = ?", (TRIGGER_START,), skipped)


def latency_marks(conn, session_id: str, skipped: "dict | None" = None, trigger_starts: "list | None" = None
                  ) -> "tuple[datetime | None, datetime | None, datetime | None]":
    """(E, T1, A) of a session by the latency rule.

    E: the time of the session_end trigger.start that names the session, else its last recorded
    event time plus LIVE_TTL_S (the sync's own rows carry the sync session id, so they are
    not events of the session and never move E). T1: the first trigger.start, session_end or
    sweep, at or after E. A: the first sync.commit for the session at or after E. Rows with an
    empty time are left out and counted once each in skipped[NO_TIME_ROWS] when a tally is passed.
    A caller judging many sessions passes `trigger_starts` (trigger_start_times) read once."""
    ended = _times(
        conn, "SELECT event_id, time FROM events WHERE kind = ? AND json_extract(fields, '$.kind') = 'session_end' "
        "AND json_extract(fields, '$.session') = ?", (TRIGGER_START, session_id), skipped)
    if ended:
        end = ended[0]
    else:
        last = _times(conn, "SELECT event_id, time FROM events WHERE session_id = ?", (session_id,), skipped)
        if not last:
            return None, None, None
        end = last[-1] + timedelta(seconds=LIVE_TTL_S)
    starts = trigger_start_times(conn, skipped) if trigger_starts is None else trigger_starts
    first = next((t for t in starts if t >= end), None)
    commit = next((t for t in _times(
        conn, "SELECT event_id, time FROM events WHERE kind = ? AND json_extract(fields, '$.session') = ?",
        (SYNC_COMMIT, session_id), skipped) if t >= end), None)
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


def _task_opens(conn, session: str) -> "list[tuple[datetime, str]]":
    """(time, event id) of every event of `session` that opens a task."""
    return [(parse_time(r[1]), r[0]) for r in conn.execute(
        "SELECT event_id, time FROM events WHERE session_id = ? AND kind = 'task' "
        "AND json_extract(fields, '$.action') = 'open'", (session,))]


def _task_row(conn, epoch: str, task_keys=None):
    """(origin, task_id, epoch, weight_class) of the task row opened by `epoch`, or None: from
    `task_keys` (read before the write transaction, see _read_task_keys) when it holds the epoch,
    else by an indexed lookup on `conn`, which catches a row filed after that read."""
    if task_keys and epoch in task_keys:
        return task_keys[epoch]
    row = conn.execute("SELECT origin, task_id, epoch, weight_class FROM tasks WHERE epoch = ?", (epoch,)).fetchone()
    return tuple(row) if row else None


def _task_key_for(conn, session: str, moment: str, explicit_epoch: "str | None", opens=None, task_keys=None):
    """(origin, task_id, epoch) the event of `session` at `moment` belongs to: the epoch it
    names, else the session's task row whose opening event is the latest at or before the event,
    else the session row's own task key, else None. A caller attributing many events of one
    session passes `opens` (from _task_opens) read once, instead of a re-read per event, and
    `task_keys` (from _read_task_keys) so the task rows need no lookup inside its transaction."""
    if explicit_epoch:
        row = _task_row(conn, explicit_epoch, task_keys)
        if row:
            return row[:3]
    if opens is None:
        opens = _task_opens(conn, session)
    stamp = parse_time(moment)
    before = [(t, i) for t, i in opens if t is not None and stamp is not None and t <= stamp]
    if before:
        row = _task_row(conn, max(before)[1], task_keys)
        if row:
            return row[:3]
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
        opens = _task_opens(conn, session)
        for event_id, moment, raw in conn.execute(
                "SELECT event_id, time, fields FROM events WHERE session_id = ?", (session,)).fetchall():
            if _fields(raw).get("task_epoch") is None and _task_key_for(conn, session, moment, None, opens) == target:
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


def _session_host(session: str) -> "str | None":
    """The one runtime host the engine bound the session's root sessions to; None when the
    session has no engine state, never bound one, predates the stamps (a null `host_binds`
    entry) or ran on more than one host. A pre-stamp session stays NULL, as it reads unknown on
    the quality-row side. A current-schema session bound without a root session id has no binds
    either: the host-comparison report then groups its task by the stamps of its rows, which a
    sessions row does not have, so telemetry keeps NULL (unknown) rather than guess a host."""
    path = config_root.resolve_agentctl_state_file(session)
    if path is None:
        return None
    try:
        binds = json.loads(path.read_text(encoding="utf-8")).get("host_binds")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(binds, list) or not binds:
        return None
    hosts = {b.get("host") if isinstance(b, dict) else None for b in binds}
    return next(iter(hosts)) if len(hosts) == 1 and None not in hosts else None


def subagent_contours(session: str) -> "list[str]":
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

SESSION_UPSERT = (
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


def put_contour(conn, contour_id: str, parent: "str | None", role: str, key, task_keys=None) -> None:
    key = key or (None, None, None)
    weight = None
    if key[2]:
        row = _task_row(conn, key[2], task_keys)
        weight = row[3] if row else None
    conn.execute(_CONTOUR_UPSERT, (contour_id, parent, role, tables.validate("roles", role), weight, *key))


def _session_role(sess_event: "dict | None", spawn: "dict | None") -> str:
    if sess_event and sess_event.get("linked"):
        return "linked-child"
    if spawn is not None:
        role = spawn.get("role")
        return role if role in ("linked-child", "spawned-child") else "spawned-child"
    return "root"


def _session_events(conn, session: str) -> list:
    return [(r[0], r[1], r[2], _fields(r[3])) for r in conn.execute(
        "SELECT event_id, kind, time, fields FROM events WHERE session_id = ? ORDER BY time, event_id", (session,))]


def _first_open(conn, session: str):
    row = conn.execute(
        "SELECT t.origin, t.task_id, t.epoch FROM events e JOIN tasks t ON t.epoch = e.event_id "
        "WHERE e.session_id = ? AND e.kind = 'task' ORDER BY e.time LIMIT 1", (session,)).fetchone()
    return tuple(row) if row else None


_TASK_KEYS_CHUNK = 500


def _read_task_keys(conn, epochs) -> dict:
    """{epoch: (origin, task_id, epoch, weight_class)} of the task rows opened by `epochs`, read in
    chunks that stay under SQLite's bound-parameter limit."""
    wanted = sorted({e for e in epochs if e})
    found = {}
    for start in range(0, len(wanted), _TASK_KEYS_CHUNK):
        chunk = wanted[start:start + _TASK_KEYS_CHUNK]
        marks = ", ".join("?" * len(chunk))
        for row in conn.execute(
                f"SELECT origin, task_id, epoch, weight_class FROM tasks WHERE epoch IN ({marks})", chunk):
            found[row[2]] = tuple(row)
    return found


def _spawn_row(conn, session: str):
    """(parent session, time, fields) of the earliest spawn event naming `session` as its child."""
    return conn.execute(
        "SELECT session_id, time, fields FROM events WHERE kind = 'spawn' AND child_session_id = ? "
        "ORDER BY time LIMIT 1", (session,)).fetchone()


def _session_event(events: list) -> "dict | None":
    return next((f for _, kind, _, f in events if kind == "session"), None)


def _parent_of(sess_event: "dict | None", spawn_row) -> "str | None":
    return (sess_event or {}).get("parent_session") or (spawn_row[0] if spawn_row else None)


def _session_epoch(conn, session: str) -> "str | None":
    row = conn.execute("SELECT task_epoch FROM sessions WHERE session_id = ?", (session,)).fetchone()
    return row[0] if row else None


def _read_session(conn, session: str, outside: bool = False) -> dict:
    """What _materialize_session reads by scanning a session's events: the events themselves, its
    task opens and the first task row its events open.

    When read outside the write transaction (`outside`), the dict also holds `task_keys`: the task
    rows of every epoch the transaction may need. These are the epochs of the session's task opens
    and first open, the ones its spawn and judge events name, and those its session row already
    holds. A child session with no task open of its own takes its key from its parent, so for it
    the map also holds the epoch the spawn names and the parent's opens and session-row epoch, and
    the dict keeps the parent's opens (`parent`, `parent_opens`). The transaction then runs no
    tasks lookup unless an epoch it needs had no task row at the time of this read. Read inside the transaction, there is
    nothing to save by reading ahead, so the map is empty and each lookup runs where it is needed."""
    events = _session_events(conn, session)
    opens = _task_opens(conn, session)
    first_open = _first_open(conn, session)
    read = {"events": events, "opens": opens, "first_open": first_open, "task_keys": {}}
    if not outside or not events:
        return read
    epochs = [i for _, i in opens]
    epochs += [f.get("task_epoch") for _, kind, _, f in events if kind in ("spawn", "judge")]
    epochs.append(_session_epoch(conn, session))
    if first_open:
        # Usually one of `opens`; kept for a row the importer filed under another task event.
        epochs.append(first_open[2])
    else:
        spawn_row = _spawn_row(conn, session)
        parent = _parent_of(_session_event(events), spawn_row)
        if parent:
            parent_opens = _task_opens(conn, parent)
            epochs += [i for _, i in parent_opens]
            epochs.append(_session_epoch(conn, parent))
            if spawn_row:
                epochs.append(_fields(spawn_row[2]).get("task_epoch"))
            read.update(parent=parent, parent_opens=parent_opens)
    read["task_keys"] = _read_task_keys(conn, epochs)
    return read


def _read_outside(task_rows: bool, session: str):
    """A session's reads for one filing step, on a read-only connection of their own, so that no
    write transaction is open while they run: its events for _file_task_rows, _read_session's
    dict for _materialize_session. None when the record cannot be read; the step then reads
    inside its transaction."""
    conn = store.open_readonly()
    if conn is None:
        return None
    try:
        return _session_events(conn, session) if task_rows else _read_session(conn, session, outside=True)
    except Exception:
        return None
    finally:
        conn.close()


def _file_task_rows(conn, origin: str, session: str, events=None) -> None:
    """The task rows a session's events open, abandon, weigh and resolve. Run for every session of a
    sync before any session's contours, so a child filed in the same sync finds its parent's row.
    `events`: the session's events read before the transaction (read here when None)."""
    if events is None:
        events = _session_events(conn, session)
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


def _materialize_session(conn, origin: str, session: str, subagents: "list[str]", artifacts,
                         read: "dict | None" = None) -> None:
    """The session, contour, cause and artifact rows of one session. `read`: _read_session's
    result taken before the transaction, so only key lookups run inside it (read here when None)."""
    if read is None:
        read = _read_session(conn, session)
    events = read["events"]
    if not events:
        return
    sess_event = _session_event(events)
    spawn_row = _spawn_row(conn, session)
    spawn = _fields(spawn_row[2]) if spawn_row else None
    parent = _parent_of(sess_event, spawn_row)
    role = _session_role(sess_event, spawn)
    depth = (sess_event or {}).get("depth")
    if depth is None:
        depth = 1 if parent else 0
    conn.execute(SESSION_UPSERT, (session, origin, parent, role, tables.validate("roles", role), depth, events[0][2]))

    task_keys = read.get("task_keys") or {}
    key = read["first_open"]
    if key is None and parent:
        stamp = spawn_row[1] if spawn_row else events[0][2]
        parent_opens = read.get("parent_opens") if read.get("parent") == parent else None
        key = _task_key_for(conn, parent, stamp, (spawn or {}).get("task_epoch"), parent_opens, task_keys)
    if key is not None:
        conn.execute(
            "UPDATE sessions SET task_origin = ?, task_id = ?, task_epoch = ? WHERE session_id = ? AND task_id IS NULL",
            (*key, session))
    session_key = conn.execute(
        "SELECT task_origin, task_id, task_epoch FROM sessions WHERE session_id = ?", (session,)).fetchone()
    session_key = tuple(session_key) if session_key and session_key[1] is not None else None

    if parent is None:
        put_contour(conn, session, None, "root", session_key, task_keys)
    elif spawn_row is None:
        put_contour(conn, session, parent, role, session_key, task_keys)
    opens = read["opens"]
    for event_id, kind, moment, fields in events:
        if kind not in ("spawn", "judge"):
            continue
        key = _task_key_for(conn, session, moment, fields.get("task_epoch"), opens, task_keys)
        if kind == "spawn":
            child_role = fields.get("role") if fields.get("role") in ("linked-child", "spawned-child") else "spawned-child"
            put_contour(conn, event_id, session, child_role, key, task_keys)
        else:
            put_contour(conn, event_id, session, "judge", key, task_keys)
    _file_causes(conn, origin, session, events)
    for contour_id in subagents:
        put_contour(conn, contour_id, session, "agent-tool", session_key, task_keys)
    for kind, text in artifacts:
        _put_artifact(conn, kind, text, session_key, events[-1][2])


_CAUSE_INSERT = (
    "INSERT OR IGNORE INTO causes (cause_id, session, difficulty_index, cause_category, cause_category_table_version, "
    "machinery_induced, mechanism, mechanism_table_version, functional_place, functional_place_table_version, "
    "cause_note, origin) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")


def _file_causes(conn, origin: str, session: str, events: list) -> None:
    """One causes row per critique event that carries a structured cause, keyed by the event's id.
    The table versions are the ones the event was stamped with: a member retired since stays valid."""
    for event_id, kind, _, fields in events:
        if kind != "critique" or not all(fields.get(k) is not None for k in cause.CAUSE_FIELDS):
            continue
        versions = [fields.get(column) for _, column in cause.VERSION_FIELDS.values()]
        if not all(isinstance(v, int) for v in versions):
            continue
        conn.execute(_CAUSE_INSERT, (
            event_id, session, fields.get("difficulty_index"), fields["cause_category"], versions[0],
            1 if fields["machinery_induced"] else 0, fields["mechanism"], versions[1], fields["functional_place"],
            versions[2], fields.get(cause.NOTE_FIELD), origin))


def _put_artifact(conn, kind: str, text: str, key, moment: str) -> None:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    key = key or (None, None, None)
    conn.execute("INSERT OR IGNORE INTO artifacts (sha256, kind, task_origin, task_id, task_epoch, text, created_at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)", (digest, kind, *key, text, moment))


def _unfiled(conn) -> "list[str]":
    """Sessions whose spool was replayed and that have events but no sessions row yet: a sync
    that stopped between replay and filing left them behind."""
    return sorted(r[0] for r in conn.execute(
        "SELECT spool FROM spool_offsets o WHERE spool != ? "
        "AND NOT EXISTS (SELECT 1 FROM sessions s WHERE s.session_id = o.spool) "
        "AND EXISTS (SELECT 1 FROM events e WHERE e.session_id = o.spool)", (SYNC_SESSION,)))


def _waiting(conn) -> "list[str]":
    """Sessions owning a contour still without a task: the parent of such a contour, or the
    root contour itself. A task row the importer opened since may now be its home."""
    waiting = {r[0] for r in conn.execute(
        "SELECT DISTINCT parent_session FROM contours WHERE task_id IS NULL AND parent_session IS NOT NULL")}
    waiting |= {r[0] for r in conn.execute("SELECT contour_id FROM contours WHERE task_id IS NULL AND role = 'root'")}
    return sorted(waiting)


def _parents(conn, session: str) -> "set[str]":
    """The session that spawned `session`, as its own session event or a spawn event names it."""
    found = {r[0] for r in conn.execute(
        "SELECT session_id FROM events WHERE kind = 'spawn' AND child_session_id = ?", (session,))}
    for (raw,) in conn.execute("SELECT fields FROM events WHERE session_id = ? AND kind = 'session'", (session,)):
        parent = _fields(raw).get("parent_session")
        if parent:
            found.add(parent)
    return found


def _rotation(pool: "list[str]", cursor: "str | None", cap: int) -> "list[str]":
    """The first `cap` sessions of the sorted `pool`, starting after `cursor` and wrapping."""
    after = [s for s in pool if cursor is None or s > cursor]
    before = [s for s in pool if cursor is not None and s <= cursor]
    return (after + before)[:cap]


def _record_marks(conn) -> "tuple[int, int]":
    """The parts of a waiting mark shared by every session: the record's highest tasks rowid and
    the count of sessions rows whose task key is set."""
    tasks = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM tasks").fetchone()[0]
    keyed = conn.execute("SELECT COUNT(*) FROM sessions WHERE task_id IS NOT NULL").fetchone()[0]
    return tasks, keyed


def _waiting_mark(conn, session: str, shared: "tuple[int, int]") -> "list[int]":
    """[the session's highest event rowid, the highest rowid of an event naming it as the child
    (a spawn in its parent's session), *shared]: what resolving its task reads, as counters."""
    events = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM events WHERE session_id = ?", (session,)).fetchone()[0]
    spawns = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM events WHERE child_session_id = ?",
                          (session,)).fetchone()[0]
    return [events, spawns, *shared]


def _stored_marks(conn) -> "dict[str, list]":
    marks = {}
    for key, value in conn.execute("SELECT key, value FROM meta WHERE substr(key, 1, ?) = ?",
                                   (len(WAITING_MARK_PREFIX), WAITING_MARK_PREFIX)):
        try:
            marks[key[len(WAITING_MARK_PREFIX):]] = json.loads(value)
        except ValueError:
            continue
    return marks


def _unchanged(mark: "list[int]", stored) -> bool:
    """None of the four parts of a waiting session's mark has grown since its last try."""
    if not isinstance(stored, list) or len(stored) != 4:
        return False
    events, spawns, tasks, keyed = mark
    return events <= stored[0] and spawns <= stored[1] and tasks <= stored[2] and keyed <= stored[3]


def _put_mark(conn, session: str, mark: "list[int]") -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                 (WAITING_MARK_PREFIX + session, json.dumps(mark)))


def materialize(sessions: "list[str]") -> dict:
    """Idempotently file the rows the replayed events of `sessions` imply, plus a bounded share of
    the sessions still waiting. Returns {committed, unattached} and, when a write did not commit,
    the session it stopped at as `failed_session`.

    The write lock stays short at any record size: each session is filed in its own transaction
    (first every target's task rows, so a child finds its parent's row, then every target's
    session and contour rows), and a call takes the replayed sessions and their parents, then at
    most WAITING_PER_SYNC others. Those come first from the unfiled sessions (replayed, with
    events, no sessions row), then in rotation from the waiting ones (owning a contour without a
    task), starting after the session id kept in meta under CURSOR_KEY and wrapping; the cursor is
    advanced before filing, so a session that fails is passed over, not retried at every sync.
    Every waiting session is thus visited within ceil(waiting / WAITING_PER_SYNC) syncs. The
    session's HEAD, dirty flag and harness version are captured only for a row that has none, so
    a later sync never rewrites them.

    No write transaction is open while a session's events are read: each filing step reads them
    on a read-only connection first (_read_outside), and BEGIN IMMEDIATE wraps only that
    session's upserts and their key lookups. A waiting session is passed over, without counting
    against the cap, while nothing its resolution reads has grown since its last try: its mark
    (its highest event rowid, the highest rowid of a spawn event naming it, the record's highest
    tasks rowid, the count of sessions rows with a task key; taken before the read) is kept in
    meta under WAITING_MARK_PREFIX + its id and written in the transaction that files it. The last
    two parts are record-wide, so any new task or newly keyed session re-arms every waiting
    session: the skip saves work only while the record is quiet and does not bound a call's work
    (the cap does)."""
    conn = store.open_record()
    if conn is None:
        return {"committed": False, "unattached": None}
    try:
        forced = set(sessions)
        for session in sessions:
            forced |= _parents(conn, session)
        forced.discard(SYNC_SESSION)
        unfiled = [s for s in _unfiled(conn) if s not in forced][:WAITING_PER_SYNC]
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (CURSOR_KEY,)).fetchone()
        pool = [s for s in _waiting(conn) if s not in forced and s not in unfiled]
        shared, stored, marks, rotated = _record_marks(conn), _stored_marks(conn), {}, []
        for session in _rotation(pool, row[0] if row else None, len(pool)):
            if len(rotated) >= WAITING_PER_SYNC - len(unfiled):
                break
            mark = _waiting_mark(conn, session, shared)
            if not _unchanged(mark, stored.get(session)):
                rotated.append(session)
                marks[session] = mark
        targets = sorted(forced) + unfiled + rotated
        needing_head = {s for s in targets if not conn.execute(
            "SELECT core_head FROM sessions WHERE session_id = ? AND core_head IS NOT NULL", (s,)).fetchone()}
    finally:
        conn.close()
    head, dirty = _canon_state() if needing_head else (None, None)
    versions = {s: _harness_version(s) for s in needing_head} if head else {}
    hosts = {s: _session_host(s) for s in targets}
    extras = {s: (subagent_contours(s), _artifact_texts(s)) for s in targets}

    def advance(conn):
        if rotated:
            conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET "
                         "value = excluded.value", (CURSOR_KEY, rotated[-1]))
        return conn.execute("SELECT value FROM meta WHERE key = 'origin'").fetchone()[0]

    started = store.transact(advance)
    if not started["committed"]:
        return {"committed": False, "unattached": None}
    origin = started["value"]

    reads = {}

    def file_rows(conn, session):
        _file_task_rows(conn, origin, session, reads.pop(session, None))

    def file_session(conn, session):
        subagents, artifacts = extras[session]
        _materialize_session(conn, origin, session, subagents, artifacts, reads.pop(session, None))
        if session in marks:
            _put_mark(conn, session, marks[session])
        if hosts.get(session):
            conn.execute("UPDATE sessions SET host = COALESCE(host, ?) WHERE session_id = ?",
                         (hosts[session], session))
        if head:
            conn.execute(
                "UPDATE sessions SET core_head = ?, dirty = ?, harness_version = COALESCE(harness_version, ?) "
                "WHERE session_id = ? AND core_head IS NULL", (head, dirty, versions.get(session), session))

    for step in (file_rows, file_session):
        for session in targets:
            reads[session] = _read_outside(step is file_rows, session)
            outcome = store.transact(lambda conn, s=session: step(conn, s))
            if not outcome["committed"]:
                return {"committed": False, "unattached": None, "failed_session": session}
    conn = store.open_record()
    if conn is None:
        return {"committed": True, "unattached": None}
    try:
        return {"committed": True, "unattached": unattached_contour_count(conn)}
    finally:
        conn.close()


# --- the sync -------------------------------------------------------------------------

def _replay_all(stems: "list[str]", deadline: float, on_replayed) -> dict:
    """First pass one attempt per spool; then only the unsuccessful ones are retried with
    backoff (1 s doubling to 30 s) until each succeeds or the next wait would end past the
    deadline, a `_monotonic` time, so the attempts count against it as well as the sleeps.
    `on_replayed(stem, result)` runs the moment a spool replays, so its commit never waits out
    the retries of another spool."""
    results = {s: {"inserted": 0, "ok": False} for s in stems}

    def attempt(stem):
        outcome = store.replay(stem)
        results[stem]["inserted"] += outcome["inserted"]
        results[stem]["ok"] = (not outcome["deferred"]) and outcome["drained"] and not outcome["failed"]
        if results[stem]["ok"]:
            on_replayed(stem, results[stem])

    for stem in stems:
        attempt(stem)
    wait = BACKOFF_FIRST_S
    while _monotonic() + wait <= deadline and not all(r["ok"] for r in results.values()):
        _sleep(wait)
        wait = min(wait * 2, BACKOFF_MAX_S)
        for stem in [s for s in stems if not results[s]["ok"]]:
            attempt(stem)
    return results


def run(session: "str | None" = None, budget_s: "float | None" = None) -> dict:
    """One sync. `session` names the session whose SessionEnd started it (kind session_end);
    otherwise it is a sweep. Returns {kind, visited, committed, deferred, unattached}."""
    deadline = _monotonic() + (BUDGET_S if budget_s is None else budget_s)
    kind = "session_end" if session else "sweep"
    store.reset_lock_hold()
    store.replay(SYNC_SESSION)
    begun = _now()
    _write_own(TRIGGER_START, {"kind": kind, **({"session": session} if session else {})}, begun)

    conn = store.open_record()
    stems = []
    skipped = {NO_TIME_ROWS: 0}
    if conn is not None:
        try:
            for stem in spool_stems():
                if stem == session or unreplayed_bytes(conn, stem) > 0:
                    stems.append(stem)
            for stem in spool_stems():
                if stem in stems or not is_eligible(stem, begun):
                    continue
                end, _, commit = latency_marks(conn, stem, skipped)
                if end is not None and end <= begun and commit is None:
                    stems.append(stem)
        finally:
            conn.close()
    if session and session not in stems:
        stems.append(session)
    stems = sorted(stems, key=lambda s: s != session)

    committed, deferred, retried = [], [], []

    def commit_replayed(stem, result):
        if _commit_due(stem, result["inserted"], begun, session, skipped):
            _write_own(SYNC_COMMIT, {"session": stem, "rows_inserted": result["inserted"]})
            committed.append(stem)

    try:
        results = _replay_all(stems, deadline, commit_replayed)
        unreplayed = [stem for stem in stems if not results[stem]["ok"]]
        if unreplayed:
            conn = store.open_record()
            try:
                for stem in unreplayed:
                    left = unreplayed_bytes(conn, stem) if conn is not None else 0
                    _write_own(SYNC_DEFERRED, {"session": stem, "unreplayed_bytes": left})
                    deferred.append(stem)
            finally:
                if conn is not None:
                    conn.close()
        filed = _materialize_recording_failure([s for s in stems if s not in deferred])
        from lib.telemetry import tokens
        collected = tokens.collect([s for s in stems if s != SYNC_SESSION])
        for stem in deferred:
            _spawn_retry(stem, begun, session)
            retried.append(stem)
    finally:
        _write_sync_end(begun, kind, committed=len(committed), deferred=len(deferred), retries=len(retried))
    return {"kind": kind, "visited": stems, "committed": committed, "deferred": deferred, "retried": retried,
            "unattached": filed["unattached"], "tokens": collected, NO_TIME_ROWS: skipped[NO_TIME_ROWS]}


def _materialize_recording_failure(sessions: "list[str]") -> dict:
    """materialize(sessions); a write that did not commit leaves a sync.deferred row naming the
    session it stopped at, phase materialize, so the gap is visible before a later rotation."""
    filed = materialize(sessions)
    if filed.get("failed_session"):
        _write_own(SYNC_DEFERRED, {"session": filed["failed_session"], "phase": "materialize"})
    return filed


def _commit_due(stem: str, inserted: int, begun: datetime, session: "str | None", skipped: dict) -> bool:
    """Whether a replayed spool gets its sync.commit row (the A mark): it inserted rows, or it is
    the session that started the sync, or it ended at or before `begun`, is eligible and has no
    commit yet (a drained spool of an ended session that nobody committed)."""
    if inserted > 0 or stem == session:
        return True
    conn = store.open_record()
    try:
        end, _, commit = latency_marks(conn, stem, skipped) if conn is not None else (None, None, None)
    finally:
        if conn is not None:
            conn.close()
    return end is not None and end <= begun and commit is None and is_eligible(stem, begun)


def _write_sync_end(begun: datetime, kind: str, **counts) -> None:
    """The sync's last act: one sync.end row carrying the trigger start it answers and the longest
    write transaction this sync held (store.reset_lock_hold() at its start), measured before this
    row's own write. The hold names this sync's own in-transaction work only: each BEGIN IMMEDIATE
    is timed from the moment it returns, so the wait a queued BEGIN spends on another writer's lock
    stays outside it. The acceptance bound (5000 ms) judges that work, and
    store.WAL_AUTOCHECKPOINT_PAGES bounds a COMMIT's checkpoint pass — on the live record the
    dominant such work — so it cannot stretch a hold past the bound by size alone."""
    fields = {"kind": kind, "trigger_start": _iso(begun), "max_lock_hold_ms": store.max_lock_hold_ms(), **counts}
    _write_own(SYNC_END, fields)


# --- the retry of a spool still deferred after the budget -------------------------------

def _popen_detached(args: "list[str]") -> None:
    """Start `args` in its own session with every descriptor closed, so the child inherits no lock
    (a sweep's SWEEP_LOCK in particular, which would block every later sweep for the retry's life)."""
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True, cwd=str(CORE_ROOT / "scripts"))


def _spawn_retry(stem: str, begun: datetime, session: "str | None") -> None:
    args = [sys.executable, str(CORE_ROOT / "scripts" / "telemetry.py"), "sync", "--retry-deferred", stem,
            "--parent-begun", _iso(begun)]
    if session:
        args += ["--parent-session", session]
    try:
        _popen_detached(args)
    except OSError:
        pass


def _retry_lock_path(stem: str) -> Path:
    return live.spool_dir() / (stem + RETRY_LOCK_SUFFIX)


def _replayed_elsewhere(stem: str) -> bool:
    conn = store.open_record()
    if conn is None:
        return False
    try:
        return unreplayed_bytes(conn, stem) == 0
    finally:
        conn.close()


def run_retry(stem: str, parent_begun: str, parent_session: "str | None" = None) -> dict:
    """Retry one spool a sync left deferred, in a process of its own. It answers the parent's
    trigger: T (`parent_begun`) is its begun time and S its session, so it writes no trigger.start
    (that row would become T1 of sessions it never replays). One retrier per stem: without the
    stem's retry lock it exits at once and writes nothing. Holding it, it retries with backoff
    capped at BACKOFF_MAX_S until the spool replays (sync.commit under the parent's rule, then the
    stem's materialize), until another sync replayed it (no bytes left), or until RETRY_CEILING_S
    after T; it then writes its own sync.end carrying T."""
    begun = parse_time(parent_begun)
    if begun is None:
        return {"kind": "retry", "stem": stem, "outcome": "bad_parent_begun"}
    try:
        emit.ensure_private_dir(live.spool_dir())
        fd = os.open(_retry_lock_path(stem), os.O_RDWR | os.O_CREAT, emit.PRIVATE_FILE_MODE)
    except OSError:
        return {"kind": "retry", "stem": stem, "outcome": "no_lock_file"}
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {"kind": "retry", "stem": stem, "outcome": "held_elsewhere"}
        return _retry_holding_lock(stem, begun, parent_session)
    finally:
        os.close(fd)


def _retry_holding_lock(stem: str, begun: datetime, session: "str | None") -> dict:
    store.reset_lock_hold()
    skipped = {NO_TIME_ROWS: 0}
    ceiling = begun + timedelta(seconds=RETRY_CEILING_S)
    outcome, inserted, attempts, committed = "ceiling", 0, 0, False
    wait = BACKOFF_FIRST_S
    try:
        while True:
            attempts += 1
            replayed = store.replay(stem)
            inserted += replayed["inserted"]
            if not replayed["deferred"] and replayed["drained"] and not replayed["failed"]:
                if _commit_due(stem, inserted, begun, session, skipped):
                    _write_own(SYNC_COMMIT, {"session": stem, "rows_inserted": inserted, "retry": True})
                    committed = True
                _materialize_recording_failure([stem])
                outcome = "replayed"
                break
            if _replayed_elsewhere(stem):
                outcome = "replayed_elsewhere"
                break
            if _now() + timedelta(seconds=wait) > ceiling:
                break
            _sleep(wait)
            wait = min(wait * 2, BACKOFF_MAX_S)
    finally:
        _write_sync_end(begun, "retry", committed=int(committed), attempts=attempts)
    return {"kind": "retry", "stem": stem, "outcome": outcome, "attempts": attempts, "committed": committed,
            "inserted": inserted}
