"""Token accounting: the collector that files every transcript's per-message usage, context
births, compactions and judge-ledger usage into the record, the standing surface per Core HEAD,
and the checks behind `telemetry.py check-tokens`.

The collector reads each transcript (a session's own and its Agent-tool subagents') and the judge
ledger from its own offset per (file, generation, consumer `tokens`), up to the last complete
line, and commits each batch's rows with that offset in one transaction
(store.insert_rows_with_offset). A message id is one token row in the whole record; its winning
line ranks by timestamp, then original before copy (a copy being a line whose sessionId is not its
file's session), then the earlier session start, transcript path and byte offset. The row goes to
the task epoch open in its session at its timestamp, or to no task (gap `no_open_epoch`).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from lib import config_root, judge_ledger
from lib.telemetry import phases, store, sync
from lib.telemetry.importer import _first_line, _resume_point

CONSUMER = "tokens"
BATCH_LINES = 1000
BATCH_DEADLINE_S = 5.0
UNKNOWN_RANK = 1 << 62
USAGE_COLUMNS = (("input_tokens", "input_tokens"), ("cache_creation_input_tokens", "cache_write_tokens"),
                 ("cache_read_input_tokens", "cache_read_tokens"), ("output_tokens", "output_tokens"))
TOKEN_COLUMNS = tuple(column for _, column in USAGE_COLUMNS)
LINT_PROSE_LENGTH = Path(__file__).resolve().parents[2] / "lint-prose-length.py"
HOOK_SCAN_LINES = 200


# --- what the collector reads ---------------------------------------------------------------

def transcripts(sessions=None) -> "list[tuple[Path, str, bool]]":
    """(path, owning session, is an Agent-tool subagent) of every transcript, or of `sessions`'."""
    found = []
    for root in config_root.projects_roots():
        for stem in ("*",) if sessions is None else sessions:
            own = [(p, p.stem, False) for p in sorted(root.glob(f"*/{stem}.jsonl"))]
            subs = [(p, p.parent.parent.name, True) for p in sorted(root.glob(f"*/{stem}/subagents/*.jsonl"))]
            found.extend(own + subs)
    return found


def _parse(raw: bytes):
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _content_id(origin: str, kind: str, session: str, line_sha: str) -> str:
    """Identity-table row 8: UUIDv5 in the origin namespace over the kind, session and line content."""
    return str(uuid.uuid5(uuid.UUID(origin), f"{kind}\0{session}\0{line_sha}"))


def _iso(ms: "int | None") -> "str | None":
    return None if ms is None else datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds")


def _usage(obj: dict):
    """(message id, model, token counts) of a line carrying usage, else None."""
    message = obj.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
        return None
    message_id = message.get("id")
    if not isinstance(message_id, str) or not message_id:
        return None
    usage = message["usage"]
    counts = {column: int(usage.get(key) or 0) for key, column in USAGE_COLUMNS}
    return message_id, message.get("model"), counts


def context_size(counts: dict) -> int:
    return counts["input_tokens"] + counts["cache_write_tokens"] + counts["cache_read_tokens"]


def _is_compaction(obj: dict) -> bool:
    return obj.get("type") == "system" and obj.get("subtype") == "compact_boundary"


def _line_rank(obj: dict, session: str, start_ms: int, source: str, start: int) -> tuple:
    ts_ms = phases._ms(obj.get("timestamp"))
    return (UNKNOWN_RANK if ts_ms is None else ts_ms, 0 if obj.get("sessionId") == session else 1,
            start_ms, source, start)


# --- attribution ----------------------------------------------------------------------------

class _Attribution:
    """Role, phase and task epoch of a session's messages, read once per session from the record."""

    def __init__(self, conn):
        self.conn = conn
        self._opens, self._rows, self._points, self._child = {}, {}, {}, {}

    def _session_row(self, session: str):
        if session not in self._rows:
            self._rows[session] = self.conn.execute(
                "SELECT parent_session, role, task_origin, task_id, task_epoch FROM sessions WHERE session_id = ?",
                (session,)).fetchone()
        return self._rows[session]

    def opens(self, session: str) -> list:
        if session not in self._opens:
            self._opens[session] = sorted((int(t.timestamp() * 1000), e) for t, e in sync._task_opens(self.conn, session))
        return self._opens[session]

    def epoch_at(self, session: str, ts_ms: "int | None"):
        """(origin, task_id, epoch) open in `session` at `ts_ms`: the latest live opening at or
        before it when the session has live openings (None before the first), else the session's
        own task key (a child's contour task, an imported session's history task)."""
        opens = self.opens(session)
        if opens:
            before = [epoch for moment, epoch in opens if ts_ms is not None and moment <= ts_ms]
            if not before:
                return None
            row = self.conn.execute("SELECT origin, task_id, epoch FROM tasks WHERE epoch = ?", (before[-1],)).fetchone()
            return tuple(row) if row else None
        row = self._session_row(session)
        return (row[2], row[3], row[4]) if row and row[3] is not None else None

    def is_child(self, session: str) -> bool:
        if session not in self._child:
            row = self._session_row(session)
            self._child[session] = bool(row and row[0]) or self.conn.execute(
                "SELECT 1 FROM events WHERE kind = 'spawn' AND child_session_id = ? LIMIT 1", (session,)).fetchone() is not None
        return self._child[session]

    def role(self, session: str, subagent: bool) -> str:
        if subagent:
            return "agent-tool"
        row = self._session_row(session)
        if row and row[1]:
            return row[1]
        return "spawned-child" if self.is_child(session) else "root"

    def phase_at(self, session: str, ts_ms: "int | None") -> str:
        if session not in self._points:
            points, landing = phases.session_points(self.conn, session)
            self._points[session] = (phases.phase_intervals(points), landing)
        intervals, landing = self._points[session]
        if ts_ms is None:
            return phases.OTHER
        if phases._in(landing, ts_ms):
            return phases.LANDING
        for start, end, phase in intervals:
            if start <= ts_ms and (end is None or ts_ms < end):
                return phase
        return phases.OTHER


def _task_columns(key) -> dict:
    key = key or (None, None, None)
    return {"task_origin": key[0], "task_id": key[1], "task_epoch": key[2]}


# --- reading one source incrementally ------------------------------------------------------

def _stored(conn, source: str, generation: str):
    return conn.execute(
        "SELECT byte_offset, last_start, last_sha256, carry FROM import_offsets "
        "WHERE source = ? AND generation = ? AND consumer = ?", (source, generation, CONSUMER)).fetchone()


def _read_source(run, path: Path, handler) -> None:
    """Read `path` from the tokens offset in batches of complete lines, each committed with its
    offset; stop at the first batch not committed, leaving the offset where it was."""
    source = str(path)
    try:
        handle = open(path, "rb")
    except FileNotFoundError:
        return
    except OSError:
        run.counts["failed"] += 1
        return
    with handle:
        first = _first_line(handle)
        if first is None:
            return
        generation = _sha(first)
        size = os.fstat(handle.fileno()).st_size
        stored = _stored(run.conn, source, generation)
        offset, reread, _ = _resume_point(handle, stored[:3] if stored else None, size)
        carry = json.loads(stored[3]) if stored and stored[3] and not reread else {}
        handle.seek(offset)
        run.counts["files"] += 1
        while True:
            position, lines = offset, []
            while len(lines) < BATCH_LINES:
                raw = handle.readline()
                if not raw.endswith(b"\n") or position + len(raw) > size:
                    break
                lines.append((position, raw))
                position += len(raw)
            if not lines:
                return
            rows, updates = handler(lines, carry)
            if not store.record_read_attempt(source, generation, CONSUMER, size, "pending", run.deadline_s):
                run.counts["deferred"] += 1
                return
            last_start, last_raw = lines[-1]
            outcome = store.insert_rows_with_offset(rows, {
                "source": source, "generation": generation, "consumer": CONSUMER, "byte_offset": position,
                "last_start": last_start, "last_sha256": _sha(last_raw), "read_size": size,
                "carry": json.dumps(carry, sort_keys=True)}, run.deadline_s, updates)
            if not outcome["committed"]:
                store.record_read_attempt(source, generation, CONSUMER, size,
                                          "deferred" if outcome["deferred"] else "failed", run.deadline_s)
                run.counts["deferred" if outcome["deferred"] else "failed"] += 1
                return
            run.counts["rows"] += outcome["inserted"]
            offset = position
            if len(lines) < BATCH_LINES:
                return


def _transcript_handler(run, path: Path, session: str, subagent: bool):
    source = str(path)
    who = run.who

    def handle(lines, carry):
        rows, updates = [], []
        for start, raw in lines:
            obj = _parse(raw)
            if obj is None:
                continue
            ts_ms = phases._ms(obj.get("timestamp"))
            if ts_ms is not None and "start_ms" not in carry:
                carry["start_ms"] = ts_ms
            line_session = obj.get("sessionId")
            original = line_session == session
            if isinstance(line_session, str) and not original:
                carry["copies"] = True
            sha = _sha(raw)
            if original and not carry.get("born"):
                carry["born"] = True
                kind = "resume" if carry.get("copies") else "spawn" if subagent or who.is_child(session) else None
                if kind:
                    rows.append(("context_births", {
                        "birth_id": _content_id(run.origin, kind, session, sha), "session_id": session, "kind": kind,
                        "born_at": _iso(ts_ms), "transcript": source, **_task_columns(who.epoch_at(session, ts_ms))}))
            if _is_compaction(obj):
                cid = _content_id(run.origin, "compaction", session, sha)
                rows.append(("context_births", {
                    "birth_id": cid, "session_id": session, "kind": "compaction", "born_at": _iso(ts_ms),
                    "transcript": source, **_task_columns(who.epoch_at(session, ts_ms))}))
                rows.append(("compactions", {"compaction_id": cid, "session_id": session, "compacted_at": _iso(ts_ms),
                                             "pre_tokens": carry.get("last_size"), "post_tokens": None,
                                             "transcript": source}))
                carry["open_compaction"] = cid
                continue
            usage = _usage(obj)
            if usage is None:
                continue
            message_id, model, counts = usage
            if carry.get("open_compaction"):
                updates.append(("compactions", "compaction_id", carry.pop("open_compaction"), "post_tokens",
                                context_size(counts)))
            carry["last_size"] = context_size(counts)
            ts_rank, copy_rank, start_ms, _, _ = _line_rank(obj, session, carry.get("start_ms", UNKNOWN_RANK),
                                                           source, start)
            rows.append(("token_usage", {
                "message_id": message_id, "session_id": session, "role": who.role(session, subagent),
                "phase": who.phase_at(session, ts_ms), "model": model, **counts, "ts": _iso(ts_ms),
                "ts_ms": ts_rank, "copy_rank": copy_rank, "session_start_ms": start_ms, "transcript": source,
                "byte_offset": start, **_task_columns(who.epoch_at(session, ts_ms))}))
        return rows, updates
    return handle


def _ledger_handler(run, path: Path):
    source = str(path)

    def handle(lines, carry):
        rows = []
        for start, raw in lines:
            obj = _parse(raw)
            session, judge = (obj or {}).get("source"), (obj or {}).get("judge")
            if obj is None or obj.get("kind") != "call" or not isinstance(session, str) or not isinstance(judge, str):
                continue
            sha = _sha(raw)
            ts_ms = phases._ms(obj.get("ts"))
            usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else None
            counts = {column: int((usage or {}).get(key) or 0) for key, column in USAGE_COLUMNS}
            rows.append(("judge_usage", {
                "usage_id": _content_id(run.origin, "judge_usage", session, sha),
                "ledger_row_id": store.imported_row_id(source, start, sha, run.origin), "session_id": session,
                "judge": judge, "ts": _iso(ts_ms), "has_usage": 1 if usage else 0, **counts,
                "phase": run.who.phase_at(session, ts_ms), **_task_columns(run.who.epoch_at(session, ts_ms))}))
        return rows, []
    return handle


# --- the standing surface -------------------------------------------------------------------

_lint_module = None


def _lint():
    """lint-prose-length.py, the measure behind always-loaded-surface-advisory-chars (hyphenated, so
    loaded by path)."""
    global _lint_module
    if _lint_module is None:
        spec = importlib.util.spec_from_file_location("lint_prose_length", LINT_PROSE_LENGTH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _lint_module = module
    return _lint_module


def static_surface_bytes() -> int:
    lint = _lint()
    total = sum(len(p.read_text(encoding="utf-8").encode("utf-8")) for _, p in lint._surface_files() if p.is_file())
    return total + sum(len(text.encode("utf-8")) for _, text in lint._iter_skill_descriptions())


def session_start_hook_bytes(path: Path) -> int:
    """UTF-8 bytes of the SessionStart hook outputs injected into one transcript's context."""
    total = 0
    with open(path, "rb") as handle:
        for _ in range(HOOK_SCAN_LINES):
            obj = _parse(handle.readline() or b"null")
            if obj is None:
                continue
            attachment = obj.get("attachment")
            if isinstance(attachment, dict) and attachment.get("hookEvent") == "SessionStart":
                text = attachment.get("content")
                total += len(text.encode("utf-8")) if isinstance(text, str) else 0
    return total


def canon_head() -> "str | None":
    return sync._canon_state()[0]


def _surface_row(conn, origin: str, head: str) -> "dict | None":
    """The standing surface at `head`, measured on the latest session started there; None until one has."""
    for (session,) in conn.execute("SELECT session_id FROM sessions WHERE core_head = ? "
                                   "ORDER BY started_at DESC", (head,)).fetchall():
        own = [p for p, _, subagent in transcripts([session]) if not subagent]
        if not own:
            continue
        static, hooks = static_surface_bytes(), session_start_hook_bytes(own[0])
        return {"origin": origin, "core_head": head, "bytes": static + hooks, "static_bytes": static,
                "hook_bytes": hooks, "session_id": session,
                "measured_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}
    return None


# --- the collector --------------------------------------------------------------------------

class _Run:
    def __init__(self, conn, origin: str, deadline_s):
        self.conn, self.origin, self.deadline_s = conn, origin, deadline_s
        self.who = _Attribution(conn)
        self.counts = {"rows": 0, "files": 0, "deferred": 0, "failed": 0}


def collect(sessions=None, deadline_s: "float | None" = None) -> dict:
    """Read every transcript (or `sessions`' transcripts), the judge ledger and the standing
    surface into the record. Returns {rows, files, deferred, failed}; never raises on a store
    or file failure."""
    deadline_s = BATCH_DEADLINE_S if deadline_s is None else deadline_s
    boot = store.open_record(deadline_s)
    conn = store.open_readonly() if boot is not None else None
    if boot is not None:
        boot.close()
    origin = store.origin_id() if conn is not None else None
    if conn is None or origin is None:
        return {"rows": 0, "files": 0, "deferred": 0, "failed": 1}
    run = _Run(conn, origin, deadline_s)
    try:
        for path, session, subagent in transcripts(sessions):
            _read_source(run, path, _transcript_handler(run, path, session, subagent))
        _read_source(run, judge_ledger.ledger_path(), _ledger_handler(run, judge_ledger.ledger_path()))
        head = canon_head()
        row = _surface_row(conn, origin, head) if head else None
        if row is not None:
            outcome = store.insert_rows("standing_surface", [row], run.deadline_s)
            run.counts["deferred"] += outcome["deferred"]
            run.counts["failed"] += outcome["failed"]
    finally:
        conn.close()
    return run.counts


# --- check-tokens ---------------------------------------------------------------------------

def _offset_row(conn, path: Path):
    """The tokens offset row of `path`'s current generation, None when never read."""
    try:
        with open(path, "rb") as handle:
            first = _first_line(handle)
    except OSError:
        return None
    if first is None:
        return None
    row = conn.execute("SELECT byte_offset, read_size, read_outcome FROM import_offsets "
                       "WHERE source = ? AND generation = ? AND consumer = ?",
                       (str(path), _sha(first), CONSUMER)).fetchone()
    return None if row is None else {"byte_offset": row[0], "read_size": row[1], "read_outcome": row[2]}


def _usage_lines(path: Path, session: str, limit: int):
    """(message id, rank, counts, timestamp ms) of every usage line of `path` before byte `limit`."""
    source, start_ms, position = str(path), None, 0
    with open(path, "rb") as handle:
        while position < limit:
            raw = handle.readline()
            if not raw.endswith(b"\n") or position + len(raw) > limit:
                break
            start, position = position, position + len(raw)
            obj = _parse(raw)
            if obj is None:
                continue
            ts_ms = phases._ms(obj.get("timestamp"))
            if start_ms is None and ts_ms is not None:
                start_ms = ts_ms
            usage = _usage(obj)
            if usage is not None:
                rank = _line_rank(obj, session, UNKNOWN_RANK if start_ms is None else start_ms, source, start)
                yield usage[0], rank, usage[2], ts_ms


def _last_line_end(path: Path, size: int) -> int:
    """End of the last complete line within the first `size` bytes of `path`."""
    with open(path, "rb") as handle:
        end = size
        while end > 0:
            begin = max(0, end - (1 << 16))
            handle.seek(begin)
            chunk = handle.read(end - begin)
            newline = chunk.rfind(b"\n")
            if newline >= 0:
                return begin + newline + 1
            end = begin
    return 0


def _task_sessions(conn, epochs: set) -> set:
    marks = ",".join("?" for _ in epochs)
    sessions = {r[0] for r in conn.execute(f"SELECT session_id FROM sessions WHERE task_epoch IN ({marks})", list(epochs))}
    sessions |= {r[0] for r in conn.execute(f"SELECT session_id FROM events WHERE event_id IN ({marks})", list(epochs))}
    return sessions


def _judge_events(conn, keys: list) -> list:
    ids = set()
    for key in keys:
        ids.update(sync.events_of_task(conn, *key))
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    return [tuple(r) for r in conn.execute(
        f"SELECT event_id, backref_id, child_session_id, source_key FROM events "
        f"WHERE kind = 'judge' AND event_id IN ({marks}) ORDER BY time, event_id", sorted(ids))]


def _sum(conn, table: str, epochs: set) -> dict:
    marks = ",".join("?" for _ in epochs)
    row = conn.execute(f"SELECT {', '.join(f'COALESCE(SUM({c}), 0)' for c in TOKEN_COLUMNS)} FROM {table} "
                       f"WHERE task_epoch IN ({marks})", list(epochs)).fetchone()
    return dict(zip(TOKEN_COLUMNS, row))


def _add(total: dict, counts: dict) -> None:
    for column in TOKEN_COLUMNS:
        total[column] += counts[column]


def check_task(conn, task_id: str, require_spawn: bool = False, require_compaction: bool = False) -> dict:
    """Conservation of one task's tokens: the (role, phase) cells of its token rows against the
    message ids whose winning line, read up to each transcript's tokens offset, falls in its epochs."""
    keys = [tuple(r) for r in conn.execute("SELECT origin, task_id, epoch FROM tasks WHERE task_id = ?", (task_id,))]
    if not keys:
        return {"ok": False, "usage_error": f"no task {task_id!r} in the record"}
    epochs = {key[2] for key in keys}
    who = _Attribution(conn)
    gaps = {"no_open_epoch": [], "owned_by_another_task": [], "grown_since_read": [], "missing_judge_call": [],
            "unexpected_judge_rows": [], "judge_rows_without_usage": 0}
    best = {}
    for path, session, _ in transcripts(sorted(_task_sessions(conn, epochs))):
        offset = _offset_row(conn, path)
        limit = offset["byte_offset"] if offset else 0
        size = path.stat().st_size
        if size > limit:
            gaps["grown_since_read"].append({"transcript": str(path), "bytes": size - limit})
        for message_id, rank, counts, ts_ms in _usage_lines(path, session, limit):
            if message_id not in best or rank < best[message_id][0]:
                best[message_id] = (rank, counts, session, ts_ms)
    expected = dict.fromkeys(TOKEN_COLUMNS, 0)
    for message_id, (rank, counts, session, ts_ms) in sorted(best.items()):
        stored = conn.execute("SELECT ts_ms, copy_rank, session_start_ms, transcript, byte_offset, task_epoch "
                              "FROM token_usage WHERE message_id = ?", (message_id,)).fetchone()
        if stored is not None and tuple(stored[:5]) < rank:
            if stored[5] not in epochs:
                gaps["owned_by_another_task"].append({"message_id": message_id, "owner_epoch": stored[5]})
            else:
                _add(expected, counts)
            continue
        key = who.epoch_at(session, ts_ms)
        if key is None:
            gaps["no_open_epoch"].append({"message_id": message_id, "session_id": session})
        elif key[2] in epochs:
            _add(expected, counts)
    marks = ",".join("?" for _ in epochs)
    cells = {f"{r[0]}/{r[1]}": dict(zip(TOKEN_COLUMNS, r[2:])) for r in conn.execute(
        f"SELECT role, phase, {', '.join(f'SUM({c})' for c in TOKEN_COLUMNS)} FROM token_usage "
        f"WHERE task_epoch IN ({marks}) GROUP BY role, phase ORDER BY role, phase", list(epochs))}
    actual = dict.fromkeys(TOKEN_COLUMNS, 0)
    for counts in cells.values():
        _add(actual, counts)
    judge = _sum(conn, "judge_usage", epochs)
    _add(expected, judge)
    _add(actual, judge)

    ledger_rows = {r[0]: r[1] for r in conn.execute(
        f"SELECT ledger_row_id, has_usage FROM judge_usage WHERE task_epoch IN ({marks})", list(epochs))}
    all_ledger_ids = {r[0] for r in conn.execute("SELECT ledger_row_id FROM judge_usage")}
    judge_events = _judge_events(conn, keys)
    named = set()
    for event_id, backref_id, child, source_key in judge_events:
        ledger_id = backref_id or event_id
        named.add(ledger_id)
        has_transcript = child is not None and conn.execute(
            "SELECT 1 FROM token_usage WHERE session_id = ? LIMIT 1", (child,)).fetchone() is not None
        if ledger_id not in all_ledger_ids and not has_transcript:
            gaps["missing_judge_call"].append({"event_id": event_id, "source_key": source_key})
    gaps["unexpected_judge_rows"] = sorted(r for r in ledger_rows if r not in named)
    gaps["judge_rows_without_usage"] = sum(1 for has in ledger_rows.values() if not has)

    births = {r[0]: r[1] for r in conn.execute(
        f"SELECT kind, COUNT(*) FROM context_births WHERE task_epoch IN ({marks}) GROUP BY kind", list(epochs))}
    missing_births = [kind for kind, wanted in (("spawn", require_spawn), ("compaction", require_compaction))
                      if wanted and not births.get(kind)]
    conserved = expected == actual
    ok = conserved and not gaps["missing_judge_call"] and not missing_births
    return {"ok": ok, "task_id": task_id, "epochs": sorted(epochs), "conserved": conserved, "expected": expected,
            "actual": actual, "cells": cells, "judge_usage": judge, "judge_calls": len(judge_events),
            "unexpected_judge_count": len(gaps["unexpected_judge_rows"]), "births": births,
            "missing_births": missing_births, "gaps": gaps}


def _attempt_problem(path: Path, offset) -> "str | None":
    if offset is None:
        return "never_read"
    if offset["read_outcome"] != "committed":
        return f"last_read_{offset['read_outcome']}"
    if offset["byte_offset"] != _last_line_end(path, offset["read_size"]):
        return "offset_short_of_last_line"
    return None


def check_imported_coverage(conn) -> dict:
    """Every transcript with usage fields has token rows and a last collector read that committed
    up to the last complete line it attempted; bytes appended since are listed, not counted."""
    uncovered, grown, covered, no_open_epoch = [], [], 0, 0
    for path, session, _ in transcripts():
        offset = _offset_row(conn, path)
        size = path.stat().st_size
        reach = offset["read_size"] if offset and offset["read_size"] is not None else size
        ids = {message_id for message_id, _, _, _ in _usage_lines(path, session, reach)}
        if not ids:
            continue
        problem = _attempt_problem(path, offset)
        if problem is None:
            read = {m for m, _, _, _ in _usage_lines(path, session, offset["byte_offset"])}
            marks = ",".join("?" for _ in read)
            present = {r[0] for r in conn.execute(
                f"SELECT message_id FROM token_usage WHERE message_id IN ({marks})", sorted(read))} if read else set()
            if read - present:
                problem = "missing_token_rows"
        if problem is not None:
            uncovered.append({"transcript": str(path), "reason": problem})
            continue
        covered += 1
        if size > offset["read_size"]:
            grown.append({"transcript": str(path), "bytes": size - offset["read_size"]})
        no_open_epoch += conn.execute("SELECT COUNT(*) FROM token_usage WHERE transcript = ? AND task_id IS NULL",
                                      (str(path),)).fetchone()[0]
    return {"ok": not uncovered, "covered": covered, "uncovered": uncovered,
            "gaps": {"grown_since_read": grown, "no_open_epoch": no_open_epoch}}
