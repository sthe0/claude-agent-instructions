"""A child's escalation of its parent stage's norm is a typed link the parent accounts for.

A linked child whose critique finds the fault in the parent stage's norm records
`critique --norm-target parent`. The child difficulty then carries `escalated_to`; the
parent difficulty its dispatch (or `adopt-escalation`) opens carries `escalated_from`.
The parent opens at most one difficulty per child difficulty, the closure of that
difficulty is derived on read on the child side (the parent never writes the child's
state), and an escalation the parent never adopted blocks the parent's `verify-final`.

Tests drive the base entry points -- `cmd_critique`, `cmd_dispatch` with a fake runner,
`cmd_children`, `cmd_status`, `cmd_verify_final` -- against a real `FileStateStore`. The
tests the stage's negative control names assert the behaviour under repair on a
`BEHAVIOUR:`-prefixed assertion, so on base they fail there and not at setup; the two it
passes assert only through surfaces base already has.
"""
from __future__ import annotations

import json
from argparse import Namespace

import pytest

from agentctl import cli, norm_handoff
from conftest import spawn_handoff
from agentctl.dispatch import RunResult
from agentctl.state import Node, SessionState, StageStatus
from agentctl.stage_norm import StageNorm

PARENT = "parent-session"
CHILD = "child-session"
NORMATIVE = "нормативное"
REPLAN_TEXT = "REPLAN: the stage's done criterion cannot be met as written\n"
EXPECTED = "stage 1's done criterion is checkable from the stage's own material"
ACTUAL = "the criterion names an artifact no stage produces"
MISMATCH = "the stage norm, not the child's work, is what makes the check unreachable"


@pytest.fixture
def fixtures_dir(venued_fixtures_dir):
    return venued_fixtures_dir


def ns(**kw):
    return Namespace(**kw)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    monkeypatch.delenv("AGENT_RECURSION_DEPTH", raising=False)
    monkeypatch.delenv("AGENTCTL_PARENT_DIFFICULTY", raising=False)
    monkeypatch.delenv("AGENTCTL_NORM_HANDOFF", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _executing(store, sid, fixtures_dir):
    cli.cmd_start(ns(session=sid, task="escalation", goal="g", done_criterion="dc",
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


def _child(store, fixtures_dir, tmp_path, monkeypatch, *, sid=CHILD):
    """A child linked to stage 1 of the parent, failed into DIAGNOSING with declaration
    and investigation recorded."""
    norm = StageNorm.from_stage(store.load(PARENT).stage(1))
    record = norm_handoff.build_record(
        parent_session=PARENT, stage_index=1, kind="developer", norm=norm, grants=[],
        depth=1, parent_lineage=None)
    monkeypatch.setenv(norm_handoff.NORM_HANDOFF_ENV, spawn_handoff(store.root, record, sid))
    _executing(store, sid, fixtures_dir)
    monkeypatch.delenv(norm_handoff.NORM_HANDOFF_ENV)
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


def _routed_child(store, fixtures_dir, tmp_path, monkeypatch):
    _executing(store, PARENT, fixtures_dir)
    _child(store, fixtures_dir, tmp_path, monkeypatch)
    return _critique(store)


def _dispatch(store, stdout=REPLAN_TEXT, *, child=CHILD):
    stderr = f"spawn-specialist: transcript=/nowhere/projects/{child}.jsonl\n"
    runner = lambda argv, cwd=None: RunResult(0, stdout=stdout, stderr=stderr)
    return cli.cmd_dispatch(ns(session=PARENT, budget="medium", complexity="medium",
                               dry_run=False), store=store, runner=runner)


def _verifying_parent(store):
    """The parent at VERIFYING: where `verify-final` runs and where an escalation the
    parent never saw is still outstanding."""
    state = store.load(PARENT)
    state.node = Node.VERIFYING.value
    store.save(state)


def _verify_final(store):
    return cli.cmd_verify_final(ns(session=PARENT), store=store)


def _adopt(store, child=CHILD):
    cmd = getattr(cli, "cmd_adopt_escalation", None)
    assert cmd is not None, "BEHAVIOUR: the engine has no adopt-escalation command"
    return cmd(ns(session=PARENT, child=child), store=store)


def _close_parent_difficulty(store):
    state = store.load(PARENT)
    cli._archive_difficulty(state)
    state.node = Node.EXECUTING.value
    state.stage(1).outcome.status = StageStatus.ACTIVE.value
    store.save(state)


def _children_escalations(store):
    d = cli.cmd_children(ns(session=PARENT, json=True), store=store)
    return d.data["children"][0].get("escalations")


def test_child_escalation_carries_typed_parent_link(store, fixtures_dir, tmp_path, monkeypatch):
    _routed_child(store, fixtures_dir, tmp_path, monkeypatch)
    baseline = StageNorm.from_stage(store.load(PARENT).stage(1)).norm_digest()
    assert getattr(store.load(CHILD).difficulty, "escalated_to", None) == {
        "parent_session": PARENT, "stage_index": 1, "norm_digest": baseline}, (
        "BEHAVIOUR: the child's critique --norm-target parent set no typed escalated_to link")


def test_parent_difficulty_carries_typed_child_link(store, fixtures_dir, tmp_path, monkeypatch):
    _routed_child(store, fixtures_dir, tmp_path, monkeypatch)
    _dispatch(store)
    child_uid = store.load(CHILD).difficulty.uid
    baseline = StageNorm.from_stage(store.load(PARENT).stage(1)).norm_digest()
    assert getattr(store.load(PARENT).difficulty, "escalated_from", None) == {
        "child_session": CHILD, "child_uid": child_uid, "norm_digest": baseline}, (
        "BEHAVIOUR: the parent difficulty the dispatch opened carries no escalated_from link")


def test_parent_closure_shows_child_escalation_closed(store, fixtures_dir, tmp_path, monkeypatch):
    _routed_child(store, fixtures_dir, tmp_path, monkeypatch)
    child_uid = store.load(CHILD).difficulty.uid
    assert _children_escalations(store) == [
        {"child_uid": child_uid, "stage_index": 1, "status": "unadopted"}], (
        "BEHAVIOUR: `children` shows no unadopted escalation before the parent adopts it")
    _dispatch(store)
    assert (_children_escalations(store) or [{}])[0].get("status") == "open", (
        "BEHAVIOUR: `children` does not show the escalation open while the parent difficulty is")
    _close_parent_difficulty(store)
    before = store.path(CHILD).read_bytes()
    assert (_children_escalations(store) or [{}])[0].get("status") == "closed", (
        "BEHAVIOUR: `children` still shows the escalation open after the parent closed it")
    status = cli.cmd_status(ns(session=CHILD), store=store)
    assert [e["status"] for e in status.data.get("escalations", [])] == ["closed"], (
        "BEHAVIOUR: the child's `status` does not show the closure the parent recorded")
    assert store.path(CHILD).read_bytes() == before, "reading the closure wrote the child's state"


def test_same_child_escalation_opens_once(store, fixtures_dir, tmp_path, monkeypatch):
    _routed_child(store, fixtures_dir, tmp_path, monkeypatch)
    _dispatch(store)
    first = store.load(PARENT).difficulty
    state = store.load(PARENT)
    state.node = Node.EXECUTING.value
    state.stage(1).outcome.status = StageStatus.ACTIVE.value
    store.save(state)

    d = _dispatch(store)
    after = store.load(PARENT)
    assert after.difficulty is not None and after.difficulty.uid == first.uid, (
        "BEHAVIOUR: a repeated REPLAN for the same child difficulty replaced the parent's difficulty")
    assert after.closed_difficulties == [] and after.difficulty_stack == [], (
        "BEHAVIOUR: a repeated REPLAN for the same child difficulty opened a second record")
    assert d.data.get("escalation", {}).get("status") == "open", (
        f"BEHAVIOUR: the repeat did not answer with the standing difficulty ({d.data!r})")

    _close_parent_difficulty(store)
    d = _dispatch(store)
    after = store.load(PARENT)
    assert after.difficulty is None, (
        "BEHAVIOUR: a REPLAN repeating an already closed escalation opened a new difficulty")
    assert d.action == "replan" and d.data["escalation"]["status"] == "closed"


def test_unread_escalation_blocks_verify_final(store, fixtures_dir, tmp_path, monkeypatch):
    _routed_child(store, fixtures_dir, tmp_path, monkeypatch)
    _verifying_parent(store)
    d = _verify_final(store)
    assert d.action == "adopt_escalation", (
        f"BEHAVIOUR: verify-final was not blocked by the unadopted escalation ({d.action!r})")
    assert d.ok is False
    assert any(f"adopt-escalation --child {CHILD}" in b for b in d.data["blockers"])
    assert store.load(PARENT).node == Node.VERIFYING.value


def test_adopt_escalation_opens_counterpart(store, fixtures_dir, tmp_path, monkeypatch):
    _routed_child(store, fixtures_dir, tmp_path, monkeypatch)
    _verifying_parent(store)
    before = store.path(CHILD).read_bytes()
    d = _adopt(store)
    state = store.load(PARENT)
    assert state.difficulty is not None, (
        f"BEHAVIOUR: adopt-escalation opened no counterpart ({d.detail!r})")
    child_uid = store.load(CHILD).difficulty.uid
    assert state.difficulty.escalated_from["child_uid"] == child_uid
    assert state.difficulty.declaration.mismatch == MISMATCH
    assert state.node == Node.DIAGNOSING.value
    assert d.marker == "OVERCOME-DIFFICULTY"
    assert store.path(CHILD).read_bytes() == before, "adopt-escalation wrote the child's state"

    _verifying_parent(store)
    again = _adopt(store)
    assert again.action == "noop" and "already adopted" in again.detail, again.detail
    assert len(store.load(PARENT).closed_difficulties) == 0


def test_suspended_child_escalation_is_listed_and_adoptable(store, fixtures_dir, tmp_path,
                                                            monkeypatch):
    _routed_child(store, fixtures_dir, tmp_path, monkeypatch)
    cli.cmd_difficulty_open(ns(session=CHILD, origin="replan"), store=store)
    child = store.load(CHILD)
    assert child.difficulty_stack and child.difficulty.escalated_to == {}
    _verifying_parent(store)
    listed = CHILD in " ".join(_verify_final(store).data.get("blockers", []))
    adopted = _adopt(store)
    adoptable = store.load(PARENT).difficulty is not None
    assert listed == adoptable, (
        f"BEHAVIOUR: the gate listing the suspended escalation is {listed} but "
        f"adopt-escalation adopting it is {adoptable} ({adopted.detail!r})")
    assert adoptable, "BEHAVIOUR: a suspended record carrying a verified escalation is not adopted"


def _nested_escalation(store, *, close=True):
    """Over the child's open difficulty A, a nested B routed to the parent's norm; closed
    into A when `close`, otherwise left open over A."""
    cli.cmd_difficulty_open(ns(session=CHILD, origin="replan"), store=store)
    cli.cmd_declare(ns(session=CHILD, expected=EXPECTED, actual=ACTUAL, mismatch=MISMATCH),
                    store=store)
    cli.cmd_investigate(ns(session=CHILD, localized_expectation="le", localized_actual="la",
                           hypotheses=["the criterion is unreachable", "the child erred"]),
                        store=store)
    _critique(store)
    if close:
        cli.cmd_normalize(ns(session=CHILD, factor="reproducible cause", level="note"),
                          store=store)
        d = cli.cmd_difficulty_close(ns(session=CHILD), store=store)
        assert d.ok, d.detail


def test_closed_nested_escalation_dispatches_via_receiving_record(store, fixtures_dir, tmp_path,
                                                                  monkeypatch):
    """B, routed to the parent's norm and closed into A, leaves A carrying the typed link; the
    parent adopts A, and once it closes its difficulty both sides show one closed escalation."""
    _executing(store, PARENT, fixtures_dir)
    _child(store, fixtures_dir, tmp_path, monkeypatch)
    _critique(store, failure_address="ресурсное", targets=())
    assert store.load(CHILD).difficulty.escalated_to == {}
    _nested_escalation(store)
    child = store.load(CHILD)
    baseline = next(t["baseline"] for t in child.difficulty.norm_targets
                    if t.get("kind") == "parent_norm")
    assert child.difficulty_stack == [] and child.difficulty.escalated_to == {
        "parent_session": PARENT, "stage_index": 1, "norm_digest": baseline}, (
        "BEHAVIOUR: difficulty-close moved the parent norm target onto the receiving record "
        "without its escalated_to link")

    d = _dispatch(store)
    state = store.load(PARENT)
    assert state.difficulty is not None and d.marker == "OVERCOME-DIFFICULTY", (
        f"BEHAVIOUR: the REPLAN of a child whose escalation moved onto the receiving record "
        f"opened no parent difficulty ({d.detail!r})")
    assert state.difficulty.escalated_from["child_uid"] == child.difficulty.uid

    state.node = Node.VERIFYING.value
    store.save(state)
    assert _verify_final(store).action != "adopt_escalation"

    _close_parent_difficulty(store)
    shown = cli.cmd_status(ns(session=CHILD), store=store).data["escalations"]
    for rows in (shown, _children_escalations(store)):
        assert [(r["child_uid"], r["status"]) for r in rows] == [(child.difficulty.uid, "closed")], (
            f"BEHAVIOUR: the closed nested record is shown as a second escalation ({rows!r})")


def test_sequential_nested_escalations_show_one_row(store, fixtures_dir, tmp_path, monkeypatch):
    """B1 and then B2, both routed to the parent's norm, are closed into the same A: A
    alone carries the escalation, so both sides show exactly one row -- before the parent
    adopts it, while it works it, and once it has closed it."""
    _executing(store, PARENT, fixtures_dir)
    _child(store, fixtures_dir, tmp_path, monkeypatch)
    _critique(store, failure_address="ресурсное", targets=())
    _nested_escalation(store)
    _nested_escalation(store)
    child = store.load(CHILD)
    uid_a = child.difficulty.uid
    assert [c.escalated_to for c in child.difficulty.children].count({}) == 0, (
        "both nested records should have been closed into A as escalations")

    def rows():
        status = cli.cmd_status(ns(session=CHILD), store=store).data["escalations"]
        return [[(r["child_uid"], r["status"]) for r in shown]
                for shown in (status, _children_escalations(store))]

    assert rows() == [[(uid_a, "unadopted")]] * 2, (
        f"BEHAVIOUR: a closed nested record is shown beside the receiving one ({rows()!r})")
    _dispatch(store)
    assert rows() == [[(uid_a, "open")]] * 2
    _close_parent_difficulty(store)
    assert rows() == [[(uid_a, "closed")]] * 2, (
        f"BEHAVIOUR: a closed nested record stays unadopted after the parent closed ({rows()!r})")
    _verifying_parent(store)
    assert _verify_final(store).action != "adopt_escalation"


def test_adopt_escalation_prefers_unadopted_record(store, fixtures_dir, tmp_path, monkeypatch):
    """A and B both escalated; dispatch adopted B. After the parent closes B without
    changing the stage's norm, the gate lists A and `adopt-escalation` adopts A rather than
    reporting B as already adopted."""
    _routed_child(store, fixtures_dir, tmp_path, monkeypatch)
    uid_a = store.load(CHILD).difficulty.uid
    _nested_escalation(store, close=False)
    child = store.load(CHILD)
    uid_b = child.difficulty.uid
    assert child.difficulty.escalated_to and child.difficulty_stack[0].escalated_to

    _dispatch(store)
    assert store.load(PARENT).difficulty.escalated_from["child_uid"] == uid_b
    _close_parent_difficulty(store)
    _verifying_parent(store)

    blockers = _verify_final(store).data.get("blockers", [])
    assert [b for b in blockers if uid_a in b] and not [b for b in blockers if uid_b in b], (
        f"BEHAVIOUR: the gate does not list exactly the unadopted record A ({blockers!r})")
    d = _adopt(store)
    state = store.load(PARENT)
    assert state.difficulty is not None and state.difficulty.escalated_from["child_uid"] == uid_a, (
        f"BEHAVIOUR: adopt-escalation did not pick the unadopted record ({d.detail!r})")


def test_adopt_escalation_from_executing_refuses_non_current_stage(store, fixtures_dir, tmp_path,
                                                                   monkeypatch):
    _routed_child(store, fixtures_dir, tmp_path, monkeypatch)
    state = store.load(PARENT)
    state.stage(1).outcome.status = StageStatus.PASSED.value
    state.stage(2).outcome.status = StageStatus.ACTIVE.value
    state.current_stage = 2
    state.node = Node.EXECUTING.value
    store.save(state)
    d = _adopt(store)
    after = store.load(PARENT)
    assert d.ok is False and "active stage is 2" in d.detail, (
        f"BEHAVIOUR: adopt-escalation did not refuse an earlier stage from EXECUTING ({d.detail!r})")
    assert after.difficulty is None and after.stage(2).outcome.status == StageStatus.ACTIVE.value
    assert after.node == Node.EXECUTING.value


def test_unlinked_replan_keeps_base_directive(store, fixtures_dir):
    _executing(store, PARENT, fixtures_dir)
    _executing(store, CHILD, fixtures_dir)
    d = _dispatch(store)
    assert (d.ok, d.action, d.marker) == (False, "replan", "REPLAN")
    assert "norm_mismatch" not in d.data and "escalation" not in d.data
    state = store.load(PARENT)
    assert state.node == Node.EXECUTING.value and state.difficulty is None
    _verifying_parent(store)
    assert _verify_final(store).action != "adopt_escalation"


def test_adopt_escalation_is_user_authority_verb():
    from lib.widening_targets import AGENTCTL_USER_AUTHORITY_VERBS
    assert "adopt-escalation" in AGENTCTL_USER_AUTHORITY_VERBS, (
        "BEHAVIOUR: adopt-escalation is not a user-authority verb a spawned process is barred from")
    assert "adopt-escalation" in cli._SESSION_COMMANDS
    assert "adopt-escalation" in cli.COMMANDS


def test_adopt_escalation_refuses_unverified_child_record(store, fixtures_dir, tmp_path,
                                                           monkeypatch):
    _routed_child(store, fixtures_dir, tmp_path, monkeypatch)
    state = store.load(PARENT)
    state.stage(1).criterion.done_criterion = "a done criterion the parent's replan rewrote"
    state.node = Node.VERIFYING.value
    store.save(state)
    d = _adopt(store)
    assert d.ok is False
    assert "adopt-escalation refused" in d.detail and "is stale" in d.detail, (
        f"BEHAVIOUR: adopt-escalation did not refuse a stale child record with its reason "
        f"({d.detail!r})")
    assert store.load(PARENT).difficulty is None

    ghost = _adopt(store, "ghost-session")
    assert ghost.ok is False and "not a readable session" in ghost.detail


def test_base_shaped_difficulty_state_loads(store, fixtures_dir, tmp_path, monkeypatch):
    _routed_child(store, fixtures_dir, tmp_path, monkeypatch)
    _dispatch(store)
    for sid in (PARENT, CHILD):
        raw = json.loads(store.path(sid).read_text(encoding="utf-8"))
        records = [raw.get("difficulty"), *raw.get("difficulty_stack", []),
                   *raw.get("closed_difficulties", [])]
        for rec in records:
            if rec is not None:
                rec.pop("escalated_to", None)
                rec.pop("escalated_from", None)
        store.path(sid).write_text(json.dumps(raw), encoding="utf-8")
    parent = store.load(PARENT)
    assert isinstance(parent, SessionState)
    assert not getattr(parent.difficulty, "escalated_from", {})
    assert not getattr(store.load(CHILD).difficulty, "escalated_to", {})
    state = store.load(PARENT)
    state.node = Node.VERIFYING.value
    store.save(state)
    assert _verify_final(store).action != "adopt_escalation"


B_EXPECTED = "the nested check can be reached from the stage's material"
B_ACTUAL = "the nested check names an output no stage declares"
B_MISMATCH = "the stage's declared outputs, not the nested attempt, make B's check unreachable"


def _nested_escalation_with_own_declaration(store):
    """`_nested_escalation` with B's declaration distinct from A's, closed into A."""
    cli.cmd_difficulty_open(ns(session=CHILD, origin="replan"), store=store)
    cli.cmd_declare(ns(session=CHILD, expected=B_EXPECTED, actual=B_ACTUAL,
                       mismatch=B_MISMATCH), store=store)
    cli.cmd_investigate(ns(session=CHILD, localized_expectation="le", localized_actual="la",
                           hypotheses=["the criterion is unreachable", "the child erred"]),
                        store=store)
    _critique(store)
    cli.cmd_normalize(ns(session=CHILD, factor="reproducible cause", level="note"), store=store)
    d = cli.cmd_difficulty_close(ns(session=CHILD), store=store)
    assert d.ok, d.detail


def test_nested_escalation_opens_parent_difficulty_with_the_escalating_declaration(
        store, fixtures_dir, tmp_path, monkeypatch):
    """B states the mismatch and is closed into A; the parent's difficulty carries B's
    declaration, not A's, while still pointing at the record that carries the link."""
    _executing(store, PARENT, fixtures_dir)
    _child(store, fixtures_dir, tmp_path, monkeypatch)
    _critique(store, failure_address="ресурсное", targets=())
    _nested_escalation_with_own_declaration(store)
    child = store.load(CHILD)

    d = _dispatch(store)
    opened = store.load(PARENT).difficulty
    assert opened is not None and d.marker == "OVERCOME-DIFFICULTY"
    declared = (opened.declaration.expected, opened.declaration.actual,
                opened.declaration.mismatch)
    assert declared == (B_EXPECTED, B_ACTUAL, B_MISMATCH), (
        f"BEHAVIOUR: the parent's difficulty carries {declared!r}, not the declaration of "
        "the nested record that stated the mismatch")
    assert opened.escalated_from["child_uid"] == child.difficulty.uid
    assert d.data["norm_mismatch"]["declaration"]["mismatch"] == B_MISMATCH


def test_nested_escalation_adopt_carries_the_escalating_declaration(
        store, fixtures_dir, tmp_path, monkeypatch):
    """`adopt-escalation` of the record B was closed into opens the parent difficulty with
    B's declaration."""
    _executing(store, PARENT, fixtures_dir)
    _child(store, fixtures_dir, tmp_path, monkeypatch)
    _critique(store, failure_address="ресурсное", targets=())
    _nested_escalation_with_own_declaration(store)
    _verifying_parent(store)

    _adopt(store)
    opened = store.load(PARENT).difficulty
    assert opened is not None and opened.declaration.mismatch == B_MISMATCH, (
        "BEHAVIOUR: adopt-escalation opened a parent difficulty with the receiving record's "
        "declaration instead of the nested record's")


def _refresh_child_after_parent_fix(store, tmp_path):
    """The parent fixes stage 1's norm and the re-dispatch renews the child's handoff."""
    from test_child_contour_gate import _load_spawn

    parent = store.load(PARENT)
    parent.stage(1).title += " (the artifact its criterion names is produced here)"
    store.save(parent)
    record = norm_handoff.build_record(
        parent_session=PARENT, stage_index=1, kind="developer",
        norm=StageNorm.from_stage(parent.stage(1)), grants=[], depth=1, parent_lineage=None)
    path, sha = norm_handoff.write_record(tmp_path / "handoffs", record)
    _load_spawn().refresh_resumed_child_link(
        store, store.load(CHILD), norm_handoff.format_env_value(path, sha))


def _child_closes(store, fixtures_dir):
    cli.cmd_normalize(ns(session=CHILD, factor="a stage whose done criterion names no producer",
                         level="note"), store=store)
    return cli.cmd_replan(
        ns(session=CHILD, plan=str(fixtures_dir / "plan_two_stage_refined.toml")), store=store)


def test_resumed_child_closes_parent_targeted_difficulty_after_parent_fix(
        store, fixtures_dir, tmp_path, monkeypatch):
    _routed_child(store, fixtures_dir, tmp_path, monkeypatch)
    _refresh_child_after_parent_fix(store, tmp_path)

    directive = _child_closes(store, fixtures_dir)

    state = store.load(CHILD)
    assert directive.ok and state.difficulty is None, (
        f"BEHAVIOUR: the resumed child could not close its parent-targeted difficulty after "
        f"the parent fixed the norm: {directive.detail}"
    )
    assert state.node == Node.PLAN_READY.value


def test_child_keeps_parent_targeted_difficulty_open_until_handoff_refreshed(
        store, fixtures_dir, tmp_path, monkeypatch):
    _routed_child(store, fixtures_dir, tmp_path, monkeypatch)

    directive = _child_closes(store, fixtures_dir)

    assert not directive.ok and store.load(CHILD).difficulty is not None
    assert "handed back to the parent stage" in str(directive.data), directive.data
