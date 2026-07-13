"""The overcome-difficulty sub-spine (Variant B): a FAILED stage routes to the
DIAGNOSING node; the engine enforces declare -> investigate -> critique in order
and machine-blocks `replan` (via gates.difficulty_blockers) until the Difficulty
record is complete. The cognition stays in the overcome-difficulty skill; this
covers the deterministic SHELL the engine owns."""
from argparse import Namespace

from agentctl import cli, gates
from agentctl.state import (
    Critique,
    Declaration,
    Difficulty,
    Investigation,
    Node,
    SessionState,
    StageStatus,
)


def ns(**kw):
    return Namespace(**kw)


def _to_failed_stage1(store, sid, plan):
    """Drive a substantive session to EXECUTING stage 1, then fail it -> DIAGNOSING."""
    cli.cmd_start(ns(session=sid, task="diff-demo", goal="", done_criterion="",
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


def _declare(store, sid):
    return cli.cmd_declare(ns(session=sid, expected="e", actual="a", mismatch="m"), store=store)


def _investigate(store, sid, hypotheses=("h1", "h2")):
    return cli.cmd_investigate(ns(session=sid, localized_expectation="le",
                                  localized_actual="la",
                                  hypotheses=list(hypotheses)), store=store)


def _critique(store, sid):
    return cli.cmd_critique(ns(session=sid, functional_ground="fg",
                               replanning_task="rt",
                               failure_address="нормативное"), store=store)


def _normalize(store, sid, *, factor="reproducible cause", level="note"):
    return cli.cmd_normalize(ns(session=sid, factor=factor, level=level), store=store)


# --- entry: FAILED -> DIAGNOSING ---------------------------------------------

def test_failed_stage_enters_diagnosing(store, fixtures_dir):
    d = _to_failed_stage1(store, "f1", str(fixtures_dir / "plan_two_stage.toml"))
    assert d.ok is False
    assert d.node == Node.DIAGNOSING.value
    assert d.action == "declare"
    assert d.marker == "OVERCOME-DIFFICULTY"
    state = store.load("f1")
    assert state.difficulty is not None
    assert not state.difficulty.complete()


# --- the gate: replan blocked until complete ---------------------------------

def test_replan_blocked_while_difficulty_incomplete(store, fixtures_dir):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_failed_stage1(store, "f2", plan)
    d = cli.cmd_replan(ns(session="f2", plan=plan), store=store)
    assert d.ok is False
    assert d.action == "declare"
    assert d.data["blockers"]  # names the missing sections


def test_replan_allowed_after_full_cycle(store, fixtures_dir):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_failed_stage1(store, "f3", plan)
    _declare(store, "f3")
    _investigate(store, "f3")
    c = _critique(store, "f3")
    assert c.action == "replan"  # cycle complete; replan unblocked
    _normalize(store, "f3")  # re-norm the reproducible factor before closure
    d = cli.cmd_replan(ns(session="f3", plan=refined), store=store)
    assert d.ok is True
    assert d.action == "next_stage"
    state = store.load("f3")
    assert state.node == Node.VERIFYING.value
    assert state.difficulty is None  # cleared on exit
    assert state.stage(1).outcome.status == StageStatus.PENDING.value  # re-armed


def test_substantive_replan_from_diagnosing_rearms_gate(store, fixtures_dir):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    bigger = str(fixtures_dir / "plan_two_stage_substantive.toml")
    _to_failed_stage1(store, "f4", plan)
    _declare(store, "f4")
    _investigate(store, "f4")
    _critique(store, "f4")
    _normalize(store, "f4")  # re-norm the reproducible factor before closure
    d = cli.cmd_replan(ns(session="f4", plan=bigger), store=store)
    assert d.marker == "PLAN-READY"
    state = store.load("f4")
    assert state.node == Node.PLAN_READY.value
    assert state.difficulty is None
    assert not state.approval.passed  # must re-approve


# --- ordering enforcement ----------------------------------------------------

def test_investigate_before_declare_refused(store, fixtures_dir):
    _to_failed_stage1(store, "f5", str(fixtures_dir / "plan_two_stage.toml"))
    d = _investigate(store, "f5")
    assert d.ok is False
    assert d.action == "declare"
    assert store.load("f5").difficulty.investigation is None


def test_critique_before_investigation_refused(store, fixtures_dir):
    _to_failed_stage1(store, "f6", str(fixtures_dir / "plan_two_stage.toml"))
    _declare(store, "f6")
    d = _critique(store, "f6")
    assert d.ok is False
    assert store.load("f6").difficulty.critique is None


def test_difficulty_commands_refused_outside_diagnosing(store, fixtures_dir):
    # at PLAN_READY (never failed a stage) declare must refuse
    cli.cmd_start(ns(session="f7", task="t", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session="f7", chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session="f7"), store=store)
    cli.cmd_submit_plan(ns(session="f7", plan=str(fixtures_dir / "plan_two_stage.toml")), store=store)
    d = _declare(store, "f7")
    assert d.ok is False
    assert d.action == "noop"


# --- guardian unit -----------------------------------------------------------

def test_difficulty_blockers_unit():
    # outside DIAGNOSING -> unconstrained
    s = SessionState(session_id="g", task_id="t", node=Node.EXECUTING.value,
                     approval=__import__("agentctl.state", fromlist=["GateRecord"]).GateRecord(
                         "plan_approval", armed=True, passed=True, by="u"))
    assert gates.difficulty_blockers(s) == []

    # in DIAGNOSING, no record -> blocked
    s2 = SessionState(session_id="g2", task_id="t", node=Node.DIAGNOSING.value)
    assert gates.difficulty_blockers(s2)

    # partial -> still blocked
    s2.difficulty = Difficulty(declaration=Declaration("e", "a", "m"))
    assert gates.difficulty_blockers(s2)

    # complete + well-formed -> clear
    s2.difficulty.investigation = Investigation("le", "la", hypotheses=["h1", "h2"])
    s2.difficulty.critique = Critique("fg", "rt")
    assert gates.difficulty_blockers(s2) == []


# --- shape enforcement: presence is necessary but not sufficient -------------

def _complete_difficulty(*, expected="e", actual="a", mismatch="m", hypotheses=("h1", "h2")):
    return Difficulty(
        declaration=Declaration(expected, actual, mismatch),
        investigation=Investigation("le", "la", hypotheses=list(hypotheses)),
        critique=Critique("fg", "rt"),
    )


def _diagnosing_with(diff):
    s = SessionState(session_id="sh", task_id="t", node=Node.DIAGNOSING.value)
    s.difficulty = diff
    return s


def test_blockers_reject_empty_declaration_field():
    s = _diagnosing_with(_complete_difficulty(mismatch="   "))
    blockers = gates.difficulty_blockers(s)
    assert blockers and "mismatch" in blockers[0]


def test_blockers_reject_single_hypothesis():
    s = _diagnosing_with(_complete_difficulty(hypotheses=("only one",)))
    blockers = gates.difficulty_blockers(s)
    assert blockers and "hypotheses" in blockers[0]


def test_blockers_reject_blank_hypotheses():
    s = _diagnosing_with(_complete_difficulty(hypotheses=("h1", "   ")))
    blockers = gates.difficulty_blockers(s)
    assert blockers and "hypotheses" in blockers[0]


def test_blockers_accept_well_formed_record():
    s = _diagnosing_with(_complete_difficulty())
    assert gates.difficulty_blockers(s) == []


# --- hypothesis distinctness check -----------------------------------------------

def test_blockers_reject_duplicate_hypotheses():
    """Hypotheses must be distinct after normalization."""
    s = _diagnosing_with(_complete_difficulty(hypotheses=("h1", "h1")))
    blockers = gates.difficulty_blockers(s)
    assert blockers and "distinct" in blockers[0]


def test_blockers_reject_case_insensitive_duplicate_hypotheses():
    """Hypotheses must be distinct after casefold normalization."""
    s = _diagnosing_with(_complete_difficulty(hypotheses=("H1", "h1")))
    blockers = gates.difficulty_blockers(s)
    assert blockers and "distinct" in blockers[0]


def test_blockers_reject_whitespace_insensitive_duplicate_hypotheses():
    """Hypotheses must be distinct after whitespace normalization."""
    s = _diagnosing_with(_complete_difficulty(hypotheses=("hypothesis 1", "hypothesis  1")))
    blockers = gates.difficulty_blockers(s)
    assert blockers and "distinct" in blockers[0]


def test_blockers_accept_genuinely_distinct_hypotheses():
    """Distinct hypotheses pass the check."""
    s = _diagnosing_with(_complete_difficulty(hypotheses=("H1", "h2  ")))
    assert gates.difficulty_blockers(s) == []


# --- declaration anti-template check ----------------------------------------------

def test_blockers_reject_placeholder_todo():
    """Declaration fields must not be 'todo' placeholder."""
    s = _diagnosing_with(_complete_difficulty(expected="TODO"))
    blockers = gates.difficulty_blockers(s)
    assert blockers and "placeholder" in blockers[0] and "expected" in blockers[0]


def test_blockers_reject_placeholder_tbd():
    """Declaration fields must not be 'tbd' placeholder."""
    s = _diagnosing_with(_complete_difficulty(actual="TBD"))
    blockers = gates.difficulty_blockers(s)
    assert blockers and "placeholder" in blockers[0] and "actual" in blockers[0]


def test_blockers_reject_placeholder_na():
    """Declaration fields must not be 'n/a' placeholder."""
    s = _diagnosing_with(_complete_difficulty(mismatch="N/A"))
    blockers = gates.difficulty_blockers(s)
    assert blockers and "placeholder" in blockers[0] and "mismatch" in blockers[0]


def test_blockers_reject_placeholder_ellipsis():
    """Declaration fields must not be '...' placeholder."""
    s = _diagnosing_with(_complete_difficulty(expected="..."))
    blockers = gates.difficulty_blockers(s)
    assert blockers and "placeholder" in blockers[0]


def test_blockers_reject_placeholder_literal_field_name():
    """Declaration fields must not use literal field names as placeholders."""
    s = _diagnosing_with(_complete_difficulty(expected="expected"))
    blockers = gates.difficulty_blockers(s)
    assert blockers and "placeholder" in blockers[0]


def test_blockers_reject_identical_expected_actual():
    """Declaration expected and actual must differ (normalized)."""
    s = _diagnosing_with(_complete_difficulty(expected="same", actual="same"))
    blockers = gates.difficulty_blockers(s)
    assert blockers and "must be distinct" in blockers[0]


def test_blockers_reject_identical_expected_actual_case_insensitive():
    """Declaration expected and actual must differ (case-insensitive)."""
    s = _diagnosing_with(_complete_difficulty(expected="Same", actual="same"))
    blockers = gates.difficulty_blockers(s)
    assert blockers and "must be distinct" in blockers[0]


def test_blockers_accept_real_observations():
    """Real, distinct observations pass all checks."""
    s = _diagnosing_with(_complete_difficulty(
        expected="expected output",
        actual="actual output",
        mismatch="the difference",
        hypotheses=("hypothesis 1", "hypothesis 2")
    ))
    assert gates.difficulty_blockers(s) == []


# --- structured critique split: round-trip + backward-compat migration -------

def test_critique_split_round_trips():
    """A SessionState carrying a Critique with both split lists survives
    from_json(to_json(s))==s and the lists are preserved verbatim."""
    crit = Critique(
        functional_ground="fg",
        replanning_task="rt",
        invariants_to_preserve=["legacy plans still load", "round-trip holds"],
        differences_to_remove=["means: ad-hoc retry", "method: blind reload"],
    )
    s = SessionState(
        session_id="rt1", task_id="t", node=Node.DIAGNOSING.value,
        difficulty=Difficulty(
            declaration=Declaration("e", "a", "m"),
            investigation=Investigation("le", "la", hypotheses=["h1", "h2"]),
            critique=crit,
        ),
    )
    restored = SessionState.from_json(s.to_json())
    assert restored == s
    rc = restored.difficulty.critique
    assert rc.invariants_to_preserve == ["legacy plans still load", "round-trip holds"]
    assert rc.differences_to_remove == ["means: ad-hoc retry", "method: blind reload"]


def test_old_critique_without_split_migrates_to_empty_lists():
    """A persisted state whose critique omits the new keys (pre-change JSON) loads
    with both split lists defaulting to []."""
    s = SessionState(
        session_id="mig1", task_id="t", node=Node.DIAGNOSING.value,
        difficulty=Difficulty(
            declaration=Declaration("e", "a", "m"),
            investigation=Investigation("le", "la", hypotheses=["h1", "h2"]),
            critique=Critique(functional_ground="fg", replanning_task="rt"),
        ),
    )
    data = s.to_dict()
    data["difficulty"]["critique"].pop("invariants_to_preserve")
    data["difficulty"]["critique"].pop("differences_to_remove")
    restored = SessionState.from_dict(data)
    assert restored.difficulty.critique.invariants_to_preserve == []
    assert restored.difficulty.critique.differences_to_remove == []
