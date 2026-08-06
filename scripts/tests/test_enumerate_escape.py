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


# --- the fold's own history entry ----------------------------------------------

class TestTheFoldRecordsItsPass:
    """`manual_enumeration_done`'s precondition is an ORDERING over state.history, and
    since the detachment the fold is the path most enumerations arrive on. A fold that
    logged nothing would leave that precondition with no anchor on exactly the sessions
    it governs."""

    def _sidecar(self, sid, digest, plan, **payload):
        enumerate_sidecar.write(sid, digest, {
            "runner_ok": False, "pairs": [], "stderr": "advisor timed out after 480s",
            "content_digest": digest, "plan_path": plan, **payload})

    def test_a_landed_fold_logs_question_enumerate_with_its_runner_health(
            self, store, fixtures_dir, tmp_path, monkeypatch):
        """Remove the fold's `state.log` and this reads zero entries — and every
        `manual_enumeration_done` on a detached session becomes unevaluable, since the
        check would find nothing to order a later question_raise against."""
        monkeypatch.setenv("CLAUDE_AGENT_HOME", str(tmp_path / "agent-home"))
        sid = "fold-logs"
        plan = str(fixtures_dir / "plan_two_stage.toml")
        _to_plan_ready_with_premise(store, sid, plan)
        self._sidecar(sid, plugins_premise._plan_content_digest(load_plan(plan)), plan)

        cli.cmd_approve(ns(session=sid, by="user"), store=store)

        entries = [e for e in store.load(sid).history if e["event"] == "question_enumerate"]
        assert len(entries) == 1, entries
        assert entries[0]["runner_ok"] is False
        assert entries[0]["via"] == "fold"

    def test_the_synchronous_command_names_its_producer_too(self, store, fixtures_dir):
        """`via` is stated on BOTH producers rather than encoded as one's absence: a
        distinction carried by a missing field reads as a forgotten field."""
        sid = "sync-via"
        plan = str(fixtures_dir / "plan_two_stage.toml")
        _to_plan_ready_with_premise(store, sid, plan)
        cli.cmd_question_enumerate(ns(session=sid, plan=None), store=store,
                                   runner=lambda argv: RunResult(0, "", ""))

        entries = [e for e in store.load(sid).history if e["event"] == "question_enumerate"]
        assert [e["via"] for e in entries] == ["command"]

    def test_a_fold_that_does_not_fire_logs_nothing(self, store, fixtures_dir, tmp_path,
                                                    monkeypatch):
        """The history records passes, not attempts. Log on the no-op returns too and
        every gate evaluation would append a phantom `question_enumerate`, moving the
        ordering anchor past a hand re-reading that really did happen."""
        monkeypatch.setenv("CLAUDE_AGENT_HOME", str(tmp_path / "agent-home"))
        sid = "fold-noop"
        plan = str(fixtures_dir / "plan_two_stage.toml")
        _to_plan_ready_with_premise(store, sid, plan)
        # a pass already on record for this exact digest: the sidecar is not even read
        cli.cmd_question_enumerate(ns(session=sid, plan=None), store=store,
                                   runner=lambda argv: RunResult(0, "", ""))
        self._sidecar(sid, plugins_premise._plan_content_digest(load_plan(plan)), plan)

        # through the gate that calls it, then directly and repeatedly: approve is a
        # one-shot transition, so the repetition the phantom entry would come from is
        # only reachable at the fold itself
        cli.cmd_approve(ns(session=sid, by="user"), store=store)
        state = store.load(sid)
        doc = load_plan(plan)
        for _ in range(3):
            assert cli._fold_enumeration_sidecar(state, doc, plan) is False
        store.save(state)

        entries = [e for e in store.load(sid).history if e["event"] == "question_enumerate"]
        assert [e["via"] for e in entries] == ["command"]


# --- manual_enumeration_done's precondition ------------------------------------

class TestManualEnumerationDonePrecondition:
    """The one reason in the closed set that asserts WORK WAS DONE rather than naming a
    failure the engine can see for itself — so it is the one that would otherwise be an
    unconditional click-through wearing a reason token."""

    def _state_with_history(self, plan_path, history):
        state, bag = _bag_state(plan_path, enumerated_runner_ok=False,
                                enumerated_runner_stderr="boom")
        state.history = history
        return state, bag

    def test_refused_when_every_question_raise_predates_the_failed_pass(
            self, store, fixtures_dir):
        """The central test, and the one that fails a DEGENERATE implementation: the
        bag holds a question here (as every substantive plan's does), so an existence
        check would pass. Only the ORDERING refuses."""
        state, bag = self._state_with_history(str(fixtures_dir / "plan_two_stage.toml"), [
            {"event": "question_raise", "question": "Q1", "target": "goal"},
            {"event": "question_enumerate", "raised": 0, "runner_ok": False, "via": "command"},
        ])
        bag["questions"] = [{"id": "Q1", "target": "goal", "question": "why?",
                             "disposition": "open", "reason": "", "research": ""}]
        store.save(state)

        d = cli.cmd_question_enumerate_escape(
            _escape_ns("s", premise.ESCAPE_MANUAL_ENUMERATION_DONE,
                       note="re-read the plan by hand"), store=store)

        assert d.ok is False, d.detail
        assert "PREDATES" in d.detail
        assert store.load("s").plugins["premise"]["escapes"] == []

    def test_accepted_when_a_question_raise_follows_the_failed_pass(
            self, store, fixtures_dir):
        """The other direction — without it the precondition would be a wedge rather
        than a condition, and the reason could never be used at all."""
        state, _ = self._state_with_history(str(fixtures_dir / "plan_two_stage.toml"), [
            {"event": "question_enumerate", "raised": 0, "runner_ok": False, "via": "fold"},
            {"event": "question_raise", "question": "Q2", "target": "stage 2"},
        ])
        store.save(state)

        d = cli.cmd_question_enumerate_escape(
            _escape_ns("s", premise.ESCAPE_MANUAL_ENUMERATION_DONE,
                       note="hand re-reading raised Q2"), store=store)

        assert d.ok is True, d.detail
        assert [r["reason"] for r in store.load("s").plugins["premise"]["escapes"]] == [
            premise.ESCAPE_MANUAL_ENUMERATION_DONE]

    def test_only_the_last_failed_pass_anchors_the_ordering(self, store, fixtures_dir):
        """A question raised after an EARLIER failure says nothing about the pass now
        being escaped. Anchor on the first failed entry instead and one old question
        would discharge every later failure for the rest of the session."""
        state, _ = self._state_with_history(str(fixtures_dir / "plan_two_stage.toml"), [
            {"event": "question_enumerate", "raised": 0, "runner_ok": False, "via": "command"},
            {"event": "question_raise", "question": "Q1", "target": "goal"},
            {"event": "question_enumerate", "raised": 0, "runner_ok": False, "via": "fold"},
        ])
        store.save(state)

        d = cli.cmd_question_enumerate_escape(
            _escape_ns("s", premise.ESCAPE_MANUAL_ENUMERATION_DONE, note="n"), store=store)

        assert d.ok is False, d.detail
        assert "PREDATES" in d.detail

    def test_a_healthy_pass_in_between_does_not_anchor_it(self, store, fixtures_dir):
        """`runner_ok is False`, not merely `event == question_enumerate`: a healthy
        pass is not a failure to have re-read after, and treating it as one would
        refuse a hand re-reading that really did follow the failure."""
        state, _ = self._state_with_history(str(fixtures_dir / "plan_two_stage.toml"), [
            {"event": "question_enumerate", "raised": 0, "runner_ok": False, "via": "command"},
            {"event": "question_raise", "question": "Q1", "target": "goal"},
            {"event": "question_enumerate", "raised": 3, "runner_ok": True, "via": "command"},
        ])
        store.save(state)

        assert cli.cmd_question_enumerate_escape(
            _escape_ns("s", premise.ESCAPE_MANUAL_ENUMERATION_DONE, note="n"),
            store=store).ok is True

    def test_refused_when_no_failed_pass_is_on_record_at_all(self, store, fixtures_dir):
        """A bag saying `runner_ok is False` with no matching history entry — a
        hand-mutated bag, or a session predating the fold's own logging. Refusing is
        the safe answer: the claim has nothing to be ordered against, so it cannot be
        checked, and an unverifiable assertion is exactly what this reason must not
        become. Not a wedge — the other four reasons stay admissible in that state,
        which the second half asserts."""
        state, _ = self._state_with_history(str(fixtures_dir / "plan_two_stage.toml"), [
            {"event": "question_raise", "question": "Q1", "target": "goal"},
        ])
        store.save(state)

        d = cli.cmd_question_enumerate_escape(
            _escape_ns("s", premise.ESCAPE_MANUAL_ENUMERATION_DONE, note="n"), store=store)

        assert d.ok is False, d.detail
        assert "nothing to be ordered against" in d.detail

        assert cli.cmd_question_enumerate_escape(
            _escape_ns("s", premise.ESCAPE_ADVISOR_ERROR, note="n"), store=store).ok is True

    def test_the_other_reasons_carry_no_ordering_condition(self, store, fixtures_dir):
        """Only `manual_enumeration_done` asserts work; the rest name a failure the
        engine already sees. Extend the condition to them and a session whose advisor
        is simply down could never escape."""
        state, _ = self._state_with_history(str(fixtures_dir / "plan_two_stage.toml"), [
            {"event": "question_enumerate", "raised": 0, "runner_ok": False, "via": "fold"},
        ])
        store.save(state)

        assert cli.cmd_question_enumerate_escape(
            _escape_ns("s", premise.ESCAPE_ADVISOR_TIMEOUT, note="n"), store=store).ok is True

    def test_end_to_end_through_the_real_verbs(self, store, fixtures_dir):
        """Hand-built histories can only prove the predicate; this proves the entries
        it reads are the ones production actually writes — with a question raised
        BEFORE the failure, so an existence check would pass here too."""
        sid = "manual-e2e"
        plan = str(fixtures_dir / "plan_two_stage.toml")
        _to_plan_ready_with_premise(store, sid, plan)
        cli.cmd_question_raise(ns(session=sid, id="Q1", target="goal",
                                  question="does the goal hold?"), store=store)
        cli.cmd_question_enumerate(ns(session=sid, plan=None), store=store,
                                   runner=_failing_runner("boom"))

        escape = _escape_ns(sid, premise.ESCAPE_MANUAL_ENUMERATION_DONE,
                            note="re-read every stage by hand")
        assert cli.cmd_question_enumerate_escape(escape, store=store).ok is False

        cli.cmd_question_raise(ns(session=sid, id="Q2", target="stage 2",
                                  question="what the hand re-reading found"), store=store)

        assert cli.cmd_question_enumerate_escape(escape, store=store).ok is True
        assert [r["reason"] for r in store.load(sid).plugins["premise"]["escapes"]] == [
            premise.ESCAPE_MANUAL_ENUMERATION_DONE]


# --- the post-pass advisory's three arms ----------------------------------------

class TestEnumerateAdvisoryArms:
    """`runner_ok` is three-valued and the three states now have three different
    truths. Nothing else in the suite reads advisory text, so a wrong arm would ship
    green — which is why each is asserted on the actual string."""

    def _advisories(self, store, sid, fixtures_dir, runner):
        plan = str(fixtures_dir / "plan_two_stage.toml")
        _to_plan_ready_with_premise(store, sid, plan)
        d = cli.cmd_question_enumerate(ns(session=sid, plan=None), store=store, runner=runner)
        return d.data.get("advisories", [])

    def test_a_failed_runner_says_blocked_and_names_the_pre_selected_reason(
            self, store, fixtures_dir):
        """The arm whose old text became FALSE the moment the blocker landed: this
        pass discharges nothing. Leave the old wording and the command tells the
        coordinator to proceed at the exact moment the gate refuses to."""
        adv = self._advisories(store, "arm-false", fixtures_dir,
                               _failing_runner("advisor timed out after 480s"))

        assert len(adv) == 1, adv
        assert "BLOCKED" in adv[0]
        assert "discharged the mandatory cross-check on the flag alone" not in adv[0]
        assert f"--reason {premise.ESCAPE_ADVISOR_TIMEOUT}" in adv[0]

    def test_an_absent_advisor_keeps_the_discharge_wording(self, store, fixtures_dir,
                                                           monkeypatch):
        """None is not a failure — the gate does not block on it. Fold it into the
        failure arm and every advisor-less session is told it is blocked when it is
        not, and sent to an escape the engine would refuse."""
        monkeypatch.setattr(advisor, "enumerate_subprocess_runner", None)
        adv = self._advisories(store, "arm-none", fixtures_dir, None)

        assert len(adv) == 1, adv
        assert "discharged the mandatory cross-check on the flag alone" in adv[0]
        assert "unavailable" in adv[0]
        assert "BLOCKED" not in adv[0]

    def test_a_healthy_pass_with_zero_pairs_keeps_the_zero_pair_wording(
            self, store, fixtures_dir):
        """A question-free plan is a HEALTHY pass; the advisory asks for the second
        reading without implying anything failed."""
        adv = self._advisories(store, "arm-empty", fixtures_dir,
                               lambda argv: RunResult(0, "", ""))

        assert len(adv) == 1, adv
        assert "the pass raised no questions" in adv[0]
        assert "BLOCKED" not in adv[0]

    def test_a_healthy_pass_with_pairs_attaches_no_advisory_at_all(
            self, store, fixtures_dir):
        adv = self._advisories(store, "arm-ok", fixtures_dir,
                               lambda argv: RunResult(0, "stage 1\tdoes the bound hold?\n", ""))

        assert adv == []
