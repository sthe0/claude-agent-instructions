"""Planning as an effort segment: its arming from classify at the configuration fallback, the
planning estimate an order agreement puts in its place, the acts that judge it, and the way it
settles at the plan's first approval. The rule-to-test map is effort_per_stage_rules.toml."""
import contextlib
import io
import json
import time
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, cli_common, cli_dispatch, config, cost, effort, gates
from agentctl import effort_segments as seg
from agentctl.config import Thresholds
from agentctl.plan import load_order_draft, understanding_digest, understanding_parts
from agentctl.render import render_order_understanding_md, render_plan_grants
from agentctl.plan import load_plan
from agentctl.state import SessionState
from agentctl.store import FileStateStore
from conftest import SUBSTANTIVE_ORDER, declare_venue
from test_order_agreement import (
    SESSION, _agree, _agreed, _classify, _confirm_delivery, _marker_of, _presented, _start, _state_file, _user_answers,
    armed_env, home, ostore,
)
from test_order_phase import _LINES, _draft, _present, _review, _submit

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TWO_STAGE = (FIXTURES / "plan_two_stage.toml").read_text(encoding="utf-8")
FALLBACK = {seg.SCALE_USD: 6.0, seg.SCALE_MINUTES: 15.4}
FLOOR = {seg.SCALE_USD: 8.0, seg.SCALE_MINUTES: 60.0}
MULTIPLE = 5.0
OVERSPENT_USD = 30.0
ESTIMATE = {seg.SCALE_USD: 4.5, seg.SCALE_MINUTES: 12.0}
ESTIMATE_LINE = "planning_estimate"
EVERY_PLANNING_ACT = (
    seg.ACT_SUBMIT_PLAN, seg.ACT_PLAN_REVIEW, seg.ACT_PRESENT_PLAN, seg.ACT_APPROVE_BY_AGENT, seg.ACT_STAMP,
    seg.ACT_SPAWNED_PLANNER_ACT, seg.ACT_SPAWNED_PLANNER_RUN_END,
)


def plan_text():
    head, sep, tail = TWO_STAGE.partition("\n[[stage]]")
    return head + "\n" + SUBSTANTIVE_ORDER + sep + tail


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

    def classified(self, sid="s"):
        self.run("start", session=sid, task=f"task-{sid}", goal="g", done_criterion="dc")
        self.run("classify", session=sid, architectural=True, changed_lines=200, wall_clock_min=60)

    def planning(self, sid="s"):
        """Classified and in planning, no plan yet."""
        self.classified(sid)
        self.run("plan", session=sid)

    def plan_ready(self, sid="s"):
        self.planning(sid)
        plan = self.write()
        assert self.run("submit_plan", session=sid, plan=plan)["ok"]
        return plan

    def book(self, usd=0.0, minutes=0.0, sid="s"):
        """Book onto planning's open arming directly, as the ledger would."""
        state = self.state(sid)
        arming = planning_of(state)
        arming["booked"][seg.SCALE_USD] += usd
        arming["booked"][seg.SCALE_MINUTES] += minutes
        self.store.save(state)
        return arming

    def row(self, usd, *, spawner=None, sid="s", ts=None):
        state = self.state(sid)
        with open(self.tmp / "costs.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"plan_path": state.plan_path, "stage_index": 1, "cost_usd": usd,
                                     "ts": time.time() if ts is None else ts,
                                     "spawner_session": spawner}) + "\n")

    def child(self, child, parent="s"):
        self.run("start", session=child, task=f"task-{child}", goal="g", done_criterion="dc")
        state = self.state(child)
        state.parent_link = {"parent_session": parent}
        self.store.save(state)

    def review(self, plan, sid="s"):
        return self.run("plan_review", session=sid, verdict="pass", reviewer="thinker", target=plan)

    def present(self, plan, sid="s"):
        doc = load_plan(plan, strict=False)
        rendering = self.tmp / f"rendering-{sid}.txt"
        blocks = (render_plan_grants(doc, fmt=fmt).strip() for fmt in ("compact", "full"))
        anchors = "\n".join(f"[stage {stage.index}] {stage.title}" for stage in doc.stages)
        rendering.write_text(f"{plan}\n\n{anchors}\n\n" + "\n\n".join(blocks), encoding="utf-8")
        return self.run("present_plan", session=sid, kind="essence", rendering_file=str(rendering))


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")
    monkeypatch.setattr(cost, "COST_LOG", tmp_path / "costs.jsonl")


@pytest.fixture
def eng(tmp_path):
    return Eng(tmp_path)


def planning_of(state):
    return seg.open_arming_of(state.effort_segments, seg.PLANNING)


def planning_armings(state):
    return [a for a in state.effort_segments["armings"] if a["segment"] == seg.PLANNING]


def fires_of(state):
    return [f for f in state.effort_fires if f.get("arming_id")]


def retiers(state):
    return state.effort_segments.get("retier", [])


def renew(store, sid, act):
    state = store.load(sid)
    held = seg.current_arming(state.effort_segments)
    renewed = cli_common._renew_arming(state, store, Namespace(), held, act, "user")
    store.save(state)
    return renewed


def stamp(eng, at, sid="s"):
    assert cli_common.stamp_and_judge(sid, at, store=eng.store) is True


def v61(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("stage_uids", "effort_segments", "effort_migrated"):
        data.pop(key, None)
    data["schema_version"] = 61
    path.write_text(json.dumps(data), encoding="utf-8")


def built(usd=0.0, minutes=0.0):
    arming = seg.open_arming({}, seg.PLANNING, dict(FALLBACK), seg.SOURCE_FALLBACK, 0.0, "classify")
    arming["booked"] = {seg.SCALE_USD: usd, seg.SCALE_MINUTES: minutes}
    return arming


def table(tmp_path, monkeypatch, **rows):
    path = tmp_path / "config.md"
    path.write_text("".join(f"| `{key}` | `{value}` | x |\n" for key, value in rows.items()), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_MD", path)


def derived_draft(tmp_path, derivation, name):
    estimate = f'planning_estimate = {{ active_minutes = 12, usd = 4.5, derivation = "{derivation}" }}'
    return _draft(tmp_path, replace={ESTIMATE_LINE: estimate}, name=name)


# --- the arming: from classify, at the fallback, until either approval ----------------------


def test_planning_arming_is_live_from_classify_until_either_approval(eng):
    for by in ("user", "agent"):
        sid = f"live-{by}"
        eng.plan_ready(sid)
        held = planning_of(eng.state(sid))
        assert held["opened_by"] == "classify" and held["budget"] == FALLBACK
        assert held["budget_source"] == seg.SOURCE_FALLBACK
        assert eng.run("approve", session=sid, by=by)["ok"]
        state = eng.state(sid)
        assert planning_of(state) is None
        assert planning_armings(state)[0]["closed_at"] is not None
        assert seg.current_arming(state.effort_segments)["segment"] != seg.PLANNING


def test_planning_settles_at_the_plans_first_approval(eng):
    eng.plan_ready()
    eng.book(usd=1.0)
    assert eng.run("approve", session="s", by="user")["ok"]
    state = eng.state()
    [closed] = planning_armings(state)
    assert closed["booked"][seg.SCALE_USD] == 1.0 and closed["closed_at"] is not None
    assert seg.current_arming(state.effort_segments)["segment"].startswith(seg.STAGE_PREFIX)
    eng.run("partition", session="s")
    eng.run("next_stage", session="s")
    eng.run("dispatch", session="s", dry_run=True)
    assert fires_of(eng.state()) == [] and planning_of(eng.state()) is None


def test_fallback_spend_part_is_two_medium_budgets_and_follows_the_key(tmp_path, monkeypatch):
    assert cli_common._planning_fallback(Thresholds())[seg.SCALE_USD] == 2.0 * Thresholds().budget_usd_float("medium")
    assert FALLBACK[seg.SCALE_USD] == 6.0
    table(tmp_path, monkeypatch, **{"budget-medium-usd": "5.00", "review-cycle-active-minutes": "50",
                                    "effort-divergence-multiple": "5.0"})
    assert cli_common._planning_fallback(Thresholds())[seg.SCALE_USD] == 10.0


def test_planning_fallback_fires_at_its_multiple_with_the_floor_just_under_it():
    minutes = FALLBACK[seg.SCALE_MINUTES]
    assert minutes * MULTIPLE == pytest.approx(77.0)
    assert minutes + FLOOR[seg.SCALE_MINUTES] < minutes * MULTIPLE
    assert minutes * MULTIPLE - minutes == pytest.approx(61.6) and 61.6 - FLOOR[seg.SCALE_MINUTES] < 2.0
    assert seg.judge(built(minutes=75.0), MULTIPLE, FLOOR) == []
    assert seg.judge(built(minutes=76.9), MULTIPLE, FLOOR) == []
    assert seg.judge(built(minutes=77.0), MULTIPLE, FLOOR) == [seg.SCALE_MINUTES]


def test_floor_does_not_bind_the_planning_fallbacks_spend():
    usd = FALLBACK[seg.SCALE_USD]
    assert usd * MULTIPLE - usd == pytest.approx(24.0) == 3 * FLOOR[seg.SCALE_USD]
    assert seg.judge(built(usd=29.9), MULTIPLE, FLOOR) == []
    assert seg.judge(built(usd=30.0), MULTIPLE, FLOOR) == [seg.SCALE_USD]


def test_fallback_fires_on_wall_clock_at_review_cycle_minutes_counted_from_classify(eng):
    assert Thresholds().review_cycle_active_minutes() == 77
    assert FALLBACK[seg.SCALE_MINUTES] * MULTIPLE == 77
    eng.planning()
    opened = planning_of(eng.state())
    assert opened["opened_at"] == eng.state().effort_segments["session_floor"]
    at = opened["opened_at"] + 1.0
    for gap in range(3):
        stamp(eng, at + gap * 1800.0)
    held = planning_of(eng.state())
    assert held["booked"][seg.SCALE_MINUTES] == pytest.approx(60.0) and not fires_of(eng.state())
    stamp(eng, at + 3 * 1800.0)
    [fire] = fires_of(eng.state())
    assert fire["scales"] == [seg.SCALE_MINUTES] and fire["arming_id"] == held["arming_id"]


def test_planning_overspend_fires_at_a_pre_approval_engine_act(eng):
    for scale, amount in ((seg.SCALE_USD, OVERSPENT_USD), (seg.SCALE_MINUTES, FALLBACK[seg.SCALE_MINUTES] * MULTIPLE)):
        sid = f"over-{scale}"
        eng.planning(sid)
        under = eng.book(sid=sid, **{scale: amount * 0.9})
        assert eng.run("submit_plan", session=sid, plan=eng.write())["ok"]
        assert fires_of(eng.state(sid)) == [] and under["fire"] is None
        eng.book(sid=sid, **{scale: amount * 0.1 + 0.01})
        refused = eng.run("plan_review", session=sid, verdict="pass", reviewer="thinker", target=eng.state(sid).plan_path)
        state = eng.state(sid)
        [fire] = fires_of(state)
        assert fire["scales"] == [scale] and fire["arming_id"] == planning_of(state)["arming_id"], refused
        assert gates.open_fire_record(state) is not None


def test_planning_carries_spend_and_wall_clock_fires_only(eng):
    eng.planning()
    eng.book(usd=500.0, minutes=5000.0)
    state = eng.state()
    state.history.extend({"event": "replan"} for _ in range(12))
    eng.store.save(state)
    assert not effort.armed(eng.state()) and effort.replans_divergence(eng.state(), Thresholds()) is None
    eng.run("submit_plan", session="s", plan=eng.write())
    [fire] = fires_of(eng.state())
    assert set(fire["scales"]) == {seg.SCALE_USD, seg.SCALE_MINUTES}
    assert not [f for f in eng.state().effort_fires if f.get("scale") == effort.SCALE_REPLANS]


# --- the acts that judge planning ---------------------------------------------------------


def judged_planning(eng, sid="s"):
    state = eng.state(sid)
    [fire] = fires_of(state)
    assert fire["arming_id"] == planning_of(state)["arming_id"] and fire["scales"] == [seg.SCALE_USD]


def test_submit_plan_judges_planning(eng):
    eng.planning()
    eng.book(usd=OVERSPENT_USD)
    refused = eng.run("submit_plan", session="s", plan=eng.write())
    judged_planning(eng)
    assert refused["ok"] is False


def test_plan_review_judges_planning(eng):
    plan = eng.plan_ready()
    eng.book(usd=OVERSPENT_USD)
    eng.review(plan)
    judged_planning(eng)


def test_present_plan_judges_planning(eng):
    plan = eng.plan_ready()
    eng.review(plan)
    eng.book(usd=OVERSPENT_USD)
    eng.present(plan)
    judged_planning(eng)


def test_prompt_boundary_stamp_judges_in_thread_planning_at_the_first_stamp_after_the_crossing(eng):
    eng.planning()
    first = planning_of(eng.state())["opened_at"] + 1.0
    stamp(eng, first)
    assert not fires_of(eng.state())
    eng.book(usd=OVERSPENT_USD)
    assert not fires_of(eng.state()), "booking alone judges nothing"
    stamp(eng, first + 1.0)
    judged_planning(eng)


def test_spawned_planners_engine_acts_judge_the_parents_planning(eng):
    eng.planning("p")
    eng.child("c", "p")
    eng.book(usd=OVERSPENT_USD, sid="p")
    assert not fires_of(eng.state("p"))
    cli_common.judge_on_parent("c", seg.ACT_DESCENDANT_RECORD_RESULT, store=eng.store, rows=[])
    judged_planning(eng, "p")


def ended_run(eng, sid="s", child="c"):
    state = eng.state(sid)
    now = cli_common._utcnow()
    run = {"child_session": child, "stage_index": 1, "started": now - 120.0, "ended": None, "dispatcher_pid": 1}
    state.effort_child_runs.append(run)
    eng.store.save(state)
    return run


def test_spawned_planners_run_end_books_and_judges_the_parents_planning(eng):
    eng.plan_ready("p")
    eng.child("c", "p")
    eng.row(OVERSPENT_USD, spawner="c", sid="p")
    state = eng.state("p")
    run = state.effort_child_runs
    assert not run and planning_of(state)["booked"][seg.SCALE_USD] == 0.0
    held = ended_run(eng, "p")
    state = eng.state("p")
    cli_dispatch._end_child_run(eng.store, state, state.effort_child_runs[0])
    after = eng.state("p")
    assert held["child_session"] == "c" and planning_of(after)["booked"][seg.SCALE_USD] == OVERSPENT_USD
    [fire] = fires_of(after)
    assert fire["arming_id"] == planning_of(after)["arming_id"] and fire["scales"] == [seg.SCALE_USD]


def test_planner_run_end_judged_at_next_submit_plan(eng):
    eng.planning("p")
    eng.child("c", "p")
    eng.row(OVERSPENT_USD, spawner="c", sid="p")
    assert planning_of(eng.state("p"))["booked"][seg.SCALE_USD] == 0.0
    refused = eng.run("submit_plan", session="p", plan=eng.write())
    state = eng.state("p")
    assert planning_of(state)["booked"][seg.SCALE_USD] == OVERSPENT_USD and refused["ok"] is False
    [fire] = fires_of(state)
    assert fire["scales"] == [seg.SCALE_USD]


def test_late_reviewer_rows_fire_at_approve_by_agent(eng):
    eng.plan_ready("p")
    eng.child("c", "p")
    eng.row(OVERSPENT_USD, spawner="c", sid="p")
    assert planning_of(eng.state("p"))["booked"][seg.SCALE_USD] == 0.0
    refused = eng.run("approve", session="p", by="agent")
    state = eng.state("p")
    assert refused["ok"] is False and planning_of(state)["booked"][seg.SCALE_USD] == OVERSPENT_USD
    judged_planning(eng, "p")


def test_late_reviewer_rows_fire_at_plan_review(eng):
    plan = eng.plan_ready("p")
    eng.child("c", "p")
    eng.row(OVERSPENT_USD, spawner="c", sid="p")
    assert planning_of(eng.state("p"))["booked"][seg.SCALE_USD] == 0.0
    eng.review(plan, "p")
    assert planning_of(eng.state("p"))["booked"][seg.SCALE_USD] == OVERSPENT_USD
    judged_planning(eng, "p")


def test_planner_run_end_judged_at_next_approve_by_agent(eng):
    eng.plan_ready("p")
    eng.child("c", "p")
    eng.row(OVERSPENT_USD, spawner="c", sid="p")
    assert fires_of(eng.state("c")) == [] and planning_of(eng.state("p"))["booked"][seg.SCALE_USD] == 0.0
    refused = eng.run("approve", session="p", by="agent")
    state = eng.state("p")
    assert refused["ok"] is False and state.node != "APPROVED" and planning_of(state) is not None
    assert planning_of(state)["booked"][seg.SCALE_USD] == OVERSPENT_USD
    judged_planning(eng, "p")
    assert retiers(state) == []


def test_approve_by_agent_at_the_planning_fire_point_fires_and_is_refused(eng):
    eng.plan_ready()
    eng.book(usd=OVERSPENT_USD)
    refused = eng.run("approve", session="s", by="agent")
    state = eng.state()
    assert refused["ok"] is False and state.node != "APPROVED"
    assert planning_of(state) is not None
    judged_planning(eng)
    assert eng.run("approve", session="s", by="user")["ok"] and planning_of(eng.state()) is None


def test_planning_fire_refuses_self_approval_until_the_user_decides(eng):
    plan = eng.plan_ready()
    eng.book(usd=OVERSPENT_USD)
    eng.review(plan)
    assert gates.open_fire_record(eng.state()) is not None
    assert eng.run("approve", session="s", by="agent")["ok"] is False
    assert eng.run("fire_acknowledge", session="s", by="user", decision="continue")["ok"]
    assert gates.open_fire_record(eng.state()) is None
    assert eng.run("approve", session="s", by="user")["ok"]


def test_planner_run_end_then_approve_by_user_writes_retier_no_fire(eng):
    eng.plan_ready("p")
    eng.child("c", "p")
    eng.row(OVERSPENT_USD, spawner="c", sid="p")
    assert eng.run("approve", session="p", by="user")["ok"]
    state = eng.state("p")
    [closed] = planning_armings(state)
    assert closed["booked"][seg.SCALE_USD] == OVERSPENT_USD
    assert [r["segment"] for r in retiers(state)] == [seg.PLANNING]
    assert fires_of(state) == [] and gates.open_fire_record(state) is None


def test_retier_record_without_a_declared_tier_names_the_budget_source(eng):
    eng.plan_ready()
    eng.book(usd=OVERSPENT_USD)
    assert eng.run("approve", session="s", by="user")["ok"]
    [record] = retiers(eng.state())
    assert record["segment"] == seg.PLANNING and record["declared_tier"] is None
    assert record["budget_source"] == seg.SOURCE_FALLBACK
    assert record["observed_tier"][seg.SCALE_USD] == seg.TIER_ABOVE_LADDER


# --- renewal and the review-cycle minutes route -------------------------------------------


def test_planning_arming_renewed_before_agreement_opens_from_zero_at_the_fallback(eng):
    plan = eng.plan_ready()
    eng.book(usd=OVERSPENT_USD, minutes=80.0)
    eng.review(plan)
    first = planning_of(eng.state())
    assert eng.run("fire_acknowledge", session="s", by="user", decision="continue")["ok"]
    state = eng.state()
    renewed = planning_of(state)
    assert renewed["arming_id"] != first["arming_id"]
    assert renewed["budget"] == FALLBACK and renewed["budget_source"] == seg.SOURCE_FALLBACK
    assert renewed["booked"] == {seg.SCALE_USD: 0.0, seg.SCALE_MINUTES: 0.0} and not renewed["carried"]["usd"]
    assert renewed["opened_at"] >= first["closed_at"] if first.get("closed_at") else True
    assert state.effort_segments.get("agreed_estimate") is None


def test_retired_cycle_minutes_limit_no_longer_routes_plan_ready_into_diagnosing(eng):
    plan = eng.plan_ready()
    state = eng.state()
    state.effort_actuals[effort.ACTUAL_MINUTES_KEY] = 5000.0
    eng.store.save(state)
    assert eng.run("plan_review", session="s", verdict="revise", reviewer="thinker", target=plan,
                   concern=["blocking: stage:1 the verify command proves nothing"])["rc"] in (0, 1)
    state = eng.state()
    assert state.node != "DIAGNOSING" and gates.review_trigger(state) is None


def cycle(**kw):
    base = dict(session_id="s", task_id="t", weight_class="SUBSTANTIVE", plan_path="/plan.toml",
                plan_verified=True, review_cycle_open=True, review_cycle_sha="sha-a",
                review_cycle_minutes_anchor=0.0)
    return SessionState(**{**base, **kw})


def book_minutes(state, minutes):
    state.effort_actuals = {**(state.effort_actuals or {}), effort.ACTUAL_MINUTES_KEY: minutes}


def test_cycle_minutes_route_kept_while_window_arming_open():
    state = cycle()
    book_minutes(state, 500.0)
    assert gates.review_trigger(state, Thresholds()) is None
    seg.open_arming(state.effort_segments, seg.PLANNING, None, seg.SOURCE_WINDOW, 0.0, "migration")
    fired = gates.review_trigger(state, Thresholds())
    assert fired["trigger"] == "cycle_minutes" and fired["value"] == 500.0


def test_review_cycle_acts_and_stage_nonpass_streak_routes_are_unchanged():
    acts = gates.review_trigger(cycle(review_cycle_acts=Thresholds().review_cycle_acts()), Thresholds())
    assert acts["trigger"] == "cycle_acts" and acts["threshold"] == Thresholds().review_cycle_acts()
    streak = Thresholds().review_stage_nonpass_streak()
    held = cycle(review_stage_streaks={"stage:2": streak})
    assert gates.review_trigger(held, Thresholds())["trigger"] == "stage_streak"
    assert gates.review_trigger(cycle(review_stage_streaks={"stage:2": streak - 1}), Thresholds()) is None


# --- the planning estimate in the order ---------------------------------------------------


def test_planning_estimate_table_is_parsed_and_printed_with_its_derivation(tmp_path):
    doc = load_order_draft(_draft(tmp_path))[0]
    estimate = doc.meta.order.planning_estimate
    assert (estimate.active_minutes, estimate.usd) == (12, 4.5)
    assert estimate.derivation == "one reviewed plan of a few stages"
    printed = "\n".join(render_order_understanding_md(doc))
    assert "12 active minutes, 4.50 USD" in printed
    assert "Derivation:** one reviewed plan of a few stages" in printed
    assert understanding_parts(doc)["planning_estimate"]


def test_understanding_digest_is_unchanged_for_an_order_without_the_table(tmp_path):
    doc = load_order_draft(_draft(tmp_path, omit=(ESTIMATE_LINE,)))[0]
    assert "planning_estimate" not in understanding_parts(doc)
    assert doc.meta.order.planning_estimate is None
    assert understanding_digest(doc) == (
        "2b5bbcd8f4d57ce4aa60aa1bf0efa53ef0e3ca25cfad8132a054c46c49eb0659")
    assert understanding_digest(load_order_draft(_draft(tmp_path, name="with.toml"))[0]) != understanding_digest(doc)


def test_order_draft_without_estimate_or_derivation_is_refused_before_first_approval(ostore, armed_env, tmp_path):
    incomplete = {
        "none": None,
        "no-derivation": "planning_estimate = { active_minutes = 12, usd = 4.5 }",
        "no-usd": 'planning_estimate = { active_minutes = 12, derivation = "d" }',
        "no-minutes": 'planning_estimate = { usd = 4.5, derivation = "d" }',
    }
    for name, line in incomplete.items():
        sid = f"inc-{name}"
        _start(ostore, session=sid)
        assert _classify(ostore, session=sid).ok
        draft = _draft(tmp_path, omit=(ESTIMATE_LINE,) if line is None else (), replace={ESTIMATE_LINE: line} if line else None,
                       name=f"{sid}.toml")
        assert _submit(ostore, draft, session=sid).ok
        assert _review(ostore, session=sid).ok
        presented = _present(ostore, tmp_path, session=sid)
        assert presented.ok is False and "planning estimate" in presented.detail, name
        agreed = cli.cmd_order_agree(Namespace(session=sid, note=""), store=ostore)
        assert agreed.ok is False and "planning estimate" in agreed.detail, name
        assert ostore.load(sid).order_agreement is None


def test_engine_accepts_any_present_derivation_without_judging_its_meaning(ostore, armed_env, tmp_path):
    for number, derivation in enumerate(("x", "lorem ipsum dolor", "42", "the plan costs what it costs")):
        sid = f"deriv-{number}"
        _start(ostore, session=sid)
        assert _classify(ostore, session=sid).ok
        assert _submit(ostore, derived_draft(tmp_path, derivation, f"{sid}.toml"), session=sid).ok
        assert _review(ostore, session=sid).ok
        presented = _present(ostore, tmp_path, session=sid)
        assert presented.ok, (derivation, presented.detail)


def test_agreed_estimate_becomes_the_next_planning_armings_budget(ostore, home, armed_env, tmp_path):
    _agreed(ostore, home, tmp_path)
    state = ostore.load(SESSION)
    held = planning_of(state)
    assert held["budget"] == ESTIMATE and held["budget_source"] == seg.SOURCE_AGREED
    assert state.effort_segments["agreed_estimate"]["derivation"] == "one reviewed plan of a few stages"


def test_order_agreement_before_first_approval_closes_the_fallback_arming_and_opens_the_estimate_arming(
        ostore, home, armed_env, tmp_path):
    _presented(ostore, tmp_path)
    before = planning_of(ostore.load(SESSION))
    assert before["budget"] == FALLBACK
    path = tmp_path / "draft.toml"
    assert _user_answers(_marker_of(path)) == 1
    assert _agree(ostore).ok
    state = ostore.load(SESSION)
    closed, opened = planning_armings(state)
    assert closed["arming_id"] == before["arming_id"] and closed["closed_by"] == seg.ACT_ORDER_AGREE
    assert closed["budget"] == FALLBACK and closed["budget_source"] == seg.SOURCE_FALLBACK
    assert opened["closed_at"] is None and opened["budget"] == ESTIMATE
    assert opened["booked"] == {seg.SCALE_USD: 0.0, seg.SCALE_MINUTES: 0.0}, "the estimate is not charged with the stretch before"


def test_renewed_planning_arming_opens_at_the_agreed_estimate(ostore, home, armed_env, tmp_path):
    _agreed(ostore, home, tmp_path)
    first = planning_of(ostore.load(SESSION))
    renewed = renew(ostore, SESSION, seg.ACT_FIRE_ACK_BY_USER)
    assert renewed["arming_id"] != first["arming_id"]
    assert renewed["budget"] == ESTIMATE and renewed["budget_source"] == seg.SOURCE_AGREED


def test_order_revision_opens_no_arming_and_judges_nothing(ostore, armed_env, tmp_path):
    _start(ostore)
    assert _classify(ostore).ok
    first = _draft(tmp_path)
    assert _submit(ostore, first).ok
    state = ostore.load(SESSION)
    planning_of(state)["booked"][seg.SCALE_USD] = OVERSPENT_USD
    ostore.save(state)
    count = len(planning_armings(ostore.load(SESSION)))
    revised = _draft(tmp_path, replace={"difficulty": 'difficulty = "a release takes a week of hand-work"'},
                     name="revised.toml")
    assert _submit(ostore, revised).ok
    state = ostore.load(SESSION)
    assert len(planning_armings(state)) == count and fires_of(state) == []


def spawned_planner_rows(ostore, tmp_path, usd):
    """A spawned planner that wrote cost rows past the fallback's fire point and made no engine act."""
    _start(ostore, session="planner")
    child = ostore.load("planner")
    child.parent_link = {"parent_session": SESSION}
    ostore.save(child)
    with open(tmp_path / "costs.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"plan_path": None, "stage_index": 1, "cost_usd": usd, "ts": time.time(),
                                 "spawner_session": "planner"}) + "\n")


def test_planner_run_end_then_order_submit_judges_nothing(ostore, armed_env, tmp_path):
    _start(ostore)
    assert _classify(ostore).ok
    spawned_planner_rows(ostore, tmp_path, OVERSPENT_USD)
    assert _submit(ostore, _draft(tmp_path)).ok
    state = ostore.load(SESSION)
    assert planning_of(state)["booked"][seg.SCALE_USD] == 0.0
    assert fires_of(state) == [] and gates.open_fire_record(state) is None
    revised = _draft(tmp_path, replace={"difficulty": 'difficulty = "release by hand twice a week"'},
                     name="revised.toml")
    assert _submit(ostore, revised).ok
    state = ostore.load(SESSION)
    assert fires_of(state) == [] and len(planning_armings(state)) == 1


def test_planner_run_end_then_order_agree_writes_retier_no_fire(ostore, home, armed_env, tmp_path):
    path = _presented(ostore, tmp_path)
    spawned_planner_rows(ostore, tmp_path, OVERSPENT_USD)
    assert _user_answers(_marker_of(path)) == 1
    agreed = _agree(ostore)
    assert agreed.ok, agreed.detail
    state = ostore.load(SESSION)
    closed, opened = planning_armings(state)
    assert closed["booked"][seg.SCALE_USD] == OVERSPENT_USD and closed["closed_by"] == seg.ACT_ORDER_AGREE
    assert [r["segment"] for r in retiers(state)] == [seg.PLANNING]
    assert fires_of(state) == [] and gates.open_fire_record(state) is None
    assert opened["budget"] == ESTIMATE and opened["booked"][seg.SCALE_USD] == 0.0


def test_order_agreement_past_the_fallback_fire_point_writes_retier_no_fire(ostore, home, armed_env, tmp_path):
    path = _presented(ostore, tmp_path)
    state = ostore.load(SESSION)
    planning_of(state)["booked"][seg.SCALE_USD] = OVERSPENT_USD
    ostore.save(state)
    assert _user_answers(_marker_of(path)) == 1
    assert _agree(ostore).ok
    state = ostore.load(SESSION)
    closed, opened = planning_armings(state)
    assert closed["booked"][seg.SCALE_USD] == OVERSPENT_USD
    [record] = retiers(state)
    assert record["segment"] == seg.PLANNING and record["arming_id"] == closed["arming_id"]
    assert fires_of(state) == [] and gates.open_fire_record(state) is None
    assert opened["budget"] == ESTIMATE and opened["booked"] == {seg.SCALE_USD: 0.0, seg.SCALE_MINUTES: 0.0}


def test_order_agreement_after_the_first_approval_is_accepted_without_an_estimate(ostore, home, armed_env, tmp_path):
    _agreed(ostore, home, tmp_path)
    state = ostore.load(SESSION)
    seg.close_arming(state.effort_segments, planning_of(state)["arming_id"], "approve-by-user", time.time(), 5.0)
    ostore.save(state)
    assert planning_of(ostore.load(SESSION)) is None
    bare = _draft(tmp_path, omit=(ESTIMATE_LINE,), replace={"difficulty": 'difficulty = "release by hand"'},
                  name="bare.toml")
    assert _submit(ostore, bare).ok and _review(ostore).ok
    assert _present(ostore, tmp_path).ok
    _confirm_delivery(ostore)
    assert _user_answers(_marker_of(bare)) == 1
    agreed = _agree(ostore)
    assert agreed.ok, agreed.detail
    assert planning_of(ostore.load(SESSION)) is None


# --- a migrated session -----------------------------------------------------------------


def migrated(ostore, home):
    ostore.save(ostore.load(SESSION))
    v61(_state_file(home))
    return ostore.load(SESSION)


def test_order_agreed_before_migration_is_judged_on_the_fallback_from_its_migration_instant_with_no_estimate(
        ostore, home, armed_env, tmp_path):
    _agreed(ostore, home, tmp_path)
    state = migrated(ostore, home)
    held = planning_of(state)
    assert held["opened_at"] == state.effort_migrated["instant"] and "agreed_estimate" not in state.effort_segments
    held["booked"][seg.SCALE_USD] = OVERSPENT_USD
    fire = cli_common._judge_act(state, seg.ACT_SUBMIT_PLAN, store=ostore)
    assert fire is not None and planning_of(state)["budget"] == FALLBACK
    assert planning_of(state)["budget_source"] == seg.SOURCE_FALLBACK


def agreed_bare_before_migration(ostore, home, tmp_path):
    """An order agreed with no estimate table (as every order agreed before the change was), its
    session then migrated while still planning."""
    _agreed(ostore, home, tmp_path)
    state = ostore.load(SESSION)
    seg.close_arming(state.effort_segments, planning_of(state)["arming_id"], "approve-by-user", time.time(), 5.0)
    ostore.save(state)
    bare = _draft(tmp_path, omit=(ESTIMATE_LINE,), replace={"difficulty": 'difficulty = "release by hand"'},
                  name="bare.toml")
    assert _submit(ostore, bare).ok and _review(ostore).ok
    assert _present(ostore, tmp_path).ok
    _confirm_delivery(ostore)
    assert _user_answers(_marker_of(bare)) == 1
    assert _agree(ostore).ok
    state = migrated(ostore, home)
    assert planning_of(state) is not None
    return bare


def test_order_agreed_before_migration_loads_keeps_its_digest_and_is_not_refused_at_present(
        ostore, home, armed_env, tmp_path):
    bare = agreed_bare_before_migration(ostore, home, tmp_path)
    state = ostore.load(SESSION)
    assert state.order_agreement.understanding_sha256 == understanding_digest(load_order_draft(bare)[0])
    assert state.effort_migrated["instant"] > state.order_agreement.at
    presented = _present(ostore, tmp_path)
    assert presented.ok, presented.detail


def test_order_agreed_before_migration_is_refused_when_its_understanding_changed(
        ostore, home, armed_env, tmp_path):
    agreed_bare_before_migration(ostore, home, tmp_path)
    changed = _draft(tmp_path, omit=(ESTIMATE_LINE,), replace={"difficulty": 'difficulty = "release by hand, twice"'},
                     name="changed.toml")
    assert _submit(ostore, changed).ok and _review(ostore).ok
    refused = _present(ostore, tmp_path)
    assert refused.ok is False and "planning estimate" in refused.detail


def test_re_agreeing_an_order_agreed_before_migration_without_the_table_is_refused(
        ostore, home, armed_env, tmp_path):
    agreed_bare_before_migration(ostore, home, tmp_path)
    assert _present(ostore, tmp_path).ok
    history = len(ostore.load(SESSION).order_agreement_history)
    refused = _agree(ostore)
    assert refused.ok is False and "planning estimate" in refused.detail
    assert len(ostore.load(SESSION).order_agreement_history) == history


def test_migrated_session_with_an_unagreed_order_is_refused_without_the_table_and_judged_on_the_fallback(
        ostore, home, armed_env, tmp_path):
    _start(ostore)
    assert _classify(ostore).ok
    state = migrated(ostore, home)
    assert planning_of(state) is not None
    bare = _draft(tmp_path, omit=(ESTIMATE_LINE,))
    assert _submit(ostore, bare).ok and _review(ostore).ok
    refused = _present(ostore, tmp_path)
    assert refused.ok is False and "planning estimate" in refused.detail
    state = ostore.load(SESSION)
    planning_of(state)["booked"][seg.SCALE_USD] = OVERSPENT_USD
    fire = cli_common._judge_act(state, seg.ACT_SUBMIT_PLAN, store=ostore)
    assert fire is not None and fire["scales"] == [seg.SCALE_USD] and planning_of(state)["budget"] == FALLBACK


def test_planning_stamp_loads_only_spawners_since_classify(eng, monkeypatch):
    eng.planning()
    floor = eng.state().effort_segments["session_floor"]
    seen = []
    real = cli_common._refresh_spend

    def spying(state, store, rows, path, **kw):
        seen.append(kw.get("since"))
        return real(state, store, rows, path, **kw)

    monkeypatch.setattr(cli_common, "_refresh_spend", spying)
    stamp(eng, floor + 1.0)
    stamp(eng, floor + 2.0)
    assert seen and all(since == floor and since > 0.0 for since in seen)
    eng.planning("m")
    path = eng.root / "m.json"
    v61(path)
    migrated_state = eng.state("m")
    assert migrated_state.effort_migrated and planning_of(migrated_state) is not None
    seen.clear()
    eng.run("status", session="m")
    stamp(eng, time.time() + 1.0, "m")
    stamp(eng, time.time() + 2.0, "m")
    assert seen == []
    state = eng.state("m")
    planning_of(state)["booked"][seg.SCALE_USD] = OVERSPENT_USD
    eng.store.save(state)
    stamp(eng, time.time() + 3.0, "m")
    [fire] = fires_of(eng.state("m"))
    assert fire["arming_id"] == planning_of(eng.state("m"))["arming_id"]
