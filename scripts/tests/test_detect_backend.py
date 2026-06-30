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

from project_entry.detect_backend import detect_backends

SCRIPTS = Path(__file__).resolve().parents[1]
ENTER_TASK = SCRIPTS / "enter-task.sh"


# ── Pure-function tests ────────────────────────────────────────────────────

def _det(commands=(), paths=(), env=None):
    cmds = set(commands)
    ps = set(paths)
    ev = env or {}
    return detect_backends(
        has_command=lambda cmd: cmd in cmds,
        path_exists=lambda p: p in ps,
        getenv=lambda k: ev.get(k),
    )


def test_ya_and_arc_gives_arc_startrek():
    assert _det(commands=["ya", "arc"]) == ("arc", "startrek")


def test_arc_without_ya_gives_git_none():
    assert _det(commands=["arc"]) == ("git", "none")


def test_ya_without_arc_gives_git_none():
    assert _det(commands=["ya"]) == ("git", "none")


def test_gh_only_gives_git_github():
    assert _det(commands=["gh"]) == ("git", "github")


def test_ya_arc_and_gh_arc_wins():
    """Internal toolchain wins over GitHub CLI when both are present."""
    assert _det(commands=["ya", "arc", "gh"]) == ("arc", "startrek")


def test_no_signals_gives_git_none():
    assert _det() == ("git", "none")


def test_path_and_env_probes_accepted_for_signature_parity():
    """path_exists and getenv are accepted (probe-signature parity) but not consumed today."""
    assert _det(commands=["gh"], paths=["~/.github-token"], env={"GITHUB_TOKEN": "tok"}) == (
        "git",
        "github",
    )


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
    r = _run(["--key", "T1", "--workspace", "bktest", "--dry-run"], env)
    assert r.returncode == 0, r.stderr
    assert _log_tr(r.stderr) == "trtest"


def test_tr_identity_overrides_detect(env_tr):
    env, tmp = env_tr
    env["CLAUDE_BACKEND_DETECTOR"] = _make_detector_stub(tmp, "bktest", "none")
    env["CLAUDE_AGENT_IDENTITY"] = _make_identity(tmp, ws="bktest", tr="trtest")
    r = _run(["--key", "T1", "--workspace", "bktest", "--dry-run"], env)
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
