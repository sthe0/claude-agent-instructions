"""The suite gate's additive verdict: unfinished ids, two reruns, and the join of the two partial sets.

Difficulty removed: a test that fails once and passes again held a landing (or, with one rerun,
a single lucky pass cleared a real failure). These tests pin the rule on stubbed runs that leave a
per-test report, the way pytest does; the real pytest is exercised by
``gate_speedup_control.py verdict-scenario``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib import flake_list
from lib import landing_gate as shared
from lib import suite_gate as gate
from test_suite_gate import build_world, ok_run

T1, T2, T3 = "scripts/tests/test_y.py::test_1", "scripts/tests/test_y.py::test_2", "scripts/tests/test_y.py::test_3"
CAND, TIP = "c" * 40, "b" * 40


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "hermetic_unavailable", lambda: "off in these tests")
    return build_world(tmp_path, monkeypatch)


def report_run(root, sha, *, collected=(), finished=(), failed=(), exit_code=0, recorder=None, **fields):
    """A run that left ``tests.jsonl`` in its sandbox; ``recorder`` is the failures file's content."""
    lines = [{"kind": "collected", "nodeid": i} for i in collected]
    lines += [{"kind": "report", "nodeid": i, "when": "call", "outcome": "passed"} for i in finished]
    lines += [{"kind": "report", "nodeid": i, "when": "call", "outcome": "failed"} for i in failed]
    path = Path(root) / gate.REPORT_NAME
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return gate.SuiteRun(exit_code, tuple(failed if recorder is None else recorder), sha, log_tail="log\n",
                         report_path=str(path), **fields)


class ReportRunner:
    """Full runs by sha as report_run keywords; each rerun round fails the ids ``rerun_failures[round]`` names."""

    def __init__(self, full, rerun_failures=()):
        self.full = full
        self.rerun_failures = list(rerun_failures)
        self.calls = []

    def __call__(self, repo, sha, root, *, select, timeout_s):
        self.calls.append((sha, tuple(select)))
        if not select:
            return report_run(root, sha, **self.full[sha])
        round_no = sum(1 for _, picked in self.calls if picked) - 1
        failed = set(self.rerun_failures[round_no] if round_no < len(self.rerun_failures) else ()) & set(select)
        return report_run(root, sha, collected=select, finished=set(select) - failed, failed=sorted(failed),
                          exit_code=1 if failed else 0)

    def reruns(self):
        return [picked for _, picked in self.calls if picked]


CLEAN = dict(collected=[T1], finished=[T1])
KILLED = dict(collected=[T1, T2, T3], finished=[T1], exit_code=-9)


# ── observe ───────────────────────────────────────────────────────────────

def test_a_report_with_no_collected_line_is_unavailable_not_pass(tmp_path):
    run = report_run(tmp_path, CAND, collected=[], exit_code=0)
    seen = gate.observe(run, CAND)
    assert seen.reason and "collection" in seen.reason and seen.failed == () and seen.unfinished == ()


def test_collected_ids_with_no_terminal_line_are_unfinished(tmp_path):
    seen = gate.observe(report_run(tmp_path, CAND, **KILLED), CAND)
    assert seen.reason == "" and seen.unfinished == (T2, T3) and seen.failed == ()


def test_a_setup_that_skipped_or_failed_is_terminal(tmp_path):
    path = tmp_path / gate.REPORT_NAME
    lines = [{"kind": "collected", "nodeid": T1}, {"kind": "collected", "nodeid": T2},
             {"kind": "report", "nodeid": T1, "when": "setup", "outcome": "skipped"},
             {"kind": "report", "nodeid": T2, "when": "setup", "outcome": "passed"}]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    seen = gate.observe(gate.SuiteRun(-9, (), CAND, report_path=str(path)), CAND)
    assert seen.unfinished == (T2,)


def test_a_failure_in_the_report_counts_when_the_killed_run_lost_the_recorders_file(tmp_path):
    run = report_run(tmp_path, CAND, collected=[T1, T2, T3], finished=[T1], failed=[T2], exit_code=-9, recorder=())
    seen = gate.observe(run, CAND)
    assert seen.failed == (T2,) and seen.unfinished == (T3,) and seen.passed == {T1}


def test_a_run_killed_with_every_test_finished_is_unavailable(tmp_path):
    seen = gate.observe(report_run(tmp_path, CAND, collected=[T1], finished=[T1], exit_code=-9), CAND)
    assert "pytest exited -9" in seen.reason


def test_a_run_without_a_report_keeps_the_failed_only_reading():
    seen = gate.observe(ok_run(CAND, (T1,)), CAND)
    assert seen.failed == (T1,) and seen.unfinished == () and seen.reason == ""


def test_a_clean_run_has_neither_failed_nor_unfinished_ids(tmp_path):
    seen = gate.observe(report_run(tmp_path, CAND, **CLEAN), CAND)
    assert (seen.failed, seen.unfinished, seen.reason) == ((), (), "") and seen.passed == {T1}


# ── join_partial_sets ─────────────────────────────────────────────────────

def _first(passing=(T1,), covered=(T1, T2)):
    return gate.PartialSet(CAND, TIP, frozenset(covered), frozenset(passing))


def test_join_unions_the_passing_ids_of_two_sets_that_fit():
    joined = gate.join_partial_sets(_first(), gate.PartialSet(CAND, TIP, frozenset({T2}), frozenset({T2})))
    assert joined.passing == {T1, T2} and joined.covered == {T1, T2}


def test_join_keeps_a_non_passing_id_the_second_set_did_not_pass():
    joined = gate.join_partial_sets(_first(), gate.PartialSet(CAND, TIP, frozenset({T2}), frozenset()))
    assert joined.covered - joined.passing == {T2}


def test_join_refuses_a_different_candidate():
    with pytest.raises(gate.JoinRefused, match="candidate"):
        gate.join_partial_sets(_first(), gate.PartialSet("d" * 40, TIP, frozenset({T2}), frozenset({T2})))


def test_join_refuses_a_different_tip():
    with pytest.raises(gate.JoinRefused, match="tip"):
        gate.join_partial_sets(_first(), gate.PartialSet(CAND, "e" * 40, frozenset({T2}), frozenset({T2})))


def test_join_refuses_a_second_set_that_misses_a_non_passing_id():
    first = _first(passing=(), covered=(T1, T2))
    with pytest.raises(gate.JoinRefused, match="cover"):
        gate.join_partial_sets(first, gate.PartialSet(CAND, TIP, frozenset({T1}), frozenset({T1})))


def test_join_refuses_a_second_set_that_covers_a_passing_id():
    with pytest.raises(gate.JoinRefused, match="cover"):
        gate.join_partial_sets(_first(), gate.PartialSet(CAND, TIP, frozenset({T1, T2}), frozenset({T1, T2})))


# ── run_suite on unfinished ids and the tip re-read ───────────────────────

def test_an_unfinished_id_that_passes_both_reruns_is_unconfirmed_and_the_landing_passes(world):
    cand = world.code_commit()
    runner = ReportRunner({cand: KILLED, world.base: CLEAN})
    out = world.run(runner)
    assert out.record["status"] == gate.PASS and out.decision.admitted
    assert out.record["unfinished"] == [T2, T3] and out.record["unconfirmed"] == [T2, T3]
    assert out.record["reruns"][T2] == {"first_run": "unfinished", "failed_rounds": [], "result": "unconfirmed"}
    assert runner.reruns() == [(T2, T3), (T2, T3)]
    entries = flake_list.read_entries(flake_list.journal_path(world.common))
    assert sorted(e["test"] for e in entries) == [T2, T3] and len({e["id"] for e in entries}) == 2


def test_an_unfinished_id_that_fails_a_rerun_is_confirmed(world):
    cand = world.code_commit()
    out = world.run(ReportRunner({cand: KILLED, world.base: CLEAN}, rerun_failures=[(), {T3}]))
    assert out.record["status"] == gate.FAIL and out.record["new_failures"] == [T3]
    assert out.record["unconfirmed"] == [T2]
    assert out.record["reruns"][T3] == {"first_run": "unfinished", "failed_rounds": [2], "result": "confirmed"}


def test_a_baseline_failure_that_is_unfinished_in_the_candidate_is_not_rerun(world):
    cand = world.code_commit()
    baseline = dict(collected=[T1, T2], finished=[T1], failed=[T2], exit_code=1)
    runner = ReportRunner({cand: dict(collected=[T1, T2], finished=[T1], exit_code=-9), world.base: baseline})
    out = world.run(runner)
    assert out.record["status"] == gate.PASS and runner.reruns() == []


def test_a_run_whose_report_has_no_collected_line_is_unavailable_in_the_record(world):
    cand = world.code_commit()
    out = world.run(ReportRunner({cand: dict(collected=[], exit_code=0)}))
    assert out.record["status"] == gate.UNAVAILABLE and "collection" in out.record["unavailable_reason"]


def test_a_tip_that_moved_during_the_reruns_is_unavailable(world):
    cand = world.code_commit()
    moved = "f" * 40
    out = world.run(ReportRunner({cand: KILLED, world.base: CLEAN}), tip_reader=lambda: moved)
    assert out.record["status"] == gate.UNAVAILABLE and "tips" in out.record["unavailable_reason"]
    assert flake_list.read_entries(flake_list.journal_path(world.common)) == []


def test_a_tip_that_cannot_be_re_read_after_the_reruns_is_unavailable(world):
    cand = world.code_commit()

    def broken():
        raise shared.GateError("fetch failed")

    out = world.run(ReportRunner({cand: KILLED, world.base: CLEAN}), tip_reader=broken)
    assert out.record["status"] == gate.UNAVAILABLE and "re-read" in out.record["unavailable_reason"]


def test_an_unmoved_tip_lets_the_reruns_count(world):
    cand = world.code_commit()
    out = world.run(ReportRunner({cand: KILLED, world.base: CLEAN}), tip_reader=lambda: world.base)
    assert out.record["status"] == gate.PASS


def test_more_unfinished_ids_than_the_reruns_take_is_unavailable(world, monkeypatch):
    cand = world.code_commit()
    monkeypatch.setattr(gate, "MAX_UNFINISHED_RERUN", 1)
    runner = ReportRunner({cand: KILLED, world.base: CLEAN})
    out = world.run(runner)
    assert out.record["status"] == gate.UNAVAILABLE and runner.reruns() == []


# ── a rerun proves a pass only by reporting it; a failure it reported is never dropped ──

class RerunScript(ReportRunner):
    """Full runs as ReportRunner; each rerun round is ``rounds[n]``, a callable(root, sha, select) -> SuiteRun."""

    def __init__(self, full, rounds):
        super().__init__(full)
        self.rounds = list(rounds)

    def __call__(self, repo, sha, root, *, select, timeout_s):
        if not select:
            return super().__call__(repo, sha, root, select=select, timeout_s=timeout_s)
        self.calls.append((sha, tuple(select)))
        return self.rounds[sum(1 for _, picked in self.calls if picked) - 1](root, sha, tuple(select))


def passes(root, sha, select):
    return report_run(root, sha, collected=select, finished=select)


def omits(*ids):
    return lambda root, sha, select: report_run(
        root, sha, collected=[i for i in select if i not in ids], finished=[i for i in select if i not in ids])


def fails(*ids):
    return lambda root, sha, select: report_run(
        root, sha, collected=select, finished=[i for i in select if i not in ids], failed=ids, exit_code=1)


def times_out_after_failing(*ids):
    return lambda root, sha, select: report_run(
        root, sha, collected=select, failed=ids, exit_code=-9, timed_out=True)


def test_an_id_the_rerun_never_reports_is_unavailable_not_a_pass(world):
    cand = world.code_commit()
    out = world.run(RerunScript({cand: KILLED, world.base: CLEAN}, [omits(T3), passes]))
    assert out.record["status"] == gate.UNAVAILABLE and "did not report" in out.record["unavailable_reason"]
    assert out.record["new_failures"] == [] and out.record["unconfirmed"] == []
    assert flake_list.read_entries(flake_list.journal_path(world.common)) == []


def skips(*ids):
    def run(root, sha, select):
        out = report_run(root, sha, collected=select, finished=[i for i in select if i not in ids])
        with open(out.report_path, "a", encoding="utf-8") as fh:
            fh.writelines(json.dumps({"kind": "report", "nodeid": i, "when": "setup", "outcome": "skipped"}) + "\n"
                          for i in ids)
        return out
    return run


def test_a_rerun_that_skips_a_new_failure_is_unavailable_not_a_pass(world):
    cand = world.code_commit()
    out = world.run(RerunScript({cand: KILLED, world.base: CLEAN}, [skips(T3), skips(T3)]))
    assert out.record["status"] == gate.UNAVAILABLE and "did not report" in out.record["unavailable_reason"]
    assert out.record["unconfirmed"] == []
    assert flake_list.read_entries(flake_list.journal_path(world.common)) == []


def test_a_rerun_with_no_report_is_unavailable_not_a_pass(world):
    cand = world.code_commit()
    out = world.run(RerunScript({cand: KILLED, world.base: CLEAN},
                                [lambda root, sha, select: ok_run(sha, ()), passes]))
    assert out.record["status"] == gate.UNAVAILABLE and "did not report" in out.record["unavailable_reason"]


def test_a_failure_of_the_first_round_stands_when_the_second_round_times_out(world):
    cand = world.code_commit()
    timed_out = lambda root, sha, select: gate.SuiteRun(-9, (), sha, timed_out=True)  # noqa: E731
    out = world.run(RerunScript({cand: KILLED, world.base: CLEAN}, [fails(T3), timed_out]))
    assert out.record["status"] == gate.FAIL and out.record["new_failures"] == [T3]
    assert not out.decision.admitted and "pytest did not finish" in out.record["unavailable_reason"]


def test_a_failure_a_cut_short_round_reported_stands(world):
    cand = world.code_commit()
    out = world.run(RerunScript({cand: KILLED, world.base: CLEAN}, [times_out_after_failing(T2), passes]))
    assert out.record["status"] == gate.FAIL and out.record["new_failures"] == [T2]
    assert not out.decision.admitted


def test_a_failure_of_the_first_round_stands_when_the_tip_cannot_be_re_read(world):
    cand = world.code_commit()

    def broken():
        raise shared.GateError("fetch failed")

    out = world.run(RerunScript({cand: KILLED, world.base: CLEAN}, [fails(T3), passes]), tip_reader=broken)
    assert out.record["status"] == gate.FAIL and out.record["new_failures"] == [T3]
    assert "re-read" in out.record["unavailable_reason"] and not out.decision.admitted


def test_a_failure_of_the_first_round_stands_when_the_tip_moved(world):
    cand = world.code_commit()
    out = world.run(RerunScript({cand: KILLED, world.base: CLEAN}, [fails(T3), passes]), tip_reader=lambda: "f" * 40)
    assert out.record["status"] == gate.FAIL and out.record["new_failures"] == [T3]


def test_the_sandboxed_pytest_never_sees_the_operators_journal_path(tmp_path):
    env = gate.suite_env(tmp_path, "/ub", tmp_path / "f", [],
                         base_env={"PATH": "/bin", flake_list.JOURNAL_ENV: "/live/journal.jsonl"})
    assert flake_list.JOURNAL_ENV not in env


def test_a_session_that_breaks_after_every_test_ran_is_unavailable_when_the_tip_has_a_failure(world):
    cand = world.code_commit()
    broken_session = dict(collected=[T1, T2], finished=[T1], failed=[T2], exit_code=3)
    tip_run = dict(collected=[T1, T2], finished=[T1], failed=[T2], exit_code=1)
    out = world.run(ReportRunner({cand: broken_session, world.base: tip_run}))
    assert out.record["status"] == gate.UNAVAILABLE and "pytest exited 3" in out.record["unavailable_reason"]
