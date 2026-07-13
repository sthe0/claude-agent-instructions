"""Goal-failure routing at difficulty closure (R2).

A goal-failure is ambiguous until ROUTED: does it address знание (сущее — the model
of the material was wrong, form was right) or норма (должное — целеполагание was
wrong), or does routing explicitly not apply (not_applicable)? The engine types the
routing as `Critique.failure_address` (reusing StatementKind's values verbatim — no
second enum — plus the not_applicable sentinel) and enforces it as a `replan`
precondition at DIAGNOSING closure (gates.failure_address_blockers): once the
declare->investigate->critique cycle is complete, replan is blocked until the critique
carries a legal routing value. A bare None (omission) blocks — the routing must be
DECIDED; an EXPLICIT not_applicable is a legal opt-out that clears. This mirrors R3's
normalization gate over the renorming act; the SMD content (which fault a failure
addresses) is the coordinator's cognition, the routing's EXISTENCE is the engine's.

This covers the deterministic SHELL: the pure guardian, cmd_critique validation, the
argparse `choices` guard, round-trip persistence, the legacy grandfather migration, and
the end-to-end block/route path at closure."""
from argparse import Namespace

import pytest

from agentctl import cli, gates
from agentctl.state import (
    Critique,
    Declaration,
    Difficulty,
    Investigation,
    Node,
    SessionState,
    StageStatus,
    FAILURE_ADDRESS_VALUES,
)


def ns(**kw):
    return Namespace(**kw)


# FAILURE_ADDRESS_VALUES REUSES StatementKind (no second enum) + the not_applicable
# sentinel — pin the exact set so a future rename of either surface trips here.
def test_failure_address_values_reuse_statement_kind():
    assert FAILURE_ADDRESS_VALUES == ("сущее", "должное", "not_applicable")


# --- guardian unit -----------------------------------------------------------

def _diagnosing(difficulty):
    s = SessionState(session_id="fa", task_id="t", node=Node.DIAGNOSING.value)
    s.difficulty = difficulty
    return s


def _full_difficulty(*, failure_address=None):
    return Difficulty(
        declaration=Declaration("e", "a", "m"),
        investigation=Investigation("le", "la", hypotheses=["h1", "h2"]),
        critique=Critique("fg", "rt", failure_address=failure_address),
    )


def test_blockers_empty_outside_diagnosing():
    s = SessionState(session_id="x", task_id="t", node=Node.VERIFYING.value)
    s.difficulty = _full_difficulty()  # bare None routing, but not at closure
    assert gates.failure_address_blockers(s) == []


def test_blockers_empty_while_cycle_incomplete():
    # difficulty_blockers owns the incomplete-cycle case — this gate never double-reports
    s = _diagnosing(Difficulty(declaration=Declaration("e", "a", "m")))
    assert gates.failure_address_blockers(s) == []


def test_blockers_block_complete_cycle_without_routing():
    s = _diagnosing(_full_difficulty(failure_address=None))
    blockers = gates.failure_address_blockers(s)
    assert blockers and "routing the goal-failure" in blockers[0]


def test_blockers_clear_with_content_fault():
    s = _diagnosing(_full_difficulty(failure_address="сущее"))
    assert gates.failure_address_blockers(s) == []


def test_blockers_clear_with_form_fault():
    s = _diagnosing(_full_difficulty(failure_address="должное"))
    assert gates.failure_address_blockers(s) == []


def test_blockers_clear_with_explicit_not_applicable():
    # the premise that distinguishes this gate from a mere field-presence check: an
    # EXPLICIT not_applicable is a legal opt-out that clears, distinct from a bare None
    s = _diagnosing(_full_difficulty(failure_address="not_applicable"))
    assert gates.failure_address_blockers(s) == []


def test_blockers_block_bogus_value():
    # defense in depth: an in-process caller that set the Critique directly, bypassing
    # argparse choices and cmd_critique validation, is still caught at the gate
    s = _diagnosing(_full_difficulty(failure_address="maybe"))
    blockers = gates.failure_address_blockers(s)
    assert blockers and "must be one of" in blockers[0]


# --- cmd_critique validation -------------------------------------------------

def _to_failed_stage1(store, sid, plan):
    cli.cmd_start(ns(session=sid, task="fa-demo", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)
    return cli.cmd_record_result(ns(session=sid, status="failed", actual="boom"), store=store)


def _declare_investigate(store, sid):
    cli.cmd_declare(ns(session=sid, expected="e", actual="a", mismatch="m"), store=store)
    cli.cmd_investigate(ns(session=sid, localized_expectation="le", localized_actual="la",
                           hypotheses=["h1", "h2"]), store=store)


def test_cmd_critique_accepts_valid_routing(store, fixtures_dir):
    _to_failed_stage1(store, "cc1", str(fixtures_dir / "plan_two_stage.toml"))
    _declare_investigate(store, "cc1")
    d = cli.cmd_critique(ns(session="cc1", functional_ground="fg", replanning_task="rt",
                            failure_address="сущее"), store=store)
    assert d.ok is True
    assert store.load("cc1").difficulty.critique.failure_address == "сущее"


def test_cmd_critique_rejects_bogus_routing(store, fixtures_dir):
    _to_failed_stage1(store, "cc2", str(fixtures_dir / "plan_two_stage.toml"))
    _declare_investigate(store, "cc2")
    d = cli.cmd_critique(ns(session="cc2", functional_ground="fg", replanning_task="rt",
                            failure_address="maybe"), store=store)
    assert d.ok is False and "failure-address" in d.detail
    # the critique was not written with the bogus value
    assert store.load("cc2").difficulty.critique is None


def test_cmd_critique_omitted_routing_is_none(store, fixtures_dir):
    # omission is legal at critique time (routing may be decided later); the gate is
    # what demands it be present at closure
    _to_failed_stage1(store, "cc3", str(fixtures_dir / "plan_two_stage.toml"))
    _declare_investigate(store, "cc3")
    d = cli.cmd_critique(ns(session="cc3", functional_ground="fg", replanning_task="rt"),
                         store=store)
    assert d.ok is True
    assert store.load("cc3").difficulty.critique.failure_address is None


# --- argparse choices guard (the outermost, declarative layer) ---------------

def test_argparse_rejects_bogus_choice():
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["critique", "--session", "s", "--functional-ground", "fg",
                           "--replanning-task", "rt", "--failure-address", "maybe"])


def test_argparse_accepts_each_legal_choice():
    parser = cli.build_parser()
    for value in FAILURE_ADDRESS_VALUES:
        args = parser.parse_args(["critique", "--session", "s", "--functional-ground", "fg",
                                  "--replanning-task", "rt", "--failure-address", value])
        assert args.failure_address == value


# --- persistence: round-trip + legacy grandfather migration ------------------

def test_failure_address_round_trips():
    for value in FAILURE_ADDRESS_VALUES:
        s = SessionState(
            session_id="rt", task_id="t", node=Node.DIAGNOSING.value,
            difficulty=_full_difficulty(failure_address=value),
        )
        restored = SessionState.from_json(s.to_json())
        assert restored == s
        assert restored.difficulty.critique.failure_address == value


def test_legacy_critique_without_failure_address_migrates_to_none():
    """A persisted state whose critique omits the failure_address key (pre-SCHEMA 17
    JSON) loads with failure_address defaulting to None."""
    s = SessionState(
        session_id="mig", task_id="t", node=Node.DIAGNOSING.value,
        difficulty=_full_difficulty(failure_address="должное"),
    )
    data = s.to_dict()
    data["difficulty"]["critique"].pop("failure_address")
    restored = SessionState.from_dict(data)
    assert restored.difficulty.critique.failure_address is None
    # and such a legacy session, at closure, is (correctly) blocked by the new gate
    assert gates.failure_address_blockers(restored)


# --- end-to-end: block / route at closure ------------------------------------

def _renorm(store, sid):
    # clear the normalization gate (checked before failure_address at closure) so the
    # replan reaches — and is decided by — the routing gate
    cli.cmd_normalize(ns(session=sid, factor="reproducible cause", level="note"), store=store)


def test_replan_blocked_without_routing(store, fixtures_dir):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_failed_stage1(store, "e1", plan)
    _declare_investigate(store, "e1")
    cli.cmd_critique(ns(session="e1", functional_ground="fg", replanning_task="rt"),
                     store=store)
    _renorm(store, "e1")
    d = cli.cmd_replan(ns(session="e1", plan=refined), store=store)
    assert d.ok is False
    assert d.action == "critique"
    assert d.data["blockers"]
    # the difficulty is NOT closed — still in DIAGNOSING with the record intact
    assert store.load("e1").node == Node.DIAGNOSING.value


def test_routed_critique_unblocks_replan(store, fixtures_dir):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_failed_stage1(store, "e2", plan)
    _declare_investigate(store, "e2")
    cli.cmd_critique(ns(session="e2", functional_ground="fg", replanning_task="rt",
                        failure_address="должное"), store=store)
    _renorm(store, "e2")
    d = cli.cmd_replan(ns(session="e2", plan=refined), store=store)
    assert d.ok is True
    state = store.load("e2")
    assert state.node == Node.VERIFYING.value
    assert state.difficulty is None  # cleared on exit
    assert state.stage(1).outcome.status == StageStatus.PENDING.value


def test_explicit_not_applicable_unblocks_replan(store, fixtures_dir):
    # not_applicable is a first-class routing decision, not an omission — it must clear
    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_failed_stage1(store, "e3", plan)
    _declare_investigate(store, "e3")
    cli.cmd_critique(ns(session="e3", functional_ground="fg", replanning_task="rt",
                        failure_address="not_applicable"), store=store)
    _renorm(store, "e3")
    d = cli.cmd_replan(ns(session="e3", plan=refined), store=store)
    assert d.ok is True
    assert store.load("e3").node == Node.VERIFYING.value
