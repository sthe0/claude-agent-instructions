"""A linked child authors its plan in a file of its own, and no other session writes it.

A spawn for a parent stage gives the child `own_plans_dir()/<sid>/plan.toml` -- granted to
that child alone (a settings allow rule for the exact file, an `--add-dir` for its
directory, a line in the brief) -- and the PreToolUse gate (`hook-state-gate.py`) decides
who may write it: the owner, through the canonical path, while it plans; nobody once it
executes; no other session at all (`foreign-own-plan`), whatever alias of the path reaches
the file. The parent's plan stays gated by the parent's node, an unlinked spawn gets no
such file, and a declared grant under the agent home stays refused.

The hook runs as a subprocess against a fictitious agent home (`FICT_HOME`): a path under
the pytest temp root is scratch and exempt from the gate, which would hide whether the gate
sees the plan at all. Session state is found through HOME's legacy state directory.

Twelve tests are the stage's negative control: they drive only entry points the base tree
has, import the new symbols inside their bodies, and put `BEHAVIOUR:` on the assertion
that checks the behaviour under repair. Three -- `test_unlinked_spawn_gets_no_own_plan_grant`,
`test_child_write_to_parent_plan_refused`,
`test_declared_grant_under_agent_home_still_refused` -- pin what already holds on base.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from test_child_contour_gate import (
    FIXTURE_PLAN, HANDOFF_ENV, HOOK, OWN_ENV, PARENT, Launch, _child_env, _denied, _Launched,
    _load_spawn, _parent_session, _reason, _settings_allow, _state_dir, _write_state,
)

CHILD = "child-s"
SIBLING = "sibling-s"
ROOT = "root-s"
FICT_HOME = "/work/agent-home"
SCRIPTS = Path(__file__).resolve().parent.parent
SPECIALIZATIONS = SCRIPTS.parent / "skills" / "specializations"


def _own_plan(sid: str, agent_home: str = FICT_HOME) -> str:
    return f"{agent_home}/own-plans/{sid}/plan.toml"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    for name in ("CLAUDE_CONFIG_DIR", "CLAUDE_AGENT_HOME", "AGENT_RECURSION_DEPTH", HANDOFF_ENV,
                 OWN_ENV, "CLAUDE_CODE_SESSION_ID", "AGENTCTL_PARENT_DIFFICULTY"):
        monkeypatch.delenv(name, raising=False)
    norms = tmp_path / "norms"
    norms.mkdir()
    monkeypatch.setenv("AGENTCTL_NORM_ROOTS", str(norms))
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")


@pytest.fixture
def home(tmp_path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def agent_home(tmp_path, home, monkeypatch) -> Path:
    """A real agent home for the in-process tests; the hook tests use `FICT_HOME`."""
    path = home / ".claude"
    path.mkdir()
    skills = path / "skills"
    skills.mkdir()
    for source in (SCRIPTS.parent / "skills", SPECIALIZATIONS):
        for entry in source.iterdir():
            if entry.is_dir() and not (skills / entry.name).exists():
                (skills / entry.name).symlink_to(entry)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(path))
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(path))
    return path


def _edit(home: Path, session: str | None, path: str, *, depth: str | None = None,
          agent_home: str = FICT_HOME, **env: str) -> subprocess.CompletedProcess:
    payload = {"tool_name": "Edit", "session_id": session or "", "tool_input": {"file_path": path}}
    extra = {"CLAUDE_CONFIG_DIR": agent_home, **env}
    if depth is not None:
        extra["AGENT_RECURSION_DEPTH"] = depth
    return subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True,
        text=True, env=_child_env(home, **extra),
    )


def _child_edit(home: Path, path: str, **kw) -> subprocess.CompletedProcess:
    """An edit by the linked child, as its harness runs it."""
    return _edit(home, CHILD, path, depth="1", **{OWN_ENV: CHILD}, **kw)


def _assert_foreign_refused(proc: subprocess.CompletedProcess, who: str) -> None:
    assert _denied(proc), f"BEHAVIOUR: {who} was allowed to write the child's own plan"
    assert "foreign-own-plan" in _reason(proc), (
        f"BEHAVIOUR: the denial of {who} does not say foreign-own-plan: {_reason(proc)!r}"
    )


# --- the spawn -------------------------------------------------------------------------


def _launch(monkeypatch, tmp_path: Path, *, kind: str = "thinker", stage_index: int | None,
            dry_run: bool = False) -> Launch:
    """Run `spawn-specialist.py main` up to the child launch. The workdir is a directory of
    its own, apart from the agent home, so no repo-root deny sits over the plan file."""
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    spawn = _load_spawn()
    monkeypatch.setattr(spawn.shutil, "which", lambda *a, **k: "/usr/bin/claude")

    def launch(cmd, env=None, **kw):
        raise _Launched(list(cmd), dict(env or {}))

    monkeypatch.setattr(spawn.proc_tree, "launch_supervised", launch)
    argv = [
        "--kind", kind, "--plan", str(FIXTURE_PLAN),
        "--done-criterion", "a verdict", "--criterion-type", "measurable",
        "--complexity", "medium", "--effort", "medium",
        "--session", PARENT, "--state-root", str(_state_dir(tmp_path / "home")),
        "--workdir", str(work),
    ]
    if stage_index is not None:
        argv += ["--stage-index", str(stage_index)]
    if dry_run:
        argv.append("--dry-run")
    try:
        return Launch(spawn.main(argv), None, None)
    except _Launched as launched:
        return Launch(None, launched.cmd, launched.env)


def _add_dirs(cmd: list[str]) -> list[str]:
    return [cmd[i + 1] for i, token in enumerate(cmd) if token == "--add-dir"]


def _denies(cmd: list[str]) -> list[str]:
    settings = json.loads(cmd[cmd.index("--settings") + 1])
    return settings.get("permissions", {}).get("deny", [])


def _all_kinds() -> list[str]:
    return sorted({p.parent.name for p in SPECIALIZATIONS.glob("*/SKILL.md")} | {"manager"})


def test_linked_spawn_grants_exactly_its_own_plan_file(tmp_path, home, agent_home, monkeypatch):
    _parent_session(home)
    launched = _launch(monkeypatch, tmp_path, stage_index=1)
    assert launched.env is not None, f"setup: the spawn did not reach the launch (rc={launched.rc})"
    own = launched.option("--session-id")
    assert own, "setup: the linked spawn passed no --session-id"
    plan = agent_home / "own-plans" / own / "plan.toml"
    mentioning = [rule for rule in _settings_allow(launched.cmd) if "own-plans" in rule]
    assert mentioning == [f"Edit(/{plan})"], (
        f"BEHAVIOUR: the child's allow rules naming its own plan are {mentioning!r}, "
        f"not exactly its plan file {plan}"
    )
    dirs = _add_dirs(launched.cmd)
    assert str(plan.parent) in dirs, f"BEHAVIOUR: --add-dir does not reach {plan.parent}: {dirs!r}"
    assert str(agent_home / "own-plans") not in dirs and str(agent_home) not in dirs, (
        f"the grant reaches past the child's own directory: {dirs!r}"
    )
    assert plan.parent.is_dir(), "BEHAVIOUR: the child's plan directory was not created at spawn"
    assert (plan.parent.stat().st_mode & 0o777) == 0o700

    # the allow must survive every kind's deny list: deny beats allow with no diagnostic
    spawn = _load_spawn()
    for kind in _all_kinds():
        run = _launch(monkeypatch, tmp_path, kind=kind, stage_index=1)
        assert run.env is not None, f"setup: kind {kind} did not reach the launch (rc={run.rc})"
        child = run.option("--session-id")
        rule = f"Edit(//{agent_home / 'own-plans' / child / 'plan.toml'})"
        shadowing = [d for d in _denies(run.cmd) if spawn._edit_deny_covers_allow(d, rule)]
        assert not shadowing, f"kind {kind}: {shadowing!r} covers the child's own-plan allow {rule}"


def test_linked_brief_names_own_plan_path(tmp_path, home, agent_home, monkeypatch, capsys):
    _parent_session(home)
    launched = _launch(monkeypatch, tmp_path, stage_index=1, dry_run=True)
    assert launched.rc == 0, f"setup: the dry run failed (rc={launched.rc})"
    out = capsys.readouterr().out
    section = out.split("## Your own engine contour", 1)[1].split("\n## ", 1)[0]
    found = re.search(r"Your plan file is `([^`]+)`", section)
    assert found, "BEHAVIOUR: the linked brief does not name the child's plan file"
    path = Path(found.group(1))
    assert path.parent.parent == agent_home / "own-plans" and path.name == "plan.toml", path
    assert f"submit-plan --plan {path}" in section, "the brief does not tell the child to submit that file"


def test_linked_child_writes_own_plan_at_planning(home):
    plan = _own_plan(CHILD)
    for node in ("CLASSIFIED", "ROUTED", "PLANNING", "PLAN_READY"):
        _write_state(home, CHILD, node, linked=True)
        proc = _child_edit(home, plan)
        assert proc.returncode == 0
        assert not _denied(proc), (
            f"BEHAVIOUR: a linked child at {node} was denied its own plan: {proc.stdout!r}"
        )


def test_own_plan_frozen_after_planning(home):
    plan = _own_plan(CHILD)
    for node in ("APPROVED", "PARTITIONED", "EXECUTING", "VERIFYING", "RESOLUTION"):
        _write_state(home, CHILD, node, linked=True)
        proc = _child_edit(home, plan)
        assert _denied(proc), f"BEHAVIOUR: a linked child at {node} was allowed to rewrite its own plan"
        assert "replan" in _reason(proc), (
            f"BEHAVIOUR: the denial at {node} does not point to `replan`: {_reason(proc)!r}"
        )


def test_foreign_own_plan_write_refused_for_root(home):
    _write_state(home, ROOT, "EXECUTING", linked=False)
    _assert_foreign_refused(_edit(home, ROOT, _own_plan(CHILD)), "the root session")


def test_foreign_own_plan_write_refused_for_linked_sibling(home):
    _write_state(home, SIBLING, "EXECUTING", linked=True)
    _assert_foreign_refused(
        _edit(home, SIBLING, _own_plan(CHILD), depth="1", **{OWN_ENV: SIBLING}), "a linked sibling",
    )
    _write_state(home, SIBLING, "PLANNING", linked=True)
    _assert_foreign_refused(
        _edit(home, SIBLING, _own_plan(CHILD), depth="1", **{OWN_ENV: SIBLING}),
        "a linked sibling that is itself planning",
    )


def test_foreign_own_plan_write_refused_for_unlinked_child(home):
    _write_state(home, "specialist-s", "EXECUTING", linked=False)
    _assert_foreign_refused(_edit(home, "specialist-s", _own_plan(CHILD), depth="1"), "an unlinked specialist")


def test_foreign_own_plan_write_refused_through_dotdot_path(home):
    _write_state(home, ROOT, "EXECUTING", linked=False)
    _assert_foreign_refused(
        _edit(home, ROOT, f"{FICT_HOME}/own-plans/elsewhere/../{CHILD}/plan.toml"),
        "a session writing through a `..` path",
    )


def test_foreign_own_plan_write_refused_through_symlink(tmp_path, home):
    _write_state(home, ROOT, "EXECUTING", linked=False)
    alias = tmp_path / "alias.toml"
    alias.symlink_to(_own_plan(CHILD))
    dir_alias = tmp_path / "other-sid"
    dir_alias.symlink_to(f"{FICT_HOME}/own-plans/{CHILD}")
    _assert_foreign_refused(_edit(home, ROOT, str(alias)), "a session writing through a file symlink")
    _assert_foreign_refused(
        _edit(home, ROOT, str(dir_alias / "plan.toml")), "a session writing through a directory symlink",
    )


def test_foreign_own_plan_write_refused_without_engine_state(home):
    _assert_foreign_refused(_edit(home, "never-started", _own_plan(CHILD)), "a session with no engine state")


def test_is_own_plan_file_rejects_sibling_symlink_and_dotdot(tmp_path, agent_home):
    from agentctl.exempt_paths import is_own_plan_file, own_plan_owner

    root = agent_home / "own-plans"
    (root / CHILD).mkdir(parents=True)
    canonical = str(root / CHILD / "plan.toml")
    assert is_own_plan_file(canonical) == CHILD
    assert own_plan_owner(canonical) == CHILD

    assert is_own_plan_file(str(root / CHILD / "notes.toml")) is None
    assert is_own_plan_file(str(root / "plan.toml")) is None
    assert is_own_plan_file(str(root / CHILD / "deeper" / "plan.toml")) is None
    assert is_own_plan_file(str(agent_home / "plan.toml")) is None
    assert is_own_plan_file("own-plans/%s/plan.toml" % CHILD) is None
    assert is_own_plan_file("") is None

    dotdot = f"{root}/elsewhere/../{CHILD}/plan.toml"
    assert is_own_plan_file(dotdot) is None
    assert own_plan_owner(dotdot) == CHILD, "the broad predicate must still see through `..`"

    (tmp_path / "plan-alias.toml").symlink_to(canonical)
    assert is_own_plan_file(str(tmp_path / "plan-alias.toml")) is None
    assert own_plan_owner(str(tmp_path / "plan-alias.toml")) == CHILD
    (root / "sid-alias").symlink_to(root / CHILD)
    assert is_own_plan_file(str(root / "sid-alias" / "plan.toml")) is None
    assert own_plan_owner(str(root / "sid-alias" / "plan.toml")) == CHILD
    assert own_plan_owner(str(root / CHILD / "notes.toml")) is None


def test_unlinked_spawn_gets_no_own_plan_grant(tmp_path, home, agent_home, monkeypatch, capsys):
    _parent_session(home)
    launched = _launch(monkeypatch, tmp_path, stage_index=None)
    assert launched.env is not None, f"setup: the spawn did not reach the launch (rc={launched.rc})"
    assert not [rule for rule in _settings_allow(launched.cmd) if "own-plans" in rule]
    assert not [d for d in _add_dirs(launched.cmd) if "own-plans" in d]
    assert not (agent_home / "own-plans").exists()
    assert _launch(monkeypatch, tmp_path, stage_index=None, dry_run=True).rc == 0
    assert "own-plans" not in capsys.readouterr().out

    monkeypatch.setenv("AGENTCTL_CHILD_LINKING", "0")
    switched_off = _launch(monkeypatch, tmp_path, stage_index=1)
    assert switched_off.env is not None, f"setup: the spawn did not reach the launch (rc={switched_off.rc})"
    assert not [rule for rule in _settings_allow(switched_off.cmd) if "own-plans" in rule]
    assert not [d for d in _add_dirs(switched_off.cmd) if "own-plans" in d]
    assert not (agent_home / "own-plans").exists()


def test_child_write_to_parent_plan_refused(home):
    parent_plan = "/work/agent-home/.claude-agent/plans/parent-plan.toml"
    directory = _state_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{PARENT}.json").write_text(json.dumps({
        "node": "EXECUTING", "weight_class": "SUBSTANTIVE", "plan_path": parent_plan,
    }), encoding="utf-8")
    for node in ("CLASSIFIED", "PLANNING", "PLAN_READY"):
        _write_state(home, CHILD, node, linked=True)
        proc = _child_edit(home, parent_plan)
        assert _denied(proc), f"a linked child at {node} was allowed to rewrite its parent's approved plan"
        assert "another live session" in _reason(proc)


def test_declared_grant_under_agent_home_still_refused(agent_home):
    from agentctl.grants import GrantValidationError, validate_rule

    for target in (agent_home / "own-plans" / CHILD / "plan.toml", agent_home / "own-plans" / "x.toml",
                   agent_home / "plans" / "plan.toml"):
        with pytest.raises(GrantValidationError):
            validate_rule(f"Edit(//{target})")


def test_own_plan_alias_write_refused_at_executing(tmp_path, home):
    alias = tmp_path / "alias.toml"
    alias.symlink_to(_own_plan(CHILD))
    for node in ("PLANNING", "EXECUTING"):
        _write_state(home, CHILD, node, linked=True)
        assert _denied(_child_edit(home, str(alias))), (
            f"BEHAVIOUR: the owner at {node} wrote its own plan through a symlink alias"
        )
        dotdot = f"{FICT_HOME}/own-plans/elsewhere/../{CHILD}/plan.toml"
        assert _denied(_child_edit(home, dotdot)), (
            f"BEHAVIOUR: the owner at {node} wrote its own plan through a `..` path"
        )


def test_own_plan_write_allowed_with_symlinked_agent_home(tmp_path, home):
    real = tmp_path / "real-home"
    (real / "own-plans" / CHILD).mkdir(parents=True)
    link = tmp_path / "link-home"
    link.symlink_to(real)
    plan = _own_plan(CHILD, str(link))

    _write_state(home, CHILD, "PLANNING", linked=True)
    proc = _child_edit(home, plan, agent_home=str(link))
    assert not _denied(proc), (
        f"BEHAVIOUR: the owner was denied its own plan through a symlinked agent home: {proc.stdout!r}"
    )

    _write_state(home, CHILD, "EXECUTING", linked=True)
    assert _denied(_child_edit(home, plan, agent_home=str(link))), (
        "BEHAVIOUR: through a symlinked agent home the own plan is not recognised, so it is not frozen"
    )
