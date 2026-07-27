"""spawn-cursor-specialist.py must deliver the specialist prompt via STDIN, never
as a single argv string.

Same failure mode the Claude-side wrapper hit (see
test_spawn_specialist_stdin_prompt.py): a plan-bearing prompt exceeds Linux
MAX_ARG_STRLEN (32 * PAGE_SIZE = 131072 bytes, the per-argv-string ceiling), which
execve rejects with E2BIG (OSError errno 7) BEFORE the child process starts. The
Cursor CLI takes its prompt as a positional argument, so the wrapper inlined it the
same way and carried the same latent E2BIG.

`agent -p` reads the prompt from stdin when no positional prompt is given —
confirmed empirically on this machine against agent version 2026.06.02-8c11d9f.
`fake_launch` below reproduces the kernel's per-string ceiling, so the test is red
on the argv path and green on the stdin path without spawning a real child or
making any live Cursor API call.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "spawn-cursor-specialist.py"
MAX_ARG_STRLEN = 131072  # Linux: 32 * PAGE_SIZE — the single-argv-string ceiling


def _load():
    spec = importlib.util.spec_from_file_location("spawn_cursor_specialist", SCRIPT)
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
        return ("COMPLETED: ok", "")


def _run_main(monkeypatch, tmp_path, prompt_size):
    mod = _load()
    captured: dict = {}

    def fake_launch(cmd, **kwargs):
        # Faithful kernel model: execve rejects any single argv string over the
        # per-string ceiling with E2BIG before the child ever runs.
        for a in cmd:
            if len(a.encode()) > MAX_ARG_STRLEN:
                raise OSError(7, "Argument list too long", "agent")
        captured["cmd"] = list(cmd)
        captured["stdin_kw"] = kwargs.get("stdin")
        captured["proc"] = _FakeProc()
        return captured["proc"]

    # Patch only the externals main() touches on the way to (and after) the launch;
    # the argv/stdin construction under test runs for real.
    monkeypatch.setattr(mod.proc_tree, "launch_supervised", fake_launch)
    monkeypatch.setattr(mod.proc_tree, "install_teardown", lambda p: None)
    monkeypatch.setattr(mod.proc_tree, "kill_tree", lambda p: None)
    monkeypatch.setattr(mod, "permissions_digest", lambda *a, **k: "")
    monkeypatch.setattr(mod, "log_cost_entry", lambda entry: None)
    monkeypatch.setattr(mod, "_build_extraction", lambda *a, **k: None)
    monkeypatch.setattr(mod, "resolve_api_key", lambda *a, **k: "stub-key")
    # `agent` resolves, `timeout` does not — so the timeout prefix is off and cmd[0]
    # is the agent binary, independent of what this machine has on PATH.
    monkeypatch.setattr(
        mod.shutil, "which", lambda name: "/usr/bin/agent" if name != "timeout" else None
    )
    # skill_path() resolves against the installed agent home, not the repo; point it
    # at a stub so the test does not depend on the local install.
    skill = tmp_path / "SKILL.md"
    skill.write_text("---\nname: developer\n---\n\nspecialization body\n", encoding="utf-8")
    monkeypatch.setattr(mod, "skill_path", lambda kind: skill)

    plan = tmp_path / "big-plan.md"
    plan.write_text("x" * prompt_size, encoding="utf-8")

    argv = ["spawn-cursor-specialist.py", "--kind", "developer", "--plan", str(plan),
            "--done-criterion", "done", "--criterion-type", "measurable",
            "--workspace", str(tmp_path)]
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
