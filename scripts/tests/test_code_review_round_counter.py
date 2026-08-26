"""Stage 5 / item A: the cli.py-level wiring for SessionState.code_review_rounds —
increment on re-review, reset on approve/replan, and push/pop_subplan custody.
gates.py-level behavior (the round-release predicate and blockers wrap) is covered
by test_gates_round_release.py; this file locks the counter itself, mirroring
test_plan_review_round_budget.py's Group 2/3 style for the sibling axis."""
from __future__ import annotations

from argparse import Namespace

from agentctl import cli
from agentctl.state import (
    Actor,
    Criterion,
    GateRecord,
    Means,
    Node,
    Outcome,
    Partition,
    SessionState,
    Stage,
    StageStatus,
    Subject,
)


def ns(**kw):
    return Namespace(**kw)


def _dev_stage(index=1):
    return Stage(
        index=index, title="s1",
        subject=Subject(material="m", result="the expected image"),
        means=Means(means="Edit", method="implement"),
        actor=Actor(executor="spawn:developer"),
        criterion=Criterion(criterion_type="measurable", done_criterion="c"),
        outcome=Outcome(status=StageStatus.ACTIVE.value),
    )


# --- 1. cmd_code_review: increment only on a RE-review ------------------------

class _Mem:
    def __init__(self, state):
        self.s = state

    def load(self, _):
        return self.s

    def save(self, s):
        self.s = s


def _executing_state(*stages):
    return SessionState(
        session_id="cr", task_id="cr-task",
        node=Node.EXECUTING.value,
        weight_class="SUBSTANTIVE",
        route="SPAWN",
        approval=GateRecord("plan_approval", armed=True, passed=True, by="user"),
        partition=Partition(verdict="not-recommended"),
        stages=list(stages),
        current_stage=stages[0].index,
    )


def _store(*stages):
    return _Mem(_executing_state(*stages))


def _code_review(store, verdict, reviewer="code-reviewer", note="", concerns=None, code_ref=None):
    return cli.cmd_code_review(
        ns(session="cr", verdict=verdict, reviewer=reviewer, note=note,
           concerns=concerns, code_ref=code_ref),
        store=store,
    )


def test_first_review_does_not_count_as_a_round():
    store = _store(_dev_stage())
    _code_review(store, "revise", note="needs work")
    assert store.s.code_review_rounds == 0


def test_a_second_review_of_the_same_stage_counts_one_round():
    store = _store(_dev_stage())
    _code_review(store, "revise", note="needs work")
    _code_review(store, "revise", note="still needs work")
    assert store.s.code_review_rounds == 1
    _code_review(store, "pass")
    assert store.s.code_review_rounds == 2


def test_a_review_of_a_non_developer_stage_is_never_counted():
    non_dev = Stage(
        index=1, title="s1",
        subject=Subject(material="m", result="img"),
        means=Means(means="Read", method="plan"),
        actor=Actor(executor="spawn:planner"),
        criterion=Criterion(criterion_type="measurable", done_criterion="c"),
        outcome=Outcome(status=StageStatus.ACTIVE.value),
    )
    store = _store(non_dev)
    _code_review(store, "pass")
    _code_review(store, "pass")
    assert store.s.code_review_rounds == 0


# --- 2. reset sites: approve / replan ------------------------------------------

def _to_plan_ready(store, sid, plan):
    cli.cmd_start(ns(session=sid, task="demo", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)


def _sha256_file(p) -> str:
    import hashlib
    from pathlib import Path
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _to_executing(store, sid, plan):
    _to_plan_ready(store, sid, plan)
    cli.cmd_plan_review(ns(session=sid, verdict="pass", reviewer="thinker", concerns=None,
                           note="", target=plan, plan_digest=_sha256_file(plan)), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)


def test_approve_resets_code_review_rounds(store, fixtures_dir):
    sid = "cr-reset-approve"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(store, sid, plan)
    s = store.load(sid)
    s.code_review_rounds = 4
    store.save(s)

    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    assert store.load(sid).code_review_rounds == 0


def test_replan_resets_code_review_rounds(store, fixtures_dir):
    sid = "cr-reset-replan"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_executing(store, sid, plan)
    s = store.load(sid)
    s.code_review_rounds = 4
    store.save(s)

    d = cli.cmd_replan(ns(session=sid, plan=plan), store=store)
    assert d.ok is True
    assert store.load(sid).code_review_rounds == 0


# --- 3. push_subplan / pop_subplan custody -------------------------------------

def _parent_at_executing(store, sid, *, code_review_rounds):
    s = SessionState(session_id=sid, task_id="parent", weight_class="SUBSTANTIVE",
                     plan_path="/plan.toml", plan_verified=True,
                     node=Node.EXECUTING.value,
                     approval=GateRecord("plan_approval", armed=True, passed=True, by="user"),
                     current_stage=1,
                     code_review_rounds=code_review_rounds)
    store.save(s)
    return s


def test_a_service_sub_plan_neither_spends_nor_inherits_the_parents_code_review_rounds(store):
    """Custody, not discard — the same shape as plan_review_rounds beside it: the
    child argues code review on its own budget, and the pop restores the parent's
    count verbatim rather than merging the child's spend into it."""
    sid = "cr-custody"
    _parent_at_executing(store, sid, code_review_rounds=2)

    cli.cmd_push_subplan(ns(session=sid, plan="/tmp/child.toml", task="child",
                            originating_stage=1), store=store)
    state = store.load(sid)
    assert state.plan_stack[0].code_review_rounds == 2
    assert state.code_review_rounds == 0        # the child argues on its own budget

    # The child spends rounds of its own, then resolves.
    state.code_review_rounds = 3
    state.stages = [Stage(index=1, title="child stage",
                          subject=Subject(material="m", result="img"),
                          means=Means(means="Edit", method="do"),
                          actor=Actor(executor="in_thread"),
                          criterion=Criterion(criterion_type="measurable", done_criterion="done"),
                          outcome=Outcome(status=StageStatus.PASSED.value))]
    state.resolution = GateRecord("resolution", armed=True, passed=True, by="user")
    state.node = Node.RESOLVED.value
    state.current_stage = None
    store.save(state)

    assert cli.cmd_pop_subplan(ns(session=sid), store=store).ok is True
    restored = store.load(sid)
    assert restored.code_review_rounds == 2
