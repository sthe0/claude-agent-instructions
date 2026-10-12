"""A difficulty a linked child routes to its parent stage's norm reaches the parent.

A child session spawned for a parent stage holds the norm handoff (`parent_link`). When
its own critique finds that the fault lies in that stage's norm -- a thing the child cannot
edit -- it records `critique --failure-address нормативное --norm-target parent` and ends
with `REPLAN:`. The parent's dispatch then decides from the child's state record, never
from the marker text: the record must exist, belong to this session and stage, carry a
parent norm target whose baseline is the stage's current norm digest, and declare
expected, actual and mismatch. Only then does the parent open DIAGNOSING on the stage with
the child's declaration; every other REPLAN keeps the directive it always had.

Tests drive the base entry points -- `cmd_start` with a handoff the test writes,
`cmd_critique`, `cmd_dispatch` with a fake runner -- against a real `FileStateStore`. The
tests the stage's negative control names assert the behaviour under repair on a
`BEHAVIOUR:`-prefixed assertion, so on base they fail there and not at setup.
"""
from __future__ import annotations

from argparse import Namespace

import pytest

from agentctl import cli, gates, norm_handoff
from agentctl.dispatch import RunResult
from agentctl.plan import load_plan
from agentctl.state import Node, StageStatus
from agentctl.stage_norm import StageNorm

PARENT = "parent-session"
CHILD = "child-session"
NORMATIVE = "нормативное"
REPLAN_TEXT = "REPLAN: the stage's done criterion cannot be met as written\n"
EXPECTED = "stage 1's done criterion is checkable from the stage's own material"
ACTUAL = "the criterion names an artifact no stage produces"
MISMATCH = "the stage norm, not the child's work, is what makes the check unreachable"


def ns(**kw):
    return Namespace(**kw)


@pytest.fixture
def fixtures_dir(venued_fixtures_dir):
    return venued_fixtures_dir


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    monkeypatch.delenv("AGENT_RECURSION_DEPTH", raising=False)
    monkeypatch.delenv("AGENTCTL_PARENT_DIFFICULTY", raising=False)
    monkeypatch.delenv("AGENTCTL_NORM_HANDOFF", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _executing(store, sid, fixtures_dir):
    """A substantive session at EXECUTING stage 1."""
    cli.cmd_start(ns(session=sid, task="norm-mismatch", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=str(fixtures_dir / "plan_two_stage.toml")),
                        store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)


def _parent(store, fixtures_dir):
    _executing(store, PARENT, fixtures_dir)


def _linked_session(store, fixtures_dir, tmp_path, monkeypatch, *, parent_session=PARENT,
                    stage_index=1, norm=None, sid=CHILD):
    """A session at EXECUTING that adopted a handoff to a parent stage. The handoff names
    `norm` (stage 1 of the parent as it stands, by default) exactly as a spawn writes it."""
    norm = norm or StageNorm.from_stage(store.load(PARENT).stage(1))
    record = norm_handoff.build_record(
        parent_session=parent_session, stage_index=stage_index, kind="developer",
        norm=norm, grants=[], depth=1, parent_lineage=None)
    path, sha = norm_handoff.write_record(tmp_path / "handoffs", record)
    monkeypatch.setenv(norm_handoff.NORM_HANDOFF_ENV, norm_handoff.format_env_value(path, sha))
    _executing(store, sid, fixtures_dir)
    monkeypatch.delenv(norm_handoff.NORM_HANDOFF_ENV)


def _child(store, fixtures_dir, tmp_path, monkeypatch, *, sid=CHILD, **link):
    """A child session linked to a parent stage, failed into DIAGNOSING with its
    declaration and investigation recorded."""
    _linked_session(store, fixtures_dir, tmp_path, monkeypatch, sid=sid, **link)
    cli.cmd_record_result(ns(session=sid, status="failed", actual="boom"), store=store)
    cli.cmd_declare(ns(session=sid, expected=EXPECTED, actual=ACTUAL, mismatch=MISMATCH),
                    store=store)
    cli.cmd_investigate(ns(session=sid, localized_expectation="le", localized_actual="la",
                           hypotheses=["the criterion is unreachable", "the child erred"]),
                        store=store)


def _critique(store, sid=CHILD, failure_address=NORMATIVE, targets=("parent",)):
    kw = dict(session=sid, functional_ground="fg", replanning_task="rt",
              failure_address=failure_address)
    if targets:
        kw["norm_targets"] = list(targets)
    return cli.cmd_critique(ns(**kw), store=store)


def _routed_child(store, fixtures_dir, tmp_path, monkeypatch, **link):
    _parent(store, fixtures_dir)
    _child(store, fixtures_dir, tmp_path, monkeypatch, **link)
    return _critique(store)


def _dispatch(store, stdout=REPLAN_TEXT, *, child=CHILD, transcript=True):
    stderr = f"spawn-specialist: transcript=/nowhere/projects/{child}.jsonl\n" if transcript else ""
    runner = lambda argv, cwd=None: RunResult(0, stdout=stdout, stderr=stderr)
    return cli.cmd_dispatch(ns(session=PARENT, budget="medium", complexity="medium",
                               dry_run=False), store=store, runner=runner)


def _assert_directive_kept(d, store, reason):
    """The directive is the plain REPLAN one and the history says why the child's record
    did not verify: a row whose reason contains `reason`, or -- `reason` None -- no
    `norm_mismatch_unverified` row at all (a spawn with no linked record to judge)."""
    assert d.ok is False
    assert d.action == "replan"
    assert d.marker == "REPLAN"
    assert d.data["reason"] == REPLAN_TEXT.removeprefix("REPLAN:").strip()
    assert "norm_mismatch" not in d.data
    state = store.load(PARENT)
    assert state.node == Node.EXECUTING.value
    assert state.difficulty is None
    rows = [e for e in state.history if e.get("event") == "norm_mismatch_unverified"]
    if reason is None:
        assert rows == []
    else:
        assert len(rows) == 1 and reason in rows[0]["reason"], rows


def test_parent_norm_target_is_recorded(store, fixtures_dir, tmp_path, monkeypatch):
    d = _routed_child(store, fixtures_dir, tmp_path, monkeypatch)
    targets = store.load(CHILD).difficulty.norm_targets
    baseline = StageNorm.from_stage(store.load(PARENT).stage(1)).norm_digest()
    assert targets == [{"target": "parent", "kind": "parent_norm", "baseline": baseline,
                        "source": "critique"}], (
        f"BEHAVIOUR: the linked child recorded no parent norm target ({d.detail!r})")
    assert d.ok is True
    assert d.marker == "REPLAN"
    assert d.action == "return_replan"
    assert d.data["parent_session"] == PARENT
    assert d.data["stage_index"] == 1


def test_verified_child_replan_opens_diagnosing(store, fixtures_dir, tmp_path, monkeypatch):
    _routed_child(store, fixtures_dir, tmp_path, monkeypatch)
    d = _dispatch(store)
    mismatch = d.data.get("norm_mismatch")
    assert mismatch is not None, (
        f"BEHAVIOUR: a REPLAN backed by the child's parent norm target kept the plain "
        f"REPLAN directive (action={d.action!r})")
    assert d.node == Node.DIAGNOSING.value
    assert d.marker == "OVERCOME-DIFFICULTY"
    assert mismatch["child_session"] == CHILD
    assert mismatch["stage_index"] == 1
    assert mismatch["norm_digest"] == StageNorm.from_stage(store.load(PARENT).stage(1)).norm_digest()
    state = store.load(PARENT)
    assert state.node == Node.DIAGNOSING.value
    assert state.stage(1).outcome.status == StageStatus.FAILED.value
    assert any(e.get("event") == "norm_mismatch" for e in state.history)


def test_child_declaration_reaches_parent_difficulty(store, fixtures_dir, tmp_path, monkeypatch):
    _routed_child(store, fixtures_dir, tmp_path, monkeypatch)
    d = _dispatch(store)
    declared = getattr(store.load(PARENT).difficulty, "declaration", None)
    assert declared is not None, (
        f"BEHAVIOUR: the parent holds no difficulty after the child's verified REPLAN "
        f"(action={d.action!r})")
    assert (declared.expected, declared.actual, declared.mismatch) == (EXPECTED, ACTUAL, MISMATCH)
    assert d.data["norm_mismatch"]["declaration"] == {
        "expected": EXPECTED, "actual": ACTUAL, "mismatch": MISMATCH}


def test_critique_parent_target_refused_unlinked(store, fixtures_dir):
    _executing(store, PARENT, fixtures_dir)
    cli.cmd_record_result(ns(session=PARENT, status="failed", actual="boom"), store=store)
    cli.cmd_declare(ns(session=PARENT, expected="e", actual="a", mismatch="m"), store=store)
    cli.cmd_investigate(ns(session=PARENT, localized_expectation="le", localized_actual="la",
                           hypotheses=["h1", "h2"]), store=store)
    d = _critique(store, PARENT)
    assert d.ok is False
    assert "not linked to a parent stage" in d.detail, (
        f"BEHAVIOUR: the unlinked session's --norm-target parent was refused for another "
        f"reason ({d.detail!r})")
    assert store.load(PARENT).difficulty.norm_targets == []


def test_critique_parent_target_refused_wrong_address(store, fixtures_dir, tmp_path, monkeypatch):
    _parent(store, fixtures_dir)
    _child(store, fixtures_dir, tmp_path, monkeypatch)
    d = _critique(store, failure_address="ресурсное")
    assert d.ok is False
    assert "нормативное" in d.detail
    assert store.load(CHILD).difficulty.norm_targets == []


def test_parent_target_stands_alone(store, fixtures_dir, tmp_path, monkeypatch):
    _parent(store, fixtures_dir)
    _child(store, fixtures_dir, tmp_path, monkeypatch)
    d = _critique(store, targets=("parent", "stage:1"))
    assert d.ok is False
    assert "stands alone" in d.detail
    assert store.load(CHILD).difficulty.norm_targets == []


def test_parent_target_stands_alone_beside_a_file(store, fixtures_dir, tmp_path, monkeypatch):
    _parent(store, fixtures_dir)
    _child(store, fixtures_dir, tmp_path, monkeypatch)
    norm_file = tmp_path / "norm.md"
    norm_file.write_text("a norm\n", encoding="utf-8")
    d = _critique(store, targets=("parent", str(norm_file)))
    assert d.ok is False
    assert "stands alone" in d.detail
    assert store.load(CHILD).difficulty.norm_targets == []


def test_replan_other_parent_keeps_directive(store, fixtures_dir, tmp_path, monkeypatch):
    _routed_child(store, fixtures_dir, tmp_path, monkeypatch, parent_session="someone-else")
    _assert_directive_kept(_dispatch(store), store, "parent_session='someone-else'")


def test_replan_other_stage_keeps_directive(store, fixtures_dir, tmp_path, monkeypatch):
    _routed_child(store, fixtures_dir, tmp_path, monkeypatch, stage_index=2)
    _assert_directive_kept(_dispatch(store), store, "stage_index=2")


def test_replan_stale_baseline_keeps_directive(store, fixtures_dir, tmp_path, monkeypatch):
    _routed_child(store, fixtures_dir, tmp_path, monkeypatch)
    state = store.load(PARENT)
    state.stage(1).criterion.done_criterion = "a done criterion the parent's replan rewrote"
    store.save(state)
    _assert_directive_kept(_dispatch(store), store, "is stale")


def test_replan_incomplete_declaration_keeps_directive(store, fixtures_dir, tmp_path, monkeypatch):
    _routed_child(store, fixtures_dir, tmp_path, monkeypatch)
    child = store.load(CHILD)
    child.difficulty.declaration.mismatch = "  "
    store.save(child)
    _assert_directive_kept(_dispatch(store), store, "lacks expected, actual or mismatch")


def test_replan_no_transcript_keeps_directive(store, fixtures_dir, tmp_path, monkeypatch):
    _routed_child(store, fixtures_dir, tmp_path, monkeypatch)
    _assert_directive_kept(_dispatch(store, transcript=False), store, None)


def test_replan_unreadable_state_keeps_directive(store, fixtures_dir, tmp_path, monkeypatch):
    _routed_child(store, fixtures_dir, tmp_path, monkeypatch)
    store.path(CHILD).write_text("{not json", encoding="utf-8")
    _assert_directive_kept(_dispatch(store), store, "is unreadable")


def test_replan_malformed_state_keeps_directive(store, fixtures_dir, tmp_path, monkeypatch):
    _routed_child(store, fixtures_dir, tmp_path, monkeypatch)
    store.path(CHILD).write_text("[]", encoding="utf-8")
    _assert_directive_kept(_dispatch(store), store, "is unreadable")


def test_replan_text_without_record_keeps_directive(store, fixtures_dir, tmp_path, monkeypatch):
    _parent(store, fixtures_dir)
    _child(store, fixtures_dir, tmp_path, monkeypatch)
    _critique(store, failure_address="ресурсное", targets=())
    d = _dispatch(store, "REPLAN: the parent stage's norm is wrong -- norm mismatch, "
                         "difficulty addressed to the parent\n")
    assert d.action == "replan"
    assert d.marker == "REPLAN"
    assert "norm_mismatch" not in d.data
    state = store.load(PARENT)
    assert state.node == Node.EXECUTING.value
    assert state.difficulty is None


def test_replan_child_without_a_parent_target_keeps_directive(store, fixtures_dir, tmp_path,
                                                              monkeypatch):
    _parent(store, fixtures_dir)
    _child(store, fixtures_dir, tmp_path, monkeypatch)
    _critique(store, failure_address="ресурсное", targets=())
    _assert_directive_kept(_dispatch(store), store, "names no parent norm target")


def test_replan_transcript_naming_no_state_record_keeps_directive(store, fixtures_dir, tmp_path,
                                                                  monkeypatch):
    _routed_child(store, fixtures_dir, tmp_path, monkeypatch)
    _assert_directive_kept(_dispatch(store, child="ghost-session"), store, None)


def test_replan_transcript_naming_the_parent_keeps_directive(store, fixtures_dir, tmp_path,
                                                             monkeypatch):
    _routed_child(store, fixtures_dir, tmp_path, monkeypatch)
    _assert_directive_kept(_dispatch(store, child=PARENT), store, None)


def test_replan_linked_child_without_difficulty_keeps_directive(store, fixtures_dir, tmp_path,
                                                                monkeypatch):
    _parent(store, fixtures_dir)
    _linked_session(store, fixtures_dir, tmp_path, monkeypatch)
    _assert_directive_kept(_dispatch(store), store, "holds no open difficulty")


def test_replan_unlinked_spawn_writes_no_history_row(store, fixtures_dir):
    _parent(store, fixtures_dir)
    _executing(store, CHILD, fixtures_dir)
    _assert_directive_kept(_dispatch(store), store, None)


def test_a_linked_childs_replan_is_refused_with_the_parent_reason(store, fixtures_dir, tmp_path,
                                                                  monkeypatch):
    _routed_child(store, fixtures_dir, tmp_path, monkeypatch)
    plan_path = str(fixtures_dir / "plan_two_stage.toml")
    blockers = gates.normative_edit_blockers(
        store.load(CHILD), load_plan(plan_path), {1, 2}, installing_plan_path=plan_path)
    assert len(blockers) == 1
    assert "handed back to the parent stage" in blockers[0]
    assert "REPLAN:" in blockers[0]
    assert "renorms" not in blockers[0]

    cli.cmd_normalize(ns(session=CHILD, factor="reproducible cause", level="note"), store=store)
    d = cli.cmd_replan(
        ns(session=CHILD, plan=plan_path, renormalize=False, coverage_waiver=None,
           normalization_waiver=None, cost_log=None),
        store=store, runner=lambda argv, timeout=None: RunResult(1, stdout="", stderr=""))
    assert d.ok is False
    assert d.data["blockers"] == blockers, d.data


def test_parent_never_writes_the_child_state(store, fixtures_dir, tmp_path, monkeypatch):
    _routed_child(store, fixtures_dir, tmp_path, monkeypatch)
    before = store.path(CHILD).read_bytes()
    _dispatch(store)
    assert store.path(CHILD).read_bytes() == before
