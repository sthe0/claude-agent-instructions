"""Integration tests for gates.replan_coverage_blockers — PRESERVE half only.

Four regions under test that together prove `replan_coverage_blockers` correctly
delegates to `_semantic_invariants_coverage`:

  1. Substring prefilter (no model): a literal match in stage conditions or
     invariants text clears the gate without any model call.
  2. AGENTCTL_ADVISOR=0 fallback: invariant absent literally → blocker (substring
     result); the suite-wide fixture sets this env var so existing tests are
     unaffected.
  3. Semantic path (model called): an injected runner via monkeypatch decides
     YES/NO, exercised by enabling AGENTCTL_ADVISOR=1 + patching subprocess_runner.
  4. Fail-open: a crashing runner does not add a coverage blocker.
"""
from __future__ import annotations

import pytest

from agentctl import gates
from agentctl.dispatch import RunResult
from agentctl.plan import parse_plan
from agentctl.state import Critique


# ---------------------------------------------------------------------------
# Helpers (mirrors test_replan_coverage_surface.py)
# ---------------------------------------------------------------------------

def _stage(index, *, means="Edit", method="do", conditions=None, invariants=None):
    s = {
        "index": index,
        "title": "s",
        "executor": "in_thread",
        "expected_result_image": "img",
        "done_criterion": "dc",
        "means": means,
        "method": method,
    }
    if conditions is not None:
        s["conditions"] = conditions
    if invariants is not None:
        s["invariants"] = invariants
    return s


def _doc(stages):
    return parse_plan({"meta": {"task_id": "t"}, "stage": stages})


def _critique(**kw):
    base = dict(
        functional_ground="fg",
        replanning_task="rt",
        invariants_to_preserve=[],
        differences_to_remove=[],
    )
    base.update(kw)
    return Critique(**base)


def _yes_runner(argv, **kw):
    return RunResult(0, stdout="YES\n", stderr="")


def _no_runner(argv, **kw):
    return RunResult(0, stdout="NO\n", stderr="")


def _crashing_runner(argv, **kw):
    raise RuntimeError("subprocess exploded")


# ---------------------------------------------------------------------------
# 1. Substring prefilter — literal match → no model call, no blocker
# ---------------------------------------------------------------------------

class TestSubstringPrefilter:
    def test_invariant_in_conditions_passes(self):
        """Literal match in stage conditions clears the gate."""
        doc = _doc([_stage(1, conditions="keep idempotency")])
        crit = _critique(invariants_to_preserve=["keep idempotency"])
        assert gates.replan_coverage_blockers(doc, doc, crit) == []

    def test_invariant_in_stage_invariants_field_passes(self):
        """Literal match in stage invariants field clears the gate."""
        doc = _doc([_stage(1, invariants="keep idempotency")])
        crit = _critique(invariants_to_preserve=["keep idempotency"])
        assert gates.replan_coverage_blockers(doc, doc, crit) == []

    def test_casefold_match_passes(self):
        """Casefold-normalised match hits the prefilter."""
        doc = _doc([_stage(1, conditions="Keep Idempotency here")])
        crit = _critique(invariants_to_preserve=["keep idempotency"])
        assert gates.replan_coverage_blockers(doc, doc, crit) == []


# ---------------------------------------------------------------------------
# 2. AGENTCTL_ADVISOR=0 fallback (the suite-wide default)
# ---------------------------------------------------------------------------

class TestAdvisorOffFallback:
    def test_literal_miss_blocks_with_advisor_off(self, monkeypatch):
        """AGENTCTL_ADVISOR=0 (suite default) + no literal match → blocker."""
        monkeypatch.setenv("AGENTCTL_ADVISOR", "0")
        doc = _doc([_stage(1, conditions="something unrelated")])
        crit = _critique(invariants_to_preserve=["keep idempotency"])
        blockers = gates.replan_coverage_blockers(doc, doc, crit)
        assert blockers, "expected a coverage blocker"
        assert "keep idempotency" in blockers[0]

    def test_literal_hit_passes_with_advisor_off(self, monkeypatch):
        """AGENTCTL_ADVISOR=0 + literal match → prefilter passes (no blocker)."""
        monkeypatch.setenv("AGENTCTL_ADVISOR", "0")
        doc = _doc([_stage(1, conditions="preserve idempotency")])
        crit = _critique(invariants_to_preserve=["preserve idempotency"])
        assert gates.replan_coverage_blockers(doc, doc, crit) == []


# ---------------------------------------------------------------------------
# 3. Semantic path — injected runner via monkeypatch
# ---------------------------------------------------------------------------

class TestSemanticPath:
    def test_yes_runner_clears_blocker_for_paraphrase(self, monkeypatch):
        """When the model says YES, a paraphrase clears the gate."""
        monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
        monkeypatch.setattr(gates._advisor, "subprocess_runner", _yes_runner)
        doc = _doc([_stage(1, conditions="the operation is repeatable")])
        crit = _critique(invariants_to_preserve=["keep idempotency"])
        assert gates.replan_coverage_blockers(doc, doc, crit) == []

    def test_no_runner_adds_blocker(self, monkeypatch):
        """When the model says NO, the gate adds a blocker."""
        monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
        monkeypatch.setattr(gates._advisor, "subprocess_runner", _no_runner)
        doc = _doc([_stage(1, conditions="something unrelated")])
        crit = _critique(invariants_to_preserve=["keep idempotency"])
        blockers = gates.replan_coverage_blockers(doc, doc, crit)
        assert blockers, "expected a coverage blocker"
        assert "keep idempotency" in blockers[0]

    def test_multiple_invariants_one_absent_one_present(self, monkeypatch):
        """Only the absent invariant (model says NO) produces a blocker."""
        call_count = []

        def selective_runner(argv, **kw):
            call_count.append(1)
            # Detect which invariant is being judged from the prompt, delivered via
            # stdin (never argv, so a large plan_text can't hit E2BIG).
            prompt = kw.get("stdin", "")
            if "preserve idempotency" in prompt:
                return RunResult(0, stdout="YES\n", stderr="")
            return RunResult(0, stdout="NO\n", stderr="")

        monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
        monkeypatch.setattr(gates._advisor, "subprocess_runner", selective_runner)
        doc = _doc([_stage(1, conditions="something unrelated")])
        crit = _critique(
            invariants_to_preserve=["preserve idempotency", "keep the retry loop"]
        )
        blockers = gates.replan_coverage_blockers(doc, doc, crit)
        assert len(blockers) == 1
        assert "keep the retry loop" in blockers[0]


# ---------------------------------------------------------------------------
# 4. Fail-open — a crashing runner never adds a blocker
# ---------------------------------------------------------------------------

class TestFailOpen:
    def test_crashing_runner_does_not_block(self, monkeypatch):
        """Runner crash → fail open → no blocker."""
        monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
        monkeypatch.setattr(gates._advisor, "subprocess_runner", _crashing_runner)
        doc = _doc([_stage(1, conditions="unrelated text")])
        crit = _critique(invariants_to_preserve=["keep idempotency"])
        assert gates.replan_coverage_blockers(doc, doc, crit) == []
