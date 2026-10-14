"""A linked child goes from its spawn brief to EXECUTING with no write by another session.

Each gate of the child contour has a suite of its own: the own plan path and the hook that
guards it (`test_own_plan_path.py`), the wildcard cover of the child's checks
(`test_child_check_cover.py`), the channel and the parent-norm approval
(`test_child_contour_gate.py`, `test_child_approval.py`). A chain of gates is closed only
when one run passes through all of them, so this file drives that run: a parent session at
EXECUTING a stage whose grants hold `Bash(python3 -m pytest:*)`, a linked spawn through a
stubbed launcher, the child's plan written at the path its brief names -- through the
PreToolUse hook as the child -- and `agentctl-own.py` carrying the child through
classify, submit-plan, plan-review and `approve --by agent`. The parent's state file is the
observable of "the root wrote nothing for it".

The entry points are those the base tree has (`spawn-specialist.py`, the hook as a
subprocess, the channel); the first test fails on base on a `BEHAVIOUR:` assertion, the second
pins what holds on base. Linking needs `AGENTCTL_CHILD_LINKING=1`, which `conftest.py` sets
for every test.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from agentctl.stage_norm import StageNorm
from agentctl.store import FileStateStore
from conftest import declare_venue
from test_child_approval import (
    PARENT_CHECK, _parent_plan, _spliced, _with_stage_grant, _with_stage_verify,
)
from test_child_contour_gate import (  # noqa: F401 -- _isolated_env is the shared autouse fixture
    ENGINE_START_HOOK, HANDOFF_ENV, HOOK, OWN_ENV, PARENT, Launch, _child_env, _cli, _denied,
    _expect_ok, _isolated_env, _Launched, _load_spawn, _own, _reason, _settings_allow, _state_dir,
)

SCRIPTS = Path(__file__).resolve().parent.parent
SPECIALIZATIONS = SCRIPTS.parent / "skills" / "specializations"
PYTEST_WILDCARD = "Bash(python3 -m pytest:*)"
CHILD_CHECK = "python3 -m pytest -q -x tests/test_mod.py"
OUTSIDE_CHECK = "python3 -m unittest tests.test_mod"


class Scene:
    """A parent at EXECUTING stage 1 (its grants hold the pytest wildcard), a linked spawn
    for that stage through a stubbed launcher, and the child's engine session started
    under the handoff the spawn wrote."""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.tmp = tmp_path
        self.venue = tmp_path / "venue"
        self.venue.mkdir()
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.agent_home = self.home / ".claude"
        self.agent_home.mkdir()
        skills = self.agent_home / "skills"
        skills.mkdir()
        for source in (SCRIPTS.parent / "skills", SPECIALIZATIONS):
            for entry in source.iterdir():
                if entry.is_dir() and not (skills / entry.name).exists():
                    (skills / entry.name).symlink_to(entry)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(self.agent_home))
        monkeypatch.setenv("CLAUDE_AGENT_HOME", str(self.agent_home))
        self.monkeypatch = monkeypatch
        self.root = _state_dir(self.home)
        self.parent_plan = self._start_parent()
        self.launch, self.prompt = self._spawn()
        self.own_session = self.launch.option("--session-id")
        assert self.launch.env is not None, f"setup: the spawn did not reach the launch (rc={self.launch.rc})"
        assert self.launch.env.get(HANDOFF_ENV) and self.launch.env.get(OWN_ENV) == self.own_session, (
            "setup: the spawn is not linked"
        )
        self.parent_before = self.parent_bytes()
        started = subprocess.run(
            [sys.executable, str(ENGINE_START_HOOK)],
            input=json.dumps({"session_id": self.own_session, "prompt": "do the stage"}),
            capture_output=True, text=True,
            env=_child_env(self.home, CLAUDE_CONFIG_DIR=str(self.agent_home),
                           **{HANDOFF_ENV: self.launch.env[HANDOFF_ENV]}),
        )
        child = FileStateStore(self.root).load(self.own_session)
        assert child is not None, f"setup: the child's engine autostart did not run: {started.stderr}"
        assert child.parent_link and child.parent_link["parent_session"] == PARENT \
            and child.parent_link["stage_index"] == 1, f"setup: the autostart did not adopt the handoff: {child.parent_link!r}"
        (self.venue / "tests").mkdir()
        (self.venue / "tests" / "test_mod.py").write_text("", encoding="utf-8")

    def _start_parent(self) -> str:
        plan = Path(_parent_plan(self.venue))
        plan.write_text(_with_stage_grant(plan.read_text(encoding="utf-8"), PYTEST_WILDCARD),
                        encoding="utf-8")
        _cli(self.root, "start", session=PARENT, task="parent", goal="g", done_criterion="dc")
        _cli(self.root, "classify", session=PARENT, architectural=True, files=5,
             changed_lines=200, wall_clock_min=60)
        _cli(self.root, "plan", session=PARENT)
        _cli(self.root, "submit-plan", session=PARENT, plan=str(plan))
        _cli(self.root, "approve", session=PARENT, by="user")
        _cli(self.root, "partition", session=PARENT)
        _cli(self.root, "next-stage", session=PARENT)
        self.norm = StageNorm.from_stage(FileStateStore(self.root).load(PARENT).stage(1))
        return str(plan)

    def _spawn(self) -> tuple[Launch, str]:
        work = self.tmp / "work"
        work.mkdir()
        spawn = _load_spawn()
        self.monkeypatch.setattr(spawn.shutil, "which", lambda *a, **k: "/usr/bin/claude")
        briefs: list[str] = []
        assemble = spawn.assemble_prompt

        def recording_assemble(*args, **kwargs):
            briefs.append(assemble(*args, **kwargs))
            return briefs[-1]

        def launch(cmd, env=None, **kw):
            raise _Launched(list(cmd), dict(env or {}))

        self.monkeypatch.setattr(spawn, "assemble_prompt", recording_assemble)
        self.monkeypatch.setattr(spawn.proc_tree, "launch_supervised", launch)
        argv = [
            "--kind", "developer", "--plan", self.parent_plan, "--stage-index", "1",
            "--done-criterion", "the stage's check passes", "--criterion-type", "measurable",
            "--complexity", "medium", "--effort", "medium",
            "--session", PARENT, "--state-root", str(self.root), "--workdir", str(work),
        ]
        try:
            launched = Launch(spawn.main(argv), None, None)
        except _Launched as stub:
            launched = Launch(None, stub.cmd, stub.env)
        return launched, (briefs[-1] if briefs else "")

    def parent_bytes(self) -> bytes:
        return (self.root / f"{PARENT}.json").read_bytes()

    def channel(self, *argv: str) -> subprocess.CompletedProcess:
        return _own(self.home, *argv, session=self.own_session, AGENTCTL_PLAN_REVIEW="1",
                    CLAUDE_CONFIG_DIR=str(self.agent_home), CLAUDE_CODE_SESSION_ID=self.own_session,
                    AGENT_RECURSION_DEPTH=self.launch.env["AGENT_RECURSION_DEPTH"])

    def edit(self, path: str) -> subprocess.CompletedProcess:
        payload = {"tool_name": "Edit", "session_id": self.own_session, "tool_input": {"file_path": path}}
        env = _child_env(
            self.home, CLAUDE_CONFIG_DIR=str(self.agent_home),
            AGENT_RECURSION_DEPTH=self.launch.env["AGENT_RECURSION_DEPTH"],
            **{OWN_ENV: self.own_session, HANDOFF_ENV: self.launch.env[HANDOFF_ENV]},
        )
        return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                              capture_output=True, text=True, env=env)

    def plan_text(self, stage_check: str) -> str:
        """The child's plan: the parent stage's own scaffold stage only, so the files it
        writes are the ones the parent's grants hold, with `stage_check` as its check."""
        text = _spliced(parent_norm=self.norm.norm_digest(), final_checks=((PARENT_CHECK, 0),))
        head, _, rest = text.partition("[[stage]]\nindex = 2")
        final_checks = rest[rest.index("\n[[final_check]]"):]
        return declare_venue(_with_stage_verify(head + final_checks, stage_check), self.venue)

    def to_planning(self) -> None:
        _expect_ok(self.channel("classify", "--architectural", "--files", "5", "--changed-lines", "200",
                                "--wall-clock-min", "60"), "own channel `classify`")
        _expect_ok(self.channel("plan"), "own channel `plan`")

    def submit_and_review(self, plan: str) -> dict:
        submitted = _expect_ok(self.channel("submit-plan", "--plan", plan), "own channel `submit-plan`")
        digest = hashlib.sha256(Path(plan).read_bytes()).hexdigest()
        _expect_ok(self.channel("plan-review", "--verdict", "pass", "--reviewer", "thinker",
                                "--target", plan, "--plan-digest", digest), "own channel `plan-review`")
        return submitted


@pytest.fixture
def scene(tmp_path, monkeypatch):
    return Scene(tmp_path, monkeypatch)


def test_linked_child_reaches_executing_without_root_writes(scene):
    found = re.search(r"Your plan file is `([^`]+)`", scene.prompt)
    assert found, "BEHAVIOUR: the linked child's brief does not name a plan file of its own"
    plan = found.group(1)
    assert Path(plan).resolve().is_relative_to(scene.tmp.resolve()), (
        f"setup: the child's plan path {plan} lies outside the test's tmp tree"
    )

    own_plan_rules = [r for r in _settings_allow(scene.launch.cmd) if "own-plans" in r]
    assert own_plan_rules == [f"Edit(/{plan})"], (
        f"BEHAVIOUR: the child's allow rules for its own plan are {own_plan_rules!r}, not exactly {plan}"
    )
    add_dirs = [scene.launch.cmd[i + 1] for i, t in enumerate(scene.launch.cmd) if t == "--add-dir"]
    assert str(Path(plan).parent) in add_dirs, f"BEHAVIOUR: --add-dir does not reach {Path(plan).parent}"
    spawn = _load_spawn()
    denies = json.loads(scene.launch.cmd[scene.launch.cmd.index("--settings") + 1]) \
        .get("permissions", {}).get("deny", [])
    assert not [d for d in denies if spawn._edit_deny_covers_allow(d, own_plan_rules[0])], (
        f"BEHAVIOUR: a deny in the child's settings covers its own plan allow {own_plan_rules[0]}"
    )

    scene.to_planning()
    allowed = scene.edit(plan)
    assert allowed.returncode == 0 and not _denied(allowed), (
        f"BEHAVIOUR: the hook denied the child's write of its own plan at PLANNING: {allowed.stdout!r}"
    )
    Path(plan).write_text(scene.plan_text(CHILD_CHECK), encoding="utf-8")

    submitted = scene.submit_and_review(plan)
    autonomy = (submitted.get("data") or {}).get("autonomy") or {}
    assert autonomy.get("action") == "self_approve" and autonomy.get("basis") == "parent_norm", (
        f"BEHAVIOUR: a plan whose pytest checks the parent's wildcard covers is not routed to "
        f"self-approval: reasons={autonomy.get('reasons')} "
        f"resources={[e['name'] for e in autonomy.get('extra_resources') or []]} "
        f"unresolved={[c['source'] for c in autonomy.get('unresolved_changed_commands') or []]}"
    )
    approved = scene.channel("approve", "--by", "agent")
    assert approved.returncode == 0 and json.loads(approved.stdout).get("ok") is True, (
        f"BEHAVIOUR: `approve --by agent` was refused: {approved.stdout[:400]!r}"
    )
    for step in (("partition",), ("next-stage",)):
        _expect_ok(scene.channel(*step), f"own channel `{step[0]}`")

    child = FileStateStore(scene.root).load(scene.own_session)
    assert child.node == "EXECUTING", f"BEHAVIOUR: the child is at {child.node}, not EXECUTING"
    rows = [h for h in child.history if h.get("event") == "approve"]
    assert rows and rows[-1].get("basis") == "parent_norm" and rows[-1].get("by") == "agent", (
        f"BEHAVIOUR: the child's last approve is not an agent approval on the parent norm: {rows!r}"
    )
    assert scene.parent_bytes() == scene.parent_before, (
        "BEHAVIOUR: the parent's state file changed while the child went to EXECUTING"
    )
    frozen = scene.edit(plan)
    assert _denied(frozen) and "replan" in _reason(frozen), (
        f"BEHAVIOUR: the child's own plan is not frozen at EXECUTING: {frozen.stdout!r}"
    )


def test_e2e_child_with_check_outside_parent_grants_is_refused(scene):
    plan = scene.tmp / "child.toml"
    plan.write_text(scene.plan_text(OUTSIDE_CHECK), encoding="utf-8")

    scene.to_planning()
    submitted = scene.submit_and_review(str(plan))
    autonomy = (submitted.get("data") or {}).get("autonomy") or {}
    assert autonomy.get("eligible") is False and "unresolved-not-in-handoff" in (autonomy.get("reasons") or []), autonomy

    refused = scene.channel("approve", "--by", "agent")
    result = json.loads(refused.stdout)
    blockers = (result.get("data") or {}).get("blockers") or []
    assert result.get("ok") is False and any("unresolved-not-in-handoff" in b for b in blockers), (
        f"approve --by agent was not refused on the boundary: {result}"
    )

    assert FileStateStore(scene.root).load(scene.own_session).node != "EXECUTING", (
        "the refused child reached EXECUTING"
    )
    assert scene.parent_bytes() == scene.parent_before, "the parent's state file changed"
