"""land-branch.py's pre-push instruction smoke gate: the step before the landing push.

Hermetic: every repository is a throwaway clone of a throwaway bare remote under tmp_path (the
stage-1 `world` fixture), and the sandbox runner is always a stub — no test builds a real
sandbox or launches `claude -p`.
"""
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from lib import instruction_smoke_gate as gate
from test_instruction_smoke_gate import (  # noqa: F401 — `world` is a fixture
    HOOK_PATH, LIVE, commit, git, make_record, stub_runner, world,
)

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
LAND_BRANCH = SCRIPTS_DIR / "land-branch.py"
GATE_CLI = SCRIPTS_DIR / "instruction-smoke-gate.py"


def _load():
    spec = importlib.util.spec_from_file_location("land_branch_smoke_gate_subject", LAND_BRANCH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


land_branch = _load()


class Counting:
    """A runner that counts launches; with no inner runner any launch is a test failure."""

    def __init__(self, inner=None):
        self.inner = inner
        self.calls = 0

    def __call__(self, *args):
        self.calls += 1
        if self.inner is None:
            raise AssertionError("the smoke must not have been launched")
        return self.inner(*args)


def feature(world, files, name="feature"):
    git(world.work, "checkout", "-q", "-b", name)
    return commit(world.work, files, "feature work")


def land(world, capsys, *extra, runner=None):
    code = land_branch.main(["-C", str(world.work), "--keep-branch", *extra], runner=runner)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def smoke_lines(out):
    return [line for line in out.splitlines() if line.startswith("SMOKE: ")]


def remote_main(world):
    return git(world.remote, "rev-parse", "main")


# ── the gate's decisions ──────────────────────────────────────────────────

def test_an_exempt_only_diff_lands_without_a_smoke_and_says_so(world, capsys):
    sha = feature(world, {"docs/new.md": "doc\n"})
    runner = Counting()
    code, out, _ = land(world, capsys, runner=runner)
    assert code == 0 and runner.calls == 0
    assert smoke_lines(out) == ["SMOKE: not required (diff touches only exempt paths)"]
    assert remote_main(world) == sha


def test_a_surface_diff_runs_the_smoke_and_lands_on_pass(world, capsys):
    sha = feature(world, {"CLAUDE.md": "new rules\n"})
    runner = Counting(stub_runner())
    code, out, _ = land(world, capsys, runner=runner)
    assert code == 0 and runner.calls == 1
    assert len(smoke_lines(out)) == 1 and "ran and admitted" in smoke_lines(out)[0]
    assert remote_main(world) == sha
    stored = gate.load_record(gate.git_common_dir(world.work), sha)
    assert stored["result"] == gate.PASS and stored["base_sha"] == world.base


def test_the_smoke_line_comes_before_the_push_report(world, capsys):
    feature(world, {"CLAUDE.md": "new rules\n"})
    _, out, _ = land(world, capsys, runner=Counting(stub_runner()))
    lines = out.splitlines()
    smoke_at = next(i for i, line in enumerate(lines) if line.startswith("SMOKE: "))
    pushed_at = next(i for i, line in enumerate(lines) if line.startswith("[land-branch] pushed "))
    assert smoke_at < pushed_at


def test_a_failing_smoke_refuses_and_pushes_nothing(world, capsys):
    feature(world, {"CLAUDE.md": "new rules\n"})
    runner = Counting(stub_runner({"canon:unchanged": gate.FAIL}))
    code, out, err = land(world, capsys, runner=runner)
    assert code == 2 and runner.calls == 1
    assert "NOT-LANDABLE" in err and smoke_lines(out) == []
    assert remote_main(world) == world.base


def test_an_unavailable_live_launch_needs_a_waiver_to_land(world, capsys):
    sha = feature(world, {"CLAUDE.md": "new rules\n"})
    unavailable = stub_runner({LIVE: gate.UNAVAILABLE})
    code, _, err = land(world, capsys, runner=Counting(unavailable))
    assert code == 2 and "NOT-LANDABLE" in err and "UNAVAILABLE" in err
    assert remote_main(world) == world.base

    code, out, _ = land(world, capsys, "--smoke-waiver", "claude offline", runner=Counting(unavailable))
    assert code == 0 and "waived" in smoke_lines(out)[0]
    assert remote_main(world) == sha


def test_a_stored_admitted_record_is_reused_without_a_launch(world, capsys):
    sha = feature(world, {"CLAUDE.md": "new rules\n"})
    world.store(make_record(sha, world.base))
    runner = Counting()
    code, out, _ = land(world, capsys, runner=runner)
    assert code == 0 and runner.calls == 0
    assert "by stored record" in smoke_lines(out)[0]
    assert remote_main(world) == sha


def test_a_stored_record_bound_to_another_base_is_not_trusted(world, capsys):
    sha = feature(world, {"CLAUDE.md": "new rules\n"})
    world.store(make_record(sha, "1" * 40))
    runner = Counting(stub_runner())
    code, _, _ = land(world, capsys, runner=runner)
    assert code == 0 and runner.calls == 1
    assert gate.load_record(gate.git_common_dir(world.work), sha)["base_sha"] == world.base


def test_a_remote_tip_the_branch_lacks_is_refused_before_any_launch(world, capsys):
    feature(world, {"CLAUDE.md": "new rules\n"})
    tip = world.advance_remote()
    runner = Counting()
    code, _, err = land(world, capsys, runner=runner)
    assert code == 2 and runner.calls == 0
    assert "not an ancestor" in err
    assert remote_main(world) == tip


def test_a_repository_without_the_sandbox_is_not_gated(world, capsys):
    git(world.work, "rm", "-q", "--", "scripts/instruction-sandbox.sh")
    git(world.work, "commit", "-q", "-m", "drop the sandbox")
    git(world.work, "push", "-q", "origin", "main")
    sha = feature(world, {"CLAUDE.md": "new rules\n"})
    runner = Counting()
    code, out, _ = land(world, capsys, runner=runner)
    assert code == 0 and runner.calls == 0
    assert smoke_lines(out) == ["SMOKE: not required (repository has no instruction sandbox)"]
    assert remote_main(world) == sha


def test_an_environment_failure_in_the_runner_refuses_in_defused_words(world, capsys):
    feature(world, {"CLAUDE.md": "new rules\n"})

    def broken(*_args):
        raise gate.GateError("sandbox build failed: permission denied")

    code, _, err = land(world, capsys, runner=broken)
    assert code == 2 and "NOT-LANDABLE" in err
    assert "permission" not in err.lower() and "denied" not in err.lower()
    assert remote_main(world) == world.base


# ── invariants kept around the gate ───────────────────────────────────────

def test_the_gate_leaves_the_working_tree_and_index_alone(world, capsys):
    feature(world, {"CLAUDE.md": "new rules\n"})
    (world.work / "untracked.txt").write_text("keep me\n", encoding="utf-8")
    (world.work / "README.md").write_text("dirty\n", encoding="utf-8")
    before = (git(world.work, "rev-parse", "HEAD"), git(world.work, "status", "--porcelain"))
    code, _, _ = land(world, capsys, runner=Counting(stub_runner()))
    assert code == 0
    assert (git(world.work, "rev-parse", "HEAD"), git(world.work, "status", "--porcelain")) == before
    assert (world.work / "untracked.txt").read_text(encoding="utf-8") == "keep me\n"


def test_check_reports_the_record_status_without_fetching_or_running(world, capsys):
    sha = feature(world, {"CLAUDE.md": "new rules\n"})
    world.advance_remote()
    tracking_before = git(world.work, "rev-parse", "refs/remotes/origin/main")

    code = land_branch.main(["-C", str(world.work), "--check"], runner=Counting())
    pending = capsys.readouterr().out
    assert code == 0 and "LANDABLE" in pending and "SMOKE-STATUS: pending" in pending
    assert git(world.work, "rev-parse", "refs/remotes/origin/main") == tracking_before

    world.store(make_record(sha, tracking_before))
    code = land_branch.main(["-C", str(world.work), "--check"], runner=Counting())
    admitted = capsys.readouterr().out
    assert code == 0 and "SMOKE-STATUS: admitted" in admitted
    assert smoke_lines(admitted) == []


def test_pre_push_lines_are_relayed_when_the_hook_is_enabled(world, capsys):
    sha = feature(world, {"CLAUDE.md": "new rules\n"})
    git(world.work, "config", "core.hooksPath", str(HOOK_PATH.parent))
    code, out, _ = land(world, capsys, runner=Counting(stub_runner()))
    assert code == 0
    assert out.splitlines().count(gate.admission_line(sha)) == 1
    assert remote_main(world) == sha


def test_without_the_hook_land_branch_never_claims_a_hook_admission(world, capsys):
    sha = feature(world, {"CLAUDE.md": "new rules\n"})
    code, out, err = land(world, capsys, runner=Counting(stub_runner()))
    assert code == 0
    assert gate.admission_line(sha) not in (out + err)
    assert remote_main(world) == sha


def test_the_hook_still_refuses_a_candidate_the_gate_never_saw(world):
    feature(world, {"CLAUDE.md": "new rules\n"})
    git(world.work, "config", "core.hooksPath", str(HOOK_PATH.parent))
    pushed = subprocess.run(
        ["git", "-C", str(world.work), "push", "origin", "HEAD:main"], capture_output=True, text=True,
    )
    assert pushed.returncode != 0 and "pre-push: refused" in pushed.stderr
    assert remote_main(world) == world.base


# ── the effects registry names the gate's effects (R7) ────────────────────

def _venue_with_registry(tmp_path):
    """A bare venue holding byte-copies of the two scripts; the repository's own registry pins them."""
    venue = tmp_path / "venue"
    (venue / "scripts").mkdir(parents=True)
    (venue / ".git").mkdir()
    for script in ("land-branch.py", "instruction-smoke-gate.py"):
        (venue / "scripts" / script).write_bytes((SCRIPTS_DIR / script).read_bytes())
    return venue


def _kinds(venue, command):
    from agentctl import tool_contracts

    resolved = tool_contracts.resolve_command(command, str(venue))
    assert resolved.status == "resolved", resolved.reason
    return sorted(res.kind for res in resolved.resources)


def test_the_smoke_cli_resolves_each_subcommand_to_its_effects(tmp_path):
    venue = _venue_with_registry(tmp_path)
    base = "python3 scripts/instruction-smoke-gate.py"
    assert _kinds(venue, f"{base} run --waiver offline") == ["file", "service"]
    assert _kinds(venue, f"{base} waive --sha HEAD --reason offline") == ["file"]
    assert _kinds(venue, f"{base} check --sha HEAD") == ["file"]
    assert _kinds(venue, f"{base} check --sha HEAD --remote-sha {'a' * 40}") == []
    assert _kinds(venue, f"{base} pre-push origin url") == []


def test_the_smoke_cli_leaves_an_unreviewed_shape_unresolved(tmp_path):
    from agentctl import tool_contracts

    venue = _venue_with_registry(tmp_path)
    for tail in ("", "frobnicate", "run --unknown-flag", "run -C /elsewhere"):
        resolved = tool_contracts.resolve_command(
            f"python3 scripts/instruction-smoke-gate.py {tail}".strip(), str(venue),
        )
        assert resolved.status == "unresolved", tail


def test_land_branch_names_the_fetch_record_and_live_launch_even_remote_only(tmp_path):
    venue = _venue_with_registry(tmp_path)
    for flags in ("", " --remote-only"):
        kinds = _kinds(venue, f"python3 scripts/land-branch.py --branch b --keep-branch{flags}")
        assert kinds == ["file", "service", "vcs_ref"]
    assert _kinds(venue, "python3 scripts/land-branch.py --check") == []


def test_the_repository_registry_pins_match_the_live_scripts():
    from agentctl import script_effects

    table = script_effects.load_script_effects_table()
    for script in ("land-branch.py", "instruction-smoke-gate.py"):
        entry = table[f"scripts/{script}"]
        live = hashlib.sha256((SCRIPTS_DIR / script).read_bytes()).hexdigest()
        assert entry.sha256 == live, f"re-pin scripts/script_effects.toml for {script}"


def test_the_record_store_is_read_from_the_common_dir_of_a_linked_worktree(world, capsys, tmp_path):
    sha = feature(world, {"CLAUDE.md": "new rules\n"})
    git(world.work, "checkout", "-q", "main")
    linked = tmp_path / "linked"
    git(world.work, "worktree", "add", "-q", str(linked), "feature")
    world.store(make_record(sha, world.base))
    code = land_branch.main(
        ["-C", str(linked), "--branch", "feature", "--keep-branch", "--remote-only"], runner=Counting(),
    )
    out = capsys.readouterr().out
    assert code == 0 and "by stored record" in out
    assert json.loads(gate.record_path(world.work / ".git", sha).read_text())["result"] == gate.PASS
