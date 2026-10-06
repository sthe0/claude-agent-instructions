"""Judged verdicts about user prompts, shared by every report that counts them.

Difficulty removed: two reports counting the same quantity (a user correction) by
different rules disagree silently. policy-scorecard.py and cost-report.py both
decide "is this human prompt a correction?" through this one path: the
si_feedback_detect prefilter nominates, ``advisor.judge_feedback_signal`` decides,
and a genuine verdict is cached in one file both scripts read and write.

The cache key is the sha256 of the injection-stripped prompt text, unchanged since
the verdicts were first cached, so no stored verdict is orphaned.

Whether a prompt asks a question and whether it confirms a resolution are the same
kind of decision: a cheap pattern only nominates (``question_prefilter`` /
``resolution_prefilter``), ``advisor.judge_user_question`` /
``judge_resolution_confirmation`` decide, and each judge keeps its own verdict file
keyed under a salt derived from its prompt, so editing a prompt orphans its verdicts.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path

from agentctl import advisor
from lib import semantic_join
from lib.semantic_join import VerdictCache

CORRECTION_VERDICTS_ENV = "POLICY_CORRECTION_VERDICTS"
DEFAULT_CORRECTION_VERDICTS = "~/.local/state/claude-correction-verdicts.json"
JUDGE_BUDGET_ENV = "POLICY_CORRECTION_JUDGE_BUDGET_S"
JUDGE_BUDGET_DEFAULT_S = 300.0
JUDGE_CALL_TIMEOUT_S = 60
JUDGE_KILLSWITCH_ENV = "CLAUDE_SI_FEEDBACK_SEMANTIC"

QUESTION_VERDICTS_ENV = "POLICY_QUESTION_VERDICTS"
DEFAULT_QUESTION_VERDICTS = "~/.local/state/claude-question-verdicts.json"
RESOLUTION_VERDICTS_ENV = "POLICY_RESOLUTION_VERDICTS"
DEFAULT_RESOLUTION_VERDICTS = "~/.local/state/claude-resolution-verdicts.json"

USER_QUESTION_JUDGE_PROMPT_SHA256 = "5cb7a183d8bdbbfd0c400a2a3babddd4dfc019d45af9d4575f8356dd28c257bc"
USER_QUESTION_JUDGE_SALT = "user-question@" + USER_QUESTION_JUDGE_PROMPT_SHA256
RESOLUTION_JUDGE_PROMPT_SHA256 = "d2547457d2d0fb12e5a40550b8ef18aa5ad17358d2d5bca38d645e90eb29cd91"
RESOLUTION_JUDGE_SALT = "resolution-confirmation@" + RESOLUTION_JUDGE_PROMPT_SHA256

_QUESTION_NOMINATION = re.compile(r"[?？؟]")
_RESOLUTION_NOMINATION = re.compile(
    r"реш(?:ен|ён|и)|так и оставим|подтвержда|готово|all good|"
    r"\bresolved\b|looks good|считаем|lgtm|закрыва|принима|accepted",
    re.IGNORECASE)


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


def is_human_entry(entry: dict) -> bool:
    origin = entry.get("origin")
    return isinstance(origin, dict) and origin.get("kind") == "human"


def runner_active(runner) -> bool:
    return runner is not None and os.environ.get(JUDGE_KILLSWITCH_ENV) != "0"


def open_deadline(runner) -> float | None:
    """Monotonic deadline of one refresh's judge budget; None when no judge runs."""
    return time.monotonic() + judge_budget_s() if runner_active(runner) else None


def question_prefilter(text: str) -> bool:
    """Nominates a prompt that may ask a question; the judge decides."""
    return bool(_QUESTION_NOMINATION.search(text))


def resolution_prefilter(text: str) -> bool:
    """Nominates a prompt that may confirm the task is resolved; the judge decides."""
    return bool(_RESOLUTION_NOMINATION.search(text))


def question_verdicts_path() -> Path:
    return Path(os.environ.get(QUESTION_VERDICTS_ENV) or os.path.expanduser(DEFAULT_QUESTION_VERDICTS))


def resolution_verdicts_path() -> Path:
    return Path(os.environ.get(RESOLUTION_VERDICTS_ENV) or os.path.expanduser(DEFAULT_RESOLUTION_VERDICTS))


def _call_timeout(deadline: float | None) -> int | None:
    """Per-call timeout under the refresh deadline; None when the budget is spent."""
    if deadline is None:
        return JUDGE_CALL_TIMEOUT_S
    left = deadline - time.monotonic()
    if left < 1:
        return None
    return int(min(JUDGE_CALL_TIMEOUT_S, left))


def prompt_verdict(text: str, *, judge, cache, runner, deadline: float | None) -> bool | None:
    """``judge``'s verdict on a prefilter-nominated prompt, through ``cache``; None when
    no genuine verdict is available (no runner, budget spent, fail-open). A fail-open
    answer is neither returned nor cached: ``VerdictCache.put`` does not look at a
    reason, so the genuineness check is here."""
    cached = cache.get(text)
    if cached is not None:
        return cached
    if not runner_active(runner):
        return None
    timeout = _call_timeout(deadline)
    if timeout is None:
        return None
    verdict, reason = judge(text, runner, timeout=timeout)
    if not semantic_join._is_genuine(reason):
        return None
    cache.put(verdict, text)
    return verdict


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
    timeout = _call_timeout(deadline)
    if timeout is None:
        return None
    verdict, reason = advisor.judge_feedback_signal(stripped, runner, timeout=timeout)
    if reason:
        return None
    cache.put(verdict, stripped)
    return verdict
