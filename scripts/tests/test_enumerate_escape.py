"""Stage 5 of advisor-timeout-f3b: a FAILED enumeration run blocks `approve` until a
typed escape from a closed set is recorded, instead of discharging itself fail-open.

Before this, the mandatory question-enumeration cross-check flipped `enumerated` the
moment the pass RAN, whatever it returned — so a runner timeout bought approve for
free and left nothing anyone could count. Covered here:

  - `advisor.classify_runner_failure` reads back the exact stderr
    `advisor.subprocess_runner` writes on TimeoutExpired (produced by forcing the
    real exception, not by restating the literal), and treats every other stderr —
    empty included — as the catch-all `advisor_error`;
  - the gate's third branch fires on `enumerated_runner_ok is False` and ONLY on
    that: `None` (advisor absent, and what a bag minted before this half yields)
    and `True` both stay silent;
  - an escape discharges the blocker for the plan content it was recorded against
    and for no other — a plan edit re-blocks;
  - admissibility is checked against the bag, not trusted from the operator: a
    runner-failure reason against a healthy (or absent) run is refused, and
    `enumeration_not_landed` is refused while the launch deadline is in the future
    (naming the time remaining), refused when no launch was ever made, and admitted
    once the deadline has passed;
  - the closed reason set is enforced at the parser AND in the body, since cmd_*
    functions are called directly with a hand-built Namespace;
  - the escape's own liveness precondition — a relaunch (`submit-plan` resubmission
    as well as `replan`) routes the outstanding-child window onto the ESCAPABLE
    `_ENUMERATE_NOT_RUN`, never the inescapable `_ENUMERATE_STALE`;
  - the failed run's stderr reaches the bag on both paths that write one (the
    synchronous `question-enumerate` and the detached worker's sidecar fold), so
    the blocker can pre-select the reason.

Every assertion about persisted bag state reads `store.load(sid)`, never the
in-memory bag a command mutated: the whole point of the gate is what the NEXT
process sees.
"""
from __future__ import annotations

import re
import subprocess
import time
from argparse import Namespace

import pytest

from agentctl import advisor, cli, enumerate_sidecar, plugins, plugins_premise, premise
from agentctl.dispatch import RunResult
from agentctl.plan import load_plan
from agentctl.state import SessionState, WeightClass


@pytest.fixture(autouse=True)
def _premise_armed(monkeypatch):
    """Override conftest's suite-wide AGENTCTL_PREMISE=0 force-off — this module is
    about the premise gate itself, so the plain weight_class predicate must run."""
    monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)


def ns(**kw):
    return Namespace(**kw)


def _escape_ns(sid, reason, note="the advisor never came back", plan=None):
    return ns(session=sid, reason=reason, note=note, plan=plan)


def _failing_runner(stderr):
    return lambda argv: RunResult(1, "", stderr)


# --- unit-level bags: the gate's own three-valued predicate ---------------------

def _bag_state(plan_path, **bag_kw):
    """A substantive session with a premise bag whose question/order halves are
    already satisfied, so the only blocker that can fire is the enumeration one."""
    state = SessionState(session_id="s", task_id="t", plan_path=plan_path,
                         weight_class=WeightClass.SUBSTANTIVE.value)
    plugins.activate(state, "premise")
    bag = state.plugins["premise"]
    bag["order_elements"] = [{
        "id": "O1", "element": "the order this plan answers",
        "disposition": "covered", "stage": 1, "reason": "",
    }]
    bag["enumerated"] = True
    bag["enumerated_at"] = plugins_premise._plan_content_digest(load_plan(plan_path))
    bag.update(bag_kw)
    return state, bag


class TestRunnerFailureBlocksApproval:
    def test_failed_runner_blocks_and_names_the_pre_selected_reason(self, fixtures_dir):
        """The core of the change: a landed pass whose runner FAILED refuses approve.
        Remove the branch and this session approves silently — which is exactly the
        fail-open discharge stage 5 exists to close."""
        plan_path = str(fixtures_dir / "plan_two_stage.toml")
        state, _ = _bag_state(plan_path, enumerated_runner_ok=False,
                              enumerated_runner_stderr="advisor timed out after 480s")

        blockers = plugins.plugin_gate_blockers(state, "plan_approval")

        assert len(blockers) == 1
        assert plugins_premise._ENUMERATE_RUNNER_FAILED in blockers[0]
        # the reason is pre-selected from the failure's OWN stderr, so the operator
        # confirms a value rather than picking one of five blind
        assert f"--reason {premise.ESCAPE_ADVISOR_TIMEOUT}" in blockers[0]

    def test_unrecognised_stderr_pre_selects_the_catch_all_reason(self, fixtures_dir):
        plan_path = str(fixtures_dir / "plan_two_stage.toml")
        state, _ = _bag_state(plan_path, enumerated_runner_ok=False,
                              enumerated_runner_stderr="Traceback (most recent call last): ...")

        blockers = plugins.plugin_gate_blockers(state, "plan_approval")

        assert f"--reason {premise.ESCAPE_ADVISOR_ERROR}" in blockers[0]

    def test_absent_runner_does_not_block_and_a_legacy_bag_still_evaluates(
            self, fixtures_dir):
        """`is False`, never `is not True`. None means the advisor was ABSENT — which
        is also what `.get` yields for a bag minted before these keys existed and for
        every stub-injected session in the suite. Fold None into the failure branch
        and every such session newly blocks at approve on a runner that never failed.

        The second half is the same predicate reached from the other side: a bag
        carrying NONE of stage 5's keys must evaluate the gate, not KeyError."""
        plan_path = str(fixtures_dir / "plan_two_stage.toml")
        state, _ = _bag_state(plan_path, enumerated_runner_ok=None)
        assert plugins.plugin_gate_blockers(state, "plan_approval") == []

        legacy, bag = _bag_state(plan_path)
        for key in ("enumerated_runner_ok", "enumerated_runner_stderr", "escapes",
                    "enumerate_deadline"):
            bag.pop(key, None)
        assert plugins.plugin_gate_blockers(legacy, "plan_approval") == []

    def test_healthy_runner_does_not_block(self, fixtures_dir):
        plan_path = str(fixtures_dir / "plan_two_stage.toml")
        state, _ = _bag_state(plan_path, enumerated_runner_ok=True,
                              enumerated_runner_stderr="a warning on a healthy run")
        assert plugins.plugin_gate_blockers(state, "plan_approval") == []


class TestEscapeDischargesOnlyThesePlanBytes:
    def test_escape_at_the_live_digest_clears_the_blocker(self, store, fixtures_dir):
        plan_path = str(fixtures_dir / "plan_two_stage.toml")
        state, _ = _bag_state(plan_path, enumerated_runner_ok=False,
                              enumerated_runner_stderr="advisor timed out after 480s")
        store.save(state)

        d = cli.cmd_question_enumerate_escape(
            _escape_ns("s", premise.ESCAPE_ADVISOR_TIMEOUT), store=store)

        assert d.ok is True, d.detail
        reloaded = store.load("s")
        recorded = reloaded.plugins["premise"]["escapes"]
        assert [r["reason"] for r in recorded] == [premise.ESCAPE_ADVISOR_TIMEOUT]
        assert recorded[0]["runner_ok"] is False
        assert recorded[0]["plan"] == plan_path
        assert plugins.plugin_gate_blockers(reloaded, "plan_approval") == []

    def test_the_blocker_returns_when_the_plan_content_changes(
            self, store, fixtures_dir, tmp_path):
        """An escape is a statement about ONE plan's ONE failed pass. Drop the
        digest binding and a single infra blip discharges the cross-check for every
        later revision of the plan — the fail-open shape in a slower form."""
        plan_path = tmp_path / "plan.toml"
        plan_path.write_text((fixtures_dir / "plan_two_stage.toml").read_text(encoding="utf-8"),
                             encoding="utf-8")
        state, _ = _bag_state(str(plan_path), enumerated_runner_ok=False,
                              enumerated_runner_stderr="boom")
        store.save(state)
        assert cli.cmd_question_enumerate_escape(
            _escape_ns("s", premise.ESCAPE_ADVISOR_ERROR), store=store).ok is True
        assert plugins.plugin_gate_blockers(store.load("s"), "plan_approval") == []

        edited = (fixtures_dir / "plan_two_stage_substantive.toml").read_text(encoding="utf-8")
        plan_path.write_text(edited, encoding="utf-8")
        # a fresh pass over the CORRECTED content, failing the same way: the only
        # thing that could clear this is an escape, and the one on record names the
        # superseded digest
        reloaded = store.load("s")
        reloaded.plugins["premise"]["enumerated_at"] = plugins_premise._plan_content_digest(
            load_plan(str(plan_path)))
        store.save(reloaded)

        blockers = plugins.plugin_gate_blockers(store.load("s"), "plan_approval")
        assert len(blockers) == 1
        assert plugins_premise._ENUMERATE_RUNNER_FAILED in blockers[0]


class TestEscapeAdmissibility:
    def test_runner_failure_reason_refused_against_a_healthy_or_absent_run(
            self, store, fixtures_dir):
        """The operator does not get to assert the failure. Drop this and any
        session can record `advisor_timeout` against a perfectly healthy pass,
        which makes the counted rows unreadable and the blocker optional."""
        plan_path = str(fixtures_dir / "plan_two_stage.toml")
        for sid, runner_ok in (("healthy", True), ("absent", None)):
            state, _ = _bag_state(plan_path, enumerated_runner_ok=runner_ok)
            state.session_id = sid
            store.save(state)

            d = cli.cmd_question_enumerate_escape(
                _escape_ns(sid, premise.ESCAPE_ADVISOR_TIMEOUT), store=store)

            assert d.ok is False, f"{sid}: {d.detail}"
            assert str(runner_ok) in d.detail
            assert store.load(sid).plugins["premise"]["escapes"] == []

    def test_not_landed_refused_while_the_deadline_is_in_the_future(
            self, store, fixtures_dir):
        """`wait` is the correct action while a child still has time, so the refusal
        has to say HOW LONG — a bare 'not yet' sends the operator back every few
        seconds or, worse, to a synchronous 480 s pass."""
        plan_path = str(fixtures_dir / "plan_two_stage.toml")
        state, bag = _bag_state(plan_path, enumerated=False, enumerated_at="",
                                enumerate_deadline=time.time() + 300)
        store.save(state)

        d = cli.cmd_question_enumerate_escape(
            _escape_ns("s", premise.ESCAPE_ENUMERATION_NOT_LANDED), store=store)

        assert d.ok is False
        assert re.search(r"\b(299|300)s remaining", d.detail), d.detail
        assert store.load("s").plugins["premise"]["escapes"] == []

    def test_not_landed_admitted_once_the_deadline_has_passed(self, store, fixtures_dir):
        """The liveness half: a worker that never landed must have SOME route out,
        or the widened bound turns every lost child into a permanently wedged
        approve."""
        plan_path = str(fixtures_dir / "plan_two_stage.toml")
        state, _ = _bag_state(plan_path, enumerated=False, enumerated_at="",
                              enumerate_deadline=time.time() - 1)
        store.save(state)

        d = cli.cmd_question_enumerate_escape(
            _escape_ns("s", premise.ESCAPE_ENUMERATION_NOT_LANDED,
                       note="the worker never wrote a sidecar"), store=store)

        assert d.ok is True, d.detail
        reloaded = store.load("s")
        assert [r["reason"] for r in reloaded.plugins["premise"]["escapes"]] == [
            premise.ESCAPE_ENUMERATION_NOT_LANDED]
        assert plugins.plugin_gate_blockers(reloaded, "plan_approval") == []

    def test_not_landed_refused_when_no_launch_was_ever_made(self, store, fixtures_dir):
        """No deadline means no child was ever launched, so nothing is late —
        `question-enumerate` is the action, not an escape. Without this, a session
        that simply never ran the cross-check could escape it outright."""
        plan_path = str(fixtures_dir / "plan_two_stage.toml")
        state, _ = _bag_state(plan_path, enumerated=False, enumerated_at="",
                              enumerate_deadline=None)
        store.save(state)

        d = cli.cmd_question_enumerate_escape(
            _escape_ns("s", premise.ESCAPE_ENUMERATION_NOT_LANDED), store=store)

        assert d.ok is False
        assert "question-enumerate" in d.detail
        assert store.load("s").plugins["premise"]["escapes"] == []

    def test_not_landed_refused_when_an_enumeration_is_on_record(self, store, fixtures_dir):
        plan_path = str(fixtures_dir / "plan_two_stage.toml")
        state, _ = _bag_state(plan_path, enumerated_runner_ok=False,
                              enumerate_deadline=time.time() - 1)
        store.save(state)

        d = cli.cmd_question_enumerate_escape(
            _escape_ns("s", premise.ESCAPE_ENUMERATION_NOT_LANDED), store=store)

        assert d.ok is False
        assert store.load("s").plugins["premise"]["escapes"] == []

    def test_empty_note_is_refused(self, store, fixtures_dir):
        """The reason token aggregates; the note is what makes one row diagnosable.
        Accept an empty one and the escape degrades to a bare counter."""
        plan_path = str(fixtures_dir / "plan_two_stage.toml")
        state, _ = _bag_state(plan_path, enumerated_runner_ok=False)
        store.save(state)

        d = cli.cmd_question_enumerate_escape(
            _escape_ns("s", premise.ESCAPE_ADVISOR_ERROR, note="   "), store=store)

        assert d.ok is False
        assert store.load("s").plugins["premise"]["escapes"] == []


class TestClosedReasonSet:
    def test_out_of_set_reason_exits_non_zero_at_the_parser(self):
        parser = cli.build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["question-enumerate-escape", "--session", "s",
                               "--reason", "it_was_slow", "--note", "n"])
        assert exc.value.code != 0

        # ...and the other direction: every shipped reason really does parse
        for reason in premise.ENUMERATION_ESCAPE_REASONS:
            args = parser.parse_args(["question-enumerate-escape", "--session", "s",
                                      "--reason", reason, "--note", "n"])
            assert args.reason == reason

    def test_out_of_set_reason_is_refused_in_the_body_too(self, store, fixtures_dir):
        """cmd_* functions are called directly with a hand-built Namespace (by the
        suite and by cmd_drive's composition), so a set enforced only at the parser
        is not closed for those callers."""
        plan_path = str(fixtures_dir / "plan_two_stage.toml")
        state, _ = _bag_state(plan_path, enumerated_runner_ok=False)
        store.save(state)

        d = cli.cmd_question_enumerate_escape(_escape_ns("s", "it_was_slow"), store=store)

        assert d.ok is False
        assert store.load("s").plugins["premise"]["escapes"] == []


class TestEscapePlanFlag:
    def test_empty_plan_path_is_refused(self, store, fixtures_dir):
        state, _ = _bag_state(str(fixtures_dir / "plan_two_stage.toml"),
                              enumerated_runner_ok=False)
        store.save(state)
        d = cli.cmd_question_enumerate_escape(
            _escape_ns("s", premise.ESCAPE_ADVISOR_ERROR, plan=""), store=store)
        assert d.ok is False
        assert "empty path" in d.detail

    def test_unloadable_plan_is_refused(self, store, fixtures_dir, tmp_path):
        state, _ = _bag_state(str(fixtures_dir / "plan_two_stage.toml"),
                              enumerated_runner_ok=False)
        store.save(state)
        d = cli.cmd_question_enumerate_escape(
            _escape_ns("s", premise.ESCAPE_ADVISOR_ERROR, plan=str(tmp_path / "nope.toml")),
            store=store)
        assert d.ok is False
        assert store.load("s").plugins["premise"]["escapes"] == []

    def test_named_plan_binds_the_escape_to_that_plans_digest(
            self, store, fixtures_dir):
        """cmd_replan evaluates the gate against the CORRECTED plan, which is not
        state.plan_path until that replan succeeds. An escape that could only bind
        to state.plan_path could never unblock the replan it exists for."""
        corrected = str(fixtures_dir / "plan_two_stage_substantive.toml")
        state, _ = _bag_state(str(fixtures_dir / "plan_two_stage.toml"),
                              enumerated_runner_ok=False)
        store.save(state)

        d = cli.cmd_question_enumerate_escape(
            _escape_ns("s", premise.ESCAPE_ADVISOR_ERROR, plan=corrected), store=store)

        assert d.ok is True, d.detail
        recorded = store.load("s").plugins["premise"]["escapes"][0]
        assert recorded["plan"] == corrected
        assert recorded["content_digest"] == plugins_premise._plan_content_digest(
            load_plan(corrected))


# --- classify_runner_failure ----------------------------------------------------

class TestClassifyRunnerFailure:
    def test_reads_back_the_runners_own_timeout_stderr(self, monkeypatch):
        """Produced by forcing the REAL TimeoutExpired rather than restating the
        literal: if the emitted wording and the classifier ever drift apart, every
        timeout would be pre-selected as the catch-all `advisor_error` and nothing
        else would notice."""
        def _timed_out(*_a, **_kw):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=advisor.ENUMERATE_TIMEOUT_S)

        monkeypatch.setattr(subprocess, "run", _timed_out)
        result = advisor.subprocess_runner(["claude", "-p", "x"],
                                           timeout=advisor.ENUMERATE_TIMEOUT_S)

        assert result.returncode != 0
        assert advisor.classify_runner_failure(result.stderr) == premise.ESCAPE_ADVISOR_TIMEOUT

    def test_any_other_stderr_is_the_catch_all(self):
        assert advisor.classify_runner_failure(
            "claude: command not found") == premise.ESCAPE_ADVISOR_ERROR

    def test_absent_stderr_is_the_catch_all(self):
        # enumerate_questions_health's exception arm returns ("", ) — a failure with
        # no diagnostic at all must still classify, not raise.
        assert advisor.classify_runner_failure("") == premise.ESCAPE_ADVISOR_ERROR
        assert advisor.classify_runner_failure(None) == premise.ESCAPE_ADVISOR_ERROR


# --- end to end through the ordinary CLI verbs ---------------------------------

def _cover_the_order(store, sid, stage=1):
    cli.cmd_order_raise(ns(session=sid, id="O1", element="the order this plan answers"),
                        store=store)
    cli.cmd_order_dispose(ns(session=sid, id="O1", as_="covered", stage=stage, reason=""),
                          store=store)


def _to_plan_ready_with_premise(store, sid, plan):
    cli.cmd_start(ns(session=sid, task="demo-two-stage", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    assert "premise" in store.load(sid).plugins
    _cover_the_order(store, sid)


class TestApproveEndToEnd:
    def test_failed_enumeration_refuses_approve_until_escaped(self, store, fixtures_dir):
        """The whole change in one path: the runner fails, `approve` refuses naming
        the escape command, the escape is recorded, `approve` passes. Before stage 5
        the first approve here returned ok=True."""
        sid = "e2e-escape"
        plan = str(fixtures_dir / "plan_two_stage.toml")
        _to_plan_ready_with_premise(store, sid, plan)
        cli.cmd_question_enumerate(ns(session=sid, plan=None), store=store,
                                   runner=_failing_runner("advisor timed out after 480s"))

        blocked = cli.cmd_approve(ns(session=sid, by="user"), store=store)
        assert blocked.ok is False
        assert any(plugins_premise._ENUMERATE_RUNNER_FAILED in b
                   for b in blocked.data.get("blockers", []))

        # the stderr reached the bag on the SYNCHRONOUS path, which is what let the
        # blocker above pre-select a reason
        bag = store.load(sid).plugins["premise"]
        assert bag["enumerated_runner_ok"] is False
        assert bag["enumerated_runner_stderr"] == "advisor timed out after 480s"

        assert cli.cmd_question_enumerate_escape(
            _escape_ns(sid, premise.ESCAPE_ADVISOR_TIMEOUT,
                       note="480s bound hit on a 40-stage plan"), store=store).ok is True

        assert cli.cmd_approve(ns(session=sid, by="user"), store=store).ok is True

    def test_a_folded_sidecars_stderr_reaches_the_bag(self, store, fixtures_dir, tmp_path,
                                                      monkeypatch):
        """The detached worker is the path this stage was built for, and it is the
        one whose stderr nobody sees live. Drop the fold's `stderr=` and every
        background failure pre-selects `advisor_error`, losing the one distinction
        the calibration work exists to measure."""
        monkeypatch.setenv("CLAUDE_AGENT_HOME", str(tmp_path / "agent-home"))
        sid = "e2e-fold"
        plan = str(fixtures_dir / "plan_two_stage.toml")
        _to_plan_ready_with_premise(store, sid, plan)
        digest = plugins_premise._plan_content_digest(load_plan(plan))
        enumerate_sidecar.write(sid, digest, {
            "runner_ok": False,
            "pairs": [],
            "stderr": "advisor timed out after 480s",
            "content_digest": digest,
            "plan_path": plan,
        })

        blocked = cli.cmd_approve(ns(session=sid, by="user"), store=store)

        assert blocked.ok is False
        assert any(f"--reason {premise.ESCAPE_ADVISOR_TIMEOUT}" in b
                   for b in blocked.data.get("blockers", []))
        bag = store.load(sid).plugins["premise"]
        assert bag["enumerated_runner_ok"] is False
        assert bag["enumerated_runner_stderr"] == "advisor timed out after 480s"


class TestRelaunchRoutesOntoTheEscapableBlocker:
    def test_a_resubmitted_plan_routes_onto_not_run_not_stale(self, store, fixtures_dir):
        """`_launch_enumeration`'s clear is the precondition of this stage's escape:
        the outstanding-child window has to land on the ESCAPABLE `_ENUMERATE_NOT_RUN`
        (whose route out is `enumeration_not_landed`), never on `_ENUMERATE_STALE`,
        which has no escape at all. The replan half of this is proven in
        test_enumerate_detach.py; this is the `submit-plan` resubmission half, where
        a prior discharged enumeration really is left pinned to the OLD digest unless
        the clear runs."""
        sid = "resubmit-routing"
        base = str(fixtures_dir / "plan_two_stage.toml")
        revised = str(fixtures_dir / "plan_two_stage_substantive.toml")
        _to_plan_ready_with_premise(store, sid, base)
        cli.cmd_question_enumerate(ns(session=sid, plan=None), store=store,
                                   runner=lambda argv: RunResult(0, "", ""))
        assert store.load(sid).plugins["premise"]["enumerated"] is True

        cli.cmd_submit_plan(ns(session=sid, plan=revised), store=store)

        reloaded = store.load(sid)
        assert reloaded.plugins["premise"]["enumerated"] is False
        assert reloaded.plugins["premise"]["enumerated_at"] == ""
        blockers = plugins.plugin_gate_blockers(reloaded, "plan_approval")
        assert any(plugins_premise._ENUMERATE_NOT_RUN in b for b in blockers)
        assert not any(plugins_premise._ENUMERATE_STALE in b for b in blockers)

    def test_the_not_run_window_is_escapable_once_its_deadline_passes(
            self, store, fixtures_dir, monkeypatch):
        """Closing the loop the two halves above open: submit-plan stamps a deadline,
        the window blocks on _ENUMERATE_NOT_RUN, and once that deadline is past the
        escape clears it. Without the deadline stamp there would be no admissible
        escape here at all — a wedge, not a gate."""
        sid = "notrun-escape"
        plan = str(fixtures_dir / "plan_two_stage.toml")
        _to_plan_ready_with_premise(store, sid, plan)
        state = store.load(sid)
        assert state.plugins["premise"]["enumerate_deadline"] is not None
        state.plugins["premise"]["enumerate_deadline"] = time.time() - 1
        store.save(state)

        assert cli.cmd_approve(ns(session=sid, by="user"), store=store).ok is False
        assert cli.cmd_question_enumerate_escape(
            _escape_ns(sid, premise.ESCAPE_ENUMERATION_NOT_LANDED,
                       note="the detached worker never landed a sidecar"),
            store=store).ok is True
        assert cli.cmd_approve(ns(session=sid, by="user"), store=store).ok is True
