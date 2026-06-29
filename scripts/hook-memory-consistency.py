#!/usr/bin/env python3
"""PreToolUse Write/Edit hook: non-blocking frontmatter reminder for memory leaves.

Classifies the target path as a memory leaf in any of the three scopes:
  1. global engineering:  …/memory-global/leaves/…
  2. project:             …/.claude/agent-memory/…
  3. personal (auto):     …/.claude/projects/*/memory/…

If the file is a memory leaf, validates the YAML frontmatter being written and
emits a [memory-consistency] reminder for any issues found. Always exits 0 —
memory writes are unconditionally gate-exempt; this hook only informs.

Index-pointer policy: when frontmatter issues are found we append a one-line
reminder to add/refresh the MEMORY.md index pointer. When frontmatter is
well-formed we stay entirely silent — emitting a reminder on every well-formed
edit would create constant noise without surfacing new information.
"""
from __future__ import annotations

import datetime
import json
import re
import sys
from pathlib import Path

import memory_dates as md

FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
VALID_TYPES = frozenset({"user", "feedback", "project", "reference"})
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_date(s: str) -> datetime.date | None:
    if not s or not _ISO_DATE_RE.match(s):
        return None
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        return None


def is_memory_leaf(path: str) -> bool:
    """True when path is a memory leaf in any of the three scopes."""
    if not path.endswith(".md"):
        return False
    p = Path(path)
    if p.name in ("MEMORY.md", ".gitkeep"):
        return False
    parts = p.parts
    # Exclude /tmp
    if len(parts) >= 2 and parts[1] == "tmp":
        return False
    # scope 1: …/memory-global/leaves/… (any depth, incl experience/ system-knowledge/)
    if "memory-global" in parts and "leaves" in parts:
        return True
    # scope 2: …/.claude/agent-memory/…
    if ".claude" in parts and "agent-memory" in parts:
        return True
    # scope 3: …/.claude/projects/<hash>/memory/…
    if ".claude" in parts:
        try:
            ci = list(parts).index(".claude")
            remaining = parts[ci + 1:]
            if (len(remaining) >= 3
                    and remaining[0] == "projects"
                    and remaining[2] == "memory"):
                return True
        except (ValueError, IndexError):
            pass
    return False


def _check_frontmatter(content: str) -> list[str]:
    """Return list of issue descriptions. Empty list means frontmatter is well-formed."""
    issues = []
    fm = FRONTMATTER_RE.match(content)
    if not fm:
        issues.append("missing frontmatter block (--- ... ---)")
        return issues
    fm_body = fm.group(1)

    def _get(key: str) -> str:
        m = re.search(rf"^{key}\s*:\s*(.*?)\s*$", fm_body, re.MULTILINE)
        return m.group(1).strip().strip("\"'") if m else ""

    name = _get("name")
    desc = _get("description")
    typ = _get("type")

    if not name:
        issues.append("missing or empty `name:`")
    if not desc:
        issues.append("missing or empty `description:`")
    if not typ:
        issues.append("missing or empty `type:`")
    elif typ not in VALID_TYPES:
        issues.append(
            f"`type: {typ}` is not one of: {', '.join(sorted(VALID_TYPES))}"
        )
    # Temporal frontmatter (memory-temporal-frontmatter.md): created +
    # last_verified required & well-formed, last_accessed format-checked.
    issues.extend(md.validate_temporal(fm_body, require=True))
    return issues


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}
    file_path = tool_input.get("file_path", "") or ""

    if not is_memory_leaf(file_path):
        return 0

    if tool == "Write":
        content = tool_input.get("content", "") or ""
    elif tool == "Edit":
        content = tool_input.get("new_string", "") or ""
        if not content:
            return 0
    else:
        return 0

    issues = _check_frontmatter(content)
    if not issues:
        return 0

    p = Path(file_path)
    index_hint = (
        f"Also ensure this file is referenced in the MEMORY.md index under `{p.parent}/`."
    )
    print("[memory-consistency] " + "; ".join(issues) + f". {index_hint}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
