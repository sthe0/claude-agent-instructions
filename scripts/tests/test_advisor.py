"""Warn-only advisory judge: default-off, fail-open, never changes directive.ok/node.

Core invariant: directive.ok and directive.node are byte-identical whether the advisor
returns a loud verdict or [] (disabled / errored). Advisories live in directive.data
only and are never persisted into gate decisions or SessionState.
"""
import ast
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import advisor, cli, premise
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
    def runner(argv, **kwargs):
        return RunResult(code, stdout=text, stderr="")
    return runner


def _raising_runner(argv, **kwargs):
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

        def recording_runner(argv, **kwargs):
            seen["argv"] = argv
            seen["timeout"] = kwargs.get("timeout")
            return RunResult(0, stdout="ok")

        advisor.judge("weight_classification", {}, recording_runner, enabled=True)
        assert seen["argv"][:4] == ["claude", "-p", "--model", "sonnet"]
        # An EXPLICIT timeout, not the runner's own default: subprocess_runner's
        # default is sized for the judge family (41s), and letting an advisory
        # call inherit it would hold the coordinator for twice this advisor's
        # own 20s deadline before failing open.
        assert seen["timeout"] == advisor._ADVISOR_TIMEOUT_S


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

    def test_runs_isolated_via_host_llm(self, monkeypatch):
        captured = {}

        def fake_run(argv, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setenv("HOST_LLM_ISOLATION_SENTINEL", "present")
        advisor.subprocess_runner(["claude", "-p", "x"], timeout=5)
        assert captured["env"]["HOST_LLM_ISOLATION_SENTINEL"] == "present"
        assert "claude-judge-sandbox" in captured["env"]["CLAUDE_CONFIG_DIR"]
        assert "claude-judge-sandbox" in captured["cwd"]

    # ── the credential label: produced from an unauthenticated WORLD ──────────
    #
    # Isolation pins CLAUDE_CONFIG_DIR, and the client resolves auth from that
    # root — so the seam can itself leave the child with no way to authenticate.
    # These three drive the real seam over a real (empty or env-authenticated)
    # config root rather than restating the literal, so a drift between what
    # subprocess_runner writes and what classify_runner_failure reads fails here.

    def _unauthenticated_world(self, monkeypatch, tmp_path):
        """An ambient config root holding no credential file, and no auth left in
        the environment either: the apiKeyHelper machine shape, plus a lost
        stored credential."""
        from lib import host_llm

        monkeypatch.setattr(host_llm, "harness_config_root", lambda: tmp_path)
        for var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            monkeypatch.delenv(var, raising=False)

    def _failing_run(self, monkeypatch, stderr="Invalid API key · Please run /login"):
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr=stderr)

        monkeypatch.setattr(subprocess, "run", fake_run)

    def test_a_failed_call_from_an_unauthenticated_child_is_labelled(
            self, monkeypatch, tmp_path):
        self._unauthenticated_world(monkeypatch, tmp_path)
        self._failing_run(monkeypatch)

        result = advisor.subprocess_runner(["claude", "-p", "x"], timeout=5)

        assert advisor.classify_runner_failure(result.stderr) == \
            premise.ESCAPE_ADVISOR_CREDENTIAL
        assert "Invalid API key" in result.stderr, "the child's own diagnostic survives"

    def test_a_successful_call_is_never_labelled_a_credential_failure(
            self, monkeypatch, tmp_path):
        """The label is a failure classification, not a machine audit: a child
        that answered is authenticated by demonstration, whatever this side
        believed it had to lend."""
        self._unauthenticated_world(monkeypatch, tmp_path)

        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout="YES", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = advisor.subprocess_runner(["claude", "-p", "x"], timeout=5)

        assert result.returncode == 0
        assert advisor._CREDENTIAL_STDERR_PREFIX not in result.stderr

    def test_an_env_authenticated_child_that_fails_keeps_the_generic_reason(
            self, monkeypatch, tmp_path):
        """The machine shape that must never be mislabelled: no stored credential
        to borrow, but a plain environment API key the child inherits. Its
        failures are ordinary failures, and telling its operator to fix a
        credential would send them after a file that was never involved."""
        self._unauthenticated_world(monkeypatch, tmp_path)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fixture")
        self._failing_run(monkeypatch, stderr="claude: unexpected error")

        result = advisor.subprocess_runner(["claude", "-p", "x"], timeout=5)

        assert advisor.classify_runner_failure(result.stderr) == \
            premise.ESCAPE_ADVISOR_ERROR


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


class TestJudgeBinaryAsk:
    def test_yes(self):
        assert advisor.judge_binary_ask("Apply this change?", _fake_runner("YES"))[0] is True

    def test_no(self):
        assert advisor.judge_binary_ask("Apply this change?", _fake_runner("NO"))[0] is False

    def test_raising_runner_fails_open(self):
        assert advisor.judge_binary_ask("Apply this change?", _raising_runner)[0] is False

    def test_no_question_mark_skips_runner(self):
        assert advisor.judge_binary_ask("Applied the change.", _raising_runner)[0] is False

    def test_fullwidth_question_mark(self):
        assert advisor.judge_binary_ask("提交做吗？", _fake_runner("YES"))[0] is True

    def test_disabled(self):
        assert advisor.judge_binary_ask("Apply this change?", _fake_runner("YES"), enabled=False)[0] is False

    def test_no_runner(self):
        assert advisor.judge_binary_ask("Apply this change?", None)[0] is False

    def test_bold_wrapped_question_reaches_runner(self):
        # The concrete miss: a confirm question wrapped in markdown bold ends in
        # '**', not '?'. The trailing-decoration rstrip must expose the '?' so the
        # judge is actually consulted (and here returns YES -> True).
        assert advisor.judge_binary_ask("**Применить правку?**", _fake_runner("YES"))[0] is True

    def test_paren_close_after_question_reaches_runner(self):
        assert advisor.judge_binary_ask("Применить правку?)", _fake_runner("YES"))[0] is True

    def test_quote_close_after_question_reaches_runner(self):
        assert advisor.judge_binary_ask('Land it?"', _fake_runner("YES"))[0] is True

    def test_decoration_then_non_question_skips_runner(self):
        # A bolded NON-question must still skip the runner: stripping the trailing
        # '**' exposes '.', not a question mark, so the raising runner is never
        # called (no over-strip into word content, no false positive).
        assert advisor.judge_binary_ask("**Готово.**", _raising_runner)[0] is False


class TestJudgePublishedAttachment:
    def test_yes(self):
        result = advisor.judge_published_attachment(
            "notes.md", "Hey team, here is a summary of what we decided today.",
            _fake_runner("YES"),
        )
        assert result == (True, "")

    def test_no(self):
        result = advisor.judge_published_attachment(
            "run.log", "2026-09-02T10:00:00Z INFO starting worker\n", _fake_runner("NO"),
        )
        assert result == (False, "")

    def test_raising_runner_fails_open(self):
        result = advisor.judge_published_attachment("notes.md", "some prose", _raising_runner)
        assert result[0] is False and result[1]

    def test_disabled_fails_open(self):
        result = advisor.judge_published_attachment(
            "notes.md", "some prose", _fake_runner("YES"), enabled=False,
        )
        assert result[0] is False and result[1]

    def test_no_runner_fails_open(self):
        result = advisor.judge_published_attachment("notes.md", "some prose", None)
        assert result[0] is False and result[1]

    def test_no_content_fails_open_without_calling_runner(self):
        result = advisor.judge_published_attachment("notes.md", "", _raising_runner)
        assert result[0] is False and result[1]

    def test_timeout_expired_fails_open(self):
        def timing_out_runner(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 0))

        result = advisor.judge_published_attachment("notes.md", "some prose", timing_out_runner)
        assert result[0] is False and result[1]

    def test_argv_carries_judge_model(self):
        seen = {}

        def recording_runner(argv, **kwargs):
            seen["argv"] = argv
            return RunResult(0, stdout="NO", stderr="")

        advisor.judge_published_attachment("notes.md", "some prose", recording_runner)
        assert seen["argv"][:4] == ["claude", "-p", "--model", "haiku"]


class TestJudgeFeedbackSignal:
    def test_yes(self):
        assert advisor.judge_feedback_signal("you shouldn't have done that", _fake_runner("YES"))[0] is True

    def test_no(self):
        assert advisor.judge_feedback_signal("please add a test for this", _fake_runner("NO"))[0] is False

    def test_disabled(self):
        assert advisor.judge_feedback_signal("ты не так сделал", _fake_runner("YES"), enabled=False)[0] is False

    def test_no_runner(self):
        assert advisor.judge_feedback_signal("ты не так сделал", None)[0] is False

    def test_empty_text_skips_runner(self):
        assert advisor.judge_feedback_signal("", _raising_runner)[0] is False

    def test_non_string_text_skips_runner(self):
        assert advisor.judge_feedback_signal(None, _raising_runner)[0] is False

    def test_non_zero_exit_fails_open(self):
        assert advisor.judge_feedback_signal("ты не так сделал", _fake_runner("YES", code=1))[0] is False

    def test_empty_stdout_fails_open(self):
        assert advisor.judge_feedback_signal("ты не так сделал", _fake_runner("   \n  "))[0] is False

    def test_unparseable_answer_fails_open(self):
        assert advisor.judge_feedback_signal("ты не так сделал", _fake_runner("maybe"))[0] is False

    def test_raising_runner_fails_open(self):
        assert advisor.judge_feedback_signal("ты не так сделал", _raising_runner)[0] is False

    def test_argv_carries_judge_model(self):
        seen = {}

        def recording_runner(argv, **kwargs):
            seen["argv"] = argv
            return RunResult(0, stdout="NO", stderr="")

        advisor.judge_feedback_signal("some text", recording_runner)
        assert seen["argv"][:4] == ["claude", "-p", "--model", "haiku"]


class TestJudgeOutageEscalation:
    def test_yes(self):
        assert advisor.judge_outage_escalation("The deploy is failing, how should I proceed?", _fake_runner("YES"))[0] is True

    def test_no(self):
        assert advisor.judge_outage_escalation("This hook detects outage escalations via regex.", _fake_runner("NO"))[0] is False

    def test_disabled(self):
        assert advisor.judge_outage_escalation("the service is down, what now?", _fake_runner("YES"), enabled=False)[0] is False

    def test_no_runner(self):
        assert advisor.judge_outage_escalation("the service is down, what now?", None)[0] is False

    def test_empty_text_skips_runner(self):
        assert advisor.judge_outage_escalation("", _raising_runner)[0] is False

    def test_non_string_text_skips_runner(self):
        assert advisor.judge_outage_escalation(None, _raising_runner)[0] is False

    def test_non_zero_exit_fails_open(self):
        assert advisor.judge_outage_escalation("the service is down, what now?", _fake_runner("YES", code=1))[0] is False

    def test_empty_stdout_fails_open(self):
        assert advisor.judge_outage_escalation("the service is down, what now?", _fake_runner("  \n  "))[0] is False

    def test_unparseable_answer_fails_open(self):
        assert advisor.judge_outage_escalation("the service is down, what now?", _fake_runner("unclear"))[0] is False

    def test_raising_runner_fails_open(self):
        assert advisor.judge_outage_escalation("the service is down, what now?", _raising_runner)[0] is False

    def test_argv_carries_judge_model(self):
        seen = {}

        def recording_runner(argv, **kwargs):
            seen["argv"] = argv
            return RunResult(0, stdout="NO", stderr="")

        advisor.judge_outage_escalation("some text", recording_runner)
        assert seen["argv"][:4] == ["claude", "-p", "--model", "haiku"]


class TestJudgeDeferringDisposition:
    _ASK = "Что делать с дефектом?\nЗавести отдельной задачей\nНе трогать"

    def test_yes(self):
        assert advisor.judge_deferring_disposition(self._ASK, _fake_runner("YES"))[0] is True

    def test_no(self):
        assert advisor.judge_deferring_disposition(self._ASK, _fake_runner("NO"))[0] is False

    def test_disabled(self):
        assert advisor.judge_deferring_disposition(self._ASK, _fake_runner("YES"), enabled=False)[0] is False

    def test_no_runner(self):
        assert advisor.judge_deferring_disposition(self._ASK, None)[0] is False

    def test_empty_text_skips_runner(self):
        assert advisor.judge_deferring_disposition("", _raising_runner)[0] is False

    def test_non_string_text_skips_runner(self):
        assert advisor.judge_deferring_disposition(None, _raising_runner)[0] is False

    def test_non_zero_exit_fails_open(self):
        assert advisor.judge_deferring_disposition(self._ASK, _fake_runner("YES", code=1))[0] is False

    def test_empty_stdout_fails_open(self):
        assert advisor.judge_deferring_disposition(self._ASK, _fake_runner("  \n  "))[0] is False

    def test_unparseable_answer_fails_open(self):
        assert advisor.judge_deferring_disposition(self._ASK, _fake_runner("unclear"))[0] is False

    def test_raising_runner_fails_open(self):
        assert advisor.judge_deferring_disposition(self._ASK, _raising_runner)[0] is False

    def test_timeout_expired_fails_open(self):
        """The dominant real failure mode, not just a generic exception. Over
        n=18 (lib/judge_latency.py) this judge runs at median 17.43s, p90 37.58s,
        max 39.99s — against the 8s budget it once inherited from
        _BINARY_ASK_TIMEOUT_S it timed out on every single call, and the four-run
        note that budget was set from ("13.9 +/- 2.4s") had seen none of the tail.
        A runner that raises subprocess.TimeoutExpired must fail open exactly
        like any other raising runner; test_every_judges_default_timeout_names_
        its_own_constant covers the sizing fix itself."""
        def timing_out_runner(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 0))

        assert advisor.judge_deferring_disposition(self._ASK, timing_out_runner)[0] is False

    def test_argv_carries_judge_model(self):
        seen = {}

        def recording_runner(argv, **kwargs):
            seen["argv"] = argv
            return RunResult(0, stdout="NO", stderr="")

        advisor.judge_deferring_disposition("some text", recording_runner)
        assert seen["argv"][:4] == ["claude", "-p", "--model", "haiku"]


class TestJudgeLandingDisciplineAsk:
    """Fail-open contract for the semantic judge behind
    hook-resolution-reminder.py's PreToolUse landing-discipline check. No real
    model call in this class — samples/judge-latency/sample_landing_discipline.py
    is the one place that costs real calls, per its own docstring."""

    _MENU = (
        "Задача решена, ветка запушена. Как приземляем?\n"
        "Открыть PR (Рекомендую)\n"
        "Открываю pull request и жду ревью перед мержем.\n"
        "Прямой push в trunk\n"
        "Мержу сейчас без ревью."
    )

    def test_yes_menu_proposes_pr(self):
        assert advisor.judge_landing_discipline_ask(self._MENU, _fake_runner("YES"))[0] is True

    def test_no_menu_proposes_direct_push(self):
        assert advisor.judge_landing_discipline_ask(self._MENU, _fake_runner("NO"))[0] is False

    def test_disabled(self):
        result = advisor.judge_landing_discipline_ask(
            self._MENU, _fake_runner("YES"), enabled=False
        )
        assert result[0] is False and result[1]

    def test_no_runner(self):
        result = advisor.judge_landing_discipline_ask(self._MENU, None)
        assert result[0] is False and result[1]

    def test_empty_text_skips_runner(self):
        result = advisor.judge_landing_discipline_ask("", _raising_runner)
        assert result[0] is False and result[1]

    def test_non_string_text_skips_runner(self):
        result = advisor.judge_landing_discipline_ask(None, _raising_runner)
        assert result[0] is False and result[1]

    def test_non_zero_exit_fails_open(self):
        result = advisor.judge_landing_discipline_ask(self._MENU, _fake_runner("YES", code=1))
        assert result[0] is False and result[1]

    def test_empty_stdout_fails_open(self):
        result = advisor.judge_landing_discipline_ask(self._MENU, _fake_runner("  \n  "))
        assert result[0] is False and result[1]

    def test_unparseable_answer_fails_open(self):
        result = advisor.judge_landing_discipline_ask(self._MENU, _fake_runner("unclear"))
        assert result[0] is False and result[1]

    def test_raising_runner_fails_open(self):
        result = advisor.judge_landing_discipline_ask(self._MENU, _raising_runner)
        assert result[0] is False and result[1]

    def test_timeout_expired_fails_open(self):
        def timing_out_runner(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 0))

        result = advisor.judge_landing_discipline_ask(self._MENU, timing_out_runner)
        assert result[0] is False and result[1]

    def test_argv_carries_judge_model(self):
        seen = {}

        def recording_runner(argv, **kwargs):
            seen["argv"] = argv
            return RunResult(0, stdout="NO", stderr="")

        advisor.judge_landing_discipline_ask(self._MENU, recording_runner)
        assert seen["argv"][:4] == ["claude", "-p", "--model", "haiku"]


# ── each judge's default timeout names ITS OWN constant (structural) ──────────

# judge function -> the module constant its `timeout` default must NAME.
#
# Every value in this table is now 41: all three last-resort defaults come from
# the same rule (lib/judge_latency.last_resort_ceiling_s — ceil of the largest
# observation anywhere in the measured family, plus 1) and that rule returns one
# number for the whole family. Which is exactly WHY this test reads the AST
# instead of calling each judge and comparing values: equal numbers cannot
# distinguish "this judge defaults to its own constant" from "this judge borrows
# a neighbour's". The predecessor test asserted the two constants DIFFERED, so it
# had to be deleted rather than updated — its whole mechanism was an accident of
# their values, and the defect it was written for (a hook passing a foreign
# hook's constant) is a NAMING defect, visible in the source and nowhere else.
_JUDGE_TIMEOUT_CONSTANTS = {
    "judge_binary_ask": "_BINARY_ASK_TIMEOUT_S",
    "judge_published_attachment": "_PUBLISHED_ATTACHMENT_TIMEOUT_S",
    "judge_feedback_signal": "_BINARY_ASK_TIMEOUT_S",
    "judge_outage_escalation": "_BINARY_ASK_TIMEOUT_S",
    "judge_deferring_disposition": "_DEFERRING_DISPOSITION_TIMEOUT_S",
    "judge_landing_discipline_ask": "_LANDING_DISCIPLINE_LAST_RESORT_TIMEOUT_S",
    "acceptance_judge": "_ACCEPTANCE_JUDGE_TIMEOUT_S",
}


def _timeout_default_expression(func_name: str) -> "ast.expr":
    """The `timeout` parameter's default expression in advisor.py's definition of
    `func_name`, as an AST node — never evaluated, so what is under test is the
    NAME the source writes, not the number it happens to resolve to."""
    tree = ast.parse(Path(advisor.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != func_name:
            continue
        args = node.args
        for arg, default in zip(
            args.kwonlyargs, args.kw_defaults
        ):
            if arg.arg == "timeout":
                assert default is not None, (
                    f"{func_name}'s timeout is keyword-only with no default"
                )
                return default
        positional = args.posonlyargs + args.args
        offset = len(positional) - len(args.defaults)
        for index, arg in enumerate(positional):
            if arg.arg == "timeout":
                assert index >= offset, f"{func_name}'s timeout has no default"
                return args.defaults[index - offset]
        raise AssertionError(f"{func_name} has no timeout parameter")
    raise AssertionError(f"advisor.py defines no {func_name}")


@pytest.mark.parametrize("func_name", sorted(_JUDGE_TIMEOUT_CONSTANTS))
def test_every_judges_default_timeout_names_its_own_constant(func_name):
    """Each judge's `timeout` default must be a bare reference to the constant
    this family assigns it — not a literal, not an arithmetic expression, and not
    another judge's constant.

    The defect this pins is real and was live in this tree:
    hook-deferring-disposition-gate.py passed
    advisor._DEFERRING_DISPOSITION_TIMEOUT_S as its own hook's per-call ceiling,
    a foreign constant that merely happened to be numerically survivable. A
    value-based check cannot see that; the source can."""
    expected = _JUDGE_TIMEOUT_CONSTANTS[func_name]
    default = _timeout_default_expression(func_name)
    assert isinstance(default, ast.Name), (
        f"{func_name}'s timeout default is {ast.dump(default)}, not a bare "
        f"reference to {expected} — a literal or expression here detaches the "
        "default from the rule that sets it"
    )
    assert default.id == expected, (
        f"{func_name}'s timeout defaults to {default.id}, but its own constant "
        f"is {expected}"
    )
    assert hasattr(advisor, expected), f"advisor has no {expected}"


def test_the_last_resort_defaults_are_computed_from_the_measurements():
    """Every last-resort default is the calibration module's family ceiling, not
    a hand-typed number that agrees with it today. This is the other half of the
    structural test above: that one fixes WHICH name each judge uses, this one
    fixes what those names are worth."""
    from lib import judge_latency

    ceiling = judge_latency.last_resort_ceiling_s()
    for const_name in sorted(set(_JUDGE_TIMEOUT_CONSTANTS.values())):
        assert getattr(advisor, const_name) == ceiling, (
            f"advisor.{const_name} is {getattr(advisor, const_name)}, but the "
            f"measured family ceiling is {ceiling}"
        )


class TestRuntimeHostArgv:
    @pytest.fixture(autouse=True)
    def _pin_cursor_binary(self, monkeypatch):
        from lib import host_llm
        monkeypatch.setattr(host_llm.shutil, "which", lambda name: "/usr/bin/agent" if name == "agent" else None)

    def _recording_runner(self, seen, stdout="YES\nreason"):
        def runner(argv, **kwargs):
            seen.append(argv)
            return RunResult(0, stdout=stdout, stderr="")
        return runner

    def test_judge_cursor_host_builds_agent_argv(self):
        seen = []
        advisor.judge(
            "weight_classification", {}, self._recording_runner(seen, "concern"),
            enabled=True, runtime_host="cursor",
        )
        argv = seen[0]
        assert argv[0] == "/usr/bin/agent"
        assert "claude" not in argv
        assert "--model" not in argv

    def test_enumerate_claims_cursor_host_builds_agent_argv(self):
        seen = []
        advisor.enumerate_claims(
            "some deliverable text", self._recording_runner(seen, "claim one"), runtime_host="cursor",
        )
        assert seen[0][0] == "/usr/bin/agent"

    def test_acceptance_judge_cursor_host_builds_agent_argv(self):
        seen = []
        advisor.acceptance_judge(
            "observation", "expected", self._recording_runner(seen), enabled=True, runtime_host="cursor",
        )
        assert seen[0][0] == "/usr/bin/agent"
        assert "--model" not in seen[0]

    def test_default_runtime_host_is_claude_for_backward_compat(self):
        seen = []
        advisor.judge("weight_classification", {}, self._recording_runner(seen, "concern"), enabled=True)
        assert seen[0][0] == "claude"

    def test_prompt_argv_dispatches_lean_true_for_a_judge_complexity_call(self, monkeypatch):
        """Pins `_prompt_argv`'s own dispatch line, not just `build_launch_argv`'s
        response to an explicit `lean` value — a spy on `build_launch_argv` proves
        the ternary actually passes `lean=True` for a `_JUDGE_COMPLEXITY` call.
        An inverted or mistyped ternary here would silently route a
        `_ADVISOR_COMPLEXITY` list-output call through the binary-classifier
        system-prompt override in production, and nothing else in this stage
        would catch it."""
        from lib import host_llm

        seen_lean = []
        real_build = host_llm.build_launch_argv

        def spy(*args, **kwargs):
            seen_lean.append(kwargs.get("lean", False))
            return real_build(*args, **kwargs)

        monkeypatch.setattr(host_llm, "build_launch_argv", spy)
        advisor.judge_binary_ask("do X or Y?", self._recording_runner([], "1\nreason"), enabled=True)
        assert seen_lean == [True]

    def test_prompt_argv_dispatches_lean_false_for_an_advisor_complexity_call(self, monkeypatch):
        from lib import host_llm

        seen_lean = []
        real_build = host_llm.build_launch_argv

        def spy(*args, **kwargs):
            seen_lean.append(kwargs.get("lean", False))
            return real_build(*args, **kwargs)

        monkeypatch.setattr(host_llm, "build_launch_argv", spy)
        advisor.enumerate_claims("some deliverable text", self._recording_runner([], "claim one"))
        assert seen_lean == [False]


# ── the prompt must never ride argv: E2BIG regression ─────────────────────────
#
# A judge/enumerate prompt built from a whole plan or artifact can exceed Linux
# MAX_ARG_STRLEN (32 * PAGE_SIZE = 131072 bytes, the per-argv-string ceiling);
# execve then rejects the launch with OSError errno E2BIG before the child even
# starts. `_fake_kernel_run` below reproduces that kernel behaviour faithfully
# (raising E2BIG for any argv element over the ceiling), so these tests are red
# on the old argv-embedded-prompt path and green on the stdin-delivery path
# without spawning a real child.

MAX_ARG_STRLEN = 131072  # Linux: 32 * PAGE_SIZE


def _fake_kernel_run(argv, *, input="", **kwargs):
    for a in argv:
        if len(a.encode()) > MAX_ARG_STRLEN:
            raise OSError(7, "Argument list too long", argv[0] if argv else None)
    return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")


class TestOversizePromptDeliveredViaStdin:
    def test_subprocess_runner_delivers_an_oversize_prompt_via_stdin_not_argv(
        self, monkeypatch
    ):
        monkeypatch.setattr(subprocess, "run", _fake_kernel_run)
        oversize_prompt = "x" * (MAX_ARG_STRLEN + 50_000)

        result = advisor.subprocess_runner(
            ["claude", "-p", "--model", "sonnet"], timeout=5, stdin=oversize_prompt
        )

        assert result.returncode == 0

    def test_subprocess_runner_raises_e2big_if_the_prompt_rides_argv(self, monkeypatch):
        """Control: proves `_fake_kernel_run` actually reproduces the defect this
        stage removes -- the old call shape (prompt appended to argv) still fails."""
        monkeypatch.setattr(subprocess, "run", _fake_kernel_run)
        oversize_prompt = "x" * (MAX_ARG_STRLEN + 50_000)

        with pytest.raises(OSError):
            advisor.subprocess_runner(
                ["claude", "-p", "--model", "sonnet", oversize_prompt], timeout=5
            )

    def test_judge_binary_ask_end_to_end_survives_an_oversize_observation(
        self, monkeypatch
    ):
        """`judge_binary_ask`'s prompt embeds the caller's observation text; with an
        oversize observation the old argv-embedded-prompt path raised E2BIG before
        the fake kernel's stdout ("ok") could even be produced. The runner is
        called directly (no try/except around the OSError at this call site), so a
        raised OSError would propagate out of this call -- asserting a normal
        return proves it no longer does."""
        monkeypatch.setattr(subprocess, "run", _fake_kernel_run)
        oversize_observation = "x" * (MAX_ARG_STRLEN + 50_000) + "?"

        verdict, reason = advisor.judge_binary_ask(
            oversize_observation, advisor.subprocess_runner, enabled=True, timeout=5
        )

        assert reason != "judge raised (fail-open)"

    def test_enumerate_claims_end_to_end_survives_an_oversize_artifact(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", _fake_kernel_run)
        oversize_artifact = "x" * (MAX_ARG_STRLEN + 50_000)

        claims = advisor.enumerate_claims(oversize_artifact, advisor.subprocess_runner)

        assert claims == ["ok"]
