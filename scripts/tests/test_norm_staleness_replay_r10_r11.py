"""Replay of the real rating-after-landing plan revisions r10 -> r11 through the engine.

The fixtures under `fixtures/norm_staleness_replay/` are cut from the two real plan files
(prose shortened, every structural field kept; stage 6 verbatim). r10 -> r11 changes the
third final check's command and stage 6's `verify_command` -- neither is part of a stage's
interface. The walk that follows the replan must therefore re-review only what that edit
reaches: the base unit (the final check list is part of the base), stage 6's unit, and the
pair base-6 (the service side's `verify_command` moved). Every other unit and pair, the
three order elements covered by stage 6 and the recorded acceptance stay valid.
`r11_deliverable.toml` is the control: the same replan plus one change to stage 6's
expected result image, which IS interface.
"""
from __future__ import annotations

import hashlib
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import advisor, checkrun, cli, dispatch, gates, permissions
from agentctl.dispatch import RunResult
from agentctl.plan import load_plan

FIX = Path(__file__).resolve().parent / "fixtures" / "norm_staleness_replay"
SID = "replay"
STALE_SET = {
    "unit:base": "stale:unit",
    "unit:6": "stale:unit",
    "base-6": "stale:service",
}
ORDER_IDS = ("O1", "O2", "O3")
REQUIREMENTS = [f"R{i}" for i in range(1, 15)]
STAGE_6_BOUND_REQUIREMENTS = ["R1", "R4", "R5"]


def ns(**kw):
    return Namespace(**kw)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _unavailable(argv, timeout=None, stdin=""):
    return RunResult(1, "", "no such model")


def _judge_yes(argv, *, timeout=None, stdin=""):
    return RunResult(0, stdout="YES\nconcrete and adequate", stderr="")


@pytest.fixture(autouse=True)
def spawned(monkeypatch, tmp_path):
    """Every real runner symbol the engine resolves a default from is swapped for a
    recorder; a test (and this fixture's teardown) fails if any was invoked."""
    calls: list[list[str]] = []

    def recorder(argv, *args, **kwargs):
        calls.append(list(argv))
        return RunResult(1, "", "no such model")

    for module in (cli, advisor, dispatch, permissions):
        monkeypatch.setattr(module, "subprocess_runner", recorder)
    # The stage-check observer falls back to a Popen runner when no runner is passed in.
    monkeypatch.setattr(checkrun, "_default_runner", lambda timeout_s: recorder)
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")
    monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")
    monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
    monkeypatch.delenv("AGENTCTL_ACCEPTANCE", raising=False)
    monkeypatch.setenv("AGENTCTL_ESCALATION_LEDGER", str(tmp_path / "escalations.jsonl"))
    yield calls
    assert calls == [], f"a real runner was invoked: {calls}"


def _review(store, plan, rid, round_tag):
    return cli.cmd_plan_review(
        ns(session=SID, target=None, scope=f"topo:{rid}", verdict="pass", reviewer="thinker",
           concerns=None, concern_ids=None, note="", regression_command=None,
           plan_digest=_sha(plan),
           customer_questions=[f"{round_tag}: does the customer want {rid}?"]),
        store=store)


def _walk(store, plan, round_tag, only=None):
    """Review every row of the plan's pair walk (or just `only`) with a passing stub
    reviewer that returns one customer question per review act, worded afresh each
    `round_tag`; returns the ids walked."""
    doc = load_plan(str(plan))
    walked = []
    for row in gates.pair_walk(store.load(SID), doc, str(plan)):
        if only is not None and row["pair"] not in only:
            continue
        d = _review(store, plan, row["pair"], round_tag)
        assert d.ok, (row["pair"], d.detail)
        walked.append(row["pair"])
    return walked


def _candidates(store) -> dict:
    return {c["id"]: c for c in store.load(SID).plugins["premise"]["candidates"]
            if c["id"].startswith("qrev-")}


def _dismiss_all(store):
    for cid, cand in _candidates(store).items():
        if cand["disposition"] != "raised":
            continue
        d = cli.cmd_question_candidate_dispose(
            ns(session=SID, id=cid, as_="dismissed", reason="the customer answered", question=""),
            store=store)
        assert d.ok, d.detail


def _statuses(store, plan) -> dict:
    state = store.load(SID)
    doc = load_plan(str(plan))
    return {r["pair"]: gates.pair_status(state, doc, str(plan), r["pair"])
            for r in gates.pair_walk(state, doc, str(plan))}


def _stale(store, plan) -> dict:
    return {rid: status for rid, status in _statuses(store, plan).items() if status != "current"}


def _order_stale_notes(store) -> dict:
    bag = store.load(SID).plugins["premise"]["order_elements"]
    return {e["id"]: e.get("stale_note", "") for e in bag}


def _cover_order_by_stage_6(store):
    for oid in ORDER_IDS:
        d = cli.cmd_order_dispose(
            ns(session=SID, id=oid, as_="covered", stage=6, reason=""), store=store)
        assert d.ok, d.detail


def _approved_r10(store, tmp_path):
    """The r10 plan walked in full, its order elements covered by stage 6, approved and
    accepted -- the state the real session was in when r11 was written. Returns the plan path."""
    plan = tmp_path / "plan.toml"
    plan.write_text((FIX / "r10.toml").read_text())
    cli.cmd_start(ns(session=SID, task="replay", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=SID, chat=False, changed_lines=200, files=5, wall_clock_min=60,
                        tracker_key=None, architectural=True, external_effect=False,
                        new_dependency=False, public_api_change=False), store=store)
    cli.cmd_plan(ns(session=SID), store=store)
    d = cli.cmd_submit_plan(ns(session=SID, plan=str(plan)), store=store, runner=_unavailable)
    assert d.ok, d.detail
    assert len(_walk(store, plan, "r10")) == 29
    _dismiss_all(store)
    for oid in ORDER_IDS:
        cli.cmd_order_raise(ns(session=SID, id=oid, element=f"order element {oid}"), store=store)
    _cover_order_by_stage_6(store)
    d = cli.cmd_plan_review_compose(ns(session=SID, target=None), store=store)
    assert d.ok, d.detail
    d = cli.cmd_approve(ns(session=SID, by="user"), store=store, runner=_unavailable)
    assert d.ok, d.detail
    d = cli.cmd_accept(
        ns(session=SID, author="user", verdict=[f"{r}|pass" for r in REQUIREMENTS],
           note="compared the delivered engine against each requirement",
           bypass=False, bypass_reason=""), store=store, runner=_judge_yes)
    assert d.ok, d.detail
    return plan


def _replan_onto(store, plan, monkeypatch):
    """Replan onto the plan file as it now stands. The plan-review gate is off for the
    call itself -- it demands a whole-plan thinker pass on the new bytes, which is the
    walk this replay performs per stale id instead."""
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "0")
    d = cli.cmd_replan(
        ns(session=SID, plan=str(plan), renormalize=False, coverage_waiver=None,
           normalization_waiver=None, cost_log=None), store=store, runner=_unavailable)
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")
    return d


def _edited_to(plan, fixture):
    plan.write_text((FIX / fixture).read_text())


def test_the_replan_stales_exactly_the_base_unit_stage_six_unit_and_the_base_six_pair(
        store, tmp_path, monkeypatch):
    plan = _approved_r10(store, tmp_path)
    assert _stale(store, plan) == {}

    _edited_to(plan, "r11.toml")
    d = _replan_onto(store, plan, monkeypatch)

    assert d.ok, d.detail
    statuses = _statuses(store, plan)
    assert len(statuses) == 29
    assert _stale(store, plan) == STALE_SET
    for rid in ("6-3", "6-4", "6-5", "7-6", "base-1", "base-2", "base-3", "base-4",
                "base-5", "base-7", "base-8", "base-9"):
        assert statuses[rid] == "current", rid


def test_the_walk_re_reviews_only_the_stale_ids_and_questions_come_only_from_those_acts(
        store, tmp_path, monkeypatch):
    plan = _approved_r10(store, tmp_path)
    questions_before = _candidates(store)
    assert len(questions_before) == 29
    assert {c["disposition"] for c in questions_before.values()} == {"dismissed"}

    _edited_to(plan, "r11.toml")
    assert _replan_onto(store, plan, monkeypatch).ok
    assert set(_candidates(store)) == set(questions_before)

    stale_ids = list(_stale(store, plan))
    assert _walk(store, plan, "r11", only=set(stale_ids)) == stale_ids

    added = {cid: c for cid, c in _candidates(store).items() if cid not in questions_before}
    assert sorted(added) == sorted(f"qrev-{rid}-2" for rid in STALE_SET)
    assert {c["disposition"] for c in added.values()} == {"raised"}
    assert {cid: c["disposition"] for cid, c in _candidates(store).items()
            if cid in questions_before} == {cid: "dismissed" for cid in questions_before}

    assert _stale(store, plan) == {}
    composed = cli.cmd_plan_review_compose(ns(session=SID, target=None), store=store)
    assert composed.ok, composed.detail
    refused = cli.cmd_approve(ns(session=SID, by="user"), store=store, runner=_unavailable)
    assert refused.ok is False
    open_blockers = [b for b in refused.data["blockers"] if "qrev-" in b]
    assert [cid for cid in added if any(cid in b for b in open_blockers)] == list(added)
    assert len(open_blockers) == len(added)

    _dismiss_all(store)
    approved = cli.cmd_approve(ns(session=SID, by="user"), store=store, runner=_unavailable)
    assert approved.ok, approved.data.get("blockers")


def test_the_order_elements_and_the_acceptance_stay_valid_across_the_replan(
        store, tmp_path, monkeypatch):
    plan = _approved_r10(store, tmp_path)
    assert _order_stale_notes(store) == {oid: "" for oid in ORDER_IDS}
    assert gates._acceptance_review_resolution_blockers(store.load(SID)) == []

    _edited_to(plan, "r11.toml")
    d = _replan_onto(store, plan, monkeypatch)

    assert d.ok, d.detail
    assert _order_stale_notes(store) == {oid: "" for oid in ORDER_IDS}
    assert d.data["acceptance_stale"] == []
    assert gates.acceptance_stale_requirements(store.load(SID)) is None
    assert gates._acceptance_review_resolution_blockers(store.load(SID)) == []


def test_a_deliverable_changing_edit_stales_the_order_elements_and_the_acceptance(
        store, tmp_path, monkeypatch):
    plan = _approved_r10(store, tmp_path)

    _edited_to(plan, "r11_deliverable.toml")
    refused = _replan_onto(store, plan, monkeypatch)

    assert refused.ok is False
    blockers = refused.data["blockers"]
    assert [oid for oid in ORDER_IDS if any(f"'{oid}'" in b for b in blockers)] == list(ORDER_IDS)
    assert all(note for note in _order_stale_notes(store).values())

    _cover_order_by_stage_6(store)
    d = _replan_onto(store, plan, monkeypatch)

    assert d.ok, d.detail
    assert d.data["acceptance_stale"] == STAGE_6_BOUND_REQUIREMENTS
    # Both texts render the id list as a whole, so "R1" cannot be satisfied by "R10".
    rendered = str(STAGE_6_BOUND_REQUIREMENTS)
    assert rendered in d.detail
    blockers = gates._acceptance_review_resolution_blockers(store.load(SID))
    assert len(blockers) == 1 and rendered in blockers[0]
    assert set(_stale(store, plan)) == {"unit:base", "unit:6", "base-6", "7-6"}


def test_no_real_runner_is_reachable_during_the_replay(
        store, tmp_path, monkeypatch, spawned):
    plan = _approved_r10(store, tmp_path)
    _edited_to(plan, "r11.toml")
    assert _replan_onto(store, plan, monkeypatch).ok
    assert spawned == []

    for module in (cli, advisor, dispatch, permissions):
        module.subprocess_runner(["claude", "-p", module.__name__])
    checkrun._default_runner(1.0)(["pytest"])
    assert len(spawned) == 5
    spawned.clear()
