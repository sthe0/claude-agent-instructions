"""hook-resolution-reminder.py: branch-hygiene line wired into the
resolution-gate nudge via land-branch.py --check.

Complements test_hook_resolution_state.py (which covers resolution_gate_open()
in isolation) by exercising main() end to end for the branch-hygiene wiring.
"""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = SCRIPTS_DIR / "hook-resolution-reminder.py"


def _git_repo(path: Path, branch: str, *, commit: bool = True) -> Path:
    """Init a throwaway git repo at `path` on `branch` with one commit."""
    path.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True,
                                    capture_output=True)
    run("init", "-q")
    run("config", "user.email", "t@t.t")
    run("config", "user.name", "t")
    run("checkout", "-q", "-b", branch)
    if commit:
        run("commit", "-q", "--allow-empty", "-m", "x")
    return path


def _load_module():
    spec = importlib.util.spec_from_file_location("hook_resolution_reminder", HOOK_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_state(state_dir: Path, session_id: str, node: str, resolution_passed: bool) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "node": node,
        "resolution": {"passed": resolution_passed},
    }
    (state_dir / f"{session_id}.json").write_text(json.dumps(data), encoding="utf-8")


def _run(monkeypatch, capsys, mod, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = mod.main()
    return rc, capsys.readouterr().out


def _point_roots_at(monkeypatch, tmp_path: Path) -> Path:
    """The hook resolves its state file via config_root at call time from env:
    CLAUDE_AGENT_HOME is the current root, HOME the legacy fallback — point
    both into tmp so no real machine state leaks in. Returns the current
    root's state dir."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(tmp_path / "root"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path / "root" / "agentctl" / "state"


def _arm_gate(mod, monkeypatch, tmp_path, session_id="sess-res"):
    state_dir = _point_roots_at(monkeypatch, tmp_path)
    _write_state(state_dir, session_id, "RESOLUTION", resolution_passed=False)
    return session_id


def test_gate_open_and_landable_appends_branch_line(monkeypatch, capsys, tmp_path):
    mod = _load_module()
    session_id = _arm_gate(mod, monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "landable_branch_hint", lambda repo_dir: mod.BRANCH_HYGIENE_HINT)

    rc, out = _run(monkeypatch, capsys, mod, {"session_id": session_id, "cwd": str(tmp_path)})

    assert rc == 0
    assert "[resolution-reminder]" in out
    assert mod.BRANCH_HYGIENE_HINT in out


def test_gate_open_and_not_landable_no_branch_line(monkeypatch, capsys, tmp_path):
    mod = _load_module()
    session_id = _arm_gate(mod, monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "landable_branch_hint", lambda repo_dir: None)

    rc, out = _run(monkeypatch, capsys, mod, {"session_id": session_id, "cwd": str(tmp_path)})

    assert rc == 0
    assert "[resolution-reminder]" in out
    assert mod.BRANCH_HYGIENE_HINT not in out


def test_gate_open_and_land_branch_check_raises_no_crash(monkeypatch, capsys, tmp_path):
    """landable_branch_hint() itself is the boundary that swallows failures
    (see next test for an end-to-end version); here we simulate a caller-side
    surprise by making it raise, and confirm main() still doesn't crash the
    hook's always-exit-0 contract if that boundary is ever weakened."""
    mod = _load_module()
    session_id = _arm_gate(mod, monkeypatch, tmp_path)

    def _boom(repo_dir):
        raise RuntimeError("subprocess exploded")

    monkeypatch.setattr(mod, "landable_branch_hint", _boom)
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps({"session_id": session_id, "cwd": str(tmp_path)}))
    )

    try:
        rc = mod.main()
    except RuntimeError:
        rc = None
    out = capsys.readouterr().out

    assert rc == 0, "main() must not let a landable_branch_hint failure propagate or skip exit 0"
    assert "[resolution-reminder]" in out
    assert mod.BRANCH_HYGIENE_HINT not in out


def test_land_branch_check_actually_failing_subprocess_no_branch_line(monkeypatch, capsys, tmp_path):
    """End-to-end (no stubbing of landable_branch_hint itself): point
    LAND_BRANCH_SCRIPT at a missing file so the real subprocess call fails,
    and confirm the hook degrades silently."""
    mod = _load_module()
    session_id = _arm_gate(mod, monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "LAND_BRANCH_SCRIPT", tmp_path / "does-not-exist.py")

    rc, out = _run(monkeypatch, capsys, mod, {"session_id": session_id, "cwd": str(tmp_path)})

    assert rc == 0
    assert "[resolution-reminder]" in out
    assert mod.BRANCH_HYGIENE_HINT not in out


def test_non_gate_gratitude_path_unchanged_no_branch_line(monkeypatch, capsys, tmp_path):
    mod = _load_module()
    _point_roots_at(monkeypatch, tmp_path)
    # Even if the check would report LANDABLE, the gratitude fallback path
    # must never invoke it.
    monkeypatch.setattr(
        mod,
        "landable_branch_hint",
        lambda repo_dir: (_ for _ in ()).throw(AssertionError("must not be called")),
    )

    rc, out = _run(monkeypatch, capsys, mod, {"session_id": "no-state", "prompt": "thanks"})

    assert rc == 0
    assert "[resolution-reminder]" in out
    assert mod.BRANCH_HYGIENE_HINT not in out


def test_gate_closed_ordinary_prompt_no_output(monkeypatch, capsys, tmp_path):
    mod = _load_module()
    _point_roots_at(monkeypatch, tmp_path)

    rc, out = _run(
        monkeypatch, capsys, mod, {"session_id": "no-state", "prompt": "please add a test"}
    )

    assert rc == 0
    assert out == ""


# --- unpushed_branch_hint(): detect committed-but-unpushed personal branch ---

pytestmark_git = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git not available",
)


@pytestmark_git
def test_unpushed_hint_fires_on_personal_branch_with_unpushed_commit(tmp_path):
    """No upstream + a local commit on a personal branch -> nudge push."""
    mod = _load_module()
    repo = _git_repo(tmp_path / "r", "users/the0/feature")
    assert mod.unpushed_branch_hint(str(repo)) == mod.UNPUSHED_BRANCH_HINT


@pytestmark_git
def test_unpushed_hint_silent_on_shared_branch(tmp_path):
    """A trunk/shared branch name is suppressed — trunk push needs explicit
    confirmation, so we must not recommend it as a default."""
    mod = _load_module()
    for shared in ("master", "main", "trunk", "release-25.3"):
        repo = _git_repo(tmp_path / shared.replace("/", "_"), shared)
        assert mod.unpushed_branch_hint(str(repo)) is None, shared


@pytestmark_git
def test_unpushed_hint_silent_when_upstream_up_to_date(tmp_path):
    """Upstream configured and HEAD == @{u} (nothing ahead) -> no nudge."""
    mod = _load_module()
    origin = _git_repo(tmp_path / "origin", "users/the0/feat")
    # bare clone acting as remote, then a working clone tracking it
    subprocess.run(["git", "clone", "-q", "--bare", str(origin), str(tmp_path / "bare")],
                   check=True, capture_output=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "bare"), str(work)],
                   check=True, capture_output=True)
    # default branch is checked out with an upstream and is up to date
    assert mod.unpushed_branch_hint(str(work)) is None


def test_unpushed_hint_silent_on_non_git_dir(tmp_path):
    mod = _load_module()
    assert mod.unpushed_branch_hint(str(tmp_path / "not-a-repo")) is None


@pytestmark_git
def test_main_prefers_landable_over_unpushed(monkeypatch, capsys, tmp_path):
    """When the branch is cleanly landable, the land hint fires and the plain
    push hint is suppressed (landable already covers delivery)."""
    mod = _load_module()
    session_id = _arm_gate(mod, monkeypatch, tmp_path)
    repo = _git_repo(tmp_path / "r", "users/the0/feature")
    monkeypatch.setattr(mod, "landable_branch_hint", lambda repo_dir: mod.BRANCH_HYGIENE_HINT)

    rc, out = _run(monkeypatch, capsys, mod, {"session_id": session_id, "cwd": str(repo)})

    assert rc == 0
    assert mod.BRANCH_HYGIENE_HINT in out
    assert mod.UNPUSHED_BRANCH_HINT not in out


@pytestmark_git
def test_main_unpushed_hint_when_not_landable(monkeypatch, capsys, tmp_path):
    """Not landable but unpushed commits on a personal branch -> push hint."""
    mod = _load_module()
    session_id = _arm_gate(mod, monkeypatch, tmp_path)
    repo = _git_repo(tmp_path / "r", "users/the0/feature")
    monkeypatch.setattr(mod, "landable_branch_hint", lambda repo_dir: None)

    rc, out = _run(monkeypatch, capsys, mod, {"session_id": session_id, "cwd": str(repo)})

    assert rc == 0
    assert "[resolution-reminder]" in out
    assert mod.UNPUSHED_BRANCH_HINT in out


def test_hints_cite_landing_discipline_leaf():
    """Both delivery nudges name the landing-discipline leaf so the agent can
    load the full rule at the gate (CLAUDE.md keeps only the short pointer)."""
    mod = _load_module()
    for hint in (mod.BRANCH_HYGIENE_HINT, mod.UNPUSHED_BRANCH_HINT,
                 mod.MERGED_LEFTOVER_HINT):
        assert "memory-global/leaves/landing-discipline.md" in hint


# --- merged_leftover_hint(): merged-but-undeleted local branches ------------

def _clone_with_tracking(tmp_path: Path) -> Path:
    """A working clone whose origin/main tracking ref exists (bare origin +
    one seed commit + clone), returned ready for `merge-base` ancestry tests."""
    run = lambda *a, cwd: subprocess.run(["git", "-C", str(cwd), *a], check=True,
                                         capture_output=True)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)],
                   check=True, capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True,
                   capture_output=True)
    run("config", "user.email", "t@t.t", cwd=seed)
    run("config", "user.name", "t", cwd=seed)
    run("commit", "-q", "--allow-empty", "-m", "seed", cwd=seed)
    run("push", "-q", "origin", "main", cwd=seed)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True,
                   capture_output=True)
    run("config", "user.email", "t@t.t", cwd=clone)
    run("config", "user.name", "t", cwd=clone)
    return clone


@pytestmark_git
def test_merged_leftover_hint_names_merged_branch(tmp_path):
    """A local branch whose tip is reachable from origin/main (merged) but not
    deleted is named in the hint."""
    mod = _load_module()
    clone = _clone_with_tracking(tmp_path)
    subprocess.run(["git", "-C", str(clone), "branch", "landed-feature", "main"],
                   check=True, capture_output=True)
    hint = mod.merged_leftover_hint(str(clone))
    assert hint is not None
    assert "landed-feature" in hint
    assert "memory-global/leaves/landing-discipline.md" in hint


@pytestmark_git
def test_merged_leftover_hint_silent_when_no_leftovers(tmp_path):
    """An unmerged branch (a commit ahead of origin/main) is not flagged, and
    with no leftover branches the hint is None."""
    mod = _load_module()
    clone = _clone_with_tracking(tmp_path)
    subprocess.run(["git", "-C", str(clone), "checkout", "-q", "-b", "ahead"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-q", "--allow-empty",
                    "-m", "ahead"], check=True, capture_output=True)
    assert mod.merged_leftover_hint(str(clone)) is None


@pytestmark_git
def test_merged_leftover_hint_excludes_shared_branch_names(tmp_path):
    """`main` (== origin/main tip, technically an ancestor of itself) and other
    shared names are never listed."""
    mod = _load_module()
    clone = _clone_with_tracking(tmp_path)
    # main itself is present and reachable from origin/main but must be excluded.
    assert mod.merged_leftover_hint(str(clone)) is None


def test_merged_leftover_hint_silent_on_non_git_dir(tmp_path):
    mod = _load_module()
    assert mod.merged_leftover_hint(str(tmp_path / "not-a-repo")) is None


@pytestmark_git
def test_main_emits_merged_leftover_hint_at_open_gate(tmp_path, monkeypatch, capsys):
    """End to end: at the open resolution gate, a merged leftover branch is
    surfaced independently of the landable/unpushed nudges."""
    mod = _load_module()
    session_id = _arm_gate(mod, monkeypatch, tmp_path)
    clone = _clone_with_tracking(tmp_path)
    subprocess.run(["git", "-C", str(clone), "branch", "landed-feature", "main"],
                   check=True, capture_output=True)
    # Suppress the other two nudges so we isolate the leftover hint.
    monkeypatch.setattr(mod, "landable_branch_hint", lambda repo_dir: None)
    monkeypatch.setattr(mod, "unpushed_branch_hint", lambda repo_dir: None)

    rc, out = _run(monkeypatch, capsys, mod, {"session_id": session_id, "cwd": str(clone)})

    assert rc == 0
    assert "landed-feature" in out
