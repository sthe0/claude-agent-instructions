"""Pre-trial defects #423, #424, #426: the lifecycle of a linked child and of a BLOCKED stage.

#423  a child that escalated the parent's norm is resumed once the parent has fixed the norm
      (the spawn used to mint a fresh session for every changed norm).
#424  the spawn wrapper returns after the child's final result line, also when the child or a
      grandchild keeps the child's stdout open (the wrapper used to block on stdout EOF).
#426  a stage parked BLOCKED twice keeps the node it was first parked from, so `unblock`
      restores it.

The wrapper is driven as a subprocess against a stub `claude`, as in
test_spawn_specialist_teardown.py. A test asserts the repaired behaviour with a message that
starts `BEHAVIOUR:`; every setup assertion lacks that marker.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from argparse import Namespace
from pathlib import Path

from agentctl import cli
from agentctl.state import Node
from test_child_session_reuse import (
    STAGE,
    _assert_resumed,
    _first_child,
    _isolated_env,  # noqa: F401 - autouse fixture
    _spawn,
    _start_child_engine,
    _write_transcript,
)
from test_child_contour_gate import _load_spawn
from test_marker_dispatch import _dispatch_with, _to_executing

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
WRAPPER = SCRIPTS_DIR / "spawn-specialist.py"


def ns(**kw) -> Namespace:
    return Namespace(**kw)


# --- #423: a child resumes after the parent fixed the norm it escalated -----------------


def test_i423_child_resumes_after_parent_norm_fix(tmp_path, monkeypatch):
    home, workdir, first = _first_child(monkeypatch, tmp_path)
    child = first.option("--session-id")
    store = _start_child_engine(monkeypatch, home, first)
    state = store.load(child).to_dict()
    assert state["parent_link"]["norm_digest"], "setup: the child engine adopted no handoff"
    old_digest = "0" * 64
    state["parent_link"]["norm_digest"] = old_digest
    state["difficulty"] = {
        "uid": "escalation1",
        "level": 0,
        "origin": "stage",
        "escalated_to": {"parent_session": state["parent_link"]["parent_session"],
                         "stage_index": STAGE, "norm_digest": old_digest},
    }
    store.path(child).write_text(json.dumps(state), encoding="utf-8")
    loaded = store.load(child)
    assert loaded.difficulty is not None and loaded.difficulty.escalated_to["norm_digest"] == old_digest, (
        "setup: the child's escalation did not survive a load"
    )
    _write_transcript(workdir, child)

    second = _spawn(monkeypatch, home, workdir)

    _assert_resumed(second, child)


# --- #424: the wrapper returns after the child's final result ----------------------------

FINAL_RESULT = {
    "type": "result", "subtype": "success", "is_error": False,
    "result": "COMPLETED: stub child finished", "session_id": "stub-child-session",
    "total_cost_usd": 0.0,
}
RETURN_BOUND_S = 45.0


def _marker(tag: str) -> str:
    return f"PRETRIALLIFECYCLE_{tag}_{os.getpid()}_{time.time_ns()}"


def _stub_claude(marker: str, *, hold: str, transcript: Path) -> str:
    """A `claude` that reads its prompt, writes a transcript (so the wrapper's transcript
    discovery does not wait), prints the final result line, and then keeps the child's
    stdout open for much longer than the test waits: `hold` "grandchild" through a forked
    grandchild after the stub itself exited, "child" by staying alive itself; "none" exits
    at once. Every long-lived process carries `marker` as $0 so the test can reap strays."""
    sleeper = "bash -c 'sleep 600; :' %s" % marker
    tail = {"grandchild": f"{sleeper} &", "child": sleeper, "none": ""}[hold]
    return (
        "#!/bin/bash\n"
        "cat > /dev/null\n"
        f"mkdir -p {transcript.parent}\n"
        f"echo '{{}}' > {transcript}\n"
        f"echo '{json.dumps(FINAL_RESULT)}'\n"
        f"{tail}\n"
    )


def _wrapper_env(tmp_path: Path, bin_dir: Path, config: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENTCTL_", "CLAUDE_"))}
    env.update({
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path / "home"),
        "CLAUDE_CONFIG_DIR": str(config),
        "CLAUDE_AGENT_HOME": str(config),
        "AGENT_RECURSION_DEPTH": "0",
        "AGENTCTL_MARKER_EXTRACTOR": "0",
        "SPAWN_FINAL_RESULT_GRACE_S": "1",
    })
    return env


def _run_wrapper_against_stub(tmp_path: Path, *, hold: str) -> tuple[bool, str]:
    """(returned within RETURN_BOUND_S, the wrapper's stdout). The wrapper is its own
    session so the test can kill it whole; its stdout goes to a file so the test never
    blocks on a pipe the stub's descendants hold."""
    marker = _marker(hold)
    workdir = tmp_path / "work"
    workdir.mkdir()
    config = tmp_path / "config"
    (config / "projects").mkdir(parents=True)
    skill = config / "skills" / "developer"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# stub developer skill for tests\n", encoding="utf-8")
    project = _load_spawn()._project_dir_name(str(workdir))
    transcript = config / "projects" / project / "stub-child-session.jsonl"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "claude"
    stub.write_text(_stub_claude(marker, hold=hold, transcript=transcript))
    stub.chmod(0o755)
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\n**<<this step>>** do the thing.\n")
    out = tmp_path / "wrapper.out"
    argv = [
        sys.executable, str(WRAPPER), "--kind", "developer", "--plan", str(plan),
        "--done-criterion", "stub does nothing", "--criterion-type", "measurable",
        "--complexity", "medium", "--effort", "medium", "--workdir", str(workdir),
    ]
    err = tmp_path / "wrapper.err"
    with out.open("w") as sink, err.open("w") as err_sink:
        wrapper = subprocess.Popen(
            argv, env=_wrapper_env(tmp_path, bin_dir, config), stdout=sink,
            stderr=err_sink, stdin=subprocess.DEVNULL, start_new_session=True,
        )
        try:
            try:
                wrapper.wait(timeout=RETURN_BOUND_S)
                returned = True
            except subprocess.TimeoutExpired:
                returned = False
        finally:
            if wrapper.poll() is None:
                try:
                    os.killpg(wrapper.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                wrapper.wait(timeout=10)
            subprocess.run(["pkill", "-9", "-f", marker], capture_output=True)
    return returned, out.read_text()


def test_i424_wrapper_returns_after_final_marker_when_grandchild_holds_pipe(tmp_path):
    returned, stdout = _run_wrapper_against_stub(tmp_path, hold="grandchild")

    assert returned, (
        f"BEHAVIOUR: the wrapper did not return within {RETURN_BOUND_S:.0f}s of the child's "
        "final result line while a grandchild kept the child's stdout open"
    )
    assert "COMPLETED" in stdout, (
        f"BEHAVIOUR: the wrapper returned without the child's final result: {stdout[:200]!r}"
    )


def test_i424_wrapper_returns_when_child_lingers_after_final_marker(tmp_path):
    returned, stdout = _run_wrapper_against_stub(tmp_path, hold="child")

    assert returned, (
        f"BEHAVIOUR: the wrapper did not return within {RETURN_BOUND_S:.0f}s of the child's "
        "final result line while the child itself stayed alive"
    )
    assert "COMPLETED" in stdout, (
        f"BEHAVIOUR: the wrapper returned without the child's final result: {stdout[:200]!r}"
    )


def test_stub_child_that_exits_returns_final_result(tmp_path):
    returned, stdout = _run_wrapper_against_stub(tmp_path, hold="none")

    assert returned, "the wrapper did not return for a stub child that exited at once"
    assert "COMPLETED" in stdout, f"the wrapper forwarded no final result: {stdout[:200]!r}"


# --- #426: a second park of a BLOCKED stage keeps the node it was parked from -----------


def _park(store, sid):
    return _dispatch_with(store, sid, "ESCALATE: spec ambiguity\n")


def test_i426_double_park_keeps_blocked_from(store, venued_fixtures_dir):
    _to_executing(store, "p1", venued_fixtures_dir)
    _park(store, "p1")
    assert store.load("p1").node == Node.BLOCKED.value, "setup: the first park did not block"
    assert store.load("p1").blocked_from == Node.EXECUTING.value, "setup: the first park lost its node"

    _park(store, "p1")

    parked = store.load("p1")
    assert parked.blocked_from == Node.EXECUTING.value, (
        f"BEHAVIOUR: a second park of a BLOCKED stage recorded blocked_from={parked.blocked_from!r} "
        f"instead of keeping {Node.EXECUTING.value!r}"
    )


def test_i426_unblock_after_double_park_restores_node(store, venued_fixtures_dir):
    _to_executing(store, "p2", venued_fixtures_dir)
    _park(store, "p2")
    _park(store, "p2")

    cli.cmd_unblock(ns(session="p2"), store=store)

    assert store.load("p2").node == Node.EXECUTING.value, (
        f"BEHAVIOUR: unblock after a double park left the session at {store.load('p2').node!r}"
    )


def test_single_park_unblock_keeps_base_behaviour(store, venued_fixtures_dir):
    _to_executing(store, "p3", venued_fixtures_dir)
    _park(store, "p3")
    assert store.load("p3").blocked_from == Node.EXECUTING.value

    cli.cmd_unblock(ns(session="p3"), store=store)

    after = store.load("p3")
    assert after.node == Node.EXECUTING.value and after.blocked_from is None
