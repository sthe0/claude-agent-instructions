"""Tests for hook-session-due.py — the nine periodic SessionStart due checks.

Hermetic: HOME, the findings store and the instructions repo are redirected into
tmp_path; the producers (self-diagnose.py, rule-salience-report.py, ...) are tiny
fake scripts dropped into a throwaway SCRIPTS_DIR that record their argv, so no
real scan, ledger refresh or model call ever runs.

The nine one-hook-per-check test files this module replaces are accounted for by
`<base file>::<base test>` markers in the docstrings below (a base case that
survives as a differently named test here) or by DROPPED_CASES (a base case whose
subject no longer exists), so no base case is lost silently.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace

import pytest

REAL_SCRIPTS = Path(__file__).resolve().parent.parent


def _load_hook():
    spec = importlib.util.spec_from_file_location("hook_session_due", REAL_SCRIPTS / "hook-session-due.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = _load_hook()
REAL_LAUNCH_SCORECARD = hook.launch_scorecard

DROPPED_CASES = {
    "test_hook_orphan_worktree_sweep.py::test_shim_accepts_the_old_flags_and_runs_the_runner":
        "the orphan-worktree-sweep hook is deleted; the reaper owns the sweep and has its own tests",
    "test_hook_orphan_worktree_sweep.py::test_shim_dry_run_prints_only_verdict_lines_and_exits_zero":
        "the orphan-worktree-sweep hook is deleted; the reaper owns the sweep and has its own tests",
    "test_budget_calibration.py::test_hook_fail_open_on_bad_stdin":
        "the session hook never reads stdin; fail-open is pinned by test_hook_fail_open_when_check_raises",
    "test_hook_policy_scorecard_due.py::test_malformed_stdin_does_not_crash":
        "the session hook never reads stdin, so there is no input to be malformed",
    "test_hook_policy_scorecard_due.py::test_stamp_env_override_redirects_and_default_stays_untouched":
        "the per-hook CLAUDE_POLICY_SCORECARD_STAMP override is gone; tests redirect every stamp through HOME",
    "test_hook_policy_scorecard_due.py::test_stamp_env_override_absent_falls_back_to_default":
        "the per-hook CLAUDE_POLICY_SCORECARD_STAMP override is gone; tests redirect every stamp through HOME",
}

Case = namedtuple("Case", "id fn stamp setup expect dry_reports")

GROOMING_STAMP = "claude-instruction-grooming.stamp.json"

_CONFIG_TEMPLATE = """\
| Key | Value | Meaning |
|---|---|---|
| `claude-md-max-lines` | `100` | . |
| `claude-md-max-chars` | `1000` | . |
| `readme-max-lines` | `50` | . |
| `cursor-mirror-max-lines` | `50` | . |
| `skill-md-max-lines` | `50` | . |
| `policy-md-max-lines` | `50` | . |
"""

_FLAGGED_CLUSTER = {
    "occurrences_total": 3,
    "members": ["2026-08-11-loop.md", "2026-08-17-loop.md", "2026-08-20-loop.md"],
    "flagged": True,
}
_UNFLAGGED_CLUSTER = {"occurrences_total": 2, "members": ["2026-07-01-foo.md"], "flagged": False}


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_INSTRUCTIONS_REPO", str(tmp_path / "no-repo"))
    monkeypatch.setenv("CLAUDE_SELF_DIAGNOSE_STORE", str(tmp_path / "findings.jsonl"))
    monkeypatch.delenv("CLAUDE_POLICY_LEDGER", raising=False)
    launches = []
    monkeypatch.setattr(hook, "SCRIPTS_DIR", scripts)
    monkeypatch.setattr(hook, "launch_scorecard", launches.append)
    monkeypatch.setattr(hook.store, "route_advisory", lambda rows, **kwargs: None)
    calls_path = tmp_path / "calls.jsonl"

    def calls():
        if not calls_path.exists():
            return []
        return [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()]

    return SimpleNamespace(
        home=home, scripts=scripts, tmp=tmp_path, launches=launches, calls=calls,
        calls_path=calls_path, monkeypatch=monkeypatch,
    )


def fake_producer(env, name, stdout="", exit_code=0):
    body = (
        "import json, os, sys\n"
        f"with open({str(env.calls_path)!r}, 'a') as f:\n"
        f"    f.write(json.dumps({{'script': {name!r}, 'argv': sys.argv[1:], "
        f"'budget': os.environ.get({hook.JUDGE_BUDGET_ENV!r})}}) + '\\n')\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.exit({exit_code})\n"
    )
    path = env.scripts / name
    path.write_text(body, encoding="utf-8")
    return path


def fake_sleeper(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    return path


def stamp_file(env, name):
    return env.home / ".local" / "state" / name


def seed_stamp(env, name, age_s, fmt="float"):
    ts = time.time() - age_s
    path = stamp_file(env, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(repr(ts) if fmt == "float" else dt.datetime.fromtimestamp(ts).isoformat(), encoding="utf-8")
    return path.read_text(encoding="utf-8")


def make_fake_repo(env, readme_lines):
    repo = env.tmp / "fake-repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(REAL_SCRIPTS / "lint-prose-length.py", repo / "scripts" / "lint-prose-length.py")
    (repo / "config.md").write_text(_CONFIG_TEMPLATE, encoding="utf-8")
    (repo / "README.md").write_text("x\n" * readme_lines, encoding="utf-8")
    (repo / "CLAUDE.md").write_text("x\n", encoding="utf-8")
    (repo / "cursor" / "rules").mkdir(parents=True)
    (repo / "cursor" / "rules" / "claude-code-sync.mdc").write_text("m\n", encoding="utf-8")
    env.monkeypatch.setenv("CLAUDE_INSTRUCTIONS_REPO", str(repo))
    return repo


def run_check(name, stamp, *, dry_run=False, force=False, budget=20.0):
    run = hook.Run(dry_run, force, budget)
    getattr(hook, name)(run, stamp)
    return run


# ── per-check setups for the table-driven cases ──────────────────────────────

def _setup_sigma(env):
    pass


def _setup_improvement_scan(env):
    hook.store.upsert_findings(
        [{"kind": "backlog-item", "path": "BL-1", "detail": "d"}],
        source=hook.store.SOURCE_IMPROVEMENT_SCAN,
    )


def _setup_scorecard(env):
    pass


def _setup_budget(env):
    fake_producer(env, "budget-calibration.py", "tier small off\n")


def _setup_judge(env):
    fake_producer(env, "judge-usage-report.py", "ceiling 30s live 29s\n", exit_code=1)


def _setup_phase3(env):
    fake_producer(env, "rule-salience-report.py", "phase3-readiness: DUE\nnever-observed: 2\n")


def _setup_self_diagnose(env):
    fake_producer(env, "self-diagnose.py", json.dumps([{"kind": "near-duplicate", "path": "p", "detail": "d"}]), 1)


def _setup_promote(env):
    fake_producer(env, "record-experience.py", json.dumps({"clusters": [_FLAGGED_CLUSTER], "stats": {}}))


CASES = [
    Case("sigma-sentinel", "check_sigma_sentinel", "claude-sigma-sentinel.stamp",
         _setup_sigma, "σ build-trigger sentinel due", True),
    Case("improvement-scan", "check_improvement_scan", "claude-improvement-scan.stamp",
         _setup_improvement_scan, "improvement-scan: 1 open finding", True),
    Case("policy-scorecard", "check_policy_scorecard", "claude-policy-scorecard.stamp",
         _setup_scorecard, "policy-scorecard ledger refreshed", False),
    Case("budget-calibration", "check_budget_calibration", "claude-budget-calibration.stamp",
         _setup_budget, "miscalibrated: tier small off", True),
    Case("judge-ceiling-drift", "check_judge_ceiling_drift", "claude-judge-ceiling-drift.stamp",
         _setup_judge, "latency has drifted", True),
    Case("phase3", "check_phase3", "claude-phase3-due.stamp",
         _setup_phase3, "[phase3-due]", True),
    Case("self-diagnose", "check_self_diagnose", "claude-self-diagnose.stamp",
         _setup_self_diagnose, "self-diagnose: 1 self-friction item(s)", True),
    Case("promote-scan", "check_promote_scan", "claude-promote-scan.stamp",
         _setup_promote, "promote-scan: 1 principle-induction candidate(s)", True),
]
CASE_PARAMS = [pytest.param(c, id=c.id) for c in CASES]


# ── the registry itself ──────────────────────────────────────────────────────

def test_due_intervals_are_the_base_values():
    day = 86400
    assert hook.DUE_INTERVALS_S == {
        "claude-instruction-grooming.stamp.json": 7 * day,
        "claude-self-diagnose.stamp": day,
        "claude-phase3-due.stamp": 7 * day,
        "claude-promote-scan.stamp": 7 * day,
        "claude-policy-scorecard.stamp": 7 * day,
        "claude-budget-calibration.stamp": 7 * day,
        "claude-sigma-sentinel.stamp": 30 * day,
        "claude-improvement-scan.stamp": 7 * day,
        "claude-judge-ceiling-drift.stamp": 7 * day,
    }


def test_every_interval_has_exactly_one_check():
    assert sorted(stamp for stamp, _ in hook.CHECKS) == sorted(hook.DUE_INTERVALS_S)
    assert {c.stamp for c in CASES} | {GROOMING_STAMP} == set(hook.DUE_INTERVALS_S)


@pytest.mark.parametrize("fmt", ["float", "iso"])
@pytest.mark.parametrize("stamp", sorted(hook.DUE_INTERVALS_S))
def test_interval_boundary_is_plus_minus_a_minute(env, stamp, fmt):
    interval = hook.DUE_INTERVALS_S[stamp]
    run = hook.Run(False, False, 20.0)
    for delta, expected in ((interval - 60, False), (interval + 60, True)):
        path = stamp_file(env, stamp)
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = run.now - delta
        path.write_text(repr(ts) if fmt == "float" else dt.datetime.fromtimestamp(ts).isoformat(), encoding="utf-8")
        assert run.due(stamp) is expected, (stamp, fmt, delta)


def test_missing_or_garbled_stamp_is_due(env):
    """Ported: test_hook_self_diagnose_due.py::test_last_run_missing_file_is_none
    test_hook_promote_scan_due.py::test_last_run_missing_file_is_none
    test_hook_phase3_due.py::test_last_throttle_missing_file_is_none"""
    name = "claude-sigma-sentinel.stamp"
    assert hook.read_stamp(name) is None
    path = stamp_file(env, name)
    path.parent.mkdir(parents=True)
    path.write_text("not a time", encoding="utf-8")
    assert hook.read_stamp(name) is None
    assert hook.Run(False, False, 20.0).due(name) is True


def test_stamp_roundtrip_and_both_formats(env):
    """Ported: test_hook_self_diagnose_due.py::test_stamp_roundtrip
    test_hook_promote_scan_due.py::test_stamp_roundtrip
    test_hook_phase3_due.py::test_throttle_stamp_roundtrip"""
    hook.record_due_stamp("claude-sigma-sentinel.stamp", 1000.0)
    assert hook.read_stamp("claude-sigma-sentinel.stamp") == 1000.0
    stamp_file(env, "claude-sigma-sentinel.stamp").write_text("2026-01-01T00:00:00", encoding="utf-8")
    assert hook.read_stamp("claude-sigma-sentinel.stamp") == dt.datetime(2026, 1, 1).timestamp()


# ── table-driven: one positive case per check, and its throttle semantics ───

@pytest.mark.parametrize("case", CASE_PARAMS)
def test_positive_case_reports_and_stamps(env, capsys, case):
    """Ported: test_hook_self_diagnose_due.py::test_main_reports_findings_to_stderr
    test_hook_policy_scorecard_due.py::test_first_run_is_due_and_launches
    test_hook_phase3_due.py::test_main_emits_on_due
    test_hook_promote_scan_due.py::test_main_reports_flagged_cluster_to_stderr
    test_improvement_scan_hook.py::test_report_names_the_skill_and_finding_count"""
    case.setup(env)
    run_check(case.fn, case.stamp)
    assert case.expect in capsys.readouterr().err
    assert abs(float(stamp_file(env, case.stamp).read_text()) - time.time()) < 60


@pytest.mark.parametrize("case", CASE_PARAMS)
def test_recent_stamp_skips_the_check_and_keeps_the_stamp(env, capsys, case):
    """Ported: test_hook_self_diagnose_due.py::test_main_throttled_skips_scan
    test_hook_policy_scorecard_due.py::test_not_due_skips_launch_and_stamp_rewrite
    test_hook_phase3_due.py::test_main_throttled_skips_check
    test_hook_promote_scan_due.py::test_main_throttled_within_7_days_skips_scan
    test_improvement_scan_hook.py::test_main_throttled_within_window_skips_report"""
    case.setup(env)
    before = seed_stamp(env, case.stamp, 60)
    run_check(case.fn, case.stamp)
    assert capsys.readouterr().err == ""
    assert env.calls() == [] and env.launches == []
    assert stamp_file(env, case.stamp).read_text() == before


@pytest.mark.parametrize("case", CASE_PARAMS)
def test_stale_stamp_fires_again_and_restamps(env, capsys, case):
    """Ported: test_hook_self_diagnose_due.py::test_main_past_throttle_window_scans_and_restamps
    test_hook_policy_scorecard_due.py::test_due_after_throttle_window_launches_again
    test_hook_phase3_due.py::test_main_past_throttle_window_checks_and_restamps
    test_hook_promote_scan_due.py::test_main_past_throttle_window_scans_and_restamps
    test_improvement_scan_hook.py::test_main_past_throttle_window_reports_and_restamps"""
    case.setup(env)
    before = seed_stamp(env, case.stamp, hook.DUE_INTERVALS_S[case.stamp] + 60)
    run_check(case.fn, case.stamp)
    assert case.expect in capsys.readouterr().err
    assert float(stamp_file(env, case.stamp).read_text()) > float(before)


@pytest.mark.parametrize("case", CASE_PARAMS)
def test_force_run_ignores_the_throttle_without_consuming_it(env, capsys, case):
    """Ported: test_hook_self_diagnose_due.py::test_main_force_run_bypasses_throttle_without_consuming
    test_hook_phase3_due.py::test_main_force_run_bypasses_throttle_without_consuming
    test_hook_promote_scan_due.py::test_main_force_run_bypasses_throttle_without_consuming
    test_improvement_scan_hook.py::test_main_force_run_bypasses_throttle_without_consuming"""
    case.setup(env)
    before = seed_stamp(env, case.stamp, 60)
    run_check(case.fn, case.stamp, force=True)
    assert case.expect in capsys.readouterr().err
    assert stamp_file(env, case.stamp).read_text() == before


@pytest.mark.parametrize("case", CASE_PARAMS)
def test_dry_run_reports_without_writing_any_state(env, capsys, case):
    """Ported: test_hook_self_diagnose_due.py::test_main_dry_run_never_touches_stamp
    test_hook_phase3_due.py::test_main_dry_run_never_touches_either_stamp
    test_hook_phase3_due.py::test_main_dry_run_still_reports_due
    test_hook_promote_scan_due.py::test_main_dry_run_never_touches_stamp
    test_improvement_scan_hook.py::test_main_dry_run_never_writes_stamp"""
    case.setup(env)
    run_check(case.fn, case.stamp, dry_run=True)
    err = capsys.readouterr().err
    if case.dry_reports:
        assert case.expect in err
    else:
        assert err == "" and env.launches == []
    assert not (env.home / ".local").exists()


@pytest.mark.parametrize("case", CASE_PARAMS)
def test_dry_run_ignores_a_recent_throttle(env, capsys, case):
    """Ported: test_improvement_scan_hook.py::test_main_dry_run_ignores_existing_throttle"""
    if not case.dry_reports:
        pytest.skip("dry-run never launches the background scorecard refresh")
    case.setup(env)
    before = seed_stamp(env, case.stamp, 60)
    run_check(case.fn, case.stamp, dry_run=True)
    assert case.expect in capsys.readouterr().err
    assert stamp_file(env, case.stamp).read_text() == before


# ── the whole hook ───────────────────────────────────────────────────────────

def test_main_runs_all_nine_checks_and_stamps_each(env, capsys):
    for case in CASES:
        case.setup(env)
    make_fake_repo(env, readme_lines=46)
    assert hook.main([]) == 0
    captured = capsys.readouterr()
    assert "[instruction-grooming]" in captured.out
    for case in CASES:
        assert case.expect in captured.err, case.id
    for stamp in hook.DUE_INTERVALS_S:
        assert stamp_file(env, stamp).exists(), stamp
    assert stamp_file(env, hook.PHASE3_BASELINE_STAMP).exists()
    assert len(env.launches) == 1


def test_one_failing_check_does_not_stop_the_others(env, monkeypatch):
    seen = []

    def boom(run, stamp):
        raise RuntimeError("boom")

    monkeypatch.setattr(hook, "CHECKS", [("a", boom), ("b", lambda run, stamp: seen.append(stamp))])
    assert hook.main([]) == 0
    assert seen == ["b"]


def test_main_fails_open_when_the_state_dir_is_unwritable(env, capsys):
    """Ported: test_hook_phase3_due.py::test_main_fails_open_when_stamp_dir_unwritable"""
    (env.home / ".local").write_text("a file, not a dir", encoding="utf-8")
    _setup_phase3(env)
    assert hook.main([]) == 0


def test_main_fails_open_when_a_producer_raises(env, monkeypatch):
    """Ported: test_hook_phase3_due.py::test_main_fails_open_when_run_check_raises"""
    _setup_phase3(env)
    monkeypatch.setattr(hook.Run, "produce", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    assert hook.main([]) == 0


# ── the shared wall-clock budget ─────────────────────────────────────────────

def test_budget_bounds_the_run_and_skips_late_checks_unstamped(env, monkeypatch):
    repo = make_fake_repo(env, readme_lines=10)
    fake_sleeper(repo / "scripts" / "lint-prose-length.py")
    _setup_budget(env)
    monkeypatch.setattr(hook, "TOTAL_BUDGET_S", 2.0)
    started = time.monotonic()
    assert hook.main([]) == 0
    assert time.monotonic() - started < 8
    assert stamp_file(env, "claude-sigma-sentinel.stamp").exists()
    for late in ("claude-budget-calibration.stamp", "claude-judge-ceiling-drift.stamp",
                 "claude-phase3-due.stamp", "claude-self-diagnose.stamp", "claude-promote-scan.stamp"):
        assert not stamp_file(env, late).exists(), late
    assert env.calls() == []


def test_a_budget_under_the_minimum_slice_runs_nothing(env, monkeypatch, capsys):
    monkeypatch.setattr(hook, "TOTAL_BUDGET_S", hook.MIN_SLICE_S / 2)
    assert hook.main([]) == 0
    assert capsys.readouterr().err == ""
    assert not (env.home / ".local").exists()


def test_a_producer_never_outlives_the_remaining_budget(env):
    producer = fake_sleeper(env.scripts / "slow.py")
    started = time.monotonic()
    assert hook.Run(False, False, 0.5).produce(producer, cap_s=15) is None
    assert time.monotonic() - started < 5


PRODUCER_CHECKS = [
    pytest.param("check_budget_calibration", "claude-budget-calibration.stamp", "budget-calibration.py", id="budget"),
    pytest.param("check_judge_ceiling_drift", "claude-judge-ceiling-drift.stamp", "judge-usage-report.py", id="judge"),
    pytest.param("check_phase3", "claude-phase3-due.stamp", "rule-salience-report.py", id="phase3"),
    pytest.param("check_self_diagnose", "claude-self-diagnose.stamp", "self-diagnose.py", id="self-diagnose"),
    pytest.param("check_promote_scan", "claude-promote-scan.stamp", "record-experience.py", id="promote-scan"),
]


@pytest.mark.parametrize("check,stamp,script", PRODUCER_CHECKS)
def test_a_producer_killed_by_the_budget_keeps_its_stale_stamp(env, check, stamp, script):
    fake_sleeper(env.scripts / script)
    stale = seed_stamp(env, stamp, age_s=30 * 86400)
    run_check(check, stamp, budget=0.5)
    assert stamp_file(env, stamp).read_text(encoding="utf-8") == stale


@pytest.mark.parametrize("check,stamp,script", PRODUCER_CHECKS)
def test_a_missing_producer_leaves_the_check_unstamped(env, check, stamp, script):
    run_check(check, stamp)
    assert not stamp_file(env, stamp).exists()


@pytest.mark.parametrize("check,stamp,script", PRODUCER_CHECKS)
def test_a_producer_that_completes_with_a_failure_exit_is_stamped(env, check, stamp, script):
    fake_producer(env, script, "", exit_code=3)
    run_check(check, stamp)
    assert stamp_file(env, stamp).exists()


def test_the_default_budget_is_twenty_seconds():
    assert hook.TOTAL_BUDGET_S == 20.0


# ── instruction-grooming (stdout offer, per-file debounce) ──────────────────

def test_grooming_offer_names_file_and_pct(env, capsys):
    """Ported: test_instruction_grooming_hook.py::test_warn_file_emits_offer"""
    make_fake_repo(env, readme_lines=46)
    run_check("check_grooming", GROOMING_STAMP)
    out = capsys.readouterr().out
    assert "[instruction-grooming]" in out and "README.md" in out and "92%" in out
    assert "AskUserQuestion" in out
    assert json.loads(stamp_file(env, GROOMING_STAMP).read_text()).keys() == {"README.md"}


def test_grooming_clean_tree_silent(env, capsys):
    """Ported: test_instruction_grooming_hook.py::test_clean_tree_silent"""
    make_fake_repo(env, readme_lines=10)
    run_check("check_grooming", GROOMING_STAMP)
    assert capsys.readouterr().out.strip() == ""


def test_grooming_missing_repo_silent(env, capsys):
    """Ported: test_instruction_grooming_hook.py::test_missing_repo_silent"""
    run_check("check_grooming", GROOMING_STAMP)
    assert capsys.readouterr().out.strip() == ""


def test_grooming_second_run_within_window_silent(env, capsys):
    """Ported: test_instruction_grooming_hook.py::test_second_run_within_window_silent"""
    make_fake_repo(env, readme_lines=46)
    run_check("check_grooming", GROOMING_STAMP)
    assert "[instruction-grooming]" in capsys.readouterr().out
    run_check("check_grooming", GROOMING_STAMP)
    assert capsys.readouterr().out.strip() == ""


def test_grooming_due_again_after_window_fires(env, capsys):
    """Ported: test_instruction_grooming_hook.py::test_due_again_after_window_fires"""
    make_fake_repo(env, readme_lines=46)
    stale = dt.datetime.now() - dt.timedelta(days=8)
    path = stamp_file(env, GROOMING_STAMP)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"README.md": stale.isoformat()}), encoding="utf-8")
    run_check("check_grooming", GROOMING_STAMP)
    out = capsys.readouterr().out
    assert "[instruction-grooming]" in out and "README.md" in out


def test_grooming_only_newly_due_file_named(env, capsys):
    """Ported: test_instruction_grooming_hook.py::test_only_newly_due_file_named"""
    repo = make_fake_repo(env, readme_lines=46)
    path = stamp_file(env, GROOMING_STAMP)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"README.md": dt.datetime.now().isoformat()}), encoding="utf-8")
    (repo / "CLAUDE.md").write_text("x" * 920, encoding="utf-8")
    run_check("check_grooming", GROOMING_STAMP)
    out = capsys.readouterr().out
    assert "CLAUDE.md" in out and "README.md" not in out


def test_grooming_debounce_boundary_is_plus_minus_a_minute():
    now = time.time()
    interval = hook.DUE_INTERVALS_S[GROOMING_STAMP]
    recent = {"README.md": dt.datetime.fromtimestamp(now - (interval - 60)).isoformat()}
    stale = {"README.md": dt.datetime.fromtimestamp(now - (interval + 60)).isoformat()}
    assert hook.grooming_file_due(recent, "README.md", now, interval) is False
    assert hook.grooming_file_due(stale, "README.md", now, interval) is True
    assert hook.grooming_file_due({}, "README.md", now, interval) is True


def test_grooming_dry_run_and_force_run_offer_without_consuming(env, capsys):
    make_fake_repo(env, readme_lines=46)
    for flags in ({"dry_run": True}, {"force": True}):
        run_check("check_grooming", GROOMING_STAMP, **flags)
        assert "README.md" in capsys.readouterr().out
        assert not stamp_file(env, GROOMING_STAMP).exists()


# ── self-diagnose ────────────────────────────────────────────────────────────

def test_scan_self_diagnose_returns_records(env):
    """Ported: test_hook_self_diagnose_due.py::test_run_scanner_returns_records"""
    fake_producer(env, "self-diagnose.py", json.dumps(
        [{"kind": "orphan-leaf", "path": "p", "detail": "d"}, {"path": "no-kind"}]), exit_code=1)
    records = hook.scan_self_diagnose(hook.Run(False, False, 20.0))
    assert records == [{"kind": "orphan-leaf", "path": "p", "detail": "d"}]
    assert env.calls()[0]["argv"] == ["--json"]


def test_scan_self_diagnose_clean_tree_is_an_empty_list_not_none(env):
    """Ported: test_hook_self_diagnose_due.py::test_run_scanner_clean_tree_is_an_empty_list"""
    fake_producer(env, "self-diagnose.py", "[]")
    assert hook.scan_self_diagnose(hook.Run(False, False, 20.0)) == []


@pytest.mark.parametrize("stdout,exit_code", [
    ("not json", 0), ('{"kind": "x"}', 0), ("", 0), ("[]", 2),
], ids=["unparseable", "non-list", "empty-output", "crash-exit"])
def test_scan_self_diagnose_failed_scan_is_none(env, stdout, exit_code):
    """Ported: test_hook_self_diagnose_due.py::test_run_scanner_unparseable_output_fails_open
    test_hook_self_diagnose_due.py::test_run_scanner_non_list_json_fails_open
    test_hook_self_diagnose_due.py::test_run_scanner_crash_fails_open"""
    fake_producer(env, "self-diagnose.py", stdout, exit_code)
    assert hook.scan_self_diagnose(hook.Run(False, False, 20.0)) is None


def test_scan_self_diagnose_missing_script_is_none(env):
    """Ported: test_hook_self_diagnose_due.py::test_run_scanner_missing_script_fails_open"""
    assert hook.scan_self_diagnose(hook.Run(False, False, 20.0)) is None


def test_scan_self_diagnose_timeout_is_none(env):
    """Ported: test_hook_self_diagnose_due.py::test_run_scanner_timeout_fails_open"""
    fake_sleeper(env.scripts / "self-diagnose.py")
    assert hook.scan_self_diagnose(hook.Run(False, False, 0.5)) is None


def test_a_failed_scan_leaves_the_store_byte_identical(env):
    """Ported: test_hook_self_diagnose_due.py::test_a_failed_scan_leaves_the_store_byte_identical"""
    hook.store.upsert_findings([{"kind": "orphan-leaf", "path": "/mem/a.md", "detail": "d"}])
    store_file = Path(hook.store.store_path())
    before = store_file.read_bytes()
    fake_producer(env, "self-diagnose.py", "not json", 2)
    run_check("check_self_diagnose", "claude-self-diagnose.stamp")
    assert store_file.read_bytes() == before


def test_a_clean_scan_still_resolves_rows_out(env):
    """Ported: test_hook_self_diagnose_due.py::test_a_clean_scan_still_resolves_rows_out"""
    hook.store.upsert_findings([{"kind": "orphan-leaf", "path": "/mem/a.md", "detail": "d"}])
    fake_producer(env, "self-diagnose.py", "[]")
    run_check("check_self_diagnose", "claude-self-diagnose.stamp")
    assert hook.store.load_rows() == []


def test_self_diagnose_persists_the_scan(env, capsys):
    """Ported: test_hook_self_diagnose_due.py::test_main_persists_the_scan"""
    fake_producer(env, "self-diagnose.py", json.dumps([{"kind": "orphan-leaf", "path": "p", "detail": "d"}]), 1)
    run_check("check_self_diagnose", "claude-self-diagnose.stamp", force=True)
    assert [r["kind"] for r in hook.store.load_rows()] == ["orphan-leaf"]
    assert "actionable will re-surface at the turn boundary" in capsys.readouterr().err


def test_self_diagnose_dry_run_reports_without_persisting(env, capsys):
    """Ported: test_hook_self_diagnose_due.py::test_dry_run_reports_without_persisting"""
    fake_producer(env, "self-diagnose.py", json.dumps([{"kind": "orphan-leaf", "path": "p", "detail": "d"}]), 1)
    run_check("check_self_diagnose", "claude-self-diagnose.stamp", dry_run=True)
    assert "1 self-friction item(s)" in capsys.readouterr().err
    assert not Path(hook.store.store_path()).exists()


def test_persist_fails_open_on_a_broken_store(env, monkeypatch):
    """Ported: test_hook_self_diagnose_due.py::test_persist_fails_open_on_a_broken_store"""
    monkeypatch.setattr(hook.store, "upsert_findings", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    assert hook.persist_findings([{"kind": "orphan-leaf", "path": "p", "detail": "d"}]) == []


def test_the_summary_accounts_for_every_open_row(env, capsys):
    """Ported: test_hook_self_diagnose_due.py::test_the_summary_accounts_for_every_open_row"""
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
    hook.store.upsert_findings([{"kind": "orphan-leaf", "path": "/mem/old.md", "detail": "d"}], now=old)
    fake_producer(env, "self-diagnose.py", json.dumps([
        {"kind": "orphan-leaf", "path": "/mem/old.md", "detail": "d"},
        {"kind": "orphan-leaf", "path": "/mem/new.md", "detail": "d"},
        {"kind": "near-duplicate", "path": "/mem/dup.md", "detail": "d"},
    ]), 1)
    run_check("check_self_diagnose", "claude-self-diagnose.stamp", force=True)
    counts = [int(n) for n in re.findall(r"(\d+) (?:actionable|advisory)", capsys.readouterr().err)]
    assert counts == [1, 1, 1]
    assert sum(counts) == len(hook.store.load_rows())


def test_self_diagnose_report_caps_printed_lines(capsys):
    """Ported: test_hook_self_diagnose_due.py::test_report_caps_printed_lines"""
    findings = [{"kind": "near-duplicate", "path": f"path{i}", "detail": "d"} for i in range(hook.MAX_PRINTED_LINES + 3)]
    hook.report_self_diagnose(findings, [])
    err = capsys.readouterr().err
    assert err.count("  - near-duplicate") == hook.MAX_PRINTED_LINES
    assert "... and 3 more" in err


# ── phase3 readiness ─────────────────────────────────────────────────────────

def test_phase3_not_due_verdict_is_silent_but_still_stamped(env, capsys):
    """Ported: test_hook_phase3_due.py::test_main_silent_on_not_due
    test_hook_phase3_due.py::test_main_throttle_stamp_written_regardless_of_verdict"""
    fake_producer(env, "rule-salience-report.py", "phase3-readiness: NOT DUE\n")
    run_check("check_phase3", "claude-phase3-due.stamp")
    assert capsys.readouterr().err == ""
    assert stamp_file(env, "claude-phase3-due.stamp").exists()
    assert env.calls()[0]["argv"] == ["--check-due"]


def test_phase3_missing_checker_fails_open_silently(env, capsys):
    """Ported: test_hook_phase3_due.py::test_main_missing_checker_fails_open_silently
    test_hook_phase3_due.py::test_run_check_missing_script_fails_open"""
    run_check("check_phase3", "claude-phase3-due.stamp")
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("exit_code", [0, 3], ids=["returns-stdout", "crash-exit"])
def test_phase3_verdict_is_taken_from_stdout(env, capsys, exit_code):
    """Ported: test_hook_phase3_due.py::test_run_check_returns_stdout
    test_hook_phase3_due.py::test_run_check_crash_fails_open"""
    fake_producer(env, "rule-salience-report.py", "phase3-readiness: DUE\n", exit_code)
    run_check("check_phase3", "claude-phase3-due.stamp")
    assert "[phase3-due]" in capsys.readouterr().err


def test_phase3_timeout_fails_open(env, capsys):
    """Ported: test_hook_phase3_due.py::test_run_check_timeout_fails_open"""
    fake_sleeper(env.scripts / "rule-salience-report.py")
    run_check("check_phase3", "claude-phase3-due.stamp", budget=0.5)
    assert capsys.readouterr().err == ""


def test_phase3_baseline_is_created_when_absent(env):
    """Ported: test_hook_phase3_due.py::test_ensure_baseline_creates_when_absent
    test_hook_phase3_due.py::test_main_establishes_baseline_on_first_run"""
    run_check("check_phase3", "claude-phase3-due.stamp")
    written = dt.datetime.fromisoformat(stamp_file(env, hook.PHASE3_BASELINE_STAMP).read_text())
    assert written.tzinfo is not None
    assert abs(written.timestamp() - time.time()) < 60


def test_phase3_baseline_is_never_overwritten(env):
    """Ported: test_hook_phase3_due.py::test_ensure_baseline_never_overwrites"""
    hook.ensure_phase3_baseline(1000.0)
    first = stamp_file(env, hook.PHASE3_BASELINE_STAMP).read_text()
    hook.ensure_phase3_baseline(999999.0)
    assert stamp_file(env, hook.PHASE3_BASELINE_STAMP).read_text() == first


def test_phase3_force_run_still_establishes_the_baseline(env):
    """Ported: test_hook_phase3_due.py::test_main_force_run_still_establishes_baseline"""
    run_check("check_phase3", "claude-phase3-due.stamp", force=True)
    assert stamp_file(env, hook.PHASE3_BASELINE_STAMP).exists()
    assert not stamp_file(env, "claude-phase3-due.stamp").exists()


def test_phase3_verdict_report_caps_printed_lines(env, capsys):
    """Ported: test_hook_phase3_due.py::test_report_caps_printed_lines"""
    lines = ["phase3-readiness: DUE"] + [f"detail {i}" for i in range(hook.MAX_PRINTED_LINES + 3)]
    fake_producer(env, "rule-salience-report.py", "\n".join(lines))
    run_check("check_phase3", "claude-phase3-due.stamp")
    err = capsys.readouterr().err
    assert err.count("\n  phase3-readiness") + err.count("\n  detail ") == hook.MAX_PRINTED_LINES
    assert "(4 more line(s)" in err


def test_phase3_baseline_stamp_matches_the_predicates_reader(env):
    """Ported: test_hook_phase3_due.py::test_baseline_stamp_matches_the_predicates_reader"""
    spec = importlib.util.spec_from_file_location(
        "rule_salience_report_coupling_check", REAL_SCRIPTS / "rule-salience-report.py")
    rsr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rsr)
    assert hook.state_path(hook.PHASE3_BASELINE_STAMP) == rsr.BASELINE_STAMP_PATH


# ── promote-scan ─────────────────────────────────────────────────────────────

def test_promote_scan_runs_cache_only(env, monkeypatch):
    """Ported: test_hook_promote_scan_due.py::test_run_scanner_returns_clusters
    test_hook_promote_scan_due.py::test_hook_runs_cache_only"""
    fake_producer(env, "record-experience.py", json.dumps({"clusters": []}))
    monkeypatch.setenv(hook.JUDGE_BUDGET_ENV, "300")
    assert hook.scan_promote(hook.Run(False, False, 20.0)) == {"clusters": []}
    call = env.calls()[0]
    assert call["argv"] == ["promote-scan", "--scope", "global", "--json"]
    assert call["budget"] == "0"


@pytest.mark.parametrize("stdout,exit_code", [
    (json.dumps({"clusters": []}), 2),
    ("not json", 0),
    (json.dumps([1]), 0),
    (json.dumps({"stats": {}}), 0),
    (json.dumps({"clusters": "x"}), 0),
], ids=["nonzero-exit", "bad-json", "non-object", "no-clusters", "clusters-not-list"])
def test_promote_scan_failure_shapes_are_none(env, stdout, exit_code):
    """Ported: test_hook_promote_scan_due.py::test_run_scanner_nonzero_exit_fails_open
    test_hook_promote_scan_due.py::test_run_scanner_crash_fails_open
    test_hook_promote_scan_due.py::test_run_scanner_bad_json_fails_open
    test_hook_promote_scan_due.py::test_run_scanner_non_object_json_fails_open
    test_hook_promote_scan_due.py::test_run_scanner_object_without_clusters_list_fails_open"""
    fake_producer(env, "record-experience.py", stdout, exit_code)
    assert hook.scan_promote(hook.Run(False, False, 20.0)) is None


def test_promote_scan_missing_script_is_none(env):
    """Ported: test_hook_promote_scan_due.py::test_run_scanner_missing_script_fails_open"""
    assert hook.scan_promote(hook.Run(False, False, 20.0)) is None


def test_promote_scan_timeout_is_none(env):
    """Ported: test_hook_promote_scan_due.py::test_run_scanner_timeout_fails_open"""
    fake_sleeper(env.scripts / "record-experience.py")
    assert hook.scan_promote(hook.Run(False, False, 0.5)) is None


def test_promote_scan_prints_unjudged_count_and_flagged_members(env, capsys):
    """Ported: test_hook_promote_scan_due.py::test_hook_prints_unjudged_count"""
    fake_producer(env, "record-experience.py",
                  json.dumps({"clusters": [_FLAGGED_CLUSTER], "stats": {"unjudged_items": 4}}))
    run_check("check_promote_scan", "claude-promote-scan.stamp", dry_run=True)
    err = capsys.readouterr().err
    assert "4 experience leaf(s) unjudged" in err and "2026-08-11-loop.md" in err


def test_promote_scan_unflagged_only_prints_nothing(env, capsys):
    """Ported: test_hook_promote_scan_due.py::test_main_unflagged_only_result_prints_nothing"""
    fake_producer(env, "record-experience.py", json.dumps({"clusters": [_UNFLAGGED_CLUSTER], "stats": {}}))
    run_check("check_promote_scan", "claude-promote-scan.stamp")
    assert capsys.readouterr().err == ""


def test_promote_scan_failure_is_silent_success_and_still_stamped(env, capsys):
    """Ported: test_hook_promote_scan_due.py::test_main_scanner_failure_is_silent_success"""
    fake_producer(env, "record-experience.py", "not json")
    run_check("check_promote_scan", "claude-promote-scan.stamp")
    assert capsys.readouterr().err == ""
    assert stamp_file(env, "claude-promote-scan.stamp").exists()


def test_promote_scan_report_caps_printed_clusters(capsys):
    """Ported: test_hook_promote_scan_due.py::test_report_caps_printed_clusters"""
    clusters = [dict(_FLAGGED_CLUSTER, members=[f"m{i}.md"]) for i in range(hook.MAX_PRINTED_LINES + 2)]
    hook.report_promote({"clusters": clusters, "stats": {}})
    err = capsys.readouterr().err
    assert err.count("occurrence(s)]") == hook.MAX_PRINTED_LINES
    assert "... and 2 more" in err


# ── policy-scorecard ─────────────────────────────────────────────────────────

SCORECARD_STAMP = "claude-policy-scorecard.stamp"


def test_scorecard_launches_exactly_once_per_due_firing(env):
    """Ported: test_hook_policy_scorecard_due.py::test_stamp_written_exactly_once_per_due_firing"""
    run_check("check_policy_scorecard", SCORECARD_STAMP)
    run_check("check_policy_scorecard", SCORECARD_STAMP)
    assert len(env.launches) == 1


def test_scorecard_launcher_raising_still_stamps_and_exits_zero(env, monkeypatch):
    """Ported: test_hook_policy_scorecard_due.py::test_launcher_raising_still_exits_zero_and_still_stamps"""
    monkeypatch.setattr(hook, "launch_scorecard", lambda cmd: (_ for _ in ()).throw(OSError("nope")))
    assert hook.main([]) == 0
    assert stamp_file(env, SCORECARD_STAMP).exists()


def test_scorecard_message_says_first_refresh_then_age(env, capsys):
    run_check("check_policy_scorecard", SCORECARD_STAMP)
    assert "(first refresh)" in capsys.readouterr().err
    seed_stamp(env, SCORECARD_STAMP, 10 * 86400)
    run_check("check_policy_scorecard", SCORECARD_STAMP)
    assert "last refreshed 10d ago" in capsys.readouterr().err


def test_scorecard_ledger_env_override_is_passed_as_a_flag(env, monkeypatch):
    """Ported: test_hook_policy_scorecard_due.py::test_ledger_env_override_passed_through_as_flag"""
    monkeypatch.setenv("CLAUDE_POLICY_LEDGER", "/some/ledger.jsonl")
    run_check("check_policy_scorecard", SCORECARD_STAMP)
    cmd = env.launches[0]
    assert cmd[cmd.index("--ledger") + 1] == "/some/ledger.jsonl"
    assert "--no-judge" in cmd


def test_scorecard_without_the_override_omits_the_flag(env):
    """Ported: test_hook_policy_scorecard_due.py::test_no_ledger_env_override_omits_flag"""
    run_check("check_policy_scorecard", SCORECARD_STAMP)
    assert "--ledger" not in env.launches[0]


def test_scorecard_upsert_cmd_base_shape(env):
    """Ported: test_hook_policy_scorecard_due.py::test_ledger_upsert_cmd_base_shape
    test_hook_policy_scorecard_due.py::test_ledger_upsert_cmd_passes_no_judge"""
    assert hook.scorecard_upsert_cmd() == [
        sys.executable, str(env.scripts / "policy-scorecard.py"),
        "--ledger-only", "--days", "7", "--no-judge",
    ]


def test_scorecard_default_launch_is_detached_and_supervised(monkeypatch):
    """Ported: test_hook_policy_scorecard_due.py::test_default_launch_uses_detached_supervised_popen"""
    import proc_tree

    captured = {}

    def fake_launch_supervised(cmd, **kwargs):
        captured["cmd"], captured["kwargs"] = cmd, kwargs
        return object()

    monkeypatch.setattr(proc_tree, "launch_supervised", fake_launch_supervised)
    REAL_LAUNCH_SCORECARD([sys.executable, "-c", "pass"])
    assert captured["cmd"] == [sys.executable, "-c", "pass"]
    assert captured["kwargs"].get("stdout") is not None and captured["kwargs"].get("stderr") is not None


def test_scorecard_cli_honours_the_ledger_flag(tmp_path):
    """Ported: test_hook_policy_scorecard_due.py::test_scorecard_cli_honours_the_ledger_flag"""
    home = tmp_path / "cli-home"
    (home / "projects").mkdir(parents=True)
    override = tmp_path / "override.jsonl"
    fallback = home / ".local" / "log" / "claude-policy-ledger.jsonl"
    proc = subprocess.run(
        [sys.executable, str(REAL_SCRIPTS / "policy-scorecard.py"), "--ledger-only", "--days", "7",
         "--ledger", str(override), "--no-judge"],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "CLAUDE_AGENT_HOME": str(home)},
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert override.exists() and not fallback.exists()


# ── improvement-scan (counts stored findings only) ───────────────────────────

def test_improvement_scan_counts_only_its_own_source(env):
    _setup_improvement_scan(env)
    hook.store.upsert_findings([{"kind": "near-duplicate", "path": "a.md", "detail": "d"}])
    assert len(hook.open_improvement_findings()) == 1


def test_improvement_scan_without_findings_is_silent_but_stamped(env, capsys):
    """Ported: test_improvement_scan_hook.py::test_report_empty_findings_prints_nothing"""
    run_check("check_improvement_scan", "claude-improvement-scan.stamp")
    assert capsys.readouterr().err == ""
    assert stamp_file(env, "claude-improvement-scan.stamp").exists()


def test_improvement_scan_findings_none_when_the_store_module_is_missing(env, monkeypatch):
    """Ported: test_improvement_scan_hook.py::test_open_findings_returns_none_when_store_module_missing"""
    monkeypatch.setattr(hook, "store", None)
    assert hook.open_improvement_findings() is None


def test_improvement_scan_findings_none_on_a_broken_store(env, monkeypatch):
    """Ported: test_improvement_scan_hook.py::test_open_findings_returns_none_on_broken_store"""
    monkeypatch.setattr(hook.store, "load_rows", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    assert hook.open_improvement_findings() is None


def test_main_fails_open_when_the_store_module_is_missing(env, monkeypatch):
    """Ported: test_improvement_scan_hook.py::test_main_fails_open_when_store_missing"""
    monkeypatch.setattr(hook, "store", None)
    assert hook.main([]) == 0


def test_hook_never_mentions_the_scan_cli_by_filename():
    """Ported: test_improvement_scan_hook.py::test_hook_never_mentions_the_scan_cli_by_filename"""
    assert "improvement-scan.py" not in Path(hook.__file__).read_text(encoding="utf-8")


# ── budget calibration, sigma sentinel, judge-ceiling drift ─────────────────

def test_budget_calibration_runs_check_mode_and_quotes_the_flag(env, capsys):
    _setup_budget(env)
    run_check("check_budget_calibration", "claude-budget-calibration.stamp")
    assert env.calls()[0]["argv"] == ["--check"]
    assert "Budget tiers may be miscalibrated: tier small off." in capsys.readouterr().err


def test_budget_calibration_silent_when_no_flag(env, capsys):
    fake_producer(env, "budget-calibration.py", "")
    run_check("check_budget_calibration", "claude-budget-calibration.stamp")
    assert capsys.readouterr().err == ""
    assert stamp_file(env, "claude-budget-calibration.stamp").exists()


def test_sigma_sentinel_message_carries_the_age(env, capsys):
    run_check("check_sigma_sentinel", "claude-sigma-sentinel.stamp")
    assert "(first reminder)" in capsys.readouterr().err
    seed_stamp(env, "claude-sigma-sentinel.stamp", 31 * 86400)
    run_check("check_sigma_sentinel", "claude-sigma-sentinel.stamp")
    assert "last checked 31d ago" in capsys.readouterr().err


def test_judge_drift_speaks_only_on_a_nonzero_exit_with_output(env, capsys):
    fake_producer(env, "judge-usage-report.py", "all within ceiling\n", exit_code=0)
    run_check("check_judge_ceiling_drift", "claude-judge-ceiling-drift.stamp")
    assert capsys.readouterr().err == ""
    _setup_judge(env)
    run_check("check_judge_ceiling_drift", "claude-judge-ceiling-drift.stamp", force=True)
    err = capsys.readouterr().err
    assert "ceiling 30s live 29s" in err
    assert env.calls()[-1]["argv"] == ["--check-drift"]
