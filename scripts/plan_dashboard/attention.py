"""Attention overlay: which sessions wait for the user, and a bounded read-only tail of their chat.

A separate overlay beside the pinned summary, with its own digest: ``tasks[]`` and
``model.content_digest`` do not move with it. Nothing here depends on the clock or writes a file;
a value changes only when a transcript or the locator plugin's answer does.

Shapes (stage 12 renders them as given)::

    overlay      {"waiting": "choice" | "permission" | "idle" | "none" | None,
                  "harness_session": str | None, "since": ISO-8601 str | None}
    chat entry   {"role": "user" | "assistant", "text": str, "tool": str | None, "cut": bool}
    session row  {"harness_session": str, "cwd": str | None, "name": str | None,
                  "waiting": ..., "since": ...}
    payload      {"digest": str, "tasks": {<task id>: overlay}, "sessions": [session row]}
                 (the body of ``GET /api/attention`` and the ``attention`` event, built by ``event_payload``)

``waiting`` ranks choice > permission > idle > none. *choice*: an ``AskUserQuestion`` or
``ExitPlanMode`` call with no later tool result or user text. *permission*: the locator plugin reports a pending
permission prompt. *idle*: the last assistant message holds text and no tool call (or ends its
turn) and nothing followed. *none*: the session is working. ``None``: no transcript was found.

A task is bound to one of its candidate harness sessions (its engine id and the sessions recorded
as having driven it): the one whose transcript has the newest mtime, ties to the greatest id. A
transcript is the locator's ``transcript_path``, else a file named for the session under the
transcripts root (the newest by mtime), else, only when no root was given, the repo's default lookup.

Transcripts are read incrementally: the first read takes the last ``FIRST_READ_BYTES`` aligned to a
line start, later reads only the appended bytes (plus a kept partial line), and a shrunk or replaced
file is read again from the start. The cache is per process, keyed by path, under ``_LOCK``.
"""
from __future__ import annotations

import collections
import hashlib
import json
import os
import threading
from pathlib import Path

from lib import config_root, engine_session_binding
from plan_dashboard import locate

CHAT_MAX_ENTRIES = 40
CHAT_TEXT_MAX = 2000
CUT_MARK = "… [cut]"
FIRST_READ_BYTES = 1 << 20
CHOICE_TOOLS = frozenset({"AskUserQuestion", "ExitPlanMode"})
QUESTION_TOOL = "AskUserQuestion"

_LOCK = threading.Lock()
_CACHE: dict[str, "Tail"] = {}


class Tail:
    """What a transcript's tail says, kept between incremental reads."""

    def __init__(self, inode: int):
        self.inode = inode
        self.offset = 0
        self.partial = b""
        self.chat: collections.deque = collections.deque(maxlen=CHAT_MAX_ENTRIES)
        self.pending: dict[str, str | None] = {}
        self.state: str | None = None
        self.state_ts: str | None = None
        self.last_ts: str | None = None


def _items(message: dict) -> list:
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [item for item in content if isinstance(item, dict)] if isinstance(content, list) else []


def chat_entry(role: str, text: str, tool: str | None) -> dict:
    cut = len(text) > CHAT_TEXT_MAX
    if cut:
        text = text[: CHAT_TEXT_MAX - len(CUT_MARK)] + CUT_MARK
    return {"role": role, "text": text, "tool": tool, "cut": cut}


def question_text(tool_input) -> str:
    """The question texts of an ``AskUserQuestion`` call, each followed by its option labels; the
    header, the option descriptions and ``multiSelect`` are never read."""
    questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
    blocks = []
    for question in questions if isinstance(questions, list) else []:
        if not isinstance(question, dict) or not isinstance(question.get("question"), str):
            continue
        options = question.get("options")
        labels = [o["label"] for o in options if isinstance(o, dict) and isinstance(o.get("label"), str)] \
            if isinstance(options, list) else []
        blocks.append("\n".join([question["question"], *(f"- {label}" for label in labels)]))
    return "\n\n".join(blocks)


def _apply(tail: Tail, entry: dict) -> None:
    kind = entry.get("type")
    ts = entry.get("timestamp") if isinstance(entry.get("timestamp"), str) else None
    if kind == "attachment":
        attachment = entry.get("attachment")
        if isinstance(attachment, dict) and attachment.get("type") == "queued_command":
            tail.state, tail.state_ts = "working", ts
        return
    message = entry.get("message")
    if kind not in ("user", "assistant") or not isinstance(message, dict) or entry.get("isSidechain"):
        return
    items = _items(message)
    if kind == "user":
        if entry.get("isMeta"):
            return
        texts = []
        for item in items:
            if item.get("type") == "tool_result" and isinstance(item.get("tool_use_id"), str):
                tail.pending.pop(item["tool_use_id"], None)
            elif item.get("type") == "text" and isinstance(item.get("text"), str):
                texts.append(item["text"])
        text = "\n".join(texts).strip()
        if text:
            tail.chat.append(chat_entry("user", text, None))
            tail.pending.clear()
        tail.state, tail.state_ts, tail.last_ts = "working", ts, ts
        return
    has_text = tool_used = False
    for item in items:
        if item.get("type") == "text" and isinstance(item.get("text"), str) and item["text"].strip():
            has_text = True
            tail.chat.append(chat_entry("assistant", item["text"].strip(), None))
        elif item.get("type") == "tool_use" and isinstance(item.get("name"), str):
            tool_used = True
            name = item["name"]
            if name in CHOICE_TOOLS and isinstance(item.get("id"), str):
                tail.pending[item["id"]] = ts
            text = question_text(item.get("input")) if name == QUESTION_TOOL else ""
            tail.chat.append(chat_entry("assistant", text, name))
    stop = message.get("stop_reason")
    ended = not tool_used and stop != "tool_use" and (has_text or stop == "end_turn")
    tail.state, tail.state_ts, tail.last_ts = ("ended" if ended else "working"), ts, ts


def _consume(tail: Tail, data: bytes) -> None:
    lines = (tail.partial + data).split(b"\n")
    tail.partial = lines.pop()
    if len(tail.partial) > FIRST_READ_BYTES:
        tail.partial = b""
    for line in lines:
        try:
            entry = json.loads(line)
            if isinstance(entry, dict):
                _apply(tail, entry)
        except (ValueError, TypeError, AttributeError, RecursionError):
            continue


def _first_read(handle, size: int) -> bytes:
    start = max(0, size - FIRST_READ_BYTES)
    if start == 0:
        return handle.read(size)
    handle.seek(start - 1)
    data = handle.read(size - start + 1)
    if data[:1] == b"\n":
        return data[1:]
    newline = data.find(b"\n")
    return data[newline + 1:] if newline >= 0 else b""


def read_tail(path) -> Tail | None:
    """The cached state of the transcript at ``path``, brought up to date; None when unreadable."""
    path = Path(path)
    try:
        stat = os.stat(path)
    except OSError:
        return None
    key = str(path)
    with _LOCK:
        tail = _CACHE.get(key)
        if tail is not None and (tail.inode != stat.st_ino or stat.st_size < tail.offset):
            tail = None
        if tail is not None and stat.st_size == tail.offset:
            return tail
        fresh = tail is None
        if fresh:
            tail = Tail(stat.st_ino)
        try:
            with open(path, "rb") as handle:
                if fresh:
                    data = _first_read(handle, stat.st_size)
                    tail.offset = stat.st_size
                else:
                    handle.seek(tail.offset)
                    data = handle.read(stat.st_size - tail.offset)
                    tail.offset += len(data)
        except OSError:
            return None
        _consume(tail, data)
        _CACHE[key] = tail
        return tail


def find_transcript(harness_session: str, locator, transcripts_root) -> Path | None:
    path = locate.transcript_path(locator, harness_session)
    if path is not None:
        return path
    name = config_root.sanitize_session_id(harness_session)
    if transcripts_root is not None:
        matches = sorted(Path(transcripts_root).glob(f"*/{name}.jsonl"))
    else:
        matches = config_root.iter_transcripts(f"*/{name}.jsonl")
    best = None
    for match in matches:
        try:
            mtime = match.stat().st_mtime
        except OSError:
            continue
        if best is None or mtime >= best[0]:
            best = (mtime, match)
    return best[1] if best else None


def bind_session(engine_session: str, locator, transcripts_root) -> tuple[str | None, Path | None, list[str]]:
    """``(harness session, its transcript, every session recorded as driving the task)``."""
    bound = engine_session_binding.harness_sessions_driving(engine_session)
    best = None
    for candidate in dict.fromkeys([engine_session, *bound]):
        path = find_transcript(candidate, locator, transcripts_root)
        if path is None:
            continue
        try:
            key = (path.stat().st_mtime, candidate)
        except OSError:
            continue
        if best is None or key > best[0]:
            best = (key, candidate, path)
    if best is not None:
        return best[1], best[2], bound
    return (max(bound) if bound else None), None, bound


def waiting_state(tail: Tail | None, permission: bool | None) -> tuple[str | None, str | None]:
    """``(waiting, since)`` for a transcript's tail and the locator's permission signal."""
    if tail is None:
        return None, None
    if tail.pending:
        return "choice", next(iter(tail.pending.values()))
    if permission:
        return "permission", tail.last_ts
    if tail.state == "ended":
        return "idle", tail.state_ts
    return "none", tail.state_ts


def _attend(harness_session: str | None, path: Path | None, locator) -> tuple[dict, Tail | None]:
    tail = read_tail(path) if path is not None else None
    permission = locate.permission_wait(locator, harness_session) if tail is not None else None
    waiting, since = waiting_state(tail, permission)
    return {"waiting": waiting, "harness_session": harness_session, "since": since}, tail


def task_overlay(engine_session: str, locator, transcripts_root) -> dict:
    harness, path, _ = bind_session(engine_session, locator, transcripts_root)
    return _attend(harness, path, locator)[0]


def session_row(entry: dict, locator, transcripts_root) -> dict:
    harness = entry["harness_session"]
    overlay, _ = _attend(harness, find_transcript(harness, locator, transcripts_root), locator)
    return {"harness_session": harness, "cwd": entry["cwd"], "name": entry["name"],
            "waiting": overlay["waiting"], "since": overlay["since"]}


def collect(engine_sessions, locator, transcripts_root) -> tuple[dict, list[dict]]:
    """``(overlays by task id, session rows)`` for the listed tasks."""
    overlays, represented = {}, set()
    for engine in engine_sessions:
        harness, path, bound = bind_session(engine, locator, transcripts_root)
        overlays[engine] = _attend(harness, path, locator)[0]
        represented.update([engine, *bound])
    rows = [session_row(entry, locator, transcripts_root)
            for entry in sorted(locate.sessions(locator), key=lambda e: e["harness_session"])
            if entry["harness_session"] not in represented]
    return overlays, rows


def attention_digest(overlays: dict, sessions: list) -> str:
    text = json.dumps({"tasks": overlays, "sessions": sessions}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def event_payload(overlays: dict, sessions: list) -> dict:
    return {"digest": attention_digest(overlays, sessions), "tasks": overlays, "sessions": sessions}


def chat_tail(harness_session: str | None, locator, transcripts_root, path: Path | None = None) -> dict:
    """``{"harness_session", "entries"}``: the chat tail of the transcript found for the session."""
    if path is None and harness_session:
        path = find_transcript(harness_session, locator, transcripts_root)
    tail = read_tail(path) if path is not None else None
    return {"harness_session": harness_session, "entries": list(tail.chat) if tail else []}


def task_chat(engine_session: str, locator, transcripts_root) -> dict:
    harness, path, _ = bind_session(engine_session, locator, transcripts_root)
    return chat_tail(harness, locator, transcripts_root, path)
