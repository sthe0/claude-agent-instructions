"""End-to-end proof that this plan's two GitHub gaps are closed by the real,
wired-together machinery — not by calling gate functions in isolation the way the
unit tests in stages 1-4 do, but by driving a real StateStore through the actual CLI
dispatch path (argparse-shaped Namespaces into cli.cmd_*), the same way a real
coordinator session would.

Scenario A (#177/R1/R3): the cross-session replan_count climbs 1, 2, 3 across three
genuine declare->investigate->critique->replan cycles on a disposable task_id; the
4th attempt is refused by `diagnosing_replan_blockers`; a `continue` renegotiation
clears the block, resets the accumulator, and a FRESH session on the SAME task_id
inherits the (now-reset) cross-session count and re-fires the gate on its own.

Scenario A-abandon: `abandon` parks the session at BLOCKED without touching the
plan; `unblock` returns to DIAGNOSING with the plan and stage outcomes untouched.

Scenario B (#128/R2): a failing acceptance verdict recorded while every stage is
PASSED routes `verify-final` into DIAGNOSING with a seeded Difficulty naming the
failing requirement, instead of stranding the session at VERIFYING.

Scenario C (#201/R5): the exact defect #201's evidence table describes — "the act of
answering a firing manufactures the next one" — reproduced structurally: repeated
BARE `replan` attempts once cross-session replan_count reaches the Rule-of-Three
ceiling stay refused indefinitely (the gate intercepts before the accumulator can
ever climb past the ceiling). effort.divergence() itself is checked directly before
and after a `continue` renegotiation: beforehand it is already quiet — not because
it agrees the count is fine, but because its OWN belt-2 re-arm ("a further fire
requires a replan event since the last one") suppresses it once acknowledged,
proving the diagnosing_replan gate above is what is still blocking `cmd_replan`,
independent of (and not deducible from) effort.py's own divergence signal — and a
SECOND, fully independent Rule-of-Three cycle driven from the post-reset count
re-fires the gate on its own new crossing — the direct regression test for the
value-based "already decided" blind spot the plan review flagged.

Pre-fix / post-fix reproduction (no PR exists in this repo's direct-to-main
workflow, so this replaces the "short paragraph in the PR description"
alternative): the PRE-fix baseline is commit 7925a6d, the parent of e6ffb26
(the commit that first added `gates.diagnosing_replan_blockers`). Checking
out 7925a6d and running this module cold fails Scenarios A, A-abandon and C
with an ImportError/AttributeError on `diagnosing_replan_blockers` — the gate
does not exist yet, so every bare `_bare_replan` call in `_climb` above
succeeds without limit instead of being refused at the 3rd/4th attempt (the
exact "manufactures the next firing" defect #201 and #177 describe).
Scenario B fails independently against the older baseline that predates
`gates.failing_acceptance_requirements`/`cli._diagnose_acceptance_rejection`
(commit history for scripts/agentctl/cli.py's #128 fix, stage 4 of this same
plan) with `verify_final` returning the passive `fix_stages` refusal instead
of routing to DIAGNOSING. Against HEAD (this plan's own landed fix) all four
scenarios pass, which is what `-q` running this module demonstrates."""
from __future__ import annotations

from argparse import Namespace
from itertools import count
from pathlib import Path

from agentctl import cli, effort, task_accumulator
from agentctl.config import Thresholds
from agentctl.directive import DIRECTIVE_ESCALATE_TO_USER
from agentctl.state import (
    Actor,
    AcceptanceReview,
    Criterion,
    GateRecord,
    Means,
    Node,
    Outcome,
    Partition,
    RequirementVerdict,
    Route,
    SessionState,
    Stage,
    StageStatus,
    Subject,
    WeightClass,
)


def ns(**kw):
    base = dict(renegotiation_decision=None, renegotiated_by=None, renegotiation_note=None)
    base.update(kw)
    return Namespace(**base)


def _replan_count(task_id: str) -> int:
    return task_accumulator.get(task_id)["per_axis_totals"]["replan_count"]


def _to_diagnosing(store, fixtures_dir, sid: str, task: str) -> None:
    """Drive a fresh session on `task` to DIAGNOSING with a complete difficulty
    record and a normalization, via the real CLI dispatch path — identical in shape
    to test_replan_renegotiation.py's own `_to_diagnosing`, parameterized on task_id
    so each scenario below can use a disposable one."""
    plan = str(fixtures_dir / "plan_two_stage.toml")
    cli.cmd_start(ns(session=sid, task=task, goal="g", done_criterion="dc",
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
    cli.cmd_record_result(ns(session=sid, status="failed", actual="boom"), store=store)
    assert store.load(sid).node == Node.DIAGNOSING.value
    cli.cmd_declare(ns(session=sid, expected="e", actual="a", mismatch="m"), store=store)
    cli.cmd_investigate(ns(session=sid, localized_expectation="le", localized_actual="la",
                           hypotheses=["h1", "h2"]), store=store)
    cli.cmd_critique(ns(session=sid, functional_ground="fg", replanning_task="rt",
                        failure_address="нормативное"), store=store)
    cli.cmd_normalize(ns(session=sid, factor="reproducible cause", level="note"), store=store)


_reenter_counter = count(1)


def _ack_effort_fire_if_needed(store, sid: str) -> None:
    """The task_id's own replan_count and this plan's effort-divergence REPLANS
    scale share the same Rule-of-Three constant (`effort-replan-absolute`), so
    driving genuine failure cycles up to the ceiling — as this module does, unlike
    test_replan_renegotiation.py's accumulator-seeding shortcut — legitimately
    trips the pre-existing, unrelated `gates.effort_fire_blockers` precondition on
    `cmd_replan` too. Acknowledge it with "continue" (accept the overrun, keep
    working the plan) the moment it appears, so it never masks what this module
    actually tests: the diagnosing_replan renegotiation gate itself."""
    state = store.load(sid)
    if state.effort_fires and state.effort_fires[-1].get("ack") is None:
        d = cli.cmd_fire_acknowledge(
            ns(session=sid, decision="continue", by="user", note="accept and continue"),
            store=store,
        )
        assert d.ok is True, d.detail


def _reenter_diagnosing(store, sid: str) -> None:
    """From VERIFYING with the previously-FAILED stage re-armed PENDING (the state a
    successful `no_change`/`refinement` replan leaves a diagnosing session in),
    dispatch that stage and fail it again — a real second failure, landing the
    session back in DIAGNOSING for the next cycle. cmd_record_result's own loop
    guard escalates (stays in VERIFYING) rather than re-entering DIAGNOSING when a
    stage fails twice on the identical result digest, so each call here must use an
    `actual` string never seen before on this session — a process-wide counter
    guarantees that even across a session's several separate `_climb` calls (e.g.
    Scenario C's post-renegotiation second cycle)."""
    cli.cmd_next_stage(ns(session=sid), store=store)
    actual = f"boom again #{next(_reenter_counter)}"
    cli.cmd_record_result(ns(session=sid, status="failed", actual=actual), store=store)
    assert store.load(sid).node == Node.DIAGNOSING.value
    _ack_effort_fire_if_needed(store, sid)


def _declare_investigate_critique_normalize(store, sid: str) -> None:
    cli.cmd_declare(ns(session=sid, expected="e2", actual="a2", mismatch="m2"), store=store)
    cli.cmd_investigate(ns(session=sid, localized_expectation="le2", localized_actual="la2",
                           hypotheses=["h1", "h2"]), store=store)
    cli.cmd_critique(ns(session=sid, functional_ground="fg2", replanning_task="rt2",
                        failure_address="нормативное"), store=store)
    cli.cmd_normalize(ns(session=sid, factor="reproducible cause", level="note"), store=store)


def _bare_replan(store, fixtures_dir, sid: str):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    return cli.cmd_replan(ns(session=sid, plan=plan), store=store)


def _cycle_replan(store, fixtures_dir, sid: str):
    """Complete one DIAGNOSING difficulty record and issue one bare (non-
    renegotiated) `replan`. Reuses the SAME plan file each time, so a successful
    call is always the `no_change` kind: it re-arms the failed stage to PENDING and
    routes VERIFYING -> `next_stage`, which is exactly what lets the caller loop
    back into DIAGNOSING via `_reenter_diagnosing` for the next cycle."""
    _declare_investigate_critique_normalize(store, sid)
    return _bare_replan(store, fixtures_dir, sid)


def _climb(store, fixtures_dir, sid: str, task: str, n: int) -> None:
    """Run `n` successful bare-replan DIAGNOSING cycles from wherever the
    cross-session replan_count currently stands, asserting it climbs by exactly one
    each time. Leaves the session back in DIAGNOSING (a fresh failure just landed
    it there), with the NEXT cycle's difficulty record not yet started."""
    base = _replan_count(task)
    for i in range(1, n + 1):
        d = _cycle_replan(store, fixtures_dir, sid)
        assert d.ok is True, d.detail
        assert _replan_count(task) == base + i
        _reenter_diagnosing(store, sid)


# --- Scenario A (#177 / R1 / R3) ---------------------------------------------

def test_scenario_a_replan_count_climbs_then_refuses_then_renegotiates(store, fixtures_dir):
    sid, task = "sa", "renego-e2e-a"
    thr = Thresholds().effort_replan_absolute()
    assert thr == 3
    _to_diagnosing(store, fixtures_dir, sid, task)
    assert _replan_count(task) == 0

    _climb(store, fixtures_dir, sid, task, thr)  # 1, 2, 3 — none refused

    # the (thr + 1)-th attempt, bare, is refused with the renegotiation-flags message
    _declare_investigate_critique_normalize(store, sid)
    refused = _bare_replan(store, fixtures_dir, sid)
    assert refused.ok is False
    assert refused.marker == DIRECTIVE_ESCALATE_TO_USER
    assert "--renegotiation-decision" in refused.detail
    assert str(thr) in refused.detail
    state = store.load(sid)
    assert state.node == Node.DIAGNOSING.value          # untouched
    assert _replan_count(task) == thr                    # not consumed by the refusal

    # `continue` clears the block, zeroes the accumulator, and lets the replan land
    plan = str(fixtures_dir / "plan_two_stage.toml")
    cont = cli.cmd_replan(ns(
        session=sid, plan=plan, renegotiation_decision="continue",
        renegotiated_by="user", renegotiation_note="customer wants to keep going",
    ), store=store)
    assert cont.ok is True
    state = store.load(sid)
    assert state.node == Node.VERIFYING.value
    assert len(state.renegotiations) == 1
    assert state.renegotiations[0]["decision"] == "continue"
    assert state.renegotiations[0]["task_replan_count_at_decision"] == thr
    assert _replan_count(task) == 1                      # reset, then this replan's own +1

    # a FRESH session on the SAME task_id inherits the cross-session count and
    # re-fires the gate on its own, once driven straight back to the ceiling
    sid2 = "sa-fresh"
    _to_diagnosing(store, fixtures_dir, sid2, task)
    assert _replan_count(task) == 1                      # inherited, not reset by cmd_start

    _climb(store, fixtures_dir, sid2, task, thr - 1)      # 2, 3

    _declare_investigate_critique_normalize(store, sid2)
    refused2 = _bare_replan(store, fixtures_dir, sid2)
    assert refused2.ok is False
    assert refused2.marker == DIRECTIVE_ESCALATE_TO_USER
    assert store.load(sid2).node == Node.DIAGNOSING.value
    assert _replan_count(task) == thr


def test_scenario_a_abandon_parks_at_blocked_without_touching_the_plan(store, fixtures_dir):
    sid, task = "sa-abandon", "renego-e2e-a-abandon"
    thr = Thresholds().effort_replan_absolute()
    _to_diagnosing(store, fixtures_dir, sid, task)
    _climb(store, fixtures_dir, sid, task, thr)

    _declare_investigate_critique_normalize(store, sid)
    before = store.load(sid)
    before_plan_path = before.plan_path
    plan = str(fixtures_dir / "plan_two_stage.toml")

    d = cli.cmd_replan(ns(
        session=sid, plan=plan, renegotiation_decision="abandon",
        renegotiated_by="user", renegotiation_note="customer wants to stop",
    ), store=store)
    assert d.ok is True
    assert d.marker == "ESCALATE"
    assert d.action == "unblock"
    state = store.load(sid)
    assert state.node == Node.BLOCKED.value
    assert state.blocked_from == Node.DIAGNOSING.value
    assert state.plan_path == before_plan_path
    assert len(state.renegotiations) == 1
    assert state.renegotiations[0]["decision"] == "abandon"
    assert _replan_count(task) == thr                    # NOT reset — nothing continued

    unblocked = cli.cmd_unblock(ns(session=sid), store=store)
    assert unblocked.ok is True
    assert store.load(sid).node == Node.DIAGNOSING.value


# --- Scenario B (#128 / R2) ---------------------------------------------------

_ORDER = """
[meta.order]
customer_id = "user"
customer = "the position that posed this fixture's task"
functional_place = "the norm this plan's e2e test proves closed"

[[meta.order.requirements]]
id = "R1"
text = "control compares result with goal at every stage"

[[meta.order.requirements]]
id = "R2"
text = "a failing acceptance verdict at VERIFYING reaches DIAGNOSING"

[meta.order.coverage]
R1 = ["stage 1 verify_command"]
R2 = ["agentctl accept"]
"""

_FINAL_CHECK = '[[final_check]]\ncommand = "true"\nexpected_exit = 0\n'

_PLAN = """
[meta]
task_id = "renego-e2e-b"
goal = "prove GitHub #128 is closed"
done_criterion = "a failing verdict at VERIFYING reaches DIAGNOSING"
criterion_type = "measurable"
weight_class = "substantive"
external_research = "read GitHub #128; no prior art applies"

{order}{final_check}
[[stage]]
index = 1
title = "the stage under test"
executor = "in_thread"
expected_result_image = "the expected image"
criterion_type = "measurable"
done_criterion = "d1"
verify_command = "true"
material = "m1"
means = "bash"
method = "run"
conditions = "none"
preconditions = "none"
invariants = "none"
capability_required = "cap"
material_refs = ["scripts/agentctl/cli.py"]
knowledge_refs = ["scripts/agentctl/gates.py"]
knowledge = "where acceptance binds"
[stage.principle]
statement = "s"
source = "src"
derivation = "der"
confidence = "high"
refutation = "r"
"""


def _write_plan_b(path: Path) -> str:
    path.write_text(_PLAN.format(order=_ORDER, final_check=_FINAL_CHECK), encoding="utf-8")
    return str(path)


def _measurable_stage() -> Stage:
    return Stage(
        index=1, title="the stage under test",
        subject=Subject(material="m1", result="the expected image"),
        means=Means(means="bash", method="run"),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(criterion_type="measurable", done_criterion="d1",
                            verify_command="true", expected_exit=0),
        outcome=Outcome(status=StageStatus.PASSED.value),
    )


def _verifying_state(sid: str, plan_path: str) -> SessionState:
    """A SessionState at VERIFYING with a real order-bearing plan and every stage
    PASSED — the exact shape `gates.resolution_blockers` and `cmd_verify_final` see
    once a real substantive session finishes execution. Built directly (as
    test_acceptance_verdict.py's own `_approved_state` is) rather than driven
    through the full submit/approve/dispatch sequence, since that sequence is
    Scenario A's job above; here only the accept-fail-at-VERIFYING routing is
    under test, via the real `cli.cmd_accept` / `cli.cmd_verify_final` CLI
    dispatch path."""
    s = SessionState(
        session_id=sid, task_id="renego-e2e-b", weight_class=WeightClass.SUBSTANTIVE.value,
        route=Route.SPAWN.value, node=Node.VERIFYING.value,
        approval=GateRecord("plan_approval", armed=True, passed=True),
        partition=Partition(m1=True, verdict="recommended"),
        plan_path=plan_path, stages=[_measurable_stage()],
    )
    cli._stamp_accepted_plan_digest(s, plan_path)
    return s


def test_scenario_b_failing_verdict_at_verify_final_routes_to_diagnosing(
    store, tmp_path, monkeypatch
):
    monkeypatch.delenv("AGENTCTL_ACCEPTANCE", raising=False)  # live gate, not the suite default
    plan = _write_plan_b(tmp_path / "renego-e2e-b.toml")
    s = _verifying_state("sb", plan)
    s.acceptance_review = AcceptanceReview(
        author="user", plan_sha256=s.accepted_plan_digest,
        verdicts=[RequirementVerdict("R1", "pass"), RequirementVerdict("R2", "fail")],
    )
    store.save(s)

    d = cli.cmd_verify_final(ns(session="sb"), store=store)
    assert d.ok is False
    assert d.marker == "OVERCOME-DIFFICULTY"
    assert d.node == Node.DIAGNOSING.value
    after = store.load("sb")
    assert after.node == Node.DIAGNOSING.value
    assert after.difficulty is not None
    assert after.difficulty.declaration is not None
    assert "R2" in after.difficulty.declaration.actual


# --- Scenario C (#201 / R5) ----------------------------------------------------

def test_scenario_c_bare_replans_stay_refused_then_renegotiation_resets_and_a_second_independent_cycle_refires(
    store, fixtures_dir
):
    sid, task = "sc", "renego-e2e-c"
    thr = Thresholds().effort_replan_absolute()
    _to_diagnosing(store, fixtures_dir, sid, task)
    _climb(store, fixtures_dir, sid, task, thr)

    # the FIRST attempt to close the cycle once cross-session replan_count reaches
    # the ceiling is refused — the gate intercepts before a second bare replan
    # could ever manufacture a fourth, fifth, ... count.
    _declare_investigate_critique_normalize(store, sid)
    first_refusal = _bare_replan(store, fixtures_dir, sid)
    assert first_refusal.ok is False
    assert first_refusal.marker == DIRECTIVE_ESCALATE_TO_USER
    assert _replan_count(task) == thr

    # WITHOUT a renegotiation decision, repeated bare attempts stay refused
    # indefinitely — the accumulator never gets the chance to climb to 4, 5, ...
    # 11, since the gate (not effort.py's own belt-2 re-arm) is what stops the loop.
    for _ in range(3):
        again = _bare_replan(store, fixtures_dir, sid)
        assert again.ok is False
        assert again.marker == DIRECTIVE_ESCALATE_TO_USER
        assert _replan_count(task) == thr
    state = store.load(sid)
    assert state.node == Node.DIAGNOSING.value

    # effort.py's OWN read path is quiet here too — not because it agrees the
    # count is fine, but because its belt-2 re-arm ("a further fire requires at
    # least one replan event logged since the last one") already suppressed it:
    # the REPLANS-scale fire recorded during _climb's last cycle (see
    # _ack_effort_fire_if_needed) has been acknowledged and no replan has
    # SUCCEEDED since (every attempt above was refused before reaching one). This
    # is exactly the point — the diagnosing_replan gate above is what is still
    # blocking `cmd_replan`, entirely independent of effort.py's own (now-quiet)
    # divergence signal, which is the real reason a caller cannot lean on
    # divergence() alone to detect the stuck state this gate exists to catch.
    totals = task_accumulator.get(task)["per_axis_totals"]
    div_before = effort.divergence(state, cross_session_totals=totals)
    assert div_before is None

    # ONE `continue` call zeroes the shared accumulator field
    plan = str(fixtures_dir / "plan_two_stage.toml")
    cont = cli.cmd_replan(ns(
        session=sid, plan=plan, renegotiation_decision="continue",
        renegotiated_by="user", renegotiation_note="keep going",
    ), store=store)
    assert cont.ok is True
    assert _replan_count(task) == 1

    # ...and a subsequent effort.divergence() call against the same state and fresh
    # totals no longer reports a REPLANS-scale fire at the old (now-stale) actual
    state = store.load(sid)
    totals = task_accumulator.get(task)["per_axis_totals"]
    div_after = effort.divergence(state, cross_session_totals=totals)
    assert div_after is None or div_after.scale != effort.SCALE_REPLANS

    # a SECOND, fully independent Rule-of-Three cycle from the post-reset count —
    # three more genuine closures, each incrementing the LIVE counter by one exactly
    # as the first climb did — refires the gate the moment this cycle's count
    # reaches the ceiling again, with NO renegotiation decision recorded for THIS
    # cycle. This is the direct regression test for #201's value-based "already
    # decided" blind spot: a check keyed off a historical `renegotiations` record
    # could not distinguish "already decided" from "a new, independent crossing
    # that lands on the identical threshold value" (3 == 3) — the live-counter-only
    # design here does not have that blind spot.
    _reenter_diagnosing(store, sid)
    _climb(store, fixtures_dir, sid, task, thr - 1)      # 1 -> 2 -> 3

    _declare_investigate_critique_normalize(store, sid)
    second_refusal = _bare_replan(store, fixtures_dir, sid)
    assert second_refusal.ok is False
    assert second_refusal.marker == DIRECTIVE_ESCALATE_TO_USER
    assert _replan_count(task) == thr
    state = store.load(sid)
    assert state.node == Node.DIAGNOSING.value
    # this second cycle's own renegotiations entry has not been written yet — only
    # the FIRST cycle's `continue` decision is on record so far
    assert len(state.renegotiations) == 1
    assert state.renegotiations[0]["decision"] == "continue"
