"""Warn-only advisory judge: default-off, fail-open, never changes directive.ok/node.

Core invariant: directive.ok and directive.node are byte-identical whether the advisor
returns a loud verdict or [] (disabled / errored). Advisories live in directive.data
only and are never persisted into gate decisions or SessionState.
"""
from argparse import Namespace

import pytest

from agentctl import advisor, cli
from agentctl.config import Thresholds
from agentctl.dispatch import RunResult
from agentctl.state import (
    Actor,
    Criterion,
    CriterionType,
    GateRecord,
    Means,
    Node,
    Outcome,
    Route,
    SessionState,
    Stage,
    StageStatus,
    Subject,
    WeightClass,
)


def _fake_runner(text, code=0):
    def runner(argv):
        return RunResult(code, stdout=text, stderr="")
    return runner


def _raising_runner(argv):
    raise RuntimeError("unexpected runner call")


def ns(**kw):
    return Namespace(**kw)


def _start(store, sid):
    cli.cmd_start(ns(session=sid, task="t", goal="improve quality", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)


# ── Unit tests for advisor.judge ─────────────────────────────────────────────

class TestJudgeUnit:
    def test_disabled_by_default_no_env(self):
        assert advisor.judge("weight_classification", {}, _fake_runner("concern")) == []

    def test_disabled_explicit(self):
        assert advisor.judge("weight_classification", {}, _fake_runner("c"), enabled=False) == []

    def test_enabled_runner_none_returns_empty(self):
        assert advisor.judge("weight_classification", {}, None, enabled=True) == []

    def test_unknown_kind_returns_empty(self):
        assert advisor.judge("nonexistent_kind", {}, _fake_runner("x"), enabled=True) == []

    def test_enabled_returns_verdict_lines(self):
        r = _fake_runner("plan looks incomplete\nsecond concern")
        lines = advisor.judge("weight_classification", {"goal": "g"}, r, enabled=True)
        assert lines == ["plan looks incomplete", "second concern"]

    def test_enabled_non_zero_exit_returns_empty(self):
        r = _fake_runner("verdict", code=1)
        assert advisor.judge("weight_classification", {}, r, enabled=True) == []

    def test_enabled_runner_raises_returns_empty(self):
        assert advisor.judge("weight_classification", {}, _raising_runner, enabled=True) == []

    def test_enabled_empty_stdout_returns_empty(self):
        assert advisor.judge("plan_completeness", {}, _fake_runner("  \n  \n"), enabled=True) == []

    def test_enabled_whitespace_lines_stripped(self):
        r = _fake_runner("  concern one  \n\n  concern two  \n")
        lines = advisor.judge("plan_completeness", {}, r, enabled=True)
        assert lines == ["concern one", "concern two"]

    def test_all_four_kinds_accepted(self):
        r = _fake_runner("advisory")
        for kind in ("weight_classification", "plan_completeness",
                     "hypothesis_distinctness", "acceptance_observation"):
            assert advisor.judge(kind, {}, r, enabled=True) == ["advisory"]

    def test_env_toggle_enables(self, monkeypatch):
        monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
        r = _fake_runner("from env")
        assert advisor.judge("weight_classification", {}, r) == ["from env"]

    def test_env_toggle_not_set_disables(self, monkeypatch):
        monkeypatch.delenv("AGENTCTL_ADVISOR", raising=False)
        assert advisor.judge("weight_classification", {}, _fake_runner("x")) == []

    def test_argv_carries_explicit_cheap_model(self):
        seen = {}

        def recording_runner(argv):
            seen["argv"] = argv
            return RunResult(0, stdout="ok")

        advisor.judge("weight_classification", {}, recording_runner, enabled=True)
        assert seen["argv"][:4] == ["claude", "-p", "--model", "sonnet"]


# ── resolve_enabled: env override + config-mode/weight-class layering ────────

class TestResolveEnabled:
    def test_env_force_on_overrides_config_off(self, monkeypatch):
        monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
        thr = Thresholds({"advisor-mode": "off"})
        assert advisor.resolve_enabled("SMALL_CHANGE", thresholds=thr) is True

    def test_env_force_off_overrides_config_substantive(self, monkeypatch):
        monkeypatch.setenv("AGENTCTL_ADVISOR", "0")
        thr = Thresholds({"advisor-mode": "substantive"})
        assert advisor.resolve_enabled("SUBSTANTIVE", thresholds=thr) is False

    def test_config_on_substantive_enables(self, monkeypatch):
        monkeypatch.delenv("AGENTCTL_ADVISOR", raising=False)
        thr = Thresholds({"advisor-mode": "substantive"})
        assert advisor.resolve_enabled("SUBSTANTIVE", thresholds=thr) is True

    def test_config_on_small_change_disables(self, monkeypatch):
        monkeypatch.delenv("AGENTCTL_ADVISOR", raising=False)
        thr = Thresholds({"advisor-mode": "substantive"})
        assert advisor.resolve_enabled("SMALL_CHANGE", thresholds=thr) is False

    def test_config_off_disables(self, monkeypatch):
        monkeypatch.delenv("AGENTCTL_ADVISOR", raising=False)
        thr = Thresholds({"advisor-mode": "off"})
        assert advisor.resolve_enabled("SUBSTANTIVE", thresholds=thr) is False

    def test_missing_advisor_mode_key_fails_closed(self, monkeypatch):
        monkeypatch.delenv("AGENTCTL_ADVISOR", raising=False)
        thr = Thresholds({})
        assert advisor.resolve_enabled("SUBSTANTIVE", thresholds=thr) is False


# ── subprocess_runner: hard timeout ───────────────────────────────────────────

class TestSubprocessRunner:
    def test_timeout_returns_failed_result_not_raise(self, monkeypatch):
        import subprocess as _subprocess

        def raise_timeout(*a, **kw):
            raise _subprocess.TimeoutExpired(cmd="claude", timeout=1)

        monkeypatch.setattr(_subprocess, "run", raise_timeout)
        result = advisor.subprocess_runner(["claude", "-p", "x"], timeout=1)
        assert result.returncode != 0


# ── cmd_classify wiring ───────────────────────────────────────────────────────

class TestClassifyWiring:
    def _classify(self, store, sid, runner=None):
        _start(store, sid)
        return cli.cmd_classify(
            ns(session=sid, chat=True, changed_lines=0, files=1,
               wall_clock_min=0, tracker_key=None, architectural=False,
               external_effect=False, new_dependency=False, public_api_change=False),
            store=store, runner=runner,
        )

    def test_advisory_surfaces_in_data(self, store, monkeypatch):
        monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
        d = self._classify(store, "adv-cls1", _fake_runner("weight class seems off"))
        assert "advisories" in d.data
        assert "weight class seems off" in d.data["advisories"]

    def test_ok_node_action_unchanged_with_loud_verdict(self, store, monkeypatch):
        monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
        d_with = self._classify(store, "adv-cls2", _fake_runner("THIS PLAN IS WRONG, BLOCK IT"))
        d_without = self._classify(store, "adv-cls3", None)
        assert d_with.ok == d_without.ok
        assert d_with.node == d_without.node
        assert d_with.action == d_without.action

    def test_no_advisory_key_when_disabled(self, store, monkeypatch):
        monkeypatch.delenv("AGENTCTL_ADVISOR", raising=False)
        d = self._classify(store, "adv-cls4", _fake_runner("x"))
        assert "advisories" not in d.data

    def test_raising_runner_still_ok(self, store, monkeypatch):
        monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
        d = self._classify(store, "adv-cls5", _raising_runner)
        assert d.ok is True
        assert "advisories" not in d.data


# ── cmd_submit_plan wiring ────────────────────────────────────────────────────

class TestSubmitPlanWiring:
    def _to_plan_ready(self, store, sid, plan_path, runner=None):
        _start(store, sid)
        cli.cmd_classify(
            ns(session=sid, chat=False, changed_lines=200, files=5,
               wall_clock_min=60, tracker_key=None, architectural=True,
               external_effect=False, new_dependency=False, public_api_change=False),
            store=store,
        )
        cli.cmd_plan(ns(session=sid), store=store)
        return cli.cmd_submit_plan(ns(session=sid, plan=plan_path), store=store, runner=runner)

    def test_advisory_surfaces_on_success(self, store, fixtures_dir, monkeypatch):
        monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
        plan = str(fixtures_dir / "plan_two_stage.toml")
        d = self._to_plan_ready(store, "adv-sp1", plan, _fake_runner("stage 3 is missing"))
        assert d.ok is True
        assert "advisories" in d.data
        assert "stage 3 is missing" in d.data["advisories"]

    def test_ok_node_unchanged_with_loud_verdict(self, store, fixtures_dir, monkeypatch):
        monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
        plan = str(fixtures_dir / "plan_two_stage.toml")
        d_with = self._to_plan_ready(store, "adv-sp2", plan, _fake_runner("BLOCK THIS PLAN"))
        d_without = self._to_plan_ready(store, "adv-sp3", plan, None)
        assert d_with.ok == d_without.ok
        assert d_with.node == d_without.node
        assert d_with.marker == d_without.marker

    def test_no_advisory_on_failed_plan(self, store, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
        _start(store, "adv-sp4")
        cli.cmd_classify(
            ns(session="adv-sp4", chat=False, changed_lines=200, files=5,
               wall_clock_min=60, tracker_key=None, architectural=True,
               external_effect=False, new_dependency=False, public_api_change=False),
            store=store,
        )
        cli.cmd_plan(ns(session="adv-sp4"), store=store)
        bad = tmp_path / "bad.md"
        bad.write_text("not a valid plan\n", encoding="utf-8")
        d = cli.cmd_submit_plan(ns(session="adv-sp4", plan=str(bad)),
                                store=store, runner=_fake_runner("advisory"))
        assert d.ok is False
        assert "advisories" not in d.data


# ── cmd_critique wiring ───────────────────────────────────────────────────────

def _to_diagnosing(store, sid, plan):
    """Drive to DIAGNOSING with a failed stage."""
    _start(store, sid)
    cli.cmd_classify(
        ns(session=sid, chat=False, changed_lines=200, files=5,
           wall_clock_min=60, tracker_key=None, architectural=True,
           external_effect=False, new_dependency=False, public_api_change=False),
        store=store,
    )
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)
    cli.cmd_record_result(ns(session=sid, status="failed", actual="boom"), store=store)


def _do_critique(store, sid, runner=None):
    cli.cmd_declare(ns(session=sid, expected="X", actual="Y", mismatch="Z"), store=store)
    cli.cmd_investigate(
        ns(session=sid, localized_expectation="at line 5", localized_actual="line 5 missing",
           hypotheses=["hypothesis A: wrong config", "hypothesis B: missing dep"]),
        store=store,
    )
    return cli.cmd_critique(
        ns(session=sid, functional_ground="the system assumes X", replanning_task="fix config",
           invariants_to_preserve=None, differences_to_remove=None),
        store=store, runner=runner,
    )


class TestCritiqueWiring:
    def test_advisory_surfaces_in_data(self, store, fixtures_dir, monkeypatch):
        monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
        plan = str(fixtures_dir / "plan_two_stage.toml")
        _to_diagnosing(store, "adv-crit1", plan)
        d = _do_critique(store, "adv-crit1", _fake_runner("hypothesis B duplicates A in meaning"))
        assert d.ok is True
        assert "advisories" in d.data
        assert "hypothesis B duplicates A in meaning" in d.data["advisories"]

    def test_ok_node_unchanged_with_loud_verdict(self, store, fixtures_dir, monkeypatch):
        monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
        plan = str(fixtures_dir / "plan_two_stage.toml")
        _to_diagnosing(store, "adv-crit2", plan)
        d_with = _do_critique(store, "adv-crit2", _fake_runner("BLOCK THIS CRITIQUE NOW"))
        _to_diagnosing(store, "adv-crit3", plan)
        d_without = _do_critique(store, "adv-crit3", None)
        assert d_with.ok == d_without.ok
        assert d_with.node == d_without.node
        assert d_with.action == d_without.action

    def test_raising_runner_still_ok(self, store, fixtures_dir, monkeypatch):
        monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
        plan = str(fixtures_dir / "plan_two_stage.toml")
        _to_diagnosing(store, "adv-crit4", plan)
        d = _do_critique(store, "adv-crit4", _raising_runner)
        assert d.ok is True
        assert "advisories" not in d.data


# ── cmd_record_result acceptance_review wiring ────────────────────────────────

def _make_acceptance_session(store, sid):
    """Construct a session with an acceptance_review stage at EXECUTING directly."""
    state = SessionState(
        session_id=sid,
        task_id="acceptance-test",
        goal="verify UI feature",
        overall_done_criterion="user accepts on review",
        overall_criterion_type=CriterionType.ACCEPTANCE_REVIEW.value,
        weight_class=WeightClass.SMALL_CHANGE.value,
        route=Route.IN_THREAD.value,
        node=Node.EXECUTING.value,
        approval=GateRecord("plan_approval", armed=True, passed=True, by="small-change-carve-out"),
        stages=[
            Stage(
                index=1,
                title="UI verification",
                subject=Subject(material="the feature", result="button is green"),
                means=Means(means="browser", method="open the page"),
                actor=Actor(executor="in_thread"),
                criterion=Criterion(
                    criterion_type=CriterionType.ACCEPTANCE_REVIEW.value,
                    done_criterion="user sees green button",
                ),
                outcome=Outcome(status=StageStatus.ACTIVE.value),
            )
        ],
        current_stage=1,
    )
    store.save(state)


class TestRecordResultAcceptanceWiring:
    def test_advisory_surfaces_on_acceptance_pass(self, store, monkeypatch):
        monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
        _make_acceptance_session(store, "adv-rr1")
        d = cli.cmd_record_result(
            ns(session="adv-rr1", status="passed", actual="observed green",
               control=None, observation="the button was green when I opened the page"),
            store=store,
            runner=_fake_runner("observation too vague to be conclusive"),
        )
        assert d.ok is True
        assert "advisories" in d.data
        assert "observation too vague to be conclusive" in d.data["advisories"]

    def test_ok_node_unchanged_with_loud_verdict(self, store, monkeypatch):
        monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
        _make_acceptance_session(store, "adv-rr2")
        d_with = cli.cmd_record_result(
            ns(session="adv-rr2", status="passed", actual="observed",
               control=None, observation="saw the button turn green on clicking submit"),
            store=store,
            runner=_fake_runner("REJECT THIS OBSERVATION, DO NOT PASS"),
        )
        _make_acceptance_session(store, "adv-rr3")
        d_without = cli.cmd_record_result(
            ns(session="adv-rr3", status="passed", actual="observed",
               control=None, observation="saw the button turn green on clicking submit"),
            store=store,
            runner=None,
        )
        assert d_with.ok == d_without.ok
        assert d_with.node == d_without.node

    def test_no_advisory_on_measurable_stage(self, store, monkeypatch):
        """Advisor is NOT attached for measurable stages (only acceptance_review)."""
        monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
        state = SessionState(
            session_id="adv-rr4",
            task_id="measurable-test",
            goal="run tests",
            overall_done_criterion="tests green",
            overall_criterion_type=CriterionType.MEASURABLE.value,
            weight_class=WeightClass.SMALL_CHANGE.value,
            route=Route.IN_THREAD.value,
            node=Node.EXECUTING.value,
            approval=GateRecord("plan_approval", armed=True, passed=True, by="small-change-carve-out"),
            stages=[
                Stage(
                    index=1,
                    title="run pytest",
                    subject=Subject(material="tests", result="all green"),
                    means=Means(means="pytest", method="python3 -m pytest"),
                    actor=Actor(executor="in_thread"),
                    criterion=Criterion(
                        criterion_type=CriterionType.MEASURABLE.value,
                        done_criterion="pytest exits 0",
                    ),
                    outcome=Outcome(status=StageStatus.ACTIVE.value),
                )
            ],
            current_stage=1,
        )
        store.save(state)
        loud = _fake_runner("BLOCK THIS MEASURABLE STAGE")
        d = cli.cmd_record_result(
            ns(session="adv-rr4", status="passed", actual="ok", control=None, observation=""),
            store=store, runner=loud,
        )
        assert d.ok is True
        assert "advisories" not in d.data

    def test_raising_runner_still_passes(self, store, monkeypatch):
        monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
        _make_acceptance_session(store, "adv-rr5")
        d = cli.cmd_record_result(
            ns(session="adv-rr5", status="passed", actual="observed",
               control=None, observation="button turned green immediately on load"),
            store=store, runner=_raising_runner,
        )
        assert d.ok is True
        assert "advisories" not in d.data
