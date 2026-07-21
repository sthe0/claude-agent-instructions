#!/usr/bin/env python3
"""SessionStart detector: is the canon read-only guard actually wired live?

Difficulty removed: hook-guard-canon-readonly.py can be present in the repo and
declared in install-reminder-hooks.sh yet completely DEAD in a session, because
the installer historically ran only on the one-time legacy-migration path — so a
hook added after this machine was onboarded never reached live settings.json and
NOTHING detected the absence. The guard silently not-enforcing is the worst
failure mode for a read-only guarantee: canon looks protected but isn't. This
check closes the whole "versioned-but-not-applied" class for the guard by reading
live settings at every session start and loudly naming the remediation when the
guard is missing from EITHER PreToolUse chain (Edit|Write or Bash) or is wired
but points at a script path that no longer exists on disk (a stale worktree path).

Non-blocking and fail-open by construction: this is a SessionStart hook, which
cannot hard-block, and any error (missing/unreadable settings, malformed JSON)
returns quietly — a detector that wedges session start would be worse than the
gap it reports. It only ever writes a warning to stderr; it never denies.

Settings source: config_root.agent_home()/settings.json, overridable via
$CLAUDE_CANON_GUARD_SETTINGS (test seam, mirroring the hook's other env seams).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import config_root  # noqa: E402

GUARD_BASENAME = "hook-guard-canon-readonly.py"


def _settings_path() -> Path:
    override = os.environ.get("CLAUDE_CANON_GUARD_SETTINGS")
    if override:
        return Path(override).expanduser()
    return config_root.agent_home() / "settings.json"


def _guard_commands_for(groups: list, matcher_needle: str) -> list[str]:
    """Every wired command in the PreToolUse groups whose matcher contains
    `matcher_needle` and whose command runs the canon guard."""
    out: list[str] = []
    for grp in groups:
        if not isinstance(grp, dict):
            continue
        if matcher_needle not in (grp.get("matcher") or ""):
            continue
        for hook in grp.get("hooks", []) or []:
            if not isinstance(hook, dict):
                continue
            cmd = hook.get("command", "") or ""
            if GUARD_BASENAME in cmd:
                out.append(cmd)
    return out


def _script_path(command: str) -> str:
    """The script path a hook command runs — its first whitespace-delimited
    token (the rest are args)."""
    return command.split()[0] if command else ""


def check(settings: dict) -> list[str]:
    """Return a list of problems (empty = the guard is correctly wired). Problems:
    the guard is absent from the Edit|Write chain, absent from the Bash chain, or
    a wired guard command points at a script path that does not exist."""
    hooks = settings.get("hooks") or {}
    pre = hooks.get("PreToolUse") or []
    if not isinstance(pre, list):
        return ["live settings.json has a malformed PreToolUse section"]

    problems: list[str] = []
    edit_cmds = _guard_commands_for(pre, "Edit")
    bash_cmds = _guard_commands_for(pre, "Bash")
    if not edit_cmds:
        problems.append("canon guard NOT wired in the PreToolUse Edit|Write chain")
    if not bash_cmds:
        problems.append("canon guard NOT wired in the PreToolUse Bash chain")
    for cmd in edit_cmds + bash_cmds:
        path = _script_path(cmd)
        if path and not os.path.exists(path):
            problems.append(f"canon guard wired to a missing script path: {path}")
    return problems


def main() -> int:
    try:
        path = _settings_path()
        settings = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(settings, dict):
            return 0
        problems = check(settings)
    except Exception:
        return 0

    if problems:
        print(
            "\n"
            "================================================================\n"
            "  CANON READ-ONLY GUARD IS NOT FULLY WIRED — canon may be WRITABLE\n"
            "================================================================",
            file=sys.stderr,
        )
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print(
            "  Remediate now (idempotent): run install-reminder-hooks.sh from the\n"
            "  canonical instructions repo, e.g.\n"
            "    bash ~/claude-agent-instructions/scripts/install-reminder-hooks.sh\n"
            "================================================================",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
