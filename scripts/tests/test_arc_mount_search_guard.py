"""Tests for hook-arc-mount-search-guard.py: deny recursive searches spanning ≥2 arc FUSE mounts."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "hook-arc-mount-search-guard.py"

spec = importlib.util.spec_from_file_location("hook_arc_mount_search_guard", str(HOOK))
_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_mod)
decide = _mod.decide
arc_mounts_from_text = _mod.arc_mounts_from_text

_PROC_TEXT = """
sysfs /sys sysfs rw 0 0
proc /proc proc rw 0 0
arc /home/the0/arcadia fuse.arc rw 0 0
arc /home/the0/arcadia_claude_local fuse.arc rw 0 0
arc /home/the0/arcadia_DEEPAGENT-100 fuse.arc rw 0 0
arc /home/the0/arcadia_DEEPAGENT-200 fuse.arc rw 0 0
arc /home/the0/arcadia_DEEPAGENT-300 fuse.arc rw 0 0
"""

MOUNTS_5 = arc_mounts_from_text(_PROC_TEXT)
HOME = "/home/the0"
PROJ = f"{HOME}/claude-agent-instructions"


def _is_deny(reason):
    return reason is not None


@pytest.fixture(autouse=True)
def _hermetic_home(monkeypatch):
    """decide() resolves roots via os.path.expanduser/expandvars/realpath, so on a
    host whose real home is not /home/the0 (macOS: ~ -> /Users/..., /home is a
    symlink into /System/Volumes/Data) every deny assertion below silently stops
    matching the fixture mounts. Pin resolution to the fixture's world."""
    monkeypatch.setenv("HOME", HOME)
    monkeypatch.setattr(_mod.os.path, "expanduser",
                        lambda p: HOME + p[1:] if p.startswith("~") else p)
    monkeypatch.setattr(_mod.os.path, "realpath", lambda p, **kw: p)


# --- arc_mounts_from_text ---

def test_arc_mounts_from_text_extracts_fuse_home_only():
    assert len(MOUNTS_5) == 5
    assert all(m.startswith("/home/the0/") for m in MOUNTS_5)


def test_arc_mounts_from_text_octal_decode():
    text = "arc /home/the0/dir\\040with\\040spaces fuse.arc rw 0 0\n"
    mounts = arc_mounts_from_text(text)
    assert mounts == ["/home/the0/dir with spaces"]


# --- Grep tool ---

def test_grep_rooted_at_home_deny():
    r = decide("Grep", {"path": HOME}, HOME, MOUNTS_5)
    assert _is_deny(r)
    assert HOME in r


def test_grep_path_omitted_cwd_is_home_deny():
    r = decide("Grep", {}, HOME, MOUNTS_5)
    assert _is_deny(r)


def test_grep_rooted_in_project_allow():
    r = decide("Grep", {"path": PROJ}, PROJ, MOUNTS_5)
    assert r is None


# --- Glob tool ---

def test_glob_tilde_deny():
    r = decide("Glob", {"path": "~"}, HOME, MOUNTS_5)
    assert _is_deny(r)


def test_glob_rooted_in_project_allow():
    r = decide("Glob", {"path": PROJ}, PROJ, MOUNTS_5)
    assert r is None


# --- Bash: find ---

def test_bash_find_home_deny():
    r = decide("Bash", {"command": f"find {HOME} -name '*.py'"}, HOME, MOUNTS_5)
    assert _is_deny(r)


def test_bash_find_tilde_deny():
    r = decide("Bash", {"command": "find ~ -name '*.py'"}, HOME, MOUNTS_5)
    assert _is_deny(r)


def test_bash_find_in_project_allow():
    r = decide("Bash", {"command": f"find {PROJ} -name '*.py'"}, PROJ, MOUNTS_5)
    assert r is None


# --- Bash: rg without explicit path → uses cwd ---

def test_bash_rg_no_path_cwd_home_deny():
    r = decide("Bash", {"command": "rg foo"}, HOME, MOUNTS_5)
    assert _is_deny(r)


def test_bash_rg_no_path_cwd_project_allow():
    r = decide("Bash", {"command": "rg foo"}, PROJ, MOUNTS_5)
    assert r is None


# --- Bash: grep -rn inside a single mount → ALLOW ---

def test_bash_grep_rn_inside_single_mount_allow():
    single_root = f"{HOME}/arcadia_claude_local/robot"
    r = decide("Bash", {"command": f"grep -rn foo {single_root}"}, single_root, MOUNTS_5)
    assert r is None


# --- Bash: non-search commands → ALLOW ---

def test_bash_echo_allow():
    r = decide("Bash", {"command": "echo hi"}, HOME, MOUNTS_5)
    assert r is None


def test_bash_git_status_allow():
    r = decide("Bash", {"command": "git status"}, HOME, MOUNTS_5)
    assert r is None


# --- No arc mounts → ALLOW regardless ---

def test_no_mounts_grep_allow():
    r = decide("Grep", {"path": HOME}, HOME, [])
    assert r is None


def test_no_mounts_bash_find_allow():
    r = decide("Bash", {"command": f"find {HOME} -name x"}, HOME, [])
    assert r is None


# --- Missing / empty inputs → no exception ---

def test_decide_missing_command_allow():
    r = decide("Bash", {}, HOME, MOUNTS_5)
    assert r is None


def test_decide_missing_path_falls_back_to_cwd():
    r = decide("Grep", {}, PROJ, MOUNTS_5)
    assert r is None


def test_decide_unknown_tool_allow():
    r = decide("Edit", {"file_path": HOME}, HOME, MOUNTS_5)
    assert r is None


# --- Subprocess: malformed stdin → exits 0, no output ---

def test_main_malformed_stdin_allows():
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input="not valid json",
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
