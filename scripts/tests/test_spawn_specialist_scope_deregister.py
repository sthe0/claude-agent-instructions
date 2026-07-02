"""spawn-specialist.py scope deregistration: on child exit, the wrapper resolves
the child's session id and removes its scope registration so a dead spawn
session never blocks a writer for the rest of its heartbeat TTL. Stage 1 of the
ghost-scope fix added a pid-liveness probe (a narrower, faster-acting
complement); this stage removes the registration outright for the common
supervised-exit case, leaving TTL/probe expiry as the fallback for a
supervisor that itself dies mid-spawn.

Unit tests exercise resolve_child_session_id / deregister_child_scope directly
(the extracted helpers wired into main()'s teardown). The subprocess tests at
the bottom drive the real wrapper end-to-end against a stub `claude`, with HOME
pointed at a tmp tree (mirrors tests/test_hook_scope_track.py's technique) so
DEFAULT_SCOPES_DIR resolves under tmp_path — verifying the wiring actually
fires on both a success and a failure exit, not just that the helper works in
isolation.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

from session_scope import registry

SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"


def _load():
    spec = importlib.util.spec_from_file_location("spawn_specialist_scope_deregister", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()


# ── resolve_child_session_id ────────────────────────────────────────────────

def test_resolve_prefers_json_session_id_over_transcript():
    stdout = json.dumps({"result": "COMPLETED: ok", "session_id": "sess-json"})
    got = MOD.resolve_child_session_id(stdout, Path("/tmp/sess-transcript.jsonl"))
    assert got == "sess-json"


def test_resolve_falls_back_to_transcript_stem_when_no_session_id_field():
    stdout = json.dumps({"result": "COMPLETED: ok"})
    got = MOD.resolve_child_session_id(stdout, Path("/tmp/sess-transcript.jsonl"))
    assert got == "sess-transcript"


def test_resolve_falls_back_to_transcript_stem_on_non_json_stdout():
    got = MOD.resolve_child_session_id("not json at all", Path("/tmp/sess-transcript.jsonl"))
    assert got == "sess-transcript"


def test_resolve_falls_back_to_transcript_stem_when_session_id_is_empty_string():
    stdout = json.dumps({"session_id": ""})
    got = MOD.resolve_child_session_id(stdout, Path("/tmp/sess-transcript.jsonl"))
    assert got == "sess-transcript"


def test_resolve_returns_none_when_neither_available():
    assert MOD.resolve_child_session_id("not json", None) is None


def test_resolve_returns_none_on_empty_stdout_and_no_transcript():
    assert MOD.resolve_child_session_id("", None) is None


# ── deregister_child_scope ──────────────────────────────────────────────────

def test_deregister_removes_the_session_scope_file(tmp_path):
    registry.heartbeat("sess-a", 1.0, scopes_dir=tmp_path)
    assert registry.scope_path(tmp_path, "sess-a").exists()
    MOD.deregister_child_scope("sess-a", scopes_dir=tmp_path)
    assert not registry.scope_path(tmp_path, "sess-a").exists()


def test_deregister_is_noop_on_missing_scope_file(tmp_path):
    MOD.deregister_child_scope("never-registered", scopes_dir=tmp_path)  # must not raise


def test_deregister_noop_when_session_id_is_none(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(MOD.registry, "delete", lambda *a, **k: calls.append((a, k)))
    MOD.deregister_child_scope(None, scopes_dir=tmp_path)
    assert calls == []


def test_deregister_noop_when_session_id_is_empty_string(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(MOD.registry, "delete", lambda *a, **k: calls.append((a, k)))
    MOD.deregister_child_scope("", scopes_dir=tmp_path)
    assert calls == []


def test_deregister_swallows_delete_failure(tmp_path, monkeypatch, capsys):
    def boom(*a, **k):
        raise OSError("disk exploded")

    monkeypatch.setattr(MOD.registry, "delete", boom)
    MOD.deregister_child_scope("sess-a", scopes_dir=tmp_path)  # must not raise
    assert "sess-a" in capsys.readouterr().err


def test_deregister_does_not_touch_other_sessions(tmp_path):
    registry.heartbeat("sess-a", 1.0, scopes_dir=tmp_path)
    registry.heartbeat("sess-b", 1.0, scopes_dir=tmp_path)
    MOD.deregister_child_scope("sess-a", scopes_dir=tmp_path)
    remaining = {r.session_id for r in registry.load_all(tmp_path)}
    assert remaining == {"sess-b"}


# ── end-to-end: the real wrapper's teardown actually deregisters ───────────

pytestmark_e2e = pytest.mark.skipif(os.name != "posix", reason="stub scripts are POSIX shell")


def _write_exec(path: Path, content: str) -> Path:
    path.write_text(content)
    path.chmod(0o755)
    return path


def _setup_fake_home(tmp_path: Path) -> Path:
    """A tmp HOME with just enough under .claude/skills/developer/ for
    skill_path() to resolve --kind developer, so the wrapper proceeds past its
    early existence check without touching the real skills catalog."""
    home = tmp_path / "home"
    skill_dir = home / ".claude" / "skills" / "developer"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# stub developer skill for tests\n")
    return home


def _stub_claude(bin_dir: Path, home: Path, session_id: str, exit_code: int) -> None:
    """A stand-in `claude` that ignores its args: touches a decoy transcript
    (so _discover_transcript_path resolves in well under its 10s timeout
    instead of the test waiting it out) and prints a result JSON carrying
    session_id, then exits with exit_code."""
    payload = json.dumps(
        {"result": f"COMPLETED: stub result for {session_id}", "cost_usd": 0.01, "session_id": session_id}
    )
    transcript_dir = home / ".claude" / "projects" / "stub"
    script = textwrap.dedent(
        f"""\
        #!/bin/bash
        mkdir -p {shlex.quote(str(transcript_dir))}
        : > {shlex.quote(str(transcript_dir))}/decoy-$$.jsonl
        printf '%s' {shlex.quote(payload)}
        exit {exit_code}
        """
    )
    _write_exec(bin_dir / "claude", script)


def _run_wrapper(tmp_path: Path, home: Path, bin_dir: Path) -> subprocess.CompletedProcess:
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\n**<<this step>>** do the thing.\n")
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "AGENT_RECURSION_DEPTH": "0",
    }
    # config_root resolves CLAUDE_CONFIG_DIR/CLAUDE_AGENT_HOME before HOME —
    # strip them so the child derives its root from the tmp HOME, not the
    # developer machine's real isolated root.
    env.pop("CLAUDE_CONFIG_DIR", None)
    env.pop("CLAUDE_AGENT_HOME", None)
    cmd = [
        "python3",
        str(SCRIPT),
        "--kind",
        "developer",
        "--plan",
        str(plan),
        "--done-criterion",
        "stub does nothing",
        "--criterion-type",
        "measurable",
    ]
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)


@pytestmark_e2e
def test_wrapper_deregisters_scope_on_success_exit(tmp_path):
    home = _setup_fake_home(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    session_id = f"sess-success-{os.getpid()}-{time.time_ns()}"
    _stub_claude(bin_dir, home, session_id, exit_code=0)

    scopes_dir = home / ".claude" / "agentctl" / "scopes"
    registry.heartbeat(session_id, 1.0, scopes_dir=scopes_dir)
    assert registry.scope_path(scopes_dir, session_id).exists()

    result = _run_wrapper(tmp_path, home, bin_dir)
    assert result.returncode == 0, result.stderr

    assert not registry.scope_path(scopes_dir, session_id).exists()


@pytestmark_e2e
def test_wrapper_deregisters_scope_on_failure_exit(tmp_path):
    home = _setup_fake_home(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    session_id = f"sess-failure-{os.getpid()}-{time.time_ns()}"
    _stub_claude(bin_dir, home, session_id, exit_code=1)

    scopes_dir = home / ".claude" / "agentctl" / "scopes"
    registry.heartbeat(session_id, 1.0, scopes_dir=scopes_dir)
    assert registry.scope_path(scopes_dir, session_id).exists()

    result = _run_wrapper(tmp_path, home, bin_dir)
    # the stub's non-zero exit propagates as the wrapper's own exit code
    assert result.returncode == 1, result.stderr

    assert not registry.scope_path(scopes_dir, session_id).exists()
