#!/usr/bin/env python3
"""PreToolUse hook: warn when the same Bash command is executed 3+ times.

Per CLAUDE.md § When the work is stuck: repeated identical commands are a
difficulty signal — invoke overcome-difficulty instead of retrying blindly.
This hook makes that rule mechanical by counting Bash command repetitions
in a session-scoped state file.

Commands are normalized before hashing:
  - Strip leading/trailing whitespace
  - Collapse internal whitespace runs to a single space
  - Drop the trailing '; exit 0' / '|| true' noise sometimes appended

The hook fires once per unique repeated command (not on every subsequent
repeat) to avoid flooding the context.

State file: /tmp/cc-retry-<session_id>.json
  {"<cmd_hash>": {"count": <int>, "nudged": <bool>, "cmd_preview": "<str>"}}

Exit 0 always; stdout is context for the model before the tool runs.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

THRESHOLD = 3

NOISE_RE = re.compile(r"\s*(;\s*(exit\s+0|true|:)\s*)+$")
WHITESPACE_RE = re.compile(r"\s+")

# Very short/cheap commands that are fine to repeat — don't nudge for these.
# Core lists the org-neutral ones; a deployment adds its own VCS's read verbs
# via `retry_detector_allowlist=` in the config root's agent-identity.local.
# Comma-separated, not whitespace-separated like the other list-valued identity
# keys: a command prefix contains spaces ("hg status").
_CORE_ALLOWLIST = (
    "ls", "pwd", "echo", "date", "whoami", "git status", "git log", "git diff",
    r"cat\s", r"head\s", r"tail\s", r"wc\s", r"grep\s", r"rg\s", r"find\s",
    r"which\s", "python3 -c", r"jq\s",
)


def _extra_allowlist(identity_path=None) -> tuple[str, ...]:
    """Machine-local extra command prefixes, regex-escaped (they are literal
    commands, not patterns). Fail-open: any error yields no extras."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from difficulty_channel.authority import read_local_identity, LOCAL_IDENTITY_PATH

        raw = read_local_identity(identity_path or LOCAL_IDENTITY_PATH).get(
            "retry_detector_allowlist", ""
        )
        return tuple(re.escape(p) for p in re.split(r"\s*,\s*", raw.strip()) if p)
    except Exception:
        return ()


def build_allowlist_re(extra=None) -> "re.Pattern[str]":
    resolved = _extra_allowlist() if extra is None else tuple(re.escape(p) for p in extra)
    return re.compile(r"^(" + "|".join(_CORE_ALLOWLIST + resolved) + r")", re.IGNORECASE)


ALLOWLIST_RE = build_allowlist_re()


def normalize(cmd: str) -> str:
    cmd = NOISE_RE.sub("", cmd)
    cmd = WHITESPACE_RE.sub(" ", cmd).strip()
    return cmd


def cmd_hash(cmd: str) -> str:
    return hashlib.sha1(cmd.encode()).hexdigest()[:16]


def state_path(session_id: str) -> Path:
    safe = "".join(c for c in (session_id or "nosession") if c.isalnum() or c in "-_")
    return Path(f"/tmp/cc-retry-{safe or 'nosession'}.json")


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_state(path: Path, state: dict) -> None:
    try:
        path.write_text(json.dumps(state))
    except Exception:
        pass


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    if payload.get("tool_name") != "Bash":
        return 0

    tool_input = payload.get("tool_input") or {}
    raw_cmd = tool_input.get("command") or ""
    session_id = payload.get("session_id") or ""

    if not raw_cmd.strip():
        return 0

    cmd = normalize(raw_cmd)

    if ALLOWLIST_RE.match(cmd):
        return 0

    # Skip very short commands (< 20 chars) — too noisy to track
    if len(cmd) < 20:
        return 0

    sp = state_path(session_id)
    state = load_state(sp)

    h = cmd_hash(cmd)
    entry = state.get(h, {"count": 0, "nudged": False, "cmd_preview": cmd[:120]})
    entry["count"] += 1
    state[h] = entry
    save_state(sp, state)

    if entry["count"] < THRESHOLD or entry.get("nudged"):
        return 0

    entry["nudged"] = True
    state[h] = entry
    save_state(sp, state)

    preview = entry["cmd_preview"]
    print(
        f"[retry-detector] Same Bash command executed {entry['count']}× this session:\n"
        f"  {preview!r}\n"
        "Per CLAUDE.md § When the work is stuck: repeated identical commands are a difficulty.\n"
        "  → Stop retrying. Invoke overcome-difficulty (declaration → investigation → critique).\n"
        "  Expected/Actual/Mismatch → replanning task → then resume on the corrected plan."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
