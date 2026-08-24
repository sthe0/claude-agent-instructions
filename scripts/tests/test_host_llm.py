"""lib.host_llm: the one seam that assembles a bare `<binary> -p ... <prompt>`
argv per coordination host and refuses to build one that crosses hosts
(Host-isolated spawn plan, step 4).

Every test monkeypatches host_llm.shutil.which / host_llm.os.environ directly
rather than depending on whether `claude`/`agent`/`cursor-agent` are actually
installed, or on ~/.cursor_api_key's real contents.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from lib import host_llm
from lib.runtime_models import HOST_CLAUDE, HOST_CURSOR


# --- binary_for ------------------------------------------------------------------

def test_binary_for_claude_is_the_literal_name_regardless_of_path(monkeypatch):
    # No shutil.which call at all for HOST_CLAUDE (see the docstring's
    # hermeticity rationale) — pin which() to prove it is never consulted.
    monkeypatch.setattr(host_llm.shutil, "which", lambda name: (_ for _ in ()).throw(
        AssertionError(f"shutil.which({name!r}) should not be called for HOST_CLAUDE")
    ))
    assert host_llm.binary_for(HOST_CLAUDE) == "claude"


def test_binary_for_cursor_prefers_agent_over_cursor_agent(monkeypatch):
    monkeypatch.setattr(host_llm.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert host_llm.binary_for(HOST_CURSOR) == "/usr/bin/agent"


def test_binary_for_cursor_falls_back_to_cursor_agent(monkeypatch):
    monkeypatch.setattr(
        host_llm.shutil, "which", lambda name: "/usr/bin/cursor-agent" if name == "cursor-agent" else None
    )
    assert host_llm.binary_for(HOST_CURSOR) == "/usr/bin/cursor-agent"


def test_binary_for_cursor_falls_back_to_agent_literal_when_neither_installed(monkeypatch):
    monkeypatch.setattr(host_llm.shutil, "which", lambda name: None)
    assert host_llm.binary_for(HOST_CURSOR) == "agent"


def test_binary_for_unknown_host_raises():
    with pytest.raises(ValueError):
        host_llm.binary_for("windows")


# --- assert_same_family: the cross-host refusal ------------------------------------

def test_assert_same_family_accepts_matching_pairs():
    host_llm.assert_same_family("claude", HOST_CLAUDE)
    host_llm.assert_same_family("agent", HOST_CURSOR)
    host_llm.assert_same_family("/usr/local/bin/cursor-agent", HOST_CURSOR)


def test_assert_same_family_refuses_claude_binary_under_cursor_host():
    with pytest.raises(host_llm.CrossHostError):
        host_llm.assert_same_family("claude", HOST_CURSOR)


def test_assert_same_family_refuses_agent_binary_under_claude_host():
    with pytest.raises(host_llm.CrossHostError):
        host_llm.assert_same_family("agent", HOST_CLAUDE)


def test_assert_same_family_unknown_host_raises_value_error():
    with pytest.raises(ValueError):
        host_llm.assert_same_family("claude", "windows")


# --- cursor_api_key_present ---------------------------------------------------------

def test_cursor_api_key_present_true_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CURSOR_API_KEY", "sk-live-123")
    assert host_llm.cursor_api_key_present(tmp_path / "nonexistent") is True


def test_cursor_api_key_present_true_from_file(monkeypatch, tmp_path):
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    key_file = tmp_path / "key"
    key_file.write_text("sk-file-456\n", encoding="utf-8")
    assert host_llm.cursor_api_key_present(key_file) is True


def test_cursor_api_key_present_false_when_missing_and_no_file(monkeypatch, tmp_path):
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    assert host_llm.cursor_api_key_present(tmp_path / "nonexistent") is False


def test_cursor_api_key_present_false_when_file_is_empty(monkeypatch, tmp_path):
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    key_file = tmp_path / "key"
    key_file.write_text("   \n", encoding="utf-8")
    assert host_llm.cursor_api_key_present(key_file) is False


# --- preflight -----------------------------------------------------------------------

def test_preflight_claude_ok_when_binary_present(monkeypatch):
    monkeypatch.setattr(host_llm.shutil, "which", lambda name: "/usr/bin/claude" if name == "claude" else None)
    ok, reason = host_llm.preflight(HOST_CLAUDE)
    assert ok is True
    assert reason == ""


def test_preflight_claude_fails_when_binary_absent(monkeypatch):
    monkeypatch.setattr(host_llm.shutil, "which", lambda name: None)
    ok, reason = host_llm.preflight(HOST_CLAUDE)
    assert ok is False
    assert "claude" in reason


def test_preflight_cursor_fails_when_no_binary(monkeypatch):
    monkeypatch.setattr(host_llm.shutil, "which", lambda name: None)
    ok, reason = host_llm.preflight(HOST_CURSOR)
    assert ok is False
    assert "PATH" in reason


def test_preflight_cursor_fails_when_binary_present_but_no_key(monkeypatch):
    monkeypatch.setattr(host_llm.shutil, "which", lambda name: "/usr/bin/agent" if name == "agent" else None)
    # cursor_api_key_present's own default arg is bound at def time, so
    # patching DEFAULT_CURSOR_API_KEY_FILE would not reach preflight's
    # no-arg call — patch the function itself instead.
    monkeypatch.setattr(host_llm, "cursor_api_key_present", lambda *a, **k: False)
    ok, reason = host_llm.preflight(HOST_CURSOR)
    assert ok is False
    assert "CURSOR_API_KEY" in reason


def test_preflight_cursor_ok_when_binary_and_key_present(monkeypatch):
    monkeypatch.setattr(host_llm.shutil, "which", lambda name: "/usr/bin/agent" if name == "agent" else None)
    monkeypatch.setenv("CURSOR_API_KEY", "sk-live-123")
    ok, reason = host_llm.preflight(HOST_CURSOR)
    assert ok is True
    assert reason == ""


def test_preflight_unknown_host_raises():
    with pytest.raises(ValueError):
        host_llm.preflight("windows")


# --- build_prompt_argv ---------------------------------------------------------------

def test_build_prompt_argv_claude_shape(monkeypatch):
    argv = host_llm.build_prompt_argv(HOST_CLAUDE, "sonnet", "do the thing")
    assert argv == ["claude", "-p", "--model", "sonnet", "do the thing"]


def test_build_prompt_argv_cursor_shape(monkeypatch):
    monkeypatch.setattr(host_llm.shutil, "which", lambda name: "/usr/bin/agent" if name == "agent" else None)
    argv = host_llm.build_prompt_argv(HOST_CURSOR, None, "do the thing")
    assert argv[0] == "/usr/bin/agent"
    assert argv[-1] == "do the thing"  # prompt is always last, both hosts
    assert "--trust" in argv and "--force" in argv and "--approve-mcps" in argv
    
    assert "--workspace" not in argv


def test_build_prompt_argv_cursor_includes_workspace_when_given(monkeypatch, tmp_path):
    monkeypatch.setattr(host_llm.shutil, "which", lambda name: "/usr/bin/agent" if name == "agent" else None)
    argv = host_llm.build_prompt_argv(HOST_CURSOR, None, "prompt", workspace=tmp_path)
    assert argv[argv.index("--workspace") + 1] == str(tmp_path)
    assert argv[-1] == "prompt"  # workspace inserted before the trailing prompt


def test_build_prompt_argv_unknown_host_raises():
    with pytest.raises(ValueError):
        host_llm.build_prompt_argv("windows", "model", "prompt")


# --- isolated_run_kwargs -------------------------------------------------------------

def test_isolated_run_kwargs_preserves_ambient_env_and_overrides_config_dir(monkeypatch):
    monkeypatch.setenv("HOST_LLM_ISOLATION_SENTINEL", "still-here")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    kwargs = host_llm.isolated_run_kwargs()
    assert kwargs["env"]["HOST_LLM_ISOLATION_SENTINEL"] == "still-here"
    assert kwargs["env"]["ANTHROPIC_API_KEY"] == "sk-ant-test"
    assert "claude-judge-sandbox" in kwargs["env"]["CLAUDE_CONFIG_DIR"]


def test_isolated_run_kwargs_cwd_and_config_dir_are_created_and_empty():
    kwargs = host_llm.isolated_run_kwargs()
    cwd = Path(kwargs["cwd"])
    config_dir = Path(kwargs["env"]["CLAUDE_CONFIG_DIR"])
    assert cwd.is_dir()
    assert config_dir.is_dir()
    assert not (cwd / "CLAUDE.md").exists()
    assert not (config_dir / "settings.json").exists()


def test_isolated_run_kwargs_reuses_one_slot_per_thread():
    """Repeated calls from one thread must not leave a directory behind each time."""
    first = host_llm.isolated_run_kwargs()
    second = host_llm.isolated_run_kwargs()
    assert first["cwd"] == second["cwd"]
    assert first["env"]["CLAUDE_CONFIG_DIR"] == second["env"]["CLAUDE_CONFIG_DIR"]


def test_isolated_run_kwargs_gives_concurrent_threads_distinct_slots():
    """`claude` writes live state into CLAUDE_CONFIG_DIR, and
    measure-marker-extractor-latency.py drives these calls through a
    ThreadPoolExecutor — so concurrent callers must not share one config root."""
    seen: list[dict] = []
    lock = threading.Lock()

    def collect():
        kwargs = host_llm.isolated_run_kwargs()
        with lock:
            seen.append(kwargs)

    threads = [threading.Thread(target=collect) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    config_dirs = {k["env"]["CLAUDE_CONFIG_DIR"] for k in seen}
    cwds = {k["cwd"] for k in seen}
    assert len(config_dirs) == 4
    assert len(cwds) == 4


def test_isolated_run_kwargs_prunes_slots_of_exited_processes(monkeypatch):
    root = host_llm._SANDBOX_ROOT
    root.mkdir(parents=True, exist_ok=True)
    dead = root / "2147483646-1"  # above /proc/sys/kernel/pid_max on Linux
    (dead / "home").mkdir(parents=True, exist_ok=True)
    live = root / f"{os.getpid()}-999999"
    (live / "home").mkdir(parents=True, exist_ok=True)

    host_llm.isolated_run_kwargs()

    assert not dead.exists()
    assert live.exists()
