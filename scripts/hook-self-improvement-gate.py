#!/usr/bin/env python3
"""Stop hook: end-of-turn GATE enforcing the self-improvement discipline.

Difficulty removed: the advisory `UserPromptSubmit` reminder
(`hook-self-improvement-reminder.py`) merely prints a nudge, which the model can
(and repeatedly did) ignore while absorbed in a technical sub-thread — so a turn
that carried agent-behavior feedback ends silently with neither `self-improvement`
nor `overcome-difficulty` engaged. This Stop hook turns that advisory into a
structural turn-boundary check: if the last user message carried a feedback
signal and neither skill was invoked this turn, it BLOCKS the stop once, forcing
the model to either engage the skill or explicitly state why it does not apply.

Contract (Claude Code Stop hook):
  - stdin: JSON payload with `transcript_path` and `stop_hook_active`.
  - `stop_hook_active` True → exit 0 immediately (loop guard: never block twice
    inside the same stop cycle).
  - On a block: print {"decision":"block","reason":"..."} on stdout, exit 0.
  - Otherwise: exit 0 with no output.

Bounding the cost (never wedge the session):
  - The `stop_hook_active` guard caps a single stop cycle to one extra turn.
  - A durable per-message marker (hash of session + triggering user text under
    <agent-home>/state/si-gate/) makes the SAME triggering message block at most
    once, even across stop cycles where `stop_hook_active` is not set.
  - Fail-open everywhere: any malformed / empty / unreadable input, or any
    unexpected error, results in exit 0 with no block.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

# Make the sibling shared detector and lib/ importable whether this hook is run
# directly (scripts/ on sys.path[0]) or loaded via importlib in tests.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from si_feedback_detect import find_signals  # noqa: E402

try:
    from lib import config_root  # noqa: E402
except Exception:  # pragma: no cover - fail-open if the resolver is unavailable
    config_root = None

# Skills whose invocation this turn satisfies the discipline.
_SATISFYING_SKILLS = {"self-improvement", "overcome-difficulty"}


def _state_dir() -> Path:
    """Durable marker dir: <agent-home>/state/si-gate/ (created on demand)."""
    if config_root is not None:
        try:
            home = config_root.agent_home()
        except Exception:
            home = Path.home() / ".claude-agent"
    else:
        home = Path.home() / ".claude-agent"
    return home / "state" / "si-gate"


def _iter_transcript(path: Path):
    """Yield parsed JSON objects from a JSONL transcript, skipping bad lines."""
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _user_text(msg: dict) -> str:
    """Extract the human-authored text of a user message, '' for tool_result
    turns (whose content carries no `text` block)."""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text") or "")
        return "\n".join(parts)
    return ""


def _skill_engaged_in(msg: dict) -> bool:
    """True if an assistant message invoked a satisfying skill via a tool_use."""
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "tool_use":
            continue
        tool_input = item.get("input") or {}
        if not isinstance(tool_input, dict):
            tool_input = {}
        candidates = {
            item.get("name"),
            tool_input.get("skill"),
            tool_input.get("subagent_type"),
        }
        if candidates & _SATISFYING_SKILLS:
            return True
    return False


def analyze(entries: list[dict]) -> tuple[str, bool]:
    """Return (last_user_text, skill_engaged_after_it) for the current turn.

    The current turn is delimited by the last user entry that carries human
    text; assistant messages after it are this turn's response.
    """
    last_user_idx = -1
    last_user_text = ""
    for i, entry in enumerate(entries):
        msg = entry.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = _user_text(msg)
        if text.strip():
            last_user_idx = i
            last_user_text = text

    if last_user_idx < 0:
        return "", False

    engaged = False
    for entry in entries[last_user_idx + 1:]:
        msg = entry.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        if _skill_engaged_in(msg):
            engaged = True
            break
    return last_user_text, engaged


def _marker_path(session_key: str, user_text: str) -> Path:
    digest = hashlib.sha256(
        (session_key + "\0" + user_text).encode("utf-8")
    ).hexdigest()[:32]
    return _state_dir() / digest


def decide(payload: dict) -> dict | None:
    """Core decision. Returns a block-directive dict, or None to allow. Never
    raises — callers rely on fail-open."""
    if payload.get("stop_hook_active"):
        return None

    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    path = Path(transcript_path).expanduser()
    if not path.exists():
        return None

    try:
        entries = list(_iter_transcript(path))
    except OSError:
        return None
    if not entries:
        return None

    user_text, engaged = analyze(entries)
    if not user_text:
        return None

    signals = find_signals(user_text)
    if not signals or engaged:
        return None

    # Block-once dedup, keyed on (session, triggering user text).
    marker = _marker_path(transcript_path, user_text)
    try:
        if marker.exists():
            return None
    except OSError:
        pass  # cannot stat — rely on stop_hook_active guard, still block once
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except OSError:
        pass  # best-effort; stop_hook_active still caps the loop

    signal = "; ".join(signals)
    reason = (
        f"The user's message carried an agent-behavior-feedback signal "
        f"({signal}), but neither the self-improvement nor the "
        f"overcome-difficulty skill was engaged this turn. Invoke the "
        f"self-improvement skill now, or explicitly state in your reply why it "
        f"does not apply."
    )
    return {"decision": "block", "reason": reason}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        directive = decide(payload)
    except Exception:
        return 0  # fail-open — a hook must never wedge the session
    if directive is not None:
        print(json.dumps(directive, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
