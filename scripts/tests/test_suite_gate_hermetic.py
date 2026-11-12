"""The gate runs pytest in the hermetic runner, says when it did not, and hermetic-run judges the result.

The gate's process edge is a stub (no pytest, no namespace); the real hiding is tested by
test_hermetic_run.py.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import gate_speedup_control as control
from lib import config_root
from lib import suite_gate as gate

SHA = "a" * 40
GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@localhost",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@localhost",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


@pytest.fixture(autouse=True)
def _private_slots(tmp_path_factory, monkeypatch):
    """The runner's worker slots come from a directory of this test, never the machine's or an enclosing run's."""
    from lib import test_slots

    monkeypatch.setenv(test_slots.DIR_ENV, str(tmp_path_factory.mktemp("slots")))
    monkeypatch.delenv(test_slots.HELD_ENV, raising=False)
    monkeypatch.delenv(gate.XDIST_WORKERS_ENV, raising=False)


@pytest.fixture
def source(tmp_path):
    repo = tmp_path / "source"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=GIT_ENV)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "i"], check=True, env=GIT_ENV)
    return repo


@pytest.fixture
def sandbox(tmp_path, source):
    """A sandbox root whose core is a --shared clone of ``source``, as instruction-sandbox.sh builds."""
    root = tmp_path / "root"
    root.mkdir()
    subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(source / ".git"), str(root / "core")],
                   check=True, env=GIT_ENV)
    (root / "sandbox.env").write_text("ISB_CORE_SHA=x\n", encoding="utf-8")
    return root


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake(argv, *, cwd, env, timeout_s, grace_s=gate.KILL_GRACE_S, pass_fds=()):
        seen.append(list(argv))
        Path(env[gate.FAILURES_ENV]).write_text("", encoding="utf-8")
        return 0, "ok\n", False

    monkeypatch.setattr(gate, "run_group", fake)
    return seen


def alternates(root: Path) -> str:
    return (root / gate.ALTERNATES).read_text(encoding="utf-8").strip()


def test_pytest_runs_behind_cap_run_then_the_hermetic_runner(source, sandbox, calls, monkeypatch):
    monkeypatch.setattr(gate, "hermetic_unavailable", lambda: "")
    run = gate.default_runner(source, SHA, sandbox, select=(), timeout_s=50)
    (argv,) = calls
    assert Path(argv[0]).name == "cap-run.sh"
    runner_at = argv.index(str(gate.HERMETIC_SCRIPT))
    assert argv[runner_at - 1] == gate.sys.executable and argv[runner_at + 1 :].index("--") >= 0
    assert argv.index("pytest") > runner_at
    assert run.hermetic and run.hermetic_reason == ""
    assert run.pytest_argv == tuple(argv)


def test_the_source_work_tree_git_directory_checkout_and_agent_homes_are_hidden(source, sandbox, calls, monkeypatch):
    monkeypatch.setattr(gate, "hermetic_unavailable", lambda: "")
    monkeypatch.setenv("CLAUDE_AGENT_HOME", "/agent/home")
    gate.default_runner(source, SHA, sandbox, select=(), timeout_s=50)
    (argv,) = calls
    hidden = [argv[i + 1] for i, word in enumerate(argv) if word == "--hide"]
    home = os.path.expanduser("~")
    assert os.path.realpath(source) in hidden and str(source / ".git") in hidden
    assert "/agent/home" in hidden
    assert os.path.join(home, ".claude-agent") in hidden and str(config_root.harness_config_root()) in hidden
    assert str(Path(home) / ".local") not in hidden, "the user site holds pytest and xdist"


def test_both_literal_agent_homes_are_hidden_when_the_launcher_points_the_config_root_at_one(
        source, monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude-agent"))
    monkeypatch.delenv("CLAUDE_AGENT_HOME", raising=False)
    hidden = gate.hidden_paths(source)
    assert str(home / ".claude") in hidden and str(home / ".claude-agent") in hidden


def test_the_clone_reads_the_sources_objects_through_the_rebinding_only(source, sandbox, calls, monkeypatch):
    monkeypatch.setattr(gate, "hermetic_unavailable", lambda: "")
    gate.default_runner(source, SHA, sandbox, select=(), timeout_s=50)
    (argv,) = calls
    mount = sandbox / gate.SOURCE_OBJECTS_DIR
    assert argv[argv.index("--objects") + 1] == f"{source / '.git' / 'objects'}:{mount}"
    assert mount.is_dir() and alternates(sandbox) == str(mount)


def test_a_run_without_namespaces_goes_on_unhidden_and_says_why(source, sandbox, calls, monkeypatch):
    monkeypatch.setattr(gate, "hermetic_unavailable", lambda: "user namespaces unavailable: EPERM")
    run = gate.default_runner(source, SHA, sandbox, select=(), timeout_s=50)
    (argv,) = calls
    assert str(gate.HERMETIC_SCRIPT) not in argv
    assert not run.hermetic and run.hermetic_reason == "user namespaces unavailable: EPERM"
    assert alternates(sandbox) == str(source / ".git" / "objects")
    assert gate.cost_fields(run)["hermetic"] is False and "EPERM" in gate.cost_fields(run)["hermetic_reason"]


def test_a_run_without_namespaces_says_NOT_HERMETIC_on_stderr(source, sandbox, calls, monkeypatch, capsys):
    monkeypatch.setattr(gate, "hermetic_unavailable", lambda: "user namespaces unavailable: EPERM")
    gate.default_runner(source, SHA, sandbox, select=(), timeout_s=50)
    assert "NOT HERMETIC: user namespaces unavailable: EPERM" in capsys.readouterr().err


def test_a_hermetic_run_prints_no_notice(source, sandbox, calls, monkeypatch, capsys):
    monkeypatch.setattr(gate, "hermetic_unavailable", lambda: "")
    gate.default_runner(source, SHA, sandbox, select=(), timeout_s=50)
    assert "NOT HERMETIC" not in capsys.readouterr().err


def test_the_core_sha_is_read_before_the_alternates_leave_the_real_objects(source, sandbox, calls, monkeypatch):
    monkeypatch.setattr(gate, "hermetic_unavailable", lambda: "")
    head = subprocess.run(["git", "-C", str(sandbox / "core"), "rev-parse", "HEAD"], capture_output=True, text=True,
                          env=GIT_ENV, check=True).stdout.strip()
    seen = []
    real_point = gate.point_alternates

    def point(root, objects_dir):
        seen.append(subprocess.run(["git", "-C", str(root / "core"), "cat-file", "-t", "HEAD"], capture_output=True,
                                   text=True, env=GIT_ENV).stdout.strip())
        real_point(root, objects_dir)

    monkeypatch.setattr(gate, "point_alternates", point)
    run = gate.default_runner(source, SHA, sandbox, select=(), timeout_s=50)
    assert run.sandbox_core_sha == head and seen == ["commit"]


def _record(**changes):
    base = {"status": gate.PASS, "new_failures": [], "unavailable_reason": "", "hermetic": True,
            "hermetic_reason": ""}
    return {**base, **changes}


def test_a_record_of_a_run_that_was_not_hermetic_shows_it_in_the_decision_line(tmp_path):
    decision = gate._record_decision(True, gate.PASS, tmp_path / "r.json",
                                     _record(hermetic=False, hermetic_reason="user namespaces unavailable: EPERM"))
    assert decision.admitted
    assert decision.line == "SUITE: ADMITTED (PASS) [NOT HERMETIC: user namespaces unavailable: EPERM]"


def test_a_hermetic_record_and_one_without_the_field_add_nothing_to_the_line(tmp_path):
    hermetic_line = gate._record_decision(True, gate.PASS, tmp_path / "r.json", _record()).line
    legacy = _record()
    del legacy["hermetic"]
    assert hermetic_line == gate._record_decision(True, gate.PASS, tmp_path / "r.json", legacy).line
    assert "NOT HERMETIC" not in hermetic_line


def test_check_shows_the_notice_for_a_stored_record(source, tmp_path, monkeypatch):
    sha = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True,
                         env=GIT_ENV, check=True).stdout.strip()
    path = tmp_path / "record.json"
    path.write_text(json.dumps(_record(hermetic=False, hermetic_reason="no namespaces")), encoding="utf-8")
    monkeypatch.setattr(gate, "record_admits", lambda record, candidate, tip: (True, gate.PASS))
    monkeypatch.setattr(gate.shared, "read_record", lambda p: json.loads(Path(p).read_text(encoding="utf-8")))
    monkeypatch.setattr(gate, "_scope_decision", lambda repo, tip, candidate: None)
    line = gate.landing_decision(source, sha, sha, common_dir=tmp_path, record_file=path).line
    assert line == "SUITE: ADMITTED (PASS) [NOT HERMETIC: no namespaces]"


def test_a_runner_failure_after_the_check_is_named_not_taken_for_pytests_interrupt():
    run = gate.SuiteRun(125, (), "c" * 40, hermetic=True, log_tail="hermetic_run: mount none -> /x: EPERM\n")
    failed, reason = gate.classify(run, "c" * 40)
    assert failed == () and "hermetic runner failed before pytest ran" in reason and "EPERM" in reason
    plain = gate.SuiteRun(2, (), "c" * 40, hermetic=True, log_tail="interrupted\n")
    assert "pytest exited 2" in gate.classify(plain, "c" * 40)[1]


def test_hiding_switched_off_is_recorded_as_such(source, sandbox, calls, monkeypatch):
    monkeypatch.setattr(gate, "hermetic_unavailable", lambda: "")
    run = gate.default_runner(source, SHA, sandbox, select=(), timeout_s=50, hide=False)
    assert not run.hermetic and "switched off" in run.hermetic_reason
    assert str(gate.HERMETIC_SCRIPT) not in calls[0]


def test_a_sandbox_reused_without_hiding_reads_the_real_objects_again(source, sandbox, calls, monkeypatch):
    monkeypatch.setattr(gate, "hermetic_unavailable", lambda: "")
    gate.default_runner(source, SHA, sandbox, select=(), timeout_s=50)
    gate.default_runner(source, SHA, sandbox, select=(), timeout_s=50, hide=False)
    assert alternates(sandbox) == str(source / ".git" / "objects")


def test_a_hermetic_run_records_it_in_the_run_record_fields(source, sandbox, calls, monkeypatch):
    monkeypatch.setattr(gate, "hermetic_unavailable", lambda: "")
    run = gate.default_runner(source, SHA, sandbox, select=(), timeout_s=50)
    assert gate.cost_fields(run)["hermetic"] is True


# ── hermetic-run: exit contract ───────────────────────────────────────────

FLAKY = "scripts/tests/test_x.py::test_flaky"


def _named(tmp_path, *ids):
    path = tmp_path / "named.md"
    path.write_text("".join(f"- `{i}`\n" for i in ids), encoding="utf-8")
    return path


def _run(monkeypatch, tmp_path, report, *, code=0, named=(FLAKY,), **kwargs):
    """hermetic-run's exit with sandbox_run stubbed to write ``report`` (or to exit ``code``)."""
    seen = {}

    def fake(repo, out, select, root, timeout_s, reruns=0, hide=True, probe=False):
        seen.update(select=select, hide=hide, probe=probe)
        if report is not None:
            out.write_text(json.dumps(report), encoding="utf-8")
        return code

    monkeypatch.setattr(control, "sandbox_run", fake)
    args = dict(repo=tmp_path, out=tmp_path / "out.json", named=_named(tmp_path, *named), max_named=20, root=None,
                timeout_s=60, reruns=2, no_hide=False, probe_only=False)
    args.update(kwargs)
    return control.hermetic_run(**args), seen


def _report(**changes):
    base = {"collected": 16000, "collection_errors": [], "failing": [], "hermetic": True,
            "hermetic_reason": "", "probe_collected": True, "probe_passed": True}
    return {**base, **changes}


def test_a_hermetic_run_with_the_probe_passed_and_named_failures_exits_0(monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path, _report(failing=[FLAKY]))[0] == 0


def test_a_run_that_was_not_hermetic_exits_1(monkeypatch, tmp_path):
    code, _ = _run(monkeypatch, tmp_path, _report(hermetic=False, hermetic_reason="no namespaces"))
    assert code == 1


def test_a_failed_probe_exits_1(monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path, _report(probe_passed=False))[0] == 1


def test_an_unnamed_failure_exits_1(monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path, _report(failing=["scripts/tests/test_y.py::test_other"]))[0] == 1


def test_a_document_over_the_bound_exits_1(monkeypatch, tmp_path):
    named = [f"scripts/tests/test_x.py::t{i}" for i in range(21)]
    assert _run(monkeypatch, tmp_path, _report(), named=named)[0] == 1


def test_an_unfinished_run_exits_1(monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path, _report(collected=10))[0] == 1


def test_a_run_that_could_not_be_made_exits_2(monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path, None, code=2)[0] == 2


def test_a_report_without_the_hermetic_fields_exits_2(monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path, {"collected": 16000, "failing": []})[0] == 2


def test_the_probe_is_always_planted_and_the_run_hides_unless_told_not_to(monkeypatch, tmp_path):
    _, seen = _run(monkeypatch, tmp_path, _report())
    assert seen == {"select": [], "hide": True, "probe": True}
    _, seen = _run(monkeypatch, tmp_path, _report(hermetic=False), no_hide=True)
    assert seen["hide"] is False and seen["probe"] is True


def test_probe_only_collects_the_probe_and_needs_no_full_run(monkeypatch, tmp_path):
    code, seen = _run(monkeypatch, tmp_path, _report(collected=1), probe_only=True)
    assert code == 0 and seen["select"] == [control.PROBE_FILE]


def test_no_hide_exits_1_when_the_probe_failed_on_the_visible_tree(monkeypatch, tmp_path):
    code, _ = _run(monkeypatch, tmp_path, _report(collected=1, hermetic=False, probe_passed=False),
                   probe_only=True, no_hide=True)
    assert code == 1


def test_no_hide_exits_2_when_the_probe_passed_so_the_control_did_not_fire(monkeypatch, tmp_path):
    code, _ = _run(monkeypatch, tmp_path, _report(collected=1, hermetic=False, probe_passed=True),
                   probe_only=True, no_hide=True)
    assert code == 2


def test_no_hide_exits_2_when_pytest_never_collected_the_probe(monkeypatch, tmp_path):
    report = _report(collected=0, hermetic=False, probe_collected=False, probe_passed=False)
    assert _run(monkeypatch, tmp_path, report, probe_only=True, no_hide=True)[0] == 2


def test_a_hidden_run_that_never_collected_the_probe_exits_2(monkeypatch, tmp_path):
    report = _report(collected=0, probe_collected=False, probe_passed=False)
    assert _run(monkeypatch, tmp_path, report, probe_only=True)[0] == 2


def test_a_hermetic_failed_probe_without_no_hide_still_exits_1(monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path, _report(probe_passed=False), no_hide=False)[0] == 1


def test_the_planted_probe_passes_on_an_empty_tree_and_fails_on_a_visible_one(tmp_path):
    empty, visible = tmp_path / "empty", tmp_path / "visible"
    empty.mkdir()
    visible.mkdir()
    (visible / "file").write_text("x")
    for tree, passes in ((empty, True), (visible, False)):
        namespace: dict = {}
        exec(compile(control._probe_source(tree), "probe", "exec"), namespace)
        if passes:
            namespace["test_the_source_work_tree_is_hidden"]()
        else:
            with pytest.raises(AssertionError):
                namespace["test_the_source_work_tree_is_hidden"]()
