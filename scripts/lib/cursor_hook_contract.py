"""Host-boundary translation between Cursor hook JSON and the Claude hook contract.

Pure functions only: the adapter script owns the I/O, this module owns the
mapping, so both directions are testable without a subprocess.

Cursor's `beforeSubmitPrompt` accepts no context field, so Claude's
`UserPromptSubmit` advisories are carried at turn end (`stop`) as a bounded
follow-up instead. Cursor `stop` cannot hard-block a turn the way Claude's Stop
decision can; that difference is intentional and is never presented as parity.
"""
from __future__ import annotations

from typing import Any

CURSOR_TO_CLAUDE_TOOL = {
    "Shell": "Bash",
    "StrReplace": "Edit",
    "Write": "Write",
    "Read": "Read",
    "Grep": "Grep",
    "Glob": "Glob",
    "Task": "Task",
    "AskQuestion": "AskUserQuestion",
    "EditNotebook": "NotebookEdit",
}

CLAUDE_TO_CURSOR_TOOL = {
    "Bash": "Shell",
    "Edit": "StrReplace",
    "Write": "Write",
    "Read": "Read",
    "Grep": "Grep",
    "Glob": "Glob",
    "Task": "Task",
    "AskUserQuestion": "AskQuestion",
    "NotebookEdit": "EditNotebook",
}

CLAUDE_EVENT_TO_CURSOR_EVENT = {
    "PreToolUse": "preToolUse",
    "PostToolUse": "postToolUse",
    "SessionStart": "sessionStart",
    "Stop": "stop",
    "UserPromptSubmit": "stop",
}

PERMISSION_EVENTS = {"preToolUse"}
CONTEXT_EVENTS = {"sessionStart", "postToolUse"}
FOLLOWUP_EVENTS = {"stop"}


def cursor_matcher_for(claude_matcher: str | None) -> str | None:
    """Translate a Claude tool matcher into its Cursor tool names."""
    if not claude_matcher:
        return None
    parts = [p for p in claude_matcher.split("|") if p]
    translated = [CLAUDE_TO_CURSOR_TOOL.get(part, part) for part in parts]
    return "|".join(translated)


def _normalized_tool_input(tool_input: Any) -> dict:
    if not isinstance(tool_input, dict):
        return {}
    normalized = dict(tool_input)
    if not normalized.get("file_path"):
        for alias in ("path", "target_file", "absolute_path"):
            if normalized.get(alias):
                normalized["file_path"] = normalized[alias]
                break
    return normalized


def to_claude_payload(
    cursor_payload: dict, claude_event: str, session_id: str
) -> dict:
    """Build the stdin payload the existing Claude hook script expects."""
    payload: dict[str, Any] = {
        "session_id": session_id,
        "hook_event_name": claude_event,
        "cwd": cursor_payload.get("cwd") or "",
        "transcript_path": cursor_payload.get("transcript_path") or "",
    }
    cursor_tool = cursor_payload.get("tool_name") or ""
    if cursor_tool:
        payload["tool_name"] = CURSOR_TO_CLAUDE_TOOL.get(cursor_tool, cursor_tool)
    payload["tool_input"] = _normalized_tool_input(cursor_payload.get("tool_input"))
    if "tool_output" in cursor_payload:
        payload["tool_response"] = cursor_payload.get("tool_output")
    if claude_event == "UserPromptSubmit":
        payload["prompt"] = cursor_payload.get("prompt") or ""
    if claude_event == "Stop":
        payload["stop_hook_active"] = bool(cursor_payload.get("loop_count"))
    return payload


def parse_claude_result(returncode: int, stdout: str, stderr: str) -> dict:
    """Classify one Claude hook run into (denied, reason, text).

    Returns a dict with `denied` (bool), `reason` (deny/block text) and `text`
    (advisory context a Cursor event may carry).
    """
    import json

    result = {"denied": False, "reason": "", "text": ""}
    body = (stdout or "").strip()
    if body:
        try:
            parsed = json.loads(body)
        except ValueError:
            result["text"] = body
        else:
            if isinstance(parsed, dict):
                specific = parsed.get("hookSpecificOutput")
                specific = specific if isinstance(specific, dict) else {}
                if specific.get("permissionDecision") == "deny":
                    result["denied"] = True
                    result["reason"] = str(
                        specific.get("permissionDecisionReason") or "denied by hook"
                    )
                elif parsed.get("decision") == "block":
                    result["denied"] = True
                    result["reason"] = str(parsed.get("reason") or "blocked by hook")
                context = specific.get("additionalContext") or parsed.get(
                    "additionalContext"
                )
                if context:
                    result["text"] = str(context)
            else:
                result["text"] = body
    if returncode == 2 and not result["denied"]:
        result["denied"] = True
        result["reason"] = (stderr or "").strip() or "hook exited 2"
    return result


def to_cursor_output(
    cursor_event: str, denied: bool, reasons: list[str], texts: list[str]
) -> dict:
    """Render the aggregated Claude decisions in the fields this event supports."""
    if cursor_event in PERMISSION_EVENTS:
        if denied:
            message = "\n".join(r for r in reasons if r)
            return {
                "permission": "deny",
                "user_message": message,
                "agent_message": message,
            }
        return {"permission": "allow"}
    merged = [t for t in (reasons + texts) if t]
    if not merged:
        return {}
    body = "\n".join(merged)
    if cursor_event in FOLLOWUP_EVENTS:
        return {"followup_message": body}
    if cursor_event in CONTEXT_EVENTS:
        return {"additional_context": body}
    return {}


def fail_open_output(cursor_event: str) -> dict:
    if cursor_event in PERMISSION_EVENTS:
        return {"permission": "allow"}
    return {}


def fail_closed_output(cursor_event: str, reason: str) -> dict:
    if cursor_event in PERMISSION_EVENTS:
        return {"permission": "deny", "user_message": reason, "agent_message": reason}
    return {}
