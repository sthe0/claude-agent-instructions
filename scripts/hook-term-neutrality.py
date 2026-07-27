#!/usr/bin/env python3
"""PreToolUse ADVISORY: warn (never block) when a Write/Edit would introduce a term-ruleset hit.

Advisory by design: the hard gate lives at commit-msg / difficulty-filing
time, where a fix is still cheap and reversible; a mid-edit pattern match is
a high-recall prefilter, not authority to deny a tool call
(memory-global/leaves/regex-not-for-semantic-classification.md). Always
exits 0 — findings are surfaced only as printed text.

Registered via scripts/install-reminder-hooks.sh's DESIRED list (PreToolUse
Edit|Write), not settings/base.json directly (hooks are machine-local
settings, merged in by that installer).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib import config_root  # noqa: E402
from lib import term_ruleset as tr  # noqa: E402

REPO_ROOT = SCRIPTS_DIR.parent


def _repo_relative(file_path: str) -> str | None:
    try:
        return str(Path(file_path).resolve().relative_to(REPO_ROOT))
    except ValueError:
        return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}
    file_path = tool_input.get("file_path", "") or ""
    if not file_path:
        return 0

    if tool == "Write":
        content = tool_input.get("content", "") or ""
    elif tool == "Edit":
        content = tool_input.get("new_string", "") or ""
    else:
        return 0

    try:
        rulesets = tr.discover_rulesets(
            agent_home=config_root.agent_home(),
            project_dir=REPO_ROOT,
            guarded_repo_root=REPO_ROOT,
        )
    except tr.RulesetError:
        return 0  # advisory: never let a malformed ruleset block editing
    if not rulesets:
        return 0

    relpath = _repo_relative(file_path) or file_path
    hits = tr.scan(content, rulesets, path=relpath)
    if not hits:
        return 0

    print(f"[term-neutrality] {len(hits)} potential org-internal term hit(s) in {relpath}:")
    for h in hits:
        print(f"  {h.format()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
