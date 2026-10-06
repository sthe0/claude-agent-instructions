"""Judged verdicts about user prompts, shared by every report that counts them.

Difficulty removed: two reports counting the same quantity (a user correction) by
different rules disagree silently. policy-scorecard.py and cost-report.py both
decide "is this human prompt a correction?" through this one path: the
si_feedback_detect prefilter nominates, ``advisor.judge_feedback_signal`` decides,
and a genuine verdict is cached in one file both scripts read and write.

The cache key is the sha256 of the injection-stripped prompt text, unchanged since
the verdicts were first cached, so no stored verdict is orphaned.
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

from agentctl import advisor
from lib.semantic_join import VerdictCache

CORRECTION_VERDICTS_ENV = "POLICY_CORRECTION_VERDICTS"
DEFAULT_CORRECTION_VERDICTS = "~/.local/state/claude-correction-verdicts.json"
JUDGE_BUDGET_ENV = "POLICY_CORRECTION_JUDGE_BUDGET_S"
JUDGE_BUDGET_DEFAULT_S = 300.0
JUDGE_CALL_TIMEOUT_S = 60
JUDGE_KILLSWITCH_ENV = "CLAUDE_SI_FEEDBACK_SEMANTIC"


def correction_cache() -> VerdictCache:
    path = os.environ.get(CORRECTION_VERDICTS_ENV) or os.path.expanduser(DEFAULT_CORRECTION_VERDICTS)
    return VerdictCache(
        path=Path(path), salt="",
        key_fn=lambda texts: hashlib.sha256(texts[0].encode("utf-8")).hexdigest(),
    )


def judge_budget_s() -> float:
    try:
        return float(os.environ[JUDGE_BUDGET_ENV])
    except (KeyError, ValueError):
        return JUDGE_BUDGET_DEFAULT_S


def runner_active(runner) -> bool:
    return runner is not None and os.environ.get(JUDGE_KILLSWITCH_ENV) != "0"


def open_deadline(runner) -> float | None:
    """Monotonic deadline of one refresh's judge budget; None when no judge runs."""
    return time.monotonic() + judge_budget_s() if runner_active(runner) else None


def correction_verdict(stripped: str, *, runner, deadline: float | None) -> bool | None:
    """The judge's verdict on an injection-stripped, prefilter-flagged prompt;
    None when no genuine verdict is available (no runner, budget spent, fail-open).
    Only genuine verdicts are cached, so an unanswered prompt is asked again later."""
    cache = correction_cache()
    cached = cache.get(stripped)
    if cached is not None:
        return cached
    if not runner_active(runner):
        return None
    timeout = JUDGE_CALL_TIMEOUT_S
    if deadline is not None:
        left = deadline - time.monotonic()
        if left < 1:
            return None
        timeout = int(min(timeout, left))
    verdict, reason = advisor.judge_feedback_signal(stripped, runner, timeout=timeout)
    if reason:
        return None
    cache.put(verdict, stripped)
    return verdict
