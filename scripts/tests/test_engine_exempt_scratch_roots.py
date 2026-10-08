"""is_engine_exempt: scratch roots and personal local-state are exempt by realpath
containment (never substring), and a scratch root as broad as $HOME exempts nothing.

Positive cases use synthetic roots whose names contain no _EXEMPT_SUBSTRINGS member,
so the base substring list cannot mask a missing containment rule."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from agentctl import exempt_paths
from lib import config_root
from test_hook_state_gate import _is_deny as _refused, edit_payload, run_hook, write_state

SCRATCH = "/nonexistent-rr-scratch"
HOME = "/nonexistent-rr-home/u"
PROJECTS = "/nonexistent-rr-proj/projects"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("HOME", HOME)
    monkeypatch.setenv("AGENTCTL_SCRATCH_ROOTS", SCRATCH)
    monkeypatch.setattr(config_root, "projects_roots", lambda: [Path(PROJECTS)])


def _unmasked(path: str) -> None:
    for form in (path, os.path.realpath(path)):
        assert not any(s in form for s in exempt_paths._EXEMPT_SUBSTRINGS), form


def test_pos_scratch_root_file():
    path = f"{SCRATCH}/a/x.py"
    _unmasked(path)
    assert exempt_paths.is_engine_exempt(path)


def test_pos_symlink_into_scratch_root(tmp_path, monkeypatch):
    monkeypatch.setattr(exempt_paths, "_EXEMPT_SUBSTRINGS", ("/memory/", "/memory-global/", "/agent-memory/"))
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = outside / "link"
    link.symlink_to(root)
    monkeypatch.setenv("AGENTCTL_SCRATCH_ROOTS", str(root))
    assert exempt_paths.is_engine_exempt(str(link / "x.py"))
    assert not exempt_paths.is_engine_exempt(str(outside / "y.py"))


def test_pos_local_state_file():
    path = f"{PROJECTS}/abc123/local-state/x.json"
    _unmasked(path)
    assert exempt_paths.is_engine_exempt(path)


def test_pos_tmpdir_sourced_root(monkeypatch):
    monkeypatch.delenv("AGENTCTL_SCRATCH_ROOTS")
    monkeypatch.setenv("TMPDIR", "/nonexistent-rr-tmpdir")
    monkeypatch.setattr(tempfile, "tempdir", None)
    path = "/nonexistent-rr-tmpdir/s/x.py"
    _unmasked(path)
    assert exempt_paths.is_engine_exempt(path)


def test_pos_hook_allows_scratch_edit_at_classified(tmp_path):
    write_state(tmp_path, "s1", "CLASSIFIED")
    path = f"{SCRATCH}/a/x.py"
    proc = run_hook(
        edit_payload("s1", path),
        tmp_path,
        extra_env={"AGENT_RECURSION_DEPTH": "0", "AGENTCTL_SCRATCH_ROOTS": SCRATCH},
    )
    assert not _refused(proc)
    proc = run_hook(
        edit_payload("s1", "/nonexistent-rr-other/x.py"),
        tmp_path,
        extra_env={"AGENT_RECURSION_DEPTH": "0", "AGENTCTL_SCRATCH_ROOTS": SCRATCH},
    )
    assert _refused(proc)


def test_neg_scratch_root_string_prefix_sibling():
    assert not exempt_paths.is_engine_exempt("/nonexistent-rr-scratch2/x.py")


def test_neg_local_state_lookalikes():
    assert not exempt_paths.is_engine_exempt(f"{PROJECTS}/abc/memory-other/local-state-ish/x.py")
    assert not exempt_paths.is_engine_exempt(f"{PROJECTS}/a/b/local-state/x.py")
    assert not exempt_paths.is_engine_exempt(f"{PROJECTS}/abc/local-state-x/x.py")


def test_neg_root_slash(monkeypatch):
    monkeypatch.setenv("AGENTCTL_SCRATCH_ROOTS", "/")
    assert not exempt_paths.is_engine_exempt("/nonexistent-rr-other/x.py")


def test_neg_root_home(monkeypatch):
    monkeypatch.setenv("AGENTCTL_SCRATCH_ROOTS", HOME)
    assert not exempt_paths.is_engine_exempt(f"{HOME}/proj/x.py")


def test_neg_root_home_ancestor(monkeypatch):
    monkeypatch.setenv("AGENTCTL_SCRATCH_ROOTS", "/nonexistent-rr-home")
    assert not exempt_paths.is_engine_exempt(f"{HOME}/proj/x.py")


def test_neg_tmpdir_equal_home_without_override(monkeypatch):
    monkeypatch.delenv("AGENTCTL_SCRATCH_ROOTS")
    monkeypatch.setenv("TMPDIR", HOME)
    monkeypatch.setattr(tempfile, "tempdir", None)
    assert not exempt_paths.is_engine_exempt(f"{HOME}/proj/x.py")
