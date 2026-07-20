#!/usr/bin/env python3
"""UserPromptSubmit hook: detect user feedback about agent behavior and emit
a system-context reminder to consider invoking the `self-improvement` skill.

Rule (CLAUDE.md § When the user corrects agent behavior): invoke the skill
when the user corrects/rejects/clarifies your action, states a principle,
evaluates agent quality, proposes changes to instructions/skills/memory/
workflow, or reminds you that the skill should have run.

This hook lifts the most common patterns from agent recall to a deterministic
scan. The skill decision still belongs to the agent — the hook only nudges.

Detection is shared with the end-of-turn `Stop` gate
(`hook-turn-end-gate.py`): both import `find_signals` from
`si_feedback_detect` so the advisory nudge and the gate can never drift apart.
See that module for the tier design and the precision rationale.

Asymmetry with the Stop gate (deliberate): this reminder shares ONLY the
deterministic half — the Tier-1 'self-improvement' proper-name mention that
`find_signals` returns. It does NOT consult the model-backed semantic judge that
the Stop gate additionally uses for the natural-language feedback that carries no
Tier-1 literal. This hook runs on UserPromptSubmit, the latency-critical prompt
path: a per-prompt `claude -p` judge call would add seconds to every user turn and
risk hook->claude->hook recursion. The Stop gate, running once at turn end, can
afford the judge behind its precondition gate; the instant nudge stays judge-free.

Stateless: no per-session suppression. Same simplicity as hook-tracker-reminder.py.
Revisit only if false-positive noise is observed.

Scope:
  - Reads the UserPromptSubmit JSON on stdin.
  - Emits one stdout line on a feedback signal (appended to model context).
  - Exit 0 always — including on malformed/empty input.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Make the sibling shared detector importable whether this hook is run directly
# (scripts/ on sys.path[0]) or loaded via importlib in tests (which does not add
# the script's dir to sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from si_feedback_detect import find_signals  # noqa: E402


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    prompt = payload.get("prompt") or ""
    if not isinstance(prompt, str) or not prompt.strip():
        return 0

    signals = find_signals(prompt)
    if not signals:
        return 0

    print(
        f"[self-improvement-reminder] User prompt contains a feedback signal — "
        f"{'; '.join(signals)}. Consider invoking the `self-improvement` skill "
        f"this turn if this is agent-behavior feedback."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
