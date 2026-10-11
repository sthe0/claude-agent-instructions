"""What survives of the typed enumeration escape after the standalone enumerator's
retirement for new plans (amendments-2.md E3).

The escape route (`question-enumerate-escape`, the runner-health blocker, the
not-landed window, the round release) is gone: a failed or absent enumeration
blocks nothing, because no enumeration runs. Covered here:

  - `advisor.classify_runner_failure` still reads back the exact stderr
    `advisor.subprocess_runner` writes on TimeoutExpired (produced by forcing the
    real exception) -- the judge paths share it;
  - the retired `question-enumerate-escape` exits 2 with the review-route message
    and leaves the persisted bag untouched, whatever reason it is handed;
  - a legacy bag with a failed pass and no escape no longer blocks `approve`, and
    one that carries an escape row still reports it;
  - the escape-count instrument (`plugins_premise.escape_counts`, the
    `enumeration_escapes` payload of `status` / the approve and replan refusals)
    still reads the rows a carried bag holds, on both axes, with the three
    families apart.

Every assertion about persisted bag state reads `store.load(sid)`, never the
in-memory bag a command mutated.
"""
from __future__ import annotations

import subprocess
from argparse import Namespace

import pytest

from agentctl import advisor, cli, plugins, plugins_premise, premise
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


def _digest(plan_path):
    return plugins_premise._plan_content_digest(load_plan(plan_path))


def _legacy_bag_state(plan_path, sid="s", **bag_kw):
    """A substantive session carrying a bag the way a pre-retirement session left
    it: an enumeration pass on record, order covered, no open question."""
    state = SessionState(session_id=sid, task_id="t", plan_path=plan_path,
                         weight_class=WeightClass.SUBSTANTIVE.value)
    plugins.activate(state, "premise")
    bag = state.plugins["premise"]
    bag["order_elements"] = [{
        "id": "O1", "element": "the order this plan answers",
        "disposition": "covered", "stage": 1, "reason": "",
    }]
    bag["enumerated"] = True
    bag["enumerated_at"] = _digest(plan_path)
    bag.update(bag_kw)
    return state, bag


def _escape_row(plan_path, reason):
    return {"reason": reason, "note": "legacy escape", "content_digest": _digest(plan_path),
            "enumerate_launch": 1, "enumerate_pass": 1}


# --- classify_runner_failure ----------------------------------------------------

class TestClassifyRunnerFailure:
    def test_reads_back_the_runners_own_timeout_stderr(self, monkeypatch):
        """Produced by forcing the REAL TimeoutExpired rather than restating the
        literal: if the emitted wording and the classifier ever drift apart, every
        timeout would be pre-selected as the catch-all `advisor_error`."""
        def _timed_out(*_a, **_kw):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=advisor.ENUMERATE_TIMEOUT_S)

        monkeypatch.setattr(subprocess, "run", _timed_out)
        result = advisor.subprocess_runner(["claude", "-p", "x"],
                                           timeout=advisor.ENUMERATE_TIMEOUT_S)

        assert result.returncode != 0
        assert advisor.classify_runner_failure(result.stderr) == premise.ESCAPE_ADVISOR_TIMEOUT

    def test_quota_refusal_gets_its_own_reason(self):
        assert advisor.classify_runner_failure(
            "You've hit your session limit · resets 12am (Europe/Moscow)"
        ) == premise.ESCAPE_ADVISOR_QUOTA

    def test_a_missing_credential_gets_its_own_reason_ahead_of_the_catch_all(self):
        assert advisor.classify_runner_failure(
            f"{advisor._CREDENTIAL_STDERR_PREFIX}\nInvalid API key"
        ) == premise.ESCAPE_ADVISOR_CREDENTIAL

    def test_a_missing_credential_wins_over_a_quota_mention(self):
        assert advisor.classify_runner_failure(
            f"{advisor._CREDENTIAL_STDERR_PREFIX}\nYou've hit your session limit"
        ) == premise.ESCAPE_ADVISOR_CREDENTIAL

    def test_quota_match_is_case_insensitive(self):
        assert advisor.classify_runner_failure(
            "Error: Session Limit reached") == premise.ESCAPE_ADVISOR_QUOTA

    def test_timeout_wins_over_a_quota_mention(self):
        assert advisor.classify_runner_failure(
            f"{advisor._TIMEOUT_STDERR_PREFIX} 480s (session limit?)"
        ) == premise.ESCAPE_ADVISOR_TIMEOUT

    def test_any_other_stderr_is_the_catch_all(self):
        assert advisor.classify_runner_failure(
            "claude: command not found") == premise.ESCAPE_ADVISOR_ERROR

    def test_absent_stderr_is_the_catch_all(self):
        assert advisor.classify_runner_failure("") == premise.ESCAPE_ADVISOR_ERROR
        assert advisor.classify_runner_failure(None) == premise.ESCAPE_ADVISOR_ERROR


# --- the escape command is retired --------------------------------------------

class TestEscapeCommandIsRetired:
    @pytest.mark.parametrize("reason", sorted(premise.ENUMERATION_ESCAPE_REASONS))
    def test_exits_2_and_leaves_the_persisted_bag_untouched(
            self, store, fixtures_dir, reason, capsys):
        plan_path = str(fixtures_dir / "plan_two_stage.toml")
        state, _ = _legacy_bag_state(plan_path, enumerated_runner_ok=False,
                                     enumerated_runner_stderr="boom")
        store.save(state)
        before = store.load("s").plugins["premise"]

        with pytest.raises(SystemExit) as exc:
            cli.cmd_question_enumerate_escape(_escape_ns("s", reason), store=store)

        assert exc.value.code == 2
        assert "question-candidate-dispose" in capsys.readouterr().err
        assert store.load("s").plugins["premise"] == before
        assert not before.get("escapes")


# --- legacy bags -------------------------------------------------------------

class TestLegacyBagsLoadAndStopBlocking:
    def test_a_failed_pass_without_an_escape_no_longer_blocks_the_blockers_list(
            self, fixtures_dir):
        plan_path = str(fixtures_dir / "plan_two_stage.toml")
        state, bag = _legacy_bag_state(plan_path, enumerated_runner_ok=False,
                                       enumerated_runner_stderr="advisor timed out after 480s")

        blockers = plugins_premise.premise_blockers(state, bag)

        assert blockers == []

    def test_an_unlanded_pass_with_a_past_deadline_no_longer_blocks(self, fixtures_dir):
        plan_path = str(fixtures_dir / "plan_two_stage.toml")
        state, bag = _legacy_bag_state(plan_path, enumerated=False, enumerated_at="",
                                       enumerate_deadline=1.0)

        assert plugins_premise.premise_blockers(state, bag) == []


# --- the escape counters, on both surfaces --------------------------------------

class TestEscapeCountsAreVisible:
    """An escape mechanism whose rate nobody can see is the fail-open it replaced,
    one level up -- so the counters outlive the escape command and still read the
    rows a carried bag holds. Two axes, deliberately not merged: `this_plan` is what
    means something AT THE GATE; `session` resets with `agentctl reset`. Within each
    axis the three families -- a failed runner, a hand re-reading, a pass that never
    landed -- are counted apart."""

    def test_advisor_unavailable_tallies_as_runner_failure(self):
        """`_tally` reads the infra/work-was-done split off
        `premise.ENUMERATION_INFRA_FAILURE_REASONS`; `advisor_unavailable` is the one
        member no other test here produces."""
        counts = plugins_premise._tally([{"reason": premise.ESCAPE_ADVISOR_UNAVAILABLE}])

        assert counts == {"runner_failure": 1, "manual": 0, "not_landed": 0}

    def _carrying_both_families(self, store, plan_path, sid="counts"):
        state, bag = _legacy_bag_state(plan_path, sid=sid)
        bag["escapes"] = [
            _escape_row(plan_path, premise.ESCAPE_ADVISOR_ERROR),
            _escape_row(plan_path, premise.ESCAPE_ENUMERATION_NOT_LANDED),
        ]
        store.save(state)
        return sid

    def test_status_reports_both_axes_with_the_two_families_apart(self, store, fixtures_dir):
        sid = self._carrying_both_families(store, str(fixtures_dir / "plan_two_stage.toml"))

        counts = cli.cmd_status(ns(session=sid), store=store).data["enumeration_escapes"]

        assert counts["this_plan"] == {"runner_failure": 1, "manual": 0, "not_landed": 1}
        assert counts["session"] == {"runner_failure": 1, "manual": 0, "not_landed": 1}

    def test_a_hand_re_reading_is_counted_apart_from_the_infrastructure_failures(
            self, store, fixtures_dir):
        plan_path = str(fixtures_dir / "plan_two_stage.toml")
        state, bag = _legacy_bag_state(plan_path, sid="manual-counts")
        bag["escapes"] = [_escape_row(plan_path, premise.ESCAPE_MANUAL_ENUMERATION_DONE)]
        store.save(state)

        counts = cli.cmd_status(ns(session="manual-counts"),
                                store=store).data["enumeration_escapes"]

        assert counts["this_plan"] == {"runner_failure": 0, "manual": 1, "not_landed": 0}
        assert counts["session"] == {"runner_failure": 0, "manual": 1, "not_landed": 0}

    def test_an_escape_against_superseded_plan_content_leaves_only_the_session_count(
            self, store, fixtures_dir, tmp_path):
        plan_path = tmp_path / "plan.toml"
        plan_path.write_text((fixtures_dir / "plan_two_stage.toml").read_text(encoding="utf-8"),
                             encoding="utf-8")
        sid = self._carrying_both_families(store, str(plan_path), sid="superseded")
        plan_path.write_text(
            (fixtures_dir / "plan_two_stage_substantive.toml").read_text(encoding="utf-8"),
            encoding="utf-8")

        counts = cli.cmd_status(ns(session=sid), store=store).data["enumeration_escapes"]

        assert counts["this_plan"] == {"runner_failure": 0, "manual": 0, "not_landed": 0}
        assert counts["session"] == {"runner_failure": 1, "manual": 0, "not_landed": 1}

    def test_no_premise_bag_reports_not_applicable_rather_than_zero(self, store):
        state = SessionState(session_id="nobag", task_id="t")
        store.save(state)

        assert cli.cmd_status(ns(session="nobag"), store=store).data[
            "enumeration_escapes"] is None

    def test_a_bag_with_no_plan_yet_reports_a_null_per_plan_axis_and_a_real_session_zero(
            self, store):
        state = SessionState(session_id="noplan", task_id="t",
                             weight_class=WeightClass.SUBSTANTIVE.value)
        plugins.activate(state, "premise")
        state.plugins["premise"].pop("escapes", None)
        store.save(state)

        counts = cli.cmd_status(ns(session="noplan"), store=store).data["enumeration_escapes"]

        assert counts["this_plan"] is None
        assert counts["session"] == {"runner_failure": 0, "manual": 0, "not_landed": 0}

    def test_an_unloadable_plan_path_does_not_break_status(self, store, tmp_path):
        state = SessionState(session_id="badplan", task_id="t",
                             plan_path=str(tmp_path / "gone.toml"),
                             weight_class=WeightClass.SUBSTANTIVE.value)
        plugins.activate(state, "premise")
        store.save(state)

        counts = cli.cmd_status(ns(session="badplan"), store=store).data["enumeration_escapes"]

        assert counts["this_plan"] is None
        assert counts["session"] == {"runner_failure": 0, "manual": 0, "not_landed": 0}

    def _refused_session(self, store, fixtures_dir, sid):
        """A plan-ready session whose bag carries one legacy runner-failure escape for
        the submitted plan's bytes, plus an OPEN question so approve still refuses —
        which is what gives the refusal payload something to carry."""
        plan = str(fixtures_dir / "plan_two_stage.toml")
        cli.cmd_start(ns(session=sid, task="demo-two-stage", goal="", done_criterion="",
                         criterion_type="measurable", recursion_depth=0), store=store)
        cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                            wall_clock_min=60, tracker_key=None, architectural=True,
                            external_effect=False, new_dependency=False,
                            public_api_change=False), store=store)
        cli.cmd_plan(ns(session=sid), store=store)
        cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
        cli.cmd_order_raise(ns(session=sid, id="O1", element="the order this plan answers"),
                            store=store)
        cli.cmd_order_dispose(ns(session=sid, id="O1", as_="covered", stage=1, reason=""),
                              store=store)
        cli.cmd_question_raise(ns(session=sid, id="Q9", target="goal",
                                  question="still open"), store=store)
        return plan

    def test_the_approve_refusal_payload_carries_the_counts(self, store, fixtures_dir):
        sid = "refusal-counts"
        plan = self._refused_session(store, fixtures_dir, sid)

        first = cli.cmd_approve(ns(session=sid, by="user"), store=store)
        assert first.ok is False
        assert first.data["enumeration_escapes"]["this_plan"] == {
            "runner_failure": 0, "manual": 0, "not_landed": 0}

        state = store.load(sid)
        state.plugins["premise"].setdefault("escapes", []).append(
            _escape_row(plan, premise.ESCAPE_ADVISOR_TIMEOUT))
        store.save(state)

        second = cli.cmd_approve(ns(session=sid, by="user"), store=store)

        assert second.ok is False
        assert second.data["enumeration_escapes"]["this_plan"] == {
            "runner_failure": 1, "manual": 0, "not_landed": 0}

    def test_the_replan_refusal_payload_carries_them_against_the_proposed_plan(
            self, store, fixtures_dir):
        """The counts are computed against the PROPOSED plan (`args.plan`), not
        state.plan_path: same plan in, the escape on record for those bytes shows; a
        corrected plan in, `this_plan` is a real zero while `session` keeps the row."""
        sid = "replan-refusal-counts"
        plan = self._refused_session(store, fixtures_dir, sid)
        corrected = str(fixtures_dir / "plan_two_stage_substantive.toml")
        state = store.load(sid)
        state.plugins["premise"].setdefault("escapes", []).append(
            _escape_row(plan, premise.ESCAPE_ADVISOR_TIMEOUT))
        store.save(state)

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


# --- the closed reason set (constants kept for legacy rows) --------------------

class TestReasonSetKeptForLegacyRows:
    def test_rounds_exhausted_is_in_the_closed_reason_set(self):
        assert premise.ESCAPE_ENUMERATE_ROUNDS_EXHAUSTED in premise.ENUMERATION_ESCAPE_REASONS

    def test_rounds_exhausted_is_not_a_runner_failure_reason(self):
        assert (premise.ESCAPE_ENUMERATE_ROUNDS_EXHAUSTED
                not in premise.ENUMERATION_RUNNER_FAILURE_REASONS)
