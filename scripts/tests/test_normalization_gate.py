"""The normalize-or-explicit-waiver gate at difficulty closure (R3).

A difficulty is a norm-failure (провал нормы = SIGNAL); because activity is
constituted by reproduction, closing one REQUIRES re-norming the reproducible
factor it exposed (перенормирование = ACT). The engine enforces the ACT as a
`replan` precondition at DIAGNOSING closure (gates.normalization_blockers): once
the declare->investigate->critique cycle is complete, replan is blocked until
`normalize` records the factor, or the user takes the explicit
--normalization-waiver escape for a genuinely one-off factor. The LEVEL
(note/leaf/principle) is payoff-gated cognition the engine never inspects. This
covers the deterministic SHELL; the ACT-vs-LEVEL cognition lives in the
overcome-difficulty skill / recording-experience.md."""
from argparse import Namespace

from agentctl import cli, gates
from agentctl.state import (
    Critique,
    Declaration,
    Difficulty,
    Investigation,
    Node,
    Normalization,
    SessionState,
    StageStatus,
)


def ns(**kw):
    return Namespace(**kw)


def _to_failed_stage1(store, sid, plan):
    cli.cmd_start(ns(session=sid, task="norm-demo", goal="", done_criterion="",
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


def _complete_cycle(store, sid):
    cli.cmd_declare(ns(session=sid, expected="e", actual="a", mismatch="m"), store=store)
    cli.cmd_investigate(ns(session=sid, localized_expectation="le", localized_actual="la",
                           hypotheses=["h1", "h2"]), store=store)
    cli.cmd_critique(ns(session=sid, functional_ground="fg", replanning_task="rt",
                        failure_address="нормативное"), store=store)


# --- guardian unit -----------------------------------------------------------

def _diagnosing(difficulty):
    s = SessionState(session_id="n", task_id="t", node=Node.DIAGNOSING.value)
    s.difficulty = difficulty
    return s


def _full_difficulty(*, normalization=None):
    return Difficulty(
        declaration=Declaration("e", "a", "m"),
        investigation=Investigation("le", "la", hypotheses=["h1", "h2"]),
        critique=Critique("fg", "rt"),
        normalization=normalization,
    )


def test_blockers_empty_outside_diagnosing():
    s = SessionState(session_id="x", task_id="t", node=Node.VERIFYING.value)
    s.difficulty = _full_difficulty()
    assert gates.normalization_blockers(s) == []


def test_blockers_empty_while_cycle_incomplete():
    # difficulty_blockers owns the incomplete-cycle case — this gate never double-reports
    s = _diagnosing(Difficulty(declaration=Declaration("e", "a", "m")))
    assert gates.normalization_blockers(s) == []


def test_blockers_block_complete_cycle_without_normalization():
    s = _diagnosing(_full_difficulty())
    blockers = gates.normalization_blockers(s)
    assert blockers and "re-norming" in blockers[0]


def test_blockers_block_empty_factor():
    s = _diagnosing(_full_difficulty(normalization=Normalization(factor="   ")))
    assert gates.normalization_blockers(s)


def test_blockers_clear_with_recorded_factor():
    s = _diagnosing(_full_difficulty(normalization=Normalization(factor="reproducible cause")))
    assert gates.normalization_blockers(s) == []


def test_blockers_clear_with_factor_and_no_level():
    # level is payoff-gated and may be None — a note below the leaf threshold still clears
    s = _diagnosing(_full_difficulty(normalization=Normalization(factor="cause", level=None)))
    assert gates.normalization_blockers(s) == []


# --- end-to-end: block / normalize / waiver ----------------------------------

def test_replan_blocked_without_normalization(store, fixtures_dir):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_failed_stage1(store, "b1", plan)
    _complete_cycle(store, "b1")
    d = cli.cmd_replan(ns(session="b1", plan=refined), store=store)
    assert d.ok is False
    assert d.action == "normalize"
    assert d.data["blockers"]
    # the difficulty is NOT closed — still in DIAGNOSING with the record intact
    assert store.load("b1").node == Node.DIAGNOSING.value


def test_normalize_unblocks_replan(store, fixtures_dir):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_failed_stage1(store, "b2", plan)
    _complete_cycle(store, "b2")
    nd = cli.cmd_normalize(ns(session="b2", factor="reproducible cause", level="leaf"), store=store)
    assert nd.ok is True and nd.action == "replan"
    d = cli.cmd_replan(ns(session="b2", plan=refined), store=store)
    assert d.ok is True
    state = store.load("b2")
    assert state.node == Node.VERIFYING.value
    assert state.difficulty is None  # cleared on exit
    assert state.stage(1).outcome.status == StageStatus.PENDING.value


def test_waiver_with_reason_unblocks_and_is_recorded(store, fixtures_dir):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_failed_stage1(store, "b3", plan)
    _complete_cycle(store, "b3")
    d = cli.cmd_replan(ns(session="b3", plan=refined,
                          normalization_waiver="genuinely one-off, no reproducible factor"),
                       store=store)
    assert d.ok is True
    state = store.load("b3")
    waived = [h for h in state.history if h.get("event") == "normalization_waived"]
    assert len(waived) == 1
    assert waived[0]["reason"] == "genuinely one-off, no reproducible factor"


def test_empty_waiver_rejected(store, fixtures_dir):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_failed_stage1(store, "b4", plan)
    _complete_cycle(store, "b4")
    d = cli.cmd_replan(ns(session="b4", plan=refined, normalization_waiver="   "), store=store)
    assert d.ok is False
    assert d.action == "normalize"
    state = store.load("b4")
    assert not any(h.get("event") == "normalization_waived" for h in state.history)
    assert state.node == Node.DIAGNOSING.value  # not closed


# --- cmd_normalize validation ------------------------------------------------

def test_normalize_requires_factor(store, fixtures_dir):
    _to_failed_stage1(store, "v1", str(fixtures_dir / "plan_two_stage.toml"))
    _complete_cycle(store, "v1")
    d = cli.cmd_normalize(ns(session="v1", factor="   ", level=None), store=store)
    assert d.ok is False and "factor" in d.detail
    assert store.load("v1").difficulty.normalization is None


def test_normalize_rejects_unknown_level(store, fixtures_dir):
    _to_failed_stage1(store, "v2", str(fixtures_dir / "plan_two_stage.toml"))
    _complete_cycle(store, "v2")
    d = cli.cmd_normalize(ns(session="v2", factor="cause", level="bogus"), store=store)
    assert d.ok is False and "level" in d.detail


def test_normalize_out_of_order_refused(store, fixtures_dir):
    # DIAGNOSING but only declared — critique missing => normalize is premature
    _to_failed_stage1(store, "v3", str(fixtures_dir / "plan_two_stage.toml"))
    cli.cmd_declare(ns(session="v3", expected="e", actual="a", mismatch="m"), store=store)
    d = cli.cmd_normalize(ns(session="v3", factor="cause", level="note"), store=store)
    assert d.ok is False
    assert store.load("v3").difficulty.normalization is None


def test_normalize_outside_diagnosing_refused(store, fixtures_dir):
    cli.cmd_start(ns(session="v4", task="t", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    d = cli.cmd_normalize(ns(session="v4", factor="cause", level="note"), store=store)
    assert d.ok is False and d.action == "noop"


# --- persistence: round-trip + legacy grandfather migration ------------------

def test_normalization_round_trips():
    s = SessionState(
        session_id="rt", task_id="t", node=Node.DIAGNOSING.value,
        difficulty=_full_difficulty(normalization=Normalization(factor="cause", level="principle")),
    )
    restored = SessionState.from_json(s.to_json())
    assert restored == s
    assert restored.difficulty.normalization.factor == "cause"
    assert restored.difficulty.normalization.level == "principle"


def test_legacy_state_without_normalization_field_migrates_to_none():
    """A persisted state whose difficulty omits the normalization key (pre-SCHEMA
    16 JSON) loads with normalization defaulting to None."""
    s = SessionState(
        session_id="mig", task_id="t", node=Node.DIAGNOSING.value,
        difficulty=_full_difficulty(normalization=Normalization(factor="cause")),
    )
    data = s.to_dict()
    data["difficulty"].pop("normalization")
    restored = SessionState.from_dict(data)
    assert restored.difficulty.normalization is None
    # and such a legacy session, at closure, is (correctly) blocked by the new gate
    assert gates.normalization_blockers(restored)
