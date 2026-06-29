#!/usr/bin/env python3
"""PostToolUse(Read) hook: stamp `last_accessed` on a memory leaf when it is Read.

`last_accessed` is the usage signal of the temporal-frontmatter contract (see
memory-global/leaves/memory-temporal-frontmatter.md). Git records create/modify
dates mechanically but cannot attribute a *recall*; auto-recall (the MEMORY.md
index, a leaf quoted in a system-reminder) fires no hook event, so the honest,
implementable definition of "accessed" is "explicitly opened with the Read tool".
This hook is the sole writer of `last_accessed`.

Properties (all load-bearing):
  - day-granular   — only the date, so same-day re-reads collapse to one value;
  - idempotent     — if `last_accessed` already equals today, nothing is written
                     (zero git churn on the second+ read of the day);
  - non-blocking   — always exits 0, never raises to the caller; a Read is never
                     failed by this hook;
  - frontmatter-safe — a file without a YAML frontmatter block is left untouched.

The leaf classifier is reused from hook-memory-consistency.py (loaded by path
because the filename carries hyphens) rather than forked, so the three memory
scopes stay defined in exactly one place.
"""
from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import re
import sys
from pathlib import Path

FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_ACCESSED_LINE_RE = re.compile(r"^last_accessed\s*:.*$", re.MULTILINE)
_ACCESSED_VALUE_RE = re.compile(r"^last_accessed\s*:\s*(.*?)\s*$", re.MULTILINE)


def _load_is_memory_leaf():
    """Load is_memory_leaf from hook-memory-consistency.py without forking it."""
    path = Path(__file__).resolve().parent / "hook-memory-consistency.py"
    spec = importlib.util.spec_from_file_location("hook_memory_consistency", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.is_memory_leaf


try:
    is_memory_leaf = _load_is_memory_leaf()
except Exception:  # pragma: no cover — defensive: never let a load error wedge Read
    def is_memory_leaf(_path: str) -> bool:  # type: ignore[misc]
        return False


def _today() -> str:
    return _dt.date.today().isoformat()


def stamp(path: Path, today: str) -> bool:
    """Set `last_accessed = today` on a memory leaf. Return True iff rewritten.

    No-ops (returns False) when: the file is unreadable, has no frontmatter, or
    already carries today's date. Never raises.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    m = FRONTMATTER_RE.match(text)
    if not m:
        return False
    fm_body = m.group(1)
    existing = _ACCESSED_VALUE_RE.search(fm_body)
    if existing:
        if existing.group(1).strip().strip("\"'") == today:
            return False  # idempotent: already stamped today
        new_fm = _ACCESSED_LINE_RE.sub(f"last_accessed: {today}", fm_body, count=1)
    else:
        new_fm = fm_body.rstrip("\n") + f"\nlast_accessed: {today}"
    new_text = text[: m.start(1)] + new_fm + text[m.end(1):]
    if new_text == text:
        return False
    try:
        path.write_text(new_text, encoding="utf-8")
    except OSError:
        return False
    return True


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if payload.get("tool_name") != "Read":
        return 0
    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path") or ""
    try:
        if file_path and is_memory_leaf(file_path):
            stamp(Path(file_path), _today())
    except Exception:
        pass  # a stamping failure must never fail the Read
    return 0


if __name__ == "__main__":
    sys.exit(main())
