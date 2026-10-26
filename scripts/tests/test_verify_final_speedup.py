"""verify-final's session green-check cache, dedupe, worker budget and run events.

R1  each distinct (venue tree identity, command, expected_exit) runs at most once per
    invocation; a check whose tree cannot be identified is never grouped or cached;
    greens land in SessionState.check_cache and serve a later run only for a
    final_check byte-identical to a stage verify_command or declaring cacheable.
R5  re-runs go concurrently under verify-final-parallel-max and a pytest `-n` worker
    budget; a lone check always starts; an injected runner is always serial.
R7  every admitted verify-final appends verify_final_check + verify_final_run events.
"""
from __future__ import annotations

import importlib
import threading
import time
from argparse import Namespace

import pytest

from agentctl import cli
from agentctl.dispatch import RunResult
from agentctl.plan import parse_plan
from agentctl.state import (
    Actor,
    Criterion,
    CriterionType,
    FinalCheck,
    GateRecord,
    Means,
    Node,
    Outcome,
    Partition,
    Route,
    SessionState,
    Stage,
    StageStatus,
    Subject,
    WeightClass,
)

class _LazyCheckCache:
    """agentctl.check_cache, imported on first use, so that against a tree without it
    each test fails on its own assertion instead of the module failing to collect."""

    def __getattr__(self, name):
        return getattr(importlib.import_module("agentctl.check_cache"), name)


check_cache = _LazyCheckCache()

HEAD = "deadbeef12345678"


def ns(**kw):
    return Namespace(**kw)


class _Runner:
    """Git identity probes plus bash checks. `exits` maps a command substring to its
    exit code; `delay` makes every bash call take that long; concurrency is recorded."""

    def __init__(self, head=HEAD, exits=None, delay=0.0, identifiable=True):
        self.head = head
        self.exits = exits or {}
        self.delay = delay
        self.identifiable = identifiable
        self.bash_calls: list[str] = []
        self.head_probes = 0
        self.running = 0
        self.peak = 0
        self._lock = threading.Lock()

    def __call__(self, argv, *, timeout=None, stdin=""):
        git_argv = argv[argv.index("git"):] if argv[:1] == ["env"] and "git" in argv else argv
        if git_argv[:1] == ["git"]:
            if "rev-parse" in git_argv:
                if "--git-path" in git_argv:
                    return RunResult(0, stdout=".git/index", stderr="")
                self.head_probes += 1
                if not self.identifiable:
                    return RunResult(128, stdout="", stderr="no repo")
                return RunResult(0, stdout=self.head, stderr="")
            if "write-tree" in git_argv:
                return RunResult(0, stdout=f"tree-of-{self.head}", stderr="")
            return RunResult(0, stdout="", stderr="")
        if argv[:2] == ["bash", "-c"]:
            with self._lock:
                self.bash_calls.append(argv[2])
                self.running += 1
                self.peak = max(self.peak, self.running)
            try:
                time.sleep(self.delay)
            finally:
                with self._lock:
                    self.running -= 1
            code = next((c for sub, c in self.exits.items() if sub in argv[2]), 0)
            return RunResult(code, stdout="", stderr="")
        raise AssertionError(f"unexpected call: {argv}")

    def ran(self, command: str) -> int:
        return sum(1 for call in self.bash_calls if call.endswith(command))


def _stage(index, command):
    return Stage(
        index=index, title=f"s{index}",
        subject=Subject(material="m", result="tests pass"),
        means=Means(means="pytest", method="run"),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(
            criterion_type=CriterionType.MEASURABLE.value, done_criterion="passes",
            verify_command=command, expected_exit=0,
        ),
        outcome=Outcome(status=StageStatus.PASSED.value),
    )


def _verifying(store, sid, stage_commands=("pytest -q",), final_checks=(), **extra):
    state = SessionState(
        session_id=sid, task_id="t",
        weight_class=WeightClass.SUBSTANTIVE.value, route=Route.SPAWN.value,
        node=Node.VERIFYING.value,
        approval=GateRecord("plan_approval", armed=True, passed=True),
        partition=Partition(m1=True, verdict="recommended"),
        stages=[_stage(i, c) for i, c in enumerate(stage_commands, 1)],
        final_check=list(final_checks), **extra,
    )
    store.save(state)
    return state


def _verify(store, sid, runner):
    return cli.cmd_verify_final(ns(session=sid), store=store, runner=runner)


def _again(store, sid):
    state = store.load(sid)
    state.node = Node.VERIFYING.value
    state.resolution = GateRecord("resolution", armed=False, passed=False)
    store.save(state)


def _events(store, sid, name):
    return [e for e in store.load(sid).history if e["event"] == name]


# --- R1: dedupe and the session cache --------------------------------------------

def test_r1_final_check_identical_to_stage_command_runs_once(store):
    _verifying(store, "r1-dedup", ["pytest -q"], [FinalCheck(command="pytest -q")])
    runner = _Runner()
    d = _verify(store, "r1-dedup", runner)
    assert d.ok is True, d.detail
    assert runner.ran("pytest -q") == 1
    decisions = [(e["check"], e["decision"], e["reason"]) for e in _events(store, "r1-dedup", "verify_final_check")]
    assert decisions == [
        ("stage:1", "miss", "no_cached_green"),
        ("final_check:1", "dedup", "same_as:stage:1"),
    ]


def test_r1_failing_check_fails_its_duplicate_too(store):
    _verifying(store, "r1-dup-fail", ["pytest -q"], [FinalCheck(command="pytest -q", label="again")])
    runner = _Runner(exits={"pytest -q": 3})
    d = _verify(store, "r1-dup-fail", runner)
    assert d.ok is False
    assert runner.ran("pytest -q") == 1
    assert d.data["failures"] == [
        "stage 1: exit 3 != 0 (pytest -q)",
        "final_check 'again': exit 3 != 0",
    ]


def test_r1_green_rerun_is_cached_and_serves_the_next_verify_final(store):
    _verifying(store, "r1-cache", ["pytest -q"])
    first = _Runner()
    assert _verify(store, "r1-cache", first).ok is True
    assert first.ran("pytest -q") == 1
    assert len(store.load("r1-cache").check_cache) == 1

    _again(store, "r1-cache")
    second = _Runner()
    assert _verify(store, "r1-cache", second).ok is True
    assert second.bash_calls == []
    last = _events(store, "r1-cache", "verify_final_check")[-1]
    assert (last["decision"], last["reason"]) == ("hit", "session_cache")


def test_r1_non_cacheable_final_check_reruns_despite_cached_greens(store):
    _verifying(store, "r1-noncache", ["pytest -q"], [FinalCheck(command="make smoke")])
    _verify(store, "r1-noncache", _Runner())
    _again(store, "r1-noncache")
    second = _Runner()
    assert _verify(store, "r1-noncache", second).ok is True
    assert second.ran("make smoke") == 1
    assert second.ran("pytest -q") == 0
    reasons = {e["check"]: (e["decision"], e["reason"]) for e in _events(store, "r1-noncache", "verify_final_check")[-2:]}
    assert reasons["final_check:1"] == ("miss", "not_cacheable")


def test_r1_cacheable_final_check_is_served_from_the_cache(store):
    _verifying(store, "r1-flag", ["pytest -q"], [FinalCheck(command="make smoke", cacheable=True)])
    _verify(store, "r1-flag", _Runner())
    _again(store, "r1-flag")
    second = _Runner()
    assert _verify(store, "r1-flag", second).ok is True
    assert second.bash_calls == []


def test_r1_moved_tree_misses_the_cache(store):
    _verifying(store, "r1-moved", ["pytest -q"])
    _verify(store, "r1-moved", _Runner(head=HEAD))
    _again(store, "r1-moved")
    moved = _Runner(head="cafebabe00000000")
    assert _verify(store, "r1-moved", moved).ok is True
    assert moved.ran("pytest -q") == 1


def test_r1_unidentifiable_tree_is_never_grouped_or_cached(store):
    _verifying(store, "r1-none", ["pytest -q"], [FinalCheck(command="pytest -q")])
    runner = _Runner(identifiable=False)
    assert _verify(store, "r1-none", runner).ok is True
    assert runner.ran("pytest -q") == 2
    assert store.load("r1-none").check_cache == {}
    reasons = {e["reason"] for e in _events(store, "r1-none", "verify_final_check")}
    assert reasons == {"identity_unavailable"}


def test_r1_red_result_is_not_cached(store):
    _verifying(store, "r1-red", ["pytest -q"])
    assert _verify(store, "r1-red", _Runner(exits={"pytest -q": 1})).ok is False
    assert store.load("r1-red").check_cache == {}


def test_r1_tree_identity_is_taken_once_per_venue_directory(store):
    _verifying(store, "r1-probe", ["pytest -q", "make lint"], [FinalCheck(command="make smoke")])
    runner = _Runner()
    assert _verify(store, "r1-probe", runner).ok is True
    assert runner.head_probes == 1


def test_r1_same_command_in_two_venues_runs_in_both(store, tmp_path):
    delivery, root = tmp_path / "delivery", tmp_path / "root"
    delivery.mkdir()
    root.mkdir()
    _verifying(
        store, "r1-venues", ["pytest -q"], [FinalCheck(command="pytest -q", venue="repo_root")],
        delivery_worktree=str(delivery), repo_root=str(root),
    )
    runner = _Runner()
    assert _verify(store, "r1-venues", runner).ok is True
    assert runner.ran("pytest -q") == 2
    assert [e["decision"] for e in _events(store, "r1-venues", "verify_final_check")] == ["miss", "miss"]
    assert len(store.load("r1-venues").check_cache) == 2

    _again(store, "r1-venues")
    second = _Runner()
    assert _verify(store, "r1-venues", second).ok is True
    assert second.bash_calls == []


def test_r1_check_key_depends_on_the_venue():
    assert check_cache.check_key("/a", "t", "pytest -q", 0) != check_cache.check_key("/b", "t", "pytest -q", 0)


def test_r1_non_cacheable_green_is_not_stored(store):
    _verifying(store, "r1-nostore", ["pytest -q"], [FinalCheck(command="make smoke")])
    _verify(store, "r1-nostore", _Runner())
    labels = {entry["label"] for entry in store.load("r1-nostore").check_cache.values()}
    assert labels == {"stage 1"}


def test_r1_final_check_cacheable_loads_from_a_plan():
    data = {
        "meta": {"task_id": "t", "goal": "g", "done_criterion": "d", "criterion_type": "measurable"},
        "stage": [{
            "index": 1, "title": "t", "executor": "in_thread",
            "expected_result_image": "img", "done_criterion": "d",
        }],
        "final_check": [{"command": "make smoke", "cacheable": True}, {"command": "make lint"}],
    }
    doc = parse_plan(data, strict=False)
    assert [fc.cacheable for fc in doc.meta.final_check] == [True, False]
    data["final_check"][0]["cacheable"] = "yes"
    with pytest.raises(Exception, match="cacheable must be a boolean"):
        parse_plan(data, strict=False)


def test_r1_cacheable_survives_the_state_round_trip(store):
    _verifying(store, "r1-roundtrip", ["pytest -q"], [FinalCheck(command="make smoke", cacheable=True)])
    assert store.load("r1-roundtrip").final_check[0].cacheable is True


def test_r1_older_final_check_without_cacheable_loads_as_false():
    assert FinalCheck.from_dict({"command": "x", "expected_exit": 0}).cacheable is False


# --- R5: worker budget and concurrency cap ----------------------------------------

def _real_runner(monkeypatch, runner, *, parallel, budget):
    """Route the engine's own (runner=None) subprocess path through `runner`."""
    monkeypatch.setattr(cli, "subprocess_runner", runner)
    # Literal env names, no check_cache attribute: a behaviour test must reach the
    # engine only through the CLI, so at a tree without check_cache it fails on its
    # own assertion. The budget override makes the machine's load irrelevant.
    monkeypatch.setenv("AGENTCTL_VERIFY_FINAL_PARALLEL", str(parallel))
    monkeypatch.setenv("AGENTCTL_VERIFY_FINAL_CPU_BUDGET", str(budget))


def _two_checks(store, sid, cmd_a="make a", cmd_b="make b"):
    _verifying(store, sid, [cmd_a], [FinalCheck(command=cmd_b)])


def test_r5_two_reruns_overlap_under_cap_two(store, monkeypatch):
    _two_checks(store, "r5-par")
    runner = _Runner(delay=0.2)
    _real_runner(monkeypatch, runner, parallel=2, budget=8)
    assert _verify(store, "r5-par", None).ok is True
    assert runner.peak == 2
    assert _events(store, "r5-par", "verify_final_run")[-1]["parallelism"] == 2


def test_r5_parallel_one_is_serial(store, monkeypatch):
    _two_checks(store, "r5-serial")
    runner = _Runner(delay=0.1)
    _real_runner(monkeypatch, runner, parallel=1, budget=8)
    assert _verify(store, "r5-serial", None).ok is True
    assert runner.peak == 1
    assert len(runner.bash_calls) == 2


def test_r5_injected_runner_is_always_serial(store, monkeypatch):
    _two_checks(store, "r5-inject")
    monkeypatch.setenv("AGENTCTL_VERIFY_FINAL_PARALLEL", "4")
    runner = _Runner(delay=0.1)
    assert _verify(store, "r5-inject", runner).ok is True
    assert runner.peak == 1


def test_r5_budget_below_two_weights_keeps_pytest_checks_apart(store, monkeypatch):
    _two_checks(store, "r5-budget", "pytest -q -n 4 a", "pytest -q -n 4 b")
    runner = _Runner(delay=0.1)
    _real_runner(monkeypatch, runner, parallel=2, budget=4)
    assert _verify(store, "r5-budget", None).ok is True
    assert runner.peak == 1


def test_r5_budget_covering_both_weights_overlaps_pytest_checks(store, monkeypatch):
    _two_checks(store, "r5-wide", "pytest -q -n 4 a", "pytest -q -n 4 b")
    runner = _Runner(delay=0.2)
    _real_runner(monkeypatch, runner, parallel=2, budget=8)
    assert _verify(store, "r5-wide", None).ok is True
    assert runner.peak == 2


def test_r5_lone_check_runs_when_budget_below_weight(store, monkeypatch):
    _two_checks(store, "r5-lone", "pytest -q -n 4 a", "pytest -q -n 4 b")
    runner = _Runner(delay=0.05)
    _real_runner(monkeypatch, runner, parallel=2, budget=0)
    d = _verify(store, "r5-lone", None)
    assert d.ok is True, d.detail
    assert len(runner.bash_calls) == 2
    assert runner.peak == 1


def test_r5_run_bounded_starts_a_job_heavier_than_the_budget_when_none_runs():
    done = []
    jobs = [check_cache.Job(5, (lambda i=i: done.append(i) or i)) for i in range(3)]
    results, peak = check_cache.run_bounded(jobs, cap=2, budget=1)
    assert results == [0, 1, 2]
    assert peak == 1


@pytest.mark.parametrize("cap", [1, 2])
def test_r5_run_bounded_turns_a_raising_job_into_its_error_result(cap):
    def boom():
        raise RuntimeError("kaput")

    jobs = [check_cache.Job(1, boom), check_cache.Job(1, lambda: "fine")]
    results, _ = check_cache.run_bounded(jobs, cap=cap, budget=4, on_error=lambda exc: f"err:{exc}")
    assert results == ["err:kaput", "fine"]
    with pytest.raises(RuntimeError):
        check_cache.run_bounded(jobs, cap=cap, budget=4)


@pytest.mark.parametrize("parallel", [1, 2])
def test_r5_raising_check_fails_the_run_and_still_logs_it(store, monkeypatch, parallel):
    class _Raising(_Runner):
        def __call__(self, argv, **kw):
            if argv[:2] == ["bash", "-c"] and argv[2].endswith("make a"):
                raise OSError("spawn failed")
            return super().__call__(argv, **kw)

    _two_checks(store, f"r5-raise-{parallel}")
    runner = _Raising()
    _real_runner(monkeypatch, runner, parallel=parallel, budget=8)
    d = _verify(store, f"r5-raise-{parallel}", None)
    assert d.ok is False
    assert d.data["failures"] == ["stage 1: raised OSError: spawn failed (make a)"]
    run = _events(store, f"r5-raise-{parallel}", "verify_final_run")
    assert [r["outcome"] for r in run] == ["failed"]
    first = _events(store, f"r5-raise-{parallel}", "verify_final_check")[0]
    assert first["exit"] is None
    assert "spawn failed" in first["reason"]
    assert store.load(f"r5-raise-{parallel}").check_cache == {}


def test_r5_failures_keep_check_order_when_completion_order_differs(store, monkeypatch):
    class _SlowFirst(_Runner):
        def __call__(self, argv, **kw):
            if argv[:2] == ["bash", "-c"] and argv[2].endswith("make a"):
                time.sleep(0.3)
            return super().__call__(argv, **kw)

    _two_checks(store, "r5-order")
    runner = _SlowFirst(exits={"make a": 1, "make b": 2})
    _real_runner(monkeypatch, runner, parallel=2, budget=8)
    d = _verify(store, "r5-order", None)
    assert d.data["failures"] == [
        "stage 1: exit 1 != 0 (make a)",
        "final_check 'make b': exit 2 != 0",
    ]
    assert [e["check"] for e in _events(store, "r5-order", "verify_final_check")] == ["stage:1", "final_check:1"]


@pytest.mark.parametrize("command, cpus, weight", [
    ("pytest -q -n 4 tests", 12, 4),
    ("pytest -q -n4 tests", 12, 4),
    ("pytest --numprocesses=3 tests", 12, 3),
    ("python3 -m pytest -n auto tests", 12, 12),
    ("python3 -m pytest -n logical tests", 6, 6),
    ("export LC_ALL=C && bash scripts/cap-run.sh -- python3 -m pytest -q -n 4 x && echo done", 12, 4),
    ("pytest -q tests", 12, 1),
    ("make smoke -n 8", 12, 1),
])
def test_r5_pytest_weight_is_its_n_value(command, cpus, weight):
    assert check_cache.pytest_weight(command, cpus) == weight


def test_r5_cap_and_budget_resolution():
    assert check_cache.resolve_cap(2, {}) == 2
    assert check_cache.resolve_cap(2, {check_cache.PARALLEL_ENV: "1"}) == 1
    assert check_cache.resolve_cap(2, {check_cache.PARALLEL_ENV: "junk"}) == 2
    assert check_cache.resolve_cap(0, {}) == 1
    assert check_cache.resolve_cap(2, {check_cache.PARALLEL_ENV: "--5"}) == 2
    assert check_cache.resolve_budget(12, 3.5, {check_cache.CPU_BUDGET_ENV: "²"}) == 8
    assert check_cache.resolve_budget(12, 3.5, {}) == 8
    assert check_cache.resolve_budget(12, 40.0, {}) == 0
    assert check_cache.resolve_budget(12, 3.5, {check_cache.CPU_BUDGET_ENV: "5"}) == 5


def test_r5_config_declares_two_as_the_default_cap():
    from agentctl.config import Thresholds, parse_config_md
    from pathlib import Path
    repo_config = Path(__file__).resolve().parents[2] / "config.md"
    assert Thresholds(parse_config_md(repo_config)).verify_final_parallel_max() == 2
    assert Thresholds({}).verify_final_parallel_max() == 1


# --- R7: verify_final_check / verify_final_run events -----------------------------

def test_r7_pass_appends_one_check_event_per_check_and_a_run_event(store):
    _verifying(store, "r7-pass", ["pytest -q"], [FinalCheck(command="make smoke")])
    assert _verify(store, "r7-pass", _Runner()).ok is True
    checks = _events(store, "r7-pass", "verify_final_check")
    assert [(c["check"], c["decision"], c["exit"]) for c in checks] == [
        ("stage:1", "miss", 0), ("final_check:1", "miss", 0),
    ]
    assert all(set(c) >= {"venue", "reason", "duration_s"} for c in checks)
    run = _events(store, "r7-pass", "verify_final_run")
    assert len(run) == 1
    assert run[0]["outcome"] == "passed"
    assert run[0]["counts"] == {"hit": 0, "dedup": 0, "miss": 2}
    assert run[0]["parallelism"] == 1
    assert run[0]["duration_s"] >= 0
    assert len(_events(store, "r7-pass", "verify_final")) == 1


def test_r7_failure_appends_events_with_the_failing_exit(store):
    _verifying(store, "r7-fail", ["pytest -q"])
    d = _verify(store, "r7-fail", _Runner(exits={"pytest -q": 4}))
    assert d.ok is False
    assert _events(store, "r7-fail", "verify_final_check")[0]["exit"] == 4
    assert _events(store, "r7-fail", "verify_final_run")[0]["outcome"] == "failed"
    assert _events(store, "r7-fail", "verify_final") == []


def test_r7_venue_refusal_appends_a_refused_run_event(store, tmp_path):
    _verifying(store, "r7-refuse", ["pytest -q"], delivery_worktree=str(tmp_path / "gone"))
    d = _verify(store, "r7-refuse", _Runner())
    assert d.action == "declare"
    run = _events(store, "r7-refuse", "verify_final_run")
    assert [r["outcome"] for r in run] == ["refused"]
    refused = _events(store, "r7-refuse", "verify_final_check")[-1]
    assert (refused["decision"], refused["reason"]) == ("miss", "refused")


def test_r7_cache_hit_and_dedup_are_reported_with_counts(store):
    _verifying(store, "r7-counts", ["pytest -q", "pytest -q"], [FinalCheck(command="pytest -q")])
    _verify(store, "r7-counts", _Runner())
    run = _events(store, "r7-counts", "verify_final_run")[-1]
    assert run["counts"] == {"hit": 0, "dedup": 2, "miss": 1}
    _again(store, "r7-counts")
    _verify(store, "r7-counts", _Runner())
    run = _events(store, "r7-counts", "verify_final_run")[-1]
    assert run["counts"] == {"hit": 1, "dedup": 2, "miss": 0}


def test_r7_hit_records_no_exit_and_dedup_keeps_the_copied_exit(store):
    _verifying(store, "r7-exit", ["pytest -q"], [FinalCheck(command="pytest -q")])
    _verify(store, "r7-exit", _Runner())
    assert [c["exit"] for c in _events(store, "r7-exit", "verify_final_check")] == [0, 0]
    _again(store, "r7-exit")
    _verify(store, "r7-exit", _Runner())
    hit, dedup = _events(store, "r7-exit", "verify_final_check")[-2:]
    assert (hit["decision"], hit["exit"]) == ("hit", None)
    assert (dedup["decision"], dedup["exit"]) == ("dedup", None)


def test_r7_dedup_of_a_failing_check_keeps_its_real_exit(store):
    _verifying(store, "r7-dupexit", ["pytest -q"], [FinalCheck(command="pytest -q")])
    _verify(store, "r7-dupexit", _Runner(exits={"pytest -q": 4}))
    assert [c["exit"] for c in _events(store, "r7-dupexit", "verify_final_check")] == [4, 4]


def test_r7_gate_blocked_call_writes_no_verify_final_events(store):
    state = _verifying(store, "r7-gated", ["pytest -q"])
    state.stages[0].outcome = Outcome(status=StageStatus.PENDING.value)
    store.save(state)
    d = _verify(store, "r7-gated", _Runner())
    assert d.ok is False
    assert _events(store, "r7-gated", "verify_final_check") == []
    assert _events(store, "r7-gated", "verify_final_run") == []
