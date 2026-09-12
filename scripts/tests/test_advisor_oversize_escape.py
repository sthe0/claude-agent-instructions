"""Regression tests for ESCAPE_ADVISOR_OVERSIZE (E2BIG-class subprocess failures).

Before this, enumerate_questions_health's `except Exception` arm swallowed E2BIG
with an empty stderr string, so classify_runner_failure classified it as the
generic ESCAPE_ADVISOR_ERROR — the same bucket as an ordinary runner crash. The
oversize escape needs its own reason (advisor_oversize) so a fleet-wide rise in
this bucket names a split-the-plan work item rather than a runner-health alarm —
the two have different operators and different fixes.

Covered here:
  - classify_runner_failure returns ESCAPE_ADVISOR_OVERSIZE on the E2BIG stderr
    marker that enumerate_questions_health now preserves;
  - the exc= path reaches the same reason without needing the marker in text;
  - the fold (via cmd_approve) sets enumeration_refused_oversize=True in the bag
    when the sidecar carries E2BIG stderr;
  - cmd_question_list --format md surfaces the distinguishing string when the flag
    is set.
"""
from __future__ import annotations

import errno
import os
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import advisor, cli, enumerate_sidecar, plugins, plugins_premise, premise
from agentctl.plan import load_plan
from agentctl.state import SessionState, WeightClass


def ns(**kw):
    return Namespace(**kw)


# ---------------------------------------------------------------------------
# classify_runner_failure — E2BIG paths
# ---------------------------------------------------------------------------

class TestClassifyRunnerFailureOversize:
    """ESCAPE_ADVISOR_OVERSIZE is the fourth recognised reason: produced when the
    plan's prompt text exceeds ARG_MAX in the judge subprocess's argv. It must land
    in its own bucket so a fleet-wide rise names a split-the-plan work item rather
    than a runner-health alarm."""

    def test_e2big_stderr_marker_returns_oversize(self):
        """The marker is what enumerate_questions_health writes into the sidecar
        stderr field when it catches OSError(E2BIG).  classify_runner_failure must
        read it back from THAT string alone — the round-trip path via the sidecar
        file, where the live exception is gone."""
        assert advisor.classify_runner_failure(
            f"{advisor._E2BIG_STDERR_MARKER}: [Errno 7] Argument list too long"
        ) == premise.ESCAPE_ADVISOR_OVERSIZE

    def test_e2big_marker_alone_suffices(self):
        """The marker string need not be wrapped in a longer OS message; the
        substring check must fire on a bare occurrence too."""
        assert advisor.classify_runner_failure(
            advisor._E2BIG_STDERR_MARKER
        ) == premise.ESCAPE_ADVISOR_OVERSIZE

    def test_e2big_oserror_via_exc_returns_oversize(self):
        """The exc= path is for call sites that have the live exception object
        before it is serialised to a sidecar.  It must reach ESCAPE_ADVISOR_OVERSIZE
        directly, without needing the marker in text."""
        e = OSError(errno.E2BIG, os.strerror(errno.E2BIG))
        assert advisor.classify_runner_failure("", exc=e) == premise.ESCAPE_ADVISOR_OVERSIZE

    def test_e2big_exc_wins_even_when_stderr_says_something_else(self):
        """A live E2BIG exception must not be overridden by unrelated text in stderr.
        The exc= branch fires first."""
        e = OSError(errno.E2BIG, os.strerror(errno.E2BIG))
        assert advisor.classify_runner_failure(
            "advisor timed out after 480s", exc=e
        ) == premise.ESCAPE_ADVISOR_OVERSIZE

    def test_non_e2big_oserror_exc_falls_through_to_stderr_check(self):
        """Only E2BIG is handled by the exc= path; a different errno must still
        yield the catch-all."""
        e = OSError(errno.ENOENT, os.strerror(errno.ENOENT))
        assert advisor.classify_runner_failure("", exc=e) == premise.ESCAPE_ADVISOR_ERROR

    def test_oversize_wins_ahead_of_the_catch_all_on_marker_only(self):
        """No other matching pattern exists for the marker text — it must never fall
        through to the generic branch."""
        result = advisor.classify_runner_failure(
            f"execve failed: {advisor._E2BIG_STDERR_MARKER}"
        )
        assert result == premise.ESCAPE_ADVISOR_OVERSIZE
        assert result != premise.ESCAPE_ADVISOR_ERROR


# ---------------------------------------------------------------------------
# fold sets enumeration_refused_oversize in the bag
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _premise_armed(monkeypatch):
    """Override conftest's suite-wide AGENTCTL_PREMISE=0 force-off."""
    monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)


def _to_plan_ready(store, sid, plan):
    """Drive sid to PLAN_READY with the premise plugin active and order covered."""
    cli.cmd_start(ns(session=sid, task="oversize-test", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    assert "premise" in store.load(sid).plugins
    cli.cmd_order_raise(ns(session=sid, id="O1", element="the order this plan answers"),
                        store=store)
    cli.cmd_order_dispose(ns(session=sid, id="O1", as_="covered", stage=1, reason=""),
                          store=store)


class TestFoldEnumerationOversize:
    """The fold path (cmd_approve reading a background sidecar) must surface the
    enumeration_refused_oversize flag when the sidecar's stderr carries the E2BIG
    marker — so question-list --format md can name the split-the-plan action."""

    def _e2big_sidecar(self, sid, digest, plan, tmp_root):
        sidecar_root = tmp_root / "sidecars"
        e2big_stderr = f"{advisor._E2BIG_STDERR_MARKER}: [Errno 7] Argument list too long"
        enumerate_sidecar.write(sid, digest, {
            "runner_ok": False,
            "pairs": [],
            "stderr": e2big_stderr,
            "content_digest": digest,
            "plan_path": plan,
        }, root=sidecar_root)
        return sidecar_root

    def test_fold_sets_oversize_flag_on_e2big_sidecar(
            self, store, fixtures_dir, tmp_path, monkeypatch):
        """After the fold fires, bag["enumeration_refused_oversize"] is True and the
        fold path's own log entry records runner_ok=False."""
        monkeypatch.setenv("CLAUDE_AGENT_HOME", str(tmp_path / "agent-home"))
        sid = "fold-oversize"
        plan = str(fixtures_dir / "plan_two_stage.toml")
        _to_plan_ready(store, sid, plan)

        digest = plugins_premise._plan_content_digest(load_plan(plan))
        sidecar_root = self._e2big_sidecar(sid, digest, plan, tmp_path)

        # Patch the default sidecar root so _fold_enumeration_sidecar reads our file.
        monkeypatch.setattr(enumerate_sidecar, "DEFAULT_ROOT", sidecar_root)

        cli.cmd_approve(ns(session=sid, by="user"), store=store)

        bag = store.load(sid).plugins["premise"]
        assert bag.get("enumeration_refused_oversize") is True, (
            f"expected enumeration_refused_oversize=True in bag; got {bag}"
        )

    def test_fold_does_not_set_flag_on_healthy_sidecar(
            self, store, fixtures_dir, tmp_path, monkeypatch):
        """The flag must not appear when runner_ok=True; it is specific to the
        oversize failure path."""
        monkeypatch.setenv("CLAUDE_AGENT_HOME", str(tmp_path / "agent-home"))
        sid = "fold-healthy"
        plan = str(fixtures_dir / "plan_two_stage.toml")
        _to_plan_ready(store, sid, plan)

        digest = plugins_premise._plan_content_digest(load_plan(plan))
        sidecar_root = tmp_path / "sidecars"
        enumerate_sidecar.write(sid, digest, {
            "runner_ok": True,
            "pairs": [],
            "stderr": "",
            "content_digest": digest,
            "plan_path": plan,
        }, root=sidecar_root)
        monkeypatch.setattr(enumerate_sidecar, "DEFAULT_ROOT", sidecar_root)

        cli.cmd_approve(ns(session=sid, by="user"), store=store)

        bag = store.load(sid).plugins["premise"]
        assert not bag.get("enumeration_refused_oversize"), (
            f"expected no oversize flag on a healthy sidecar; got {bag}"
        )


# ---------------------------------------------------------------------------
# cmd_question_list --format md renders the oversize warning
# ---------------------------------------------------------------------------

class TestQuestionListOversizeRendering:
    """question-list --format md must include the split-the-plan action text when
    enumeration_refused_oversize is True — the only surface a reviewer reading the
    bag would encounter it."""

    def _state_with_oversize_flag(self, plan_path):
        state = SessionState(session_id="s", task_id="t", plan_path=plan_path,
                             weight_class=WeightClass.SUBSTANTIVE.value)
        plugins.activate(state, "premise")
        bag = state.plugins["premise"]
        bag["enumeration_refused_oversize"] = True
        return state

    def test_oversize_flag_appears_in_md_output(self, store, fixtures_dir):
        state = self._state_with_oversize_flag(str(fixtures_dir / "plan_two_stage.toml"))
        store.save(state)

        d = cli.cmd_question_list(ns(session="s", format="md"), store=store)

        assert d.ok is True
        assert "enumeration refused (oversize)" in d.detail, (
            f"expected 'enumeration refused (oversize)' in detail; got: {d.detail!r}"
        )
        assert "advisor_oversize" in d.detail

    def test_oversize_flag_absent_produces_no_warning(self, store, fixtures_dir):
        """The warning must NOT appear when the flag is not set — guards against
        the string leaking into every question-list output."""
        state = SessionState(session_id="s", task_id="t",
                             plan_path=str(fixtures_dir / "plan_two_stage.toml"),
                             weight_class=WeightClass.SUBSTANTIVE.value)
        plugins.activate(state, "premise")
        store.save(state)

        d = cli.cmd_question_list(ns(session="s", format="md"), store=store)

        assert "enumeration refused (oversize)" not in d.detail

    def test_oversize_warning_not_in_plain_format(self, store, fixtures_dir):
        """The warning is only injected in --format md; the plain format must stay
        unaffected."""
        state = self._state_with_oversize_flag(str(fixtures_dir / "plan_two_stage.toml"))
        store.save(state)

        d = cli.cmd_question_list(ns(session="s"), store=store)

        assert "enumeration refused (oversize)" not in d.detail
