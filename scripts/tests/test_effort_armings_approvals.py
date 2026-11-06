"""User approvals as the deciding acts of an arming: what closes it, what opens next, the review
term a replan adds, the budget of an unfolded parent, and the figures a deciding ask carries."""
import contextlib
import io
import json
import time
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, cli_common, cli_replan, config, cost, effort, gates
from agentctl import effort_segments as seg
from agentctl.config import Thresholds
from agentctl.plan import PlanError, diff_plans, load_plan
from agentctl.render import render_plan_grants
from agentctl.state import frame_path, stage_uid_key
from agentctl.store import FileStateStore
from conftest import STAGE_OBSERVATIONS, SUBSTANTIVE_ORDER, declare_venue

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TWO_STAGE = (FIXTURES / "plan_two_stage.toml").read_text(encoding="utf-8")
MEDIUM = {seg.SCALE_USD: 3.0, seg.SCALE_MINUTES: 25.0}
LARGE = {seg.SCALE_USD: 8.0, seg.SCALE_MINUTES: 60.0}
FALLBACK = {seg.SCALE_USD: 6.0, seg.SCALE_MINUTES: 15.4}
MULTIPLE_OF_MEDIUM = 15.0
METHOD = 'method = "build it in two passes"'


def plan_text(stage1=(), stage2=()):
    text = TWO_STAGE
    for artifact, lines in (('output_artifacts = ["mod.py"]', stage1), ('output_artifacts = ["tests/test_mod.py"]', stage2)):
        text = text.replace(artifact, "\n".join((artifact, *lines)))
    head, sep, tail = text.partition("\n[[stage]]")
    return head + "\n" + SUBSTANTIVE_ORDER + sep + tail


def with_added_stage(stage1=()):
    """A change of the stage set: the replan goes back to an approval instead of applying in place."""
    return plan_text(stage1=stage1) + f'''
[[stage]]
index = 3
title = "Add docs"
executor = "spawn:developer"
expected_result_image = "docs exist"
criterion_type = "measurable"
done_criterion = "docs.md present"
depends_on = [2]
'''


def without_stage_one():
    head, _, tail = plan_text().partition("[[stage]]\nindex = 1")
    _, _, second = tail.partition("[[stage]]\nindex = 2")
    return head + "[[stage]]\nindex = 1" + second.replace("depends_on = [1]", "depends_on = []")


def without_stage_two():
    return plan_text().partition("\n[[stage]]\nindex = 2")[0] + "\n"


class Eng:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.root = tmp_path / "state"
        self.store = FileStateStore(self.root)
        self._plans = 0

    def run(self, cmd, **flags):
        argv = ["--state-root", str(self.root), cmd.replace("_", "-")]
        for name, value in flags.items():
            flag = "--" + name.replace("_", "-")
            if value is True:
                argv.append(flag)
            elif isinstance(value, list):
                for item in value:
                    argv.extend([flag, str(item)])
            elif value is not None and value is not False:
                argv.extend([flag, str(value)])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(argv)
        result = json.loads(out.getvalue())
        result["rc"] = rc
        return result

    def write(self, text=None):
        self._plans += 1
        path = self.tmp / f"plan{self._plans}.toml"
        path.write_text(declare_venue(text or plan_text(), self.tmp), encoding="utf-8")
        return str(path)

    def state(self, sid="s"):
        return self.store.load(sid)

    def plan_ready(self, text=None, sid="s"):
        self.run("start", session=sid, task=f"task-{sid}", goal="g", done_criterion="dc")
        self.run("classify", session=sid, architectural=True, changed_lines=200, wall_clock_min=60)
        self.run("plan", session=sid)
        plan = self.write(text)
        assert self.run("submit_plan", session=sid, plan=plan)["ok"]
        return plan

    def executing(self, text=None, sid="s"):
        self.plan_ready(text, sid)
        assert self.run("approve", session=sid, by="user")["ok"]
        self.run("partition", session=sid)
        return self.run("next_stage", session=sid)

    def book(self, usd=0.0, minutes=0.0, uid=None, sid="s"):
        """Book onto a stage's open arming directly, as the ledger would."""
        state = self.state(sid)
        arming = open_of(state, uid or state.stage_uid(1))
        arming["booked"][seg.SCALE_USD] = arming["booked"].get(seg.SCALE_USD, 0.0) + usd
        arming["booked"][seg.SCALE_MINUTES] = arming["booked"].get(seg.SCALE_MINUTES, 0.0) + minutes
        self.store.save(state)
        return arming

    def row(self, usd, *, ts=None, sid="s"):
        """A cost-ledger row of this session's plan, written now."""
        state = self.state(sid)
        with open(self.tmp / "costs.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"plan_path": state.plan_path, "stage_index": 1, "cost_usd": usd,
                                     "ts": time.time() if ts is None else ts}) + "\n")

    def fired(self, sid="s", usd=30.0):
        self.executing(sid=sid)
        self.book(usd=usd, sid=sid)
        self.run("dispatch", session=sid)
        assert gates.open_fire_record(self.state(sid)) is not None

    def critiqued(self, sid="s"):
        self.run("record_result", session=sid, status="failed", actual="bad")
        self.run("declare", session=sid, expected="e", actual="a", mismatch="m")
        self.run("investigate", session=sid, localized_expectation="le", localized_actual="la",
                 hypothesis=["h1", "h2"])
        self.run("critique", session=sid, functional_ground="fg", replanning_task="rt",
                 failure_address="resource")
        self.run("normalize", session=sid, factor="reproducible cause", level="note")

    def replanned(self, text=None, sid="s", review=True):
        """A failed stage worked through to a candidate replan awaiting its approval."""
        plan = self.write(text or with_added_stage())
        directive = self.run("replan", session=sid, plan=plan)
        assert directive["ok"], directive
        if review:
            self.review(plan, sid)
        return plan

    def review(self, plan, sid="s"):
        return self.run("plan_review", session=sid, verdict="pass", reviewer="thinker", target=plan)

    def passed(self, sid="s", index=1):
        return self.run("record_result", session=sid, status="passed", actual="ok",
                        control="reviewed: ok", observation=STAGE_OBSERVATIONS[index - 1])

    def present(self, plan, kind, sid="s"):
        doc = load_plan(plan, strict=False)
        rendering = self.tmp / f"rendering-{sid}-{kind}.txt"
        blocks = (render_plan_grants(doc, fmt=fmt).strip() for fmt in ("compact", "full"))
        anchors = "\n".join(f"[stage {stage.index}] {stage.title}" for stage in doc.stages)
        rendering.write_text(f"{plan}\n\n{anchors}\n\n" + "\n\n".join(blocks), encoding="utf-8")
        target = {"plan": plan} if kind == "replan_diff" else {}
        return self.run("present_plan", session=sid, kind=kind, rendering_file=str(rendering), **target)

    def unfolded(self, sid="s", approve=None, expect_submit_ok=True):
        """Push a sub-plan under stage 1 up to the point of its approval; the parent's identity."""
        parent = self.state(sid).stage_uid(1)
        child = self.write(plan_text())
        assert self.run("push_subplan", session=sid, plan=child, task="child", originating_stage=1)["ok"]
        self.run("classify", session=sid, architectural=True, changed_lines=200, wall_clock_min=60)
        self.run("plan", session=sid)
        assert self.run("submit_plan", session=sid, plan=child)["ok"] is expect_submit_ok
        if approve:
            assert self.run("approve", session=sid, by=approve)["ok"]
        return parent


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")
    monkeypatch.setattr(cost, "COST_LOG", tmp_path / "costs.jsonl")


@pytest.fixture
def eng(tmp_path):
    return Eng(tmp_path)


def open_of(state, uid):
    return seg.open_arming_of(state.effort_segments, seg.stage_segment(uid))


def armings_of(state, uid):
    return [a for a in state.effort_segments["armings"] if a["segment"] == seg.stage_segment(uid)]


def retiers(state):
    return state.effort_segments.get("retier", [])


def renew(eng, act, sid="s", arming=None):
    state = eng.state(sid)
    held = arming or seg.current_arming(state.effort_segments)
    held = next(a for a in state.effort_segments["armings"] if a["arming_id"] == held["arming_id"])
    renewed = cli_common._renew_arming(state, eng.store, Namespace(), held, act, "user")
    eng.store.save(state)
    return renewed


def test_an_armings_budget_counts_only_the_review_terms_added_during_it(eng):
    eng.executing()
    uid = eng.state().stage_uid(1)
    eng.critiqued()
    eng.replanned()
    first = open_of(eng.state(), uid)
    assert first["budget"][seg.SCALE_USD] == 6.0 and first["review_terms"][seg.SCALE_USD] == 3.0
    assert eng.run("approve", session="s", by="user")["ok"]
    second = open_of(eng.state(), uid)
    assert second["arming_id"] != first["arming_id"]
    assert second["budget"] == MEDIUM and not second["review_terms"]


def test_a_user_fire_acknowledgment_or_order_agreement_renews_the_live_stage_arming(eng):
    eng.fired()
    state = eng.state()
    uid = state.stage_uid(1)
    fired = open_of(state, uid)
    eng.run("fire_acknowledge", session="s", by="user", decision="continue")
    renewed = open_of(eng.state(), uid)
    assert renewed["arming_id"] != fired["arming_id"]
    cli_common_renewed = renew(eng, seg.ACT_ORDER_AGREE)
    assert cli_common_renewed["arming_id"] != renewed["arming_id"]
    assert cli_common_renewed["segment"] == seg.stage_segment(uid)


def test_renewed_unfolded_parent_arming_opens_at_the_unfold_budget(eng):
    eng.executing(plan_text(stage1=("lazy = true",)))
    parent = eng.unfolded()
    held = open_of(eng.state(), parent)
    renewed = renew(eng, seg.ACT_FIRE_ACK_BY_USER, arming=held)
    assert renewed["budget"] == FALLBACK and renewed["budget_source"] == seg.SOURCE_FALLBACK


def test_user_rearm_of_unfolded_parent_keeps_unfold_budget(eng):
    eng.executing()
    parent = eng.unfolded()
    held = open_of(eng.state(), parent)
    renewed = renew(eng, seg.ACT_FIRE_ACK_BY_USER, arming=held)
    assert renewed["budget"] == {seg.SCALE_USD: 3.0, seg.SCALE_MINUTES: 15.4}
    assert renewed["budget_source"] == seg.SOURCE_FALLBACK_CAPPED


def test_renewal_for_a_stage_without_an_approved_version_uses_the_declared_tier(eng):
    eng.executing(plan_text(stage1=('cost_tier = "large"',)))
    state = eng.state()
    state.effort_segments["approved_tiers"] = {}
    eng.store.save(state)
    renewed = renew(eng, seg.ACT_FIRE_ACK_BY_USER)
    assert renewed["budget"] == LARGE and renewed["budget_source"] == seg.SOURCE_DECLARED


def test_review_term_is_derived_from_the_plan_at_the_review_tier(eng):
    eng.executing(plan_text(stage1=('cost_tier = "large"',)))
    eng.critiqued()
    eng.replanned(plan_text(stage1=('cost_tier = "large"', METHOD)), review=False)
    arming = open_of(eng.state(), eng.state().stage_uid(1))
    assert arming["review_terms"] == effort.stage_budget(effort._REVIEW_TIER, Thresholds())
    assert arming["budget"] == {scale: LARGE[scale] + MEDIUM[scale] for scale in seg.SCALES}


def test_replan_priced_at_exactly_its_review_term_never_fires_the_stage(eng):
    eng.executing()
    eng.book(usd=MULTIPLE_OF_MEDIUM - 1.0)
    eng.critiqued()
    plan = eng.replanned(review=False)
    eng.book(usd=3.0)
    eng.review(plan)
    assert gates.open_fire_record(eng.state()) is None


def test_review_cost_beyond_its_term_counts_toward_the_stages_fire(eng):
    eng.executing()
    eng.critiqued()
    plan = eng.replanned(review=False)
    eng.book(usd=2 * MULTIPLE_OF_MEDIUM)
    eng.review(plan)
    assert gates.open_fire_record(eng.state()) is not None


def test_unfold_keeps_the_parents_arming_and_actual_under_the_fallback_budget(eng):
    eng.executing(plan_text(stage1=("lazy = true",)))
    held = eng.book(usd=2.0)
    parent = eng.unfolded()
    arming = open_of(eng.state(), parent)
    assert arming["arming_id"] == held["arming_id"]
    assert seg.arming_actual(arming)[seg.SCALE_USD] == 2.0
    assert arming["budget"] == FALLBACK


def test_unfold_replaces_tier_with_fallback_keeps_actual(eng):
    eng.executing(plan_text(stage1=("lazy = true", 'cost_tier = "large"')))
    eng.book(usd=2.0)
    parent = eng.unfolded()
    arming = open_of(eng.state(), parent)
    assert arming["budget"] == FALLBACK and arming["declared_tier"] is None
    assert arming["budget_source"] == seg.SOURCE_FALLBACK
    assert seg.arming_actual(arming)[seg.SCALE_USD] == 2.0


def test_a_second_unfold_of_the_same_parent_replaces_nothing(eng):
    eng.executing(plan_text(stage1=("lazy = true",)))
    state = eng.state()
    cli_common._unfold_parent_budget(state, 1)
    uid = state.stage_uid(1)
    first = dict(state.effort_segments["unfolds"][uid])
    arming = open_of(state, uid)
    arming["budget"] = {seg.SCALE_USD: 1.0, seg.SCALE_MINUTES: 1.0}
    cli_common._unfold_parent_budget(state, 1)
    assert state.effort_segments["unfolds"][uid] == first
    assert arming["budget"] == {seg.SCALE_USD: 1.0, seg.SCALE_MINUTES: 1.0}


def test_unfold_unmarked_parent_caps_budget_at_fallback(eng):
    eng.executing(plan_text(stage1=('cost_tier = "large"',)))
    parent = eng.unfolded()
    arming = open_of(eng.state(), parent)
    assert arming["budget"] == FALLBACK and arming["budget_source"] == seg.SOURCE_FALLBACK_CAPPED


def test_unfold_agent_marked_lazy_caps_budget_at_fallback(eng):
    eng.executing(plan_text(stage1=('cost_tier = "small"',)))
    eng.critiqued()
    eng.replanned(with_added_stage(stage1=('cost_tier = "small"', "lazy = true")))
    approved = eng.run("approve", session="s", by="agent")
    assert approved["ok"], approved
    state = eng.state()
    uid = state.stage_uid(1)
    assert not state.effort_segments["lazy_marks"][uid]
    cli_common._unfold_parent_budget(state, 1)
    assert state.effort_segments["unfolds"][uid] == {
        "budget": {seg.SCALE_USD: 1.0, seg.SCALE_MINUTES: 10.0}, "source": seg.SOURCE_FALLBACK_CAPPED}


def test_unfold_past_new_fire_point_fires_at_next_judging_act(eng):
    eng.executing(plan_text(stage1=("lazy = true",)))
    eng.book(usd=31.0)
    parent = eng.unfolded(expect_submit_ok=False)
    assert gates.open_fire_record(eng.state())["arming_id"] == open_of(eng.state(), parent)["arming_id"]


def test_unfold_past_new_fire_point_approve_by_user_writes_retier_no_fire(eng):
    eng.executing(plan_text(stage1=("lazy = true",)))
    parent = eng.unfolded()
    eng.book(usd=31.0, uid=parent)
    assert eng.run("approve", session="s", by="user")["ok"]
    state = eng.state()
    assert gates.open_fire_record(state) is None
    assert [r["segment"] for r in retiers(state)] == [seg.stage_segment(parent)]


def test_order_agreement_after_first_approval_concerns_the_live_stage(eng):
    eng.executing()
    uid = eng.state().stage_uid(1)
    held = eng.book(usd=2.0)
    state = eng.state()
    cli_common._renew_order_agreement(state, eng.store, Namespace())
    eng.store.save(state)
    closed = next(a for a in armings_of(eng.state(), uid) if a["arming_id"] == held["arming_id"])
    assert closed["closed_by"] == seg.ACT_ORDER_AGREE
    assert seg.arming_actual(open_of(eng.state(), uid))[seg.SCALE_USD] == 0.0


def to_closing(eng):
    eng.executing()
    assert eng.passed(index=1)["ok"]
    eng.run("next_stage", session="s")
    assert eng.passed(index=2)["ok"]
    return seg.open_arming_of(eng.state().effort_segments, seg.CLOSING)


def test_order_agreement_after_the_last_settlement_opens_nothing(eng):
    closing = to_closing(eng)
    assert closing is not None
    state = eng.state()
    count = len(state.effort_segments["armings"])
    cli_common._renew_order_agreement(state, eng.store, Namespace())
    assert len(state.effort_segments["armings"]) == count
    assert seg.open_arming_of(state.effort_segments, seg.CLOSING)["arming_id"] == closing["arming_id"]


def test_overspend_found_at_a_user_approval_is_a_retier_record_not_a_fire(eng):
    eng.executing()
    uid = eng.state().stage_uid(1)
    eng.critiqued()
    eng.replanned()
    eng.book(usd=40.0)
    assert eng.run("approve", session="s", by="user")["ok"]
    state = eng.state()
    assert gates.open_fire_record(state) is None
    assert [r["segment"] for r in retiers(state)] == [seg.stage_segment(uid)]


def test_user_approval_at_the_multiple_writes_a_retier_record_on_the_live_segment(eng):
    eng.executing()
    uid = eng.state().stage_uid(1)
    eng.critiqued()
    eng.replanned()
    eng.book(usd=40.0)
    assert eng.run("approve", session="s", by="user")["ok"]
    record = retiers(eng.state())[-1]
    assert record["segment"] == seg.stage_segment(uid)


def test_user_approval_below_the_multiple_writes_no_retier_record(eng):
    eng.executing()
    eng.critiqued()
    eng.replanned()
    eng.book(usd=1.0)
    assert eng.run("approve", session="s", by="user")["ok"]
    assert retiers(eng.state()) == []


def test_open_fire_closes_as_acknowledged_at_a_user_approval(eng):
    eng.executing()
    uid = eng.state().stage_uid(1)
    eng.critiqued()
    plan = eng.replanned(review=False)
    fired = eng.book(usd=40.0)
    eng.review(plan)
    assert gates.open_fire_record(eng.state()) is not None
    assert eng.run("approve", session="s", by="user")["ok"]
    state = eng.state()
    closed = next(a for a in armings_of(state, uid) if a["arming_id"] == fired["arming_id"])
    assert closed["closed_at"] is not None and closed["fire"]["ack"]["by"] == "user"
    assert gates.open_fire_record(state) is None


def test_any_user_approval_closing_the_fired_arming_acknowledges_the_fire_and_its_ask_shows_it(eng):
    eng.executing()
    eng.critiqued()
    plan = eng.replanned(review=False)
    fired = eng.book(usd=40.0)
    eng.review(plan)
    figures = cli_common._closing_arming_figures(eng.state())
    assert figures["open_fire"]["arming_id"] == fired["arming_id"]
    assert "open fire" in figures["text"]
    assert eng.run("approve", session="s", by="user")["ok"]
    assert gates.open_fire_record(eng.state()) is None


def test_arming_opened_by_a_user_approval_is_judged_from_its_own_zero(eng):
    eng.executing()
    uid = eng.state().stage_uid(1)
    eng.critiqued()
    plan = eng.replanned(review=False)
    eng.book(usd=40.0)
    eng.review(plan)
    assert eng.run("approve", session="s", by="user")["ok"]
    assert seg.arming_actual(open_of(eng.state(), uid))[seg.SCALE_USD] == 0.0
    eng.book(usd=MULTIPLE_OF_MEDIUM - 1.0, uid=uid)
    eng.run("dispatch", session="s", dry_run=True)
    assert gates.open_fire_record(eng.state()) is None
    eng.book(usd=1.0, uid=uid)
    eng.run("dispatch", session="s", dry_run=True)
    assert gates.open_fire_record(eng.state()) is not None


def test_each_listed_user_approval_is_a_deciding_act(eng):
    eng.executing(sid="approve")
    uid = eng.state("approve").stage_uid(1)
    eng.critiqued(sid="approve")
    eng.replanned(sid="approve")
    assert eng.run("approve", session="approve", by="user")["ok"]
    assert armings_of(eng.state("approve"), uid)[0]["closed_by"] == seg.ACT_APPROVE_BY_USER

    eng.executing(sid="agree")
    uid = eng.state("agree").stage_uid(1)
    renew(eng, seg.ACT_ORDER_AGREE, sid="agree")
    assert armings_of(eng.state("agree"), uid)[0]["closed_by"] == seg.ACT_ORDER_AGREE

    eng.fired(sid="ack")
    uid = eng.state("ack").stage_uid(1)
    eng.run("fire_acknowledge", session="ack", by="user", decision="continue")
    assert armings_of(eng.state("ack"), uid)[0]["closed_by"] == seg.ACT_FIRE_ACK_BY_USER


def test_replans_scale_acknowledgment_opens_closes_and_switches_nothing(eng):
    eng.executing()
    state = eng.state()
    state.history.extend({"event": "replan"} for _ in range(int(Thresholds().effort_replan_absolute())))
    eng.store.save(state)
    eng.run("record_result", session="s", status="failed", actual="bad")
    state = eng.state()
    assert any(f.get("scale") == "replans" for f in state.effort_fires)
    before = [(a["arming_id"], a["closed_at"]) for a in state.effort_segments["armings"]]
    eng.run("fire_acknowledge", session="s", by="user", decision="continue")
    after = [(a["arming_id"], a["closed_at"]) for a in eng.state().effort_segments["armings"]]
    assert after == before


def test_directive_data_carries_closing_arming_figures_with_or_without_a_fire(eng):
    plan = eng.plan_ready()
    eng.review(plan)
    quiet = eng.present(plan, "full")
    assert "closing_arming" in quiet["data"], quiet
    assert quiet["data"]["closing_arming"]["open_fire"] is None
    eng.run("approve", session="s", by="user")
    eng.run("partition", session="s")
    eng.run("next_stage", session="s")
    eng.book(usd=30.0)
    failing = eng.run("record_result", session="s", status="failed", actual="bad")
    figures = failing["data"]["closing_arming"]
    assert figures["open_fire"] is not None and figures["actual"][seg.SCALE_USD] == 30.0


def test_fire_raised_in_a_replan_cycle_stays_open_until_the_replans_approval(eng):
    eng.executing()
    eng.critiqued()
    plan = eng.replanned(review=False)
    eng.book(usd=40.0)
    eng.review(plan)
    assert gates.open_fire_record(eng.state()) is not None
    assert eng.run("approve", session="s", by="agent")["ok"] is False
    assert gates.open_fire_record(eng.state()) is not None
    assert eng.run("approve", session="s", by="user")["ok"]
    assert gates.open_fire_record(eng.state()) is None


def test_user_approved_skip_of_an_overspending_stage_settles_with_a_retier_record(eng):
    optional = ("optional = true", 'backlog_issue = "core#1"')
    eng.executing(plan_text(stage2=optional))
    assert eng.passed(index=1)["ok"]
    eng.run("next_stage", session="s")
    uid = eng.state().stage_uid(2)
    eng.critiqued()
    eng.replanned(with_added_stage().replace(
        'output_artifacts = ["tests/test_mod.py"]', "\n".join(('output_artifacts = ["tests/test_mod.py"]', *optional))
    ).replace("depends_on = [2]", "depends_on = [1]"))
    eng.book(usd=40.0, uid=uid)
    approved = eng.run("approve", session="s", by="user", skip_optional=2)
    assert approved["ok"], approved
    state = eng.state()
    assert open_of(state, uid) is None
    assert [r["segment"] for r in retiers(state)] == [seg.stage_segment(uid)]


def test_user_fire_acknowledgment_opens_a_fresh_arming_from_the_fire_point(eng):
    eng.fired()
    state = eng.state()
    uid = state.stage_uid(1)
    fired = open_of(state, uid)
    eng.run("fire_acknowledge", session="s", by="user", decision="continue")
    state = eng.state()
    renewed = open_of(state, uid)
    closed = armings_of(state, uid)[0]
    assert closed["arming_id"] == fired["arming_id"] and closed["fire"]["ack"]["by"] == "user"
    assert renewed["budget"] == MEDIUM and seg.arming_actual(renewed)[seg.SCALE_USD] == 0.0
    assert gates.open_fire_record(state) is None


def test_agent_acknowledgment_leaves_the_arming_open(eng):
    eng.fired()
    state = eng.state()
    uid = state.stage_uid(1)
    fired = open_of(state, uid)
    eng.run("fire_acknowledge", session="s", by="agent", decision="continue")
    state = eng.state()
    assert [a["arming_id"] for a in armings_of(state, uid)] == [fired["arming_id"]]
    assert open_of(state, uid)["arming_id"] == fired["arming_id"]
    assert gates.open_fire_record(state) is not None


def test_self_approved_replan_keeps_the_arming_and_adds_only_the_review_term(eng):
    eng.executing()
    uid = eng.state().stage_uid(1)
    held = eng.book(usd=2.0)
    eng.critiqued()
    eng.replanned()
    approved = eng.run("approve", session="s", by="agent")
    assert approved["ok"], approved
    arming = open_of(eng.state(), uid)
    assert arming["arming_id"] == held["arming_id"]
    assert arming["budget"] == {scale: 2 * MEDIUM[scale] for scale in seg.SCALES}
    assert arming["review_terms"] == MEDIUM
    assert seg.arming_actual(arming)[seg.SCALE_USD] == 2.0


def test_user_approved_replan_opens_a_new_arming_at_the_approved_tier(eng):
    eng.executing()
    uid = eng.state().stage_uid(1)
    held = eng.book(usd=2.0)
    eng.critiqued()
    eng.replanned(with_added_stage(stage1=('cost_tier = "large"',)))
    assert eng.run("approve", session="s", by="user")["ok"]
    renewed = open_of(eng.state(), uid)
    assert renewed["arming_id"] != held["arming_id"]
    assert renewed["budget"] == LARGE and renewed["budget_source"] == seg.SOURCE_USER_APPROVED


def test_tier_raised_in_a_self_approved_replan_waits_for_a_user_approved_version(eng):
    eng.executing()
    uid = eng.state().stage_uid(1)
    eng.critiqued()
    eng.replanned(with_added_stage(stage1=('cost_tier = "large"',)))
    approved = eng.run("approve", session="s", by="agent")
    assert approved["ok"], approved
    state = eng.state()
    assert open_of(state, uid)["budget"] == {scale: 2 * MEDIUM[scale] for scale in seg.SCALES}
    assert state.effort_segments["approved_tiers"][uid] == "medium"
    renewed = renew(eng, seg.ACT_FIRE_ACK_BY_USER)
    assert renewed["budget"] == MEDIUM


def test_self_approved_replan_leaving_the_live_stage_unmatched_is_refused(eng):
    eng.executing()
    eng.critiqued()
    eng.replanned(without_stage_one())
    refused = eng.run("approve", session="s", by="agent")
    assert refused["ok"] is False and "only the customer" in json.dumps(refused)


def test_user_approved_drop_at_the_multiple_settles_with_a_retier_record(eng):
    eng.executing()
    uid = eng.state().stage_uid(1)
    eng.critiqued()
    eng.replanned(without_stage_one())
    eng.book(usd=40.0, uid=uid)
    assert eng.run("approve", session="s", by="user")["ok"]
    state = eng.state()
    assert open_of(state, uid) is None
    assert [r["segment"] for r in retiers(state)] == [seg.stage_segment(uid)]


def test_user_approved_drop_below_the_multiple_writes_no_retier_record(eng):
    eng.executing()
    uid = eng.state().stage_uid(1)
    eng.critiqued()
    eng.replanned(without_stage_one())
    eng.book(usd=1.0, uid=uid)
    assert eng.run("approve", session="s", by="user")["ok"]
    state = eng.state()
    assert open_of(state, uid) is None and retiers(state) == []
    assert seg.arming_actual(armings_of(state, uid)[0])[seg.SCALE_USD] == 1.0


def test_user_approved_drop_of_last_stage_opens_closing_arming(eng):
    eng.executing()
    assert eng.passed(index=1)["ok"]
    eng.run("next_stage", session="s")
    eng.critiqued()
    eng.replanned(without_stage_two())
    assert eng.run("approve", session="s", by="user")["ok"]
    assert seg.open_arming_of(eng.state().effort_segments, seg.CLOSING) is not None


@pytest.mark.parametrize("by", ["user", "agent"])
def test_subplan_approval_settles_the_unfolded_parents_planning_arming(eng, by):
    eng.executing(plan_text(stage1=("lazy = true",)))
    parent = eng.unfolded(approve=by)
    state = eng.state()
    assert open_of(state, parent) is None
    assert armings_of(state, parent)[0]["closed_at"] is not None


def test_subplan_agent_approve_at_fire_point_fires_and_refuses(eng):
    eng.executing(plan_text(stage1=("lazy = true",)))
    parent = eng.unfolded()
    eng.book(usd=31.0, uid=parent)
    refused = eng.run("approve", session="s", by="agent")
    assert refused["ok"] is False
    assert gates.open_fire_record(eng.state())["arming_id"] == open_of(eng.state(), parent)["arming_id"]


def test_subplan_agent_approve_books_late_rows_before_judging(eng):
    eng.executing(plan_text(stage1=("lazy = true",)))
    parent = eng.unfolded()
    eng.row(31.0)
    assert eng.run("approve", session="s", by="agent")["ok"] is False
    arming = open_of(eng.state(), parent)
    assert seg.arming_actual(arming)[seg.SCALE_USD] == 31.0
    assert gates.open_fire_record(eng.state())["arming_id"] == arming["arming_id"]


def test_subplan_user_approval_opens_no_further_arming_of_the_parent(eng):
    eng.executing(plan_text(stage1=("lazy = true",)))
    parent = eng.unfolded(approve="user")
    assert len(armings_of(eng.state(), parent)) == 1


def test_parent_settles_when_its_last_substage_settles_and_is_judged_no_later(eng):
    eng.executing()
    parent = eng.unfolded(approve="user")
    assert open_of(eng.state(), parent) is not None
    eng.run("partition", session="s")
    eng.run("next_stage", session="s")
    assert eng.passed(index=1)["ok"]
    assert open_of(eng.state(), parent) is not None
    eng.run("next_stage", session="s")
    eng.book(usd=31.0, uid=parent)
    assert eng.passed(index=2)["ok"]
    state = eng.state()
    assert open_of(state, parent) is None
    assert [r["segment"] for r in retiers(state)] == [seg.stage_segment(parent)]


def test_two_level_unfold_settles_both_parents_and_hands_on_at_the_top_plan(eng):
    eng.executing()
    top = eng.unfolded(approve="user")
    eng.run("partition", session="s")
    eng.run("next_stage", session="s")
    assert eng.passed(index=1)["ok"]
    eng.run("next_stage", session="s")
    middle = eng.state().stage_uid(2)
    assert eng.run("push_subplan", session="s", plan=eng.write(plan_text()), task="grandchild",
                   originating_stage=2)["ok"]
    eng.run("classify", session="s", architectural=True, changed_lines=200, wall_clock_min=60)
    eng.run("plan", session="s")
    assert eng.run("submit_plan", session="s", plan=eng.write(plan_text()))["ok"]
    assert eng.run("approve", session="s", by="user")["ok"]
    eng.run("partition", session="s")
    eng.run("next_stage", session="s")
    assert eng.passed(index=1)["ok"]
    eng.run("next_stage", session="s")
    assert open_of(eng.state(), top) is not None and open_of(eng.state(), middle) is not None
    assert eng.passed(index=2)["ok"]
    state = eng.state()
    assert open_of(state, middle) is None and open_of(state, top) is None
    following = open_of(state, state.stage_uids[stage_uid_key(frame_path([]), 2)])
    assert following is not None and following["opened_by"] == seg.ACT_PASSING_RECORD_RESULT


def test_row_ingested_after_a_user_approval_belongs_to_the_closed_arming(eng):
    eng.executing()
    uid = eng.state().stage_uid(1)
    eng.critiqued()
    eng.replanned()
    assert eng.run("approve", session="s", by="user")["ok"]
    state = eng.state()
    closed = armings_of(state, uid)[0]
    eng.row(1.5, ts=closed["closed_at"] - 0.01)
    cli_common._refresh_spend(state, eng.store, cost.read_rows(cost.COST_LOG), state.plan_path)
    assert seg.arming_actual(armings_of(state, uid)[0])[seg.SCALE_USD] == 1.5
    assert seg.arming_actual(open_of(state, uid))[seg.SCALE_USD] == 0.0


def test_late_rows_crossing_multiple_booked_at_approve_by_user(eng):
    eng.executing()
    uid = eng.state().stage_uid(1)
    eng.critiqued()
    eng.replanned()
    eng.row(40.0)
    assert eng.run("approve", session="s", by="user")["ok"]
    state = eng.state()
    assert seg.arming_actual(armings_of(state, uid)[0])[seg.SCALE_USD] == 40.0
    assert [r["segment"] for r in retiers(state)] == [seg.stage_segment(uid)]
    assert gates.open_fire_record(state) is None


def test_late_rows_booked_at_fire_acknowledge_by_user(eng):
    eng.fired()
    uid = eng.state().stage_uid(1)
    eng.row(5.0)
    eng.run("fire_acknowledge", session="s", by="user", decision="continue")
    assert seg.arming_actual(armings_of(eng.state(), uid)[0])[seg.SCALE_USD] == 35.0


def test_present_plan_data_shows_closing_arming_figures_before_approve(eng):
    def shows_figures(directive):
        assert "closing_arming" in directive["data"], directive
        figures = directive["data"]["closing_arming"]
        assert figures["text"] in json.dumps(directive["data"]["next_steps"], ensure_ascii=False)
        return figures

    plain = eng.plan_ready(sid="plain")
    eng.review(plain, sid="plain")
    fallback = {seg.SCALE_USD: 6.0, seg.SCALE_MINUTES: 15.4}
    assert shows_figures(eng.present(plain, "essence", sid="plain"))["budget"] == fallback
    assert shows_figures(eng.present(plain, "full", sid="plain"))["budget"] == fallback

    eng.executing()
    eng.book(usd=2.0)
    eng.critiqued()
    plan = eng.replanned()
    figures = shows_figures(eng.present(plan, "replan_diff"))
    assert "2.00" in figures["text"] and figures["actual"][seg.SCALE_USD] == 2.0


def test_approve_by_agent_refused_under_a_fire_shows_closing_arming_figures(eng):
    eng.executing()
    eng.critiqued()
    candidate = eng.replanned(review=False)
    eng.book(usd=40.0)
    eng.review(candidate)
    refused = eng.run("approve", session="s", by="agent")
    assert refused["data"]["closing_arming"]["open_fire"] is not None
    assert refused["data"]["closing_arming"]["text"] in json.dumps(refused["data"]["next_steps"], ensure_ascii=False)


def test_each_config_key_moves_the_fallback(tmp_path, monkeypatch):
    table = tmp_path / "config.md"
    table.write_text(
        "| `review-cycle-active-minutes` | `50` | x |\n| `budget-medium-usd` | `4.00` | x |\n"
        "| `effort-divergence-multiple` | `10` | x |\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_MD", table)
    assert cli_common._planning_fallback(Thresholds()) == {seg.SCALE_USD: 8.0, seg.SCALE_MINUTES: 5.0}


def test_carried_unmatched_stage_settles_at_user_approval_with_a_retier_record(eng, monkeypatch):
    eng.executing()
    state = eng.state()
    entry = {"budget": dict(MEDIUM), "declared_tier": "medium", "title": "gone",
             "tier_ladder": None, "actual": {seg.SCALE_USD: 20.0, seg.SCALE_MINUTES: 0.0}}
    monkeypatch.setattr(cli_common, "_carried_entries", lambda _state, _key: {"stage:gone": entry})
    cli_common._settle_carried_drops(state, "key", ["stage:gone"], time.time())
    arming = armings_of(state, "gone")[0]
    assert arming["closed_at"] is not None and arming["budget_source"] == seg.SOURCE_CARRIED
    assert seg.arming_actual(arming)[seg.SCALE_USD] == 20.0
    assert [r["segment"] for r in retiers(state)] == ["stage:gone"]


def test_lazy_mark_is_loaded_and_a_non_boolean_is_refused(eng, tmp_path):
    good = load_plan(eng.write(plan_text(stage1=("lazy = true",))), strict=False)
    assert good.stages[0].lazy is True and good.stages[1].lazy is False
    with pytest.raises(PlanError):
        load_plan(eng.write(plan_text(stage1=('lazy = "yes"',))), strict=False)


STAGE_DEFS = {
    1: dict(title="Scaffold module", result="module file exists and imports cleanly",
            done="python -c 'import mod' exits 0", deps=[], outputs=["mod.py"], extra=[]),
    2: dict(title="Add tests", result="pytest green for the new module",
            done="pytest tests/test_mod.py green", deps=[1], outputs=["tests/test_mod.py"], extra=[]),
}


def stage_block(index, like=None, **over):
    """One `[[stage]]` of a continuation scenario: stage `like` (default `index`) of the two-stage
    fixture, or a bare new stage, with `over` applied."""
    base = dict(STAGE_DEFS.get(like or index, dict(
        title=f"Stage {index}", result=f"stage {index} result", done=f"stage {index} done",
        deps=[], outputs=[], extra=[])))
    base.update(over)
    lines = ["", "[[stage]]", f"index = {index}", f'title = "{base["title"]}"',
             'executor = "spawn:developer"', f'expected_result_image = "{base["result"]}"',
             'criterion_type = "measurable"', f'done_criterion = "{base["done"]}"',
             f"depends_on = {base['deps']}", f"output_artifacts = {json.dumps(base['outputs'])}",
             *base["extra"], ""]
    return "\n".join(lines)


def build_plan(*blocks):
    return plan_text().partition("\n[[stage]]")[0] + "\n" + "".join(blocks)


ADDED = stage_block(3, title="Add docs", deps=[2])
REWORDED = stage_block(1, title="Scaffold the module")
TWO = (stage_block(1), stage_block(2))


def live_replan(eng, text, base=None, settled=0, booked=2.0, reopen=None):
    """A failed live stage (the one after `settled` passed ones) worked through to its replan of
    `text`: the stage's identity and arming, and the replan's directive."""
    eng.executing(base)
    for index in range(1, settled + 1):
        assert eng.passed(index=index)["ok"]
        eng.run("next_stage", session="s")
    uid = eng.state().stage_uid(settled + 1)
    eng.book(usd=booked, uid=uid)
    held = open_of(eng.state(), uid)
    eng.critiqued()
    plan = eng.write(text)
    directive = eng.run("replan", session="s", plan=plan, reopen=reopen)
    assert directive["ok"], directive
    return plan, uid, held, directive


def assert_continues(eng, plan, uid, held, index):
    """The replan is accepted for self-approval and the live stage keeps identity and arming whole."""
    assert eng.state().node == "PLAN_READY"
    eng.review(plan)
    approved = eng.run("approve", session="s", by="agent")
    assert approved["ok"], approved
    state = eng.state()
    assert state.stage_uid(index) == uid
    arming = open_of(state, uid)
    assert arming["arming_id"] == held["arming_id"]
    assert seg.arming_actual(arming)[seg.SCALE_USD] == 2.0
    assert arming["budget"] == {scale: 2 * MEDIUM[scale] for scale in seg.SCALES}


def assert_refused(eng, plan, uid, held, directive):
    """The replan routes to PLAN_READY for the user and `approve --by agent` is refused with the
    saved state unchanged."""
    if directive is not None:
        assert directive["action"] == "await_user_approval" and directive["data"]["continuation_breaches"]
    assert eng.state().node == "PLAN_READY"
    eng.review(plan)
    before = eng.state().to_dict()
    refused = eng.run("approve", session="s", by="agent")
    assert refused["ok"] is False and refused["action"] == "await_user_approval", refused
    assert "no clean continuation" in refused["detail"]
    assert eng.state().to_dict() == before
    assert open_of(eng.state(), uid)["arming_id"] == held["arming_id"]


def test_continuation_by_own_outputs_after_rewording(eng):
    plan, uid, held, _ = live_replan(eng, build_plan(REWORDED, stage_block(2), ADDED))
    assert_continues(eng, plan, uid, held, 1)


def test_continuation_after_revised_supplier(eng):
    plan, uid, held, _ = live_replan(eng, build_plan(REWORDED, stage_block(2), ADDED), settled=1, reopen=[1])
    assert_continues(eng, plan, uid, held, 2)


def test_continuation_after_inserted_stage(eng):
    prepare = stage_block(1, title="Prepare", result="prepared", done="prepared", outputs=[])
    plan, uid, held, _ = live_replan(eng, build_plan(
        prepare, stage_block(2, like=1, deps=[1]), stage_block(3, like=2, deps=[2])))
    assert_continues(eng, plan, uid, held, 2)


def test_continuation_when_another_stage_changes(eng):
    plan, uid, held, _ = live_replan(eng, build_plan(
        stage_block(1), stage_block(2, title="Add the tests"), ADDED))
    assert_continues(eng, plan, uid, held, 1)


def test_continuation_after_shared_output_rewording(eng):
    shared = build_plan(stage_block(1, outputs=["mod.py", "tests/test_mod.py"]), stage_block(2))
    reworded = build_plan(
        stage_block(1, title="Scaffold the module", outputs=["mod.py", "tests/test_mod.py"]),
        stage_block(2, outputs=["./tests/test_mod.py"]), ADDED)
    plan, uid, held, _ = live_replan(eng, reworded, base=shared)
    assert_continues(eng, plan, uid, held, 1)


def test_continuation_by_definition_rule_without_own_outputs(eng):
    base = build_plan(stage_block(1, outputs=[]), stage_block(2))
    prepare = stage_block(1, title="Prepare", result="prepared", done="prepared", outputs=[])
    plan, uid, held, _ = live_replan(eng, build_plan(
        prepare, stage_block(2, like=1, deps=[1], outputs=[]), stage_block(3, like=2, deps=[2])), base=base)
    assert_continues(eng, plan, uid, held, 2)


def test_split_claiming_own_outputs_refused(eng):
    text = build_plan(REWORDED, stage_block(2), stage_block(3, title="Add docs", deps=[2], outputs=["mod.py"]))
    plan, uid, held, directive = live_replan(eng, text)
    assert_refused(eng, plan, uid, held, directive)


def test_claimant_by_respelled_path_refused(eng):
    text = build_plan(REWORDED, stage_block(2), stage_block(3, title="Add docs", deps=[2], outputs=["./mod.py"]))
    plan, uid, held, directive = live_replan(eng, text)
    assert_refused(eng, plan, uid, held, directive)


def test_claimant_by_path_prefix_refused(eng):
    text = build_plan(REWORDED, stage_block(2), stage_block(3, title="Add docs", deps=[2], outputs=["mod.py/inner"]))
    plan, uid, held, directive = live_replan(eng, text)
    assert_refused(eng, plan, uid, held, directive)


def test_claimant_beside_matched_live_stage_refused(eng):
    text = build_plan(stage_block(1), stage_block(2), stage_block(3, title="Add docs", deps=[2], outputs=["mod.py"]))
    plan, uid, held, directive = live_replan(eng, text)
    assert_refused(eng, plan, uid, held, directive)


def test_carry_matched_sole_claimant_refused(eng):
    base = build_plan(stage_block(1, outputs=["pkg/a.py"]), stage_block(2, outputs=["pkg/b.py"]))
    text = build_plan(stage_block(1, outputs=["pkg"]), stage_block(2, outputs=["pkg/b.py"]))
    plan, uid, held, directive = live_replan(eng, text, base=base)
    assert_refused(eng, plan, uid, held, directive)


def test_rename_of_own_output_refused(eng):
    plan, uid, held, directive = live_replan(eng, build_plan(stage_block(1, outputs=["mod2.py"]), stage_block(2), ADDED))
    assert_refused(eng, plan, uid, held, directive)


def test_removal_of_live_stage_refused(eng):
    plan, uid, held, directive = live_replan(eng, without_stage_two(), settled=1)
    assert_refused(eng, plan, uid, held, directive)


def test_narrowing_of_own_outputs_refused(eng):
    base = build_plan(stage_block(1, outputs=["mod.py", "mod_extra.py"]), stage_block(2))
    plan, uid, held, directive = live_replan(eng, build_plan(stage_block(1), stage_block(2), ADDED), base=base)
    assert_refused(eng, plan, uid, held, directive)


def test_widening_onto_other_stage_output_refused(eng):
    text = build_plan(stage_block(1, outputs=["."]), stage_block(2), ADDED)
    plan, uid, held, directive = live_replan(eng, text)
    assert_refused(eng, plan, uid, held, directive)


def test_drop_with_rewired_consumer_refused(eng):
    plan, uid, held, directive = live_replan(eng, without_stage_one())
    assert_refused(eng, plan, uid, held, directive)


TWO_OUTPUTS = build_plan(stage_block(1, outputs=["mod.py", "mod_extra.py"]), stage_block(2))


def diff_kind(eng, base, text):
    return diff_plans(load_plan(eng.write(base), strict=False), load_plan(eng.write(text), strict=False))


def test_no_change_class_narrowing_routes_to_plan_ready(eng):
    text = build_plan(*TWO)
    assert diff_kind(eng, TWO_OUTPUTS, text) in ("no_change", "refinement")
    plan, uid, held, directive = live_replan(eng, text, base=TWO_OUTPUTS)
    assert_refused(eng, plan, uid, held, directive)


def test_no_change_class_claim_routes_to_plan_ready(eng, monkeypatch):
    monkeypatch.setattr(cli_replan, "_replan_kind", lambda *_: "no_change")
    text = build_plan(stage_block(1), stage_block(2, outputs=["tests/test_mod.py", "mod.py"]))
    assert diff_kind(eng, build_plan(*TWO), text) == "substantive"
    plan, uid, held, directive = live_replan(eng, text)
    assert_refused(eng, plan, uid, held, directive)


def test_unchanged_resubmission_refused_again(eng):
    text = build_plan(REWORDED, stage_block(2), stage_block(3, title="Add docs", deps=[2], outputs=["mod.py"]))
    plan, uid, held, directive = live_replan(eng, text)
    assert_refused(eng, plan, uid, held, directive)
    again = eng.run("replan", session="s", plan=plan)
    assert again["ok"], again
    assert_refused(eng, plan, uid, held, again)


def test_breach_edited_in_at_plan_ready_refused(eng):
    plan, uid, held, directive = live_replan(eng, build_plan(REWORDED, stage_block(2), ADDED))
    assert "continuation_breaches" not in directive["data"]
    breach = build_plan(REWORDED, stage_block(2), stage_block(3, title="Add docs", deps=[2], outputs=["mod.py"]))
    Path(plan).write_text(declare_venue(breach, eng.tmp), encoding="utf-8")
    assert_refused(eng, plan, uid, held, None)


def test_continuation_refusal_of_a_self_approval_comes_after_judging_the_live_stage(eng):
    plan, uid, held, _ = live_replan(eng, build_plan(REWORDED, stage_block(2), ADDED))
    breach = build_plan(REWORDED, stage_block(2), stage_block(3, title="Add docs", deps=[2], outputs=["mod.py"]))
    Path(plan).write_text(declare_venue(breach, eng.tmp), encoding="utf-8")
    eng.review(plan)
    eng.book(usd=1000.0, uid=uid)
    refused = eng.run("approve", session="s", by="agent")
    assert refused["ok"] is False
    state = eng.state()
    assert state.node == "PLAN_READY"
    [fire] = [f for f in state.effort_fires if f.get("arming_id") == held["arming_id"]]
    assert seg.SCALE_USD in fire["scales"] and gates.open_fire_record(state) is not None


ADDED_LAST = stage_block(4, title="Add docs", deps=[3])
PREPARE_AHEAD = build_plan(
    stage_block(1, title="Prepare", result="prepared", done="prepared", outputs=[]),
    stage_block(2, like=1, deps=[1]), stage_block(3, like=2, deps=[2]))


def parked(eng, text=PREPARE_AHEAD):
    """A replan put a new stage ahead of the live one and `next_stage` selected it: the live
    stage's identity, its arming and the arming of the stage that now holds the work."""
    plan, uid, held, _ = live_replan(eng, text)
    assert_continues(eng, plan, uid, held, 2)
    eng.run("partition", session="s")
    selected = eng.run("next_stage", session="s")
    state = eng.state()
    ahead = open_of(state, state.stage_uid(1))
    live = open_of(state, uid)
    assert live["arming_id"] == held["arming_id"] and live["spans"][-1]["end_act"] == "parked", (
        selected, [(s.index, s.outcome.status) for s in state.stages], live, ahead)
    return uid, live, ahead


def settle_ahead_and_resume(eng):
    assert eng.passed(index=1)["ok"]
    eng.run("next_stage", session="s")


def test_parked_stage_resumes_with_no_new_arming(eng):
    uid, live, ahead = parked(eng)
    settle_ahead_and_resume(eng)
    state = eng.state()
    assert [a["arming_id"] for a in armings_of(state, uid)] == [live["arming_id"]]
    resumed = open_of(state, uid)
    assert len(resumed["spans"]) == 2 and resumed["spans"][1]["end"] is None
    assert resumed["spans"][1]["start"] == seg.find_arming(state.effort_segments, ahead["arming_id"])["closed_at"]


def test_parked_arming_books_nothing_and_is_not_judged(eng):
    uid, live, ahead = parked(eng)
    eng.row(31.0, ts=time.time() + 1.0)
    state = eng.state()
    cli_common._refresh_spend(state, eng.store, cost.read_rows(cost.COST_LOG), state.plan_path)
    assert seg.arming_actual(open_of(state, uid))[seg.SCALE_USD] == 2.0
    assert seg.arming_actual(open_of(state, state.stage_uid(1)))[seg.SCALE_USD] == 31.0
    eng.run("dispatch", session="s")
    fire = gates.open_fire_record(eng.state())
    assert fire is not None and fire["arming_id"] == ahead["arming_id"]
    assert not [f for f in eng.state().effort_fires if f.get("arming_id") == live["arming_id"]]


def test_user_approval_while_parked_rearms_each_live_stage_parked_stays_parked(eng):
    uid, live, ahead = parked(eng)
    eng.book(usd=30.0, uid=eng.state().stage_uid(1))
    eng.run("dispatch", session="s")
    assert gates.open_fire_record(eng.state())["arming_id"] == ahead["arming_id"]
    eng.run("fire_acknowledge", session="s", by="user", decision="continue")
    state = eng.state()
    renewed = open_of(state, state.stage_uid(1))
    assert renewed["arming_id"] != ahead["arming_id"]
    still = open_of(state, uid)
    assert still["arming_id"] == live["arming_id"] and still["spans"][-1]["end_act"] == "parked"
    assert still["closed_at"] is None and still["booked"] == live["booked"]


def test_replan_while_parked_checks_each_live_stage(eng):
    uid, live, ahead = parked(eng)
    ahead_uid = eng.state().stage_uid(1)
    eng.critiqued()
    text = build_plan(
        stage_block(1, title="Prepare", result="prepared", done="prepared", outputs=[]),
        stage_block(2, like=1, deps=[1]), stage_block(3, like=2, deps=[2]),
        stage_block(4, title="Publish", deps=[3], outputs=["mod.py"]))
    directive = eng.run("replan", session="s", plan=eng.write(text))
    assert directive["ok"] and directive["data"]["continuation_breaches"]
    assert {b["uid"] for b in directive["data"]["continuation_breaches"] if isinstance(b, dict)} <= {uid, ahead_uid}


def test_parked_plan_edit_inserting_stage_keeps_arming_with_continuation(eng):
    uid, live, ahead = parked(eng)
    ahead_uid = eng.state().stage_uid(1)
    eng.critiqued()
    prepare = stage_block(1, title="Prepare", result="prepared", done="prepared", outputs=[])
    candidate = build_plan(prepare, stage_block(2, like=1, deps=[1]), stage_block(3, like=2, deps=[2]), ADDED_LAST)
    plan = eng.write(candidate)
    assert eng.run("replan", session="s", plan=plan)["ok"] and eng.state().node == "PLAN_READY"
    inserted = build_plan(
        prepare, stage_block(2, title="Gather inputs", deps=[1]),
        stage_block(3, like=1, deps=[2]), stage_block(4, like=2, deps=[3]), ADDED_LAST.replace("index = 4", "index = 5"))
    Path(plan).write_text(declare_venue(inserted, eng.tmp), encoding="utf-8")
    eng.review(plan)
    assert eng.run("approve", session="s", by="agent")["ok"]
    state = eng.state()
    assert open_of(state, uid)["arming_id"] == live["arming_id"]
    assert [state.stage_uid(i) for i in (1, 3)] == [ahead_uid, uid]
    assert state.stage_uid(2) not in (uid, ahead_uid)


def test_a_write_time_in_a_gap_books_to_the_nearest_arming_with_a_warning(caplog):
    budget = {"usd": 1.0, "minutes": 10.0}
    ledger = {"session_floor": 100.0}
    first = seg.open_arming(ledger, "stage:a", budget, seg.SOURCE_DECLARED, 100.0, "t")
    seg.close_arming(ledger, first["arming_id"], "t", 150.0, 5.0)
    second = seg.open_arming(ledger, "stage:b", budget, seg.SOURCE_DECLARED, 160.0, "t")
    with caplog.at_level("WARNING", logger=seg.LOG.name):
        assert seg.book(ledger, 152.0, 1.0, 0.0, "row") is first
        assert seg.book(ledger, 159.0, 2.0, 0.0, "row") is second
    assert len(caplog.records) == 2 and "covered by no span" in caplog.records[0].getMessage()
    assert seg.conserve(ledger)["total"]["usd"] == 3.0
    assert seg.book({"session_floor": 100.0}, 200.0, 1.0, 0.0, "row") is None


def pushed_subplan(eng):
    """The live stage of an approved sub-plan pushed under a lazy parent stage, failed and
    diagnosed: its identity, arming and the parent's stage identity."""
    eng.executing(plan_text(stage1=("lazy = true",)))
    parent = eng.state().stage_uid(1)
    child = eng.write(build_plan(*TWO))
    assert eng.run("push_subplan", session="s", plan=child, task="child", originating_stage=1)["ok"]
    eng.run("classify", session="s", architectural=True, changed_lines=200, wall_clock_min=60)
    eng.run("plan", session="s")
    assert eng.run("submit_plan", session="s", plan=child)["ok"]
    assert eng.run("approve", session="s", by="user")["ok"]
    eng.run("partition", session="s")
    eng.run("next_stage", session="s")
    uid = eng.state().stage_uid(1)
    assert uid != parent
    eng.book(usd=2.0, uid=uid)
    held = open_of(eng.state(), uid)
    eng.critiqued()
    return parent, uid, held


def test_continuation_searched_in_pushed_subplan_frame(eng):
    parent, uid, held = pushed_subplan(eng)
    plan = eng.write(build_plan(REWORDED, stage_block(2), ADDED))
    directive = eng.run("replan", session="s", plan=plan)
    assert directive["ok"] and "continuation_breaches" not in directive["data"], directive
    assert_continues(eng, plan, uid, held, 1)
    assert eng.state().stage_uid(1) != parent


def test_continuation_breach_in_pushed_subplan_frame_is_refused(eng):
    parent, uid, held = pushed_subplan(eng)
    plan = eng.write(build_plan(REWORDED, stage_block(2), stage_block(3, title="Add docs", deps=[2], outputs=["mod.py"])))
    directive = eng.run("replan", session="s", plan=plan)
    assert directive["ok"], directive
    assert_refused(eng, plan, uid, held, directive)


@pytest.mark.parametrize("writer", ["resubmission", "in_place_edit"])
def test_reindexing_edit_at_plan_ready_keeps_identity_through_either_writer(eng, writer):
    candidate = build_plan(REWORDED, stage_block(2), ADDED)
    plan, uid, held, directive = live_replan(eng, candidate)
    ahead = build_plan(stage_block(1, title="Prepare", result="prepared", done="prepared", outputs=[]),
                       stage_block(2, like=1, title="Scaffold the module", deps=[1]),
                       stage_block(3, like=2, deps=[2]), stage_block(4, title="Add docs", deps=[3]))
    Path(plan).write_text(declare_venue(ahead, eng.tmp), encoding="utf-8")
    if writer == "resubmission":
        assert eng.run("submit_plan", session="s", plan=plan)["ok"]
    eng.review(plan)
    approved = eng.run("approve", session="s", by="agent")
    assert approved["ok"], approved
    state = eng.state()
    assert state.stage_uid(2) == uid and state.stage_uid(1) != uid
    assert open_of(state, uid)["arming_id"] == held["arming_id"]


def test_replan_with_a_live_stage_and_no_readable_snapshot_awaits_the_user(eng):
    eng.executing()
    uid = eng.state().stage_uid(1)
    eng.book(usd=2.0, uid=uid)
    held = open_of(eng.state(), uid)
    eng.critiqued()
    state = eng.state()
    state.plan_snapshot_path = None
    eng.store.save(state)
    plan = eng.write(build_plan(REWORDED, stage_block(2), ADDED))
    directive = eng.run("replan", session="s", plan=plan)
    assert directive["ok"], directive
    assert directive["action"] == "await_user_approval"
    assert any("no readable approved-plan snapshot" in b for b in directive["data"]["continuation_breaches"])
    assert_refused(eng, plan, uid, held, directive)


def test_check_changed_flag_in_replan_event_and_review_prompt(eng, monkeypatch):
    changed = build_plan(stage_block(1, done="python -c 'import mod; mod.ok()' exits 0"), stage_block(2), ADDED)
    unchanged = build_plan(REWORDED, stage_block(2), ADDED)
    plan, uid, held, directive = live_replan(eng, changed)
    assert directive["data"]["check_changed"] == [1]
    event = [e for e in eng.state().history if e["event"] == "replan"][-1]
    assert event["check_changed"] == [1]

    eng.critiqued()
    monkeypatch.setattr(gates, "plan_review_blockers", lambda *_: ["needs a review"])
    refused = eng.run("replan", session="s", plan=eng.write(changed))
    assert refused["action"] == "plan_review" and refused["data"]["check_changed"] == [1]
    assert "check_changed:" in refused["detail"]
    clean = eng.run("replan", session="s", plan=eng.write(unchanged))
    assert "check_changed" not in clean["data"] and "check_changed:" not in clean["detail"]


def test_refinement_removing_all_own_outputs_in_diagnosing_routes_to_plan_ready(eng):
    text = build_plan(stage_block(1, outputs=[]), stage_block(2))
    assert diff_kind(eng, build_plan(*TWO), text) in ("no_change", "refinement")
    plan, uid, held, directive = live_replan(eng, text)
    assert_refused(eng, plan, uid, held, directive)
