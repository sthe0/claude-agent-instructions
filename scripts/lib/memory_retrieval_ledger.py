"""Append-only ledger of which memory leaves the retrieval hook surfaced and which leaf a session used.

Difficulty removed: a retrieval layer is improvable only if its misses are recorded at the
moment the true target becomes known, i.e. when a session extends or searches for a leaf.
Comparing that target with what `hook-memory-retrieval.py` surfaced earlier in the same session
yields a hit/miss label with no judgement. Rows are one JSON object per line:

    {ts, event: surface|extend|new|search, session, query, nonce, surfaced, outcome, target}

`new` rows carry outcome null: a leaf that did not exist could not have been surfaced. A row
without a session also carries null and is reported as unattributed, never as a miss.

Every write is fail-open: a ledger problem never changes a hook's or a writer's behaviour.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

LEDGER_ENV = "CLAUDE_MEMORY_RETRIEVAL_LEDGER"
QUERY_MAX_CHARS = 200
EVENTS = ("surface", "extend", "new", "search")
NONCE_MARKER = "run-nonce:"
_ROOT_TAGS = (("memory-global", "global:"), ("agent-memory", "project:"))


def ledger_path() -> Path:
    override = os.environ.get(LEDGER_ENV)
    if override:
        return Path(override)
    return Path.home() / ".local" / "log" / "claude-memory-retrieval.jsonl"


def leaf_key(path) -> str:
    """Spelling-independent identity of a leaf: tag + path under its memory root.

    The same leaf reached through the symlinked agent home, the canon or a linked worktree
    resolves to one key; a path under no memory root keeps its realpath."""
    real = os.path.realpath(str(path))
    parts = Path(real).parts
    for idx in range(len(parts) - 2, -1, -1):
        for dirname, tag in _ROOT_TAGS:
            if parts[idx] == dirname:
                return tag + "/".join(parts[idx + 1:])
    return real


def extract_nonce(prompt) -> str | None:
    if not isinstance(prompt, str):
        return None
    _, marker, tail = prompt.partition(NONCE_MARKER)
    tokens = tail.split(maxsplit=1) if marker else []
    return tokens[0] if tokens else None


def read_rows(path: Path | None = None) -> list[dict]:
    try:
        text = (path or ledger_path()).read_text(encoding="utf-8")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def surfaced_in_session(session, path: Path | None = None) -> set:
    if not session:
        return set()
    out: set = set()
    for row in read_rows(path):
        if row.get("event") == "surface" and row.get("session") == session:
            out.update(k for k in row.get("surfaced") or [] if isinstance(k, str))
    return out


def classify(targets, surfaced) -> str:
    """'hit' when any target key was surfaced earlier in the session, else 'miss'."""
    if isinstance(targets, str):
        targets = [targets]
    surfaced = set(surfaced)
    return "hit" if any(t in surfaced for t in targets) else "miss"


def append(event: str, *, session=None, query: str = "", surfaced=(), outcome=None,
           target=None, nonce=None, path: Path | None = None) -> bool:
    """Append one row; returns False (never raises) when the write failed."""
    try:
        row = {
            "ts": time.time(),
            "event": event,
            "session": session or None,
            "query": (query or "")[:QUERY_MAX_CHARS],
            "nonce": nonce,
            "surfaced": list(surfaced),
            "outcome": outcome,
            "target": target,
        }
        line = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
        target_path = path or ledger_path()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(target_path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
        return True
    except Exception:
        return False


def record_use(event: str, *, session, query: str, targets, path: Path | None = None) -> bool:
    """Append an extend/new/search row classified against the session's surfaced set.

    `targets` are leaf paths (extend: one; search: top results; new: one). `new` and a
    session-less row are unclassified (outcome null); a search with no results is too."""
    try:
        keys = [leaf_key(t) for t in targets]
        surfaced = sorted(surfaced_in_session(session, path))
        outcome = None
        if session and keys and event in ("extend", "search"):
            outcome = classify(keys, surfaced)
        return append(event, session=session, query=query, surfaced=surfaced,
                      outcome=outcome, target=keys[0] if keys else None, path=path)
    except Exception:
        return False
