#!/usr/bin/env python3
"""Shared long-job launch-pattern scan.

Difficulty removed: two consumers must apply the SAME "does this Bash command
launch a long-running external job?" predicate and never drift apart —

  - the PreToolUse advisory `hook-long-job-arm.py` nudges at launch time;
  - the turn-end guardian in `hook-turn-end-gate.py` blocks a turn that launched
    such a job without arming a harness-tracked auto-wake.

Keeping detect() (and its regexes / orchestrator-list resolution) in ONE
importable module — mirroring timer_arm_detect.py — makes that divergence
impossible. `hook-long-job-arm.py` has a hyphenated name and cannot be imported,
so the shared logic lives here and the hook re-exports it.

The orchestrator name list is operator-configurable so this works in any org:
set `long_job_orchestrators=name1,name2` (comma/space-separated) in the system
config root's `agent-identity.local`. When the key is absent the built-in default
(Yandex orchestrators) is used, so an unconfigured machine behaves unchanged.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

NOHUP_RE = re.compile(r"\bnohup\b")

# Built-in default orchestrator names (Yandex). An unconfigured machine uses these,
# so behaviour is byte-identical to before the list became configurable.
DEFAULT_ORCHESTRATORS = ("nirvana", "sandbox", "reactor", "vh3", "hitman", "yt")


def _orchestrator_names(identity_path=None) -> tuple[str, ...]:
    """Resolve the orchestrator name list. Reads `long_job_orchestrators=`
    (comma/space-separated) from the resolved agent-identity.local, falling back to
    DEFAULT_ORCHESTRATORS. Fail-open: any error yields the default (a hook must
    never crash the Bash call it advises on)."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from difficulty_channel.authority import read_local_identity, LOCAL_IDENTITY_PATH

        raw = read_local_identity(identity_path or LOCAL_IDENTITY_PATH).get(
            "long_job_orchestrators", ""
        )
        names = tuple(n for n in re.split(r"[,\s]+", raw.strip()) if n)
        return names or DEFAULT_ORCHESTRATORS
    except Exception:
        return DEFAULT_ORCHESTRATORS


def _build_tool_re(names=None) -> "re.Pattern[str]":
    """Compile the orchestrator-name alternation from the resolved (or given) list."""
    alts = "|".join(re.escape(n) for n in (names or _orchestrator_names()))
    return re.compile(rf"\b({alts})\b", re.IGNORECASE)


# orchestration tool + a launch verb, in either order, within the command
TOOL_RE = _build_tool_re()
VERB_RE = re.compile(
    r"\b(start[-_]?op|start|launch|submit|create|run|exec|operation|vanilla)\b",
    re.IGNORECASE,
)


def detect(cmd: str) -> str | None:
    """Return a short reason string if the command looks like a long-job launch."""
    if NOHUP_RE.search(cmd):
        return "detached process (nohup)"
    if TOOL_RE.search(cmd) and VERB_RE.search(cmd):
        tool = TOOL_RE.search(cmd).group(1).lower()
        return f"orchestrator launch ({tool})"
    return None
