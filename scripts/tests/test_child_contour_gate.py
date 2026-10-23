"""A linked child has an engine contour of its own: it is gated by its own session and
moves that session only through `scripts/agentctl-own.py`.

A spawn for a parent stage hands the child a norm handoff, a pre-assigned session id and
the one permission rule that runs the channel script. Three layers carry the contour and
each is pinned here:

* the PreToolUse gate (`hook-state-gate.py`) releases a linked child's production edits
  only at EXECUTING / VERIFYING / RESOLUTION -- the depth bypass an unlinked specialist
  keeps is not available to it, and an own-contour signal in the env of a session that
  holds no handoff is denied rather than trusted;
* the channel (`agentctl-own.py`) fixes the session and the verb set, so a prefix rule
  over it grants what a user-authority verb rule never could;
* the spawn (`spawn-specialist.py`) exports the session id the channel is pinned to,
  names the contour in the brief, and leaves an unlinked spawn as it was.

Four tests -- `test_linked_child_edit_denied_outside_execution`,
`test_linked_spawn_exports_own_session`, `test_linked_brief_names_own_contour`,
`test_own_session_env_without_link_is_denied` -- are the stage's negative control, and
three -- `test_unlinked_depth_session_keeps_bypass`, `test_unlinked_spawn_unchanged`,
`test_linked_child_final_turn_not_blocked` -- pin what must stay as on the base tree.
They drive only entry points the base tree has and put `BEHAVIOUR:` on the one assertion
that checks the behaviour under repair, so on base they fail there rather than at
collection or setup. The channel script is never imported at module level."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, norm_handoff
from agentctl.dispatch import RunResult
from agentctl.state import SessionState
from agentctl.stage_norm import StageNorm
from agentctl.store import FileStateStore

SCRIPTS = Path(__file__).resolve().parent.parent
HOOK = SCRIPTS / "hook-state-gate.py"
ENGINE_START_HOOK = SCRIPTS / "hook-engine-start.py"
TURN_END_HOOK = SCRIPTS / "hook-turn-end-gate.py"
SPAWN_SCRIPT = SCRIPTS / "spawn-specialist.py"
OWN_SCRIPT = SCRIPTS / "agentctl-own.py"
FIXTURE_PLAN = Path(__file__).resolve().parent / "fixtures" / "plan_two_stage.toml"

OWN_ENV = "AGENTCTL_OWN_SESSION"
HANDOFF_ENV = norm_handoff.NORM_HANDOFF_ENV
PARENT = "parent-s"
CHILD = "child-s"
OTHER = "other-s"
EDIT_PATH = "/work/project/module.py"
REFUSED = "agentctl-own: refused"

OWN_CONTOUR_VERBS = {
    "classify", "status", "plan", "next-stage", "submit-plan", "plan-review",
    "plan-review-delta", "approve", "partition", "dispatch", "record-result",
    "code-review", "verify-final", "declare", "investigate", "critique",
    "normalize", "replan", "stage-grants", "plan-grants", "question-raise",
    "question-dispose", "question-research", "question-candidate-dispose",
    "order-raise", "order-dispose",
    "plan-review-walk", "plan-review-pair-history", "plan-review-compose",
}
PARTITION_MARKER_FLAGS = ("--m1", "--m2", "--m3", "--m4", "--m3-severe", "--m4-severe")
NAMED_EXCLUDED_VERBS = (
    "resolve", "resolve-permission", "reset", "start", "task-reset", "fire-acknowledge",
    "confirm-delivery", "present-plan", "accept", "risk-accept", "reject", "stage-review",
    "push-subplan", "pop-subplan", "partition-units", "close", "block", "unblock", "drive",
    "difficulty-open", "difficulty-close",
)
_AMBIENT_ENV = (
    "CLAUDE_CONFIG_DIR", "CLAUDE_AGENT_HOME", "AGENT_RECURSION_DEPTH", HANDOFF_ENV, OWN_ENV,
    "CLAUDE_CODE_SESSION_ID", "AGENTCTL_PARENT_DIFFICULTY",
)


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    for name in _AMBIENT_ENV:
        monkeypatch.delenv(name, raising=False)
    norms = tmp_path / "norms"
    norms.mkdir()
    monkeypatch.setenv("AGENTCTL_NORM_ROOTS", str(norms))
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")


def spawn_handoff(state_root, record, child_session):
    """Mirror of conftest.spawn_handoff, which this file does not import."""
    path, sha = norm_handoff.write_record(state_root, record)
    norm_handoff.write_child_session(
        state_root, record["parent_session"], record["stage_index"],
        session=child_session, workdir="/tmp/spawn-workdir")
    return norm_handoff.format_env_value(path, sha)


def _state_dir(home: Path) -> Path:
    return home / ".claude" / "agentctl" / "state"


def _child_env(home: Path, **extra: str) -> dict:
    """The environment a hook or the channel runs in: this process's gate toggles, a
    private HOME (so state resolves under `home`), and no ambient session signals."""
    env = {k: v for k, v in os.environ.items() if k not in _AMBIENT_ENV}
    env["HOME"] = str(home)
    env.update(extra)
    return env


def _write_state(home: Path, session: str, node: str, *, linked: bool,
                 weight_class: str | None = "SUBSTANTIVE") -> None:
    directory = _state_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    data: dict = {"node": node}
    if weight_class is not None:
        data["weight_class"] = weight_class
    if linked:
        data["parent_link"] = {
            "parent_session": PARENT, "stage_index": 1, "norm_digest": "0" * 64,
            "grants": [], "depth": 1,
        }
    (directory / f"{session}.json").write_text(json.dumps(data), encoding="utf-8")


def _edit(home: Path, session: str, *, depth: str | None = "1", **env: str) -> subprocess.CompletedProcess:
    payload = {"tool_name": "Edit", "session_id": session, "tool_input": {"file_path": EDIT_PATH}}
    extra = dict(env)
    if depth is not None:
        extra["AGENT_RECURSION_DEPTH"] = depth
    return subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True,
        text=True, env=_child_env(home, **extra),
    )


def _denied(proc: subprocess.CompletedProcess) -> bool:
    if proc.returncode != 0 or not proc.stdout.strip():
        return False
    out = json.loads(proc.stdout)
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def _reason(proc: subprocess.CompletedProcess) -> str:
    return json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


# --- the gate ------------------------------------------------------------------------


def test_linked_child_edit_denied_outside_execution(tmp_path):
    for node, weight in (("CLASSIFIED", "SUBSTANTIVE"), ("PLANNING", "SUBSTANTIVE"),
                         ("PLAN_READY", "SUBSTANTIVE"), ("APPROVED", "SUBSTANTIVE"),
                         ("PARTITIONED", "SUBSTANTIVE"), ("ROUTED", "CHAT"),
                         ("DIAGNOSING", "SUBSTANTIVE"), ("RESOLVED", "SUBSTANTIVE"),
                         ("BLOCKED", "SUBSTANTIVE")):
        _write_state(tmp_path, CHILD, node, linked=True, weight_class=weight)
        proc = _edit(tmp_path, CHILD, **{OWN_ENV: CHILD})
        assert proc.returncode == 0
        assert _denied(proc), (
            f"BEHAVIOUR: a linked child at {node} ({weight}) was allowed a production "
            f"edit through the depth bypass"
        )
        assert "agentctl-own.py" in _reason(proc), (
            f"the denial at {node} does not name the channel the child moves its node with"
        )


def test_linked_child_edit_allowed_in_execution(tmp_path):
    for node in ("EXECUTING", "VERIFYING", "RESOLUTION"):
        _write_state(tmp_path, CHILD, node, linked=True)
        proc = _edit(tmp_path, CHILD, **{OWN_ENV: CHILD})
        assert proc.returncode == 0 and not _denied(proc), (
            f"a linked child at {node} was denied: {proc.stdout!r}"
        )


def test_unlinked_depth_session_keeps_bypass(tmp_path):
    """A spawned specialist whose engine session holds no handoff is gated as it was:
    the depth bypass releases its edits wherever its own node is."""
    _write_state(tmp_path, "specialist-s", "PLAN_READY", linked=False)
    proc = _edit(tmp_path, "specialist-s")
    assert proc.returncode == 0
    assert not _denied(proc), f"an unlinked depth-1 session was denied: {proc.stdout!r}"


def test_own_session_env_without_link_is_denied(tmp_path, monkeypatch):
    _write_state(tmp_path, "unlinked-s", "EXECUTING", linked=False)
    proc = _edit(tmp_path, "unlinked-s", **{OWN_ENV: "unlinked-s"})
    assert _denied(proc), (
        "BEHAVIOUR: a session holding no handoff was allowed to edit with "
        f"{OWN_ENV} in its env"
    )
    assert "parent_link" in _reason(proc)

    assert _denied(_edit(tmp_path, "unlinked-s", **{HANDOFF_ENV: "/nowhere/h.json:" + "0" * 64})), (
        f"a handoff in the env of a session that holds none was trusted"
    )
    assert _denied(_edit(tmp_path, "never-started", **{OWN_ENV: "never-started"})), (
        "a contour that never started (no engine state) was allowed to edit"
    )
    assert not _denied(_edit(tmp_path, "never-started")), (
        "a session with no engine state and no contour signal must stay as it was"
    )

    _write_state(tmp_path, CHILD, "EXECUTING", linked=True)
    assert _denied(_edit(tmp_path, CHILD, **{OWN_ENV: OTHER})), (
        f"{OWN_ENV} naming another session was accepted for this one"
    )
    assert not _denied(_edit(tmp_path, CHILD, **{OWN_ENV: CHILD}))

    # state that cannot be read does not make a contour signal trustworthy
    (_state_dir(tmp_path) / "corrupt-s.json").write_text("{not json", encoding="utf-8")
    assert _denied(_edit(tmp_path, "corrupt-s", **{OWN_ENV: "corrupt-s"})), (
        f"BEHAVIOUR: a session whose state is unreadable was allowed to edit with {OWN_ENV} in its env"
    )
    assert _denied(_edit(tmp_path, "corrupt-s", **{HANDOFF_ENV: "/nowhere/h.json:" + "0" * 64}))
    assert not _denied(_edit(tmp_path, "corrupt-s")), (
        "unreadable state with no contour signal must stay allowed, as it was"
    )

    # the signals reach a child only from its own spawn: an unlinked spawn clears both
    _parent_session(tmp_path / "home")
    monkeypatch.setenv(OWN_ENV, "stale-spawner-session")
    monkeypatch.setenv(HANDOFF_ENV, "/nowhere/h.json:" + "0" * 64)
    launched = _launch(monkeypatch, tmp_path, stage_index=None)
    assert launched.env is not None, f"setup: the spawn did not reach the launch (rc={launched.rc})"
    assert OWN_ENV not in launched.env and HANDOFF_ENV not in launched.env, (
        "an unlinked spawn passed the spawner's own-contour signals on to its child"
    )


# --- the channel ---------------------------------------------------------------------


def _load_channel():
    spec = importlib.util.spec_from_file_location("agentctl_own_under_test", OWN_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _own(home: Path, *argv: str, session: str | None = CHILD, **env: str) -> subprocess.CompletedProcess:
    extra = dict(env)
    if session is not None:
        extra[OWN_ENV] = session
    return subprocess.run(
        [sys.executable, str(OWN_SCRIPT), *argv], capture_output=True, text=True,
        env=_child_env(home, **extra),
    )


def _cli(root: Path, cmd: str, **flags) -> dict:
    """One engine command through `cli.main` against the store at `root`, as a parent or
    a spawn's autostart would run it -- the channel itself is always a subprocess."""
    argv = ["--state-root", str(root), cmd]
    for name, value in flags.items():
        flag = "--" + name.replace("_", "-")
        if value is True:
            argv.append(flag)
        elif value is not None and value is not False:
            argv.extend([flag, str(value)])
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = cli.main(argv)
        except SystemExit as exit_:
            raise AssertionError(f"setup: `{cmd}` exited {exit_.code}: {err.getvalue()}") from exit_
    result = json.loads(out.getvalue())
    result["rc"] = rc
    return result


def _expect_ok(proc: subprocess.CompletedProcess, what: str) -> dict:
    assert proc.returncode == 0, f"{what}: rc={proc.returncode} stderr={proc.stderr!r} stdout={proc.stdout[:400]!r}"
    result = json.loads(proc.stdout)
    assert result.get("ok") is True, f"{what}: {result}"
    return result


class Contour:
    """A parent session at EXECUTING stage 1 and a child session linked to its norm, both
    in the state directory under a private HOME that the hook and the channel read."""

    def __init__(self, tmp_path: Path, monkeypatch):
        from conftest import declare_venue
        from test_child_approval import PARENT_CHECK, _covering_grants, _parent_plan, _spliced

        self.tmp = tmp_path
        self.home = tmp_path / "home"
        self.root = _state_dir(self.home)
        self.store = FileStateStore(self.root)
        parent_plan = _parent_plan(tmp_path)
        _cli(self.root, "start", session=PARENT, task="parent", goal="g", done_criterion="dc")
        _cli(self.root, "classify", session=PARENT, architectural=True, files=5,
             changed_lines=200, wall_clock_min=60)
        _cli(self.root, "plan", session=PARENT)
        _cli(self.root, "submit-plan", session=PARENT, plan=parent_plan)
        _cli(self.root, "approve", session=PARENT, by="user")
        _cli(self.root, "partition", session=PARENT)
        _cli(self.root, "next-stage", session=PARENT)
        self.norm = StageNorm.from_stage(self.store.load(PARENT).stage(1))
        self.plan = str(tmp_path / "child.toml")
        Path(self.plan).write_text(
            declare_venue(_spliced(parent_norm=self.norm.norm_digest(),
                                   final_checks=((PARENT_CHECK, 0),)), tmp_path / "work"),
            encoding="utf-8",
        )
        record = norm_handoff.build_record(
            parent_session=PARENT, stage_index=1, kind="developer", norm=self.norm,
            grants=_covering_grants(self.plan), depth=1, parent_lineage=None,
        )
        self.handoff = spawn_handoff(self.root, record, CHILD)
        monkeypatch.setenv(HANDOFF_ENV, self.handoff)
        _cli(self.root, "start", session=CHILD, task="child", goal="g", done_criterion="dc")
        monkeypatch.delenv(HANDOFF_ENV)

    def own(self, *argv: str, session: str | None = CHILD, **env: str) -> subprocess.CompletedProcess:
        return _own(self.home, *argv, session=session, **env)

    def state_bytes(self, session: str) -> bytes:
        return (self.root / f"{session}.json").read_bytes()

    def to_executing(self) -> None:
        env = {"AGENTCTL_PLAN_REVIEW": "1"}
        digest = hashlib.sha256(Path(self.plan).read_bytes()).hexdigest()
        steps = [
            ("classify", "--architectural", "--files", "5", "--changed-lines", "200",
             "--wall-clock-min", "60"),
            ("plan",),
            ("submit-plan", "--plan", self.plan),
            ("plan-review", "--verdict", "pass", "--reviewer", "thinker",
             "--target", self.plan, "--plan-digest", digest),
            ("approve", "--by", "agent"),
            ("partition",),
            ("next-stage",),
        ]
        for step in steps:
            _expect_ok(self.own(*step, **env), f"own channel `{step[0]}`")


@pytest.fixture
def contour(tmp_path, monkeypatch):
    return Contour(tmp_path, monkeypatch)


def test_own_channel_refuses_explicit_session(contour):
    _cli(contour.root, "start", session=OTHER, task="other", goal="g", done_criterion="dc")
    before = {s: contour.state_bytes(s) for s in (CHILD, OTHER, PARENT)}
    for session_args in (["--session", OTHER], [f"--session={OTHER}"], ["--sess", OTHER],
                         ["--se", OTHER], [f"--sess={OTHER}"]):
        proc = contour.own("status", *session_args)
        assert proc.returncode == 2, (session_args, proc.stdout, proc.stderr)
        assert REFUSED in proc.stderr and "session" in proc.stderr, (session_args, proc.stderr)
        assert proc.stdout == "", f"the engine was reached with {session_args}: {proc.stdout!r}"
    assert {s: contour.state_bytes(s) for s in before} == before


def test_own_channel_refuses_missing_env(contour):
    before = contour.state_bytes(CHILD)
    proc = contour.own("status", session=None)
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert REFUSED in proc.stderr and OWN_ENV in proc.stderr
    assert proc.stdout == ""
    blank = contour.own("status", session="   ")
    assert blank.returncode == 2 and REFUSED in blank.stderr
    assert contour.state_bytes(CHILD) == before


def test_own_channel_refuses_unlinked_session(contour):
    _cli(contour.root, "start", session="unlinked-s", task="u", goal="g", done_criterion="dc")
    before = contour.state_bytes("unlinked-s")
    for argv in (("status",), ("classify", "--changed-lines", "1", "--files", "1")):
        proc = contour.own(*argv, session="unlinked-s")
        assert proc.returncode == 2, (argv, proc.stdout, proc.stderr)
        assert REFUSED in proc.stderr and "parent_link" in proc.stderr
        assert proc.stdout == ""
    assert contour.state_bytes("unlinked-s") == before
    ghost = contour.own("status", session="no-such-session")
    assert ghost.returncode == 2 and REFUSED in ghost.stderr and "no engine session" in ghost.stderr


def _agentctl_subcommands() -> set[str]:
    import argparse

    for action in cli.build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    raise AssertionError("agentctl's parser declares no subcommands")


def test_own_channel_refuses_excluded_verb(contour):
    excluded = sorted(set(NAMED_EXCLUDED_VERBS) | (_agentctl_subcommands() - OWN_CONTOUR_VERBS))
    for prefix in ("mandate-", "plugin-"):
        assert any(v.startswith(prefix) for v in excluded), f"no {prefix}* verb is checked"
    assert {"resolve", "resolve-permission", "partition-units", "reset", "start"} <= set(excluded)
    before = {s: contour.state_bytes(s) for s in (CHILD, PARENT)}
    for verb in excluded:
        proc = contour.own(verb)
        assert proc.returncode == 2, (verb, proc.returncode, proc.stdout, proc.stderr)
        assert REFUSED in proc.stderr and "outside the own contour" in proc.stderr, (verb, proc.stderr)
        assert proc.stdout == "", f"`{verb}` reached the engine: {proc.stdout!r}"
    assert {s: contour.state_bytes(s) for s in before} == before


def test_own_channel_verb_set_is_exact(contour):
    channel = _load_channel()
    assert set(channel.OWN_CONTOUR_VERBS) == OWN_CONTOUR_VERBS
    assert OWN_CONTOUR_VERBS <= _agentctl_subcommands(), "a verb of the set is no agentctl command"
    assert "partition-units" not in OWN_CONTOUR_VERBS
    before = {s: contour.state_bytes(s) for s in (CHILD, PARENT)}
    for flag in (*PARTITION_MARKER_FLAGS, "--unit"):
        argv = ["partition", flag] + (["1:routing"] if flag == "--unit" else [])
        proc = contour.own(*argv)
        assert proc.returncode == 2, (flag, proc.stdout, proc.stderr)
        assert REFUSED in proc.stderr and "stays the root's" in proc.stderr, (flag, proc.stderr)
        assert proc.stdout == ""
    for argv in (("approve", "--by", "user"), ("approve", "--by=user"),
                 ("approve", "--by", "agentx"), ("approve", "--by", "agent", "--by", "user")):
        proc = contour.own(*argv)
        assert proc.returncode == 2 and "available only as" in proc.stderr, (argv, proc.stderr)
        assert proc.stdout == ""
    # an abbreviation of a declared option is how a checked flag arrives unchecked: the
    # parser the channel runs matches exact spellings only, so none of these reaches the
    # engine, whatever value the abbreviated flag carries
    for argv in (("partition", "--m3-sev"), ("partition", "--un=a b"), ("partition", "--unit=x"),
                 ("partition", "--un", "x"), ("approve", "--by", "agent", "--b=user x"),
                 ("approve", "--b", "user"), ("approve", "--by", "agent", "--skip-opt", "1"),
                 ("status", "--no-such-option"), ("approve",), ("approve", "--by")):
        proc = contour.own(*argv)
        assert proc.returncode == 2, (argv, proc.returncode, proc.stdout, proc.stderr)
        assert REFUSED in proc.stderr, (argv, proc.stderr)
        assert proc.stdout == "", f"{argv} reached the engine: {proc.stdout!r}"
    assert {s: contour.state_bytes(s) for s in before} == before, (
        "a refused call changed an engine state file"
    )
    # `--by agent` passes the wrapper's own checks: what answers here is the engine
    reached = contour.own("approve", "--by", "agent")
    assert "available only as" not in reached.stderr and REFUSED not in reached.stderr
    assert reached.stdout.strip(), "approve --by agent never reached the engine"


def test_own_channel_reaches_own_session(contour):
    parent_before = contour.state_bytes(PARENT)
    result = _expect_ok(contour.own("status"), "own channel `status`")
    link = result["data"].get("parent_link")
    assert link and link["parent_session"] == PARENT, (
        f"the directive is not the child's own (no handoff link): {result}"
    )
    assert result["node"] != "EXECUTING"
    classified = _expect_ok(
        contour.own("classify", "--architectural", "--files", "5", "--changed-lines", "200",
                    "--wall-clock-min", "60"),
        "own channel `classify`",
    )
    assert classified["node"] != "EXECUTING"
    child = contour.store.load(CHILD)
    assert child.weight_class == "SUBSTANTIVE", "classify did not land on the child's own session"
    assert child.parent_link is not None
    assert contour.state_bytes(PARENT) == parent_before, "the channel wrote the parent's session"


def test_own_channel_approves_on_parent_norm(contour):
    contour.to_executing()
    child = contour.store.load(CHILD)
    assert child.node == "EXECUTING", child.node
    approvals = [h for h in child.history if h.get("event") == "approve"]
    assert approvals and approvals[-1].get("basis") == "parent_norm", approvals
    assert approvals[-1].get("by") == "agent"
    assert child.parent_link["parent_session"] == PARENT
    proc = _edit(contour.home, CHILD, **{OWN_ENV: CHILD, HANDOFF_ENV: contour.handoff})
    assert proc.returncode == 0 and not _denied(proc), (
        f"the hook still denies the child's edit at EXECUTING: {proc.stdout!r}"
    )


def test_own_channel_reaches_parent_norm_critique(contour):
    contour.to_executing()
    failed = contour.own("record-result", "--status", "failed", "--actual", "boom")
    assert failed.returncode == 1, (failed.returncode, failed.stdout, failed.stderr)
    assert json.loads(failed.stdout)["node"] == "DIAGNOSING", failed.stdout
    _expect_ok(contour.own(
        "declare",
        "--expected", "the stage's done criterion is checkable from the stage's own material",
        "--actual", "the criterion names an artifact no stage produces",
        "--mismatch", "the stage norm, not the child's work, makes the check unreachable",
    ), "own channel `declare`")
    _expect_ok(contour.own(
        "investigate", "--localized-expectation", "the criterion is reachable",
        "--localized-actual", "the criterion is unreachable",
        "--hypothesis", "the criterion is unreachable as written",
        "--hypothesis", "the child's own work erred",
    ), "own channel `investigate`")
    critique = contour.own(
        "critique", "--functional-ground", "the parent stage's norm cannot be met",
        "--replanning-task", "rewrite the parent stage's done criterion",
        "--failure-address", "normative", "--norm-target", "parent",
    )
    result = _expect_ok(critique, "own channel `critique`")
    assert result["marker"] == "REPLAN" and result["action"] == "return_replan", result
    targets = contour.store.load(CHILD).difficulty.norm_targets
    assert [(t["target"], t["kind"], t["baseline"]) for t in targets] == [
        ("parent", "parent_norm", contour.norm.norm_digest())
    ], targets

    # the parent's dispatch of that child's REPLAN verifies the record the channel wrote
    runner = lambda argv, cwd=None: RunResult(  # noqa: E731
        0, stdout="REPLAN: the stage's done criterion cannot be met as written\n",
        stderr=f"spawn-specialist: transcript=/nowhere/projects/{CHILD}.jsonl\n",
    )
    dispatched = cli.cmd_dispatch(
        Namespace(session=PARENT, budget="medium", complexity="medium", dry_run=False),
        store=contour.store, runner=runner,
    )
    mismatch = dispatched.data.get("norm_mismatch")
    assert mismatch is not None, f"the parent did not accept the child's record: {dispatched}"
    assert mismatch["child_session"] == CHILD and mismatch["stage_index"] == 1
    assert dispatched.marker == "OVERCOME-DIFFICULTY"


# --- the spawn -----------------------------------------------------------------------


class _Launched(Exception):
    def __init__(self, cmd, env):
        super().__init__("launched")
        self.cmd = cmd
        self.env = env


class Launch:
    def __init__(self, rc, cmd, env):
        self.rc, self.cmd, self.env = rc, cmd, env

    def option(self, name: str) -> str | None:
        return self.cmd[self.cmd.index(name) + 1] if self.cmd and name in self.cmd else None


def _load_spawn():
    spec = importlib.util.spec_from_file_location("spawn_specialist_child_contour", SPAWN_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _parent_session(home: Path) -> FileStateStore:
    """A session at EXECUTING stage 1 of the shared two-stage fixture, in the state
    directory under `home` (the one a spawned child's engine and hooks resolve)."""
    root = _state_dir(home)
    _cli(root, "start", session=PARENT, task="parent", goal="g", done_criterion="dc")
    _cli(root, "classify", session=PARENT, architectural=True, files=5, changed_lines=200,
         wall_clock_min=60)
    _cli(root, "plan", session=PARENT)
    _cli(root, "submit-plan", session=PARENT, plan=str(FIXTURE_PLAN))
    _cli(root, "approve", session=PARENT, by="user")
    _cli(root, "partition", session=PARENT)
    _cli(root, "next-stage", session=PARENT)
    return FileStateStore(root)


def _spawn_argv(home: Path, tmp_path: Path, stage_index: int | None) -> list[str]:
    argv = [
        "--kind", "thinker", "--plan", str(FIXTURE_PLAN),
        "--done-criterion", "a verdict", "--criterion-type", "measurable",
        "--complexity", "medium", "--effort", "medium",
        "--session", PARENT, "--state-root", str(_state_dir(home)),
        "--workdir", str(tmp_path),
    ]
    if stage_index is not None:
        argv += ["--stage-index", str(stage_index)]
    return argv


def _launch(monkeypatch, tmp_path: Path, *, stage_index: int | None) -> Launch:
    """Run `spawn-specialist.py main` up to the child launch; the env and argv the child
    `claude -p` would get (`env` None when the wrapper stopped before launching)."""
    home = tmp_path / "home"
    spawn = _load_spawn()
    monkeypatch.setattr(spawn.shutil, "which", lambda *a, **k: "/usr/bin/claude")

    def launch(cmd, env=None, **kw):
        raise _Launched(list(cmd), dict(env or {}))

    monkeypatch.setattr(spawn.proc_tree, "launch_supervised", launch)
    try:
        return Launch(spawn.main(_spawn_argv(home, tmp_path, stage_index)), None, None)
    except _Launched as launched:
        return Launch(None, launched.cmd, launched.env)


def _own_rule() -> str:
    return f"Bash(python3 {OWN_SCRIPT}:*)"


def _settings_allow(cmd: list[str]) -> list[str]:
    settings = json.loads(cmd[cmd.index("--settings") + 1])
    return settings.get("permissions", {}).get("allow", [])


def test_linked_spawn_exports_own_session(tmp_path, monkeypatch):
    _parent_session(tmp_path / "home")
    launched = _launch(monkeypatch, tmp_path, stage_index=1)
    assert launched.env is not None, f"setup: the spawn did not reach the launch (rc={launched.rc})"
    assert launched.env.get(HANDOFF_ENV), "setup: the linked spawn exported no handoff"
    own = launched.env.get(OWN_ENV)
    assert own, f"BEHAVIOUR: a linked spawn exports no {OWN_ENV}"
    assert own != PARENT
    assert launched.option("--session-id") == own, (
        "the child's harness session id is not the id its channel is pinned to"
    )
    assert _own_rule() in _settings_allow(launched.cmd), (
        "the child's settings do not allow the channel script by its absolute path"
    )
    assert not any("agentctl-cli.py" in rule and "approve" in rule for rule in _settings_allow(launched.cmd))


def test_own_contour_names_have_one_definition():
    """The env name and the verb set are read from `agentctl.own_contour` by the gate, the
    channel and the spawner -- none of them keeps a copy that could drift."""
    own_contour = importlib.import_module("agentctl.own_contour")
    hook_spec = importlib.util.spec_from_file_location("hook_state_gate_one_definition", HOOK)
    hook = importlib.util.module_from_spec(hook_spec)
    hook_spec.loader.exec_module(hook)
    channel, spawn = _load_channel(), _load_spawn()
    assert own_contour.OWN_SESSION_ENV == OWN_ENV
    assert hook.OWN_SESSION_ENV is own_contour.OWN_SESSION_ENV
    assert spawn.OWN_SESSION_ENV is own_contour.OWN_SESSION_ENV
    assert channel.OWN_SESSION_ENV is own_contour.OWN_SESSION_ENV
    assert channel.OWN_CONTOUR_VERBS is own_contour.OWN_CONTOUR_VERBS
    assert spawn.OWN_CONTOUR_VERBS is own_contour.OWN_CONTOUR_VERBS
    assert set(own_contour.OWN_CONTOUR_VERBS) == OWN_CONTOUR_VERBS


def test_linked_brief_names_own_contour(tmp_path, capsys):
    _parent_session(tmp_path / "home")
    argv = _spawn_argv(tmp_path / "home", tmp_path, 1) + ["--dry-run"]
    assert _load_spawn().main(argv) == 0
    out = capsys.readouterr().out
    assert "agentctl-own.py" in out, "BEHAVIOUR: the linked child's brief does not name its channel"
    assert "## Your own engine contour" in out
    assert "## The engine belongs to the parent" not in out
    section = out.split("## Your own engine contour", 1)[1].split("\n## ", 1)[0]
    for needle in (OWN_ENV, "approve --by agent", "--norm-target parent", "REPLAN:", "verify-final"):
        assert needle in section, f"the contour section lacks {needle!r}"
    for verb in sorted(OWN_CONTOUR_VERBS):
        assert f"`{verb}`" in section, f"the brief's verb list lacks {verb!r}"


def test_unlinked_spawn_unchanged(tmp_path, monkeypatch, capsys):
    _parent_session(tmp_path / "home")
    launched = _launch(monkeypatch, tmp_path, stage_index=None)
    assert launched.env is not None, f"setup: the spawn did not reach the launch (rc={launched.rc})"
    assert OWN_ENV not in launched.env and HANDOFF_ENV not in launched.env
    assert "--session-id" not in launched.cmd
    assert not any("agentctl-own.py" in rule for rule in _settings_allow(launched.cmd))
    assert _load_spawn().main(_spawn_argv(tmp_path / "home", tmp_path, None) + ["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "## The engine belongs to the parent" in out
    assert "## Your own engine contour" not in out


def test_preassigned_id_is_engine_session_id(tmp_path, monkeypatch):
    # Shows only that the spawn exports the very uuid it passes as `--session-id` and that
    # the engine autostart under that id adopts the exported handoff. That the harness
    # reports the `--session-id` value as the hook payload's `session_id` is assumed, not
    # proved here.
    home = tmp_path / "home"
    store = _parent_session(home)
    launched = _launch(monkeypatch, tmp_path, stage_index=1)
    assert launched.env is not None, f"setup: the spawn did not reach the launch (rc={launched.rc})"
    own = launched.option("--session-id")
    assert own and launched.env.get(OWN_ENV) == own, (
        f"BEHAVIOUR: the pre-assigned session id {own!r} is not the one exported as {OWN_ENV}"
    )
    assert store.load(own) is None
    proc = subprocess.run(
        [sys.executable, str(ENGINE_START_HOOK)],
        input=json.dumps({"session_id": own, "prompt": "do the stage"}),
        capture_output=True, text=True,
        env=_child_env(home, **{HANDOFF_ENV: launched.env[HANDOFF_ENV]}),
    )
    assert proc.returncode == 0, proc.stderr
    started = store.load(own)
    assert started is not None, "the autostart did not create the engine session under the id"
    assert started.parent_link is not None and started.parent_link["parent_session"] == PARENT
    assert started.parent_link["stage_index"] == 1


# --- the turn end --------------------------------------------------------------------


def _load_turn_end_gate():
    spec = importlib.util.spec_from_file_location("hook_turn_end_gate_child_contour", TURN_END_HOOK)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_linked_child_final_turn_not_blocked(tmp_path, monkeypatch):
    """A linked child ends its turn with a return marker: the turn-end obligations stay a
    root coordinator's, whatever node its own engine session is at."""
    home = tmp_path / "agent-home"
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(home))
    state_dir = home / "agentctl" / "state"
    state_dir.mkdir(parents=True)
    state = SessionState(
        session_id=CHILD, task_id="t", node="RESOLUTION", weight_class="SUBSTANTIVE",
        parent_link={"parent_session": PARENT, "stage_index": 1, "norm_digest": "0" * 64,
                     "grants": [], "depth": 1},
    )
    (state_dir / f"{CHILD}.json").write_text(state.to_json(), encoding="utf-8")
    transcript = tmp_path / "t.jsonl"
    lines = [
        {"message": {"role": "user", "content": "почему ты не запустил тесты"}},
        {"message": {"role": "assistant", "content": [{"type": "text", "text": "COMPLETED: done"}]}},
    ]
    transcript.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    payload = {"transcript_path": str(transcript), "stop_hook_active": False, "session_id": CHILD}
    gate = _load_turn_end_gate()
    judge_yes = lambda argv, **kw: RunResult(0, stdout="YES", stderr="")  # noqa: E731

    monkeypatch.setenv("AGENT_RECURSION_DEPTH", "1")
    assert gate.decide(payload, runner=judge_yes) is None, (
        "a linked child's final turn was blocked by a root-coordinator obligation"
    )
    monkeypatch.setenv("AGENT_RECURSION_DEPTH", "0")
    blocked = gate.decide(payload, runner=judge_yes)
    assert blocked is not None and blocked["decision"] == "block", (
        "setup: the same turn is not blocked for a root, so the depth-1 pass above proves nothing"
    )


# --- a linked child closes its own contour by verify-final alone --------------------------
# The acceptance a root records with `accept` is, for a linked child, the parent's
# record-result on the stage the child serves; `accept` and `resolve` stay root-only.

def _linked_verifying(store, sid: str, tmp_path: Path, *, linked: bool, monkeypatch):
    from test_acceptance_verdict import _approved_state, _write_plan

    monkeypatch.delenv("AGENTCTL_ACCEPTANCE", raising=False)
    state = _approved_state(sid, _write_plan(tmp_path / f"{sid}.toml"))
    if linked:
        state.parent_link = {
            "parent_session": PARENT, "stage_index": 1, "norm_digest": "0" * 64,
            "grants": [], "depth": 1,
        }
    store.save(state)
    return state


def _verify_final(store, sid: str):
    return cli.cmd_verify_final(Namespace(session=sid), store=store)


def test_linked_reaches_resolution_by_verify_final(store, tmp_path, monkeypatch):
    _linked_verifying(store, "lnk", tmp_path, linked=True, monkeypatch=monkeypatch)
    d = _verify_final(store, "lnk")
    assert d.ok is True, f"a linked child with every stage PASSED is held at verify-final: {d}"
    assert store.load("lnk").node == "RESOLUTION"


def test_unlinked_still_blocked_without_accept(store, tmp_path, monkeypatch):
    _linked_verifying(store, "unl", tmp_path, linked=False, monkeypatch=monkeypatch)
    d = _verify_final(store, "unl")
    assert d.ok is False and d.action == "fix_stages"
    assert any("AcceptanceReview" in b for b in d.data["blockers"])
    assert store.load("unl").node == "VERIFYING"


def test_own_channel_refuses_accept_resolve(contour):
    before = {s: contour.state_bytes(s) for s in (CHILD, PARENT)}
    for verb in ("accept", "resolve"):
        assert verb not in OWN_CONTOUR_VERBS
        proc = contour.own(verb)
        assert proc.returncode == 2, (verb, proc.returncode, proc.stdout, proc.stderr)
        assert REFUSED in proc.stderr and "outside the own contour" in proc.stderr, (verb, proc.stderr)
        assert proc.stdout == "", f"`{verb}` reached the engine: {proc.stdout!r}"
    assert {s: contour.state_bytes(s) for s in before} == before


@pytest.mark.parametrize("forgery", ["outside-handoffs-dir", "no-child-session-record"])
def test_forged_handoff_is_not_adopted_and_acceptance_still_blocks(contour, tmp_path, monkeypatch, forgery):
    record = norm_handoff.build_record(
        parent_session=PARENT, stage_index=1, kind="developer", norm=contour.norm,
        grants=[], depth=1, parent_lineage=None)
    if forgery == "outside-handoffs-dir":
        path, sha = norm_handoff.write_record(tmp_path / "forged", record)
        norm_handoff.write_child_session(contour.root, PARENT, 1, session="forged", workdir="/tmp")
    else:
        path, sha = norm_handoff.write_record(contour.root, record)
        norm_handoff.write_child_session(contour.root, PARENT, 1, session=CHILD, workdir="/tmp")
    state = _linked_verifying(contour.store, "forged", tmp_path, linked=False, monkeypatch=monkeypatch)
    monkeypatch.setenv(HANDOFF_ENV, norm_handoff.format_env_value(path, sha))

    cli._adopt_norm_handoff(state, contour.store)
    contour.store.save(state)

    assert contour.store.load("forged").parent_link is None, "a forged handoff was adopted"
    d = _verify_final(contour.store, "forged")
    assert d.ok is False and d.action == "fix_stages", d
    assert any("AcceptanceReview" in b for b in d.data["blockers"]), d


def test_linked_failed_stage_blocks(store, tmp_path, monkeypatch):
    state = _linked_verifying(store, "lnk", tmp_path, linked=True, monkeypatch=monkeypatch)
    state.stages[0].outcome.status = "FAILED"
    store.save(state)
    d = _verify_final(store, "lnk")
    assert d.ok is False and d.action == "fix_stages"
    assert any("stages not PASSED" in b for b in d.data["blockers"])
    assert store.load("lnk").node == "VERIFYING"


def test_linked_pending_renorm_blocks(store, tmp_path, monkeypatch):
    from agentctl import gates

    state = _linked_verifying(store, "lnk", tmp_path, linked=True, monkeypatch=monkeypatch)
    norm_file = tmp_path / "norm.md"
    norm_file.write_text("norm", encoding="utf-8")
    state.pending_renorms = [{"target": str(norm_file), "kind": "stage", "stage": 1, "depth": 0,
                              "baseline": gates.file_norm_digest(str(norm_file))}]
    store.save(state)
    d = _verify_final(store, "lnk")
    assert d.ok is False and d.action == "renorm"
    assert store.load("lnk").node == "VERIFYING"


def test_linked_escalation_orphan_blocks(store, tmp_path, monkeypatch, venued_fixtures_dir):
    from test_escalation_closure import CHILD as GRANDCHILD
    from test_escalation_closure import PARENT as MIDDLE
    from test_escalation_closure import _routed_child

    _routed_child(store, venued_fixtures_dir, tmp_path, monkeypatch)
    state = store.load(MIDDLE)
    state.node = "VERIFYING"
    state.parent_link = {
        "parent_session": PARENT, "stage_index": 1, "norm_digest": "0" * 64,
        "grants": [], "depth": 1,
    }
    store.save(state)
    d = _verify_final(store, MIDDLE)
    assert d.ok is False and d.action == "adopt_escalation", d
    assert any(f"adopt-escalation --child {GRANDCHILD}" in b for b in d.data["blockers"])
    assert store.load(MIDDLE).node == "VERIFYING"


def test_linked_failing_final_check_blocks(store, tmp_path, monkeypatch):
    from agentctl.state import FinalCheck

    state = _linked_verifying(store, "lnk", tmp_path, linked=True, monkeypatch=monkeypatch)
    state.final_check = [FinalCheck(command="false", expected_exit=0, label="fails")]
    store.save(state)
    d = _verify_final(store, "lnk")
    assert d.ok is False and d.action == "declare", d
    assert d.data["failures"] == ["final_check 'fails': exit 1 != 0"], d
    assert store.load("lnk").node == "DIAGNOSING"
