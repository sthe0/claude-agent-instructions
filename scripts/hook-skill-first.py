#!/usr/bin/env python3
"""PreToolUse(Bash) hook: nudge to prefer a domain Skill when a Bash command
hand-rolls a known domain operation.

Rule (CLAUDE.md § Skill-first dispatch + memory leaf skill-first-dispatch):
before issuing Bash for a known domain operation (VCS, secrets, tracker REST,
monorepo code search, …) scan the skill list and prefer the Skill — it is the
cheaper, single-call, auditable, write-capable path. Passive listing is not a
trigger; this hook makes the scan mechanical by matching operation-class
signatures in the raw command.

Each matched class fires once per session (state file) so a repeated operation
does not flood context. Advisory only: stdout, exit 0, never blocks.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# operation-class -> (compiled signature, suggested skill family). High
# precision: each pattern targets a write/domain op a skill clearly covers.
CLASSES: list[tuple[str, re.Pattern, str]] = [
    ("vcs", re.compile(r"\barc\s+(add|commit|push|pr|branch|checkout)\b"),
     "arc / arc-worktrees / create-pr"),
    ("secrets", re.compile(r"\bya\s+vault\b"),
     "ya-vault"),
    ("codesearch", re.compile(r"\barc\s+grep\b"),
     "codesearch / ast-index"),
    ("tracker", re.compile(
        r"curl\b[^\n]*\b(st-api\.yandex|startrek|tracker\.yandex|/v2/issues)\b",
        re.IGNORECASE),
     "tracker / tracker-management / startrek-client"),
    ("ci", re.compile(r"\bya\s+(make\s+-A|test)\b|\bsandbox\b.*\b(create|run)\b",
                      re.IGNORECASE),
     "ci / sandbox / sandbox-client"),
    ("yt", re.compile(r"\byt\s+(read-table|write-table|map|reduce|map-reduce|select|list)\b"),
     "yt / yql"),
    ("wiki", re.compile(r"curl\b[^\n]*\bwiki-api\b|\bwiki\s+(page|upload)\b", re.IGNORECASE),
     "wiki / wiki-client"),
]


def detect(cmd: str) -> list[tuple[str, str]]:
    """Return [(class_name, skill_family), …] for every matched operation class."""
    return [(name, skill) for name, rx, skill in CLASSES if rx.search(cmd)]


def state_path(session_id: str) -> Path:
    safe = "".join(c for c in (session_id or "nosession") if c.isalnum() or c in "-_")
    return Path(f"/tmp/cc-skill-first-{safe or 'nosession'}.json")


def load_fired(p: Path) -> set[str]:
    try:
        return set(json.loads(p.read_text()))
    except Exception:
        return set()


def save_fired(p: Path, fired: set[str]) -> None:
    try:
        p.write_text(json.dumps(sorted(fired)))
    except Exception:
        pass


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    if payload.get("tool_name") != "Bash":
        return 0

    cmd = (payload.get("tool_input") or {}).get("command") or ""
    if not cmd.strip():
        return 0

    matches = detect(cmd)
    if not matches:
        return 0

    sp = state_path(payload.get("session_id") or "")
    fired = load_fired(sp)
    fresh = [(name, skill) for name, skill in matches if name not in fired]
    if not fresh:
        return 0
    fired.update(name for name, _ in fresh)
    save_fired(sp, fired)

    lines = "\n".join(f"  - {name}: prefer Skill family → {skill}" for name, skill in fresh)
    print(
        "[skill-first] This Bash command hand-rolls a known domain operation:\n"
        f"{lines}\n"
        "Per CLAUDE.md § Skill-first dispatch: a Skill is the cheaper, single-call,\n"
        "auditable, write-capable path. Scan the system-reminder skill list and prefer\n"
        "the Skill over raw CLI (and over an mcp__* tool for the same op)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
