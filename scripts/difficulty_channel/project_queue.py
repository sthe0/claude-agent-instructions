"""Project-queue resolver for the difficulty channel.

Walks up the filesystem from a starting path to find the nearest
``.claude/agent-project.json`` with a non-empty ``instruction_queue`` field.
Used by ``file-difficulty.py`` to resolve the Startrek queue for project-local work.
"""
from __future__ import annotations

import json
from pathlib import Path


def resolve_project_queue(start: Path) -> str | None:
    """Return instruction_queue from the nearest ancestor .claude/agent-project.json, or None.

    Tolerates missing files and malformed JSON — returns None rather than raising.
    Nearest ancestor wins when multiple ancestors declare a queue.
    """
    current = start if start.is_dir() else start.parent
    root = Path(current.root)
    while True:
        candidate = current / ".claude" / "agent-project.json"
        if candidate.exists():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                queue = data.get("instruction_queue")
                if isinstance(queue, str) and queue:
                    return queue
            except (json.JSONDecodeError, OSError):
                pass
        if current == root:
            break
        current = current.parent
    return None
