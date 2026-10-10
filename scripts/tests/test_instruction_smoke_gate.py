"""Pre-land instruction smoke gate: surface classifier, record binding, waiver scope, pre-push hook.

Hermetic: every repository is a throwaway clone of a throwaway bare remote under tmp_path, and
the sandbox runner is always a stub — no test builds a real sandbox or launches `claude -p`.
"""
import importlib.util
import io
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from lib import instruction_smoke_gate as gate

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
CLI_PATH = SCRIPTS_DIR / "instruction-smoke-gate.py"
HOOK_PATH = SCRIPTS_DIR.parent / "githooks" / "pre-push"
ZEROS = "0" * 40


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
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@dataclass
class World:
    root: Path
    remote: Path
    work: Path
    base: str
    counter: int = 0

    def surface_commit(self):
        self.counter += 1
        return commit(self.work, {"CLAUDE.md": f"rules {self.counter}\n"}, "surface")

    def advance_remote(self):
        self.counter += 1
        other = self.root / f"other{self.counter}"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(other)], check=True, capture_output=True)
        commit(other, {"docs/new.md": f"n{self.counter}\n"}, "remote moves")
        git(other, "push", "-q", "origin", "main")
        return git(other, "rev-parse", "HEAD")

    def record(self, candidate=None, **kwargs):
        candidate = candidate or git(self.work, "rev-parse", "HEAD")
        return make_record(candidate, kwargs.pop("base", self.base), **kwargs)

    def store(self, record, name_sha=None):
        path = gate.record_path(self.work / ".git", name_sha or record["candidate_sha"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
        return path


@pytest.fixture
def world(tmp_path, monkeypatch):
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "Smoke Test")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "smoke@example.invalid")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    git(work, "remote", "add", "origin", str(remote))
    base = commit(work, {
        "CLAUDE.md": "rules\n",
        "README.md": "readme\n",
        "docs/a.md": "doc\n",
        "scripts/instruction-sandbox.sh": "#!/bin/sh\n",
    }, "initial")
    git(work, "push", "-q", "origin", "main")
    return World(tmp_path, remote, work, base)


def make_record(candidate, base, *, statuses=None, result=None, waiver=None, sandbox=None):
    checks = [{"name": n, "status": gate.PASS, "detail": ""} for n in gate.REQUIRED_CHECKS]
    for name, status in (statuses or {}).items():
        for check in checks:
            if check["name"] == name:
                check["status"] = status
                check["detail"] = f"{status.lower()} detail"
    derived = gate.derive_result(checks)
    return {
        "schema": gate.SCHEMA,
        "candidate_sha": candidate,
        "base_sha": base,
        "remote": "origin",
        "trunk": "main",
        "fetched_at": "2026-10-10T00:00:00Z",
        "ran_at": "2026-10-10T00:01:00Z",
        "result": result or derived,
        "verify_exit": gate.VERIFY_EXIT[derived],
        "sandbox_core_sha": sandbox or candidate,
        "checks": checks,
        "waiver": waiver,
    }


WAIVER = {"reason": "claude -p offline", "at": "2026-10-10T00:02:00Z", "detail": ""}
LIVE = "live:core-marker"


def verify_text(statuses=None, result=None):
    lines = []
    for name in gate.REQUIRED_CHECKS:
        status = (statuses or {}).get(name, gate.PASS)
        lines.append(f"CHECK {name} {status} {status.lower()} detail")
    derived = gate.derive_result([{"status": (statuses or {}).get(n, gate.PASS)} for n in gate.REQUIRED_CHECKS])
    lines.append(f"RESULT: {result or derived}")
    return "\n".join(lines) + "\n"


def stub_runner(statuses=None, exit_code=None, core_sha=None, output=None):
    text = output if output is not None else verify_text(statuses)
    derived = gate.derive_result([{"status": (statuses or {}).get(n, gate.PASS)} for n in gate.REQUIRED_CHECKS])

    def runner(repo, candidate, root, timeout_s):
        return gate.SandboxRun(gate.VERIFY_EXIT[derived] if exit_code is None else exit_code, text, core_sha or candidate)

    return runner


def push_line(local_sha, remote_sha, ref="refs/heads/main"):
    return f"{ref} {local_sha} refs/heads/main {remote_sha}"


# ── surface classifier ────────────────────────────────────────────────────

def test_surface_exempt_paths_are_exactly_the_named_ones():
    exempt = ["docs/a.md", "docs/deep/b.md", "memory-global/MEMORY.md", "scripts/tests/test_x.py", "README.md"]
    assert gate.surface_paths(exempt) == []
    assert not gate.touches_surface(exempt)


def test_everything_else_including_the_instruction_files_is_surface():
    paths = ["CLAUDE.md", "config.md", "scripts/foo.py", "skills/x/SKILL.md", "githooks/pre-push",
             "cursor/rules/claude-code-sync.mdc", "scripts/lib/instruction_smoke_gate.py"]
    assert gate.surface_paths(paths) == paths
    assert gate.touches_surface(["docs/a.md", "CLAUDE.md"])


def test_unknown_and_malformed_paths_are_surface():
    odd = ["docsx/a.md", "Docs/a.md", "scripts/testsfoo/a.py", "sub/README.md", "README.md/x", "/docs/a.md",
           "docs//a.md", "docs/./a.md", "", "scripts/tests"]
    assert gate.surface_paths(odd) == odd


def test_a_traversal_out_of_an_exempt_directory_is_surface():
    assert gate.touches_surface(["docs/../CLAUDE.md"])
    assert gate.touches_surface(["scripts/tests/../../scripts/foo.py"])


def test_every_instruction_surface_file_in_the_repository_is_surface():
    tracked = subprocess.run(
        ["git", "-C", str(SCRIPTS_DIR.parent), "ls-files", "-z"], capture_output=True, text=True,
    ).stdout.split("\0")
    tracked = [p for p in tracked if p]
    if not tracked:
        pytest.skip("not a git checkout")
    must_be_surface = [
        p for p in tracked
        if p in ("CLAUDE.md", "config.md")
        or (p.startswith("skills/") and p.endswith("SKILL.md"))
        or p.startswith(("agents/", "githooks/"))
        or (p.startswith("scripts/hook-") and p.endswith(".py"))
    ]
    assert "CLAUDE.md" in must_be_surface
    assert gate.surface_paths(must_be_surface) == must_be_surface


def test_changed_paths_lists_both_sides_of_a_rename(world):
    moved = commit(world.work, {"CLAUDE.md": None, "docs/CLAUDE.md": "rules\n"}, "move into docs")
    paths = gate.changed_paths(world.work, world.base, moved)
    assert sorted(paths) == ["CLAUDE.md", "docs/CLAUDE.md"]
    assert gate.touches_surface(paths)


def test_changed_paths_compares_the_whole_candidate_against_the_base(world):
    commit(world.work, {"CLAUDE.md": "rules 2\n"}, "surface")
    tip = commit(world.work, {"docs/b.md": "b\n"}, "exempt on top")
    assert gate.changed_paths(world.work, world.base, tip) == ["CLAUDE.md", "docs/b.md"]


# ── verify-output parser ──────────────────────────────────────────────────

def test_parse_a_pass_output():
    result, checks = gate.parse_verify_output(verify_text(), 0)
    assert result == gate.PASS
    assert [c["name"] for c in checks] == list(gate.REQUIRED_CHECKS)
    assert checks[0]["detail"] == "pass detail"


def test_parse_an_unavailable_and_a_fail_output():
    result, checks = gate.parse_verify_output(verify_text({LIVE: gate.UNAVAILABLE}), 3)
    assert result == gate.UNAVAILABLE and checks[4]["status"] == gate.UNAVAILABLE
    result, _ = gate.parse_verify_output(verify_text({LIVE: gate.UNAVAILABLE, "canon:unchanged": gate.FAIL}), 1)
    assert result == gate.FAIL


def test_parse_refuses_every_malformed_output():
    good = verify_text()
    bad = [
        "",
        good.replace("RESULT: PASS\n", ""),
        good + "RESULT: PASS\n",
        good.replace("CHECK static:lint-prose-length PASS", "CHECK static:lint-prose-length MAYBE"),
        good + "CHECK static:lint-prose-length PASS again\n",
        good.replace("RESULT: PASS", "RESULT: FAIL"),
        "RESULT: PASS\n",
        good.replace("RESULT: PASS", "RESULT: DONE"),
    ]
    for text in bad:
        with pytest.raises(gate.GateError):
            gate.parse_verify_output(text, 0)
    with pytest.raises(gate.GateError):
        gate.parse_verify_output(good, 1)


# ── waiver scope ──────────────────────────────────────────────────────────

def test_waiver_allowed_only_for_live_checks_that_were_unavailable():
    ok, _ = gate.waiver_allowed(make_record("a" * 40, "b" * 40, statuses={LIVE: gate.UNAVAILABLE}))
    assert ok


def test_waiver_never_covers_a_fail():
    for failing in (LIVE, "canon:unchanged", "static:lint-prose-length"):
        record = make_record("a" * 40, "b" * 40, statuses={LIVE: gate.UNAVAILABLE, failing: gate.FAIL})
        ok, why = gate.waiver_allowed(record)
        assert not ok and "FAIL" in why


def test_waiver_never_covers_an_unavailable_static_or_canon_check():
    for name in ("static:lint-hooks-executable", "canon:unchanged"):
        record = make_record("a" * 40, "b" * 40, statuses={name: gate.UNAVAILABLE, LIVE: gate.UNAVAILABLE})
        assert not gate.waiver_allowed(record)[0]
        assert not gate.record_admits({**record, "waiver": WAIVER}, "a" * 40, "b" * 40)[0]


def test_waiver_refused_when_nothing_is_unavailable():
    ok, why = gate.waiver_allowed(make_record("a" * 40, "b" * 40))
    assert not ok and "nothing to waive" in why


def test_a_waiver_needs_a_reason_and_is_one_line():
    record = make_record("a" * 40, "b" * 40, statuses={LIVE: gate.UNAVAILABLE})
    with pytest.raises(gate.GateError):
        gate.make_waiver(record, "   ", "t")
    waiver = gate.make_waiver(record, "line one\nline two\x1b[31m", "t")
    assert "\n" not in waiver["reason"] and "\x1b" not in waiver["reason"]
    assert waiver["detail"].startswith(LIVE)


# ── record admission ──────────────────────────────────────────────────────

def test_a_pass_record_for_this_commit_on_this_base_is_admitted():
    cand, base = "a" * 40, "b" * 40
    assert gate.record_admits(make_record(cand, base), cand, base) == (True, gate.PASS)


def test_a_fail_record_is_refused_even_with_a_waiver_attached():
    cand, base = "a" * 40, "b" * 40
    record = make_record(cand, base, statuses={LIVE: gate.FAIL}, waiver=WAIVER)
    ok, why = gate.record_admits(record, cand, base)
    assert not ok and "FAIL" in why
    static = make_record(cand, base, statuses={"static:verify-layout-contract": gate.FAIL, LIVE: gate.UNAVAILABLE},
                         waiver=WAIVER)
    assert not gate.record_admits(static, cand, base)[0]


def test_an_unavailable_record_needs_a_waiver_with_reason_and_time():
    cand, base = "a" * 40, "b" * 40
    record = make_record(cand, base, statuses={LIVE: gate.UNAVAILABLE})
    ok, why = gate.record_admits(record, cand, base)
    assert not ok and "no waiver" in why
    for broken in ({"reason": "", "at": "t"}, {"reason": "r"}, "yes", True):
        assert not gate.record_admits({**record, "waiver": broken}, cand, base)[0]
    ok, why = gate.record_admits({**record, "waiver": WAIVER}, cand, base)
    assert ok and why.startswith("UNAVAILABLE, waived: claude -p offline")


def test_a_record_for_another_commit_is_refused():
    cand, other, base = "a" * 40, "c" * 40, "b" * 40
    ok, why = gate.record_admits(make_record(other, base, sandbox=cand), cand, base)
    assert not ok and "not" in why


def test_a_sandbox_built_from_another_commit_is_refused():
    cand, other, base = "a" * 40, "c" * 40, "b" * 40
    ok, why = gate.record_admits(make_record(cand, base, sandbox=other), cand, base)
    assert not ok and "sandbox" in why


def test_a_record_built_on_a_stale_base_is_refused():
    cand, base, moved = "a" * 40, "b" * 40, "d" * 40
    ok, why = gate.record_admits(make_record(cand, base), cand, moved)
    assert not ok and "remote tip" in why


def test_a_record_lacking_a_required_check_is_refused():
    cand, base = "a" * 40, "b" * 40
    for required in gate.REQUIRED_CHECKS:
        record = make_record(cand, base)
        record["checks"] = [c for c in record["checks"] if c["name"] != required]
        ok, why = gate.record_admits(record, cand, base)
        assert not ok and required in why


def test_an_internally_inconsistent_or_foreign_record_is_refused():
    cand, base = "a" * 40, "b" * 40
    lying = make_record(cand, base, statuses={LIVE: gate.FAIL})
    lying["result"], lying["verify_exit"] = gate.PASS, 0
    assert not gate.record_admits(lying, cand, base)[0]
    assert not gate.record_admits({**make_record(cand, base), "schema": "other/v9"}, cand, base)[0]
    assert not gate.record_admits(["not", "a", "record"], cand, base)[0]
    assert not gate.record_admits({**make_record(cand, base), "checks": []}, cand, base)[0]


# ── record store ──────────────────────────────────────────────────────────

def test_record_round_trips_atomically_under_the_common_dir(world):
    record = world.record()
    path = gate.write_record(world.work / ".git", record)
    assert path == world.work / ".git" / "instruction-smoke" / f"{record['candidate_sha']}.json"
    assert gate.load_record(world.work / ".git", record["candidate_sha"]) == record
    assert [p.name for p in path.parent.iterdir()] == [path.name]


def test_a_non_sha_candidate_cannot_name_a_path_outside_the_store(world):
    with pytest.raises(gate.RecordError):
        gate.record_path(world.work / ".git", "../../evil")


def test_linked_worktrees_share_one_record_store(world):
    linked = world.root / "linked"
    git(world.work, "worktree", "add", "-q", "-b", "side", str(linked))
    assert gate.git_common_dir(linked).resolve() == (world.work / ".git").resolve()


# ── landing decision ──────────────────────────────────────────────────────

def test_an_exempt_only_change_lands_without_a_record(world):
    tip = commit(world.work, {"docs/b.md": "b\n", "README.md": "r2\n"}, "docs only")
    decision = gate.evaluate_landing(world.work, tip, world.base)
    assert decision.allowed and decision.kind == gate.NOT_REQUIRED


def test_a_surface_change_without_a_record_is_refused(world):
    tip = world.surface_commit()
    decision = gate.evaluate_landing(world.work, tip, world.base)
    assert not decision.allowed and "no smoke record" in decision.reason


def test_a_repository_without_the_sandbox_is_not_gated(world):
    git(world.work, "rm", "-q", "scripts/instruction-sandbox.sh")
    git(world.work, "commit", "-q", "-m", "drop sandbox")
    git(world.work, "push", "-q", "origin", "main")
    base = git(world.work, "rev-parse", "HEAD")
    tip = world.surface_commit()
    decision = gate.evaluate_landing(world.work, tip, base)
    assert decision.allowed and decision.kind == gate.NOT_REQUIRED


def test_a_stored_pass_record_admits_and_a_remote_advance_stales_it(world):
    tip = world.surface_commit()
    world.store(world.record(candidate=tip))
    assert gate.evaluate_landing(world.work, tip, world.base).kind == gate.ADMITTED
    moved = world.advance_remote()
    git(world.work, "fetch", "-q", "origin")
    decision = gate.evaluate_landing(world.work, tip, moved)
    assert not decision.allowed and "remote tip" in decision.reason


def test_a_record_stored_under_another_commits_name_is_refused(world):
    first = world.surface_commit()
    world.store(world.record(candidate=first))
    second = world.surface_commit()
    world.store(world.record(candidate=first, sandbox=second), name_sha=second)
    decision = gate.evaluate_landing(world.work, second, world.base)
    assert not decision.allowed


def test_an_unreadable_record_file_refuses(world):
    tip = world.surface_commit()
    path = gate.record_path(world.work / ".git", tip)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    decision = gate.evaluate_landing(world.work, tip, world.base)
    assert not decision.allowed and "cannot decide" in decision.reason


def test_a_git_error_refuses_rather_than_admits(world):
    decision = gate.evaluate_landing(world.work, "e" * 40, world.base)
    assert not decision.allowed


# ── pre-push hook decision ────────────────────────────────────────────────

def test_a_push_to_another_branch_or_a_trunk_delete_is_not_gated(world):
    tip = world.surface_commit()
    side = gate.prepush_decision([f"refs/heads/side {tip} refs/heads/side {ZEROS}"], world.work)
    assert side.allowed
    delete = gate.prepush_decision([f"(delete) {ZEROS} refs/heads/main {world.base}"], world.work)
    assert delete.allowed


def test_a_surface_push_to_trunk_without_a_record_is_refused_with_the_command(world):
    tip = world.surface_commit()
    result = gate.prepush_decision([push_line(tip, world.base)], world.work)
    assert not result.allowed
    assert any("no smoke record" in m for m in result.messages)
    assert any(gate.RUN_COMMAND in m for m in result.messages)


def test_a_push_with_a_matching_record_prints_the_admission_line(world):
    tip = world.surface_commit()
    world.store(world.record(candidate=tip))
    result = gate.prepush_decision([push_line(tip, world.base)], world.work)
    assert result.allowed and gate.admission_line(tip) in result.messages


def test_a_waived_push_prints_the_waiver(world):
    tip = world.surface_commit()
    world.store(world.record(candidate=tip, statuses={LIVE: gate.UNAVAILABLE}, waiver=WAIVER))
    result = gate.prepush_decision([push_line(tip, world.base)], world.work)
    assert result.allowed and any("waiver in effect" in m and "claude -p offline" in m for m in result.messages)


def test_a_new_remote_trunk_and_an_unknown_remote_tip_are_refused(world):
    tip = world.surface_commit()
    assert not gate.prepush_decision([push_line(tip, ZEROS)], world.work).allowed
    assert not gate.prepush_decision([push_line(tip, "f" * 40)], world.work).allowed


def test_malformed_hook_input_is_refused(world):
    assert not gate.prepush_decision(["refs/heads/main"], world.work).allowed


def test_hook_messages_never_read_as_missing_push_rights(world):
    cursed = "a" * 10 + "403" + "b" * 27
    result = gate.prepush_decision([push_line(cursed, world.base)], world.work)
    assert not result.allowed
    for message in result.messages:
        assert gate._PUSH_RIGHTS.search(message) is None
    text = gate.defuse("Permission denied (403) read-only: not authorized, access rights, Forbidden")
    assert gate._PUSH_RIGHTS.search(text) is None


def test_the_defused_pattern_is_the_one_sync_instructions_repo_matches():
    script = (SCRIPTS_DIR / "sync-instructions-repo.sh").read_text(encoding="utf-8")
    assert gate.PUSH_RIGHTS_PATTERN in script


# ── hook wiring ───────────────────────────────────────────────────────────

def test_the_pre_push_hook_is_executable_and_installed_and_required():
    assert os.access(HOOK_PATH, os.X_OK)
    assert 'scripts/instruction-smoke-gate.py" pre-push' in HOOK_PATH.read_text(encoding="utf-8")
    assert '"$HOOKS/pre-push"' in (SCRIPTS_DIR / "install-git-hooks.sh").read_text(encoding="utf-8")
    assert "githooks/pre-push" in (SCRIPTS_DIR / "verify-layout-contract.sh").read_text(encoding="utf-8")


def test_git_push_runs_the_hook_and_a_recorded_candidate_lands(world):
    push = ["git", "-C", str(world.work), "-c", f"core.hooksPath={HOOK_PATH.parent}", "push", "origin", "HEAD:main"]
    tip = world.surface_commit()
    refused = subprocess.run(push, capture_output=True, text=True)
    assert refused.returncode != 0
    assert "refused" in refused.stderr and "no smoke record" in refused.stderr
    assert git(world.remote, "rev-parse", "main") == world.base

    gate.write_record(world.work / ".git", world.record(candidate=tip))
    landed = subprocess.run(push, capture_output=True, text=True)
    assert landed.returncode == 0, landed.stderr
    assert gate.admission_line(tip) in landed.stderr
    assert f"pre-push: instruction smoke record admitted {tip}" in landed.stderr
    assert git(world.remote, "rev-parse", "main") == tip


# ── run_smoke and the waiver verb ─────────────────────────────────────────

def test_run_smoke_writes_a_record_bound_to_the_candidate_and_the_live_tip(world, tmp_path):
    tip = world.surface_commit()
    outcome = gate.run_smoke(world.work, runner=stub_runner(), sandbox_parent=tmp_path)
    record = gate.load_record(world.work / ".git", tip)
    assert outcome.admitted and record == outcome.record
    assert record["candidate_sha"] == tip and record["base_sha"] == world.base
    assert record["sandbox_core_sha"] == tip and record["waiver"] is None
    assert outcome.sandbox_root is None


def test_run_smoke_unavailable_is_refused_until_waived(world, tmp_path):
    world.surface_commit()
    runner = stub_runner({LIVE: gate.UNAVAILABLE})
    outcome = gate.run_smoke(world.work, runner=runner, sandbox_parent=tmp_path)
    assert not outcome.admitted and outcome.record["result"] == gate.UNAVAILABLE
    assert outcome.sandbox_root is not None and outcome.sandbox_root.exists()
    waived = gate.run_smoke(world.work, runner=runner, waiver="auth expired", sandbox_parent=tmp_path)
    assert waived.admitted and waived.record["waiver"]["reason"] == "auth expired"
    assert LIVE in waived.record["waiver"]["detail"]


def test_run_smoke_never_waives_a_fail(world, tmp_path):
    world.surface_commit()
    outcome = gate.run_smoke(world.work, runner=stub_runner({LIVE: gate.FAIL}), waiver="please",
                             sandbox_parent=tmp_path)
    assert not outcome.admitted and outcome.record["waiver"] is None
    assert "waiver not needed" in outcome.waiver_note
    assert outcome.sandbox_root is not None and outcome.sandbox_root.exists()


def test_run_smoke_refuses_a_candidate_that_lacks_the_remote_tip(world, tmp_path):
    world.surface_commit()
    world.advance_remote()
    with pytest.raises(gate.GateError) as err:
        gate.run_smoke(world.work, runner=stub_runner(), sandbox_parent=tmp_path)
    assert err.value.refused
    assert not (world.work / ".git" / gate.RECORD_DIR).exists()


def test_run_smoke_writes_no_record_when_the_sandbox_fails(world, tmp_path):
    world.surface_commit()

    def broken(repo, candidate, root, timeout_s):
        raise gate.GateError("sandbox build failed")

    with pytest.raises(gate.GateError, match="sandbox kept at"):
        gate.run_smoke(world.work, runner=broken, sandbox_parent=tmp_path)
    assert not (world.work / ".git" / gate.RECORD_DIR).exists()


def test_run_smoke_refuses_output_that_contradicts_the_exit_code(world, tmp_path):
    world.surface_commit()
    with pytest.raises(gate.GateError):
        gate.run_smoke(world.work, runner=stub_runner(exit_code=0, statuses={LIVE: gate.FAIL}),
                       sandbox_parent=tmp_path)


def test_waive_record_attaches_a_waiver_to_an_unavailable_record_only(world):
    tip = world.surface_commit()
    world.store(world.record(candidate=tip, statuses={LIVE: gate.UNAVAILABLE}))
    path, record = gate.waive_record(world.work, tip, "claude -p offline", now=lambda: "T")
    assert record["waiver"]["reason"] == "claude -p offline" and record["waiver"]["at"] == "T"
    assert gate.record_admits(gate.read_record(path), tip, world.base)[0]
    failed = world.surface_commit()
    world.store(world.record(candidate=failed, statuses={LIVE: gate.FAIL}))
    with pytest.raises(gate.GateError):
        gate.waive_record(world.work, failed, "nope")


# ── command line ──────────────────────────────────────────────────────────

def _cli():
    spec = importlib.util.spec_from_file_location("instruction_smoke_gate_cli", CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_run_exit_codes_and_waiver_banner(world, monkeypatch, capsys):
    cli = _cli()
    world.surface_commit()
    monkeypatch.setattr(gate, "default_runner", stub_runner())
    assert cli.main(["run", "-C", str(world.work)]) == 0
    monkeypatch.setattr(gate, "default_runner", stub_runner({LIVE: gate.UNAVAILABLE}))
    assert cli.main(["run", "-C", str(world.work)]) == 1
    assert cli.main(["run", "-C", str(world.work), "--waiver", "auth expired"]) == 0
    assert "WAIVER IN EFFECT" in capsys.readouterr().out
    monkeypatch.setattr(gate, "default_runner", stub_runner({LIVE: gate.FAIL}))
    assert cli.main(["run", "-C", str(world.work)]) == 1


def test_cli_run_prints_the_record_path_as_its_last_line(world, monkeypatch, capsys):
    cli = _cli()
    tip = world.surface_commit()
    expected = gate.record_path(world.work / ".git", tip)
    cases = (({}, gate.PASS, 0), ({LIVE: gate.UNAVAILABLE}, gate.UNAVAILABLE, 1), ({LIVE: gate.FAIL}, gate.FAIL, 1))
    for statuses, result, code in cases:
        monkeypatch.setattr(gate, "default_runner", stub_runner(statuses))
        assert cli.main(["run", "-C", str(world.work)]) == code
        lines = capsys.readouterr().out.splitlines()
        assert lines[-1] == f"RECORD: {expected.resolve()}"
        assert gate.read_record(expected)["result"] == result


def test_cli_run_prints_a_refusal_but_leaves_other_output_undefused(world, monkeypatch, capsys):
    cli = _cli()
    world.surface_commit()
    runner = stub_runner(output=verify_text().replace("pass detail", "permission denied 403", 1))
    monkeypatch.setattr(gate, "default_runner", runner)
    assert cli.main(["run", "-C", str(world.work)]) == 0
    assert "permission denied 403" in capsys.readouterr().out
    monkeypatch.setattr(gate, "default_runner", stub_runner({LIVE: gate.FAIL}))
    assert cli.main(["run", "-C", str(world.work)]) == 1
    refused = [ln for ln in capsys.readouterr().out.splitlines() if "REFUSED" in ln]
    assert refused and all(gate._PUSH_RIGHTS.search(ln) is None for ln in refused)


def test_cli_waive_takes_sha_and_attaches_the_waiver(world, capsys):
    cli = _cli()
    tip = world.surface_commit()
    world.store(world.record(candidate=tip, statuses={LIVE: gate.UNAVAILABLE}))
    assert cli.main(["check", "-C", str(world.work), "--sha", tip, "--remote-sha", world.base]) == 1
    assert cli.main(["waive", "-C", str(world.work), "--sha", tip, "--reason", "claude -p offline"]) == 0
    assert cli.main(["check", "-C", str(world.work), "--sha", tip, "--remote-sha", world.base]) == 0
    assert "ADMITTED" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["waive", "-C", str(world.work), "--ref", tip, "--reason", "x"])


def _run_cli(world, *args):
    return subprocess.run([sys.executable, str(CLI_PATH), *args, "-C", str(world.work)],
                          capture_output=True, text=True)


def test_cli_check_with_an_explicit_record_and_remote_sha_never_fetches(world, tmp_path):
    tip = world.surface_commit()
    git(world.work, "remote", "set-url", "origin", str(tmp_path / "no-such-remote.git"))
    record_file = tmp_path / "elsewhere.json"
    record_file.write_text(json.dumps(world.record(candidate=tip)), encoding="utf-8")
    args = ("check", "--sha", tip, "--remote-sha", world.base, "--record", str(record_file))

    admitted = _run_cli(world, *args)
    assert admitted.returncode == 0, admitted.stdout + admitted.stderr
    assert "ADMITTED" in admitted.stdout

    unfetchable = _run_cli(world, "check", "--sha", tip)
    assert unfetchable.returncode == 2


def test_cli_check_refuses_every_corrupted_record_and_the_stale_inputs(world, tmp_path):
    first = world.surface_commit()
    tip = world.surface_commit()
    good = world.record(candidate=tip)
    waiver = {"reason": "negative control", "at": "2026-10-10T00:00:00Z"}
    variants = {
        "fail": world.record(candidate=tip, statuses={"static:lint-prose-length": gate.FAIL}),
        "unavailable-no-waiver": world.record(candidate=tip, statuses={LIVE: gate.UNAVAILABLE}),
        "waiver-over-static-fail": world.record(
            candidate=tip, statuses={LIVE: gate.UNAVAILABLE, "static:lint-prose-length": gate.FAIL}, waiver=waiver),
        "drop-live": {**good, "checks": [c for c in good["checks"] if c["name"] != LIVE]},
        "sandbox-mismatch": {**good, "sandbox_core_sha": world.base},
    }

    def check(sha, remote, record):
        path = tmp_path / "rec.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        return _run_cli(world, "check", "--sha", sha, "--remote-sha", remote, "--record", str(path)).returncode

    assert check(tip, world.base, good) == 0
    for name, record in variants.items():
        assert check(tip, world.base, record) == 1, name
    assert check(tip, first, good) == 1  # stale base: the record was built on world.base, not on first
    assert check(first, world.base, good) == 1  # a record bound to tip does not admit its parent
    missing = _run_cli(world, "check", "--sha", tip, "--remote-sha", world.base,
                       "--record", str(tmp_path / "absent.json"))
    assert missing.returncode == 1


def test_cli_check_and_pre_push(world, monkeypatch, capsys):
    cli = _cli()
    tip = world.surface_commit()
    assert cli.main(["check", "-C", str(world.work)]) == 1
    world.store(world.record(candidate=tip))
    assert cli.main(["check", "-C", str(world.work)]) == 0
    monkeypatch.chdir(world.work)
    monkeypatch.setattr(sys, "stdin", io.StringIO(push_line(tip, world.base) + "\n"))
    assert cli.main(["pre-push", "origin", str(world.remote)]) == 0
    assert gate.admission_line(tip) in capsys.readouterr().err
    monkeypatch.setattr(sys, "stdin", io.StringIO(push_line(tip, "f" * 40) + "\n"))
    assert cli.main(["pre-push", "origin", str(world.remote)]) == 1


# ── mutation catalogue ────────────────────────────────────────────────────

@pytest.mark.skipif(os.environ.get("SMOKE_GATE_MUTATION_CHILD") == "1", reason="running inside a mutant subprocess")
def test_every_catalogue_mutant_is_killed_and_the_control_is_green():
    import smoke_gate_mutation_control as control

    survivors = {name: control.run_mutant(name) for name in control.CATALOGUE}
    assert survivors == {name: control.KILLED for name in control.CATALOGUE}
    assert control.run_control() == 0
