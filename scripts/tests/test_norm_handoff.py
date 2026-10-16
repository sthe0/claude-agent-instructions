"""The norm handoff: a spawned specialist is an engine-linked manager of depth n+1.

`spawn-specialist.py` writes one digest-bound record naming the parent stage's norm,
its grants, the child's depth and the ancestor-norm chain, and exports it as
`AGENTCTL_NORM_HANDOFF=<path>:<sha256>`; the child's engine verifies it and adopts it as
`SessionState.parent_link` (and `recursion_depth`); `agentctl children` lists the
sessions linked to a parent.

Three tests -- `test_spawn_exports_norm_handoff`, `test_child_start_adopts_handoff_depth`,
`test_spawn_cap_counts_session_depth` -- are the stage's negative control: they drive
only entry points that exist on the base tree, build the handoff file by hand, and read
every new attribute through `getattr` with a default, so on base they fail on their
`BEHAVIOUR:` assertion rather than at collection or setup."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli
from conftest import spawn_handoff
from agentctl import state as state_module
from agentctl.plan import load_plan
from agentctl.stage_norm import StageNorm

HANDOFF_ENV = "AGENTCTL_NORM_HANDOFF"
SPAWN_SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"


def _load_spawn_module():
    spec = importlib.util.spec_from_file_location("spawn_specialist_norm_handoff", SPAWN_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SPAWN = _load_spawn_module()


def ns(**kw):
    return Namespace(**kw)


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_RECURSION_DEPTH", raising=False)
    monkeypatch.delenv("AGENTCTL_PARENT_DIFFICULTY", raising=False)
    root = tmp_path / "norms"
    root.mkdir()
    monkeypatch.setenv("AGENTCTL_NORM_ROOTS", str(root))
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")


def _cap() -> int:
    return cli.Thresholds().max_recursion_depth


def _start(store, sid, *, recursion_depth=0):
    return cli.cmd_start(ns(session=sid, task="norm-handoff", goal="", done_criterion="",
                            criterion_type="measurable", recursion_depth=recursion_depth),
                         store=store)


def _executing(store, sid, venued_fixtures_dir, plan="plan_two_stage.toml"):
    """A substantive session at EXECUTING stage 1, whose plan is the shared fixture."""
    _start(store, sid)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=str(venued_fixtures_dir / plan)), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)


def _plan_stage_norm(venued_fixtures_dir, index):
    doc = load_plan(str(venued_fixtures_dir / "plan_two_stage.toml"))
    stages = {s.index: s for s in doc.stages}
    stage = stages[index]
    suppliers = {e.on: stages[e.on] for e in stage.supplies if e.on in stages}
    return StageNorm.from_stage(stage, suppliers, doc=doc)


def _record(venued_fixtures_dir, *, parent_session="parent-s", stage_index=1, depth=1, parent_lineage=()):
    norm = _plan_stage_norm(venued_fixtures_dir, stage_index)
    digest = norm.norm_digest()
    entry = {"session": parent_session, "stage_index": stage_index, "norm_digest": digest,
             "title": norm.title, "done_criterion": norm.done_criterion}
    return {
        "schema": "norm-handoff/v1",
        "parent_session": parent_session,
        "stage_index": stage_index,
        "kind": "developer",
        "norm": norm.to_dict(),
        "norm_digest": digest,
        "grants": [],
        "depth": depth,
        "norm_lineage": [*parent_lineage, entry],
        "created_at": "2026-01-01T00:00:00+00:00",
    }


def _write_handoff(store, record, sid):
    """The `AGENTCTL_NORM_HANDOFF` value a real spawn exports for `sid`."""
    return spawn_handoff(store.root, record, sid)


class _Launched(Exception):
    def __init__(self, env):
        super().__init__("launched")
        self.env = env


def _spawn(monkeypatch, tmp_path, venued_fixtures_dir, session, *, stage_index=None):
    """Drive `spawn-specialist.py main` up to the child launch. Returns `(rc, env)`:
    `env` is the environment the child `claude -p` would get (None when the wrapper
    refused or stopped before launching), `rc` the wrapper's return code in that case."""
    monkeypatch.setattr(SPAWN.shutil, "which", lambda *a, **k: "/usr/bin/claude")

    def launch(cmd, env=None, **kw):
        raise _Launched(dict(env or {}))

    monkeypatch.setattr(SPAWN.proc_tree, "launch_supervised", launch)
    argv = [
        "--kind", "thinker", "--plan", str(venued_fixtures_dir / "plan_two_stage.toml"),
        "--done-criterion", "a verdict", "--criterion-type", "measurable",
        "--complexity", "medium", "--effort", "medium",
        "--session", session, "--state-root", str(tmp_path / "state"),
        "--workdir", str(tmp_path),
    ]
    if stage_index is not None:
        argv += ["--stage-index", str(stage_index)]
    try:
        return SPAWN.main(argv), None
    except _Launched as launched:
        return None, launched.env


def _read_exported(value):
    path_text, _, digest = value.rpartition(":")
    data = Path(path_text).read_bytes()
    return Path(path_text), digest, data, json.loads(data.decode("utf-8"))


def _link(monkeypatch, store, tmp_path, venued_fixtures_dir, sid, **record_kw):
    """Start `sid` under a handoff naming `parent_session` stage `stage_index`."""
    value = _write_handoff(store, _record(venued_fixtures_dir, **record_kw), sid)
    monkeypatch.setenv(HANDOFF_ENV, value)
    _start(store, sid)
    monkeypatch.delenv(HANDOFF_ENV)
    return value


# --- writer: the spawn wrapper ------------------------------------------------

def test_spawn_exports_norm_handoff(store, venued_fixtures_dir, tmp_path, monkeypatch):
    """negative control: a spawn for a session's stage hands the child one digest-bound
    record naming that stage's norm."""
    _executing(store, "w1", venued_fixtures_dir)
    rc, env = _spawn(monkeypatch, tmp_path, venued_fixtures_dir, "w1", stage_index=2)
    assert env is not None, f"setup: the spawn did not reach the launch (rc={rc})"
    exported = env.get(HANDOFF_ENV)
    assert exported is not None, \
        "BEHAVIOUR: a spawn for a session's stage exports no AGENTCTL_NORM_HANDOFF"
    path, digest, data, record = _read_exported(exported)
    assert hashlib.sha256(data).hexdigest() == digest, \
        "BEHAVIOUR: the exported digest is not the sha256 of the handoff file's bytes"
    expected = _plan_stage_norm(venued_fixtures_dir, 2)
    assert record.get("norm") == json.loads(json.dumps(expected.to_dict())), \
        "BEHAVIOUR: the handoff's norm is not StageNorm.from_stage of the plan's stage"
    assert record.get("norm_digest") == expected.norm_digest()
    assert record.get("depth") == 1 and env["AGENT_RECURSION_DEPTH"] == "1"
    assert path.is_absolute() and path.parent.name == "handoffs"


def test_record_carries_exactly_the_documented_fields(store, venued_fixtures_dir, tmp_path, monkeypatch):
    _executing(store, "w2", venued_fixtures_dir)
    _, env = _spawn(monkeypatch, tmp_path, venued_fixtures_dir, "w2", stage_index=1)
    _, _, _, record = _read_exported(env[HANDOFF_ENV])
    assert set(record) == {"schema", "parent_session", "stage_index", "kind", "norm",
                           "norm_digest", "grants", "depth", "norm_lineage", "created_at"}
    assert record["schema"] == "norm-handoff/v1"
    assert (record["parent_session"], record["stage_index"], record["kind"]) == ("w2", 1, "thinker")
    assert record["grants"] == []
    assert [e["session"] for e in record["norm_lineage"]] == ["w2"]
    assert record["norm_lineage"][-1]["norm_digest"] == record["norm_digest"]


def test_record_lineage_extends_the_parents_own_link(store, venued_fixtures_dir, tmp_path, monkeypatch):
    """A session that is itself a linked child hands its own children the chain it
    received, extended by the stage it is spawning for."""
    _link(monkeypatch, store, tmp_path, venued_fixtures_dir, "mid", parent_session="root-s",
          stage_index=1, depth=1)
    cli.cmd_classify(ns(session="mid", chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session="mid"), store=store)
    cli.cmd_submit_plan(ns(session="mid", plan=str(venued_fixtures_dir / "plan_two_stage.toml")), store=store)
    _, env = _spawn(monkeypatch, tmp_path, venued_fixtures_dir, "mid", stage_index=2)
    assert env is not None
    _, _, _, record = _read_exported(env[HANDOFF_ENV])
    assert [e["session"] for e in record["norm_lineage"]] == ["root-s", "mid"]
    assert record["depth"] == 2 and env["AGENT_RECURSION_DEPTH"] == "2"


def test_spawn_removes_an_inherited_handoff_it_cannot_replace(store, venued_fixtures_dir, tmp_path, monkeypatch):
    """An ancestor's value names the ancestor's stage; a spawn that cannot name its own
    passes none, never the inherited one."""
    monkeypatch.setenv(HANDOFF_ENV, "/ancestor/handoff.json:deadbeef")
    _executing(store, "w4", venued_fixtures_dir)
    rc, env = _spawn(monkeypatch, tmp_path, venued_fixtures_dir, "w4")
    assert env is not None, f"setup: the spawn did not reach the launch (rc={rc})"
    assert HANDOFF_ENV not in env, "BEHAVIOUR: an unlinked spawn passed an ancestor's handoff on"


def test_spawn_for_an_unresolvable_stage_exports_nothing(store, venued_fixtures_dir, tmp_path, monkeypatch):
    monkeypatch.setenv(HANDOFF_ENV, "/ancestor/handoff.json:deadbeef")
    _executing(store, "w5", venued_fixtures_dir)
    rc, env = _spawn(monkeypatch, tmp_path, venued_fixtures_dir, "w5", stage_index=99)
    assert env is not None, f"setup: the spawn did not reach the launch (rc={rc})"
    assert HANDOFF_ENV not in env
    assert not (tmp_path / "state" / "handoffs").exists()


def test_spawn_for_an_unknown_session_exports_nothing(venued_fixtures_dir, tmp_path, monkeypatch):
    rc, env = _spawn(monkeypatch, tmp_path, venued_fixtures_dir, "no-such-session", stage_index=1)
    assert env is not None, f"setup: the spawn did not reach the launch (rc={rc})"
    assert HANDOFF_ENV not in env


def test_handoff_write_failure_leaves_the_spawn_unlinked(store, venued_fixtures_dir, tmp_path, monkeypatch):
    """A handoff directory that cannot be created is a warning, not a failed spawn."""
    _executing(store, "w6", venued_fixtures_dir)
    (tmp_path / "state" / "handoffs").write_text("a file where the directory should be")
    rc, env = _spawn(monkeypatch, tmp_path, venued_fixtures_dir, "w6", stage_index=1)
    assert env is not None, f"the spawn failed instead of launching unlinked (rc={rc})"
    assert HANDOFF_ENV not in env


def test_spawn_never_writes_the_parent_session_state(store, venued_fixtures_dir, tmp_path, monkeypatch):
    _executing(store, "w7", venued_fixtures_dir)
    before = store.path("w7").read_bytes()
    _, env = _spawn(monkeypatch, tmp_path, venued_fixtures_dir, "w7", stage_index=1)
    assert env is not None and HANDOFF_ENV in env
    assert store.path("w7").read_bytes() == before


# --- reader: the child's engine -----------------------------------------------

def test_child_start_adopts_handoff_depth(store, venued_fixtures_dir, tmp_path, monkeypatch):
    """negative control: a session started under a verified handoff is linked to the
    parent stage and takes the depth the handoff names."""
    value = _write_handoff(store, _record(venued_fixtures_dir, parent_session="par", stage_index=1, depth=3), "r1")
    monkeypatch.setenv(HANDOFF_ENV, value)
    d = _start(store, "r1", recursion_depth=0)
    assert d.ok is True, d.detail
    state = store.load("r1")
    link = getattr(state, "parent_link", None)
    assert link is not None, "BEHAVIOUR: a session started under a verified handoff is not linked"
    assert (link["parent_session"], link["stage_index"], link["depth"]) == ("par", 1, 3)
    assert state.recursion_depth == 3, \
        f"BEHAVIOUR: the linked session's recursion_depth is {state.recursion_depth}, not the handoff's 3"
    assert link["norm_digest"] == _plan_stage_norm(venued_fixtures_dir, 1).norm_digest()
    assert any(e.get("event") == "norm_handoff" for e in state.history)


@pytest.mark.parametrize("case", ["tampered-bytes", "norm-digest-mismatch", "missing-file",
                                  "malformed-value", "lineage-not-ending-in-parent", "depth-zero"])
def test_an_untrusted_handoff_is_logged_and_ignored(store, venued_fixtures_dir, tmp_path, monkeypatch, capsys, case):
    record = _record(venued_fixtures_dir, parent_session="par", stage_index=1, depth=2)
    if case == "norm-digest-mismatch":
        record["norm_digest"] = "0" * 64
        record["norm_lineage"][-1]["norm_digest"] = "0" * 64
    elif case == "lineage-not-ending-in-parent":
        record["norm_lineage"][-1]["session"] = "somebody-else"
    elif case == "depth-zero":
        record["depth"] = 0
    value = _write_handoff(store, record, f"r-{case}")
    path_text, _, digest = value.rpartition(":")
    if case == "tampered-bytes":
        Path(path_text).write_bytes(Path(path_text).read_bytes() + b" ")
    elif case == "missing-file":
        Path(path_text).unlink()
    elif case == "malformed-value":
        value = "no-separator-here"
    monkeypatch.setenv(HANDOFF_ENV, value)
    d = _start(store, f"r-{case}", recursion_depth=0)
    assert d.ok is True, d.detail
    state = store.load(f"r-{case}")
    assert state.parent_link is None
    assert state.recursion_depth == 0
    events = [e for e in state.history if e.get("event") == "norm_handoff_ignored"]
    assert len(events) == 1 and events[0].get("reason")
    assert capsys.readouterr().err.count(HANDOFF_ENV) == 1


def test_unlinked_start_records_the_requested_depth_not_the_ambient_one(store, monkeypatch):
    monkeypatch.setenv("AGENT_RECURSION_DEPTH", "3")
    _start(store, "d1", recursion_depth=0)
    assert store.load("d1").recursion_depth == 0
    _start(store, "d2", recursion_depth=2)
    assert store.load("d2").recursion_depth == 2


def test_the_handoff_depth_wins_over_the_requested_one(store, venued_fixtures_dir, tmp_path, monkeypatch):
    monkeypatch.setenv(HANDOFF_ENV, _write_handoff(store, _record(venued_fixtures_dir, depth=4), "d3"))
    _start(store, "d3", recursion_depth=1)
    assert store.load("d3").recursion_depth == 4


def test_adoption_is_idempotent_and_a_different_value_is_logged(store, venued_fixtures_dir, tmp_path, monkeypatch):
    first = _link(monkeypatch, store, tmp_path, venued_fixtures_dir, "i1", parent_session="p1", depth=2)
    state = store.load("i1")
    other = _write_handoff(store, _record(venued_fixtures_dir, parent_session="p2", depth=5), "i1")
    monkeypatch.setenv(HANDOFF_ENV, other)
    cli._adopt_norm_handoff(state, store)
    assert state.parent_link["parent_session"] == "p1" and state.recursion_depth == 2
    assert [e["reason"] for e in state.history if e.get("event") == "norm_handoff_ignored"] \
        == ["session already linked"]
    monkeypatch.setenv(HANDOFF_ENV, first)
    cli._adopt_norm_handoff(state, store)
    assert len([e for e in state.history if e.get("event") == "norm_handoff_ignored"]) == 1


def test_reset_of_a_linked_session_keeps_the_link(store, venued_fixtures_dir, tmp_path, monkeypatch):
    _link(monkeypatch, store, tmp_path, venued_fixtures_dir, "i2", parent_session="p1", depth=2)
    d = cli.cmd_reset(ns(session="i2", task="another-task", goal="", done_criterion="",
                         criterion_type="measurable", recursion_depth=0, force=True), store=store)
    assert d.ok is True, d.detail
    state = store.load("i2")
    assert state.parent_link is not None and state.parent_link["parent_session"] == "p1"
    assert state.recursion_depth == 2


# --- depth cap ----------------------------------------------------------------

def test_spawn_cap_counts_session_depth(store, venued_fixtures_dir, tmp_path, monkeypatch, capsys):
    """negative control: a spawn whose environment carries no depth but whose engine
    session is already at the cap is refused (rc 3, `max-recursion-depth`)."""
    _start(store, "c1", recursion_depth=_cap())
    monkeypatch.delenv("AGENT_RECURSION_DEPTH", raising=False)
    rc, env = _spawn(monkeypatch, tmp_path, venued_fixtures_dir, "c1")
    assert env is None, \
        "BEHAVIOUR: a spawn from a session already at max-recursion-depth reached the launch"
    assert rc == 3, f"BEHAVIOUR: expected the recursion-cap refusal (rc 3), got rc={rc}"
    assert "max-recursion-depth" in capsys.readouterr().err


def test_spawn_below_the_cap_launches_with_the_session_depth_plus_one(store, venued_fixtures_dir, tmp_path, monkeypatch):
    _start(store, "c2", recursion_depth=_cap() - 1)
    rc, env = _spawn(monkeypatch, tmp_path, venued_fixtures_dir, "c2")
    assert env is not None, f"the spawn at depth cap-1 was refused (rc={rc})"
    assert env["AGENT_RECURSION_DEPTH"] == str(_cap())


def test_spawn_cap_still_reads_the_environment_depth(store, venued_fixtures_dir, tmp_path, monkeypatch):
    _start(store, "c3", recursion_depth=0)
    monkeypatch.setenv("AGENT_RECURSION_DEPTH", str(_cap()))
    rc, env = _spawn(monkeypatch, tmp_path, venued_fixtures_dir, "c3")
    assert env is None and rc == 3


# --- observation: children / status -------------------------------------------

def test_children_lists_linked_sessions_only_and_writes_nothing(store, venued_fixtures_dir, tmp_path, monkeypatch):
    _start(store, "par")
    _link(monkeypatch, store, tmp_path, venued_fixtures_dir, "kid-a", parent_session="par", stage_index=1, depth=1)
    _link(monkeypatch, store, tmp_path, venued_fixtures_dir, "kid-b", parent_session="par", stage_index=2, depth=1)
    _link(monkeypatch, store, tmp_path, venued_fixtures_dir, "other-kid", parent_session="elsewhere")
    _start(store, "stranger")
    files = sorted(store.root.glob("*.json"))
    before = {p.name: p.read_bytes() for p in files}

    d = cli.cmd_children(ns(session="par", json=True), store=store)

    assert d.ok is True
    rows = {c["session"]: c for c in d.data["children"]}
    assert sorted(rows) == ["kid-a", "kid-b"]
    assert rows["kid-a"]["stage_index"] == 1 and rows["kid-b"]["stage_index"] == 2
    assert rows["kid-a"]["recursion_depth"] == 1
    assert rows["kid-a"]["difficulty_level"] is None and rows["kid-a"]["resolved"] is False
    assert json.loads(d.detail) == d.data
    assert sorted(store.root.glob("*.json")) == files
    assert {p.name: p.read_bytes() for p in files} == before


def test_children_reports_the_open_difficulty_level_and_resolution(store, venued_fixtures_dir, tmp_path, monkeypatch):
    _start(store, "par2")
    value = _write_handoff(store, _record(venued_fixtures_dir, parent_session="par2"), "kid-d")
    monkeypatch.setenv(HANDOFF_ENV, value)
    _executing(store, "kid-d", venued_fixtures_dir)
    monkeypatch.delenv(HANDOFF_ENV)
    cli.cmd_record_result(ns(session="kid-d", status="failed", actual="boom"), store=store)
    _link(monkeypatch, store, tmp_path, venued_fixtures_dir, "kid-r", parent_session="par2")
    path = store.path("kid-r")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["node"] = state_module.Node.RESOLVED.value
    data["resolution"]["passed"] = True
    path.write_text(json.dumps(data), encoding="utf-8")

    d = cli.cmd_children(ns(session="par2", json=False), store=store)

    rows = {c["session"]: c for c in d.data["children"]}
    assert rows["kid-d"]["difficulty_level"] == store.load("kid-d").difficulty.level
    assert rows["kid-d"]["difficulty_level"] is not None
    assert rows["kid-r"]["resolved"] is True
    assert "kid-d" in d.detail and "resolved" in d.detail


def test_children_of_a_session_nobody_linked_is_empty(store):
    _start(store, "lonely")
    d = cli.cmd_children(ns(session="lonely", json=False), store=store)
    assert d.ok is True and d.data["children"] == []


def test_status_reports_the_parent_link(store, venued_fixtures_dir, tmp_path, monkeypatch):
    _link(monkeypatch, store, tmp_path, venued_fixtures_dir, "st1", parent_session="par", stage_index=2, depth=3)
    _start(store, "st2")
    linked = cli.cmd_status(ns(session="st1"), store=store).data["parent_link"]
    assert linked == {"parent_session": "par", "stage_index": 2, "depth": 3,
                      "norm_digest": _plan_stage_norm(venued_fixtures_dir, 2).norm_digest()}
    assert cli.cmd_status(ns(session="st2"), store=store).data["parent_link"] is None


def test_children_is_a_registered_session_command():
    assert "children" in cli._SESSION_COMMANDS and "children" in cli.COMMANDS
    from lib.widening_targets import AGENTCTL_USER_AUTHORITY_VERBS
    assert "children" not in AGENTCTL_USER_AUTHORITY_VERBS


# --- schema -------------------------------------------------------------------

def test_state_at_the_previous_version_loads_unlinked(store):
    """A state file written before `parent_link` existed loads with no link and keeps
    its recursion_depth; its schema version stays the one the file declared."""
    _start(store, "m1", recursion_depth=2)
    path = store.path("m1")
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("parent_link", None)
    data["schema_version"] = state_module.SCHEMA_VERSION - 1
    path.write_text(json.dumps(data), encoding="utf-8")

    loaded = store.load("m1")

    assert loaded.parent_link is None
    assert loaded.recursion_depth == 2
    assert loaded.schema_version == state_module.SCHEMA_VERSION - 1
    _start(store, "m2")
    assert store.load("m2").schema_version == state_module.SCHEMA_VERSION
