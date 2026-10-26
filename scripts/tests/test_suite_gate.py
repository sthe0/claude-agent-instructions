"""Pre-land suite gate: classification, baseline subtraction, record binding, exemption, pre-push hook.

Hermetic: every repository is a throwaway clone of a throwaway bare remote under tmp_path, and the
suite runner is always a stub - no test runs the repository's suite or builds a real sandbox. The
only real child processes are tiny: the process-group helper and the pytest plugin on a two-file
temporary project.
"""
import importlib.util
import inspect
import io
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from lib import landing_gate as shared
from lib import suite_gate as gate

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = SCRIPTS_DIR.parent
CLI_PATH = SCRIPTS_DIR / "suite-gate.py"
NOW = "2026-10-26T00:00:00Z"
ZEROS = "0" * 40
SHA_A, SHA_B = "a" * 40, "b" * 40
PUSH_RIGHTS = re.compile(shared.PUSH_RIGHTS_PATTERN, re.IGNORECASE)


# ── fixtures ──────────────────────────────────────────────────────────────

def git(repo, *args, check=True):
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()


def commit(repo, files, message="change"):
    for rel, content in files.items():
        path = Path(repo) / rel
        if content is None:
            git(repo, "rm", "-q", "--", rel)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        git(repo, "add", "--", rel)
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@dataclass
class Call:
    sha: str
    select: tuple
    root: Path
    timeout_s: int


class FakeRunner:
    """Stands in for the sandbox + pytest edge: failed ids per sha, or a ready SuiteRun."""

    def __init__(self, by_sha, rerun_fail=None):
        self.by_sha = by_sha
        self.rerun_fail = rerun_fail
        self.calls = []

    def __call__(self, repo, sha, root, *, select, timeout_s):
        self.calls.append(Call(sha, tuple(select), Path(root), timeout_s))
        spec = self.by_sha[sha]
        if isinstance(spec, gate.SuiteRun):
            return spec
        ids = tuple(spec)
        if select:
            pool = ids if self.rerun_fail is None else tuple(i for i in ids if i in self.rerun_fail)
            ids = tuple(i for i in select if i in pool)
        return ok_run(sha, ids)

    def shas(self):
        return [c.sha for c in self.calls]


def ok_run(sha, ids=(), **kwargs):
    return gate.SuiteRun(1 if ids else 0, tuple(ids), sha, log_tail="log line\n", **kwargs)


@dataclass
class World:
    root: Path
    remote: Path
    work: Path
    base: str
    sandbox: Path
    counter: int = 0

    @property
    def common(self):
        return self.work / ".git"

    def head(self):
        return git(self.work, "rev-parse", "HEAD")

    def code_commit(self):
        self.counter += 1
        return commit(self.work, {"scripts/foo.py": f"x = {self.counter}\n"}, "code")

    def docs_commit(self):
        self.counter += 1
        return commit(self.work, {"docs/a.md": f"doc {self.counter}\n", "memory-global/m.md": f"m {self.counter}\n"}, "docs")

    def advance_remote(self):
        self.counter += 1
        other = self.root / f"other{self.counter}"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(other)], check=True, capture_output=True)
        commit(other, {"docs/new.md": f"n{self.counter}\n"}, "remote moves")
        git(other, "push", "-q", "origin", "main")
        git(self.work, "fetch", "-q", "origin")
        return git(self.work, "rev-parse", "origin/main")

    def run(self, runner, candidate=None, tip=None, **kwargs):
        return gate.run_suite(
            self.work, candidate or self.head(), tip or self.base, runner=runner,
            timeout_s=kwargs.pop("timeout_s", 60), clock=lambda: NOW, sandbox_parent=self.sandbox, **kwargs,
        )

    def store(self, record, key=None):
        shared.write_record(self.common, gate.RECORD_DIR, key or record["candidate_sha"], record)

    def stored(self, sha):
        return shared.load_record(self.common, gate.RECORD_DIR, sha)

    def decide(self, candidate=None, tip=None):
        return gate.landing_decision(self.work, candidate or self.head(), tip or self.base)


def build_world(tmp_path, monkeypatch, with_gate=True):
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "Suite Test")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "suite@example.invalid")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    git(work, "remote", "add", "origin", str(remote))
    files = {
        "CLAUDE.md": "rules\n", "README.md": "readme\n", "docs/a.md": "doc\n", "memory-global/m.md": "mem\n",
        "scripts/foo.py": "x = 0\n",
    }
    if with_gate:
        files[gate.GATE_SCRIPT] = "#!/usr/bin/env python3\n"
    base = commit(work, files, "initial")
    git(work, "push", "-q", "origin", "main")
    sandbox = tmp_path / "sandboxes"
    sandbox.mkdir()
    return World(tmp_path, remote, work, base, sandbox)


@pytest.fixture
def world(tmp_path, monkeypatch):
    return build_world(tmp_path, monkeypatch)


@pytest.fixture
def world_no_gate(tmp_path, monkeypatch):
    return build_world(tmp_path, monkeypatch, with_gate=False)


def make_record(candidate, tip, *, status=gate.PASS, new=(), reason="", waiver=None, sandbox=None):
    return {
        "schema": gate.SCHEMA,
        "candidate_sha": candidate,
        "base_sha": tip,
        "sandbox_core_sha": sandbox or candidate,
        "status": status,
        "new_failures": list(new),
        "unavailable_reason": reason,
        "waiver": waiver,
    }


WAIVER = {"reason": "pytest host offline", "at": NOW}
FAILED_A, FAILED_B = "scripts/tests/test_x.py::test_a", "scripts/tests/test_x.py::test_b"


def push_line(local_sha, remote_sha, ref="refs/heads/main"):
    return f"{ref} {local_sha} refs/heads/main {remote_sha}"


# ── exemption: derived from git's path list only ──────────────────────────

@pytest.mark.parametrize("path", ["docs/a.md", "docs/deep/b.md", "memory-global/MEMORY.md", "memory-global/leaves/x.md"])
def test_exempt_paths_are_markdown_under_the_two_trees(path):
    assert gate.is_exempt_path(path)


@pytest.mark.parametrize("path", [
    "", "README.md", "CLAUDE.md", "docs/tool.py", "memory-global/x.json", "scripts/docs/a.md", "docs/a.md.py",
    "docs/../scripts/x.md", "docs/./a.md", "docs//a.md", "/docs/a.md", "docsx/a.md", "docs.md",
])
def test_everything_else_is_not_exempt(path):
    assert not gate.is_exempt_path(path)


def test_a_python_file_under_docs_is_not_an_exempt_path():
    assert not gate.is_exempt_path("docs/tool.py")
    assert not gate.is_exempt_path("memory-global/hook.sh")


def test_markdown_at_the_top_level_is_not_an_exempt_path():
    assert not gate.is_exempt_path("README.md")
    assert not gate.is_exempt_path("CLAUDE.md")


def test_a_docs_and_memory_only_landing_is_exempt_with_its_files_listed(world):
    cand = world.docs_commit()
    scope, files = gate.landing_scope(world.work, world.base, cand)
    assert scope == gate.EXEMPT
    assert files == ["docs/a.md", "memory-global/m.md"]


def test_one_code_file_among_docs_makes_the_landing_required(world):
    commit(world.work, {"docs/a.md": "changed\n", "scripts/foo.py": "y = 1\n"}, "mixed")
    assert gate.landing_scope(world.work, world.base, world.head()) == (gate.REQUIRED, [])


def test_a_non_markdown_file_under_docs_is_required(world):
    cand = commit(world.work, {"docs/tool.py": "x = 1\n"}, "tool")
    assert gate.landing_scope(world.work, world.base, cand)[0] == gate.REQUIRED


def test_markdown_outside_the_two_trees_is_required(world):
    cand = commit(world.work, {"README.md": "changed\n"}, "readme")
    assert gate.landing_scope(world.work, world.base, cand)[0] == gate.REQUIRED
    cand = commit(world.work, {"CLAUDE.md": "changed\n"}, "claude")
    assert gate.landing_scope(world.work, world.base, cand)[0] == gate.REQUIRED


def test_moving_a_code_file_into_docs_is_required_because_both_sides_count(world):
    git(world.work, "mv", "scripts/foo.py", "docs/foo.md")
    git(world.work, "commit", "-q", "-m", "move")
    assert gate.landing_scope(world.work, world.base, world.head())[0] == gate.REQUIRED


def test_deleting_a_code_file_beside_a_doc_edit_is_required(world):
    cand = commit(world.work, {"scripts/foo.py": None, "docs/a.md": "changed\n"}, "delete and edit")
    assert gate.landing_scope(world.work, world.base, cand)[0] == gate.REQUIRED


def test_a_landing_that_changes_no_path_is_not_exempt(world):
    cand = commit(world.work, {}, "empty")
    assert gate.landing_exemption(world.work, world.base, cand) is None
    assert gate.landing_scope(world.work, world.base, cand)[0] == gate.REQUIRED


def test_a_repository_without_the_gate_script_is_not_gated(world_no_gate):
    cand = world_no_gate.code_commit()
    assert gate.landing_scope(world_no_gate.work, world_no_gate.base, cand)[0] == shared.NOT_REQUIRED


def test_the_gate_binds_when_only_the_candidate_adds_the_script(world_no_gate):
    cand = commit(world_no_gate.work, {gate.GATE_SCRIPT: "#!/usr/bin/env python3\n"}, "add gate")
    assert gate.gate_applies(world_no_gate.work, world_no_gate.base, cand)


def test_no_caller_can_claim_an_exemption():
    entry_points = [gate.landing_decision, gate.evaluate_landing, gate.run_suite, gate.landing_scope,
                    gate.landing_exemption, gate.prepush_decision, gate.record_admits, gate.waive_record,
                    gate.default_runner, gate.gate_applies, gate.suite_status]
    for fn in entry_points:
        assert not any("exempt" in name for name in inspect.signature(fn).parameters), fn.__name__
    assert not any("exempt" in field for field in gate.SuiteRun.__dataclass_fields__)
    cli = _cli()
    parser = cli.build_parser()
    options = {opt for action in parser._actions for opt in action.option_strings}
    for sub in parser._subparsers._group_actions[0].choices.values():
        options |= {opt for action in sub._actions for opt in action.option_strings}
    assert not any("exempt" in opt for opt in options)


def test_a_record_cannot_declare_an_exemption(world):
    cand = world.code_commit()
    record = make_record(cand, world.base, status=gate.FAIL, new=[FAILED_A])
    record.update(exempt=True, scope="exempt", exemption=["scripts/foo.py"])
    world.store(record)
    decision = world.decide(cand)
    assert not decision.admitted and decision.kind == shared.REFUSED


def test_a_docs_only_landing_needs_no_record(world):
    cand = world.docs_commit()
    decision = world.decide(cand)
    assert decision.admitted and decision.kind == gate.EXEMPT
    assert "docs/a.md" in decision.line and "memory-global/m.md" in decision.line


def test_the_exempt_line_lists_every_file_untruncated(world):
    files = {f"docs/gen/f{i}.md": f"{i}\n" for i in range(30)}
    cand = commit(world.work, files, "many docs")
    line = world.decide(cand).line
    assert all(f in line for f in files)


def test_a_repository_without_the_gate_is_reported_not_required(world_no_gate):
    cand = world_no_gate.code_commit()
    decision = gate.landing_decision(world_no_gate.work, cand, world_no_gate.base)
    assert decision.admitted and decision.kind == shared.NOT_REQUIRED
    assert decision.line.startswith("SUITE: not required")


# ── classification of one suite run ───────────────────────────────────────

def test_a_clean_exit_zero_is_clean():
    assert gate.classify(ok_run(SHA_A), SHA_A) == ((), "")


def test_exit_one_with_recorded_ids_yields_the_sorted_unique_ids():
    run = ok_run(SHA_A, (FAILED_B, FAILED_A, FAILED_B))
    assert gate.classify(run, SHA_A) == ((FAILED_A, FAILED_B), "")


def test_exit_one_without_a_recorded_failure_is_unavailable():
    ids, reason = gate.classify(gate.SuiteRun(1, (), SHA_A), SHA_A)
    assert ids == () and "recorder saw no failed test" in reason


def test_exit_zero_with_recorded_failures_is_unavailable():
    ids, reason = gate.classify(gate.SuiteRun(0, (FAILED_A,), SHA_A), SHA_A)
    assert ids == () and reason


def test_a_pytest_usage_error_exit_is_unavailable():
    ids, reason = gate.classify(gate.SuiteRun(2, (), SHA_A), SHA_A)
    assert ids == () and reason.startswith("pytest exited 2")


@pytest.mark.parametrize("code", [2, 3, 4, 5, 126, 137, None])
def test_any_other_exit_code_is_unavailable(code):
    ids, reason = gate.classify(gate.SuiteRun(code, (), SHA_A), SHA_A)
    assert ids == () and reason.startswith("pytest exited")


def test_a_timed_out_run_is_unavailable_whatever_it_recorded():
    ids, reason = gate.classify(gate.SuiteRun(1, (FAILED_A,), SHA_A, timed_out=True), SHA_A)
    assert ids == () and "timeout" in reason


def test_a_failed_sandbox_build_is_unavailable():
    ids, reason = gate.classify(gate.SuiteRun(None, build_ok=False, detail="exit 3"), SHA_A)
    assert ids == () and "build failed" in reason and "exit 3" in reason


def test_a_missing_xdist_is_unavailable():
    ids, reason = gate.classify(gate.SuiteRun(0, (), SHA_A, xdist_ok=False), SHA_A)
    assert ids == () and "xdist" in reason


def test_a_sandbox_built_from_another_commit_is_unavailable():
    ids, reason = gate.classify(ok_run(SHA_B), SHA_A)
    assert ids == () and "not the commit under test" in reason


def test_subtract_is_by_node_id():
    assert gate.subtract([FAILED_B, FAILED_A], [FAILED_A]) == [FAILED_B]
    assert gate.subtract([FAILED_A], []) == [FAILED_A]
    assert gate.subtract([], [FAILED_A]) == []


def test_status_precedence_is_fail_then_unavailable_then_pass():
    assert gate.derive_status([FAILED_A], "reason") == gate.FAIL
    assert gate.derive_status([], "reason") == gate.UNAVAILABLE
    assert gate.derive_status([], "") == gate.PASS


# ── candidate run, tip baseline, rerun ────────────────────────────────────

def test_a_clean_candidate_passes_without_building_a_baseline(world):
    cand = world.code_commit()
    runner = FakeRunner({cand: ()})
    out = world.run(runner)
    assert out.record["status"] == gate.PASS and out.decision.admitted
    assert runner.shas() == [cand]
    assert out.record["baseline_cache"] == "none"
    assert not (world.common / gate.BASELINE_DIR).exists()


def test_a_failure_the_tip_also_has_does_not_block(world):
    cand = world.code_commit()
    runner = FakeRunner({cand: (FAILED_A,), world.base: (FAILED_A,)})
    out = world.run(runner)
    assert out.record["status"] == gate.PASS and out.decision.admitted
    assert out.record["new_failures"] == [] and out.record["baseline_cache"] == "built"
    assert runner.shas() == [cand, world.base]


def test_a_new_failure_blocks_and_is_named(world):
    cand = world.code_commit()
    runner = FakeRunner({cand: (FAILED_A,), world.base: ()})
    out = world.run(runner)
    assert out.record["status"] == gate.FAIL and not out.decision.admitted
    assert out.record["new_failures"] == [FAILED_A]
    assert FAILED_A in out.decision.line and out.decision.line.startswith("SUITE: REFUSED")


def test_only_the_failures_absent_on_the_tip_are_new(world):
    cand = world.code_commit()
    runner = FakeRunner({cand: (FAILED_A, FAILED_B), world.base: (FAILED_A,)})
    out = world.run(runner)
    assert out.record["new_failures"] == [FAILED_B]
    assert out.record["baseline_failures"] == [FAILED_A]
    assert out.record["candidate_failures"] == [FAILED_A, FAILED_B]


def test_the_baseline_is_built_from_the_tips_own_tree_and_never_from_the_candidate(world):
    cand = world.code_commit()
    runner = FakeRunner({cand: (FAILED_A, FAILED_B), world.base: (FAILED_A,)})
    world.run(runner)
    baseline = shared.load_record(world.common, gate.BASELINE_DIR, world.base)
    assert baseline["failures"] == [FAILED_A] and baseline["tip_sha"] == world.base
    assert baseline["sandbox_core_sha"] == world.base
    assert [(c.sha, c.select) for c in runner.calls][1] == (world.base, ())


def test_the_baseline_gets_its_own_sandbox_root(world):
    cand = world.code_commit()
    runner = FakeRunner({cand: (FAILED_A,), world.base: ()})
    world.run(runner)
    assert runner.calls[0].root != runner.calls[1].root


def test_a_new_failure_is_rerun_once_in_the_candidates_sandbox(world):
    cand = world.code_commit()
    runner = FakeRunner({cand: (FAILED_A,), world.base: ()})
    world.run(runner)
    first, _, rerun = runner.calls
    assert (rerun.sha, rerun.select, rerun.root) == (cand, (FAILED_A,), first.root)


def test_a_new_failure_that_passes_on_rerun_is_unavailable_not_admitted(world):
    cand = world.code_commit()
    runner = FakeRunner({cand: (FAILED_A,), world.base: ()}, rerun_fail=set())
    out = world.run(runner)
    assert out.record["status"] == gate.UNAVAILABLE and not out.decision.admitted
    assert out.record["unconfirmed"] == [FAILED_A] and out.record["new_failures"] == []
    assert "did not fail again" in out.record["unavailable_reason"]


def test_a_confirmed_failure_blocks_even_when_another_did_not_repeat(world):
    cand = world.code_commit()
    runner = FakeRunner({cand: (FAILED_A, FAILED_B), world.base: ()}, rerun_fail={FAILED_A})
    out = world.run(runner)
    assert out.record["status"] == gate.FAIL
    assert out.record["new_failures"] == [FAILED_A] and out.record["unconfirmed"] == [FAILED_B]


def test_a_rerun_that_cannot_finish_is_unavailable(world):
    cand = world.code_commit()

    class Runner(FakeRunner):
        def __call__(self, repo, sha, root, *, select, timeout_s):
            if select:
                self.calls.append(Call(sha, tuple(select), Path(root), timeout_s))
                return gate.SuiteRun(None, timed_out=True)
            return super().__call__(repo, sha, root, select=select, timeout_s=timeout_s)

    out = world.run(Runner({cand: (FAILED_A,), world.base: ()}))
    assert out.record["status"] == gate.UNAVAILABLE and "rerun" in out.record["unavailable_reason"]


def test_the_baseline_is_cached_by_tip_across_candidates(world):
    first = world.code_commit()
    runner = FakeRunner({first: (FAILED_A,), world.base: (FAILED_A,)})
    assert world.run(runner).record["baseline_cache"] == "built"
    second = world.code_commit()
    runner.by_sha[second] = (FAILED_A,)
    out = world.run(runner)
    assert out.record["baseline_cache"] == "hit" and out.record["status"] == gate.PASS
    assert runner.shas().count(world.base) == 1


def test_a_baseline_stored_under_the_tip_but_made_for_another_tip_is_not_used(world):
    cand = world.code_commit()
    shared.write_record(world.common, gate.BASELINE_DIR, world.base, {
        "schema": gate.BASELINE_SCHEMA, "tip_sha": SHA_B, "sandbox_core_sha": world.base,
        "status": gate.COMPLETE, "failures": [FAILED_A],
    })
    runner = FakeRunner({cand: (FAILED_A,), world.base: ()})
    out = world.run(runner)
    assert out.record["baseline_cache"] == "built" and out.record["status"] == gate.FAIL
    assert world.base in runner.shas()


def test_a_baseline_made_for_another_tip_is_not_usable():
    record = {"schema": gate.BASELINE_SCHEMA, "tip_sha": SHA_B, "sandbox_core_sha": SHA_A,
              "status": gate.COMPLETE, "failures": []}
    assert not gate.usable_baseline(record, SHA_A)


@pytest.mark.parametrize("change", [
    {"schema": "other/v1"}, {"status": gate.UNAVAILABLE}, {"tip_sha": SHA_B}, {"sandbox_core_sha": SHA_B},
    {"failures": "not-a-list"},
])
def test_a_baseline_record_is_usable_only_when_complete_and_for_this_tip(change):
    good = {"schema": gate.BASELINE_SCHEMA, "tip_sha": SHA_A, "sandbox_core_sha": SHA_A,
            "status": gate.COMPLETE, "failures": []}
    assert gate.usable_baseline(good, SHA_A)
    assert not gate.usable_baseline({**good, **change}, SHA_A)
    assert not gate.usable_baseline(None, SHA_A)


def test_an_unavailable_baseline_makes_the_candidate_unavailable_and_is_not_reused(world):
    cand = world.code_commit()
    runner = FakeRunner({cand: (FAILED_A,), world.base: gate.SuiteRun(None, timed_out=True)})
    out = world.run(runner)
    assert out.record["status"] == gate.UNAVAILABLE and "baseline is unavailable" in out.record["unavailable_reason"]
    baseline = shared.load_record(world.common, gate.BASELINE_DIR, world.base)
    assert baseline["status"] == gate.UNAVAILABLE
    runner.by_sha[world.base] = ()
    assert world.run(runner).record["baseline_cache"] == "built"


def test_a_candidate_run_that_times_out_is_unavailable_and_runs_no_baseline(world):
    cand = world.code_commit()
    runner = FakeRunner({cand: gate.SuiteRun(None, timed_out=True)})
    out = world.run(runner)
    assert out.record["status"] == gate.UNAVAILABLE and runner.shas() == [cand]


def test_a_bad_pytest_exit_is_unavailable_not_pass(world):
    cand = world.code_commit()
    out = world.run(FakeRunner({cand: gate.SuiteRun(2, (), cand)}))
    assert out.record["status"] == gate.UNAVAILABLE and not out.decision.admitted


def test_a_failing_pytest_exit_without_recorded_failures_is_not_a_pass(world):
    cand = world.code_commit()
    out = world.run(FakeRunner({cand: gate.SuiteRun(1, (), cand)}))
    assert out.record["status"] == gate.UNAVAILABLE and not out.decision.admitted


def test_a_timeout_cannot_turn_recorded_failures_into_a_verdict(world):
    cand = world.code_commit()
    out = world.run(FakeRunner({cand: gate.SuiteRun(1, (FAILED_A,), cand, timed_out=True), world.base: ()}))
    assert out.record["status"] == gate.UNAVAILABLE and out.record["new_failures"] == []


def test_the_timeout_reaches_every_runner_call(world):
    cand = world.code_commit()
    runner = FakeRunner({cand: (FAILED_A,), world.base: ()})
    world.run(runner, timeout_s=77)
    assert {c.timeout_s for c in runner.calls} == {77}


def test_a_candidate_that_does_not_contain_the_tip_is_refused_before_any_run(world):
    cand = world.code_commit()
    tip = world.advance_remote()
    runner = FakeRunner({cand: ()})
    with pytest.raises(shared.GateError) as err:
        world.run(runner, tip=tip)
    assert err.value.refused and runner.calls == []


def test_the_sandbox_is_removed_after_a_run(world):
    cand = world.code_commit()
    world.run(FakeRunner({cand: (FAILED_A,), world.base: ()}))
    assert list(world.sandbox.iterdir()) == []


def test_the_sandbox_is_removed_when_the_runner_raises(world):
    cand = world.code_commit()

    def boom(*args, **kwargs):
        raise RuntimeError("runner exploded")

    with pytest.raises(RuntimeError):
        world.run(boom)
    assert list(world.sandbox.iterdir()) == []


def test_remove_sandbox_only_touches_a_sandbox_directly_under_its_parent(tmp_path):
    inside = tmp_path / f"{gate.SANDBOX_PREFIX}abc"
    inside.mkdir()
    other = tmp_path / "keep"
    other.mkdir()
    gate.remove_sandbox(other, tmp_path)
    gate.remove_sandbox(inside, tmp_path / "elsewhere")
    assert other.exists() and inside.exists()
    gate.remove_sandbox(inside, tmp_path)
    assert not inside.exists()


def test_a_clean_candidate_run_writes_the_record_and_the_log_tail(world):
    cand = world.code_commit()
    big = "\n".join(f"line {i}" for i in range(1000))
    run = gate.SuiteRun(0, (), cand, log_tail=big)
    out = world.run(FakeRunner({cand: run}))
    stored = json.loads(out.record_path.read_text(encoding="utf-8"))
    assert stored["schema"] == gate.SCHEMA and stored["candidate_sha"] == cand and stored["base_sha"] == world.base
    lines = Path(stored["log"]).read_text(encoding="utf-8").splitlines()
    assert len(lines) == gate.LOG_TAIL_LINES and lines[-1] == "line 999"


def test_the_lock_serialises_concurrent_holders(tmp_path):
    path = tmp_path / "x.lock"
    got = threading.Event()

    def contender():
        with gate._exclusive(path):
            got.set()

    with gate._exclusive(path):
        thread = threading.Thread(target=contender)
        thread.start()
        time.sleep(0.3)
        assert not got.is_set()
    thread.join(5)
    assert got.is_set()


# ── admission: binding, status, waiver ────────────────────────────────────

def test_a_pass_record_bound_to_candidate_and_tip_is_admitted():
    assert gate.record_admits(make_record(SHA_A, SHA_B), SHA_A, SHA_B) == (True, gate.PASS)


def test_a_record_built_on_a_stale_tip_is_refused():
    ok, reason = gate.record_admits(make_record(SHA_A, SHA_B), SHA_A, "c" * 40)
    assert not ok and "remote tip" in reason


def test_a_record_for_another_candidate_is_refused_even_with_a_matching_sandbox():
    record = make_record(SHA_B, SHA_B, sandbox=SHA_A)
    ok, reason = gate.record_admits(record, SHA_A, SHA_B)
    assert not ok and "not" in reason


def test_a_record_stored_under_another_commits_name_is_refused(world):
    cand = world.code_commit()
    world.store(make_record(SHA_B, world.base, sandbox=cand), key=cand)
    decision = world.decide(cand)
    assert not decision.admitted and decision.kind == shared.REFUSED


def test_a_sandbox_built_from_another_commit_makes_a_record_unusable():
    ok, _ = gate.record_admits(make_record(SHA_A, SHA_B, sandbox=SHA_B), SHA_A, SHA_B)
    assert not ok


def test_a_record_of_another_schema_or_shape_is_refused():
    assert not gate.record_admits(["not", "a", "dict"], SHA_A, SHA_B)[0]
    assert not gate.record_admits({**make_record(SHA_A, SHA_B), "schema": "suite-run/v0"}, SHA_A, SHA_B)[0]
    assert not gate.record_admits({**make_record(SHA_A, SHA_B), "status": "MAYBE"}, SHA_A, SHA_B)[0]
    assert not gate.record_admits({**make_record(SHA_A, SHA_B), "new_failures": "x"}, SHA_A, SHA_B)[0]


def test_a_record_whose_status_contradicts_its_failure_lists_is_refused():
    assert not gate.record_admits(make_record(SHA_A, SHA_B, new=[FAILED_A]), SHA_A, SHA_B)[0]
    assert not gate.record_admits(make_record(SHA_A, SHA_B, reason="timeout"), SHA_A, SHA_B)[0]
    assert not gate.record_admits(make_record(SHA_A, SHA_B, status=gate.FAIL), SHA_A, SHA_B)[0]


def test_a_fail_record_is_refused_even_with_a_waiver_attached():
    record = make_record(SHA_A, SHA_B, status=gate.FAIL, new=[FAILED_A], waiver=WAIVER)
    ok, reason = gate.record_admits(record, SHA_A, SHA_B)
    assert not ok and "adds 1 test failure" in reason and FAILED_A in reason


def test_an_unavailable_record_needs_a_waiver_with_reason_and_time():
    record = make_record(SHA_A, SHA_B, status=gate.UNAVAILABLE, reason="pytest exited 2")
    assert not gate.record_admits(record, SHA_A, SHA_B)[0]
    for waiver in ({"reason": "", "at": NOW}, {"reason": "ok", "at": ""}, {"reason": "ok"}, "free text", 7):
        assert not gate.record_admits({**record, "waiver": waiver}, SHA_A, SHA_B)[0]
    ok, summary = gate.record_admits({**record, "waiver": WAIVER}, SHA_A, SHA_B)
    assert ok and "waived: pytest host offline" in summary


def test_a_waiver_is_attached_only_to_an_unavailable_run(world):
    cand = world.code_commit()
    failing = world.run(FakeRunner({cand: (FAILED_A,), world.base: ()}), waiver="trust me")
    assert failing.record["status"] == gate.FAIL and failing.record["waiver"] is None
    clean = world.run(FakeRunner({cand: ()}), waiver="trust me")
    assert clean.record["status"] == gate.PASS and clean.record["waiver"] is None
    hung = gate.SuiteRun(None, sandbox_core_sha=cand, timed_out=True)
    unavailable = world.run(FakeRunner({cand: hung}), waiver="host offline")
    assert unavailable.record["waiver"] == {"reason": "host offline", "at": NOW}
    assert unavailable.decision.admitted


def test_a_run_whose_sandbox_never_built_cannot_be_waived_into_a_landing(world):
    cand = world.code_commit()
    out = world.run(FakeRunner({cand: gate.SuiteRun(None, build_ok=False, detail="exit 3")}), waiver="host offline")
    assert out.record["status"] == gate.UNAVAILABLE and not out.decision.admitted


def test_an_unavailable_run_without_a_waiver_is_refused(world):
    cand = world.code_commit()
    out = world.run(FakeRunner({cand: gate.SuiteRun(None, sandbox_core_sha=cand, timed_out=True)}))
    assert not out.decision.admitted and "no waiver" in out.decision.reason


def test_waive_attaches_a_reason_to_an_unavailable_record_only(world):
    cand = world.code_commit()
    world.run(FakeRunner({cand: gate.SuiteRun(None, sandbox_core_sha=cand, timed_out=True)}))
    path, record = gate.waive_record(world.work, cand, "  host \n offline ", now=lambda: NOW)
    assert record["waiver"] == {"reason": "host offline", "at": NOW}
    assert world.decide(cand).admitted


def test_waive_refuses_a_fail_record_and_a_missing_one(world):
    cand = world.code_commit()
    with pytest.raises(shared.GateError, match="no suite record"):
        gate.waive_record(world.work, cand, "reason")
    world.run(FakeRunner({cand: (FAILED_A,), world.base: ()}))
    with pytest.raises(shared.GateError) as err:
        gate.waive_record(world.work, cand, "reason")
    assert err.value.refused and world.stored(cand)["waiver"] is None


def test_waive_refuses_an_empty_reason(world):
    cand = world.code_commit()
    world.run(FakeRunner({cand: gate.SuiteRun(None, sandbox_core_sha=cand, timed_out=True)}))
    with pytest.raises(shared.GateError):
        gate.waive_record(world.work, cand, "   ")


# ── landing decision, reuse, status ───────────────────────────────────────

def test_a_code_landing_without_a_record_is_refused_with_the_run_command(world):
    decision = world.decide(world.code_commit())
    assert not decision.admitted and gate.RUN_COMMAND in decision.line


def test_a_stored_pass_record_admits_and_a_remote_advance_stales_it(world):
    cand = world.code_commit()
    world.store(make_record(cand, world.base))
    assert world.decide(cand).admitted
    moved = world.advance_remote()
    stale = world.decide(cand, moved)
    assert not stale.admitted and "remote tip" in stale.line


def test_an_unreadable_record_is_a_refusal_not_a_crash(world):
    cand = world.code_commit()
    path = shared.record_path(world.common, gate.RECORD_DIR, cand)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    decision = world.decide(cand)
    assert not decision.admitted and "cannot decide" in decision.line


def test_the_decision_line_is_defused(world):
    cand = world.code_commit()
    world.store(make_record(cand, world.base, status=gate.UNAVAILABLE, reason="Permission denied by the sandbox"))
    line = world.decide(cand).line
    assert "REFUSED" in line and not PUSH_RIGHTS.search(line)


def test_cli_check_output_is_defused(world, capsys):
    cli = _cli()
    cand = world.code_commit()
    world.store(make_record(cand, world.base, status=gate.UNAVAILABLE, reason="Permission denied by the sandbox"))
    assert cli.main(["check", "-C", str(world.work), "--remote-sha", world.base]) == 1
    assert not PUSH_RIGHTS.search(capsys.readouterr().out)


def test_suite_status_is_the_decision_line(world):
    cand = world.code_commit()
    assert gate.suite_status(world.work, cand, world.base) == world.decide(cand).line


def test_evaluate_landing_reuses_an_admitting_record_without_running(world):
    cand = world.code_commit()
    world.store(make_record(cand, world.base))
    runner = FakeRunner({})
    decision = gate.evaluate_landing(world.work, cand, world.base, runner=runner)
    assert decision.admitted and runner.calls == []


def test_evaluate_landing_runs_when_the_stored_record_is_stale(world):
    cand = world.code_commit()
    world.store(make_record(cand, SHA_B))
    runner = FakeRunner({cand: ()})
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gate, "SANDBOX_PARENT", str(world.sandbox))
        decision = gate.evaluate_landing(world.work, cand, world.base, runner=runner)
    assert decision.admitted and runner.shas() == [cand]


def test_evaluate_landing_skips_the_run_for_an_exempt_landing(world):
    cand = world.docs_commit()
    runner = FakeRunner({})
    decision = gate.evaluate_landing(world.work, cand, world.base, runner=runner)
    assert decision.kind == gate.EXEMPT and runner.calls == []
    assert world.stored(cand) is None


def test_evaluate_landing_turns_a_git_error_into_a_refusal(world):
    decision = gate.evaluate_landing(world.work, SHA_A, world.base, runner=FakeRunner({}))
    assert not decision.admitted and "cannot decide" in decision.line


def test_admission_summary_names_the_waiver():
    record = make_record(SHA_A, SHA_B, status=gate.UNAVAILABLE, reason="timeout", waiver=WAIVER)
    assert "waived: pytest host offline" in gate.admission_summary(record)
    assert gate.admission_summary(make_record(SHA_A, SHA_B)) == gate.PASS


# ── pre-push hook ─────────────────────────────────────────────────────────

def test_pre_push_refuses_a_candidate_without_a_record(world):
    cand = world.code_commit()
    result = gate.prepush_decision([push_line(cand, world.base)], world.work)
    assert not result.allowed
    assert any(gate.RUN_COMMAND in m for m in result.messages)


def test_pre_push_admits_a_recorded_candidate(world):
    cand = world.code_commit()
    world.store(make_record(cand, world.base))
    result = gate.prepush_decision([push_line(cand, world.base)], world.work)
    assert result.allowed and gate.admission_line(cand) in result.messages


def test_pre_push_prints_the_waiver_in_effect(world):
    cand = world.code_commit()
    world.store(make_record(cand, world.base, status=gate.UNAVAILABLE, reason="timeout", waiver=WAIVER))
    result = gate.prepush_decision([push_line(cand, world.base)], world.work)
    assert result.allowed and any("waiver in effect" in m for m in result.messages)


def test_pre_push_refuses_a_record_built_on_an_older_tip(world):
    cand = world.code_commit()
    world.store(make_record(cand, world.base))
    moved = world.advance_remote()
    result = gate.prepush_decision([push_line(cand, moved)], world.work)
    assert not result.allowed


def test_pre_push_lets_a_docs_only_landing_through_and_names_the_files(world):
    cand = world.docs_commit()
    result = gate.prepush_decision([push_line(cand, world.base)], world.work)
    assert result.allowed
    text = "\n".join(result.messages)
    assert "exempt" in text and "docs/a.md" in text and "memory-global/m.md" in text


def test_pre_push_refuses_a_push_that_creates_the_trunk(world):
    cand = world.code_commit()
    assert not gate.prepush_decision([push_line(cand, ZEROS)], world.work).allowed


def test_pre_push_refuses_a_remote_tip_it_does_not_know(world):
    cand = world.code_commit()
    assert not gate.prepush_decision([push_line(cand, SHA_B)], world.work).allowed


def test_pre_push_ignores_pushes_to_other_branches(world):
    cand = world.code_commit()
    other = f"refs/heads/topic {cand} refs/heads/topic {ZEROS}"
    assert gate.prepush_decision([other], world.work).allowed


def test_pre_push_output_is_defused(world):
    cand = world.code_commit()
    world.store(make_record(cand, world.base, status=gate.UNAVAILABLE, reason="Permission denied"))
    result = gate.prepush_decision([push_line(cand, world.base)], world.work)
    assert not result.allowed and not any(PUSH_RIGHTS.search(m) for m in result.messages)


# ── process edge ──────────────────────────────────────────────────────────

def test_run_group_returns_output_and_exit_code():
    code, out, timed_out = gate.run_group([sys.executable, "-c", "print('hi'); raise SystemExit(3)"],
                                          cwd=None, env=dict(os.environ), timeout_s=30)
    assert (code, out.strip(), timed_out) == (3, "hi", False)


def test_run_group_terminates_the_group_on_timeout():
    started = time.monotonic()
    _, _, timed_out = gate.run_group([sys.executable, "-c", "import time; time.sleep(60)"],
                                     cwd=None, env=dict(os.environ), timeout_s=0.5, grace_s=5)
    assert timed_out and time.monotonic() - started < 20


def test_run_group_escalates_to_kill_when_term_is_ignored():
    code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('up', flush=True); time.sleep(60)"
    started = time.monotonic()
    rc, out, timed_out = gate.run_group([sys.executable, "-c", code], cwd=None, env=dict(os.environ),
                                        timeout_s=1.0, grace_s=0.5)
    assert timed_out and rc == -signal.SIGKILL and time.monotonic() - started < 20


def test_run_group_reports_a_missing_binary_as_a_gate_error(tmp_path):
    with pytest.raises(shared.GateError):
        gate.run_group([str(tmp_path / "nope")], cwd=None, env=dict(os.environ), timeout_s=5)


def test_the_sandbox_scrub_list_is_read_from_the_sandbox_script():
    names = gate.sandbox_scrub_names(SCRIPTS_DIR / gate.SANDBOX_SCRIPT)
    assert names and all(re.fullmatch(r"[A-Z][A-Z0-9_]+", n) for n in names)


def test_a_sandbox_script_without_a_scrub_list_is_an_error(tmp_path):
    script = tmp_path / "isb.sh"
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    with pytest.raises(shared.GateError):
        gate.sandbox_scrub_names(script)


def _suite_env(base, scrub=("SECRET_TOKEN",)):
    return gate.suite_env(Path("/sb"), "/real/userbase", Path("/sb/failures.txt"), scrub, base_env=base)


def test_suite_env_drops_scrubbed_agent_and_pytest_names():
    env = _suite_env({"PATH": "/bin", "SECRET_TOKEN": "s", "AGENTCTL_SESSION": "x", "PYTEST_ADDOPTS": "-n 8",
                      "PYTHONPATH": "/evil", "KEEP": "1"})
    assert env["KEEP"] == "1" and env["PATH"] == "/bin"
    for gone in ("SECRET_TOKEN", "AGENTCTL_SESSION", "PYTEST_ADDOPTS"):
        assert gone not in env
    assert env["PYTHONPATH"] == "/sb/plugin"


def test_suite_env_roots_every_home_in_the_sandbox():
    env = _suite_env({})
    assert env["HOME"] == "/sb/home" and env["CLAUDE_AGENT_HOME"] == "/sb/home/.claude-agent"
    assert env["CLAUDE_CONFIG_DIR"] == "/sb/home/.claude-agent" and env["CLAUDE_INSTRUCTIONS_REPO"] == "/sb/core"
    assert env["TMPDIR"] == "/sb/tmp" and env[gate.FAILURES_ENV] == "/sb/failures.txt"


def test_suite_env_never_passes_the_agent_session_on():
    env = _suite_env({"AGENTCTL_SESSION": "abc", "AGENTCTL_NODE": "n", "PYTEST_ADDOPTS": "-x"})
    assert not [n for n in env if n.startswith("AGENTCTL_")] and "PYTEST_ADDOPTS" not in env


def test_suite_env_keeps_the_real_user_site():
    assert _suite_env({})["PYTHONUSERBASE"] == "/real/userbase"


def test_suite_env_gives_git_a_fixed_identity():
    env = _suite_env({"GIT_AUTHOR_NAME": "someone else"})
    assert env["GIT_AUTHOR_NAME"] == "suite-gate" and env["GIT_COMMITTER_EMAIL"] == "suite-gate@localhost"


def test_pytest_argv_runs_the_whole_tree_or_exactly_the_selected_ids():
    root = Path("/sb")
    whole = gate.pytest_argv(root, (), 100)
    assert "scripts/tests" in whole and "--continue-on-collection-errors" in whole
    assert whole[whole.index("--rootdir") + 1] == "/sb/core" and gate.PLUGIN_MODULE in whole
    assert whole[whole.index("--timeout") + 1] == str(100 + gate.KILL_GRACE_S + gate.CAP_RUN_SLACK_S)
    picked = gate.pytest_argv(root, (FAILED_A, FAILED_B), 100)
    assert FAILED_A in picked and FAILED_B in picked and "scripts/tests" not in picked


def _fake_group(failed=(FAILED_A,), build_rc=0, pytest_rc=1, timed_out=False):
    calls = []

    def fake(argv, *, cwd, env, timeout_s, grace_s=gate.KILL_GRACE_S):
        calls.append((list(argv), cwd, env, timeout_s))
        if Path(argv[0]).name == gate.SANDBOX_SCRIPT:
            if build_rc == 0:
                root = Path(argv[argv.index("--root") + 1])
                core = root / "core"
                core.mkdir(parents=True)
                subprocess.run(["git", "init", "-q", str(core)], check=True)
                subprocess.run(["git", "-C", str(core), "-c", "user.name=t", "-c", "user.email=t@e.invalid",
                                "commit", "-q", "--allow-empty", "-m", "i"], check=True)
                (root / "sandbox.env").write_text("ISB_CORE_SHA=x\n", encoding="utf-8")
            return build_rc, "build output", False
        Path(env[gate.FAILURES_ENV]).write_text("".join(f"{f}\n" for f in failed), encoding="utf-8")
        return pytest_rc, "p1\np2\n", timed_out

    fake.calls = calls
    return fake


def test_default_runner_builds_the_sandbox_then_runs_pytest_in_it(tmp_path, monkeypatch):
    fake = _fake_group()
    monkeypatch.setattr(gate, "run_group", fake)
    root = tmp_path / "root"
    root.mkdir()
    run = gate.default_runner(tmp_path, SHA_A, root, select=(), timeout_s=50)
    core_sha = subprocess.run(["git", "-C", str(root / "core"), "rev-parse", "HEAD"], capture_output=True,
                              text=True).stdout.strip()
    assert (run.pytest_exit, run.failures, run.sandbox_core_sha) == (1, (FAILED_A,), core_sha)
    build, pytest_call = fake.calls
    assert build[0][build[0].index("--core-ref") + 1] == SHA_A and build[3] == gate.BUILD_TIMEOUT_S
    assert pytest_call[1] == root / "core" and pytest_call[3] == 50
    assert (root / "plugin" / f"{gate.PLUGIN_MODULE}.py").read_text(encoding="utf-8") == gate.PLUGIN_SOURCE


def test_default_runner_reuses_an_existing_sandbox_for_a_rerun(tmp_path, monkeypatch):
    fake = _fake_group()
    monkeypatch.setattr(gate, "run_group", fake)
    root = tmp_path / "root"
    root.mkdir()
    gate.default_runner(tmp_path, SHA_A, root, select=(), timeout_s=50)
    gate.default_runner(tmp_path, SHA_A, root, select=(FAILED_A,), timeout_s=50)
    builds = [c for c in fake.calls if Path(c[0][0]).name == gate.SANDBOX_SCRIPT]
    assert len(builds) == 1 and FAILED_A in fake.calls[-1][0]


def test_default_runner_reports_a_failed_build(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "run_group", _fake_group(build_rc=3))
    root = tmp_path / "root"
    root.mkdir()
    run = gate.default_runner(tmp_path, SHA_A, root, select=(), timeout_s=50)
    assert not run.build_ok and "exit 3" in run.detail
    assert gate.classify(run, SHA_A)[1].startswith("sandbox build failed")


def test_default_runner_reports_a_timed_out_pytest(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "run_group", _fake_group(timed_out=True))
    root = tmp_path / "root"
    root.mkdir()
    run = gate.default_runner(tmp_path, SHA_A, root, select=(), timeout_s=50)
    assert run.timed_out and run.pytest_exit is None


# ── the recording plugin, on a real two-file project ──────────────────────

def _plugin_project(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (project / "test_red.py").write_text("def test_red():\n    assert False\n", encoding="utf-8")
    (project / "test_broken.py").write_text("import module_that_does_not_exist\n", encoding="utf-8")
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / f"{gate.PLUGIN_MODULE}.py").write_text(gate.PLUGIN_SOURCE, encoding="utf-8")
    return project, plugin


def _plugin_run(tmp_path, *extra):
    project, plugin = _plugin_project(tmp_path)
    out = tmp_path / "failures.txt"
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("AGENTCTL_", "SUITE_GATE", "PYTEST_")) and k != "PYTHONPATH"}
    env.update(PYTHONPATH=str(plugin), **{gate.FAILURES_ENV: str(out)})
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(project), "-q", "--continue-on-collection-errors", "--rootdir", str(project),
         "-p", gate.PLUGIN_MODULE, "-p", "no:cacheprovider", *extra],
        cwd=project, env=env, capture_output=True, text=True,
    )
    return proc, gate.read_failures(out)


def test_the_plugin_records_failed_tests_and_collection_errors(tmp_path):
    proc, failures = _plugin_run(tmp_path, "-p", "no:xdist")
    assert proc.returncode == 1
    assert failures == ("test_broken.py", "test_red.py::test_red")


@pytest.mark.skipif(importlib.util.find_spec("xdist") is None, reason="pytest-xdist is not installed")
def test_the_plugin_records_the_same_failures_under_xdist(tmp_path):
    proc, failures = _plugin_run(tmp_path, "-n", "2")
    assert proc.returncode == 1
    assert failures == ("test_broken.py", "test_red.py::test_red")


def test_the_plugin_stays_silent_without_its_environment_variable(tmp_path):
    project, plugin = _plugin_project(tmp_path)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SUITE_GATE", "PYTEST_")) and k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(plugin)
    proc = subprocess.run([sys.executable, "-m", "pytest", str(project / "test_ok.py"), "-q", "--rootdir", str(project),
                           "-p", gate.PLUGIN_MODULE, "-p", "no:cacheprovider"],
                          cwd=project, env=env, capture_output=True, text=True)
    assert proc.returncode == 0 and not list(tmp_path.glob("*.txt"))


def test_read_failures_of_a_missing_file_is_empty(tmp_path):
    assert gate.read_failures(tmp_path / "none.txt") == ()


# ── configuration ─────────────────────────────────────────────────────────

def test_the_default_timeout_equals_the_config_row():
    assert gate.configured_timeout_s() == gate.DEFAULT_TIMEOUT_S == 1800
    row = re.search(r"^\| `suite-gate-timeout-s` \| `(\d+)` \|", (REPO_DIR / "config.md").read_text(encoding="utf-8"), re.M)
    assert row and int(row.group(1)) == gate.DEFAULT_TIMEOUT_S


def test_the_config_row_has_a_rationale_row():
    text = (REPO_DIR / "docs" / "operations" / "config-rationale.md").read_text(encoding="utf-8")
    assert re.search(r"^\| `suite-gate-timeout-s` \| `1800` \|", text, re.M)


@pytest.mark.parametrize("value,expected", [("120", 120), ("abc", gate.DEFAULT_TIMEOUT_S), ("0", gate.DEFAULT_TIMEOUT_S), ("-5", gate.DEFAULT_TIMEOUT_S), ("", gate.DEFAULT_TIMEOUT_S)])
def test_the_configured_timeout_falls_back_on_a_bad_row(tmp_path, value, expected):
    config = tmp_path / "config.md"
    config.write_text(f"| `{gate.CONFIG_KEY}` | `{value or ' '}` | meaning |\n", encoding="utf-8")
    assert gate.configured_timeout_s(config) == expected


def test_a_config_without_the_row_falls_back(tmp_path):
    config = tmp_path / "config.md"
    config.write_text("| `other-key` | `5` | meaning |\n", encoding="utf-8")
    assert gate.configured_timeout_s(config) == gate.DEFAULT_TIMEOUT_S


# ── command line ──────────────────────────────────────────────────────────

def _cli():
    spec = importlib.util.spec_from_file_location("suite_gate_cli", CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_run_admits_a_clean_candidate_and_ends_with_the_record(world, monkeypatch, capsys):
    cli = _cli()
    cand = world.code_commit()
    monkeypatch.setattr(gate, "SANDBOX_PARENT", str(world.sandbox))
    monkeypatch.setattr(gate, "default_runner", FakeRunner({cand: ()}))
    assert cli.main(["run", "-C", str(world.work), "--remote-sha", world.base]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert any(line.startswith("SUITE: ADMITTED") for line in lines)
    assert lines[-1] == f"RECORD: {shared.record_path(world.common, gate.RECORD_DIR, cand)}"


def test_cli_run_refuses_a_new_failure_and_names_it(world, monkeypatch, capsys):
    cli = _cli()
    cand = world.code_commit()
    monkeypatch.setattr(gate, "SANDBOX_PARENT", str(world.sandbox))
    monkeypatch.setattr(gate, "default_runner", FakeRunner({cand: (FAILED_A,), world.base: ()}))
    assert cli.main(["run", "-C", str(world.work), "--remote-sha", world.base]) == 1
    out = capsys.readouterr().out
    assert f"NEW FAILURE {FAILED_A}" in out and "SUITE: REFUSED" in out


def test_cli_run_on_a_docs_only_landing_is_exempt_and_writes_no_record(world, monkeypatch, capsys):
    cli = _cli()
    world.docs_commit()
    monkeypatch.setattr(gate, "default_runner", FakeRunner({}))
    assert cli.main(["run", "-C", str(world.work), "--remote-sha", world.base]) == 0
    out = capsys.readouterr().out
    assert "SUITE: exempt" in out and "docs/a.md" in out and "RECORD:" not in out


def test_cli_run_on_a_non_descendant_exits_refused(world, monkeypatch, capsys):
    cli = _cli()
    world.code_commit()
    tip = world.advance_remote()
    monkeypatch.setattr(gate, "default_runner", FakeRunner({}))
    assert cli.main(["run", "-C", str(world.work), "--remote-sha", tip]) == 1


def test_cli_check_reads_the_record_and_never_runs(world, monkeypatch, capsys):
    cli = _cli()
    cand = world.code_commit()
    monkeypatch.setattr(gate, "default_runner", FakeRunner({}))
    args = ["check", "-C", str(world.work), "--remote-sha", world.base]
    assert cli.main(args) == 1
    world.store(make_record(cand, world.base))
    assert cli.main(args) == 0
    assert "SUITE: ADMITTED" in capsys.readouterr().out


def test_cli_check_evaluates_an_explicit_record_file(world, tmp_path, capsys):
    cli = _cli()
    cand = world.code_commit()
    elsewhere = tmp_path / "rec.json"
    elsewhere.write_text(json.dumps(make_record(cand, world.base)), encoding="utf-8")
    assert cli.main(["check", "-C", str(world.work), "--remote-sha", world.base, "--record", str(elsewhere)]) == 0


def test_cli_show_prints_the_stored_status_and_is_read_only(world, capsys):
    cli = _cli()
    cand = world.code_commit()
    assert cli.main(["show", "-C", str(world.work)]) == 0
    assert "no record" in capsys.readouterr().out
    world.store(make_record(cand, world.base))
    assert cli.main(["show", "-C", str(world.work)]) == 0
    assert "PASS" in capsys.readouterr().out


def test_cli_waive_covers_unavailable_only(world, capsys):
    cli = _cli()
    cand = world.code_commit()
    world.store(make_record(cand, world.base, status=gate.FAIL, new=[FAILED_A]))
    assert cli.main(["waive", "-C", str(world.work), "--sha", cand, "--reason", "because"]) == 1
    world.store(make_record(cand, world.base, status=gate.UNAVAILABLE, reason="timeout"))
    assert cli.main(["waive", "-C", str(world.work), "--sha", cand, "--reason", "because"]) == 0
    assert cli.main(["check", "-C", str(world.work), "--remote-sha", world.base]) == 0


def test_cli_missing_repository_is_a_usage_or_environment_error(tmp_path, capsys):
    cli = _cli()
    assert cli.main(["check", "-C", str(tmp_path / "nope"), "--remote-sha", "HEAD"]) == 2


def test_cli_pre_push_exit_codes(world, monkeypatch, capsys):
    cli = _cli()
    cand = world.code_commit()
    monkeypatch.chdir(world.work)
    monkeypatch.setattr(sys, "stdin", io.StringIO(push_line(cand, world.base) + "\n"))
    assert cli.main(["pre-push", "origin", "url"]) == 1
    assert gate.RUN_COMMAND in capsys.readouterr().err
    world.store(make_record(cand, world.base))
    monkeypatch.setattr(sys, "stdin", io.StringIO(push_line(cand, world.base) + "\n"))
    assert cli.main(["pre-push", "origin", "url"]) == 0


def test_cli_pre_push_refuses_when_the_gate_itself_crashes(world, monkeypatch, capsys):
    cli = _cli()

    def boom(*args, **kwargs):
        raise RuntimeError("internal failure")

    monkeypatch.setattr(gate, "prepush_decision", boom)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert cli.main(["pre-push"]) == 1
    assert "refused" in capsys.readouterr().err


# ── the library is built on the shared layer ──────────────────────────────

def test_the_suite_library_uses_the_shared_layer_and_not_the_smoke_gate():
    source = (SCRIPTS_DIR / "lib" / "suite_gate.py").read_text(encoding="utf-8")
    cli_source = CLI_PATH.read_text(encoding="utf-8")
    assert "instruction_smoke_gate" not in source and "instruction_smoke_gate" not in cli_source
    for name in ("check_binding", "write_record", "changed_paths", "prepush_loop"):
        assert f"shared.{name}" in source
    assert not {"check_binding", "write_record", "read_record", "prepush_loop", "changed_paths"} & {
        n for n, v in vars(gate).items() if getattr(v, "__module__", None) == gate.__name__}


# ── mutation catalogue ────────────────────────────────────────────────────

@pytest.mark.skipif(os.environ.get("SUITE_GATE_MUTATION_CHILD") == "1", reason="running inside a mutant subprocess")
def test_the_catalogue_control_is_green_and_every_anchor_matches_once():
    import suite_gate_mutation_control as control

    assert len(control.CATALOGUE) == 26
    for name, mutant in control.CATALOGUE.items():
        source = (control.SCRIPTS_DIR.parent / mutant.relpath).read_text(encoding="utf-8")
        assert source.count(mutant.anchor) == 1, name
    assert control.run_control() == 0


# ── scripts index ─────────────────────────────────────────────────────────

def test_readme_lists_suite_gate_after_smoke_gate():
    rows = (SCRIPTS_DIR / "README.md").read_text(encoding="utf-8").splitlines()
    smoke = next(i for i, row in enumerate(rows) if row.startswith("| [instruction-smoke-gate.py]"))
    assert rows[smoke + 1].startswith("| [suite-gate.py](suite-gate.py) |")
