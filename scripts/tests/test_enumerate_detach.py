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
  - a sidecar keyed to a stale digest is discarded by `read_and_discard`;
  - `cmd_submit_plan` and `cmd_replan`, driven end to end through ordinary CLI
    verbs against REAL premise bags, stamp `enumerate_deadline` at launch
    instant + `ENUMERATE_TIMEOUT_S`;
  - the two crux cases: a digest-CHANGING replan clears `enumerated`/
    `enumerated_at` back to not-run and launches exactly one new detached
    worker over the corrected plan (proven via the bag mutation and the
    recorded launch argv, not merely via the gate's blocker list); a
    digest-UNCHANGED (final_check-only) replan clears nothing, launches
    nothing, and touches no deadline.
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

        def fake_subprocess_runner(argv, *, timeout=None):
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
            lambda argv: calls.append(argv) or RunResult(0, "", ""),
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
            lambda argv: calls.append(argv) or RunResult(0, "", ""),
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
            lambda argv: calls.append(argv) or RunResult(0, "", ""),
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
        payload = enumerate_sidecar.read_and_discard("worker-default", digest, root=sidecar_root)
        assert payload is not None
        assert payload["content_digest"] == digest


# --- judge fallback stays on the plain (short-timeout) runner ------------------

class TestJudgeFallbackUnaffectedByEnumerateTimeout:
    def test_record_result_acceptance_judge_still_uses_plain_subprocess_runner(
            self, store, monkeypatch):
        """acceptance_judge calls its runner with ONLY argv -- no explicit timeout
        kwarg -- so the bound-in-effect timeout is whatever the runner's own
        default parameter resolves to. Patch underneath `subprocess_runner` (its
        real `subprocess.run` call) so the real default (`_ADVISOR_TIMEOUT_S`)
        is what gets exercised, and confirm `enumerate_subprocess_runner` -- the
        wider-timeout entry point -- is never touched on this path."""
        monkeypatch.setenv("AGENTCTL_STAGE_REVIEW", "1")
        _make_acceptance_session(store, "judge-1")

        calls = []

        def fake_run(argv, *, capture_output, text, timeout):
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
        assert calls == [advisor._ADVISOR_TIMEOUT_S]  # NOT ENUMERATE_TIMEOUT_S


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
        assert advisor.ENUMERATE_TIMEOUT_S == advisor._ENUMERATE_TIMEOUT_S_DEFAULT


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
        assert bag["enumerate_deadline"] is not None
        assert before + advisor.ENUMERATE_TIMEOUT_S - 2 <= bag["enumerate_deadline"]
        assert bag["enumerate_deadline"] <= after + advisor.ENUMERATE_TIMEOUT_S + 2

    def test_replan_restamps_enumerate_deadline_on_digest_change(
            self, store, fixtures_dir, monkeypatch):
        monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
        sid = "deadline-replan"
        base = str(fixtures_dir / "plan_two_stage.toml")
        corrected = str(fixtures_dir / "plan_two_stage_substantive.toml")

        _to_executing_stage1_with_premise(store, sid, base)

        before = time.time()
        cli.cmd_replan(ns(session=sid, plan=corrected), store=store)
        after = time.time()

        bag = store.load(sid).plugins["premise"]
        assert bag["enumerate_deadline"] is not None
        assert before + advisor.ENUMERATE_TIMEOUT_S - 2 <= bag["enumerate_deadline"]
        assert bag["enumerate_deadline"] <= after + advisor.ENUMERATE_TIMEOUT_S + 2


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

        # cmd_replan does not persist state on this refusing path (by design --
        # see docs/operations/detached-enumeration-design.md Q2: "a fold lost on
        # a refusing cmd_approve costs nothing... the next call re-folds", and
        # cmd_replan's own store.save() sites are all past this early return) --
        # so store.load here would still show the pre-replan bag. The relaunch
        # itself is the observable: exactly one new detached worker, over the
        # CORRECTED plan.
        assert len(launches) == 1
        assert corrected in launches[0]

    def test_digest_unchanged_replan_does_not_clear_or_relaunch(
            self, store, fixtures_dir, monkeypatch):
        """The negative case: a final_check-only refinement leaves
        _plan_content_digest byte-identical (final_check is excluded from the
        digest entirely), so nothing should be cleared, nothing relaunched, and
        the deadline must be untouched."""
        monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
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


# --- sidecar digest-mismatch discard --------------------------------------------

class TestSidecarDigestMismatchDiscard:
    def test_read_and_discard_ignores_a_sidecar_written_for_a_different_digest(
            self, tmp_path):
        root = tmp_path / "sidecars"
        enumerate_sidecar.write("sess", "digest-a", {"pairs": [], "content_digest": "digest-a"},
                                root=root)

        result = enumerate_sidecar.read_and_discard("sess", "digest-b", root=root)

        assert result is None
        # discard_all_for_session is unconditional -- the stale sidecar is gone too
        assert not enumerate_sidecar.sidecar_path("sess", "digest-a", root=root).exists()


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
        payload = enumerate_sidecar.read_and_discard(session_id, digest, root=sidecar_root)
        assert payload is not None
        assert payload["content_digest"] == digest
