"""Pre-trial defect #437: the pre-land smoke exercises the PreToolUse hook gate live.

The smoke's only live launch runs with every tool disabled, so a change to a tool-call hook
lands with no live check. The repair adds a required live check, `live:hook-gate-deny`, whose
launch enables an editing tool inside the sandbox.

A test asserts the repaired behaviour with a message that starts `BEHAVIOUR:`; every setup
assertion lacks that marker. The probe builder the repair adds is reached inside the test
body only, so this file collects on a tree that lacks it.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from lib import instruction_smoke_gate

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
LIVE_SCRIPT = SCRIPTS_DIR / "instruction-sandbox-live.py"
HOOK_CHECK = "live:hook-gate-deny"
#: `--tools` selects the built-in tools a launch has; `--allowedTools` only pre-approves
#: permissions for tools the launch already has, so it enables nothing.
TOOL_LIST_FLAGS = ("--tools",)


def _load_live_script():
    spec = importlib.util.spec_from_file_location("instruction_sandbox_live", LIVE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _enabled_tools(argv: list[str]) -> list[str]:
    """The tool names an argv hands to a tool-list flag."""
    return [
        tool
        for flag, value in zip(argv, argv[1:])
        if flag in TOOL_LIST_FLAGS
        for tool in value.replace(",", " ").split()
    ]


def test_i437_smoke_requires_live_hook_check():
    assert "live:core-marker" in instruction_smoke_gate.REQUIRED_CHECKS, (
        "setup: the existing live check is not required"
    )

    required = instruction_smoke_gate.REQUIRED_CHECKS

    assert HOOK_CHECK in required, (
        f"BEHAVIOUR: the pre-land smoke does not require a live hook check; the required "
        f"checks are {list(required)}"
    )


def test_i437_live_hook_probe_runs_with_edit_tool():
    live = _load_live_script()
    marker_argv = live.build_argv("haiku")
    assert ["--tools", ""] == marker_argv[-2:], "setup: the marker probe no longer disables its tools"

    build_hook_probe_argv = getattr(live, "build_hook_probe_argv", None)

    assert callable(build_hook_probe_argv), (
        "BEHAVIOUR: instruction-sandbox-live.py has no builder for a hook probe launch"
    )
    argv = build_hook_probe_argv("haiku")
    enabled = _enabled_tools(argv)
    assert "Edit" in enabled or "default" in enabled, (
        f"BEHAVIOUR: the hook probe launch does not enable the Edit tool: {argv}"
    )
    assert not any(flag == "--tools" and value == "" for flag, value in zip(argv, argv[1:])), (
        f"BEHAVIOUR: the hook probe launch still disables every tool: {argv}"
    )


DENY_EVENT = (
    '{"type":"user","message":{"content":[{"type":"tool_result","is_error":true,'
    '"content":"agentctl session is in node=CLASSIFIED - unclassified"}]}}'
)
APPLIED_EVENT = (
    '{"type":"user","message":{"content":[{"type":"tool_result",'
    '"content":"The file has been updated successfully."}]}}'
)


def _prompt_target(prompt: str) -> Path:
    words = [w for w in prompt.split() if w.endswith("/probe-target.py")]
    assert words, f"setup: no probe target path in the prompt: {prompt!r}"
    return Path(words[0])


def _probe(live, monkeypatch, tmp_path, stream: str, edit_file: bool) -> tuple[str, str]:
    root = tmp_path / "root"
    (root / "home").mkdir(parents=True)
    monkeypatch.setattr(live, "PROBE_DIR_PARENT", str(tmp_path))
    monkeypatch.setattr(live, "probe_target_is_gated", lambda target, env: True)

    def fake_launch(argv, env, cwd, timeout, prompt=""):
        if edit_file:
            _prompt_target(prompt).write_text("changed\n")
        return "ok", stream

    monkeypatch.setattr(live, "run_launch", fake_launch)
    return live.run_hook_probe("haiku", {}, root, 5)


def test_required_checks_include_live_hook_gate():
    assert "live:core-marker" in instruction_smoke_gate.REQUIRED_CHECKS, (
        "setup: the existing live check is not required"
    )
    verify = (SCRIPTS_DIR / "instruction-sandbox-verify.sh").read_text()

    assert HOOK_CHECK in instruction_smoke_gate.REQUIRED_CHECKS, (
        "BEHAVIOUR: the required checks do not name the live hook check"
    )
    assert HOOK_CHECK in verify, "BEHAVIOUR: the verify recipe never reports the live hook check"


def test_live_hook_probe_reports_fail_when_gate_does_not_deny(monkeypatch, tmp_path):
    live = _load_live_script()
    for name in ("ok", "applied", "nodeny", "changed"):
        (tmp_path / name).mkdir()
    kind, _ = _probe(live, monkeypatch, tmp_path / "ok", DENY_EVENT, edit_file=False)
    assert kind == "pass", f"setup: a deny with an unchanged file is not a pass: {kind}"

    applied, _ = _probe(live, monkeypatch, tmp_path / "applied", APPLIED_EVENT, edit_file=True)
    no_deny, _ = _probe(live, monkeypatch, tmp_path / "nodeny", APPLIED_EVENT, edit_file=False)
    changed, _ = _probe(live, monkeypatch, tmp_path / "changed", DENY_EVENT, edit_file=True)

    assert applied == "fail", f"BEHAVIOUR: an applied edit with no deny text was reported {applied}"
    assert no_deny == "fail", f"BEHAVIOUR: a stream without the deny text was reported {no_deny}"
    assert changed == "fail", f"BEHAVIOUR: a changed file was reported {changed} despite a deny"


def test_live_hook_probe_names_the_failure_reason(monkeypatch, tmp_path):
    live = _load_live_script()
    cases = {
        "nodeny": (APPLIED_EVENT, False, "no-gate-deny"),
        "changed": (APPLIED_EVENT, True, "file-changed"),
        "both": (DENY_EVENT, True, "deny-and-changed"),
        "notool": ('{"type":"result"}', False, "no-tool-call"),
    }
    for name, (stream, edit_file, reason) in cases.items():
        (tmp_path / name).mkdir()
        kind, detail = _probe(live, monkeypatch, tmp_path / name, stream, edit_file)
        assert kind == "fail" and detail.startswith(reason), (
            f"BEHAVIOUR: {name}: expected reason {reason}, got {kind} {detail}"
        )
    (tmp_path / "excerpt").mkdir()
    _, detail = _probe(live, monkeypatch, tmp_path / "excerpt", APPLIED_EVENT, edit_file=False)
    assert "result=" in detail and "updated successfully" in detail, (
        f"BEHAVIOUR: the failure detail carries no tool-result excerpt: {detail}"
    )


def test_core_marker_probe_keeps_tools_disabled():
    live = _load_live_script()
    argv = live.build_argv("haiku")

    assert argv[-2:] == ["--tools", ""], f"the marker probe enables tools: {argv}"
    assert "live:core-marker" in instruction_smoke_gate.REQUIRED_CHECKS


def test_hook_deny_text_matches_the_state_gate_message():
    gate = (SCRIPTS_DIR / "hook-state-gate.py").read_text()
    live = _load_live_script()
    assert 'f"agentctl session is in node=' in gate, "setup: the gate's deny message moved"

    assert f'f"{live.HOOK_DENY_TEXT}' in gate, (
        "BEHAVIOUR: HOOK_DENY_TEXT is no longer the prefix of the state gate's deny message"
    )


def test_live_hook_probe_target_is_a_real_file_in_a_removed_private_dir(monkeypatch, tmp_path):
    live = _load_live_script()
    root = tmp_path / "root"
    (root / "home").mkdir(parents=True)
    parent = tmp_path / "shm"
    parent.mkdir()
    monkeypatch.setattr(live, "PROBE_DIR_PARENT", str(parent))
    monkeypatch.setattr(live, "probe_target_is_gated", lambda target, env: True)
    seen = {}

    def fake_launch(argv, env, cwd, timeout, prompt=""):
        path = _prompt_target(prompt)
        seen["is_symlink"] = path.is_symlink()
        seen["parent"] = path.parent.parent
        return "ok", DENY_EVENT

    monkeypatch.setattr(live, "run_launch", fake_launch)
    live.run_hook_probe("haiku", {}, root, 5)

    assert seen["is_symlink"] is False, "BEHAVIOUR: the probe target is a symlink"
    assert seen["parent"] == parent, "BEHAVIOUR: the probe target is not under the private dir"
    assert list(parent.iterdir()) == [], "BEHAVIOUR: the private probe dir was not removed"


def test_live_hook_probe_unavailable_when_probe_dir_cannot_be_created(monkeypatch, tmp_path):
    live = _load_live_script()
    root = tmp_path / "root"
    (root / "home").mkdir(parents=True)
    monkeypatch.setattr(live, "PROBE_DIR_PARENT", str(tmp_path / "missing" / "parent"))
    launched = []
    monkeypatch.setattr(live, "run_launch", lambda *a, **k: launched.append(1) or ("ok", DENY_EVENT))

    kind, detail = live.run_hook_probe("haiku", {}, root, 5)

    assert kind == "unavailable" and detail == "no-probe-dir", (
        f"BEHAVIOUR: an uncreatable probe dir was reported {kind} {detail}"
    )
    assert not launched, "BEHAVIOUR: the probe launched without its private dir"


def test_live_hook_probe_unavailable_when_the_gate_exempts_the_probe_dir(monkeypatch, tmp_path):
    live = _load_live_script()
    root = tmp_path / "root"
    (root / "home").mkdir(parents=True)
    monkeypatch.setattr(live, "PROBE_DIR_PARENT", str(tmp_path))
    launched = []
    monkeypatch.setattr(live, "run_launch", lambda *a, **k: launched.append(1) or ("ok", APPLIED_EVENT))

    kind, detail = live.run_hook_probe("haiku", {"TMPDIR": str(tmp_path)}, root, 5)

    assert kind == "unavailable" and detail == "probe-dir-exempt", (
        f"BEHAVIOUR: a probe dir the gate exempts was reported {kind} {detail}"
    )
    assert not launched, "BEHAVIOUR: the probe launched against a path the gate does not guard"


def test_probe_target_gating_projects_the_childs_tmpdir_and_home_and_restores_the_parents(monkeypatch):
    import os
    import shutil
    import tempfile

    live = _load_live_script()
    for name in ("TMPDIR", "HOME", "AGENTCTL_SCRATCH_ROOTS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", "/nonexistent-parent-home")
    before = dict(os.environ)
    # A host outside any /tmp path and every scratch root: /dev/shm where it exists,
    # else a directory inside the checkout itself (removed in the finally below).
    host = "/dev/shm" if os.path.isdir("/dev/shm") else str(Path(__file__).resolve().parents[2])
    shm = os.path.realpath(host)
    probe_dir = tempfile.mkdtemp(dir=host, prefix=".probe-target-")
    target = Path(probe_dir) / "probe-target.py"
    target.write_text("x\n")
    try:
        exempt = live.probe_target_is_gated(target, {"TMPDIR": shm, "HOME": "/nonexistent-child-home"})
        plain = live.probe_target_is_gated(target, {"HOME": "/nonexistent-child-home"})
        broad_home = live.probe_target_is_gated(target, {"TMPDIR": shm, "HOME": shm})
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)

    assert exempt is False, "BEHAVIOUR: the child's TMPDIR scratch root was not applied to the gate verdict"
    assert plain is True, "BEHAVIOUR: a target under no scratch root was reported unguarded"
    assert broad_home is True, "BEHAVIOUR: the child's HOME (which drops a scratch root containing it) was not applied"
    assert dict(os.environ) == before, "BEHAVIOUR: the parent environment was not restored"


def test_hook_probe_launch_drops_the_recursion_depth_but_marker_launch_keeps_it(monkeypatch, tmp_path):
    live = _load_live_script()
    root = tmp_path / "root"
    (root / "home").mkdir(parents=True)
    monkeypatch.setenv("AGENT_RECURSION_DEPTH", "2")
    monkeypatch.setattr(live.host_llm, "_lend_auth", lambda env: "ok")
    monkeypatch.setattr(live.host_llm, "AUTHENTICATED_TOKEN_STATUSES", ("ok",))
    monkeypatch.setattr(live, "PROBE_DIR_PARENT", str(tmp_path))
    monkeypatch.setattr(live, "probe_target_is_gated", lambda target, env: True)
    seen = {}

    def fake_launch(argv, env, cwd, timeout, prompt=""):
        seen["depth"] = env.get("AGENT_RECURSION_DEPTH")
        return "ok", DENY_EVENT

    monkeypatch.setattr(live, "run_launch", fake_launch)

    live.main(["--root", str(root), "--cwd", str(root), "--expect", "x"])
    assert seen["depth"] == "2", "setup: the marker launch lost the recursion depth"

    live.main(["--root", str(root), "--cwd", str(root), "--probe", "hook-gate-deny"])

    assert seen["depth"] is None, (
        "BEHAVIOUR: the hook probe child keeps AGENT_RECURSION_DEPTH, the gate's executor bypass"
    )
