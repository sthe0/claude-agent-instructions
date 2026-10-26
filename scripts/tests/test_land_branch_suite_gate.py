"""land-branch.py and githooks/pre-push around the pre-land suite gate.

Hermetic: every repository is a throwaway clone of a throwaway bare remote under tmp_path (the
suite-gate `world` fixture), the suite runner is always a stub, and the smoke runner is a counter
that fails the test when launched. The only real child processes are `git push` runs that execute
the repository's own hook.
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from lib import instruction_smoke_gate as smoke_gate
from lib import suite_gate as gate
from test_instruction_smoke_gate import make_record as make_smoke_record
from test_instruction_smoke_gate import stub_runner
from test_land_branch_smoke_gate import Counting
from test_suite_gate import (  # noqa: F401 — `world` and `world_no_gate` are fixtures
    FAILED_A, FakeRunner, commit, git, make_record, world, world_no_gate,
)

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
LAND_BRANCH = SCRIPTS_DIR / "land-branch.py"
HOOK_PATH = SCRIPTS_DIR.parent / "githooks" / "pre-push"

CODE = {"scripts/foo.py": "x = 1\n"}
SURFACE_AND_CODE = {"CLAUDE.md": "new rules\n", "scripts/instruction-sandbox.sh": "#!/bin/sh\n", **CODE}


def _load():
    spec = importlib.util.spec_from_file_location("land_branch_suite_gate_subject", LAND_BRANCH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


land_branch = _load()


@pytest.fixture(autouse=True)
def sandboxes_under_tmp(tmp_path, monkeypatch):
    parent = tmp_path / "suite-sandboxes"
    parent.mkdir()
    monkeypatch.setattr(gate, "SANDBOX_PARENT", parent)


def feature(world, files, name="feature"):
    git(world.work, "checkout", "-q", "-b", name)
    return commit(world.work, files, "feature work")


def land(world, capsys, *extra, runner=None, suite_runner=None):
    code = land_branch.main(["-C", str(world.work), "--keep-branch", *extra],
                            runner=runner, suite_runner=suite_runner)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def check(world, capsys, suite_runner=None):
    code = land_branch.main(["-C", str(world.work), "--check"], suite_runner=suite_runner)
    return code, capsys.readouterr().out


def lines_with(out, prefix):
    return [line for line in out.splitlines() if line.startswith(prefix)]


def remote_main(world):
    return git(world.remote, "rev-parse", "main")


def unavailable_run(sha):
    return gate.SuiteRun(None, (), sha, timed_out=True, log_tail="timed out\n")


# ── the gate's place in a landing ─────────────────────────────────────────

def test_a_code_diff_runs_the_suite_and_lands_on_pass(world, capsys):
    sha = feature(world, CODE)
    runner = FakeRunner({sha: ()})
    code, out, err = land(world, capsys, suite_runner=runner)
    assert code == 0, err
    assert runner.shas() == [sha]
    assert lines_with(out, "SUITE: ") == ["SUITE: ADMITTED (PASS)"]
    assert remote_main(world) == sha


def test_the_suite_line_comes_before_the_push_report(world, capsys):
    sha = feature(world, CODE)
    _, out, _ = land(world, capsys, suite_runner=FakeRunner({sha: ()}))
    lines = out.splitlines()
    suite_at = next(i for i, line in enumerate(lines) if line.startswith("SUITE: "))
    pushed_at = next(i for i, line in enumerate(lines) if line.startswith("[land-branch] pushed "))
    assert suite_at < pushed_at


def test_a_new_failure_refuses_names_it_and_pushes_nothing(world, capsys):
    sha = feature(world, CODE)
    runner = FakeRunner({sha: (FAILED_A,), world.base: ()})
    code, out, err = land(world, capsys, suite_runner=runner)
    assert code == 2
    assert "NOT-LANDABLE" in err and FAILED_A in err
    assert lines_with(out, "SUITE: ") == []
    assert remote_main(world) == world.base


def test_a_failure_the_tip_also_has_does_not_refuse(world, capsys):
    sha = feature(world, CODE)
    runner = FakeRunner({sha: (FAILED_A,), world.base: (FAILED_A,)})
    code, out, _ = land(world, capsys, suite_runner=runner)
    assert code == 0 and lines_with(out, "SUITE: ")[0].startswith("SUITE: ADMITTED")
    assert remote_main(world) == sha


def test_a_refused_suite_stops_before_the_smoke_launch(world, capsys):
    sha = feature(world, SURFACE_AND_CODE)
    suite_runner = FakeRunner({sha: (FAILED_A,), world.base: ()})
    smoke_runner = Counting()
    code, out, err = land(world, capsys, runner=smoke_runner, suite_runner=suite_runner)
    assert code == 2 and smoke_runner.calls == 0
    assert "NOT-LANDABLE" in err and lines_with(out, "SMOKE: ") == []
    assert remote_main(world) == world.base


def test_the_suite_line_precedes_the_smoke_line_and_each_gate_has_its_own_runner(world, capsys):
    sha = feature(world, SURFACE_AND_CODE)
    suite_runner = FakeRunner({sha: ()})
    smoke_runner = Counting(stub_runner())
    code, out, _ = land(world, capsys, runner=smoke_runner, suite_runner=suite_runner)
    assert code == 0 and smoke_runner.calls == 1 and suite_runner.shas() == [sha]
    lines = out.splitlines()
    suite_at = next(i for i, line in enumerate(lines) if line.startswith("SUITE: "))
    smoke_at = next(i for i, line in enumerate(lines) if line.startswith("SMOKE: "))
    pushed_at = next(i for i, line in enumerate(lines) if line.startswith("[land-branch] pushed "))
    assert suite_at < smoke_at < pushed_at


def test_a_docs_only_diff_is_exempt_and_runs_nothing(world, capsys):
    sha = feature(world, {"docs/new.md": "doc\n", "memory-global/n.md": "mem\n"})
    runner = FakeRunner({})
    code, out, _ = land(world, capsys, suite_runner=runner)
    assert code == 0 and runner.calls == []
    assert lines_with(out, "SUITE: ") == ["SUITE: exempt (memory/docs-only): docs/new.md, memory-global/n.md"]
    assert remote_main(world) == sha


def test_a_repository_without_the_suite_gate_lands_without_a_suite_run(world_no_gate, capsys):
    sha = feature(world_no_gate, CODE)
    runner = FakeRunner({})
    code, out, _ = land(world_no_gate, capsys, suite_runner=runner)
    assert code == 0 and runner.calls == []
    assert lines_with(out, "SUITE: ") == ["SUITE: not required (repository has no suite gate)"]
    assert remote_main(world_no_gate) == sha


# ── tip, ancestry, stored records ─────────────────────────────────────────

def test_suite_gate_gets_the_fetched_remote_tip(world, capsys):
    tip = world.advance_remote()
    git(world.work, "checkout", "-q", "-b", "feature", "origin/main")
    sha = commit(world.work, CODE, "feature work")
    code, _, _ = land(world, capsys, suite_runner=FakeRunner({sha: ()}))
    assert code == 0
    assert world.stored(sha)["base_sha"] == tip != world.base
    assert remote_main(world) == sha


def test_a_remote_tip_the_branch_lacks_is_refused_before_any_suite_run(world, capsys):
    tip = world.advance_remote()
    feature(world, CODE)
    runner = FakeRunner({})
    code, out, err = land(world, capsys, suite_runner=runner)
    assert code == 2 and runner.calls == []
    assert "NOT-LANDABLE" in err and "is not an ancestor of 'feature'" in err and "suite gate" not in err
    assert lines_with(out, "SUITE: ") == []
    assert remote_main(world) == tip


def test_a_stored_admitting_record_is_reused_without_a_run(world, capsys):
    sha = feature(world, CODE)
    world.store(make_record(sha, world.base))
    runner = FakeRunner({})
    code, out, _ = land(world, capsys, suite_runner=runner)
    assert code == 0 and runner.calls == []
    assert lines_with(out, "SUITE: ") == ["SUITE: ADMITTED (PASS)"]
    assert remote_main(world) == sha


def test_the_suite_timeout_reaches_the_runner(world, capsys):
    sha = feature(world, CODE)
    runner = FakeRunner({sha: ()})
    land(world, capsys, "--suite-timeout", "77", suite_runner=runner)
    assert [call.timeout_s for call in runner.calls] == [77]


def test_without_a_suite_timeout_the_configured_limit_applies(world, capsys):
    sha = feature(world, CODE)
    runner = FakeRunner({sha: ()})
    land(world, capsys, suite_runner=runner)
    assert [call.timeout_s for call in runner.calls] == [gate.configured_timeout_s()]


# ── waiver and error handling ─────────────────────────────────────────────

def test_an_unavailable_suite_needs_a_waiver_to_land(world, capsys):
    sha = feature(world, CODE)
    runner = FakeRunner({sha: unavailable_run(sha)})
    code, out, err = land(world, capsys, suite_runner=runner)
    assert code == 2 and "NOT-LANDABLE" in err and "UNAVAILABLE" in err
    assert lines_with(out, "SUITE: ") == [] and remote_main(world) == world.base

    code, out, _ = land(world, capsys, "--suite-waiver", "runner down", suite_runner=runner)
    assert code == 0
    assert lines_with(out, "SUITE: ")[0].startswith("SUITE: ADMITTED (UNAVAILABLE, waived: runner down")
    assert world.stored(sha)["waiver"]["reason"] == "runner down"
    assert len(runner.calls) == 1
    assert remote_main(world) == sha


def test_a_waiver_given_with_the_first_run_admits_an_unavailable_suite(world, capsys):
    sha = feature(world, CODE)
    runner = FakeRunner({sha: unavailable_run(sha)})
    code, out, _ = land(world, capsys, "--suite-waiver", "runner down", suite_runner=runner)
    assert code == 0
    assert lines_with(out, "SUITE: ")[0].startswith("SUITE: ADMITTED (UNAVAILABLE, waived: runner down")
    assert len(runner.calls) == 1 and remote_main(world) == sha


def test_a_waiver_never_admits_a_failing_suite(world, capsys):
    sha = feature(world, CODE)
    runner = FakeRunner({sha: (FAILED_A,), world.base: ()})
    code, _, err = land(world, capsys, "--suite-waiver", "just land it", suite_runner=runner)
    assert code == 2 and "NOT-LANDABLE" in err
    assert remote_main(world) == world.base


def test_a_gate_crash_refuses_and_pushes_nothing(world, capsys, monkeypatch):
    feature(world, CODE)

    def crash(*args, **kwargs):
        raise RuntimeError("suite exploded")

    monkeypatch.setattr(land_branch.suite_gate, "evaluate_landing", crash)
    code, out, err = land(world, capsys, suite_runner=FakeRunner({}))
    assert code == 2 and "NOT-LANDABLE" in err and "suite exploded" in err
    assert lines_with(out, "SUITE: ") == [] and remote_main(world) == world.base


def test_a_runner_crash_refuses_and_pushes_nothing(world, capsys):
    feature(world, CODE)

    def crash(*args, **kwargs):
        raise RuntimeError("sandbox exploded")

    code, _, err = land(world, capsys, suite_runner=crash)
    assert code == 2 and "NOT-LANDABLE" in err
    assert remote_main(world) == world.base


# ── --check ───────────────────────────────────────────────────────────────

def test_check_reports_a_repository_without_the_gate_as_not_required(world_no_gate, capsys):
    feature(world_no_gate, CODE)
    code, out = check(world_no_gate, capsys)
    assert code == 0
    assert lines_with(out, "SUITE-STATUS: ") == ["SUITE-STATUS: not required (repository has no suite gate)"]


def test_check_reports_a_missing_record_and_runs_nothing(world, capsys):
    sha = feature(world, CODE)
    runner = FakeRunner({})
    code, out = check(world, capsys, suite_runner=runner)
    assert code == 0 and runner.calls == []
    status = lines_with(out, "SUITE-STATUS: ")
    assert len(status) == 1 and f"no suite record for {gate.short(sha)}" in status[0]


def test_check_reports_a_stored_admitting_record(world, capsys):
    sha = feature(world, CODE)
    world.store(make_record(sha, world.base))
    code, out = check(world, capsys)
    assert code == 0 and lines_with(out, "SUITE-STATUS: ") == ["SUITE-STATUS: ADMITTED (PASS)"]


def test_check_uses_the_local_tracking_ref_and_never_fetches(world, capsys):
    sha = feature(world, CODE)
    world.store(make_record(sha, world.base))
    other = world.root / "other-clone"
    subprocess.run(["git", "clone", "-q", str(world.remote), str(other)], check=True, capture_output=True)
    commit(other, {"docs/late.md": "late\n"}, "remote moves unseen")
    git(other, "push", "-q", "origin", "main")
    code, out = check(world, capsys)
    assert code == 0 and lines_with(out, "SUITE-STATUS: ") == ["SUITE-STATUS: ADMITTED (PASS)"]
    assert git(world.work, "rev-parse", "origin/main") == world.base


# ── githooks/pre-push ─────────────────────────────────────────────────────

def push(world):
    return subprocess.run(
        ["git", "-C", str(world.work), "-c", f"core.hooksPath={HOOK_PATH.parent}", "push", "origin", "HEAD:main"],
        capture_output=True, text=True,
    )


def test_the_hook_names_both_gates_and_never_execs_one_of_them():
    text = HOOK_PATH.read_text(encoding="utf-8")
    assert "instruction-smoke-gate.py" in text and "suite-gate.py" in text
    assert not any(line.startswith("exec ") for line in text.splitlines())


def test_the_hook_refuses_a_push_without_a_suite_record_and_admits_one_with_it(world):
    sha = world.code_commit()
    refused = push(world)
    assert refused.returncode != 0
    assert "refused" in refused.stderr and "no suite record" in refused.stderr
    assert f"to land a change run: {gate.RUN_COMMAND}" in refused.stderr
    assert remote_main(world) == world.base

    world.store(make_record(sha, world.base))
    landed = push(world)
    assert landed.returncode == 0, landed.stderr
    assert gate.admission_line(sha) in landed.stderr
    assert remote_main(world) == sha


def test_the_hook_refuses_when_only_the_smoke_gate_refuses(world):
    sha = commit(world.work, SURFACE_AND_CODE, "surface and code")
    world.store(make_record(sha, world.base))
    refused = push(world)
    assert refused.returncode != 0
    assert "no smoke record" in refused.stderr and gate.admission_line(sha) in refused.stderr
    assert remote_main(world) == world.base


def test_the_hook_reports_both_refusals_of_one_push(world):
    commit(world.work, SURFACE_AND_CODE, "surface and code")
    refused = push(world)
    assert refused.returncode != 0
    assert "no smoke record" in refused.stderr and "no suite record" in refused.stderr
    assert remote_main(world) == world.base


def test_the_hook_admits_a_push_both_gates_admit(world):
    sha = commit(world.work, SURFACE_AND_CODE, "surface and code")
    world.store(make_record(sha, world.base))
    smoke_gate.write_record(world.work / ".git", make_smoke_record(sha, world.base))
    landed = push(world)
    assert landed.returncode == 0, landed.stderr
    assert gate.admission_line(sha) in landed.stderr and smoke_gate.admission_line(sha) in landed.stderr
    assert remote_main(world) == sha
