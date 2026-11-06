"""Import the history that predates live emission into the telemetry record, and keep it rebuildable.

Difficulty removed: the record only holds what was emitted after live emission began, while the
agent's whole past lives in file ledgers (gate log, decision log, judge ledger, spawn-cost ledger,
quality ledger), engine state files and transcripts. This module reads them into the same tables
under provenance `imported`, never twice and never in competition with a live row:

- every source line becomes one row whose id is `store.imported_row_id(path, line offset, sha256 of
  the line with its newline)`, so a second import adds nothing and a live event that names the line
  in its back-reference supersedes it (the live-wins rule; replay applies the other half);
- each (source file, consumer) pair has its own offset in `import_offsets`, keyed by the sha256 of
  the file's first line (not its inode); a resumed read checks the bytes before the offset are the
  ones it read, and a shortened or rewritten tail is read again from zero;
- files are streamed a bounded batch at a time, each batch one transaction with its offset;
- a batch the record is too busy to take is retried with backoff within the run's budget, else the
  offset stays and the run reports itself incomplete;
- a session that has live events (a record row, or a spool/end marker) gets no imported history events
  or transcript events; a live root that started before live emission keeps the identity (task rows,
  key, contour) imported from its engine history and counts its early acts as a loss;
- child sessions never open task rows: they attach to their parent's task row.

`rebuild` moves a corrupt record aside, replays every spool and imports every source again (the
origin id is kept in the record's side file), and reports the sessions whose live-only events could
not come back. `prune` drops sync and decision rows past the retention window, keeping the rows the
latency rule reads, and never touches the per-task aggregate tables.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import resource
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lib import config_root, judge_ledger
from lib.telemetry import emit, live, phases, store, sync, tables

BATCH_LINES = 1000
BACKOFF_FIRST_S = 1.0
BACKOFF_MAX_S = 30.0
BUDGET_S = 300.0
BATCH_DEADLINE_S = 5.0
FIRST_LINE_MAX = 1 << 16
IN_CLAUSE_MAX = 500
SELF_SESSION = "telemetry:importer"
SELF_SOURCE_KEY = "telemetry:importer"
RETENTION_KEY = "telemetry-prune-retention-days"
COUNTED_MODES = frozenset({
    "torn_event", "partial_session_before_first_live_event", "record_corrupt_rebuilt", "source_truncated",
    "malformed_source_line", "sync_deferred"})
OPENING_VERBS = ("cli:start", "cli:reset")

_clock = time.monotonic
_sleep = time.sleep
_BASH_EXIT = re.compile(r"Exit code (\d+)")


def _tp():
    from lib import time_profile
    return time_profile


def quality_ledger() -> Path:
    return Path.home() / ".local" / "log" / "claude-task-quality.jsonl"


def spawn_ledger() -> Path:
    return Path.home() / ".local" / "log" / "claude-spawn-costs.jsonl"


def _commit_index() -> list:
    return _tp().load_commit_index(sync.CORE_ROOT)


def _iso(value) -> str:
    ms = _tp()._epoch_or_iso_ms(value)
    return "" if ms is None else datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds")


def _canonical(entry) -> bytes:
    return json.dumps(entry, sort_keys=True, separators=(",", ":")).encode("utf-8")


class _Stats:
    def __init__(self):
        self.rows_added = 0
        self.per_source = {}
        self.errors = 0
        self.malformed_lines = 0
        self.complete = True

    def source(self, name: str) -> dict:
        return self.per_source.setdefault(name, {"rows": 0, "rereads": 0, "truncated_bytes": 0})

    def fail(self) -> None:
        self.errors += 1
        self.complete = False

    def defer(self) -> None:
        self.complete = False


class _Run:
    def __init__(self, origin: str, ro):
        self.origin = origin
        self.ro = ro
        self.stats = _Stats()
        self.deadline = _clock() + BUDGET_S
        self.live_sessions = set()
        self.commit_index = None

    def exhausted(self, delay: float) -> bool:
        return _clock() + delay > self.deadline

    def stored_offset(self, source: str, generation: str, consumer: str):
        return self.ro.execute(
            "SELECT byte_offset, last_start, last_sha256 FROM import_offsets "
            "WHERE source = ? AND generation = ? AND consumer = ?", (source, generation, consumer)).fetchone()

    def is_live(self, session: str) -> bool:
        return session in self.live_sessions or live.spool_or_end_marker_exists(session)

    def refresh_live(self) -> None:
        self.live_sessions = {r[0] for r in self.ro.execute("SELECT DISTINCT session_id FROM events WHERE provenance = 'live'")}

    def head_at(self, ms) -> "str | None":
        if self.commit_index is None:
            self.commit_index = _commit_index()
        return None if ms is None else _tp()._sha_at(self.commit_index, ms)


def _commit(run: _Run, work) -> dict:
    """One write transaction, retried with backoff while the record is busy and the budget lasts."""
    delay = BACKOFF_FIRST_S
    while True:
        out = store.transact(work, deadline_s=BATCH_DEADLINE_S)
        if out["committed"] or out["failed"]:
            return out
        if run.exhausted(delay):
            return out
        _sleep(delay)
        delay = min(delay * 2, BACKOFF_MAX_S)


def _settle(run: _Run, out: dict) -> bool:
    """True when the transaction committed; otherwise the run's books say why."""
    if out["committed"]:
        return True
    if out["deferred"]:
        run.stats.defer()
    else:
        run.stats.fail()
    return False


# --- source lines -------------------------------------------------------------------------

class _Line:
    __slots__ = ("start", "raw", "sha", "obj", "event_id")

    def __init__(self, start: int, raw: bytes, obj, event_id: str):
        self.start = start
        self.raw = raw
        self.sha = hashlib.sha256(raw).hexdigest()
        self.obj = obj
        self.event_id = event_id


def _parse_object(raw: bytes):
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _chunks(items, size=IN_CLAUSE_MAX):
    for index in range(0, len(items), size):
        yield items[index:index + size]


def _live_backrefs(conn, ids: list) -> set:
    """Of the imported ids, those a live event names in its back-reference."""
    found = set()
    for chunk in _chunks(ids):
        marks = ",".join("?" for _ in chunk)
        found.update(r[0] for r in conn.execute(
            f"SELECT backref_id FROM events WHERE provenance = 'live' AND backref_id IN ({marks})", chunk))
    return found


def _child_is_live(conn, run: _Run, child) -> bool:
    if not child:
        return False
    if run.is_live(child):
        return True
    return conn.execute("SELECT 1 FROM events WHERE kind = 'spawn' AND provenance = 'live' "
                        "AND child_session_id = ? LIMIT 1", (child,)).fetchone() is not None


_EVENT_INSERT = (
    "INSERT OR IGNORE INTO events (event_id, session_id, kind, source_key, provenance, time, backref_id, "
    "child_session_id, fields) VALUES (?, ?, ?, ?, 'imported', ?, ?, ?, ?)")


def _put_event(conn, event_id: str, session: str, kind: str, source_key: str, moment: str, fields: dict,
               child=None) -> int:
    body = dict(fields)
    body["source_key"] = source_key
    return conn.execute(_EVENT_INSERT, (event_id, session, kind, source_key, moment, None, child,
                                        json.dumps(body, separators=(",", ":")))).rowcount


def _backref(line: _Line, path: str) -> dict:
    return {"path": path, "offset": line.start, "sha256": line.sha}


def _gate_part(obj: dict):
    gate, session = obj.get("gate"), obj.get("session")
    if not isinstance(gate, str) or not isinstance(session, str):
        return None
    moment = _iso(obj.get("ts"))
    return {"session": session, "kind": "gate", "source_key": f"gate:{gate}", "time": moment,
            "fields": {"session": session, "verb": None, "gate": gate,
                       "outcome": "pass" if obj.get("passed") else "refuse", "time": moment}}


def _decision_part(obj: dict):
    mechanism, session = obj.get("mechanism"), obj.get("session")
    if not isinstance(mechanism, str) or not isinstance(session, str):
        return None
    return {"session": session, "kind": "decision", "source_key": f"hook:{mechanism}", "time": _iso(obj.get("ts")),
            "fields": {"mechanism": mechanism, "event": obj.get("event"), "decision": obj.get("decision"),
                       "layer": obj.get("layer"), "test": obj.get("test")}}


def _judge_part(obj: dict):
    session, judge = obj.get("source"), obj.get("judge")
    if obj.get("kind") != "call" or not isinstance(session, str) or not isinstance(judge, str):
        return None
    return {"session": session, "kind": "judge", "source_key": f"judge:{judge}", "time": _iso(obj.get("ts")),
            "fields": {"judge": judge, "timed_out": bool(obj.get("timed_out")), "duration": obj.get("duration"),
                       "returncode": obj.get("returncode"), "prompt_chars": obj.get("prompt_chars"),
                       "invocation_id": obj.get("invocation_id")}}


def _spawn_part(obj: dict):
    child = obj.get("child_session_id")
    if obj.get("event") != "spawn" or not isinstance(child, str) or not child:
        return None
    spawner = obj.get("spawner_session")
    return {"session": spawner if isinstance(spawner, str) and spawner else "import:spawn", "kind": "spawn_cost",
            "source_key": "spawn", "time": _iso(obj.get("ts")), "child": child, "spawner": spawner,
            "fields": {"role": "spawned-child", "kind": obj.get("kind"), "depth": obj.get("depth"),
                       "stage_index": obj.get("stage_index"), "cost_usd": obj.get("cost_usd"),
                       "duration_ms": obj.get("duration_ms"), "return_marker": obj.get("return_marker"),
                       "child_session_id": child}}


def _event_consumer(build):
    def handle(conn, run, unit, lines):
        parts = []
        for line in lines:
            part = build(line.obj) if line.obj is not None else None
            if part is not None:
                parts.append((line, part))
        live_ids = _live_backrefs(conn, [line.event_id for line, _ in parts])
        added = 0
        for line, part in parts:
            if line.event_id in live_ids or (part["kind"] == "spawn_cost" and _child_is_live(conn, run, part["child"])):
                continue
            fields = dict(part["fields"])
            fields["backref"] = _backref(line, unit.path)
            added += _put_event(conn, line.event_id, part["session"], part["kind"], part["source_key"], part["time"],
                                fields, part.get("child"))
        return added
    return handle


def _contour_consumer(build, role: str, parent_of):
    def handle(conn, run, unit, lines):
        parts = []
        for line in lines:
            part = build(line.obj) if line.obj is not None else None
            if part is not None:
                parts.append((line, part))
        live_ids = _live_backrefs(conn, [line.event_id for line, _ in parts])
        for line, part in parts:
            if line.event_id in live_ids or _child_is_live(conn, run, part.get("child")):
                continue
            parent = parent_of(part)
            if part["kind"] == "spawn_cost":
                conn.execute(sync._SESSION_UPSERT, (part["child"], run.origin, parent, role,
                                                    tables.validate("roles", role), 1, part["time"] or None))
            sync._put_contour(conn, line.event_id, parent, role, None)
        return 0
    return handle


def _quality_consumer(conn, run, unit, lines):
    added = 0
    for line in lines:
        obj = line.obj
        session, head = (obj or {}).get("session"), (obj or {}).get("instructions_head")
        if not isinstance(session, str) or not isinstance(head, str) or not head:
            continue
        added += conn.execute(
            "INSERT INTO sessions (session_id, origin, core_head) VALUES (?, ?, ?) ON CONFLICT (session_id) DO UPDATE "
            "SET core_head = CASE WHEN sessions.core_head IS NULL OR (SELECT derived FROM session_head_marks "
            "WHERE session_id = sessions.session_id) = 1 THEN excluded.core_head ELSE sessions.core_head END",
            (session, run.origin, head)).rowcount
        conn.execute("INSERT INTO session_head_marks (session_id, derived) VALUES (?, 0) "
                     "ON CONFLICT (session_id) DO UPDATE SET derived = 0", (session,))
    return added


class _Unit:
    """One consumer of one source file: its own offset, its handler, its fallback session."""

    def __init__(self, source, consumer, path, handler, fallback_session, primary=True):
        self.source = source
        self.consumer = consumer
        self.path = str(path)
        self.handler = handler
        self.fallback_session = fallback_session
        self.primary = primary


def _file_units() -> list:
    gate, decision = config_root.agentctl_gate_log(), config_root.decision_log()
    judge, spawn, quality = judge_ledger.ledger_path(), spawn_ledger(), quality_ledger()
    return [
        _Unit("gate", "events", gate, _event_consumer(_gate_part), "import:gate"),
        _Unit("decision", "events", decision, _event_consumer(_decision_part), "import:decision"),
        _Unit("judge", "events", judge, _event_consumer(_judge_part), "import:judge"),
        _Unit("judge", "contours", judge, _contour_consumer(_judge_part, "judge", lambda part: part["session"]),
              "import:judge", primary=False),
        _Unit("spawn", "events", spawn, _event_consumer(_spawn_part), "import:spawn"),
        _Unit("spawn", "contours", spawn, _contour_consumer(_spawn_part, "spawned-child", lambda part: part["spawner"]),
              "import:spawn", primary=False),
        _Unit("quality", "sessions", quality, _quality_consumer, "import:quality"),
    ]


# --- reading a source file ------------------------------------------------------------------

def _first_line(handle):
    handle.seek(0)
    raw = handle.readline(FIRST_LINE_MAX)
    return raw if raw.endswith(b"\n") else None


def _resume_point(handle, stored, size: int) -> "tuple[int, bool, int]":
    """(offset to read from, whether the file is read again, bytes missing against the old offset)."""
    if stored is None:
        return 0, False, 0
    offset, last_start, last_sha = stored
    if size < offset:
        return 0, True, offset - size
    handle.seek(last_start)
    if hashlib.sha256(handle.read(offset - last_start)).hexdigest() == last_sha:
        return offset, False, 0
    return 0, True, 0


_OFFSET_UPSERT = (
    "INSERT INTO import_offsets (source, generation, consumer, byte_offset, last_start, last_sha256) "
    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (source, generation, consumer) DO UPDATE SET "
    "byte_offset = excluded.byte_offset, last_start = excluded.last_start, last_sha256 = excluded.last_sha256")


def _import_unit(run: _Run, unit: _Unit, path) -> None:
    try:
        with open(path, "rb") as handle:
            _read_unit(run, unit, handle)
    except FileNotFoundError:
        return
    except OSError:
        run.stats.fail()


def _read_unit(run: _Run, unit: _Unit, handle) -> None:
    first = _first_line(handle)
    if first is None:
        return
    generation = hashlib.sha256(first).hexdigest()
    stored = run.stored_offset(unit.path, generation, unit.consumer)
    offset, reread, truncated = _resume_point(handle, stored, os.fstat(handle.fileno()).st_size)
    counters = run.stats.source(unit.source)
    if reread and unit.primary:
        counters["rereads"] += 1
        counters["truncated_bytes"] += truncated
    pending_loss = reread and truncated > 0 and unit.primary
    handle.seek(offset)
    while True:
        position, batch = offset, []
        while len(batch) < BATCH_LINES:
            raw = handle.readline()
            if not raw.endswith(b"\n"):
                break
            batch.append(_Line(position, raw, _parse_object(raw), store.imported_row_id(
                unit.path, position, hashlib.sha256(raw).hexdigest(), run.origin)))
            position += len(raw)
        if not batch:
            return
        outcome = _commit(run, _batch_work(run, unit, generation, batch, position, pending_loss))
        if not _settle(run, outcome):
            return
        pending_loss = False
        counters["rows"] += outcome["value"]["added"]
        run.stats.rows_added += outcome["value"]["added"]
        if unit.primary:
            run.stats.malformed_lines += outcome["value"]["malformed"]
        offset = position
        if len(batch) < BATCH_LINES:
            return


def _batch_work(run, unit, generation, batch, end_offset, pending_loss):
    def work(conn):
        good = [line for line in batch if line.obj is not None]
        added = unit.handler(conn, run, unit, good)
        malformed = len(batch) - len(good)
        if malformed and unit.primary:
            store._add_loss(conn, unit.fallback_session, "malformed_source_line", malformed)
        if pending_loss:
            store._add_loss(conn, unit.fallback_session, "source_truncated", 1)
        last = batch[-1]
        conn.execute(_OFFSET_UPSERT, (unit.path, generation, unit.consumer, end_offset, last.start, last.sha))
        return {"added": added, "malformed": malformed if unit.primary else 0}
    return work


# --- spools of ended sessions ---------------------------------------------------------------

def _replay_with_retry(run: _Run, stem: str) -> None:
    delay = BACKOFF_FIRST_S
    while True:
        out = store.replay(stem, deadline_s=BATCH_DEADLINE_S)
        if out["failed"]:
            run.stats.fail()
            return
        if out["drained"] and not out["deferred"]:
            return
        if run.exhausted(delay):
            run.stats.defer()
            return
        _sleep(delay)
        delay = min(delay * 2, BACKOFF_MAX_S)


def _replay_ended_spools(run: _Run, everything: bool = False) -> None:
    for stem in sync.spool_stems():
        if everything or (sync.unreplayed_bytes(run.ro, stem) > 0 and sync.is_eligible(stem)):
            _replay_with_retry(run, stem)


# --- transcripts ----------------------------------------------------------------------------

def _tool_exit(block: dict) -> int:
    if not block.get("is_error"):
        return 0
    content = block.get("content")
    if isinstance(content, list):
        content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    found = _BASH_EXIT.search(content) if isinstance(content, str) else None
    return int(found.group(1)) if found else 1


def _bash_fields(command) -> "dict | None":
    program = _tp().command_label(command, False)
    if not program:
        return None
    verb = None
    if program in ("agentctl-cli.py", "git"):
        label = _tp().command_label(command, True) or ""
        verb = label.partition(":")[2] or None
    return {"program": program, "verb": verb}


def _transcript_consumer(session: str):
    pending = {}

    def handle(conn, run, unit, lines):
        added = 0
        for line in lines:
            message = line.obj.get("message")
            blocks = message.get("content") if isinstance(message, dict) else None
            for block in blocks if isinstance(blocks, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and block.get("name") == "Bash":
                    pending[block.get("id")] = (block.get("input") or {}).get("command")
                elif block.get("type") == "tool_result" and block.get("tool_use_id") in pending:
                    fields = _bash_fields(pending[block["tool_use_id"]])
                    if fields is not None:
                        fields["exit"] = _tool_exit(block)
                        added += _put_event(conn, line.event_id, session, "bash", "transcript:Bash",
                                            _iso(line.obj.get("timestamp")), fields)
        return added
    return handle


def _import_transcripts(run: _Run) -> None:
    for path in config_root.iter_transcripts():
        session = path.stem
        if run.is_live(session):
            continue
        unit = _Unit("transcripts", "transcript_events", path, _transcript_consumer(session), session)
        _import_unit(run, unit, path)


# --- engine state history -------------------------------------------------------------------

def _load_states() -> dict:
    states = {}
    try:
        paths = sorted(config_root.agentctl_state_dir().glob("*.json"))
    except OSError:
        return states
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("session_id"), str) and isinstance(data.get("history"), list):
            states[data["session_id"]] = (path, data)
    return states


def _is_resolved(entry: dict) -> bool:
    return entry.get("event") == "resolve" or entry.get("node") == "RESOLVED"


def _opening_index(history: list) -> int:
    for index, entry in enumerate(history):
        if isinstance(entry, dict) and entry.get("event") in ("start", "reset"):
            return index
    return 0


def _spool_scan(stem: str):
    """(has an opening command, time of the first event) of a spool still waiting for replay."""
    opening, first = False, None
    try:
        with open(emit.spool_path(stem), "rb") as handle:
            for raw in handle:
                if any(f'"source_key":"{verb}"'.encode() in raw for verb in OPENING_VERBS):
                    opening = True
                if first is None:
                    obj = _parse_object(raw.strip() or b"x")
                    first = obj.get("time") if obj else None
    except OSError:
        pass
    return opening, first


def _sessions_with_opening(ro) -> set:
    marks = ",".join("?" for _ in OPENING_VERBS)
    return {r[0] for r in ro.execute(
        f"SELECT DISTINCT session_id FROM events WHERE provenance = 'live' AND source_key IN ({marks})", OPENING_VERBS)}


class _Session:
    def __init__(self, session, path, state, partial):
        self.session = session
        self.path = path
        self.state = state
        self.partial = partial
        self.history = [(i, e) for i, e in enumerate(state["history"]) if isinstance(e, dict)]
        link = state.get("parent_link")
        self.parent = link.get("parent_session") if isinstance(link, dict) else None
        self.linked = bool(link)
        self.task_id = state.get("task_id") or session
        self.times = {}
        previous = -1
        for index, entry in self.history:
            ms = _tp()._epoch_or_iso_ms(entry.get("ts"))
            self.times[index] = (ms, previous if ms is None else ms)
            previous = previous if ms is None else ms
        self.entry_ids = {index: store.imported_row_id(path, index, hashlib.sha256(_canonical(entry)).hexdigest())
                          for index, entry in self.history}


def _plan_sessions(run: _Run) -> "tuple[list, list, dict]":
    """(sessions importing identity and events, roots in task order, losses per partial session)."""
    ro = run.ro
    children_db = {r[0] for r in ro.execute("SELECT DISTINCT child_session_id FROM events WHERE child_session_id IS NOT NULL")}
    opened = _sessions_with_opening(ro)
    first_live = {r[0]: r[1] for r in ro.execute(
        "SELECT session_id, MIN(time) FROM events WHERE provenance = 'live' GROUP BY session_id")}
    chosen, partial_losses = [], {}
    for session, (path, state) in _load_states().items():
        is_child = bool(state.get("parent_link")) or (state.get("recursion_depth") or 0) > 0 or session in children_db
        spool_opening, spool_first = _spool_scan(session) if live.spool_or_end_marker_exists(session) else (False, None)
        if not run.is_live(session):
            chosen.append(_Session(session, path, state, False))
        elif not is_child and not (session in opened or spool_opening):
            entry = _Session(session, path, state, True)
            chosen.append(entry)
            cut = _tp()._epoch_or_iso_ms(first_live.get(session) or spool_first)
            partial_losses[session] = sum(
                1 for index, _ in entry.history if cut is None or (entry.times[index][1] is not None
                                                                    and entry.times[index][1] < cut))
    return chosen, partial_losses


def _assign_epochs(roots: list) -> "tuple[dict, dict]":
    """session -> epoch, epoch -> task row facts. A `start` opens an epoch; a `reset` continues the
    task's open epoch unless a resolve of the task lies between; entries before a session's opening
    entry take the session's epoch."""
    merged = sorted(((s.times[i][1], s.session, i) for s in roots for i, _ in s.history))
    by_session = {s.session: s for s in roots}
    opening = {s.session: _opening_index(s.state["history"]) for s in roots}
    task_state, session_epoch, facts = {}, {}, {}
    for ms, session, index in merged:
        owner = by_session[session]
        entry = dict(owner.history)[index]
        if index == opening[session]:
            current = task_state.get(owner.task_id)
            if entry.get("event") == "start" or current is None or current["resolved"]:
                epoch = owner.entry_ids[index]
                task_state[owner.task_id] = {"epoch": epoch, "resolved": False}
                facts[epoch] = {"task_id": owner.task_id, "weight_class": None, "opened_at": _iso(entry.get("ts")),
                                "ended_at": None}
            session_epoch[session] = task_state[owner.task_id]["epoch"]
        if _is_resolved(entry):
            current = task_state.get(owner.task_id)
            if current is not None and not current["resolved"]:
                current["resolved"] = True
                facts[current["epoch"]]["ended_at"] = _iso(entry.get("ts"))
    for owner in roots:
        epoch = session_epoch.get(owner.session)
        if epoch is None:
            continue
        weight = next((e.get("weight_class") for _, e in owner.history
                       if e.get("event") == "classify" and e.get("weight_class")), None) or owner.state.get("weight_class")
        if facts[epoch]["weight_class"] is None:
            facts[epoch]["weight_class"] = weight
    return session_epoch, facts


def _history_events(conn, run, owner: _Session, epoch) -> int:
    targets = phases.transition_targets()
    node, added = "CLASSIFIED", 0
    for index, entry in owner.history:
        before = node
        node = entry["node"] if isinstance(entry.get("node"), str) else targets.get(entry.get("event"), node)
        fields = {"command": entry.get("event"), "outcome": "ok", "node_before": before, "node_after": node}
        if epoch is not None:
            fields["task_epoch"] = epoch
        if entry.get("event") == "classify" and entry.get("weight_class"):
            fields["weight_class"] = entry["weight_class"]
        added += _put_event(conn, owner.entry_ids[index], owner.session, "command", f"cli:{entry.get('event')}",
                            _iso(entry.get("ts")), fields)
    return added


def _write_identity(conn, run: _Run, owner: _Session, key, parent_key) -> None:
    role = "linked-child" if owner.linked else "root"
    first_ms = next((owner.times[i][0] for i, _ in owner.history if owner.times[i][0] is not None), None)
    started = _iso(first_ms / 1000) if first_ms is not None else None
    depth = owner.state.get("recursion_depth")
    conn.execute(sync._SESSION_UPSERT, (owner.session, run.origin, owner.parent, role, tables.validate("roles", role),
                                        depth if depth is not None else (1 if owner.linked else 0), started))
    if owner.linked:
        conn.execute("UPDATE sessions SET role = ?, role_table_version = ? WHERE session_id = ?",
                     (role, tables.validate("roles", role), owner.session))
    attach = key or parent_key
    if attach is not None and (not owner.linked or key is None):
        store.attach_session_task(conn, owner.session, attach)
    head = run.head_at(first_ms)
    if head and conn.execute("UPDATE sessions SET core_head = ? WHERE session_id = ? AND core_head IS NULL",
                             (head, owner.session)).rowcount:
        conn.execute("INSERT OR IGNORE INTO session_head_marks (session_id, derived) VALUES (?, 1)", (owner.session,))
    if not owner.linked:
        sync._put_contour(conn, owner.session, None, "root", key)
    else:
        sync._put_contour(conn, owner.session, owner.parent, role, attach)
    for contour_id in sync._subagent_contours(owner.session):
        sync._put_contour(conn, contour_id, owner.session, "agent-tool", attach)


def _import_history(run: _Run) -> None:
    sessions, partial_losses = _plan_sessions(run)
    roots = [s for s in sessions if not (s.linked or (s.state.get("recursion_depth") or 0) > 0)]
    session_epoch, facts = _assign_epochs(roots)
    root_keys = {s.session: (run.origin, s.task_id, session_epoch[s.session]) for s in roots if s.session in session_epoch}

    def tasks_work(conn):
        for epoch, fact in facts.items():
            conn.execute("INSERT OR IGNORE INTO tasks (origin, task_id, epoch, status, weight_class, synthetic, opened_at) "
                         "VALUES (?, ?, ?, 'open', ?, 0, ?)", (run.origin, fact["task_id"], epoch, fact["weight_class"],
                                                              fact["opened_at"] or None))
            conn.execute("UPDATE tasks SET weight_class = COALESCE(weight_class, ?), "
                         "status = CASE WHEN status = 'open' AND ? IS NOT NULL THEN 'resolved' ELSE status END, "
                         "ended_at = COALESCE(ended_at, ?) WHERE epoch = ?",
                         (fact["weight_class"], fact["ended_at"], fact["ended_at"], epoch))
    if facts and not _settle(run, _commit(run, tasks_work)):
        return

    counters = run.stats.source("history")
    for chunk in _chunks(sessions, 50):
        def work(conn, chunk=chunk):
            added = 0
            for owner in chunk:
                key = root_keys.get(owner.session)
                parent_key = root_keys.get(owner.parent) if owner.parent else None
                epoch = (key or parent_key or (None, None, None))[2]
                _write_identity(conn, run, owner, key, parent_key)
                if owner.partial:
                    conn.execute("INSERT INTO losses (session_id, loss_mode, loss_mode_table_version, count) "
                                 "VALUES (?, 'partial_session_before_first_live_event', ?, ?) "
                                 "ON CONFLICT (session_id, loss_mode) DO UPDATE SET count = max(count, excluded.count)",
                                 (owner.session, tables.validate("loss_modes", "partial_session_before_first_live_event"),
                                  partial_losses[owner.session]))
                else:
                    added += _history_events(conn, run, owner, epoch)
            return added
        out = _commit(run, work)
        if not _settle(run, out):
            return
        counters["rows"] += out["value"]
        run.stats.rows_added += out["value"]


def _attach_pass(conn) -> None:
    """Give every session and contour still without a task key the key of its parent session."""
    parent = "(SELECT p.{col} FROM sessions p WHERE p.session_id = {tbl}.parent_session)"
    conn.execute(
        "UPDATE sessions SET " + ", ".join(f"{c} = " + parent.format(col=c, tbl="sessions")
                                           for c in ("task_origin", "task_id", "task_epoch"))
        + " WHERE task_id IS NULL AND parent_session IS NOT NULL AND " + parent.format(col="task_id", tbl="sessions")
        + " IS NOT NULL")
    conn.execute(
        "UPDATE contours SET " + ", ".join(f"{c} = " + parent.format(col=c, tbl="contours")
                                           for c in ("task_origin", "task_id", "task_epoch"))
        + " WHERE task_id IS NULL AND parent_session IS NOT NULL AND " + parent.format(col="task_id", tbl="contours")
        + " IS NOT NULL")
    conn.execute("UPDATE contours SET weight_class = (SELECT t.weight_class FROM tasks t WHERE t.epoch = contours.task_epoch) "
                 "WHERE weight_class IS NULL AND task_epoch IS NOT NULL")


# --- the run ----------------------------------------------------------------------------------

def _peak_rss_mib() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _losses(ro) -> dict:
    out = {}
    for session, mode, count in ro.execute("SELECT session_id, loss_mode, count FROM losses ORDER BY session_id, loss_mode"):
        out.setdefault(session, {})[mode] = count
    return out


def _uncounted_modes() -> list:
    return sorted(set(tables.load("loss_modes").members) - COUNTED_MODES)


def _emit_run_event(run: _Run, started: float) -> None:
    stats = run.stats
    store.insert_rows("events", [{
        "event_id": emit.uuid7(), "session_id": SELF_SESSION, "kind": "import.run", "source_key": SELF_SOURCE_KEY,
        "provenance": "live", "time": emit.now_iso(),
        "fields": {"source_key": SELF_SOURCE_KEY, "rows_added": stats.rows_added, "errors": stats.errors,
                   "malformed_lines": stats.malformed_lines, "complete": stats.complete,
                   "duration_s": round(_clock() - started, 3)}}], BATCH_DEADLINE_S)


def run_import(include_transcripts: bool = False, replay_all_spools: bool = False) -> dict:
    """Import every source once; the stats dict is the whole report."""
    started = _clock()
    boot = store.open_record()
    ro = store.open_readonly() if boot is not None else None
    if boot is None or ro is None:
        stats = _Stats()
        stats.fail()
        return _report(stats, None)
    boot.close()
    try:
        run = _Run(store.origin_id(), ro)
        _replay_ended_spools(run, everything=replay_all_spools)
        run.refresh_live()
        for unit in _file_units():
            _import_unit(run, unit, unit.path)
        if include_transcripts:
            _import_transcripts(run)
        run.refresh_live()
        _import_history(run)
        _settle(run, _commit(run, _attach_pass))
        _emit_run_event(run, started)
        return _report(run.stats, ro)
    finally:
        ro.close()


def _report(stats: _Stats, ro) -> dict:
    return {"rows_added": stats.rows_added, "per_source": stats.per_source, "errors": stats.errors,
            "malformed_lines": stats.malformed_lines, "complete": stats.complete,
            "peak_rss_mib": round(_peak_rss_mib(), 1),
            "losses_by_session": _losses(ro) if ro is not None else {}, "uncounted_modes": _uncounted_modes()}


# --- rebuild --------------------------------------------------------------------------------

def _live_counts(record: Path) -> "dict | None":
    """Live events per session in an existing record, None when it cannot be read."""
    if not record.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{record}?mode=ro", uri=True, timeout=1.0)
        try:
            return {r[0]: r[1] for r in conn.execute(
                "SELECT session_id, COUNT(*) FROM events WHERE provenance = 'live' GROUP BY session_id")}
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def rebuild(include_transcripts: bool = False) -> dict:
    """Move the record aside, build a new one with the same origin from the spools and the sources,
    and report the sessions whose live-only events did not come back."""
    record = config_root.telemetry_db()
    before = _live_counts(record)
    if record.exists():
        aside = record.with_name(f"{record.name}.corrupt-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}")
        for suffix in ("", "-wal", "-shm"):
            part = record.with_name(record.name + suffix)
            if part.exists():
                os.replace(part, aside.with_name(aside.name + suffix))
    report = run_import(include_transcripts, replay_all_spools=True)
    ro = store.open_readonly()
    lost = {}
    if ro is not None and before is not None:
        now = {r[0]: r[1] for r in ro.execute(
            "SELECT session_id, COUNT(*) FROM events WHERE provenance = 'live' GROUP BY session_id")}
        lost = {s: n - now.get(s, 0) for s, n in before.items() if n > now.get(s, 0)}
    if ro is not None:
        ro.close()
    if lost:
        def work(conn):
            for session, count in lost.items():
                store._add_loss(conn, session, "record_corrupt_rebuilt", count)
        store.transact(work, BATCH_DEADLINE_S)
    report["live_only_lost"] = None if before is None else lost
    report["rebuilt_sessions"] = sorted(lost)
    return report


# --- prune ------------------------------------------------------------------------------------

def retention_days() -> int:
    table = _tp().parse_config_md(_tp().CONFIG_PATH)
    if RETENTION_KEY not in table:
        raise ValueError(f"config.md has no {RETENTION_KEY} row")
    return int(table[RETENTION_KEY])


def _protected_sync_rows(conn) -> set:
    """Ids of the sync rows the latency rule still reads: per session the session_end trigger, the
    first trigger at or after eligibility (E) and the first commit at or after it, and the
    deferral rows of a spool that still holds unreplayed bytes."""
    keep = set()
    sessions = {r[0] for r in conn.execute(
        "SELECT DISTINCT json_extract(fields, '$.session') FROM events WHERE kind IN (?, ?, ?)",
        (sync.SYNC_COMMIT, sync.SYNC_DEFERRED, sync.TRIGGER_START)) if r[0]}
    triggers = sorted((sync._parse(r[1]), r[0]) for r in conn.execute(
        "SELECT event_id, time FROM events WHERE kind = ?", (sync.TRIGGER_START,)))
    for session in sessions:
        if sync.unreplayed_bytes(conn, session) > 0:
            keep.update(r[0] for r in conn.execute(
                "SELECT event_id FROM events WHERE kind = ? AND json_extract(fields, '$.session') = ?",
                (sync.SYNC_DEFERRED, session)))
        ended = sorted((sync._parse(r[1]), r[0]) for r in conn.execute(
            "SELECT event_id, time FROM events WHERE kind = ? AND json_extract(fields, '$.kind') = 'session_end' "
            "AND json_extract(fields, '$.session') = ?", (sync.TRIGGER_START, session)))
        if ended:
            end = ended[0][0]
            keep.add(ended[0][1])
        else:
            last = sorted(sync._parse(r[0]) for r in conn.execute("SELECT time FROM events WHERE session_id = ?", (session,)))
            if not last:
                continue
            end = last[-1] + timedelta(seconds=sync.LIVE_TTL_S)
        keep.update(i for t, i in triggers if t >= end)
        commits = sorted((sync._parse(r[1]), r[0]) for r in conn.execute(
            "SELECT event_id, time FROM events WHERE kind = ? AND json_extract(fields, '$.session') = ?",
            (sync.SYNC_COMMIT, session)))
        keep.update(i for _, i in [c for c in commits if c[0] >= end][:1])
    return keep


def prune(days: "int | None" = None, now: "datetime | None" = None) -> dict:
    """Delete sync and decision rows older than the retention window except the protected ones, and
    the spools of ended sessions that are fully replayed. Per-task aggregate tables are untouched."""
    days = retention_days() if days is None else days
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    result = {"events_removed": 0, "spools_removed": 0}

    def work(conn):
        keep = _protected_sync_rows(conn)
        doomed = []
        for event_id, moment, session, kind in conn.execute("SELECT event_id, time, session_id, kind FROM events"):
            if (session == sync.SYNC_SESSION or kind == "decision") and event_id not in keep:
                try:
                    old = sync._parse(moment) < cutoff
                except ValueError:
                    continue
                if old:
                    doomed.append(event_id)
        for chunk in _chunks(doomed):
            conn.execute(f"DELETE FROM events WHERE event_id IN ({','.join('?' for _ in chunk)})", chunk)
        return len(doomed)
    out = store.transact(work, BATCH_DEADLINE_S)
    if out["committed"]:
        result["events_removed"] = out["value"]
    ro = store.open_readonly()
    if ro is not None:
        try:
            for stem in sync.spool_stems():
                if sync.unreplayed_bytes(ro, stem) == 0 and sync.is_eligible(stem):
                    for path in (emit.spool_path(stem), config_root.telemetry_spool_dir() / (stem + ".lock")):
                        try:
                            path.unlink()
                        except OSError:
                            continue
                    result["spools_removed"] += 1
        finally:
            ro.close()
    return result
