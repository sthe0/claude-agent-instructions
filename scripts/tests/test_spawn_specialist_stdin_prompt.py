"""spawn-specialist.py must deliver the specialist prompt via STDIN, never as a
single argv string.

A plan-bearing prompt exceeds Linux MAX_ARG_STRLEN (32 * PAGE_SIZE = 131072 bytes,
the per-argv-string ceiling), which execve rejects with E2BIG (OSError errno 7)
BEFORE the child process starts. This is the regression for the question-provenance
dispatch that died with "OSError: [Errno 7] Argument list too long: 'claude'" on a
172 KB plan: `agentctl dispatch` -> spawn-specialist inlined the plan into the
prompt and appended the prompt to argv.

The 131072-byte per-string ceiling (which ulimit/ARG_MAX tuning does NOT raise) was
confirmed empirically on this machine (2026-07-18): one identical 215063-byte
payload sent to `claude -p` succeeded via stdin and raised OSError [Errno 7] via
argv. `fake_launch` below reproduces exactly that kernel behaviour, so the test is
red on the old argv path and green on the stdin path without spawning a real child.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"
MAX_ARG_STRLEN = 131072  # Linux: 32 * PAGE_SIZE — the single-argv-string ceiling


def _load():
    spec = importlib.util.spec_from_file_location("spawn_specialist", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeProc:
    def __init__(self) -> None:
        self.returncode = 0
        self.stdin_input = None
        self.pid = 424242

    def communicate(self, input=None):
        self.stdin_input = input
        return ('{"result": "COMPLETED: ok", "cost_usd": 0}', "")


def _run_main(monkeypatch, tmp_path, prompt_size):
    mod = _load()
    captured: dict = {}

    def fake_launch(cmd, **kwargs):
        # Faithful kernel model: execve rejects any single argv string over the
        # per-string ceiling with E2BIG before the child ever runs.
        for a in cmd:
            if len(a.encode()) > MAX_ARG_STRLEN:
                raise OSError(7, "Argument list too long", "claude")
        captured["cmd"] = list(cmd)
        captured["stdin_kw"] = kwargs.get("stdin")
        captured["proc"] = _FakeProc()
        return captured["proc"]

    # Patch only the externals main() touches on the way to (and after) the launch;
    # the argv/stdin construction under test runs for real.
    monkeypatch.setattr(mod.proc_tree, "launch_supervised", fake_launch)
    monkeypatch.setattr(mod.proc_tree, "install_teardown", lambda p: None)
    monkeypatch.setattr(mod.proc_tree, "kill_tree", lambda p: None)
    monkeypatch.setattr(mod, "_snapshot_transcripts", lambda: set())
    monkeypatch.setattr(mod, "_discover_transcript_path", lambda *a, **k: None)
    monkeypatch.setattr(mod, "permissions_digest", lambda *a, **k: "")
    monkeypatch.setattr(mod, "log_cost_entry", lambda entry: None)
    monkeypatch.setattr(mod, "deregister_child_scope", lambda *a, **k: None)
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/claude")
    sysprompt = tmp_path / "sysprompt.md"
    sysprompt.write_text("system prompt", encoding="utf-8")
    monkeypatch.setattr(mod, "composed_system_prompt_file", lambda skill: sysprompt)

    plan = tmp_path / "big-plan.md"
    plan.write_text("x" * prompt_size, encoding="utf-8")

    argv = ["spawn-specialist.py", "--kind", "developer", "--plan", str(plan),
            "--done-criterion", "done", "--criterion-type", "measurable",
            "--model", "haiku"]
    monkeypatch.setattr(sys, "argv", argv)
    rc = mod.main()
    return rc, captured


def test_big_prompt_delivered_via_stdin_not_argv(monkeypatch, tmp_path):
    # A plan well over the argv ceiling. On the old argv path launch_supervised
    # raised E2BIG here and main() never returned.
    rc, captured = _run_main(monkeypatch, tmp_path, prompt_size=MAX_ARG_STRLEN + 50_000)

    assert rc == 0
    # No single argv string carries the prompt (the fix's core invariant).
    assert all(len(a.encode()) <= MAX_ARG_STRLEN for a in captured["cmd"])
    # The stdin channel was opened...
    assert captured["stdin_kw"] is not None
    # ...and the whole prompt (plan wrapper + the >131 KB plan) rode it.
    body = captured["proc"].stdin_input
    assert body is not None
    assert len(body) > MAX_ARG_STRLEN
    assert "## Working plan" in body


def test_small_prompt_also_uses_stdin(monkeypatch, tmp_path):
    # The channel is stdin for every size, not only oversize prompts.
    rc, captured = _run_main(monkeypatch, tmp_path, prompt_size=100)

    assert rc == 0
    assert captured["stdin_kw"] is not None
    assert captured["proc"].stdin_input is not None
    # The plan text is never an argv element.
    assert not any("## Working plan" in a for a in captured["cmd"])
