"""The two hard gates: empty --by is refused at both (regression lock for feb673b),
non-empty passes, and the guardian predicates report the expected blockers."""
from argparse import Namespace

from agentctl import cli, gates
from agentctl.state import (
    Actor,
    Criterion,
    GateRecord,
    Means,
    Node,
    Outcome,
    SessionState,
    Stage,
    StageStatus,
    Subject,
)


def ns(**kw):
    return Namespace(**kw)


def _stage(index, status):
    return Stage(
        index=index,
        title="a",
        subject=Subject(material="m", result="img"),
        means=Means(means="Edit", method="do"),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(criterion_type="measurable", done_criterion="dc"),
        outcome=Outcome(status=status),
    )


def _to_plan_ready(store, sid, plan):
    cli.cmd_start(ns(session=sid, task="demo", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)


def _to_resolution(store, sid, plan):
    _to_plan_ready(store, sid, plan)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    # pass both stages of the two-stage fixture
    for _ in range(2):
        cli.cmd_next_stage(ns(session=sid), store=store)
        cli.cmd_record_result(ns(session=sid, status="passed", actual="ok",
                                  control="reviewed: ok"), store=store)
    cli.cmd_verify_final(ns(session=sid), store=store)
    # experience auto-activates for substantive sessions and gates resolution
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="searched"), store=store)
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="recorded"), store=store)


# --- empty --by refused at the plan-approval gate ------------------------

def test_approve_empty_by_is_refused(store, fixtures_dir):
    sid = "ga"
    _to_plan_ready(store, sid, str(fixtures_dir / "plan_two_stage.toml"))
    d = cli.cmd_approve(ns(session=sid, by=""), store=store)
    assert d.ok is False
    assert store.load(sid).node == Node.PLAN_READY.value  # gate held
    assert any("empty approver" in b for b in d.data["blockers"])


def test_approve_blank_by_is_refused(store, fixtures_dir):
    sid = "gab"
    _to_plan_ready(store, sid, str(fixtures_dir / "plan_two_stage.toml"))
    d = cli.cmd_approve(ns(session=sid, by="   "), store=store)
    assert d.ok is False
    assert store.load(sid).node == Node.PLAN_READY.value


def test_approve_nonempty_by_passes(store, fixtures_dir):
    sid = "gp"
    _to_plan_ready(store, sid, str(fixtures_dir / "plan_two_stage.toml"))
    d = cli.cmd_approve(ns(session=sid, by="alice"), store=store)
    assert d.ok is True
    assert store.load(sid).node == Node.APPROVED.value


# --- empty --by refused at the resolution gate ---------------------------

def test_resolve_empty_by_is_refused(store, fixtures_dir):
    sid = "gr"
    _to_resolution(store, sid, str(fixtures_dir / "plan_two_stage.toml"))
    d = cli.cmd_resolve(ns(session=sid, by=""), store=store)
    assert d.ok is False
    assert store.load(sid).node == Node.RESOLUTION.value  # gate held
    assert any("empty confirmer" in b for b in d.data["blockers"])


def test_resolve_nonempty_by_passes(store, fixtures_dir):
    sid = "grp"
    _to_resolution(store, sid, str(fixtures_dir / "plan_two_stage.toml"))
    d = cli.cmd_resolve(ns(session=sid, by="user", quality=5, quality_by="user-confirmed",
                           quality_note=None), store=store)
    assert d.ok is True
    assert store.load(sid).node == Node.RESOLVED.value


# --- guardian predicates -------------------------------------------------

def test_plan_approval_blockers_for_missing_plan():
    s = SessionState(session_id="x", task_id="t")
    blockers = gates.plan_approval_blockers(s)
    assert any("no plan artifact" in b for b in blockers)
    assert any("not verified" in b for b in blockers)


def test_plan_approval_blockers_empty_when_plan_verified():
    s = SessionState(session_id="x", task_id="t", plan_path="/p.toml", plan_verified=True)
    assert gates.plan_approval_blockers(s) == []


def test_resolution_blockers_reports_unpassed_stages():
    s = SessionState(
        session_id="x", task_id="t",
        stages=[
            _stage(1, StageStatus.PASSED.value),
            _stage(2, StageStatus.PENDING.value),
        ],
    )
    blockers = gates.resolution_blockers(s)
    assert any("[2]" in b for b in blockers)


def test_resolution_blockers_for_no_stages():
    s = SessionState(session_id="x", task_id="t")
    assert any("no stages" in b for b in gates.resolution_blockers(s))


def test_resolution_blockers_empty_when_all_passed():
    s = SessionState(
        session_id="x", task_id="t",
        approval=GateRecord("plan_approval", armed=True, passed=True),
        stages=[_stage(1, StageStatus.PASSED.value)],
    )
    assert gates.resolution_blockers(s) == []


def test_blockers_unknown_gate():
    s = SessionState(session_id="x", task_id="t")
    assert gates.blockers(s, "nope") == ["unknown gate 'nope'"]


# --- replan coverage gate: similarities preserved, differences change a means ---

def _cov_stage(index, *, means="Edit", method="do", conditions=None, invariants=None):
    s = {"index": index, "title": "s", "executor": "in_thread",
         "expected_result_image": "img", "done_criterion": "dc",
         "means": means, "method": method}
    if conditions is not None:
        s["conditions"] = conditions
    if invariants is not None:
        s["invariants"] = invariants
    return s


def _cov_doc(stages):
    from agentctl.plan import parse_plan
    return parse_plan({"meta": {"task_id": "t"}, "stage": stages})


def _critique(**kw):
    from agentctl.state import Critique
    base = dict(functional_ground="fg", replanning_task="rt",
                invariants_to_preserve=[], differences_to_remove=[])
    base.update(kw)
    return Critique(**base)


def test_coverage_uncovered_similarity_blocks():
    old = _cov_doc([_cov_stage(1)])
    new = _cov_doc([_cov_stage(1, conditions="something else")])
    crit = _critique(invariants_to_preserve=["keep idempotency"])
    blockers = gates.replan_coverage_blockers(old, new, crit)
    assert blockers and "keep idempotency" in blockers[0]


def test_coverage_unchanged_means_for_declared_difference_blocks():
    old = _cov_doc([_cov_stage(1, means="Edit", method="do")])
    new = _cov_doc([_cov_stage(1, means="Edit", method="do")])  # means identical
    crit = _critique(differences_to_remove=["ad-hoc retry"])
    blockers = gates.replan_coverage_blockers(old, new, crit)
    assert blockers and "means/method" in blockers[0]


def test_coverage_correct_plan_passes():
    old = _cov_doc([_cov_stage(1, means="Edit", method="blind reload")])
    new = _cov_doc([_cov_stage(1, means="Edit", method="mirror working caller",
                               conditions="keep idempotency")])
    crit = _critique(invariants_to_preserve=["keep idempotency"],
                     differences_to_remove=["blind reload"])
    assert gates.replan_coverage_blockers(old, new, crit) == []


def test_coverage_empty_split_passes_even_with_identical_means():
    old = _cov_doc([_cov_stage(1)])
    new = _cov_doc([_cov_stage(1)])  # nothing changed
    assert gates.replan_coverage_blockers(old, new, _critique()) == []


def test_coverage_rephrased_similarity_passes_after_normalization():
    """Case/whitespace-only rephrasing of a carried invariant must not block —
    the gate checks substance, not verbatim text."""
    old = _cov_doc([_cov_stage(1)])
    new = _cov_doc([_cov_stage(1, conditions="  Keep   Idempotency  ")])
    crit = _critique(invariants_to_preserve=["keep idempotency"])
    assert gates.replan_coverage_blockers(old, new, crit) == []


def test_coverage_missing_invariant_still_blocks_after_normalization():
    """Normalization must not turn the gate into a rubber stamp: an invariant
    genuinely absent from the corrected plan still blocks."""
    old = _cov_doc([_cov_stage(1)])
    new = _cov_doc([_cov_stage(1, conditions="totally unrelated text")])
    crit = _critique(invariants_to_preserve=["keep idempotency"])
    blockers = gates.replan_coverage_blockers(old, new, crit)
    assert blockers and "keep idempotency" in blockers[0]
