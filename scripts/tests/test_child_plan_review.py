"""A linked child reviews its own plan: the review capability is part of its contour.

A child spawned for a parent stage is gated by its own engine session and may not
approve its plan before a thinker has passed it. The topological review driver
(`plan-review-topological.py`) is the one command that walks, spawns the reviewers,
records their verdicts and composes the pass; this file pins the three layers that put
it inside the child's contour:

* the channel (`agentctl-own.py`) admits the three read/compose verbs the driver runs
  besides `plan-review` -- and still refuses every user-authority verb;
* the spawn grants a linked child exactly one more rule, the driver by its absolute
  path in own mode (`--own`), names the command in the brief, and gives an unlinked
  spawn (a reviewer included) neither;
* the driver, with `--own`, takes its session from the contour and drives the engine
  only through the channel; own mode and `AGENTCTL_OWN_SESSION` must agree, or it
  exits 2.

Known limitation, pinned nowhere: a child can still hand-record a thinker pass with
`plan-review`; the driver is the supported route, not the only one the verb set allows."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shlex
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest
from lib.widening_targets import AGENTCTL_USER_AUTHORITY_VERBS
from test_child_contour_gate import (  # noqa: F401 - `_isolated_env` is autouse
    CHILD,
    FIXTURE_PLAN,
    HANDOFF_ENV,
    OTHER,
    OWN_CONTOUR_VERBS,
    OWN_ENV,
    OWN_SCRIPT,
    REFUSED,
    _expect_ok,
    _isolated_env,
    _load_spawn,
    _parent_session,
    _settings_allow,
    _spawn_argv,
    contour,
)
from test_plan_review_topological import canonical_stdout, review_original, summary_line

SCRIPTS = Path(__file__).resolve().parent.parent
DRIVER_SCRIPT = SCRIPTS / "plan-review-topological.py"
REVIEW_VERBS = ("plan-review-walk", "plan-review-pair-history", "plan-review-compose")
REVIEW_RULE = f"Bash(python3 {DRIVER_SCRIPT} --own:*)"
BRIEF_COMMAND_RE = re.compile(r"`(python3 \S*plan-review-topological\.py --own --plan <plan>[^`]*)`")
USER_AUTHORITY_VERBS = sorted(AGENTCTL_USER_AUTHORITY_VERBS - OWN_CONTOUR_VERBS)
PLAN_REVIEW_ENV = {"AGENTCTL_PLAN_REVIEW": "1"}


@pytest.fixture(autouse=True)
def _review_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCTL_TOPO_UNITS_DIR", str(tmp_path / "topo-units"))
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")


def _driver():
    spec = importlib.util.spec_from_file_location("plan_review_topological_child", DRIVER_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Launched(Exception):
    def __init__(self, cmd, env):
        super().__init__("launched")
        self.cmd = cmd
        self.env = env


def _launch(spawn, monkeypatch, argv):
    """`spawn.main(argv)` up to the child launch: the `claude -p` argv and env, or None."""
    monkeypatch.setattr(spawn.shutil, "which", lambda *a, **k: "/usr/bin/claude")

    def launch(cmd, env=None, **kw):
        raise _Launched(list(cmd), dict(env or {}))

    monkeypatch.setattr(spawn.proc_tree, "launch_supervised", launch)
    try:
        rc = spawn.main(argv)
    except _Launched as launched:
        return launched.cmd, launched.env
    raise AssertionError(f"setup: the spawn did not reach the launch (rc={rc})")


def _brief(capsys, argv) -> str:
    assert _load_spawn().main(argv + ["--dry-run"]) == 0
    return capsys.readouterr().out


def _run_driver(drv, argv):
    """`drv.main(argv)` as an exit code, whether it returns or exits (argparse)."""
    try:
        return drv.main(argv)
    except SystemExit as exit_:
        return exit_.code


# --- the channel ---------------------------------------------------------------------


def test_own_channel_admits_review_verbs(contour):
    channel = importlib.import_module("agentctl.own_contour")
    assert set(REVIEW_VERBS) <= set(channel.OWN_CONTOUR_VERBS), (
        f"BEHAVIOUR: the own-contour verb set lacks "
        f"{sorted(set(REVIEW_VERBS) - set(channel.OWN_CONTOUR_VERBS))}"
    )
    assert set(channel.OWN_CONTOUR_VERBS) == OWN_CONTOUR_VERBS, (
        f"BEHAVIOUR: the own-contour verb set lacks "
        f"{sorted(OWN_CONTOUR_VERBS - set(channel.OWN_CONTOUR_VERBS))}"
    )
    calls = (
        ("plan-review-walk", "--target", contour.plan, "--format", "json"),
        ("plan-review-pair-history", "--target", contour.plan, "--pair", "base-1"),
        ("plan-review-compose", "--target", contour.plan),
    )
    for argv in calls:
        proc = contour.own(*argv)
        assert REFUSED not in proc.stderr, (argv, proc.stderr)
        assert "outside the own contour" not in proc.stderr, (argv, proc.stderr)
        assert proc.stdout.strip(), f"`{argv[0]}` never reached the engine: {proc.stderr!r}"


def test_own_channel_still_refuses_user_authority_verbs(contour):
    before = {s: contour.state_bytes(s) for s in (CHILD, "parent-s")}
    for verb in USER_AUTHORITY_VERBS:
        proc = contour.own(verb)
        assert proc.returncode == 2, (verb, proc.returncode, proc.stdout, proc.stderr)
        assert REFUSED in proc.stderr and "outside the own contour" in proc.stderr, (verb, proc.stderr)
        assert proc.stdout == "", f"`{verb}` reached the engine: {proc.stdout!r}"
    assert {s: contour.state_bytes(s) for s in before} == before


# --- the spawn -----------------------------------------------------------------------


def test_linked_spawn_grants_exactly_the_review_driver(tmp_path, monkeypatch):
    _parent_session(tmp_path / "home")
    spawn = _load_spawn()
    cmd, env = _launch(spawn, monkeypatch, _spawn_argv(tmp_path / "home", tmp_path, 1))
    assert env.get(OWN_ENV), "setup: the spawn is not linked"
    allow = _settings_allow(cmd)
    granted = [rule for rule in allow if "plan-review-topological.py" in rule]
    assert granted == [REVIEW_RULE], (
        f"BEHAVIOUR: a linked child is granted {granted!r}, not exactly the review driver "
        f"by its absolute path"
    )
    assert spawn.own_contour_review_rule().endswith("plan-review-topological.py --own:*)"), (
        "the driver grant does not pin own mode"
    )
    unlinked_cmd, _ = _launch(_load_spawn(), monkeypatch, _spawn_argv(tmp_path / "home", tmp_path, None))
    extra = sorted(set(allow) - set(_settings_allow(unlinked_cmd)))
    assert extra == sorted([f"Bash(python3 {OWN_SCRIPT}:*)", REVIEW_RULE]), (
        f"a linked child is granted more than the channel and the review driver: {extra}"
    )


def test_linked_brief_names_review_driver(tmp_path, capsys):
    _parent_session(tmp_path / "home")
    out = _brief(capsys, _spawn_argv(tmp_path / "home", tmp_path, 1))
    section = out.split("## Your own engine contour", 1)[1].split("\n## ", 1)[0]
    found = BRIEF_COMMAND_RE.findall(section)
    assert found, "BEHAVIOUR: the brief does not name `plan-review-topological.py --own --plan <plan>`"
    assert found[0].split()[1] == str(DRIVER_SCRIPT)
    paragraph = next(p for p in section.split("\n\n") if "plan-review-topological.py" in p)
    assert OWN_ENV in paragraph and "--session" in paragraph, (
        "the brief does not say that the session comes from the contour"
    )


def test_unlinked_spawn_gets_no_review_driver_rule(tmp_path, monkeypatch, capsys):
    _parent_session(tmp_path / "home")
    spawn = _load_spawn()
    calls = []
    monkeypatch.setattr(spawn, "own_contour_review_rule",
                        lambda: calls.append("called") or REVIEW_RULE)
    cmd, env = _launch(spawn, monkeypatch, _spawn_argv(tmp_path / "home", tmp_path, None))
    assert OWN_ENV not in env and HANDOFF_ENV not in env, "setup: the spawn is linked"
    assert not any("plan-review-topological.py" in rule for rule in _settings_allow(cmd))
    assert calls == [], "an unlinked spawn asked for the review-driver rule"
    out = _brief(capsys, _spawn_argv(tmp_path / "home", tmp_path, None))
    assert "plan-review-topological.py" not in out
    assert "## Your own engine contour" not in out


# --- the reviewer spawn ---------------------------------------------------------------


def _reviewer_launch(tmp_path, monkeypatch, depth: str):
    """The launch of one reviewer, spawned by the driver from inside a linked child."""
    drv = _driver()
    monkeypatch.setenv(OWN_ENV, CHILD)
    monkeypatch.setenv(HANDOFF_ENV, "/nowhere/h.json:" + "0" * 64)
    monkeypatch.setenv("AGENT_RECURSION_DEPTH", depth)
    argv = drv.build_spawn_argv(Namespace(model=None, complexity="high"), "unit:base",
                                str(FIXTURE_PLAN), dry_run=False)
    return argv, *_launch(_load_spawn(), monkeypatch, argv)


def test_reviewer_spawn_is_unlinked_read_only_thinker(tmp_path, monkeypatch):
    argv, cmd, env = _reviewer_launch(tmp_path, monkeypatch, "1")
    assert argv[argv.index("--kind") + 1] == "thinker"
    assert "--review-topo" in argv
    for flag in ("--session", "--stage-index", "--state-root"):
        assert flag not in argv, f"the reviewer spawn carries {flag}"
    assert OWN_ENV not in env and HANDOFF_ENV not in env, (
        "a reviewer received the child's contour signals"
    )
    for flag in ("--session-id", "--resume"):
        assert flag not in cmd, f"the reviewer's `claude -p` carries {flag}"
    for rule in _settings_allow(cmd):
        assert "agentctl-own.py" not in rule and "plan-review-topological.py" not in rule, rule


def test_reviewer_depth_is_child_depth_plus_one(tmp_path, monkeypatch):
    for depth in ("1", "3"):
        _, _, env = _reviewer_launch(tmp_path, monkeypatch, depth)
        assert env.get("AGENT_RECURSION_DEPTH") == str(int(depth) + 1), (depth, env.get("AGENT_RECURSION_DEPTH"))


# --- the driver under the contour ------------------------------------------------------


class _Engine:
    """Stands in for `drv.subprocess`: records each engine launch and answers it."""

    def __init__(self, status: str = "missing", records=()):
        self.calls: list[list[str]] = []
        self.status = status
        self.records = list(records)

    @staticmethod
    def verb(cmd) -> str:
        return cmd[2] if len(cmd) > 2 else ""

    def run(self, cmd, **kw):
        self.calls.append(list(cmd))
        verb = self.verb(cmd)
        plan_path = cmd[cmd.index("--target") + 1] if "--target" in cmd else ""
        if verb == "plan-review-walk":
            row = {"pair": "base-1", "base": "b", "service": "s", "level": 0,
                   "status": self.status, "ready": True, "waiting": [], "spawn": None,
                   "record": None}
            directive = {"ok": True, "data": {"plan_path": plan_path, "discharges": [],
                                              "levels": [[row]]}}
        elif verb == "plan-review-pair-history":
            directive = {"ok": True, "data": {"pair": "base-1", "records": self.records,
                                              "changed_parts_since_last": []}}
        else:
            directive = {"ok": True, "data": {}}
        return SimpleNamespace(returncode=0, stdout=json.dumps(directive), stderr="")


def _stub_reviewer(drv, monkeypatch, verdict="pass", concerns=()):
    spawned = []

    def spawn(argv):
        spawned.append(list(argv))
        if "--dry-run" in argv:
            return 0, "TOPO-VIEW: /v/base-1 files=node-1.md\n# stdin: <prompt 10 chars>\n", ""
        sha = hashlib.sha256(Path(argv[argv.index("--plan") + 1]).read_bytes()).hexdigest()
        return 0, canonical_stdout(review_original(verdict, sha, concerns)), summary_line() + "\n"

    monkeypatch.setattr(drv, "spawn_pair", spawn)
    return spawned


@pytest.fixture
def own_driver(tmp_path, monkeypatch):
    drv = _driver()
    monkeypatch.setattr(drv, "COST_LOG", tmp_path / "costs.jsonl")
    plan = tmp_path / "plan.toml"
    plan.write_text("plan bytes\n", encoding="utf-8")
    monkeypatch.setenv(OWN_ENV, CHILD)
    return SimpleNamespace(drv=drv, plan=str(plan), tmp=tmp_path, monkeypatch=monkeypatch)


def _engine(own_driver, **kw) -> _Engine:
    engine = _Engine(**kw)
    own_driver.monkeypatch.setattr(own_driver.drv, "subprocess", SimpleNamespace(run=engine.run))
    return engine


def test_driver_own_mode_takes_session_from_own_channel(own_driver, capsys):
    engine = _engine(own_driver)
    spawned = _stub_reviewer(own_driver.drv, own_driver.monkeypatch)
    rc = _run_driver(own_driver.drv, ["--own", "--plan", own_driver.plan, "--complexity", "low"])
    err = capsys.readouterr().err
    assert rc == 0, f"BEHAVIOUR: the --own run without --session under {OWN_ENV} exited {rc}: {err!r}"
    assert engine.calls, "the driver never reached the engine"
    for call in engine.calls:
        assert call[:2] == [sys.executable, str(OWN_SCRIPT)], f"an engine call bypasses the channel: {call}"
        assert not any(t == "--session" or t.startswith("--session=") for t in call), call
        assert call[call.index("--target") + 1] == own_driver.plan, call
    verbs = {_Engine.verb(call) for call in engine.calls}
    assert {"plan-review-walk", "plan-review", "plan-review-compose"} <= verbs, verbs
    reviewed = [a for a in spawned if "--dry-run" not in a]
    assert len(reviewed) == 1 and "--session" not in reviewed[0]


def test_driver_own_mode_names_channel_in_dry_run_hints(own_driver, capsys):
    engine = _engine(own_driver, status="revise", records=[{"verdict": "revise"}])
    _stub_reviewer(own_driver.drv, own_driver.monkeypatch)
    rc = _run_driver(own_driver.drv, ["--own", "--plan", own_driver.plan, "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0, f"BEHAVIOUR: the --own dry run without --session under {OWN_ENV} exited {rc}"
    hints = [line for line in out.splitlines() if "plan-review-pair-history" in line]
    assert hints, out
    for line in hints + [ln for ln in out.splitlines() if ln.strip().startswith("record:")]:
        assert str(OWN_SCRIPT) in line and "--session" not in line, line
        assert "agentctl-cli.py" not in line, line
    for call in engine.calls:
        assert call[:2] == [sys.executable, str(OWN_SCRIPT)], call


def test_driver_own_mode_refuses_foreign_session(own_driver, capsys):
    engine = _engine(own_driver)
    spawned = _stub_reviewer(own_driver.drv, own_driver.monkeypatch)
    rc = _run_driver(own_driver.drv, ["--own", "--session", OTHER, "--plan", own_driver.plan])
    capsys.readouterr()
    assert rc == 2, f"BEHAVIOUR: a --session naming another session under {OWN_ENV} exited {rc}"
    assert engine.calls == [] and spawned == [], "a refused run reached the engine or a reviewer"
    for blank in ("", "   "):
        own_driver.monkeypatch.setenv(OWN_ENV, blank)
        assert _run_driver(own_driver.drv, ["--own", "--session", CHILD, "--plan", own_driver.plan]) == 2, (
            f"a set-but-empty {OWN_ENV} ({blank!r}) was taken for no contour"
        )
        capsys.readouterr()
    assert engine.calls == [] and spawned == []
    own_driver.monkeypatch.setenv(OWN_ENV, CHILD)
    assert _run_driver(own_driver.drv, ["--own", "--session", CHILD, "--plan", own_driver.plan]) == 0
    assert engine.calls, "the session the contour names was refused"


def test_driver_own_flag_without_contour_exits_2(own_driver, capsys):
    engine = _engine(own_driver)
    spawned = _stub_reviewer(own_driver.drv, own_driver.monkeypatch)
    own_driver.monkeypatch.delenv(OWN_ENV)
    for argv in (["--own"], ["--own", "--session", CHILD]):
        rc = _run_driver(own_driver.drv, [*argv, "--plan", own_driver.plan])
        err = capsys.readouterr().err
        assert rc == 2, f"BEHAVIOUR: {argv} without {OWN_ENV} exited {rc}"
        assert OWN_ENV in err, err
    assert engine.calls == [] and spawned == [], "a refused run reached the engine or a reviewer"


def test_driver_contour_without_own_flag_exits_2(own_driver, capsys):
    engine = _engine(own_driver)
    spawned = _stub_reviewer(own_driver.drv, own_driver.monkeypatch)
    for value in (CHILD, ""):
        own_driver.monkeypatch.setenv(OWN_ENV, value)
        for argv in ([], ["--session", CHILD], ["--session", OTHER]):
            rc = _run_driver(own_driver.drv, [*argv, "--plan", own_driver.plan])
            err = capsys.readouterr().err
            assert rc == 2, f"BEHAVIOUR: {argv} with {OWN_ENV}={value!r} and no --own exited {rc}"
            assert "--own" in err, err
    assert engine.calls == [] and spawned == [], "a child reached base mode"


def test_driver_own_mode_refuses_ledger(own_driver, capsys):
    engine = _engine(own_driver)
    _stub_reviewer(own_driver.drv, own_driver.monkeypatch)
    ledger = own_driver.tmp / "ledger" / "gaps.jsonl"
    rc = _run_driver(own_driver.drv, ["--own", "--session", CHILD, "--plan", own_driver.plan,
                                      "--ledger", str(ledger)])
    capsys.readouterr()
    assert rc == 2, f"BEHAVIOUR: --ledger under {OWN_ENV} exited {rc}"
    assert engine.calls == [], "a refused --ledger run reached the engine"
    assert not ledger.exists() and not ledger.parent.exists(), "the ledger path was created"


# --- the child reviews and approves its own plan --------------------------------------------


def _brief_command(tmp_path, capsys, plan: str) -> list[str]:
    """The review command the linked child's brief gives it, with `<plan>` filled in."""
    spawn_home = tmp_path / "spawn"
    _parent_session(spawn_home / "home")
    out = _brief(capsys, _spawn_argv(spawn_home / "home", spawn_home, 1))
    section = out.split("## Your own engine contour", 1)[1].split("\n## ", 1)[0]
    found = BRIEF_COMMAND_RE.findall(section)
    assert found, "BEHAVIOUR: the brief does not name the review command"
    return shlex.split(found[0].replace("<plan>", plan))


def _covered(command: list[str]) -> bool:
    """Whether the launcher's allow rule covers the command (`Bash(<prefix>:*)`)."""
    prefix = REVIEW_RULE[len("Bash("):-len(":*)")]
    return shlex.join(command).startswith(prefix)


def _own_up_to_review(contour) -> None:
    for step in (
        ("classify", "--architectural", "--files", "5", "--changed-lines", "200",
         "--wall-clock-min", "60"),
        ("plan",),
        ("submit-plan", "--plan", contour.plan),
    ):
        _expect_ok(contour.own(*step, **PLAN_REVIEW_ENV), f"own channel `{step[0]}`")


def _run_review(contour, monkeypatch, capsys, verdict, concerns=()) -> int:
    command = _brief_command(contour.tmp, capsys, contour.plan)
    assert _covered(command), f"the allow rule does not cover the brief's command: {command}"
    drv = _driver()
    monkeypatch.setattr(drv, "COST_LOG", contour.tmp / "costs.jsonl")
    _stub_reviewer(drv, monkeypatch, verdict, concerns)
    monkeypatch.setenv("HOME", str(contour.home))
    monkeypatch.setenv(OWN_ENV, CHILD)
    rc = _run_driver(drv, command[2:])
    capsys.readouterr()
    return rc


def test_e2e_linked_child_reviews_and_approves_its_own_plan(contour, monkeypatch, capsys):
    _own_up_to_review(contour)
    parent_before = contour.state_bytes("parent-s")
    rc = _run_review(contour, monkeypatch, capsys, "pass")
    assert rc == 0, f"BEHAVIOUR: the child's own review run exited {rc}"
    assert contour.state_bytes("parent-s") == parent_before, "the review wrote the parent's session"
    for step in (("approve", "--by", "agent"), ("partition",), ("next-stage",)):
        _expect_ok(contour.own(*step, **PLAN_REVIEW_ENV), f"own channel `{step[0]}`")
    child = contour.store.load(CHILD)
    assert child.node == "EXECUTING", child.node
    assert any(h.get("event") == "approve" and h.get("basis") == "parent_norm" for h in child.history)
    assert contour.state_bytes("parent-s") == parent_before


def test_e2e_child_revise_verdict_blocks_approval(contour, monkeypatch, capsys):
    _own_up_to_review(contour)
    rc = _run_review(contour, monkeypatch, capsys, "revise", ["blocking: C1: the criterion is unchecked"])
    assert rc == 1, f"BEHAVIOUR: a revise verdict on the child's own plan exited {rc}"
    reviews = contour.store.load(CHILD).plan_pair_reviews
    assert any(r.verdict == "revise" for r in reviews.values()), reviews
    blocked = contour.own("approve", "--by", "agent", **PLAN_REVIEW_ENV)
    assert blocked.returncode != 0, f"approve passed over a revise verdict: {blocked.stdout!r}"
    assert contour.store.load(CHILD).node == "PLAN_READY"


def test_child_approve_refused_without_review(contour):
    _own_up_to_review(contour)
    refused = contour.own("approve", "--by", "agent", **PLAN_REVIEW_ENV)
    assert refused.returncode != 0, f"approve passed with no review: {refused.stdout!r}"
    assert contour.store.load(CHILD).node == "PLAN_READY"
