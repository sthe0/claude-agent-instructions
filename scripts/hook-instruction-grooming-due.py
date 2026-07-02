#!/usr/bin/env python3
"""UserPromptSubmit hook: OFFER instruction-grooming when a file crosses WARN.

`lint-prose-length.py` emits a non-fatal `WARN` line once a governed file
(`CLAUDE.md`, `README.md`, the cursor mirror, any `SKILL.md`/`policy.md`)
reaches WARN_FRACTION of its ceiling — but a linter run only happens on
someone's initiative (a commit, a manual check). Bloat discovered only at
100% (this task's own trigger) is a crisis; this hook turns the WARN signal
into a proactive nudge so grooming happens before the ceiling is hit.

Runs the repo's own `lint-prose-length.py` (never re-implements the ceiling
check), parses its WARN lines, and — for any WARN'd file not already offered
within the debounce window — prints one line instructing the agent to OFFER
(never auto-run) the `instruction-grooming` skill via AskUserQuestion.

Debounce is per-file (a JSON stamp file mapping file -> last-offered ISO
timestamp), not a single whole-hook throttle: two different files can cross
WARN on different days and each deserves its own offer, but a file already
offered this window should not repeat every prompt.

Fail-open throughout: any error (missing linter, subprocess failure, bad
stamp file) is treated as "nothing to offer" and stays silent, so a broken
linter or stale state file can never wedge or spam a prompt. Detection (the
WARN parse + debounce gate) is deterministic; the offer itself, and any
resulting edit, stays the model's judgment via the instruction-grooming skill.

Output goes to stdout (UserPromptSubmit convention — becomes turn context,
mirrors hook-instructions-refresh-due.py). Exit 0 always.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

LINT_TIMEOUT_S = 5
THROTTLE_DAYS = 7

WARN_RE = re.compile(r"^lint-prose-length: WARN — ([^:]+): .*?(\d+)% of limit")


def _repo_root() -> Path:
    return Path(os.environ.get("CLAUDE_INSTRUCTIONS_REPO", str(Path.home() / "claude-agent-instructions")))


def _stamp_path() -> Path:
    return Path.home() / ".local" / "state" / "claude-instruction-grooming.stamp.json"


def _load_stamps() -> dict[str, str]:
    try:
        raw = json.loads(_stamp_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_stamps(stamps: dict[str, str]) -> None:
    try:
        path = _stamp_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(stamps), encoding="utf-8")
    except OSError:
        pass


def _due(stamps: dict[str, str], file: str, now: dt.datetime) -> bool:
    raw = stamps.get(file)
    if raw is None:
        return True
    try:
        prev = dt.datetime.fromisoformat(raw)
    except ValueError:
        return True
    return (now - prev).total_seconds() / 86400 >= THROTTLE_DAYS


def run_linter(repo_root: Path) -> str | None:
    linter = repo_root / "scripts" / "lint-prose-length.py"
    if not linter.is_file():
        return None
    try:
        proc = subprocess.run(
            [sys.executable, str(linter)],
            capture_output=True, text=True, timeout=LINT_TIMEOUT_S, check=False,
        )
    except Exception:
        return None
    return proc.stdout


def parse_warnings(lint_output: str) -> list[tuple[str, str]]:
    """Return (file, pct) pairs for every WARN line, in the order they appear."""
    out = []
    for line in lint_output.splitlines():
        m = WARN_RE.match(line)
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def build_nudge(due: list[tuple[str, str]]) -> str:
    parts = [f"{f} ({pct}% of ceiling)" for f, pct in due]
    return (
        "[instruction-grooming] " + ", ".join(parts) + " past the WARN threshold. "
        "OFFER the `instruction-grooming` skill to the user via AskUserQuestion before "
        "running it — grooming edits still go through the normal plan-approval spine."
    )


def main() -> int:
    try:
        json.load(sys.stdin)  # drain payload; no field needed
    except Exception:
        pass

    lint_output = run_linter(_repo_root())
    if not lint_output:
        return 0

    warnings = parse_warnings(lint_output)
    if not warnings:
        return 0

    now = dt.datetime.now()
    stamps = _load_stamps()
    due = [(f, pct) for f, pct in warnings if _due(stamps, f, now)]
    if not due:
        return 0

    for f, _pct in due:
        stamps[f] = now.isoformat()
    _save_stamps(stamps)

    print(build_nudge(due))
    return 0


if __name__ == "__main__":
    sys.exit(main())
