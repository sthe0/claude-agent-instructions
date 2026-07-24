"""Tests for project_entry.detect_backend — pure-function unit tests + enter-task
selection-precedence tests via subprocess.

Mirrors scripts/tests/test_detect.py (difficulty_channel variant).
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from project_entry.detect_backend import detect_backends, load_detect_hook

SCRIPTS = Path(__file__).resolve().parents[1]
ENTER_TASK = SCRIPTS / "enter-task.sh"
PROJECTS_PY = SCRIPTS / "project_entry" / "projects.py"


# ── Pure-function tests ────────────────────────────────────────────────────

def _det(commands=(), paths=(), env=None, hook=None):
    cmds = set(commands)
    ps = set(paths)
    ev = env or {}
    return detect_backends(
        has_command=lambda cmd: cmd in cmds,
        path_exists=lambda p: p in ps,
        getenv=lambda k: ev.get(k),
        hook=hook,
    )


def test_gh_only_gives_git_github():
    assert _det(commands=["gh"]) == ("git", "github")


def test_no_signals_gives_git_none():
    assert _det() == ("git", "none")


def test_no_toolchain_is_org_neutral():
    """Core has no rule keyed on an org toolchain: with no hook installed, no command
    on PATH other than `gh` can move the pair off the neutral default."""
    assert _det(commands=["orgtool", "orgvcs"]) == ("git", "none")


def test_path_and_env_probes_accepted_for_signature_parity():
    """path_exists and getenv are accepted (probe-signature parity) but not consumed today."""
    assert _det(commands=["gh"], paths=["~/.github-token"], env={"GITHUB_TOKEN": "tok"}) == (
        "git",
        "github",
    )


# ── Hook seam ─────────────────────────────────────────────────────────────

def test_hook_decision_wins_over_neutral_rules():
    """A hook that decides overrides `gh` — the org's own precedence is the hook's
    business, not Core's."""
    assert _det(commands=["gh"], hook=lambda **kw: ("orgws", "orgtr")) == ("orgws", "orgtr")


def test_hook_returning_none_defers_to_neutral_rules():
    assert _det(commands=["gh"], hook=lambda **kw: None) == ("git", "github")
    assert _det(hook=lambda **kw: None) == ("git", "none")


def test_hook_receives_all_three_probes_by_keyword():
    seen = {}

    def hook(**kwargs):
        seen.update(kwargs)
        return None

    _det(commands=["gh"], hook=hook)
    assert set(seen) == {"has_command", "path_exists", "getenv"}
    assert seen["has_command"]("gh") is True


def test_plugin_absent_yields_no_hook_and_the_default_pair(tmp_path, monkeypatch):
    """The REAL default path, with no stub anywhere: an empty plugin dir installs no
    hook, and detection then resolves git/none without raising."""
    monkeypatch.setenv("CLAUDE_PROJECT_PLUGIN_DIR", str(tmp_path / "empty-plugins"))
    hook = load_detect_hook()
    assert hook is None
    assert _det(hook=hook) == ("git", "none")


def test_plugin_dir_without_detect_hook_is_not_an_error(tmp_path, monkeypatch):
    """A machine that installs backends but no detect hook must still detect cleanly."""
    (tmp_path / "backends").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECT_PLUGIN_DIR", str(tmp_path))
    assert load_detect_hook() is None


def test_plugin_detect_hook_is_loaded_and_used(tmp_path, monkeypatch):
    (tmp_path / "detect.py").write_text(
        "def detect(has_command, path_exists, getenv):\n"
        "    if has_command('orgvcs'):\n"
        "        return ('orgws', 'orgtr')\n"
        "    return None\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_PROJECT_PLUGIN_DIR", str(tmp_path))
    hook = load_detect_hook()
    assert hook is not None
    assert _det(commands=["orgvcs"], hook=hook) == ("orgws", "orgtr")
    assert _det(commands=["gh"], hook=hook) == ("git", "github")


# ── Subprocess helpers ────────────────────────────────────────────────────


def _write_exec(path: Path, content: str) -> Path:
    path.write_text(content)
    path.chmod(0o755)
    return path


def _make_git_stub(tmp: Path) -> Path:
    fake_toplevel = tmp / "myrepo"
    fake_toplevel.mkdir(exist_ok=True)
    stub = tmp / "git-stub"
    _write_exec(
        stub,
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            case "$1 $2" in
              "rev-parse --show-toplevel") printf '%s\\n' "{fake_toplevel}" ;;
              "worktree list")             : ;;
              "worktree add")              : ;;
              *)                           : ;;
            esac
            """
        ),
    )
    return stub


def _make_plugin_dir(tmp: Path) -> Path:
    """Fake plugin dir with workspace 'bktest' and tracker 'trtest'."""
    plugin = tmp / "plugins"
    (plugin / "backends").mkdir(parents=True, exist_ok=True)
    (plugin / "trackers").mkdir(parents=True, exist_ok=True)
    _write_exec(
        plugin / "backends" / "bktest.sh",
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            backend_detect() { return 0; }
            backend_ensure_workspace() { printf '/fakews/%s\\n' "$1"; }
            backend_compose() { :; }
            """
        ),
    )
    _write_exec(
        plugin / "trackers" / "trtest.sh",
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            tracker_resolve() { printf 'T1\\tstub-task\\n'; }
            tracker_create() { printf 'T2\\tstub-created\\n'; }
            """
        ),
    )
    return plugin


def _make_detector_stub(tmp: Path, ws: str, tr: str) -> str:
    """Python stub for CLAUDE_BACKEND_DETECTOR — prints '<ws> <tr>' and exits 0."""
    stub = tmp / f"det-{ws}-{tr}.py"
    stub.write_text(f'print("{ws} {tr}")\n')
    return str(stub)


def _make_identity(tmp: Path, ws: str = "", tr: str = "") -> str:
    """Write a minimal identity file with optional backend keys; return its path."""
    lines = []
    if ws:
        lines.append(f"project_backend={ws}")
    if tr:
        lines.append(f"tracker_backend={tr}")
    idf = tmp / "test-identity.local"
    idf.write_text(("\n".join(lines) + "\n") if lines else "")
    return str(idf)


def _base_env(tmp: Path, git_stub: Path) -> dict:
    plugin_dir = _make_plugin_dir(tmp)
    env = dict(os.environ)
    env["GIT_BIN"] = str(git_stub)
    env["GH_BIN"] = "true"
    env["CLAUDE_PROJECT_PLUGIN_DIR"] = str(plugin_dir)
    for k in (
        "CLAUDE_WORKSPACE_BACKEND",
        "CLAUDE_TRACKER_BACKEND",
        "CLAUDE_AGENT_IDENTITY",
        "CLAUDE_BACKEND_DETECTOR",
    ):
        env.pop(k, None)
    return env


def _register_no_tracker_project(tmp: Path, key: str = "P1") -> Path:
    """Register a project with NO tracker binding in a fresh tmp registry and
    return that registry root (for CLAUDE_PROJECTS_DIR). A resolvable project
    satisfies enter-task's empty-context guard for --key/--new, while the empty
    tracker_backend lets tracker resolution fall through to identity/detector —
    so the detect/identity-is-last-resort precedence under test is preserved."""
    reg = tmp / "registry"
    reg.mkdir(exist_ok=True)
    subprocess.run(
        ["python3", str(PROJECTS_PY), "register", str(reg), key,
         f"workspace_path={tmp / 'myrepo'}"],
        check=True, capture_output=True, text=True,
    )
    return reg


def _run(args: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(ENTER_TASK)] + args,
        env=env,
        capture_output=True,
        text=True,
    )


def _log_ws(stderr: str) -> str:
    """Extract workspace value from the enter-task log line."""
    for line in stderr.splitlines():
        if "enter-task: workspace=" in line:
            for part in line.split():
                if part.startswith("workspace="):
                    return part.split("=", 1)[1]
    return ""


def _log_tr(stderr: str) -> str:
    """Extract tracker value from the enter-task log line."""
    for line in stderr.splitlines():
        if "enter-task: workspace=" in line:
            for part in line.split():
                if part.startswith("tracker="):
                    return part.split("=", 1)[1]
    return ""


# ── Workspace selection precedence ────────────────────────────────────────
# --name + --dry-run: tracker is forced to 'none' by the --name path, so only
# the workspace axis is exercised here.


@pytest.fixture
def env_ws(tmp_path):
    git_stub = _make_git_stub(tmp_path)
    return _base_env(tmp_path, git_stub), tmp_path


def test_ws_detect_is_last_resort(env_ws):
    env, tmp = env_ws
    env["CLAUDE_BACKEND_DETECTOR"] = _make_detector_stub(tmp, "bktest", "none")
    env["CLAUDE_AGENT_IDENTITY"] = _make_identity(tmp)
    r = _run(["--name", "foo", "--dry-run"], env)
    assert r.returncode == 0, r.stderr
    assert _log_ws(r.stderr) == "bktest"


def test_ws_identity_overrides_detect(env_ws):
    env, tmp = env_ws
    env["CLAUDE_BACKEND_DETECTOR"] = _make_detector_stub(tmp, "git", "none")
    env["CLAUDE_AGENT_IDENTITY"] = _make_identity(tmp, ws="bktest")
    r = _run(["--name", "foo", "--dry-run"], env)
    assert r.returncode == 0, r.stderr
    assert _log_ws(r.stderr) == "bktest"


def test_ws_env_overrides_identity(env_ws):
    env, tmp = env_ws
    env["CLAUDE_WORKSPACE_BACKEND"] = "bktest"
    env["CLAUDE_BACKEND_DETECTOR"] = _make_detector_stub(tmp, "git", "none")
    env["CLAUDE_AGENT_IDENTITY"] = _make_identity(tmp, ws="git")
    r = _run(["--name", "foo", "--dry-run"], env)
    assert r.returncode == 0, r.stderr
    assert _log_ws(r.stderr) == "bktest"


def test_ws_flag_overrides_env(env_ws):
    env, tmp = env_ws
    env["CLAUDE_WORKSPACE_BACKEND"] = "git"
    env["CLAUDE_BACKEND_DETECTOR"] = _make_detector_stub(tmp, "git", "none")
    env["CLAUDE_AGENT_IDENTITY"] = _make_identity(tmp, ws="git")
    r = _run(["--name", "foo", "--workspace", "bktest", "--dry-run"], env)
    assert r.returncode == 0, r.stderr
    assert _log_ws(r.stderr) == "bktest"


# ── Tracker selection precedence ──────────────────────────────────────────
# --key T1 + --dry-run + --workspace bktest (fake plugin): only the tracker
# axis varies. The fake trtest backend provides tracker_resolve so --key works.


@pytest.fixture
def env_tr(tmp_path):
    git_stub = _make_git_stub(tmp_path)
    return _base_env(tmp_path, git_stub), tmp_path


def test_tr_detect_is_last_resort(env_tr):
    env, tmp = env_tr
    env["CLAUDE_BACKEND_DETECTOR"] = _make_detector_stub(tmp, "bktest", "trtest")
    env["CLAUDE_AGENT_IDENTITY"] = _make_identity(tmp)
    env["CLAUDE_PROJECTS_DIR"] = str(_register_no_tracker_project(tmp))
    r = _run(["--key", "T1", "--workspace", "bktest", "--project", "P1", "--dry-run"], env)
    assert r.returncode == 0, r.stderr
    assert _log_tr(r.stderr) == "trtest"


def test_tr_identity_overrides_detect(env_tr):
    env, tmp = env_tr
    env["CLAUDE_BACKEND_DETECTOR"] = _make_detector_stub(tmp, "bktest", "none")
    env["CLAUDE_AGENT_IDENTITY"] = _make_identity(tmp, ws="bktest", tr="trtest")
    env["CLAUDE_PROJECTS_DIR"] = str(_register_no_tracker_project(tmp))
    r = _run(["--key", "T1", "--workspace", "bktest", "--project", "P1", "--dry-run"], env)
    assert r.returncode == 0, r.stderr
    assert _log_tr(r.stderr) == "trtest"


def test_tr_env_overrides_identity(env_tr):
    env, tmp = env_tr
    env["CLAUDE_TRACKER_BACKEND"] = "trtest"
    env["CLAUDE_BACKEND_DETECTOR"] = _make_detector_stub(tmp, "bktest", "none")
    env["CLAUDE_AGENT_IDENTITY"] = _make_identity(tmp, ws="bktest", tr="none")
    r = _run(["--key", "T1", "--workspace", "bktest", "--dry-run"], env)
    assert r.returncode == 0, r.stderr
    assert _log_tr(r.stderr) == "trtest"


def test_tr_flag_overrides_env(env_tr):
    env, tmp = env_tr
    env["CLAUDE_TRACKER_BACKEND"] = "none"
    env["CLAUDE_BACKEND_DETECTOR"] = _make_detector_stub(tmp, "bktest", "none")
    env["CLAUDE_AGENT_IDENTITY"] = _make_identity(tmp, ws="bktest", tr="none")
    r = _run(
        ["--key", "T1", "--workspace", "bktest", "--tracker", "trtest", "--dry-run"],
        env,
    )
    assert r.returncode == 0, r.stderr
    assert _log_tr(r.stderr) == "trtest"
