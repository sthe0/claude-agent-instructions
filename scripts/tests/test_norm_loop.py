"""Delegation rules at the spawn seam: a linked session may not re-delegate its own
norm, nor delegate a stage that would hold more authority than the stage that spawned it.

`spawn-specialist.py` refuses before any child launches -- an exact `norm_digest`
match against the session's `parent_link.norm_lineage` is a loop (exit 3,
`LOOP_DETECTED:`), a stage grant its handoff and user-resolved runtime grants do not
cover is a widening (exit 4, `Refusal: grant-widening`). A child session at or above
`loop-sensitivity-depth` is told, at classify, to return `LOOP_DETECTED` itself when its
task re-frames an ancestor's: exact digest equality is the engine's rule, "re-framing"
is the model's perception.

Three tests -- `test_spawn_refuses_unchanged_norm_redelegation`,
`test_spawn_refuses_grant_widening`, `test_loop_detected_marker_routes_to_escalate` --
are the stage's negative control: they drive only entry points that exist on the base
tree (`cmd_start` under a hand-written handoff, spawn-specialist's `main`, `cmd_dispatch`
with a fake runner) and read every new attribute through `getattr` with a default, so on
base they fail on their `BEHAVIOUR:` assertion rather than at collection or setup."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli
from agentctl.dispatch import RunResult
from agentctl.plan import load_plan
from agentctl.stage_norm import StageNorm
from agentctl.state import Node

HANDOFF_ENV = "AGENTCTL_NORM_HANDOFF"
SPAWN_SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"


def _load_spawn_module():
    spec = importlib.util.spec_from_file_location("spawn_specialist_norm_loop", SPAWN_SCRIPT)
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
    monkeypatch.delenv(HANDOFF_ENV, raising=False)
    root = tmp_path / "norms"
    root.mkdir()
    monkeypatch.setenv("AGENTCTL_NORM_ROOTS", str(root))
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")


@pytest.fixture
def refusals(monkeypatch):
    """Every `log_refused(reason, extra)` the wrapper makes, in order."""
    rows: list[tuple[str, dict]] = []
    monkeypatch.setattr(SPAWN, "log_refused", lambda reason, extra: rows.append((reason, extra)))
    return rows


# --- fixtures: plans, handoff records, linked sessions ------------------------

def _plan_text(fixtures_dir, *, grants_toml: str = "", stage1_done: str | None = None) -> str:
    text = (fixtures_dir / "plan_two_stage.toml").read_text()
    if stage1_done is not None:
        text = text.replace("python -c 'import mod' exits 0", stage1_done, 1)
    if grants_toml:
        text = text.replace(
            'output_artifacts = ["mod.py"]\n', 'output_artifacts = ["mod.py"]\n\n' + grants_toml, 1,
        )
    return text


def _write_plan(fixtures_dir, tmp_path, name, **kw) -> str:
    out = tmp_path / name
    out.write_text(_plan_text(fixtures_dir, **kw))
    return str(out)


def _stage_norm(plan_path, index):
    doc = load_plan(plan_path)
    stages = {s.index: s for s in doc.stages}
    stage = stages[index]
    suppliers = {e.on: stages[e.on] for e in stage.supplies if e.on in stages}
    return StageNorm.from_stage(stage, suppliers, doc=doc)


def _lineage_entry(plan_path, index, session):
    norm = _stage_norm(plan_path, index)
    return {"session": session, "stage_index": index, "norm_digest": norm.norm_digest(),
            "title": norm.title, "done_criterion": norm.done_criterion}


def _record(plan_path, *, parent_session="par-s", stage_index=1, depth=1,
            parent_lineage=(), grants=()):
    norm = _stage_norm(plan_path, stage_index)
    entry = _lineage_entry(plan_path, stage_index, parent_session)
    return {
        "schema": "norm-handoff/v1",
        "parent_session": parent_session,
        "stage_index": stage_index,
        "kind": "developer",
        "norm": norm.to_dict(),
        "norm_digest": norm.norm_digest(),
        "grants": list(grants),
        "depth": depth,
        "norm_lineage": [*parent_lineage, entry],
        "created_at": "2026-01-01T00:00:00+00:00",
    }


def _handoff_value(tmp_path, record, name):
    path = tmp_path / name
    payload = json.dumps(record, sort_keys=True).encode("utf-8")
    path.write_bytes(payload)
    return f"{path}:{hashlib.sha256(payload).hexdigest()}"


def _executing(store, sid, plan_path, *, record=None, tmp_path=None, monkeypatch=None):
    """A substantive session at EXECUTING stage 1 on `plan_path`; when `record` is
    given, started under that handoff so it is a linked child."""
    if record is not None:
        monkeypatch.setenv(HANDOFF_ENV, _handoff_value(tmp_path, record, f"{sid}.json"))
    cli.cmd_start(ns(session=sid, task="norm-loop", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    if record is not None:
        monkeypatch.delenv(HANDOFF_ENV)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan_path), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)


class _Launched(Exception):
    def __init__(self, env):
        super().__init__("launched")
        self.env = env


def _spawn(monkeypatch, tmp_path, plan_path, session, *, stage_index, kind="developer"):
    """Drive `spawn-specialist.py main` up to the child launch. Returns `(rc, env)`:
    `env` is the environment the child `claude -p` would get (None when the wrapper
    refused before launching), `rc` the wrapper's return code in that case."""
    monkeypatch.setattr(SPAWN.shutil, "which", lambda *a, **k: "/usr/bin/claude")

    def launch(cmd, env=None, **kw):
        raise _Launched(dict(env or {}))

    monkeypatch.setattr(SPAWN.proc_tree, "launch_supervised", launch)
    argv = [
        "--kind", kind, "--plan", plan_path,
        "--done-criterion", "tests green", "--criterion-type", "measurable",
        "--complexity", "medium", "--effort", "medium",
        "--session", session, "--stage-index", str(stage_index),
        "--state-root", str(tmp_path / "state"), "--workdir", str(tmp_path),
    ]
    try:
        return SPAWN.main(argv), None
    except _Launched as launched:
        return None, launched.env


def _engine_grants(plan_path, tmp_path, session, stage_index=1, kind="developer"):
    return SPAWN.load_engine_stage_grants(session, stage_index, kind, state_root=tmp_path / "state")


DECLARED = "[stage.grants]\nallow = [\"Bash(git status:*)\", \"Bash(git diff:*)\"]\n"


def _grants_plan(fixtures_dir, tmp_path):
    return _write_plan(fixtures_dir, tmp_path, "plan_grants.toml", grants_toml=DECLARED)


def _child_directive_runner(returncode, stdout="", stderr=""):
    return lambda argv: RunResult(returncode, stdout=stdout, stderr=stderr)


def _dispatch(store, sid, runner):
    return cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
                               dry_run=False), store=store, runner=runner)


# --- (a) loop rule at the spawn seam ------------------------------------------

def test_spawn_refuses_unchanged_norm_redelegation(store, fixtures_dir, tmp_path, monkeypatch,
                                                   capsys, refusals):
    """negative control: a linked session delegating the stage whose norm is the one it
    was itself spawned for is refused before any child launches."""
    plan = str(fixtures_dir / "plan_two_stage.toml")
    record = _record(plan, parent_session="par-s", stage_index=1, depth=1)
    _executing(store, "own", plan, record=record, tmp_path=tmp_path, monkeypatch=monkeypatch)

    rc, env = _spawn(monkeypatch, tmp_path, plan, "own", stage_index=1)

    assert env is None, \
        "BEHAVIOUR: a linked session re-delegated its own parent stage's norm unchanged"
    assert rc == 3, f"BEHAVIOUR: expected the norm-loop refusal (rc 3), got rc={rc}"
    first = capsys.readouterr().out.splitlines()[0]
    assert first.startswith("LOOP_DETECTED:"), \
        f"BEHAVIOUR: the refusal's first stdout line is not LOOP_DETECTED: {first!r}"
    assert "par-s" in first and "stage 1" in first
    assert [r for r, _ in refusals] == ["norm-loop"]
    assert not (tmp_path / "state" / "handoffs").exists(), "a refused spawn wrote a handoff"


def test_spawn_refuses_a_norm_of_a_non_parent_ancestor(store, fixtures_dir, tmp_path, monkeypatch,
                                                      capsys, refusals):
    """The lineage is the whole ancestor chain, not only the immediate parent."""
    plan = str(fixtures_dir / "plan_two_stage.toml")
    root_entry = _lineage_entry(plan, 1, "root-s")
    record = _record(plan, parent_session="mid-s", stage_index=2, depth=2,
                     parent_lineage=[root_entry])
    _executing(store, "deep", plan, record=record, tmp_path=tmp_path, monkeypatch=monkeypatch)

    rc, env = _spawn(monkeypatch, tmp_path, plan, "deep", stage_index=1)

    assert env is None and rc == 3, \
        "BEHAVIOUR: a norm an ancestor above the parent already delegated was re-delegated"
    first = capsys.readouterr().out.splitlines()[0]
    assert first.startswith("LOOP_DETECTED:") and "root-s" in first
    assert refusals and refusals[0][0] == "norm-loop"
    assert refusals[0][1]["matching"]["session"] == "root-s"


def test_spawn_admits_an_edited_norm(store, fixtures_dir, tmp_path, monkeypatch, refusals):
    """A stage whose norm differs from every lineage entry -- the plan edited the done
    criterion -- is a different task and launches."""
    parent_plan = str(fixtures_dir / "plan_two_stage.toml")
    child_plan = _write_plan(fixtures_dir, tmp_path, "plan_edited.toml",
                             stage1_done="python -c 'import mod; mod.run()' exits 0")
    record = _record(parent_plan, parent_session="par-s", stage_index=1, depth=1,
                     grants=_rules("Bash(python3 mod.py:*)", "Edit(//./mod.py)"))
    _executing(store, "edited", child_plan, record=record, tmp_path=tmp_path, monkeypatch=monkeypatch)

    rc, env = _spawn(monkeypatch, tmp_path, child_plan, "edited", stage_index=1)

    assert env is not None, f"BEHAVIOUR: an edited norm was refused (rc={rc})"
    assert refusals == []


def test_spawn_never_loop_checks_an_unlinked_session(store, fixtures_dir, tmp_path, monkeypatch, refusals):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _executing(store, "free", plan)

    rc, env = _spawn(monkeypatch, tmp_path, plan, "free", stage_index=1)

    assert env is not None, f"an unlinked session's spawn was refused (rc={rc})"
    assert refusals == []


def test_dispatch_routes_a_norm_loop_refusal_to_a_loop_escalation(store, fixtures_dir, monkeypatch):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _executing(store, "dl", plan)
    line = "LOOP_DETECTED: stage 1 carries the norm already delegated at stage 1 of session par-s"

    d = _dispatch(store, "dl", _child_directive_runner(3, stdout=line + "\n"))

    assert not d.ok and d.node == Node.BLOCKED.value and d.marker == "ESCALATE"
    loop = (d.data or {}).get("loop_detected")
    assert loop == line, "BEHAVIOUR: the norm-loop refusal's LOOP_DETECTED line is not on the directive"
    assert "norm-loop" in d.detail
    events = [e for e in store.load("dl").history if e.get("event") == "dispatch_refused"]
    assert [e.get("reason") for e in events] == ["norm-loop"]


def test_dispatch_cap_refusal_keeps_its_own_route(store, fixtures_dir):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _executing(store, "dc", plan)

    d = _dispatch(store, "dc", _child_directive_runner(3, stderr="above max-recursion-depth=5\n"))

    assert d.marker == "ESCALATE" and "recursion cap" in d.detail
    assert "loop_detected" not in (d.data or {})
    events = [e for e in store.load("dc").history if e.get("event") == "dispatch_refused"]
    assert [e.get("reason") for e in events] == ["recursion-cap"]


# --- (c) the LOOP_DETECTED return marker --------------------------------------

def test_loop_detected_marker_routes_to_escalate(store, fixtures_dir):
    """negative control: a child returning LOOP_DETECTED parks the stage like ESCALATE,
    the report carried on the directive."""
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _executing(store, "lm", plan)

    d = _dispatch(store, "lm", _child_directive_runner(
        0, stdout="LOOP_DETECTED: this task re-frames the parent stage's\n"))

    assert d.marker == "ESCALATE" and d.node == Node.BLOCKED.value and not d.ok
    loop = (d.data or {}).get("loop_detected")
    assert loop is not None and "re-frames the parent stage's" in loop, \
        "BEHAVIOUR: a child's LOOP_DETECTED report did not reach the directive"


def test_return_marker_tuples_are_identical_and_carry_loop_detected():
    from agentctl import dispatch as engine_dispatch
    from lib import planner_plan_check

    assert "LOOP_DETECTED" in engine_dispatch.RETURN_MARKERS
    assert tuple(engine_dispatch.RETURN_MARKERS) == tuple(planner_plan_check.RETURN_MARKERS)


def test_other_markers_keep_their_routes(store, fixtures_dir):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _executing(store, "om", plan)
    d = _dispatch(store, "om", _child_directive_runner(0, stdout="CLARIFY: which file?\n"))
    assert d.marker == "CLARIFY" and d.action == "answer_clarify"
    assert "loop_detected" not in (d.data or {})


# --- (b) classify-time self check ---------------------------------------------

def _classify(store, sid):
    return cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                               wall_clock_min=60, tracker_key=None, architectural=True,
                               external_effect=False, new_dependency=False,
                               public_api_change=False), store=store)


def _linked_start(store, sid, tmp_path, monkeypatch, plan, **record_kw):
    monkeypatch.setenv(HANDOFF_ENV, _handoff_value(tmp_path, _record(plan, **record_kw), f"{sid}.json"))
    cli.cmd_start(ns(session=sid, task="t", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    monkeypatch.delenv(HANDOFF_ENV)


def test_classify_at_the_sensitivity_depth_carries_the_ancestor_task(store, fixtures_dir, tmp_path, monkeypatch):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    threshold = cli.Thresholds().loop_sensitivity_depth
    root_entry = _lineage_entry(plan, 1, "root-s")
    _linked_start(store, "ls1", tmp_path, monkeypatch, plan, parent_session="mid-s",
                  stage_index=2, depth=threshold, parent_lineage=[root_entry])

    d = _classify(store, "ls1")

    check = (d.data or {}).get("loop_self_check")
    assert check is not None, "BEHAVIOUR: a child at loop-sensitivity-depth got no loop_self_check"
    assert check["depth"] == threshold and check["threshold"] == threshold
    assert [a["session"] for a in check["ancestors"]] == ["root-s", "mid-s"]
    first = check["ancestors"][0]
    assert first["stage_index"] == 1 and first["title"] == "Scaffold module"
    assert first["done_criterion"] == "python -c 'import mod' exits 0"
    assert "LOOP_DETECTED" in d.detail


def test_classify_below_the_sensitivity_depth_has_no_self_check(store, fixtures_dir, tmp_path, monkeypatch):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    threshold = cli.Thresholds().loop_sensitivity_depth
    _linked_start(store, "ls2", tmp_path, monkeypatch, plan, depth=threshold - 1)

    d = _classify(store, "ls2")

    assert "loop_self_check" not in (d.data or {})
    assert "LOOP_DETECTED" not in d.detail


def test_classify_of_an_unlinked_session_has_no_self_check(store):
    cli.cmd_start(ns(session="ls3", task="t", goal="g", done_criterion="dc",
                     criterion_type="measurable",
                     recursion_depth=cli.Thresholds().loop_sensitivity_depth + 1), store=store)

    d = _classify(store, "ls3")

    assert "loop_self_check" not in (d.data or {})


# --- (d) grant narrowing at the spawn seam ------------------------------------

def _rules(*texts):
    return [{"rule": t} for t in texts]


# The grants `_grants_plan`'s stage 1 holds: two declared, two derived from its outputs.
ENGINE = ("Bash(git status:*)", "Bash(git diff:*)", "Bash(python3 mod.py:*)", "Edit(//./mod.py)")


def _grant_session(store, sid, fixtures_dir, tmp_path, monkeypatch, handoff_rules):
    """A linked session at EXECUTING stage 1 of the grants plan whose handoff came from
    a *different* parent stage (2), so only the grant check can act on it."""
    plan = _grants_plan(fixtures_dir, tmp_path)
    record = _record(plan, parent_session="par-s", stage_index=2, depth=1,
                     grants=_rules(*handoff_rules))
    _executing(store, sid, plan, record=record, tmp_path=tmp_path, monkeypatch=monkeypatch)
    return plan


def test_engine_grants_of_the_fixture_stage_are_what_the_tests_assume(store, fixtures_dir, tmp_path):
    plan = _grants_plan(fixtures_dir, tmp_path)
    _executing(store, "eg", plan)
    got = tuple(e["rule"] for e in _engine_grants(plan, tmp_path, "eg"))
    assert sorted(got) == sorted(ENGINE)


def test_spawn_admits_grants_identical_to_the_handoff(store, fixtures_dir, tmp_path, monkeypatch, refusals):
    plan = _grant_session(store, "g1", fixtures_dir, tmp_path, monkeypatch, ENGINE)

    rc, env = _spawn(monkeypatch, tmp_path, plan, "g1", stage_index=1)

    assert env is not None, f"BEHAVIOUR: grants identical to the handoff's were refused (rc={rc})"
    assert refusals == []


def test_spawn_admits_grants_narrower_than_the_handoff(store, fixtures_dir, tmp_path, monkeypatch, refusals):
    """A file edit the handoff's directory-wide `Edit` covers needs no identical entry."""
    held = ("Bash(git status:*)", "Bash(git diff:*)", "Bash(python3 mod.py:*)", "Edit(//.)")
    plan = _grant_session(store, "g2", fixtures_dir, tmp_path, monkeypatch, held)

    rc, env = _spawn(monkeypatch, tmp_path, plan, "g2", stage_index=1)

    assert env is not None, f"BEHAVIOUR: a grant narrower than a handoff grant was refused (rc={rc})"
    assert refusals == []


def test_spawn_refuses_grant_widening(store, fixtures_dir, tmp_path, monkeypatch, capsys, refusals):
    """negative control: a delegated stage holding grants the handoff and the user's
    runtime grants do not cover is refused with a PERMISSION-REQUEST before launch."""
    plan = _grant_session(store, "g3", fixtures_dir, tmp_path, monkeypatch, ("Bash(git status:*)",))

    rc, env = _spawn(monkeypatch, tmp_path, plan, "g3", stage_index=1)

    assert env is None, "BEHAVIOUR: a stage wider than the handoff's authority was launched"
    assert rc == getattr(SPAWN, "EXIT_GRANT_WIDENING", -1) == 4, \
        f"BEHAVIOUR: expected the grant-widening refusal (rc 4), got rc={rc}"
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0] == "PERMISSION-REQUEST:"
    assert "Refusal: grant-widening" in lines
    assert "Session: g3" in lines
    sha = [ln for ln in lines if ln.startswith("Handoff-sha256: ")]
    assert len(sha) == 1 and len(sha[0].split(": ")[1]) == 64
    assert sorted(ln.removeprefix("Rule: ") for ln in lines if ln.startswith("Rule: ")) == sorted(
        ["Bash(git diff:*)", "Bash(python3 mod.py:*)", "Edit(//./mod.py)"])
    assert [r for r, _ in refusals] == ["grant-widening"]
    assert not (tmp_path / "state" / "handoffs").exists(), "a refused spawn wrote a handoff"


def test_wildcard_tail_bash_rule_is_covered_only_by_an_identical_entry(
        store, fixtures_dir, tmp_path, monkeypatch, capsys, refusals):
    held = ("Bash(git:*)", "Bash(git diff:*)", "Bash(python3 mod.py:*)", "Edit(//.)")
    plan = _grant_session(store, "g4", fixtures_dir, tmp_path, monkeypatch, held)

    rc, env = _spawn(monkeypatch, tmp_path, plan, "g4", stage_index=1)

    assert env is None and rc == 4, "BEHAVIOUR: a broader wildcard Bash rule was taken to cover a narrower one"
    rules = [ln.removeprefix("Rule: ") for ln in capsys.readouterr().out.splitlines()
             if ln.startswith("Rule: ")]
    assert rules == ["Bash(git status:*)"]


def test_a_user_resolved_runtime_grant_widens_the_covering_set(store, fixtures_dir, tmp_path,
                                                               monkeypatch, refusals):
    """The stage's own engine grants include what the user resolved at runtime; those
    are authority the session was handed, not widening."""
    plan = _grant_session(store, "g5", fixtures_dir, tmp_path, monkeypatch, ENGINE)
    state = store.load("g5")
    state.runtime_grants.setdefault("1", []).append({
        "rule": "Bash(git log:*)", "provenance": "runtime", "consumed": False,
        "scope": "stage", "granted_by": "user", "stage_title": "Scaffold module",
    })
    store.save(state)

    rc, env = _spawn(monkeypatch, tmp_path, plan, "g5", stage_index=1)

    assert env is not None, f"BEHAVIOUR: a user-resolved runtime grant was counted as widening (rc={rc})"
    assert refusals == []


def test_spawn_never_grant_checks_an_unlinked_session(store, fixtures_dir, tmp_path, monkeypatch, refusals):
    plan = _grants_plan(fixtures_dir, tmp_path)
    _executing(store, "g6", plan)

    rc, env = _spawn(monkeypatch, tmp_path, plan, "g6", stage_index=1)

    assert env is not None, f"an unlinked session's grants were checked (rc={rc})"
    assert refusals == []


# --- (e) refusal order ---------------------------------------------------------

def test_refusal_order_is_cap_then_loop_then_grants(store, fixtures_dir, tmp_path, monkeypatch,
                                                    capsys, refusals):
    """One session that is at once a loop and a widening: the loop is refused, not the
    grants; at the recursion cap the cap is refused, not the loop."""
    plan = _grants_plan(fixtures_dir, tmp_path)
    record = _record(plan, parent_session="par-s", stage_index=1, depth=1, grants=[])
    _executing(store, "ord", plan, record=record, tmp_path=tmp_path, monkeypatch=monkeypatch)

    rc, env = _spawn(monkeypatch, tmp_path, plan, "ord", stage_index=1)
    assert env is None and rc == 3 and [r for r, _ in refusals] == ["norm-loop"], \
        "BEHAVIOUR: the grant check ran ahead of the loop rule"
    assert capsys.readouterr().out.startswith("LOOP_DETECTED:")

    refusals.clear()
    cap = SPAWN.recursion_max(SPAWN.parse_config_md())
    monkeypatch.setenv("AGENT_RECURSION_DEPTH", str(cap))
    rc, env = _spawn(monkeypatch, tmp_path, plan, "ord", stage_index=1)
    assert env is None and rc == 3 and [r for r, _ in refusals] == ["recursion-cap"], \
        "BEHAVIOUR: the loop rule ran ahead of the recursion cap"


# --- (d') dispatch routes a grant-widening refusal to the user -----------------

BLOCK = (
    "PERMISSION-REQUEST:\n"
    "Action: delegate stage 1 to a developer child with 2 grant(s) beyond the authority "
    "session gw was handed\n"
    "Why: not covered\n"
    "Fallback if denied: stop the step\n"
    "Refusal: grant-widening\n"
    "Session: gw\n"
    f"Handoff-sha256: {'a1' * 32}\n"
    "Rule: Bash(git diff:*)\n"
    "Rule: Edit(//./mod.py)\n"
)


def _assert_parked_for_the_user(store, sid, d):
    assert d.action == "ask_user_permission" and d.marker == "PERMISSION-REQUEST", \
        f"BEHAVIOUR: a grant-widening refusal was routed as {d.action!r}/{d.marker!r}"
    gw = (d.data or {}).get("grant_widening")
    assert gw is not None and gw["rules"] == ["Bash(git diff:*)", "Edit(//./mod.py)"]
    assert gw["handoff_sha256"] == "a1" * 32
    state = store.load(sid)
    assert state.permission_request is not None and "Refusal: grant-widening" in state.permission_request.raw
    assert state.materialization_defects == [], "a grant-widening refusal was booked as a materialization defect"
    assert d.action != "self_grant" and state.node != Node.DIAGNOSING


def test_dispatch_routes_direct_grant_widening_refusal_to_the_user(store, fixtures_dir):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _executing(store, "gw", plan)

    d = _dispatch(store, "gw", _child_directive_runner(4, stdout=BLOCK))

    _assert_parked_for_the_user(store, "gw", d)


def test_dispatch_routes_relayed_grant_widening_refusal_to_the_user(store, fixtures_dir):
    """A manager child relaying the wrapper's block exits 0 with the block on stdout."""
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _executing(store, "gr", plan)

    d = _dispatch(store, "gr", _child_directive_runner(0, stdout="Summary.\n" + BLOCK))

    _assert_parked_for_the_user(store, "gr", d)


def test_dispatch_exit_4_without_the_refusal_line_is_not_a_grant_widening(store, fixtures_dir):
    """Exit 4 is also "claude not on PATH"; only the stdout line disambiguates."""
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _executing(store, "gn", plan)

    d = _dispatch(store, "gn", _child_directive_runner(4, stderr="claude not on PATH\n"))

    assert "grant_widening" not in (d.data or {})
