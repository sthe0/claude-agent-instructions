#!/usr/bin/env python3
"""PreToolUse hook: deny an `Agent`/`Task` call that stands in for a specialist.

Difficulty removed: a specialist (planner, developer, thinker, tech-writer,
code-reviewer, ...) can be impersonated by a general `Agent` subagent told to
read the specialist's SKILL.md. The run then has no spawn-costs row, no return-marker
validation and no permission digest -- no hook saw it. The sanctioned routes are an
inline `Skill` call and `scripts/spawn-specialist.py --kind <skill>`.

The decision is a literal-token prefilter on two string fields of the payload
(`prompt`, `description`): a path token `skills/<...>/SKILL.md` (this covers
`skills/specializations/<name>/SKILL.md`), whatever the `subagent_type`. It reads
path syntax, never what the prompt means; an exploration or polling prompt that
does not name a SKILL.md passes. Always exits 0; any unexpected error falls through
to allow.

DENY is signaled with the PreToolUse permissionDecision JSON on stdout, matching
hook-guard-subagent-destructive-action.py.
"""
from __future__ import annotations

import json
import re
import sys

_TOOLS = frozenset({"Agent", "Task"})
_FIELDS = ("prompt", "description")
_SKILL_PATH = re.compile(r"skills/[\w./-]*SKILL\.md")


def decide(payload: dict) -> str | None:
    if payload.get("tool_name", "") not in _TOOLS:
        return None
    tool_input = payload.get("tool_input") or {}
    for field in _FIELDS:
        text = tool_input.get(field)
        if not isinstance(text, str):
            continue
        match = _SKILL_PATH.search(text)
        if match:
            return (
                f"Refusing an {payload.get('tool_name')} call whose {field} names "
                f"{match.group(0)!r}: a general Agent told to read a specialist's "
                "SKILL.md stands in for that specialist, so the run gets no "
                "spawn-costs row, no return-marker validation and no permission "
                "digest. Run the specialist inline with the `Skill` tool, or spawn "
                "it with `scripts/spawn-specialist.py --kind <skill>`. A general "
                "Agent is for exploration and polling only."
            )
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        reason = decide(payload)
    except Exception:
        return 0

    if reason:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }))

    return 0


if __name__ == "__main__":
    sys.exit(main())
