"""Unit tests for gates._semantic_invariants_coverage.

Five branches under test — each is an independent probe of the decision tree:

  1. substring-match short-circuit: prefilter hits → runner is never called.
  2. paraphrase pass:  prefilter misses, model says YES → covered (True).
  3. paraphrase fail:  prefilter misses, model says NO  → not covered (False).
  4. unparseable response: model returns neither YES nor NO → fail open (True).
  5. advisor off env var: AGENTCTL_ADVISOR=0, no explicit runner → falls back to
     substring result (False on miss, True on hit).

Tests that exercise the model path inject an explicit runner so the suite-wide
AGENTCTL_ADVISOR=0 isolation fixture (conftest._advisor_off_by_default) does not
suppress the call.  The runner is a simple lambda that returns a RunResult; tests
that assert runner-not-called use a raising sentinel instead.
"""
from __future__ import annotations

import os

import pytest

from agentctl import gates
from agentctl.dispatch import RunResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_semantic = gates._semantic_invariants_coverage


def _yes_runner(argv, **kw):
    return RunResult(0, stdout="YES\n", stderr="")


def _no_runner(argv, **kw):
    return RunResult(0, stdout="NO\n", stderr="")


def _unparseable_runner(argv, **kw):
    return RunResult(0, stdout="MAYBE something\n", stderr="")


def _empty_runner(argv, **kw):
    return RunResult(0, stdout="", stderr="")


def _raising_runner(argv, **kw):
    raise RuntimeError("runner must not have been called")


def _timeout_runner(argv, **kw):
    import subprocess
    raise subprocess.TimeoutExpired(argv, timeout=20)


# ---------------------------------------------------------------------------
# 1. Substring-match short-circuit
# ---------------------------------------------------------------------------

class TestSubstringShortCircuit:
    def test_exact_match_returns_true_without_model_call(self):
        """Literal casefold+whitespace match hits the fast path: runner never invoked."""
        result = _semantic("keep idempotency", "keep idempotency and other stuff",
                           runner=_raising_runner)
        assert result is True

    def test_casefold_match_skips_runner(self):
        result = _semantic("Keep Idempotency", "keep idempotency here",
                           runner=_raising_runner)
        assert result is True

    def test_whitespace_normalised_match_skips_runner(self):
        result = _semantic("  keep   idempotency  ", "keep idempotency",
                           runner=_raising_runner)
        assert result is True


# ---------------------------------------------------------------------------
# 2. Paraphrase pass
# ---------------------------------------------------------------------------

class TestParaphrasePass:
    def test_model_yes_returns_true(self):
        """Prefilter misses; model says YES → covered."""
        calls = []

        def counting_yes(argv, **kw):
            calls.append(argv)
            return RunResult(0, stdout="YES\n", stderr="")

        result = _semantic("preserve idempotency", "the operation is repeatable",
                           runner=counting_yes)
        assert result is True
        assert len(calls) == 1

    def test_yes_with_trailing_text_on_first_line(self):
        """The prompt asks for YES/NO on the first line; extra words are tolerated."""
        runner = lambda argv, **kw: RunResult(0, stdout="YES because reasons\n", stderr="")
        result = _semantic("absent phrase", "unrelated text", runner=runner)
        assert result is True


# ---------------------------------------------------------------------------
# 3. Paraphrase fail
# ---------------------------------------------------------------------------

class TestParaphraseFail:
    def test_model_no_returns_false(self):
        """Prefilter misses; model says NO → not covered."""
        result = _semantic("keep idempotency", "the plan changed the retry loop",
                           runner=_no_runner)
        assert result is False

    def test_no_with_trailing_text_on_first_line(self):
        runner = lambda argv, **kw: RunResult(0, stdout="NO it's gone\n", stderr="")
        result = _semantic("preserve cache", "nothing relevant", runner=runner)
        assert result is False

    def test_nonzero_exit_fails_open(self):
        """Non-zero exit from the runner is an error path — fail open."""
        runner = lambda argv, **kw: RunResult(1, stdout="NO\n", stderr="err")
        result = _semantic("absent", "unrelated", runner=runner)
        assert result is True


# ---------------------------------------------------------------------------
# 4. Fail-open on every error class
# ---------------------------------------------------------------------------

class TestFailOpen:
    def test_unparseable_response_fails_open(self):
        result = _semantic("x", "unrelated", runner=_unparseable_runner)
        assert result is True

    def test_empty_output_fails_open(self):
        result = _semantic("x", "unrelated", runner=_empty_runner)
        assert result is True

    def test_crash_fails_open(self):
        def crashing(argv, **kw):
            raise ValueError("subprocess exploded")

        result = _semantic("x", "unrelated", runner=crashing)
        assert result is True

    def test_timeout_fails_open(self):
        result = _semantic("x", "unrelated", runner=_timeout_runner)
        assert result is True


# ---------------------------------------------------------------------------
# 5. Advisor-off env var (AGENTCTL_ADVISOR=0)
# ---------------------------------------------------------------------------

class TestAdvisorOff:
    def test_advisor_off_substring_miss_falls_back_to_false(self, monkeypatch):
        """AGENTCTL_ADVISOR=0 with no explicit runner: skip the model, return the
        substring result (False for a miss).  The suite sets this env var by default
        so existing coverage tests continue to work without changes."""
        monkeypatch.setenv("AGENTCTL_ADVISOR", "0")
        result = _semantic("genuinely absent invariant", "unrelated plan text")
        assert result is False

    def test_advisor_off_substring_hit_still_returns_true(self, monkeypatch):
        """A literal substring match is found even with advisor off."""
        monkeypatch.setenv("AGENTCTL_ADVISOR", "0")
        result = _semantic("keep it", "we must keep it in the plan")
        assert result is True

    def test_explicit_runner_overrides_advisor_off(self, monkeypatch):
        """When a runner is injected explicitly it is used regardless of AGENTCTL_ADVISOR."""
        monkeypatch.setenv("AGENTCTL_ADVISOR", "0")
        result = _semantic("paraphrase absent", "unrelated", runner=_yes_runner)
        assert result is True
