"""Stage 4 of advisor-timeout-f3b: the whole-plan enumeration cross-check
(`enumerate_questions_health`/`enumerate_claims`) runs at a widened
`ENUMERATE_TIMEOUT_S` bound and is launched as a DETACHED background worker
(`proc_tree.launch_supervised`) rather than synchronously inside
`cmd_submit_plan`/`cmd_replan`/`cmd_approve` -- see
`docs/operations/detached-enumeration-design.md` for the full rationale.

Covers:
  - the two CLI fallbacks (`cmd_ledger_enumerate`, `cmd_question_enumerate`)
    resolve `runner=None` to `advisor.enumerate_subprocess_runner`, and
    `enumerate_subprocess_runner` itself delegates to `advisor.subprocess_runner`
    bound at `ENUMERATE_TIMEOUT_S` -- a genuinely different bound from the judge
    timeouts;
  - the judge fallback (`cmd_record_result`'s acceptance-review path) is
    UNCHANGED: it still resolves to `advisor.subprocess_runner` at its own
    `_ADVISOR_TIMEOUT_S`, never `enumerate_subprocess_runner`;
  - the shipped `_ENUMERATE_TIMEOUT_S_DEFAULT` (480) is exactly what the
    calibration-dataset formula recomputes from the COMMITTED
    `docs/operations/advisor-calibration.jsonl`, not asserted from prose;
  - `_launch_enumeration` (the launch site inside `cmd_submit_plan`/`cmd_replan`)
    returns in well under a second while the detached worker is still
    outstanding, and its sidecar lands even after the launching process exits;
  - `read_discarding_superseded` discards a sidecar keyed to a stale digest,
    leaves the MATCHING one in place (so the fold is idempotent) and never
    touches a concurrent worker's `.tmp-*.json`;
  - the fold end to end through `cmd_approve` and against `store.load()`, not
    an in-memory bag: a landed sidecar is folded, PERSISTED, and refuses the
    approve on its own `qenum-<part>-N` candidates -- which are then dispositionable,
    the whole point of persisting before the gate is evaluated -- plus its two
    dispositions-are-not-resurrected halves (a no-op at an already-enumerated
    digest; a statement-keyed re-raise when the plan moved on);
  - `cmd_submit_plan` and `cmd_replan`, driven end to end through ordinary CLI
    verbs against REAL premise bags, stamp `enumerate_deadline` at launch
    instant + `ENUMERATE_TIMEOUT_S`;
  - the two crux cases: a digest-CHANGING replan clears `enumerated`/
    `enumerated_at` back to not-run and launches exactly one new detached
    worker over the corrected plan (proven via the bag mutation and the
    recorded launch argv, not merely via the gate's blocker list); a
    digest-UNCHANGED (final_check-only) replan clears nothing, launches
    nothing, and touches no deadline;
  - and the refusal case that ordering implies: a replan whose corrected plan
    fails submission validation leaves the persisted premise bag byte-identical
    and spawns no worker -- a command that refuses must not mutate persisted
    state.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentctl import advisor, cli, enumerate_sidecar, plugins, plugins_premise
from agentctl.dispatch import RunResult
from agentctl.plan import diff_plans, load_plan
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

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
CALIBRATION_PATH = SCRIPTS_DIR.parent / "docs" / "operations" / "advisor-calibration.jsonl"

# Captured at MODULE IMPORT time (pytest collection), before any per-test
# monkeypatch runs -- conftest's `_no_real_enumeration_launch_by_default`
# autouse fixture stubs this attribute for the suite at large, so a test that
# means to exercise the real launch mechanics re-patches it back to this.
_REAL_SPAWN_ENUMERATION_WORKER = cli._spawn_enumeration_worker


def ns(**kw):
    return Namespace(**kw)


def _raise_if_called(*_a, **_kw):
    raise AssertionError("this runner must not be invoked")


class _PinnedClock:
    """`cli.time` with `time()` frozen and everything else delegated to the real
    module — patched onto `cli` alone rather than onto the shared stdlib module, so
    no other importer (or pytest's own timing) sees a frozen clock."""

    def __init__(self, at: float):
        self._at = at

    def __getattr__(self, name):
        return getattr(time, name)

    def time(self) -> float:
        return self._at


def _silent_advisor(argv, **kw):
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def _cover_the_order(store, sid, stage=1):
    """Mirrors test_replan.py's own local helper: satisfy the order-coverage
    half of the premise gate so it never blocks on the QUESTION channel these
    tests are actually about."""
    cli.cmd_order_raise(ns(session=sid, id="O1", element="the order this plan answers"),
                        store=store)
    cli.cmd_order_dispose(ns(session=sid, id="O1", as_="covered", stage=stage, reason=""),
                          store=store)


def _to_executing_stage1_with_premise(store, sid, plan):
    """Drive a fresh session to EXECUTING/stage-1, with the premise plugin
    genuinely armed and its enumeration cross-check already discharged, using
    ONLY ordinary CLI verbs -- the bag is never touched directly."""
    cli.cmd_start(ns(session=sid, task="demo-two-stage", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    assert "premise" in store.load(sid).plugins  # gate really is live
    cli.cmd_question_enumerate(ns(session=sid, plan=None), store=store,
                               runner=_silent_advisor)
    _cover_the_order(store, sid)
    assert cli.cmd_approve(ns(session=sid, by="user"), store=store).ok is True
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)


def _to_plan_ready_with_premise(store, sid, plan):
    """Same as `_to_executing_stage1_with_premise` up to PLAN_READY, but WITHOUT the
    synchronous `question-enumerate` — the enumeration is left to the detached
    worker's sidecar, which is the state the fold exists for."""
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


def _land_sidecar(store, sid, plan, pairs, *, runner_ok=True, root=None):
    """Write the sidecar a detached worker would have landed for `plan`'s current
    content digest. Returns that digest."""
    digest = plugins_premise._plan_content_digest(load_plan(plan))
    enumerate_sidecar.write(sid, digest, {
        "runner_ok": runner_ok,
        "pairs": [list(p) for p in pairs],
        "stderr": "",
        "content_digest": digest,
        "plan_path": str(plan),
    }, root=root)
    return digest


def _make_acceptance_session(store, sid):
    """Construct a session with an acceptance_review stage at EXECUTING
    directly -- mirrors test_advisor.py's own helper of the same name."""
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


# --- runner-timeout binding: enumerate_subprocess_runner vs subprocess_runner ---

class TestEnumerateRunnerTimeoutBinding:
    def test_enumerate_subprocess_runner_delegates_at_enumerate_timeout(self, monkeypatch):
        calls = []

        def fake_subprocess_runner(argv, *, timeout=None, stdin=""):
            calls.append((argv, timeout))
            return RunResult(0, "", "")

        monkeypatch.setattr(advisor, "subprocess_runner", fake_subprocess_runner)

        result = advisor.enumerate_subprocess_runner(["claude", "-p", "x"])

        assert result.returncode == 0
        assert len(calls) == 1
        argv, timeout = calls[0]
        assert argv == ["claude", "-p", "x"]
        assert timeout == advisor.ENUMERATE_TIMEOUT_S
        assert timeout != advisor._ADVISOR_TIMEOUT_S


# --- CLI fallbacks: cmd_ledger_enumerate / cmd_question_enumerate default -------

class TestCliDefaultsToEnumerateRunner:
    def test_ledger_enumerate_defaults_to_enumerate_subprocess_runner(
            self, store, tmp_path, monkeypatch):
        state = SessionState(session_id="ledger-default", task_id="t")
        plugins.activate(state, "ledger")
        store.save(state)

        calls = []
        monkeypatch.setattr(
            advisor, "enumerate_subprocess_runner",
            lambda argv, **_kw: calls.append(argv) or RunResult(0, "", ""),
        )
        monkeypatch.setattr(advisor, "subprocess_runner", _raise_if_called)

        artifact = tmp_path / "deliverable.md"
        artifact.write_text("chose approach A because latency spiked 3x.", encoding="utf-8")

        d = cli.cmd_ledger_enumerate(
            ns(session="ledger-default", artifact=str(artifact)), store=store, runner=None,
        )
        assert d.ok is True
        assert calls  # advisor.enumerate_subprocess_runner (module attr) was invoked

    def test_question_enumerate_defaults_to_enumerate_subprocess_runner(
            self, store, fixtures_dir, monkeypatch):
        plan_path = str(fixtures_dir / "plan_two_stage.toml")
        state = SessionState(session_id="question-default", task_id="t", plan_path=plan_path)
        plugins.activate(state, "premise")
        store.save(state)

        calls = []
        monkeypatch.setattr(
            advisor, "enumerate_subprocess_runner",
            lambda argv, **_kw: calls.append(argv) or RunResult(0, "", ""),
        )
        monkeypatch.setattr(advisor, "subprocess_runner", _raise_if_called)

        d = cli.cmd_question_enumerate(
            ns(session="question-default", plan=None), store=store, runner=None,
        )
        assert d.ok is True
        assert calls

    def test_worker_defaults_to_enumerate_subprocess_runner(
            self, store, fixtures_dir, tmp_path, monkeypatch):
        sidecar_root = tmp_path / "sidecars"
        monkeypatch.setattr(enumerate_sidecar, "DEFAULT_ROOT", sidecar_root)

        calls = []
        monkeypatch.setattr(
            advisor, "enumerate_subprocess_runner",
            lambda argv, **_kw: calls.append(argv) or RunResult(0, "", ""),
        )
        monkeypatch.setattr(advisor, "subprocess_runner", _raise_if_called)

        plan_path = fixtures_dir / "plan_two_stage.toml"
        doc = load_plan(str(plan_path))
        digest = plugins_premise._plan_content_digest(doc)

        d = cli.cmd_question_enumerate_worker(
            ns(session="worker-default", plan=str(plan_path), digest=digest), store=store,
        )
        assert d.ok is True
        assert calls
        payload = enumerate_sidecar.read_discarding_superseded(
            "worker-default", digest, root=sidecar_root)
        assert payload is not None
        assert payload["content_digest"] == digest

    def test_a_plan_edited_after_the_launch_writes_no_sidecar(
            self, store, fixtures_dir, tmp_path, monkeypatch):
        """The child can be in flight for up to ENUMERATE_TIMEOUT_S, so the plan it
        loads need not be the plan the launcher keyed it to. Keying the sidecar by
        the handed-down `--digest` made an enumeration of the NEW bytes fold as the
        awaited result for the OLD ones -- a gate discharged by a pass it never had.
        The runner assertion is load-bearing: the refusal must precede the advisor
        call, or the mismatch costs a full enumeration before being thrown away."""
        sidecar_root = tmp_path / "sidecars"
        monkeypatch.setattr(enumerate_sidecar, "DEFAULT_ROOT", sidecar_root)
        monkeypatch.setattr(advisor, "enumerate_subprocess_runner", _raise_if_called)

        plan_path = tmp_path / "plan.toml"
        plan_path.write_text(
            (fixtures_dir / "plan_two_stage.toml").read_text(encoding="utf-8"),
            encoding="utf-8")
        launched_digest = plugins_premise._plan_content_digest(load_plan(str(plan_path)))

        plan_path.write_text(
            plan_path.read_text(encoding="utf-8").replace(
                "Demonstrate the full two-stage coordination cycle",
                "Demonstrate something else entirely"),
            encoding="utf-8")

        d = cli.cmd_question_enumerate_worker(
            ns(session="worker-drift", plan=str(plan_path), digest=launched_digest),
            store=store,
        )
        assert d.ok is False
        assert "refusing to write a sidecar" in d.detail
        assert enumerate_sidecar.read_discarding_superseded(
            "worker-drift", launched_digest, root=sidecar_root) is None
        # and it did not silently re-key to the digest it computed either
        current = plugins_premise._plan_content_digest(load_plan(str(plan_path)))
        assert enumerate_sidecar.read_discarding_superseded(
            "worker-drift", current, root=sidecar_root) is None

    def test_a_hand_invoked_worker_cannot_mint_a_sidecar_for_a_chosen_digest(
            self, store, fixtures_dir, tmp_path, monkeypatch):
        """This verb sits in COMMANDS and the parser with a `--digest` the caller
        supplies, so it doubles as a way to write, by hand, the sidecar the next
        `approve` would fold. Recomputation is what closes that: the digest can no
        longer be chosen, only agreed with."""
        sidecar_root = tmp_path / "sidecars"
        monkeypatch.setattr(enumerate_sidecar, "DEFAULT_ROOT", sidecar_root)
        monkeypatch.setattr(advisor, "enumerate_subprocess_runner", _raise_if_called)

        chosen = "0" * 64
        d = cli.cmd_question_enumerate_worker(
            ns(session="worker-byhand",
               plan=str(fixtures_dir / "plan_two_stage.toml"), digest=chosen),
            store=store,
        )
        assert d.ok is False
        assert enumerate_sidecar.read_discarding_superseded(
            "worker-byhand", chosen, root=sidecar_root) is None


# --- judge fallback stays on the plain (short-timeout) runner ------------------

class TestJudgeFallbackUnaffectedByEnumerateTimeout:
    def test_record_result_acceptance_judge_still_uses_plain_subprocess_runner(
            self, store, monkeypatch):
        """acceptance_judge names its own ceiling at the call site, and it is a
        JUDGE ceiling: `_ACCEPTANCE_JUDGE_TIMEOUT_S`, computed by
        `lib/judge_latency.py::last_resort_ceiling_s` from measured haiku rows.
        Patch underneath `subprocess_runner` (its real `subprocess.run` call) so
        the number that actually reaches the subprocess is what gets exercised,
        and confirm `enumerate_subprocess_runner` -- the wider-timeout entry
        point sized for a whole-plan payload -- is never touched on this path."""
        monkeypatch.setenv("AGENTCTL_STAGE_REVIEW", "1")
        _make_acceptance_session(store, "judge-1")

        calls = []

        def fake_run(argv, *, capture_output, text, timeout, input=None, **kwargs):
            calls.append(timeout)
            return SimpleNamespace(returncode=0, stdout="YES\nlooks concrete", stderr="")

        monkeypatch.setattr(advisor.subprocess, "run", fake_run)
        monkeypatch.setattr(advisor, "enumerate_subprocess_runner", _raise_if_called)

        d = cli.cmd_record_result(
            ns(session="judge-1", status="passed", actual="observed",
               control=None, observation="the button turned green on load"),
            store=store, runner=None,
        )
        assert d.ok is True
        assert calls == [advisor._ACCEPTANCE_JUDGE_TIMEOUT_S]
        # Without this the assertion above stops discriminating the two ceilings.
        assert advisor._ACCEPTANCE_JUDGE_TIMEOUT_S != advisor.ENUMERATE_TIMEOUT_S


# --- the shipped default vs the committed calibration dataset ------------------

class TestCalibrationConstant:
    def test_default_timeout_matches_calibration_dataset_formula(self):
        rows = [
            json.loads(line)
            for line in CALIBRATION_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert rows, "calibration dataset must be non-empty"

        by_size: dict[int, list[float]] = {}
        for row in rows:
            by_size.setdefault(row["input_chars"], []).append(row["elapsed_s"])

        max_spread = max(max(vals) / min(vals) for vals in by_size.values())
        largest_size = max(by_size)
        min_elapsed_at_largest = min(by_size[largest_size])

        raw = max_spread * min_elapsed_at_largest
        expected = math.ceil(raw / 60.0) * 60

        assert expected == 480
        assert expected == advisor._ENUMERATE_TIMEOUT_S_DEFAULT

    def test_an_unset_environment_resolves_to_the_shipped_default(self, monkeypatch):
        """Re-resolves rather than reading `advisor.ENUMERATE_TIMEOUT_S`: that
        module-level value is bound at import, so asserting on it directly turns
        anyone who exports the override knob into a red suite — a false report about
        their shell, not about the constant this class exists to pin."""
        monkeypatch.delenv(advisor._ENUMERATE_TIMEOUT_ENV, raising=False)
        assert advisor._positive_int_env(
            advisor._ENUMERATE_TIMEOUT_ENV,
            advisor._ENUMERATE_TIMEOUT_S_DEFAULT) == advisor._ENUMERATE_TIMEOUT_S_DEFAULT


class TestTimeoutOverrideParsing:
    """`_positive_int_env` runs at IMPORT time and `cli` imports `advisor` at module
    scope, so its failure mode is not a bad timeout -- it is every agentctl command
    dying on a traceback before it can say which variable was at fault."""

    def _read(self, monkeypatch, raw):
        if raw is None:
            monkeypatch.delenv(advisor._ENUMERATE_TIMEOUT_ENV, raising=False)
        else:
            monkeypatch.setenv(advisor._ENUMERATE_TIMEOUT_ENV, raw)
        return advisor._positive_int_env(advisor._ENUMERATE_TIMEOUT_ENV, 480)

    def test_a_positive_integer_is_honoured(self, monkeypatch):
        assert self._read(monkeypatch, "900") == 900

    @pytest.mark.parametrize("raw", ["8m", "", "480.0", "eight hundred"])
    def test_unparseable_values_fall_back_and_name_themselves(
            self, monkeypatch, capsys, raw):
        assert self._read(monkeypatch, raw) == 480
        err = capsys.readouterr().err
        assert advisor._ENUMERATE_TIMEOUT_ENV in err
        assert repr(raw) in err

    @pytest.mark.parametrize("raw", ["0", "-30"])
    def test_non_positive_values_are_rejected_rather_than_obeyed(
            self, monkeypatch, capsys, raw):
        """`0` parses fine and is the dangerous one: obeyed, it makes every
        enumeration time out instantly, converting the gate to permanent
        escape-taking -- fail-CLOSED in form, and in practice a fleet that always
        escapes. Nobody chooses that by typing a number, so it is refused."""
        assert self._read(monkeypatch, raw) == 480
        assert advisor._ENUMERATE_TIMEOUT_ENV in capsys.readouterr().err


# --- deadline stamping against REAL premise bags --------------------------------

class TestDeadlineStamping:
    def test_submit_plan_stamps_enumerate_deadline(self, store, fixtures_dir, monkeypatch):
        monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
        sid = "deadline-submit"
        plan = str(fixtures_dir / "plan_two_stage.toml")

        cli.cmd_start(ns(session=sid, task="demo-two-stage", goal="", done_criterion="",
                         criterion_type="measurable", recursion_depth=0), store=store)
        cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                            wall_clock_min=60, tracker_key=None, architectural=True,
                            external_effect=False, new_dependency=False,
                            public_api_change=False), store=store)
        cli.cmd_plan(ns(session=sid), store=store)

        before = time.time()
        cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
        after = time.time()

        bag = store.load(sid).plugins["premise"]
        budget = advisor.ENUMERATE_TIMEOUT_S + cli._ENUMERATE_LAUNCH_MARGIN_S
        assert bag["enumerate_deadline"] is not None
        assert before + budget - 2 <= bag["enumerate_deadline"]
        assert bag["enumerate_deadline"] <= after + budget + 2

    def test_replan_restamps_enumerate_deadline_on_digest_change(
            self, store, fixtures_dir, monkeypatch):
        """Asserts against the PERSISTED bag and against a PINNED clock, both
        deliberately. The prior version read, within a ±2 s slack, the deadline
        `cmd_submit_plan` had already stamped inside the helper: it passed with the
        replan-side stamp deleted outright (vacuous) and flaked on a loaded machine.
        Pinning cli's clock makes the expected value exact, and the pin differing
        from the submit-time instant is what makes a missing restamp fail."""
        monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
        sid = "deadline-replan"
        base = str(fixtures_dir / "plan_two_stage.toml")
        corrected = str(fixtures_dir / "plan_two_stage_substantive.toml")

        _to_executing_stage1_with_premise(store, sid, base)
        submit_deadline = store.load(sid).plugins["premise"]["enumerate_deadline"]

        pinned = time.time() + 3.0
        monkeypatch.setattr(cli, "time", _PinnedClock(pinned))
        cli.cmd_replan(ns(session=sid, plan=corrected), store=store)

        bag = store.load(sid).plugins["premise"]
        assert bag["enumerate_deadline"] == (
            pinned + advisor.ENUMERATE_TIMEOUT_S + cli._ENUMERATE_LAUNCH_MARGIN_S)
        assert bag["enumerate_deadline"] != submit_deadline
        # the not-run clear is persisted too, not merely mutated in memory: on disk
        # a still-True `enumerated` pinned to the superseded digest yields the
        # INESCAPABLE _ENUMERATE_STALE on the next load (plugins_premise.py's
        # if/elif), which is the routing the clear exists to prevent.
        assert bag["enumerated"] is False
        assert bag["enumerated_at"] == ""


# --- the launch leaves a trace either way ---------------------------------------

class TestLaunchIsRecorded:
    """The launch is the one step of the detached design nobody watches: it happens
    inside `submit-plan`, its child is detached, and its failure mode is silence. A
    swallowed spawn error used to be indistinguishable from a healthy launch whose
    child is still thinking -- both present as `enumerated=False` with a deadline
    ticking -- so the deadline had to expire before anyone learned there had never
    been a child. Both outcomes are logged so the two states are tellable apart."""

    def _submit(self, store, fixtures_dir, sid, spawn):
        cli.cmd_start(ns(session=sid, task="demo-two-stage", goal="", done_criterion="",
                         criterion_type="measurable", recursion_depth=0), store=store)
        cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                            wall_clock_min=60, tracker_key=None, architectural=True,
                            external_effect=False, new_dependency=False,
                            public_api_change=False), store=store)
        cli.cmd_plan(ns(session=sid), store=store)
        cli.cmd_submit_plan(
            ns(session=sid, plan=str(fixtures_dir / "plan_two_stage.toml")), store=store)
        return [row for row in store.load(sid).history
                if row.get("event") == "enumerate_launch"]

    def test_a_successful_launch_is_logged_with_its_counter(
            self, store, fixtures_dir, monkeypatch):
        monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
        monkeypatch.setattr(cli, "_spawn_enumeration_worker", lambda *a, **k: None)

        rows = self._submit(store, fixtures_dir, "launch-log-ok", None)
        assert len(rows) == 1
        assert rows[0]["ok"] is True
        assert rows[0]["launch"] == 1

    def test_a_failed_launch_is_logged_with_its_error_and_still_stamps_a_deadline(
            self, store, fixtures_dir, monkeypatch):
        monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)

        def _boom(*args, **kwargs):
            raise OSError("no such file or directory: python3")

        monkeypatch.setattr(cli, "_spawn_enumeration_worker", _boom)

        rows = self._submit(store, fixtures_dir, "launch-log-fail", None)
        assert len(rows) == 1
        assert rows[0]["ok"] is False
        assert "no such file or directory" in rows[0]["error"]
        # the failure is recorded, not raised: a spawn that cannot happen must not
        # take `submit-plan` down with it, and the deadline it already stamped is
        # what routes the session to the escape rather than to a hang.
        bag = store.load("launch-log-fail").plugins["premise"]
        assert bag["enumerate_deadline"] is not None
        assert bag["enumerate_launch"] == 1


# --- the two crux cases: digest-changing vs digest-unchanged replan ------------

class TestDetachedRelaunchOnReplan:
    def test_digest_changing_replan_clears_enumerated_and_relaunches(
            self, store, fixtures_dir, monkeypatch):
        """The clear must actually happen (bag state), and exactly one new
        detached worker must be launched over the CORRECTED plan -- not merely
        satisfiable if the relaunch were silently dropped, which is what
        inspecting only the gate's blocker list (as test_replan.py's #48(b)
        does) cannot rule out."""
        monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
        sid = "clear-notrun"
        base = str(fixtures_dir / "plan_two_stage.toml")
        corrected = str(fixtures_dir / "plan_two_stage_substantive.toml")

        launches = []
        monkeypatch.setattr(
            cli, "_spawn_enumeration_worker",
            lambda cmd, **kw: launches.append(cmd),
        )

        _to_executing_stage1_with_premise(store, sid, base)
        bag_before = store.load(sid).plugins["premise"]
        assert bag_before["enumerated"] is True
        assert bag_before["enumerated_at"] != ""
        launches.clear()  # discard the submit_plan-time launch; only replan's matters

        blocked = cli.cmd_replan(ns(session=sid, plan=corrected), store=store)
        assert blocked.ok is False
        # The differentiator: if the clear were silently dropped, the bag would
        # still show enumerated=True at the OLD (base) digest, and the gate would
        # report _ENUMERATE_STALE, not _ENUMERATE_NOT_RUN -- so this specific
        # blocker is proof the clear actually happened, not merely that *some*
        # blocker fired.
        assert any(plugins_premise._ENUMERATE_NOT_RUN in b
                  for b in blocked.data.get("blockers", []))
        assert not any(plugins_premise._ENUMERATE_STALE in b
                      for b in blocked.data.get("blockers", []))

        # the not-run clear IS persisted on this refusing path -- cli.py's
        # enumeration_bag_dirty branch saves before the pblock return -- so a
        # still-True `enumerated` pinned to the OLD digest never survives to
        # the next load, which is exactly what routes the outstanding-child
        # window onto the escapable _ENUMERATE_NOT_RUN above.
        assert store.load(sid).plugins["premise"]["enumerated"] is False
        assert len(launches) == 1
        assert corrected in launches[0]

    def test_digest_unchanged_replan_does_not_clear_or_relaunch(
            self, store, fixtures_dir, monkeypatch):
        """The negative case: a final_check-only refinement leaves
        _plan_content_digest byte-identical (final_check is excluded from the
        digest entirely), so nothing should be cleared, nothing relaunched, and
        the deadline must be untouched."""
        monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
        # This test predates the replan-authorization gate (stage 5 of the
        # plan-review-override-customer-id fix) and expects a bare refinement
        # replan on a SUBSTANTIVE session to apply without a user-facing diff
        # presentation; that gate's own scoping is covered directly in
        # test_replan_authorization.py, so it is switched off here.
        monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")
        sid = "no-clear"
        base = str(fixtures_dir / "plan_two_stage_finalcheck.toml")
        changed = str(fixtures_dir / "plan_two_stage_finalcheck_changed.toml")

        # sanity: the fixture pair really does classify as a refinement while
        # leaving the digest identical -- the whole premise of this test
        assert diff_plans(load_plan(base), load_plan(changed)) == "refinement"
        assert (plugins_premise._plan_content_digest(load_plan(base))
                == plugins_premise._plan_content_digest(load_plan(changed)))

        launches = []
        monkeypatch.setattr(
            cli, "_spawn_enumeration_worker",
            lambda cmd, **kw: launches.append(cmd),
        )

        _to_executing_stage1_with_premise(store, sid, base)
        bag_before = dict(store.load(sid).plugins["premise"])
        launches.clear()

        d = cli.cmd_replan(ns(session=sid, plan=changed), store=store)
        assert d.action == "continue"  # refinement resumes execution, no re-approval

        bag_after = store.load(sid).plugins["premise"]
        assert bag_after["enumerated"] is bag_before["enumerated"]
        assert bag_after["enumerated_at"] == bag_before["enumerated_at"]
        assert bag_after["enumerate_deadline"] == bag_before["enumerate_deadline"]
        assert launches == []

    def test_replan_persists_folded_sidecar_to_disk(
            self, store, fixtures_dir, tmp_path, monkeypatch):
        """The fold-persistence half of the replan path: a sidecar that lands for
        the CORRECTED plan's digest before a retried `replan` runs must be folded
        AND PERSISTED, not merely mutated on the in-memory `state` object --
        cmd_replan's own store.save() sites are all past the early return this
        refusing path takes. An unpersisted fold would name qenum-<part>-N candidates
        that exist nowhere on disk, and `question-candidate-dispose` could not
        address them -- the central case for detaching on the replan side: replan
        against a corrected plan, launch, _ENUMERATE_NOT_RUN, wait, retry replan
        once the pass has landed.

        Mutation-proof: replacing `enumeration_bag_dirty =
        _fold_enumeration_sidecar(state, proposed, args.plan)` with a bare
        `_fold_enumeration_sidecar(state, proposed, args.plan)` call (dropping the
        assignment) at cli.py's replan site leaves the rest of the suite green but
        turns this test red, because `store.save(state)` is then never reached on
        this path and `store.load(sid)` below still shows the pre-fold bag."""
        monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
        root = tmp_path / "sidecars"
        monkeypatch.setattr(enumerate_sidecar, "DEFAULT_ROOT", root)
        sid = "replan-fold-persist"
        base = str(fixtures_dir / "plan_two_stage.toml")
        corrected = str(fixtures_dir / "plan_two_stage_substantive.toml")

        launches = []
        monkeypatch.setattr(
            cli, "_spawn_enumeration_worker",
            lambda cmd, **kw: launches.append(cmd),
        )

        _to_executing_stage1_with_premise(store, sid, base)
        # simulate an earlier digest-changing replan attempt that already cleared
        # the bag back to not-run and launched a worker over `corrected`
        state = store.load(sid)
        state.plugins["premise"]["enumerated"] = False
        state.plugins["premise"]["enumerated_at"] = ""
        store.save(state)
        digest = _land_sidecar(
            store, sid, corrected,
            [("goal", "which failure mode is out of scope?"),
             ("stage 1", "what makes the check go red?")],
        )
        launches.clear()

        blocked = cli.cmd_replan(ns(session=sid, plan=corrected), store=store)

        assert blocked.ok is False
        assert any("qenum-meta-1" in b for b in blocked.data.get("blockers", []))
        # a matching sidecar folds in place of a redundant relaunch
        assert launches == []

        bag = store.load(sid).plugins["premise"]
        assert bag["enumerated"] is True
        assert bag["enumerated_at"] == digest
        assert [c["id"] for c in bag["candidates"]] == ["qenum-meta-1", "qenum-meta-2"]
        assert all(c["disposition"] == "raised" for c in bag["candidates"])

    def test_a_replan_refused_at_submission_leaves_the_premise_bag_untouched(
            self, store, fixtures_dir, tmp_path, monkeypatch):
        """A refusal must not cost the session its enumeration record.

        `_launch_enumeration` is destructive and its caller PERSISTS it: it clears
        `enumerated`/`enumerated_at` back to not-run, bumps `enumerate_launch`, pins
        `enumerate_launch_digest` to the PROPOSED bytes and stamps a new deadline.
        With that block ahead of submission seam (b), a replan carrying a plan that
        fails submission validation was refused *after* the bag had already been
        destroyed and saved -- so the plan still current, and never at fault, was
        left blocked on the enumeration axis with a launch digest naming bytes the
        session had just rejected, plus a detached worker running over them.

        Asserted as a PROPERTY, not as line order: every enumeration field of the
        RELOADED bag is byte-identical across the refused call, and no worker was
        spawned. Mutation-proof (run): moving `cmd_replan`'s `submission =
        _submission_problems(...)` refusal back below the `_saved_plan_path =
        state.plan_path` block turns this red on `enumerated` (False on disk where
        the pre-call bag had True), while the rest of the suite stays green."""
        monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
        sid = "refused-submission-keeps-bag"
        base = str(fixtures_dir / "plan_two_stage.toml")
        # a plan that STRICT-LOADS (it never claims the substantive grade, so
        # `plan._validate_substantive_stage` never runs) yet fails submission for a
        # SUBSTANTIVE session, which refuses silence about the grade. Derived from the
        # shipped fixture so only the declaration under test differs.
        corrected = tmp_path / "corrected_no_weight_class.toml"
        corrected.write_text(
            Path(fixtures_dir / "plan_two_stage_substantive.toml")
            .read_text(encoding="utf-8")
            .replace('weight_class = "small_change"\n', ""),
            encoding="utf-8")
        corrected = str(corrected)
        assert load_plan(corrected).meta.weight_class is None
        # and the bytes really would have driven the enumeration block: a digest-
        # UNCHANGED replan clears and launches nothing anyway, which would make the
        # assertions below vacuous.
        assert (plugins_premise._plan_content_digest(load_plan(corrected))
                != plugins_premise._plan_content_digest(load_plan(base)))

        launches = []
        monkeypatch.setattr(
            cli, "_spawn_enumeration_worker",
            lambda cmd, **kw: launches.append(cmd),
        )

        _to_executing_stage1_with_premise(store, sid, base)
        bag_before = dict(store.load(sid).plugins["premise"])
        launches.clear()  # discard the submit_plan-time launch

        refused = cli.cmd_replan(ns(session=sid, plan=corrected), store=store)

        assert refused.ok is False
        # the property, asserted BEFORE the refusal's shape: under the defect the call
        # still refuses (on `close_questions`, for the enumeration it had just cleared),
        # so a shape assertion placed first would hide which claim the ordering carries.
        bag_after = store.load(sid).plugins["premise"]
        for field in ("enumerated", "enumerated_at", "enumerate_launch",
                      "enumerate_launch_digest", "enumerate_deadline"):
            assert bag_after[field] == bag_before[field], field
        assert launches == []
        # and it is seam (b) the command refuses at, not something downstream
        assert refused.action == "fix_plan"
        assert any("weight_class is not declared" in p
                   for p in refused.data.get("problems", []))


# --- the fold itself, end to end through cmd_approve ---------------------------

class TestFoldThroughApprove:
    """`_fold_enumeration_sidecar` is where a landed background result becomes the
    bag the gate reads, and every assertion here is made against `store.load(sid)`
    rather than the in-memory state: the three defects this class covers (a
    destructive read, an unpersisted fold, a disposition reset) are all invisible to
    a test that inspects the object the command mutated."""

    def _sidecar_root(self, tmp_path, monkeypatch):
        root = tmp_path / "sidecars"
        monkeypatch.setattr(enumerate_sidecar, "DEFAULT_ROOT", root)
        return root

    def test_approve_folds_persists_and_can_then_be_unblocked(
            self, store, fixtures_dir, tmp_path, monkeypatch):
        """The ordinary happy path of detaching: the worker lands pairs, `approve`
        folds them, refuses naming them, and the coordinator disposes them and
        approves. Every step of that is on disk — the refusing `approve` returns
        before its own store.save(), so an unpersisted fold would name `qenum-<part>-N`
        ids `question-candidate-dispose` could not find, and a destructive read
        would leave no sidecar to re-fold and no launch site on the approve path:
        `_ENUMERATE_NOT_RUN` forever, escapable only by the 480 s synchronous
        `question-enumerate` this whole change exists to avoid."""
        monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
        root = self._sidecar_root(tmp_path, monkeypatch)
        sid = "fold-approve"
        plan = str(fixtures_dir / "plan_two_stage.toml")

        _to_plan_ready_with_premise(store, sid, plan)
        digest = _land_sidecar(store, sid, plan,
                               [("goal", "which failure mode is out of scope?"),
                                ("stage 1", "what makes the check go red?")])

        blocked = cli.cmd_approve(ns(session=sid, by="user"), store=store)
        assert blocked.ok is False
        assert any("qenum-meta-1" in b for b in blocked.data["blockers"])

        bag = store.load(sid).plugins["premise"]
        assert bag["enumerated"] is True
        assert bag["enumerated_at"] == digest
        assert bag["enumerated_runner_ok"] is True
        assert [c["id"] for c in bag["candidates"]] == ["qenum-meta-1", "qenum-meta-2"]
        assert all(c["disposition"] == "raised" for c in bag["candidates"])
        # idempotent: the matching sidecar survives the refusing fold
        assert enumerate_sidecar.sidecar_path(sid, digest, root=root).exists()

        for cid in ("qenum-meta-1", "qenum-meta-2"):
            d = cli.cmd_question_candidate_dispose(
                ns(session=sid, id=cid, as_="dismissed", reason="answered in the goal",
                   question=None), store=store)
            assert d.ok is True, d.detail

        assert cli.cmd_approve(ns(session=sid, by="user"), store=store).ok is True

    def test_fold_preserves_dispositions_already_recorded_at_the_same_digest(
            self, store, fixtures_dir, tmp_path, monkeypatch):
        """The flow the coordinator actually uses: `submit-plan` (which launches a
        worker) → synchronous `question-enumerate` → dispose every candidate →
        `approve`. The worker's sidecar carries the SAME digest, so an unguarded
        fold re-raised every candidate the user had just dispositioned and refused
        the approve — one spurious refusal plus a full re-disposition per cycle."""
        monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
        self._sidecar_root(tmp_path, monkeypatch)
        sid = "fold-preserve"
        plan = str(fixtures_dir / "plan_two_stage.toml")
        pairs = [("goal", "which failure mode is out of scope?")]

        _to_plan_ready_with_premise(store, sid, plan)
        cli.cmd_question_enumerate(
            ns(session=sid, plan=None), store=store,
            runner=lambda argv, **_kw: RunResult(0, "\n".join(f"{t}\t{q}" for t, q in pairs), ""),
        )
        assert cli.cmd_question_candidate_dispose(
            ns(session=sid, id="qenum-meta-1", as_="dismissed", reason="answered in the goal",
               question=None), store=store).ok is True

        _land_sidecar(store, sid, plan, pairs)

        d = cli.cmd_approve(ns(session=sid, by="user"), store=store)

        assert d.ok is True, d.data.get("blockers")
        bag = store.load(sid).plugins["premise"]
        assert [c["disposition"] for c in bag["candidates"]] == ["dismissed"]
        assert bag["candidates"][0]["reason"] == "answered in the goal"

    def test_fold_is_a_noop_when_the_same_digest_is_already_enumerated(
            self, store, fixtures_dir, tmp_path, monkeypatch):
        """Same flow as above, but the worker's independent pass over the same plan
        asks DIFFERENT questions than the synchronous one the coordinator ran — the
        usual case, since two model calls rarely phrase a question identically. The
        result for this digest is already on record, so the sidecar is not read at
        all; folding it would raise fresh candidates and refuse an approve whose
        cross-check has demonstrably run."""
        monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
        root = self._sidecar_root(tmp_path, monkeypatch)
        sid = "fold-noop"
        plan = str(fixtures_dir / "plan_two_stage.toml")

        _to_plan_ready_with_premise(store, sid, plan)
        cli.cmd_question_enumerate(
            ns(session=sid, plan=None), store=store,
            runner=lambda argv, **_kw: RunResult(0, "goal\tthe question the coordinator saw", ""),
        )
        assert cli.cmd_question_candidate_dispose(
            ns(session=sid, id="qenum-meta-1", as_="dismissed", reason="answered", question=None),
            store=store).ok is True
        digest = _land_sidecar(store, sid, plan,
                               [("stage 2", "a question the worker asked instead")])

        d = cli.cmd_approve(ns(session=sid, by="user"), store=store)

        assert d.ok is True, d.data.get("blockers")
        bag = store.load(sid).plugins["premise"]
        assert len(bag["candidates"]) == 1
        assert bag["candidates"][0]["disposition"] == "dismissed"
        assert "the question the coordinator saw" in bag["candidates"][0]["statement"]
        # not read, so not discarded either — session-end cleanup collects it
        assert enumerate_sidecar.sidecar_path(sid, digest, root=root).exists()

    def test_fold_re_raises_a_candidate_whose_statement_changed(
            self, store, fixtures_dir, tmp_path, monkeypatch):
        """Preservation is keyed on the statement, not the id: `qenum-meta-1` of a pass
        over corrected plan content is a DIFFERENT question, and inheriting the old
        disposition would discharge a question nobody read. Here the bag's prior
        enumeration is stale (a digest-changing replan cleared it), so the fold runs
        and must re-raise."""
        monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
        self._sidecar_root(tmp_path, monkeypatch)
        sid = "fold-restatement"
        plan = str(fixtures_dir / "plan_two_stage.toml")

        _to_plan_ready_with_premise(store, sid, plan)
        cli.cmd_question_enumerate(
            ns(session=sid, plan=None), store=store,
            runner=lambda argv, **_kw: RunResult(0, "goal\tthe OLD question", ""),
        )
        assert cli.cmd_question_candidate_dispose(
            ns(session=sid, id="qenum-meta-1", as_="dismissed", reason="answered", question=None),
            store=store).ok is True
        # simulate the not-run clear a digest-changing relaunch leaves behind, so the
        # fold is not short-circuited by the same-digest no-op
        state = store.load(sid)
        state.plugins["premise"]["enumerated"] = False
        state.plugins["premise"]["enumerated_at"] = ""
        store.save(state)

        _land_sidecar(store, sid, plan, [("goal", "a DIFFERENT question")])

        blocked = cli.cmd_approve(ns(session=sid, by="user"), store=store)

        assert blocked.ok is False
        assert any("qenum-meta-1" in b for b in blocked.data["blockers"])
        cand = store.load(sid).plugins["premise"]["candidates"][0]
        assert cand["disposition"] == "raised"
        assert "a DIFFERENT question" in cand["statement"]


# --- sidecar digest-mismatch discard --------------------------------------------

class TestSidecarDigestMismatchDiscard:
    def test_read_ignores_and_discards_a_sidecar_written_for_a_different_digest(
            self, tmp_path):
        root = tmp_path / "sidecars"
        enumerate_sidecar.write("sess", "digest-a", {"pairs": [], "content_digest": "digest-a"},
                                root=root)

        result = enumerate_sidecar.read_discarding_superseded("sess", "digest-b", root=root)

        assert result is None
        # a result computed against superseded plan content is dead weight
        assert not enumerate_sidecar.sidecar_path("sess", "digest-a", root=root).exists()

    def test_read_is_idempotent_for_the_matching_digest(self, tmp_path):
        """The MATCHING sidecar survives its own read. cmd_approve folds it and then
        refuses on the very candidates the fold raised, without persisting anything;
        an unlink-on-read left that session with no sidecar to re-fold and no launch
        site on the approve path, i.e. _ENUMERATE_NOT_RUN forever."""
        root = tmp_path / "sidecars"
        enumerate_sidecar.write("sess", "digest-a",
                                {"pairs": [["stage 1", "why?"]], "content_digest": "digest-a"},
                                root=root)

        first = enumerate_sidecar.read_discarding_superseded("sess", "digest-a", root=root)
        second = enumerate_sidecar.read_discarding_superseded("sess", "digest-a", root=root)

        assert first == second
        assert first["pairs"] == [["stage 1", "why?"]]
        assert enumerate_sidecar.sidecar_path("sess", "digest-a", root=root).exists()

    def test_read_leaves_a_concurrent_workers_tempfile_alone(self, tmp_path):
        """A worker's `.tmp-*.json` is mid-write: unlinking it makes its os.replace
        raise, losing a payload that was about to land."""
        root = tmp_path / "sidecars"
        enumerate_sidecar.write("sess", "digest-a", {"pairs": [], "content_digest": "digest-a"},
                                root=root)
        session_dir = enumerate_sidecar.sidecar_path("sess", "digest-a", root=root).parent
        tmp_file = session_dir / ".tmp-x.json"
        tmp_file.write_text("{}", encoding="utf-8")

        enumerate_sidecar.read_discarding_superseded("sess", "digest-b", root=root)

        assert tmp_file.exists()

    def test_read_discards_a_matching_sidecar_that_fails_to_parse(self, tmp_path):
        """A JSONDecodeError is permanent for a given byte sequence -- keeping the
        corrupt matching sidecar around buys nothing, since re-reading it next
        time fails the same way. Discard it rather than retaining it forever."""
        root = tmp_path / "sidecars"
        match = enumerate_sidecar.sidecar_path("sess", "digest-a", root=root)
        match.parent.mkdir(parents=True, exist_ok=True)
        match.write_text("not json", encoding="utf-8")

        result = enumerate_sidecar.read_discarding_superseded("sess", "digest-a", root=root)

        assert result is None
        assert not match.exists()

    def test_read_discards_a_matching_sidecar_of_non_utf8_bytes(self, tmp_path):
        """The third failure cause, and the one neither `except` names by accident:
        `read_text(encoding='utf-8')` on corrupt bytes raises UnicodeDecodeError, a
        ValueError SIBLING of JSONDecodeError rather than a subclass. Catch only the
        latter and this propagates out of read_discarding_superseded, through
        _fold_enumeration_sidecar, into cmd_approve / cmd_replan -- a traceback where
        the by-cause split promised a discard. Byte-corruption is squarely the
        permanent class that split decided to throw away."""
        root = tmp_path / "sidecars"
        match = enumerate_sidecar.sidecar_path("sess", "digest-a", root=root)
        match.parent.mkdir(parents=True, exist_ok=True)
        match.write_bytes(b'{"pairs": "\xff\xfe not utf-8"}')

        result = enumerate_sidecar.read_discarding_superseded("sess", "digest-a", root=root)

        assert result is None
        assert not match.exists()

    def test_read_retains_a_matching_sidecar_on_transient_os_error(self, tmp_path, monkeypatch):
        """An OSError reading the matching sidecar may not recur -- unlike a
        decode failure, discarding it here could lose a payload that is still
        perfectly readable moments later."""
        root = tmp_path / "sidecars"
        enumerate_sidecar.write("sess", "digest-a", {"pairs": [], "content_digest": "digest-a"},
                                root=root)
        match = enumerate_sidecar.sidecar_path("sess", "digest-a", root=root)

        real_read_text = Path.read_text

        def _flaky_read_text(self, *a, **kw):
            if self == match:
                raise OSError("transient failure")
            return real_read_text(self, *a, **kw)

        monkeypatch.setattr(Path, "read_text", _flaky_read_text)

        result = enumerate_sidecar.read_discarding_superseded("sess", "digest-a", root=root)

        assert result is None
        assert match.exists()

    def test_discard_all_for_session_sweeps_an_orphaned_tempfile(self, tmp_path):
        """A worker killed between mkstemp and os.replace leaves a `.tmp-*.json`
        orphan behind. The read path must leave it alone for a concurrent worker's
        sake; session-end cleanup sweeps it -- not because no worker can still be
        alive at resolve (one can: a hand-run `question-enumerate` that SUCCEEDS
        discharges the gate while the detached child for the same digest is still
        inside its bound),
        but because no CONSUMER is left for whatever it writes. So the orphan, and
        the session directory it pins open, should not survive `cmd_resolve`."""
        root = tmp_path / "sidecars"
        enumerate_sidecar.write("sess", "digest-a", {"pairs": [], "content_digest": "digest-a"},
                                root=root)
        session_dir = enumerate_sidecar.sidecar_path("sess", "digest-a", root=root).parent
        tmp_file = session_dir / ".tmp-x.json"
        tmp_file.write_text("{}", encoding="utf-8")

        enumerate_sidecar.discard_all_for_session("sess", root=root)

        assert not tmp_file.exists()
        assert not session_dir.exists()


# --- the launch call itself: fast return + survives the launcher's own exit ----

class TestLaunchTiming:
    def test_launch_returns_fast_while_work_outstanding(
            self, monkeypatch, fixtures_dir, tmp_path):
        monkeypatch.setattr(cli, "_spawn_enumeration_worker", _REAL_SPAWN_ENUMERATION_WORKER)

        fake_bin = tmp_path / "fakebin"
        fake_bin.mkdir()
        fake_claude = fake_bin / "claude"
        fake_claude.write_text("#!/bin/sh\nexit 1\n")
        fake_claude.chmod(0o755)
        monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")
        monkeypatch.setenv("CLAUDE_AGENT_HOME", str(tmp_path / "agent-home"))

        plan_path = str(fixtures_dir / "plan_two_stage.toml")
        doc = load_plan(plan_path)
        bag = {"enumerated": True, "enumerated_at": "stale-marker"}
        state = SessionState(session_id="timing-1", task_id="t")

        started = time.monotonic()
        cli._launch_enumeration(state, bag, doc, plan_path)
        elapsed = time.monotonic() - started

        assert elapsed < 1.0
        assert bag["enumerated"] is False
        assert bag["enumerated_at"] == ""
        assert bag["enumerate_deadline"] is not None


class TestLaunchSurvivesLauncherExit:
    def test_sidecar_lands_after_launcher_process_exits(self, tmp_path, fixtures_dir):
        fake_bin = tmp_path / "fakebin"
        fake_bin.mkdir()
        fake_claude = fake_bin / "claude"
        fake_claude.write_text("#!/bin/sh\nexit 1\n")
        fake_claude.chmod(0o755)

        agent_home = tmp_path / "agent-home"
        sidecar_root = agent_home / "agentctl" / "enumerate-sidecars"

        env = dict(os.environ)
        env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
        env["CLAUDE_AGENT_HOME"] = str(agent_home)
        env.pop("CLAUDE_CONFIG_DIR", None)

        plan_path = str(fixtures_dir / "plan_two_stage.toml")
        session_id = "survive-1"

        driver = tmp_path / "driver.py"
        driver.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(SCRIPTS_DIR)!r})\n"
            "from agentctl import cli\n"
            "from agentctl.plan import load_plan\n"
            "from agentctl.state import SessionState\n"
            f"doc = load_plan({plan_path!r})\n"
            "bag = {'enumerated': False, 'enumerated_at': ''}\n"
            f"state = SessionState(session_id={session_id!r}, task_id='t')\n"
            f"cli._launch_enumeration(state, bag, doc, {plan_path!r})\n",
            encoding="utf-8",
        )

        started = time.monotonic()
        result = subprocess.run(
            [sys.executable, str(driver)], env=env, timeout=15,
            capture_output=True, text=True,
        )
        launcher_elapsed = time.monotonic() - started
        assert result.returncode == 0, result.stderr
        assert launcher_elapsed < 5.0  # the driver itself never waits on the worker

        doc = load_plan(plan_path)
        digest = plugins_premise._plan_content_digest(doc)
        sidecar_file = enumerate_sidecar.sidecar_path(session_id, digest, root=sidecar_root)

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not sidecar_file.exists():
            time.sleep(0.1)

        assert sidecar_file.exists(), "detached worker never wrote its sidecar"
        payload = enumerate_sidecar.read_discarding_superseded(
            session_id, digest, root=sidecar_root)
        assert payload is not None
        assert payload["content_digest"] == digest


# --- the runner signature is pinned, not inferred from a stub ------------------

@pytest.mark.parametrize(
    "entry",
    ["enumerate_claims", "enumerate_questions_health"],
)
def test_enumerate_runner_signature_matches_what_the_entry_points_pass(entry, monkeypatch):
    """The two enumeration entry points call their DEFAULT runner, and the ceiling
    that reaches `subprocess_runner` is ENUMERATE_TIMEOUT_S.

    Every other test here supplies a stub runner, and a stub accepts any signature,
    so none of them can see a mismatch between what the call sites pass and what
    `enumerate_subprocess_runner` accepts. That mismatch is not hypothetical: it is
    exactly what the trunk merge produced, and both entry points wrap their call in
    a bare `except Exception`, so a surviving keyword would raise TypeError and be
    reported as an UNHEALTHY RUNNER — the F3b symptom, reinstated, with every
    stubbed test still green. Patching `advisor.subprocess_runner` rather than the
    runner keeps the real `enumerate_subprocess_runner` in the path while launching
    nothing."""
    seen: dict = {}

    def fake_subprocess_runner(argv, *, timeout=None, stdin=""):
        seen["argv"] = argv
        seen["timeout"] = timeout
        seen["stdin"] = stdin
        return RunResult(0, "", "")

    monkeypatch.setattr(advisor, "subprocess_runner", fake_subprocess_runner)

    if entry == "enumerate_claims":
        advisor.enumerate_claims("artifact text")
    else:
        advisor.enumerate_questions_health("goal", "done criterion", "plan text")

    assert seen, (
        f"{entry} never reached subprocess_runner — its call site and "
        "enumerate_subprocess_runner disagree, and the TypeError was swallowed "
        "into an unhealthy-runner verdict"
    )
    assert seen["timeout"] == advisor.ENUMERATE_TIMEOUT_S
    assert seen["timeout"] != advisor._ADVISOR_TIMEOUT_S
