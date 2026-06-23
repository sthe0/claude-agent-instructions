#!/usr/bin/env python3
"""Guardrail for the versioned settings base (settings/base.json).

The base is merged into every machine's ~/.claude/settings.json by
apply-settings.sh, so a `git pull` can change what the agent runs without a
prompt. To keep that safe, every `permissions.allow` entry in the base MUST be
side-effect-free (read-only). This check fails on anything that could mutate
state, so a write/exec permission can never ride into the base unnoticed.

Allowed entry classes:
- Read(...) / WebSearch / WebFetch(domain:...)
- read-only MCP tools: method (after the last `__`) starts with
  get/list/search/describe, or contains "search"
- Bash(<verb> ...) where <verb> is in READONLY_BASH
- Bash(git <sub> ...) / Bash(arc <sub> ...) where <sub> is read-only

Exit code 1 if any entry falls outside these classes.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentctl.classify import READONLY_BASH, READONLY_GIT, READONLY_ARC, classify_action  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE = REPO_ROOT / "settings" / "base.json"

READONLY_PYTHON3 = ('-c "', "-m json.tool")


def _bash_ok(inner: str) -> bool:
    # Bash entries are exact commands or "<prefix>:*" arg-wildcards; strip ":*".
    inner = inner.strip()
    if inner.endswith(":*"):
        inner = inner[:-2].strip()
    if inner == "git" or inner.startswith("git "):
        sub = inner[len("git"):].strip().split()
        subverb = sub[0] if sub else None
        return classify_action("Bash", "git", subverb) == "side-effect-free"
    if inner == "arc" or inner.startswith("arc "):
        sub = inner[len("arc"):].strip().split()
        subverb = sub[0] if sub else None
        return classify_action("Bash", "arc", subverb) == "side-effect-free"
    parts = inner.split()
    verb = parts[0] if parts else ""
    if verb == "python3":
        rest = inner[len("python3"):].strip()
        return rest.startswith(READONLY_PYTHON3)
    return classify_action("Bash", verb) == "side-effect-free"


def _mcp_ok(name: str) -> bool:
    return classify_action(name) == "side-effect-free"


def entry_ok(entry: str) -> bool:
    if entry == "WebSearch" or entry.startswith(("Read(", "WebFetch(")):
        return True
    if entry.startswith("mcp__"):
        return _mcp_ok(entry)
    m = re.fullmatch(r"Bash\((.*)\)", entry)
    if m:
        return _bash_ok(m.group(1))
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--staged", action="store_true")  # base is small; scan always
    parser.parse_args(argv)

    if not BASE.exists():
        print("lint-settings-base: OK — no settings/base.json")
        return 0
    try:
        data = json.loads(BASE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"lint-settings-base: FAIL — invalid JSON ({exc})")
        return 1

    allow = (data.get("permissions") or {}).get("allow") or []
    bad = [e for e in allow if not (isinstance(e, str) and entry_ok(e))]
    if bad:
        print(f"lint-settings-base: FAIL — {len(bad)} non-read-only entry(ies) in base.json:")
        for e in bad:
            print(f"  {e}")
        return 1
    print(f"lint-settings-base: OK — {len(allow)} read-only allow entry(ies)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
