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
    return lambda argv, **_kw: RunResult(1, "", stderr)


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

    def test_a_repeat_escape_is_recorded_again_and_says_so(self, store, fixtures_dir):
        """Re-running the same escape against the same bytes unblocks nothing — the
        blocker was already clear — but it is still counted, deliberately: the rate is
        this stage's refutation instrument, and a silently-deduped second escape is a
        use of the gate that the number never sees. What must NOT happen is the command
        reporting the second row as though it had just bought the approve, so the
        Directive says one was already on record and carries `already_recorded` for a
        caller reading the payload rather than the prose. Both rows are asserted on
        RELOADED state: dedupe the append and the second assert fails; drop the message
        and the first pair does."""
        plan_path = str(fixtures_dir / "plan_two_stage.toml")
        state, _ = _bag_state(plan_path, enumerated_runner_ok=False,
                              enumerated_runner_stderr="boom")
        store.save(state)
        args = _escape_ns("s", premise.ESCAPE_ADVISOR_ERROR, note="the advisor blew up")

        first = cli.cmd_question_enumerate_escape(args, store=store)
        second = cli.cmd_question_enumerate_escape(args, store=store)

        assert first.ok is True and first.data["already_recorded"] is False
        assert "ALREADY discharged" not in first.detail
        assert second.ok is True, second.detail
        assert second.data["already_recorded"] is True
        assert "ALREADY discharged" in second.detail
        assert [r["reason"] for r in store.load("s").plugins["premise"]["escapes"]] == [
            premise.ESCAPE_ADVISOR_ERROR, premise.ESCAPE_ADVISOR_ERROR]

    def test_a_cross_reason_repeat_is_also_already_discharged(self, store, fixtures_dir):
        """`premise_blockers` clears the runner-failure branch on ANY reason in
        `ENUMERATION_RUNNER_FAILURE_REASONS` (see plugins_premise.premise_blockers's
        elif chain) — not on the one reason the first escape happened to name. So
        escaping `advisor_timeout` against a digest that already carries an
        `advisor_error` escape must report `already_recorded` too: the blocker for
        these bytes was already clear before this second, DIFFERENT-reason row
        landed. Computing `already` from `(reason,)` alone (the pre-fix code) would
        report this as a fresh discharge — asserted on RELOADED state, per the
        codebase's persisted-state convention, so a fix that only patches the
        in-memory Directive would still fail here."""
        plan_path = str(fixtures_dir / "plan_two_stage.toml")
        state, _ = _bag_state(plan_path, enumerated_runner_ok=False,
                              enumerated_runner_stderr="boom")
        store.save(state)

        first = cli.cmd_question_enumerate_escape(
            _escape_ns("s", premise.ESCAPE_ADVISOR_ERROR, note="the advisor blew up"),
            store=store)
        second = cli.cmd_question_enumerate_escape(
            _escape_ns("s", premise.ESCAPE_ADVISOR_TIMEOUT, note="it also timed out once"),
            store=store)

        assert first.ok is True and first.data["already_recorded"] is False
        assert second.ok is True, second.detail
        assert second.data["already_recorded"] is True
        assert "ALREADY discharged" in second.detail
        reloaded = store.load("s")
        assert [r["reason"] for r in reloaded.plugins["premise"]["escapes"]] == [
            premise.ESCAPE_ADVISOR_ERROR, premise.ESCAPE_ADVISOR_TIMEOUT]
        assert plugins.plugin_gate_blockers(reloaded, "plan_approval") == []


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
        to state.plan_path could never unblock the replan it exists for.

        The bag is in the state cmd_replan's own fold leaves behind: the failed pass
        on record is the CORRECTED plan's, which is what makes the escape admissible
        for those bytes (see the digest-agreement test below)."""
        corrected = str(fixtures_dir / "plan_two_stage_substantive.toml")
        state, bag = _bag_state(str(fixtures_dir / "plan_two_stage.toml"),
                                enumerated_runner_ok=False)
        bag["enumerated_at"] = plugins_premise._plan_content_digest(load_plan(corrected))
        store.save(state)

        d = cli.cmd_question_enumerate_escape(
            _escape_ns("s", premise.ESCAPE_ADVISOR_ERROR, plan=corrected), store=store)

        assert d.ok is True, d.detail
        recorded = store.load("s").plugins["premise"]["escapes"][0]
        assert recorded["plan"] == corrected
        assert recorded["content_digest"] == plugins_premise._plan_content_digest(
            load_plan(corrected))

    def test_a_runner_failure_escape_is_refused_ahead_of_the_pass_it_claims_to_escape(
            self, store, fixtures_dir):
        """The escape binds PER DIGEST while `enumerated_runner_ok` is session-GLOBAL,
        so without a digest-agreement check a `False` left by a SUPERSEDED pass admits
        an escape recorded AHEAD of the plan content it names — and when that content's
        own pass later lands and fails, escape_recorded matches on (digest, reason) and
        clears the blocker. The failure is then never surfaced to anyone: the fail-open
        this gate exists to close, one level in.

        Both halves are asserted, because the refusal alone would also pass if the
        escape were merely dropped: the second half replays the sequence the escape was
        aimed at — the D2 pass lands failing — and shows the blocker still standing."""
        base = str(fixtures_dir / "plan_two_stage.toml")
        corrected = str(fixtures_dir / "plan_two_stage_substantive.toml")
        state, _ = _bag_state(base, enumerated_runner_ok=False,
                              enumerated_runner_stderr="advisor timed out after 480s")
        store.save(state)

        d = cli.cmd_question_enumerate_escape(
            _escape_ns("s", premise.ESCAPE_ADVISOR_TIMEOUT, plan=corrected), store=store)

        assert d.ok is False, d.detail
        # the refusal says WHICH pass the bag's failure speaks for, and what to do
        assert "question-enumerate" in d.detail
        assert store.load("s").plugins["premise"]["escapes"] == []

        # ...and the D2 failure, once it does land, therefore still blocks
        landed = store.load("s")
        landed.plan_path = corrected
        landed.plugins["premise"]["enumerated_at"] = plugins_premise._plan_content_digest(
            load_plan(corrected))
        store.save(landed)

        blockers = plugins.plugin_gate_blockers(store.load("s"), "plan_approval")
        assert any(plugins_premise._ENUMERATE_RUNNER_FAILED in b for b in blockers)


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

    def test_quota_refusal_gets_its_own_reason(self):
        """A session-limit refusal names a resource ceiling, not a broken runner:
        it must land in its own bucket so a fleet-wide quota exhaustion is
        visible instead of vanishing into the generic-error tally. Wording is
        the one observed verbatim from the host CLI."""
        assert advisor.classify_runner_failure(
            "You've hit your session limit · resets 12am (Europe/Moscow)"
        ) == premise.ESCAPE_ADVISOR_QUOTA

    def test_a_missing_credential_gets_its_own_reason_ahead_of_the_catch_all(self):
        """"We could not read a local credential" and "the service refused us for
        quota" have different operators and different fixes: the first is ours to
        repair, the second is waiting. The stderr is the one subprocess_runner
        itself writes; the end-to-end production of it from an unauthenticated
        world lives in test_advisor.py."""
        assert advisor.classify_runner_failure(
            f"{advisor._CREDENTIAL_STDERR_PREFIX}\nInvalid API key"
        ) == premise.ESCAPE_ADVISOR_CREDENTIAL

    def test_a_missing_credential_wins_over_a_quota_mention(self):
        """A child that never authenticated cannot have been refused for quota, so
        a session-limit phrase in its output must not re-label the one failure this
        seam can itself cause — and mask the local fix."""
        assert advisor.classify_runner_failure(
            f"{advisor._CREDENTIAL_STDERR_PREFIX}\nYou've hit your session limit"
        ) == premise.ESCAPE_ADVISOR_CREDENTIAL

    def test_quota_match_is_case_insensitive(self):
        assert advisor.classify_runner_failure(
            "Error: Session Limit reached") == premise.ESCAPE_ADVISOR_QUOTA

    def test_timeout_wins_over_a_quota_mention(self):
        """The timeout arm is the stderr this process itself wrote, so it stays
        first: a quota phrase appearing inside a timeout diagnostic must not
        re-label a runner this side knows timed out."""
        assert advisor.classify_runner_failure(
            f"{advisor._TIMEOUT_STDERR_PREFIX} 480s (session limit?)"
        ) == premise.ESCAPE_ADVISOR_TIMEOUT

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
                                   runner=lambda argv, **_kw: RunResult(0, "", ""))
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


# --- an escape speaks for ONE window and ONE pass, not for the bytes forever ----

class TestEscapeBindsToTheLaunchAndThePassItEscapes:
    """The fix's own defect class, one level in. Binding an escape to the content
    digest ALONE makes it a statement about one plan's EVERY pass — which is what the
    `escape_recorded` docstring already said it must not be. Both holes below leave a
    gate that is supposed to block reachable in a discharged state; the second also
    silently disarms the counter this stage's refutation depends on."""

    def test_a_relaunch_over_the_same_bytes_does_not_inherit_the_old_escape(
            self, store, fixtures_dir):
        """Variant A — discharged although it never ran. `cmd_submit_plan` calls
        `_launch_enumeration` unconditionally, so a resubmission of byte-identical
        plan content opens a NEW window: `enumerated` is cleared, the deadline is
        restamped, and nothing has been enumerated for it. Matching on the digest
        alone, `approve` then finds the PREVIOUS window's `enumeration_not_landed`
        row and discharges instantly — the new window gets no enumeration, no wait,
        and no new escape row.

        Asserted through `store.load`, per this module's convention: the whole
        question is what the next process sees."""
        sid = "relaunch-not-inherited"
        plan = str(fixtures_dir / "plan_two_stage.toml")
        _to_plan_ready_with_premise(store, sid, plan)

        # window 1: nothing lands, its deadline passes, the operator escapes
        state = store.load(sid)
        state.plugins["premise"]["enumerate_deadline"] = time.time() - 1
        store.save(state)
        assert cli.cmd_question_enumerate_escape(
            _escape_ns(sid, premise.ESCAPE_ENUMERATION_NOT_LANDED,
                       note="window 1's worker never landed a sidecar"),
            store=store).ok is True
        assert plugins.plugin_gate_blockers(store.load(sid), "plan_approval") == []

        # window 2: the same bytes resubmitted — a fresh launch, a fresh deadline
        cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
        reloaded = store.load(sid)
        assert reloaded.plugins["premise"]["enumerated"] is False
        assert reloaded.plugins["premise"]["enumerate_launch"] == 2

        blockers = plugins.plugin_gate_blockers(reloaded, "plan_approval")
        assert any(plugins_premise._ENUMERATE_NOT_RUN in b for b in blockers), blockers
        assert cli.cmd_approve(ns(session=sid, by="user"), store=store).ok is False

        # ...and the route out is a SECOND escape, which the counter therefore sees
        state = store.load(sid)
        state.plugins["premise"]["enumerate_deadline"] = time.time() - 1
        store.save(state)
        assert cli.cmd_question_enumerate_escape(
            _escape_ns(sid, premise.ESCAPE_ENUMERATION_NOT_LANDED,
                       note="window 2's worker never landed either"),
            store=store).ok is True
        assert cli.cmd_approve(ns(session=sid, by="user"), store=store).ok is True
        bag = store.load(sid).plugins["premise"]
        assert len(bag["escapes"]) == 2
        assert [r["enumerate_launch"] for r in bag["escapes"]] == [1, 2]
        assert plugins_premise.escape_counts(
            bag, bag["escapes"][-1]["content_digest"])["session"]["not_landed"] == 2

    def test_a_second_failed_pass_at_the_same_digest_re_blocks_and_is_counted(
            self, store, fixtures_dir):
        """Variant B — the second failure is uncounted. After a runner-failure escape
        at digest D, the operator re-runs `question-enumerate` hoping the advisor
        recovered, and it fails again. On the digest alone that second failure is
        discharged with no blocker and no new row, so `escape_counts` never sees it —
        and a counter that undercounts cannot refute the claim ('refuted if the escape
        degrades into a click-through') it exists to test."""
        sid = "second-failure-counted"
        plan = str(fixtures_dir / "plan_two_stage.toml")
        _to_plan_ready_with_premise(store, sid, plan)
        failing = _failing_runner("advisor timed out after 480s")

        cli.cmd_question_enumerate(ns(session=sid, plan=None), store=store, runner=failing)
        assert cli.cmd_approve(ns(session=sid, by="user"), store=store).ok is False
        assert cli.cmd_question_enumerate_escape(
            _escape_ns(sid, premise.ESCAPE_ADVISOR_TIMEOUT, note="pass 1 timed out"),
            store=store).ok is True
        assert plugins.plugin_gate_blockers(store.load(sid), "plan_approval") == []

        # the operator retries the check on the SAME bytes; it fails again
        cli.cmd_question_enumerate(ns(session=sid, plan=None), store=store, runner=failing)
        reloaded = store.load(sid)
        assert reloaded.plugins["premise"]["enumerate_pass"] == 2
        assert any(plugins_premise._ENUMERATE_RUNNER_FAILED in b
                   for b in plugins.plugin_gate_blockers(reloaded, "plan_approval"))
        assert cli.cmd_approve(ns(session=sid, by="user"), store=store).ok is False

        assert cli.cmd_question_enumerate_escape(
            _escape_ns(sid, premise.ESCAPE_ADVISOR_TIMEOUT, note="pass 2 timed out too"),
            store=store).ok is True
        assert cli.cmd_approve(ns(session=sid, by="user"), store=store).ok is True
        bag = store.load(sid).plugins["premise"]
        assert [r["enumerate_pass"] for r in bag["escapes"]] == [1, 2]
        assert plugins_premise.escape_counts(
            bag, bag["enumerated_at"])["this_plan"]["runner_failure"] == 2

    def test_the_within_window_flow_still_approves_with_one_escape(
            self, store, fixtures_dir):
        """The over-blocking check the two above must not cost. With no relaunch and
        no new pass between the escape and the approve, ONE escape still discharges —
        the counters are an identity for the window, not a per-command nonce."""
        sid = "within-window"
        plan = str(fixtures_dir / "plan_two_stage.toml")
        _to_plan_ready_with_premise(store, sid, plan)
        cli.cmd_question_enumerate(ns(session=sid, plan=None), store=store,
                                   runner=_failing_runner("advisor timed out after 480s"))

        assert cli.cmd_question_enumerate_escape(
            _escape_ns(sid, premise.ESCAPE_ADVISOR_TIMEOUT, note="the one failure"),
            store=store).ok is True
        assert plugins.plugin_gate_blockers(store.load(sid), "plan_approval") == []
        assert cli.cmd_approve(ns(session=sid, by="user"), store=store).ok is True
        assert len(store.load(sid).plugins["premise"]["escapes"]) == 1

    def test_a_legacy_escape_row_without_the_counters_fails_closed(self, fixtures_dir):
        """The second design decision, stated as a test: a row minted before the
        counters existed does NOT match. This is a fail-open fix, so the ambiguous
        row must block; the cost is one extra escape on a session carried across the
        change, against a hole that would otherwise stay open for exactly the bags
        most likely to have one."""
        plan_path = str(fixtures_dir / "plan_two_stage.toml")
        state, bag = _bag_state(plan_path, enumerated_runner_ok=False,
                                enumerated_runner_stderr="boom")
        digest = plugins_premise._plan_content_digest(load_plan(plan_path))
        bag["escapes"] = [{"reason": premise.ESCAPE_ADVISOR_TIMEOUT, "note": "n",
                           "content_digest": digest, "runner_ok": False,
                           "plan": plan_path}]

        assert plugins_premise.escape_recorded(
            bag, digest, premise.ENUMERATION_RUNNER_FAILURE_REASONS) is False
        assert any(plugins_premise._ENUMERATE_RUNNER_FAILED in b
                   for b in plugins.plugin_gate_blockers(state, "plan_approval"))

    def test_a_retried_replan_inside_one_window_does_not_reopen_it(
            self, store, fixtures_dir, monkeypatch):
        """The liveness half of binding to the launch counter. `cmd_replan` relaunches
        whenever the proposed digest differs from `enumerated_at` — which it always
        does while a window is outstanding, since the launch clears that field. So a
        replan retried after its escape would bump the counter past the row the
        operator just recorded and re-block forever: a wedge, not a gate. A window
        already outstanding for these exact bytes is therefore not reopened."""
        monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
        sid = "replan-retry"
        base = str(fixtures_dir / "plan_two_stage.toml")
        corrected = str(fixtures_dir / "plan_two_stage_substantive.toml")
        _to_plan_ready_with_premise(store, sid, base)
        cli.cmd_approve(ns(session=sid, by="user"), store=store)

        # the corrected plan opens its own window, and nothing lands in it
        assert cli.cmd_replan(ns(session=sid, plan=corrected), store=store).ok is False
        launch = store.load(sid).plugins["premise"]["enumerate_launch"]
        state = store.load(sid)
        state.plugins["premise"]["enumerate_deadline"] = time.time() - 1
        store.save(state)
        assert cli.cmd_question_enumerate_escape(
            _escape_ns(sid, premise.ESCAPE_ENUMERATION_NOT_LANDED, plan=corrected,
                       note="the corrected plan's worker never landed"),
            store=store).ok is True

        retried = cli.cmd_replan(ns(session=sid, plan=corrected), store=store)

        assert store.load(sid).plugins["premise"]["enumerate_launch"] == launch
        assert not any(plugins_premise._ENUMERATE_NOT_RUN in b
                       for b in retried.data.get("blockers", [])), retried.detail


# --- a window's child that comes back late, after its escape -------------------

class TestALateSidecarLandingAfterAnEscape:
    """A `not_landed` escape says the window's child never came back — but nothing
    stops it coming back late, after the escape is on record. Both arms are load-
    bearing and neither was exercised: a healthy late landing must not leave the gate
    wedged behind counters that have moved past the escape, and a failing one must
    re-block with a route out rather than a dead end."""

    def _escaped_window(self, store, sid, plan):
        _to_plan_ready_with_premise(store, sid, plan)
        state = store.load(sid)
        state.plugins["premise"]["enumerate_deadline"] = time.time() - 1
        store.save(state)
        assert cli.cmd_question_enumerate_escape(
            _escape_ns(sid, premise.ESCAPE_ENUMERATION_NOT_LANDED,
                       note="the worker had not landed by the deadline"),
            store=store).ok is True
        assert plugins.plugin_gate_blockers(store.load(sid), "plan_approval") == []

    def test_a_healthy_late_landing_does_not_wedge_the_gate(
            self, store, fixtures_dir, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_AGENT_HOME", str(tmp_path / "agent-home"))
        sid = "late-fold-ok"
        plan = str(fixtures_dir / "plan_two_stage.toml")
        self._escaped_window(store, sid, plan)

        digest = plugins_premise._plan_content_digest(load_plan(plan))
        enumerate_sidecar.write(sid, digest, {
            "runner_ok": True, "pairs": [], "stderr": "",
            "content_digest": digest, "plan_path": plan,
        })

        assert cli.cmd_approve(ns(session=sid, by="user"), store=store).ok is True
        bag = store.load(sid).plugins["premise"]
        assert bag["enumerated"] is True
        assert bag["enumerated_at"] == digest
        assert bag["enumerated_runner_ok"] is True
        assert len(bag["escapes"]) == 1

    def test_a_failing_late_landing_re_blocks_and_keeps_a_route_out(
            self, store, fixtures_dir, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_AGENT_HOME", str(tmp_path / "agent-home"))
        sid = "late-fold-failed"
        plan = str(fixtures_dir / "plan_two_stage.toml")
        self._escaped_window(store, sid, plan)

        digest = plugins_premise._plan_content_digest(load_plan(plan))
        enumerate_sidecar.write(sid, digest, {
            "runner_ok": False, "pairs": [], "stderr": "advisor timed out after 480s",
            "content_digest": digest, "plan_path": plan,
        })

        blocked = cli.cmd_approve(ns(session=sid, by="user"), store=store)
        assert blocked.ok is False
        assert any(plugins_premise._ENUMERATE_RUNNER_FAILED in b
                   for b in blocked.data.get("blockers", []))

        assert cli.cmd_question_enumerate_escape(
            _escape_ns(sid, premise.ESCAPE_ADVISOR_TIMEOUT,
                       note="the late landing reported a timeout"), store=store).ok is True
        assert cli.cmd_approve(ns(session=sid, by="user"), store=store).ok is True
        assert [r["reason"] for r in store.load(sid).plugins["premise"]["escapes"]] == [
            premise.ESCAPE_ENUMERATION_NOT_LANDED, premise.ESCAPE_ADVISOR_TIMEOUT]

    def test_a_late_landing_onto_an_already_enumerated_digest_stays_a_no_op(
            self, store, fixtures_dir, tmp_path, monkeypatch):
        """Why the two arms above re-block on the REASON family and never on the pass
        counter: the fold refuses to apply at all once the bag records an enumeration
        for this digest, so a late sidecar cannot stack a second pass on top of a
        synchronous one. `enumerate_pass` therefore distinguishes passes only on the
        synchronous retry path — pinned here so the untested-looking gap is read as
        unreachable rather than as missing coverage."""
        monkeypatch.setenv("CLAUDE_AGENT_HOME", str(tmp_path / "agent-home"))
        sid = "late-fold-noop"
        plan = str(fixtures_dir / "plan_two_stage.toml")
        _to_plan_ready_with_premise(store, sid, plan)
        cli.cmd_question_enumerate(ns(session=sid, plan=None), store=store,
                                   runner=_failing_runner("advisor timed out after 480s"))
        assert cli.cmd_question_enumerate_escape(
            _escape_ns(sid, premise.ESCAPE_ADVISOR_TIMEOUT, note="pass 1 timed out"),
            store=store).ok is True

        digest = plugins_premise._plan_content_digest(load_plan(plan))
        enumerate_sidecar.write(sid, digest, {
            "runner_ok": False, "pairs": [], "stderr": "a second timeout, landing late",
            "content_digest": digest, "plan_path": plan,
        })

        assert cli.cmd_approve(ns(session=sid, by="user"), store=store).ok is True
        bag = store.load(sid).plugins["premise"]
        assert bag["enumerate_pass"] == 1
        assert bag["enumerated_runner_stderr"] == "advisor timed out after 480s"


# --- submit-plan reopens a window where replan suppresses one ------------------

class TestSubmitPlanReopensAWindowUnconditionally:
    def test_a_digest_preserving_resubmit_reopens_the_window_and_costs_a_second_escape(
            self, store, fixtures_dir, tmp_path):
        """`cmd_submit_plan` relaunches unconditionally where `cmd_replan` suppresses a
        relaunch inside an outstanding window. The asymmetry is deliberate — a resubmit
        is a deliberate act, and this whole path exists because a mandatory check never
        ran, so giving it another chance to genuinely execute is the trade this task's
        premise says to take — but it is NOT free, and this pins the price rather than
        leaving it to be rediscovered.

        `final_check` edits and comments sit outside `_plan_content_digest` by design,
        so a resubmit touching only those changes no content and still opens a fresh
        window: the escape recorded seconds earlier stops discharging, the waited-out
        deadline is restamped, and a second `not_landed` row inflates `escape_counts` —
        the instrument this stage's refutable principle turns on. An accepted cost, not
        desirable behaviour."""
        sid = "digest-preserving-resubmit"
        plan = fixtures_dir / "plan_two_stage.toml"
        _to_plan_ready_with_premise(store, sid, str(plan))

        state = store.load(sid)
        state.plugins["premise"]["enumerate_deadline"] = time.time() - 1
        store.save(state)
        assert cli.cmd_question_enumerate_escape(
            _escape_ns(sid, premise.ESCAPE_ENUMERATION_NOT_LANDED,
                       note="window 1 never landed"), store=store).ok is True
        assert plugins.plugin_gate_blockers(store.load(sid), "plan_approval") == []

        commented = tmp_path / "plan_two_stage.toml"
        commented.write_text(plan.read_text(encoding="utf-8")
                             + "\n# a comment, which the content digest ignores\n",
                             encoding="utf-8")
        assert (plugins_premise._plan_content_digest(load_plan(str(commented)))
                == plugins_premise._plan_content_digest(load_plan(str(plan))))

        cli.cmd_submit_plan(ns(session=sid, plan=str(commented)), store=store)

        reloaded = store.load(sid)
        assert reloaded.plugins["premise"]["enumerate_launch"] == 2
        assert reloaded.plugins["premise"]["enumerate_deadline"] > time.time()
        assert any(plugins_premise._ENUMERATE_NOT_RUN in b
                   for b in plugins.plugin_gate_blockers(reloaded, "plan_approval"))

        state = store.load(sid)
        state.plugins["premise"]["enumerate_deadline"] = time.time() - 1
        store.save(state)
        assert cli.cmd_question_enumerate_escape(
            _escape_ns(sid, premise.ESCAPE_ENUMERATION_NOT_LANDED,
                       note="window 2 never landed either"), store=store).ok is True
        bag = store.load(sid).plugins["premise"]
        assert len(bag["escapes"]) == 2
        assert plugins_premise.escape_counts(
            bag, bag["escapes"][-1]["content_digest"])["session"]["not_landed"] == 2


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
                                   runner=lambda argv, **_kw: RunResult(0, "", ""))

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
                                   runner=lambda argv, **_kw: RunResult(0, "", ""))
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


# --- the escape counters, on both surfaces --------------------------------------

class TestEscapeCountsAreVisible:
    """This stage's own refutation is the escape RATE: "refuted if the escape is taken
    so routinely that it degrades into a click-through". An escape mechanism whose rate
    nobody can see is the fail-open it replaced, one level up — so the two surfaces are
    the deliverable, and each is tested against the payload a caller actually reads.

    Two axes, deliberately not merged. `this_plan` is what means something AT THE GATE;
    `session` resets with `agentctl reset`, which is why it cannot be the only place a
    rising rate would show. And within each axis the three escape families — a failed
    runner, a hand re-reading, a pass that never landed — are counted apart: collapsing
    any two hides whichever is rarer, and calls for the wrong fix."""

    def test_advisor_unavailable_tallies_as_runner_failure(self):
        """`_tally` reads the infra/work-was-done split off
        `premise.ENUMERATION_INFRA_FAILURE_REASONS` — the closed set naming the FIVE
        infra reasons — rather than re-deriving it as "in the wider family and not
        manual". `advisor_unavailable` is the one member of that set no end-to-end
        test above ever produces (the blocker never pre-selects it; only an operator
        who knows the advisor was stubbed out reaches for it), so nothing else in
        this file would catch a future edit that dropped it from the infra tuple, or
        a `_tally` rewrite that went back to deriving the bucket by exclusion and
        missed it. Calls `plugins_premise._tally` directly, the same pure function
        `escape_counts` (and so both `agentctl status` and the refusal payload)
        delegates to."""
        counts = plugins_premise._tally([{"reason": premise.ESCAPE_ADVISOR_UNAVAILABLE}])

        assert counts == {"runner_failure": 1, "manual": 0, "not_landed": 0}

    def _escaped_both_ways(self, store, plan_path, sid="counts"):
        """One session, both families, across the TWO bag states in which each is
        admissible — in the order a real session meets them. First a landed pass whose
        runner failed (escaped `advisor_error`), then the relaunch window it opens:
        `enumerated` cleared back to not-run with the deadline past (escaped
        `enumeration_not_landed`). Both bind the SAME plan content, which is what makes
        the per-plan axis below count two.

        These are deliberately not folded into one bag. A single state carrying a
        superseded pass's `enumerated_runner_ok=False` alongside `enumerated_at=""` is
        precisely the gap
        `test_a_runner_failure_escape_is_refused_ahead_of_the_pass_it_claims_to_escape`
        closes — a fixture is not a reason to keep admitting it."""
        state, _ = _bag_state(plan_path, enumerated_runner_ok=False,
                              enumerated_runner_stderr="boom")
        state.session_id = sid
        store.save(state)
        assert cli.cmd_question_enumerate_escape(
            _escape_ns(sid, premise.ESCAPE_ADVISOR_ERROR,
                       note="escaping the failed pass"), store=store).ok is True

        relaunched = store.load(sid)
        relaunched.plugins["premise"].update(
            enumerated=False, enumerated_at="", enumerate_deadline=time.time() - 1)
        store.save(relaunched)
        assert cli.cmd_question_enumerate_escape(
            _escape_ns(sid, premise.ESCAPE_ENUMERATION_NOT_LANDED,
                       note="no child ever landed"), store=store).ok is True
        return sid

    def test_status_reports_both_axes_with_the_two_families_apart(self, store, fixtures_dir):
        """Merge the families and this reads `2` twice, which is the same number a
        session that timed out twice would show — and the two call for opposite fixes
        (raise the bound vs. chase a worker that never lands)."""
        sid = self._escaped_both_ways(store, str(fixtures_dir / "plan_two_stage.toml"))

        counts = cli.cmd_status(ns(session=sid), store=store).data["enumeration_escapes"]

        assert counts["this_plan"] == {"runner_failure": 1, "manual": 0, "not_landed": 1}
        assert counts["session"] == {"runner_failure": 1, "manual": 0, "not_landed": 1}

    def test_a_hand_re_reading_is_counted_apart_from_the_infrastructure_failures(
            self, store, fixtures_dir):
        """`manual_enumeration_done` is admitted by the SAME condition as the other
        runner-failure reasons — a landed pass whose runner failed — but it reports the
        opposite fact about the fleet: somebody did the cross-check by hand. Folded into
        `runner_failure` (as it was), a fleet that always re-reads and a fleet that
        always clicks through report the identical number, and the refutation this stage
        is measured against ("refuted if the escape degrades into a click-through") has
        no observable left. Both halves are asserted, because a `manual` bucket that
        merely double-counted into `runner_failure` would satisfy the first alone."""
        sid = "manual-counts"
        plan = str(fixtures_dir / "plan_two_stage.toml")
        _to_plan_ready_with_premise(store, sid, plan)
        cli.cmd_question_enumerate(ns(session=sid, plan=None), store=store,
                                   runner=_failing_runner("boom"))
        cli.cmd_question_raise(ns(session=sid, id="Q1", target="stage 2",
                                  question="what the hand re-reading found"), store=store)
        assert cli.cmd_question_enumerate_escape(
            _escape_ns(sid, premise.ESCAPE_MANUAL_ENUMERATION_DONE,
                       note="re-read every stage by hand"), store=store).ok is True

        counts = cli.cmd_status(ns(session=sid), store=store).data["enumeration_escapes"]

        assert counts["this_plan"] == {"runner_failure": 0, "manual": 1, "not_landed": 0}
        assert counts["session"] == {"runner_failure": 0, "manual": 1, "not_landed": 0}

    def test_an_escape_against_superseded_plan_content_leaves_only_the_session_count(
            self, store, fixtures_dir, tmp_path):
        """The reason both axes exist. Report only `this_plan` and a plan edited after
        each escape shows a permanent zero however often the gate was escaped; report
        only `session` and the number at the gate is about a plan version that no
        longer exists."""
        plan_path = tmp_path / "plan.toml"
        plan_path.write_text((fixtures_dir / "plan_two_stage.toml").read_text(encoding="utf-8"),
                             encoding="utf-8")
        sid = self._escaped_both_ways(store, str(plan_path), sid="superseded")
        plan_path.write_text(
            (fixtures_dir / "plan_two_stage_substantive.toml").read_text(encoding="utf-8"),
            encoding="utf-8")

        counts = cli.cmd_status(ns(session=sid), store=store).data["enumeration_escapes"]

        assert counts["this_plan"] == {"runner_failure": 0, "manual": 0, "not_landed": 0}
        assert counts["session"] == {"runner_failure": 1, "manual": 0, "not_landed": 1}

    def test_no_premise_bag_reports_not_applicable_rather_than_zero(self, store):
        """Most sessions never arm the premise plugin. A zero there would read as
        'this gate was never escaped' when the truth is 'this gate does not apply',
        and an escape rate computed over both is meaningless."""
        state = SessionState(session_id="nobag", task_id="t")
        store.save(state)

        assert cli.cmd_status(ns(session="nobag"), store=store).data[
            "enumeration_escapes"] is None

    def test_a_bag_with_no_plan_yet_reports_a_null_per_plan_axis_and_a_real_session_zero(
            self, store):
        """The distinction one level down, and the state cmd_status must not raise in:
        the bag exists, so the session axis is a MEASURED zero, but there is no plan
        version for a per-version count to be about. The bag also carries none of this
        half's keys, as one minted before it existed would not."""
        state = SessionState(session_id="noplan", task_id="t",
                             weight_class=WeightClass.SUBSTANTIVE.value)
        plugins.activate(state, "premise")
        state.plugins["premise"].pop("escapes", None)
        store.save(state)

        counts = cli.cmd_status(ns(session="noplan"), store=store).data["enumeration_escapes"]

        assert counts["this_plan"] is None
        assert counts["session"] == {"runner_failure": 0, "manual": 0, "not_landed": 0}

    def test_an_unloadable_plan_path_does_not_break_status(self, store, tmp_path):
        """`state.plan_path` set to something that no longer parses is not a state a
        read-only status command may raise in — it is exactly the state someone runs
        `status` to understand."""
        state = SessionState(session_id="badplan", task_id="t",
                             plan_path=str(tmp_path / "gone.toml"),
                             weight_class=WeightClass.SUBSTANTIVE.value)
        plugins.activate(state, "premise")
        store.save(state)

        counts = cli.cmd_status(ns(session="badplan"), store=store).data["enumeration_escapes"]

        assert counts["this_plan"] is None
        assert counts["session"] == {"runner_failure": 0, "manual": 0, "not_landed": 0}

    def test_the_approve_refusal_payload_carries_the_counts(self, store, fixtures_dir):
        """The surface that matters most: the coordinator reading a refusal is the one
        person who can both see the number and is about to decide whether to add to
        it. Both readings are asserted — zero before any escape, one after — because a
        payload that only ever reported zero would look identical to a hard-coded one.
        """
        sid = "refusal-counts"
        plan = str(fixtures_dir / "plan_two_stage.toml")
        _to_plan_ready_with_premise(store, sid, plan)
        cli.cmd_question_enumerate(ns(session=sid, plan=None), store=store,
                                   runner=_failing_runner("advisor timed out after 480s"))

        first = cli.cmd_approve(ns(session=sid, by="user"), store=store)
        assert first.ok is False
        assert first.data["enumeration_escapes"]["this_plan"] == {
            "runner_failure": 0, "manual": 0, "not_landed": 0}

        assert cli.cmd_question_enumerate_escape(
            _escape_ns(sid, premise.ESCAPE_ADVISOR_TIMEOUT,
                       note="480s bound hit"), store=store).ok is True
        # a fresh OPEN question keeps approve refusing, so there is still a payload to
        # read — and shows the counts ride every refusal, not just the enumeration one
        cli.cmd_question_raise(ns(session=sid, id="Q9", target="goal",
                                  question="still open"), store=store)

        second = cli.cmd_approve(ns(session=sid, by="user"), store=store)

        assert second.ok is False
        assert not any(plugins_premise._ENUMERATE_RUNNER_FAILED in b
                       for b in second.data["blockers"])
        assert second.data["enumeration_escapes"]["this_plan"] == {
            "runner_failure": 1, "manual": 0, "not_landed": 0}

    def test_the_replan_refusal_payload_carries_them_against_the_proposed_plan(
            self, store, fixtures_dir):
        """cmd_replan is where `question-enumerate-escape --plan` exists to be used, so
        the person most likely to record an escape is the one reading THIS payload —
        and before the fix it was the one surface that did not carry the number.

        The two calls pin the axis, which is the part a re-implementation gets wrong:
        the counts are computed against the PROPOSED plan (`args.plan`), not
        state.plan_path. Same plan in, the escape on record for those bytes shows; a
        corrected plan in, `this_plan` is a real zero for the version the refusal
        speaks for while `session` still carries the row. Attach the counts against
        state.plan_path instead and the second assertion reads 1."""
        sid = "replan-refusal-counts"
        plan = str(fixtures_dir / "plan_two_stage.toml")
        corrected = str(fixtures_dir / "plan_two_stage_substantive.toml")
        _to_plan_ready_with_premise(store, sid, plan)
        cli.cmd_question_enumerate(ns(session=sid, plan=None), store=store,
                                   runner=_failing_runner("advisor timed out after 480s"))
        assert cli.cmd_question_enumerate_escape(
            _escape_ns(sid, premise.ESCAPE_ADVISOR_TIMEOUT,
                       note="480s bound hit"), store=store).ok is True
        # an open question keeps the plan_approval plugin gate refusing, so there is a
        # refusal payload to read at all once the enumeration axis is escaped
        cli.cmd_question_raise(ns(session=sid, id="Q9", target="goal",
                                  question="still open"), store=store)

        same = cli.cmd_replan(ns(session=sid, plan=plan), store=store)

        assert same.ok is False
        assert same.data["enumeration_escapes"]["this_plan"] == {
            "runner_failure": 1, "manual": 0, "not_landed": 0}

        other = cli.cmd_replan(ns(session=sid, plan=corrected), store=store)

        assert other.ok is False
        assert other.data["enumeration_escapes"]["this_plan"] == {
            "runner_failure": 0, "manual": 0, "not_landed": 0}
        assert other.data["enumeration_escapes"]["session"] == {
            "runner_failure": 1, "manual": 0, "not_landed": 0}


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
                               lambda argv, **_kw: RunResult(0, "", ""))

        assert len(adv) == 1, adv
        assert "the pass raised no questions" in adv[0]
        assert "BLOCKED" not in adv[0]

    def test_a_healthy_pass_with_pairs_attaches_no_advisory_at_all(
            self, store, fixtures_dir):
        adv = self._advisories(store, "arm-ok", fixtures_dir,
                               lambda argv, **_kw: RunResult(0, "stage 1\tdoes the bound hold?\n", ""))

        assert adv == []


# --- enumerate_rounds_exhausted admissibility ---

class TestEnumerateRoundsExhaustedAdmissibility:
    """The new reason is admissible only once the enumerate round budget is spent
    (gates.plan_enumerate_round_release_active returns True). Before that, the gate
    refuses it, naming the pass count so the operator knows how far they are."""

    def _stale_bag_state(self, plan_path, *, passes):
        state = SessionState(session_id="s", task_id="t", plan_path=plan_path,
                             weight_class=WeightClass.SUBSTANTIVE.value)
        plugins.activate(state, "premise")
        bag = state.plugins["premise"]
        bag["order_elements"] = [{
            "id": "O1", "element": "the order this plan answers",
            "disposition": "covered", "stage": 1, "reason": "",
        }]
        bag["enumerated"] = True
        bag["enumerated_at"] = "a-stale-digest-from-an-earlier-plan"
        bag["enumerate_pass"] = passes
        return state, bag

    def test_refused_when_release_inactive(self, store, fixtures_dir):
        """Below the threshold (passes=2 < 3) the reason is rejected with the
        current pass count so the operator knows what is needed."""
        plan_path = str(fixtures_dir / "plan_two_stage.toml")
        state, _ = self._stale_bag_state(plan_path, passes=2)
        store.save(state)

        d = cli.cmd_question_enumerate_escape(
            _escape_ns("s", premise.ESCAPE_ENUMERATE_ROUNDS_EXHAUSTED,
                       note="want to skip", plan=plan_path),
            store=store)

        assert not d.ok
        assert "admissible only once" in d.detail or "budget is exhausted" in d.detail
        assert "2/3" in d.detail

    def test_admitted_when_release_active(self, store, fixtures_dir):
        """At the threshold the reason is admitted and the escape is recorded."""
        plan_path = str(fixtures_dir / "plan_two_stage.toml")
        state, _ = self._stale_bag_state(plan_path, passes=3)
        store.save(state)

        d = cli.cmd_question_enumerate_escape(
            _escape_ns("s", premise.ESCAPE_ENUMERATE_ROUNDS_EXHAUSTED,
                       note="acceptable at this pass count", plan=plan_path),
            store=store)

        assert d.ok, d.detail
        saved = store.load("s")
        escapes = saved.plugins["premise"].get("escapes", [])
        assert any(e.get("reason") == premise.ESCAPE_ENUMERATE_ROUNDS_EXHAUSTED
                   for e in escapes)

    def test_in_closed_reason_set(self):
        """The new reason token is a member of ENUMERATION_ESCAPE_REASONS — the
        argparse choices= at the CLI surface picks it up automatically."""
        assert premise.ESCAPE_ENUMERATE_ROUNDS_EXHAUSTED in premise.ENUMERATION_ESCAPE_REASONS

    def test_not_in_runner_failure_reasons(self):
        """The reason speaks for a budget-exhaustion decision, not a failed run —
        offering it while the runner reports healthy must not be admitted via the
        runner-failure admissibility path."""
        assert (premise.ESCAPE_ENUMERATE_ROUNDS_EXHAUSTED
                not in premise.ENUMERATION_RUNNER_FAILURE_REASONS)
