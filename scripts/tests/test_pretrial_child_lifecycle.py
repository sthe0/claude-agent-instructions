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
#: Handed to the wrapper through SPAWN_FINAL_RESULT_GRACE_S: how long it may wait for the child
#: to close its stdout after the final result line. The wrapper's default is the constant
#: FINAL_RESULT_GRACE_S; the tests shorten it so a repaired wrapper returns quickly.
GRACE_S = 1.0
#: Time a repaired wrapper may take after the stub started: the grace, plus the wrapper's own
#: start-up and teardown (taken as 15 s on a loaded machine) and a margin of the same size,
#: still far below the stub's 600 s hold, so a wrapper that blocks on EOF cannot pass.
RETURN_BOUND_S = GRACE_S + 15.0 + 15.0


def _marker(tag: str) -> str:
    return f"PRETRIALLIFECYCLE_{tag}_{os.getpid()}_{time.time_ns()}"


def _stub_claude(marker: str, *, hold: str, transcript: Path) -> str:
    """A `claude` that reads its prompt, writes a transcript (so the wrapper's transcript
    discovery does not wait), prints the final result line, and then keeps the child's
    stdout open for much longer than the test waits: `hold` "grandchild" through a forked
    grandchild after the stub itself exited, "child" by staying alive itself; "none" exits
    at once. The process that holds the pipe is the `sleep` itself, exec'd under `marker` as
    its argv[0], so the test's `pkill -f marker` reaps exactly that process and no
    unmarked sleeper outlives the run."""
    sleeper = f"exec -a {marker} sleep 600"
    tail = {"grandchild": f"( {sleeper} ) &", "child": sleeper, "none": ""}[hold]
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
        "SPAWN_FINAL_RESULT_GRACE_S": str(GRACE_S),
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


# --- #423: what the resume renews, and where it stays a fresh session -------------------

from agentctl import norm_handoff  # noqa: E402
from test_child_contour_gate import HANDOFF_ENV  # noqa: E402
from test_child_session_reuse import _assert_fresh  # noqa: E402


def _escalated_child(monkeypatch, tmp_path, *, node: str, approved: bool):
    """The first child's engine session after it escalated the parent's norm: its adopted
    link and its `escalated_to` both name a norm digest the parent has since changed, and
    it stands at `node` with its plan approved or not."""
    home, workdir, first = _first_child(monkeypatch, tmp_path)
    child = first.option("--session-id")
    store = _start_child_engine(monkeypatch, home, first)
    state = store.load(child).to_dict()
    old_digest = "0" * 64
    state["parent_link"]["norm_digest"] = old_digest
    state["difficulty"] = {
        "uid": "escalation1", "level": 0, "origin": "stage",
        "escalated_to": {"parent_session": state["parent_link"]["parent_session"],
                         "stage_index": STAGE, "norm_digest": old_digest},
    }
    state["node"] = node
    state["approval"]["passed"] = approved
    store.path(child).write_text(json.dumps(state), encoding="utf-8")
    _write_transcript(workdir, child)
    return home, workdir, first, child, store, old_digest


def test_resume_refreshes_link_and_requires_reapproval(monkeypatch, tmp_path):
    home, workdir, first, child, store, old_digest = _escalated_child(
        monkeypatch, tmp_path, node=Node.APPROVED.value, approved=True)

    second = _spawn(monkeypatch, home, workdir)

    _assert_resumed(second, child)
    resumed = store.load(child)
    assert resumed.parent_link["norm_digest"] != old_digest, (
        "BEHAVIOUR: the resumed child's parent_link still names the norm the parent fixed"
    )
    handed = norm_handoff.verify_env_value(second.env[HANDOFF_ENV])[0]
    assert handed["norm_digest"] != old_digest, (
        "BEHAVIOUR: the resumed child was handed the handoff of the old norm"
    )
    assert handed["norm_digest"] == resumed.parent_link["norm_digest"], (
        "BEHAVIOUR: the handoff the resumed child reads differs from its own parent_link"
    )
    assert not resumed.approval.passed and resumed.node == Node.PLAN_READY.value, (
        f"BEHAVIOUR: the resumed child kept its approval of the old norm "
        f"(node={resumed.node}, approval.passed={resumed.approval.passed})"
    )


def test_unescalated_norm_change_mints_fresh_session(monkeypatch, tmp_path):
    home, workdir, first = _first_child(monkeypatch, tmp_path)
    child = first.option("--session-id")
    store = _start_child_engine(monkeypatch, home, first)
    stale = store.load(child).to_dict()
    stale["parent_link"]["norm_digest"] = "0" * 64
    store.path(child).write_text(json.dumps(stale), encoding="utf-8")
    _write_transcript(workdir, child)

    second = _spawn(monkeypatch, home, workdir)

    _assert_fresh(second, child)


def test_escalation_of_another_norm_mints_fresh_session(monkeypatch, tmp_path):
    home, workdir, first, child, store, old_digest = _escalated_child(
        monkeypatch, tmp_path, node=Node.DIAGNOSING.value, approved=False)
    state = store.load(child).to_dict()
    state["difficulty"]["escalated_to"]["norm_digest"] = "f" * 64
    store.path(child).write_text(json.dumps(state), encoding="utf-8")

    second = _spawn(monkeypatch, home, workdir)

    _assert_fresh(second, child)


# --- #424: pins of what the wrapper keeps doing ------------------------------------------


def _run_wrapper(tmp_path: Path, build_stub) -> tuple[bool, str, "int | None"]:
    """(returned within RETURN_BOUND_S, the wrapper's stdout, its exit code) for a wrapper
    run against the stub `build_stub(transcript)` returns."""
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
    stub.write_text(build_stub(transcript))
    stub.chmod(0o755)
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\n**<<this step>>** do the thing.\n")
    out = tmp_path / "wrapper.out"
    argv = [
        sys.executable, str(WRAPPER), "--kind", "developer", "--plan", str(plan),
        "--done-criterion", "stub does nothing", "--criterion-type", "measurable",
        "--complexity", "medium", "--effort", "medium", "--workdir", str(workdir),
    ]
    with out.open("w") as sink, (tmp_path / "wrapper.err").open("w") as err_sink:
        wrapper = subprocess.Popen(
            argv, env=_wrapper_env(tmp_path, bin_dir, config), stdout=sink,
            stderr=err_sink, stdin=subprocess.DEVNULL, start_new_session=True,
        )
        try:
            try:
                wrapper.wait(timeout=RETURN_BOUND_S)
            except subprocess.TimeoutExpired:
                pass
        finally:
            returned = wrapper.poll() is not None
            if not returned:
                try:
                    os.killpg(wrapper.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                wrapper.wait(timeout=10)
    return returned, out.read_text(), wrapper.returncode


def test_wrapper_does_not_kill_child_before_final_result(tmp_path):
    finished = tmp_path / "child-finished"
    delay = GRACE_S + 2.0

    def stub(transcript: Path) -> str:
        return (
            "#!/bin/bash\n"
            "cat > /dev/null\n"
            f"mkdir -p {transcript.parent}\n"
            f"echo '{{}}' > {transcript}\n"
            f"sleep {delay}\n"
            f"echo '{json.dumps(FINAL_RESULT)}'\n"
            f"touch {finished}\n"
        )

    returned, stdout, _ = _run_wrapper(tmp_path, stub)

    assert returned, "the wrapper did not return for a child that finished after a pause"
    assert finished.exists(), "the wrapper reaped a child that had not yet printed its result"
    assert "COMPLETED" in stdout, f"the wrapper forwarded no final result: {stdout[:200]!r}"


def test_wrapper_normal_exit_result_as_base(tmp_path):
    def stub(transcript: Path) -> str:
        return _stub_claude(_marker("normal"), hold="none", transcript=transcript)

    returned, stdout, returncode = _run_wrapper(tmp_path, stub)

    assert returned, "the wrapper did not return for a child that exited at once"
    assert returncode == 0, f"the wrapper exited {returncode} for a child that succeeded"
    assert "COMPLETED: stub child finished" in stdout, f"result text not forwarded: {stdout[:200]!r}"


# --- #423: the escalation must be addressed to this parent session and stage -------------


def _fresh_after_escalation_rewritten(monkeypatch, tmp_path, **escalated_to):
    home, workdir, first, child, store, old_digest = _escalated_child(
        monkeypatch, tmp_path, node=Node.DIAGNOSING.value, approved=False)
    state = store.load(child).to_dict()
    state["difficulty"]["escalated_to"].update(escalated_to)
    store.path(child).write_text(json.dumps(state), encoding="utf-8")

    second = _spawn(monkeypatch, home, workdir)

    _assert_fresh(second, child)


def test_escalation_to_another_parent_session_mints_fresh_session(monkeypatch, tmp_path):
    _fresh_after_escalation_rewritten(monkeypatch, tmp_path, parent_session="another-parent")


def test_escalation_to_another_stage_mints_fresh_session(monkeypatch, tmp_path):
    _fresh_after_escalation_rewritten(monkeypatch, tmp_path, stage_index=STAGE + 1)


# --- #423: a resumed child that stands at DIAGNOSING re-approves instead of crashing ----


def _diagnosing_child_with_closed_cycle(store, venued_fixtures_dir, sid: str) -> None:
    from test_difficulty import (
        _critique, _declare, _investigate, _normalize, _to_failed_stage1,
    )

    _to_failed_stage1(store, sid, str(venued_fixtures_dir / "plan_two_stage.toml"))
    _declare(store, sid)
    _investigate(store, sid)
    _critique(store, sid)
    _normalize(store, sid)


def test_resumed_diagnosing_child_reapproves_after_refinement_replan(
        store, venued_fixtures_dir, tmp_path):
    from agentctl.stage_norm import StageNorm

    sid = "resumed-diagnosing"
    _diagnosing_child_with_closed_cycle(store, venued_fixtures_dir, sid)
    state = store.load(sid)
    assert state.node == Node.DIAGNOSING.value and state.approval.passed, "setup"
    record = norm_handoff.build_record(
        parent_session="parent", stage_index=1, kind="developer",
        norm=StageNorm.from_stage(state.stage(1)), grants=[], depth=1, parent_lineage=None)
    path, sha = norm_handoff.write_record(tmp_path / "handoffs", record)
    _load_spawn().refresh_resumed_child_link(
        store, state, norm_handoff.format_env_value(path, sha))
    state = store.load(sid)
    assert state.node == Node.DIAGNOSING.value and not state.approval.passed, "setup"

    cli.cmd_replan(ns(session=sid, plan=str(venued_fixtures_dir / "plan_two_stage_refined.toml")),
                   store=store)

    state = store.load(sid)
    assert state.node == Node.PLAN_READY.value and not state.approval.passed, (
        f"BEHAVIOUR: a refinement replan of the resumed child left node={state.node} "
        f"approval.passed={state.approval.passed} instead of asking for a new approval"
    )
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)
    assert store.load(sid).node == Node.EXECUTING.value, (
        "BEHAVIOUR: the re-approved resumed child cannot reach EXECUTING"
    )


# --- #424: the exit code after the wrapper reaped a child that outlived its result -------


def _reaped_child_run(tmp_path: Path, *, is_error: bool) -> "tuple[bool, int | None, bool]":
    """(wrapper returned, its exit code, a process of the child's group still alive) for a
    stub that prints a final result with `is_error` and then stays alive past the grace."""
    marker = _marker("reaped")
    result = {**FINAL_RESULT, "is_error": is_error}

    def stub(transcript: Path) -> str:
        return (
            "#!/bin/bash\n"
            "cat > /dev/null\n"
            f"mkdir -p {transcript.parent}\n"
            f"echo '{{}}' > {transcript}\n"
            f"echo '{json.dumps(result)}'\n"
            f"exec -a {marker} sleep 600\n"
        )

    try:
        returned, _, returncode = _run_wrapper(tmp_path, stub)
        alive = subprocess.run(["pgrep", "-f", marker], capture_output=True).returncode == 0
    finally:
        subprocess.run(["pkill", "-9", "-f", marker], capture_output=True)
    return returned, returncode, alive


def test_reaped_child_exit_code_follows_its_successful_result(tmp_path):
    returned, returncode, alive = _reaped_child_run(tmp_path, is_error=False)

    assert returned, "BEHAVIOUR: the wrapper did not return for a child that outlived its result"
    assert returncode == 0, (
        f"BEHAVIOUR: the wrapper exited {returncode} for a successful result of a reaped child"
    )
    assert not alive, "BEHAVIOUR: the reaped child's process is still alive"


def test_reaped_child_exit_code_follows_its_error_result(tmp_path):
    returned, returncode, alive = _reaped_child_run(tmp_path, is_error=True)

    assert returned, "BEHAVIOUR: the wrapper did not return for a child that outlived its result"
    assert returncode == 1, (
        f"BEHAVIOUR: the wrapper exited {returncode} for an error result of a reaped child"
    )
    assert not alive, "BEHAVIOUR: the reaped child's process is still alive"


# --- #424: the grace override rejects non-finite values -----------------------------------


def test_final_result_grace_rejects_non_finite_override():
    spawn = _load_spawn()
    for raw in ("nan", "inf", "-inf", "-1", "abc", ""):
        got = spawn.final_result_grace_s({spawn.FINAL_RESULT_GRACE_ENV: raw})
        assert got == spawn.FINAL_RESULT_GRACE_S, f"{raw!r} produced a grace of {got}"
    assert spawn.final_result_grace_s({spawn.FINAL_RESULT_GRACE_ENV: "2.5"}) == 2.5
