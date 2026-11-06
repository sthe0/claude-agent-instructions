"""Per-segment effort accounting: the ledger, stage identity, write-time booking, the single
current stage and the schema 62 migration. Judgment on top of it lives in
test_effort_per_stage_judging.py; the rule-to-test map is effort_per_stage_rules.toml.
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentctl import cli, cost, effort, order_approvals as oa
from agentctl import effort_segments as seg
from agentctl.plan import load_plan, order_digest
from agentctl.state import GateRecord, Node, SessionState, StageStatus
from agentctl.store import FileStateStore
from ast_purity import impure_names
from conftest import STAGE_OBSERVATIONS, SUBSTANTIVE_ORDER, declare_venue

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TWO_STAGE = (FIXTURES / "plan_two_stage.toml").read_text(encoding="utf-8")


def with_order(text: str = TWO_STAGE) -> str:
    head, sep, tail = text.partition("\n[[stage]]")
    return head + "\n" + SUBSTANTIVE_ORDER + sep + tail


def inserted_ahead(text: str = TWO_STAGE) -> str:
    """The same stages, renumbered behind a new first stage: the old supplier moves too."""
    head, sep, tail = text.partition("\n[[stage]]")
    moved = (tail.replace("index = 2", "index = 3").replace("depends_on = [1]", "depends_on = [2]")
             .replace("index = 1", "index = 2"))
    first = ('\n[[stage]]\nindex = 1\ntitle = "New first"\nexecutor = "in_thread"\n'
             'expected_result_image = "ground prepared"\ncriterion_type = "measurable"\n'
             'done_criterion = "ground prepared"\ndepends_on = []\n')
    return head + first + sep + moved


class Eng:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.root = tmp_path / "state"
        self.store = FileStateStore(self.root)
        self._plans = 0

    def run(self, cmd: str, **flags):
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

    def write(self, text: str) -> str:
        self._plans += 1
        path = self.tmp / f"plan{self._plans}.toml"
        path.write_text(declare_venue(text, self.tmp), encoding="utf-8")
        return str(path)

    def state(self, sid: str) -> SessionState:
        return self.store.load(sid)

    def classify(self, sid: str, task: str | None = None):
        self.run("start", session=sid, task=task or f"task-{sid}", goal="g", done_criterion="dc")
        self.run("classify", session=sid, architectural=True, changed_lines=200, wall_clock_min=60)

    def open(self, sid: str, plan: str):
        self.classify(sid)
        self.run("plan", session=sid)
        return self.run("submit_plan", session=sid, plan=plan)

    def approved(self, sid: str, plan: str):
        self.open(sid, plan)
        d = self.run("approve", session=sid, by="user")
        assert d["ok"], d
        return d

    def executing(self, sid: str, plan: str):
        self.approved(sid, plan)
        self.run("partition", session=sid)
        return self.run("next_stage", session=sid)

    def passed(self, sid: str, index: int, log: str | None = None):
        d = self.run("record_result", session=sid, status="passed", actual="ok", control="reviewed: ok",
                     observation=STAGE_OBSERVATIONS[index - 1], cost_log=log)
        assert d["ok"], d
        return d

    def log(self, *rows: dict) -> str:
        path = self.tmp / "costs.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return str(path)

    def to_v61(self, sid: str) -> Path:
        path = self.root / f"{sid}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in ("stage_uids", "effort_segments", "effort_migrated"):
            data.pop(key, None)
        data["schema_version"] = 61
        path.write_text(json.dumps(data), encoding="utf-8")
        return path


@pytest.fixture
def eng(tmp_path):
    return Eng(tmp_path)


@pytest.fixture(autouse=True)
def _no_ambient_harness_session(monkeypatch):
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")


def row(plan: str, usd: float, ts: float, spawner: str | None = None, stage: int = 1) -> dict:
    return {"plan_path": plan, "stage_index": stage, "cost_usd": usd, "ts": ts, "spawner_session": spawner}


def armings(state: SessionState, segment: str | None = None) -> list[dict]:
    held = state.effort_segments.get("armings", [])
    return [a for a in held if segment is None or a["segment"] == segment]


def backdate(state: SessionState, arming: dict, ts: float) -> None:
    """Move an arming's first start (and, for the ledger's earliest, the session floor) back."""
    arming["opened_at"] = ts
    arming["spans"][0]["start"] = ts
    ledger = state.effort_segments
    ledger["session_floor"] = min(ledger["session_floor"], ts)


def stage_arming(state: SessionState, index: int) -> dict:
    return seg.open_arming_of(state.effort_segments, seg.stage_segment(state.stage_uid(index)))


def total(state: SessionState) -> dict:
    return seg.conserve(state.effort_segments)["total"]


def ledger_of_two(a_open=100.0, a_close=200.0, b_open=200.0):
    ledger: dict = {}
    a = seg.open_arming(ledger, "stage:a", {"usd": 1.0, "minutes": 10.0}, seg.SOURCE_DECLARED, a_open, "t")
    seg.close_arming(ledger, a["arming_id"], "t", a_close, 5.0)
    b = seg.open_arming(ledger, "stage:b", {"usd": 1.0, "minutes": 10.0}, seg.SOURCE_DECLARED, b_open, "t")
    return ledger, a, b


# --- the pure module --------------------------------------------------------------------


def test_effort_segments_is_pure():
    assert impure_names(seg) == set()
    tree = ast.parse(Path(seg.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.partition(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "the pure module imports nothing of the engine"
            imported.add(node.module.partition(".")[0])
    assert imported <= set(sys.stdlib_module_names)
    assert not imported & {"os", "time", "pathlib", "subprocess"}


def test_row_books_to_the_arming_current_at_its_write_time():
    ledger, a, b = ledger_of_two()
    assert seg.book(ledger, 150.0, 2.0, 0.0, "row") is a
    assert seg.book(ledger, 250.0, 3.0, 0.0, "row") is b
    assert a["booked"]["usd"] == 2.0 and b["booked"]["usd"] == 3.0
    assert seg.conserve(ledger)["total"]["usd"] == 5.0


def test_book_accepts_a_late_row_on_a_closed_arming():
    ledger, a, b = ledger_of_two()
    late = seg.book(ledger, 199.0, 1.5, 0.0, "row")
    assert late is a and a["closed_at"] is not None and a["booked"]["usd"] == 1.5
    assert b["booked"]["usd"] == 0.0


def test_booking_older_than_the_earliest_arming_is_not_booked():
    ledger, _, _ = ledger_of_two()
    assert seg.book(ledger, 99.0, 7.0, 0.0, "row") is None
    assert seg.conserve(ledger)["total"]["usd"] == 0.0


def test_empty_ledger_books_nothing_and_takes_no_bound():
    ledger: dict = {}
    assert seg.book(ledger, 150.0, 1.0, 1.0, "row") is None
    assert seg.book_interval(ledger, 100.0, 200.0, 1.0, "gap") == []
    assert seg.conserve(ledger)["total"] == {"usd": 0.0, "minutes": 0.0}


def test_gap_straddling_the_first_arming_books_only_its_part_after_it():
    ledger: dict = {}
    seg.open_arming(ledger, seg.PLANNING, None, seg.SOURCE_UNBUDGETED, 150.0, "classify")
    seg.book_interval(ledger, 100.0, 200.0, 10.0, "gap")
    assert seg.conserve(ledger)["total"]["minutes"] == pytest.approx(5.0)


def test_carried_actual_is_never_a_booking():
    ledger, _, b = ledger_of_two()
    b["carried"] = {"usd": 9.0, "minutes": 9.0}
    assert seg.arming_actual(b)["usd"] == 9.0
    assert seg.conserve(ledger)["total"]["usd"] == 0.0


def test_match_identities_matches_by_identity_key_and_keeps_identity():
    previous = [
        {"uid": "A", "index": 1, "key": "ka", "edges": []},
        {"uid": "B", "index": 2, "key": "kb", "edges": [[1, "e", "x", "d"]]},
    ]
    new = [
        {"index": 1, "key": "fresh", "edges": []},
        {"index": 2, "key": "ka", "edges": []},
        {"index": 3, "key": "kb", "edges": [[2, "e", "x", "d"]]},
        {"index": 4, "key": "kb", "edges": [[1, "e", "x", "d"]]},
    ]
    matches = seg.match_identities(previous, new)
    assert matches[1] is None and matches[2] == "A" and matches[3] == "B"
    assert matches[4] is None, "the supplier is a different stage, whatever the index says"


# --- identity ---------------------------------------------------------------------------


def test_every_stage_gets_an_identity_at_creation(eng):
    plan = eng.write(with_order())
    eng.approved("s", plan)
    state = eng.state("s")
    uids = [state.stage_uid(s.index) for s in state.stages]
    assert all(uids) and len(set(uids)) == 2


def test_identity_with_actual_and_budget_survives_replan_and_reindexing(eng):
    from agentctl.cli_common import _rematch_stage_uids

    eng.executing("s", eng.write(with_order()))
    eng.passed("s", 1)
    state = eng.state("s")
    live = stage_arming(state, 2)
    live["booked"]["usd"] = 3.0
    uid_2, budget = state.stage_uid(2), dict(live["budget"])
    new = load_plan(eng.write(with_order(inserted_ahead())), strict=False)
    _rematch_stage_uids(state, new.stages)
    state.stages = new.stages
    assert state.stage_uid(3) == uid_2
    assert stage_arming(state, 3) is live
    assert live["budget"] == budget and seg.arming_actual(live)["usd"] == 3.0


def test_a_stage_added_by_a_replan_starts_from_zero(eng):
    from agentctl.cli_common import _ensure_stage_arming, _rematch_stage_uids

    eng.executing("s", eng.write(with_order()))
    state = eng.state("s")
    known = set(state.stage_uids.values())
    new = load_plan(eng.write(with_order(inserted_ahead())), strict=False)
    _rematch_stage_uids(state, new.stages)
    state.stages = new.stages
    assert state.stage_uid(1) not in known
    added = _ensure_stage_arming(state, state.stage(1), time.time(), "next_stage")
    assert seg.arming_actual(added) == {"usd": 0.0, "minutes": 0.0}


def test_identity_survives_lazy_unfolding(eng):
    eng.executing("s", eng.write(with_order()))
    parent_uid = eng.state("s").stage_uid(1)
    child = eng.write(TWO_STAGE)
    eng.run("push_subplan", session="s", plan=child, task="child", originating_stage=1)
    pushed = eng.state("s")
    assert pushed.stage_uids["#1"] == parent_uid
    eng.run("classify", session="s", architectural=True, changed_lines=200, wall_clock_min=60)
    eng.run("plan", session="s")
    eng.run("submit_plan", session="s", plan=child)
    eng.run("approve", session="s", by="user")
    eng.run("partition", session="s")
    state = eng.state("s")
    assert state.stage_uid(1) and state.stage_uid(1) != parent_uid
    assert state.stage_uids["#1"] == parent_uid
    for stage in state.stages:
        stage.outcome.status = StageStatus.PASSED.value
    state.current_stage = None
    state.resolution = GateRecord("resolution", armed=True, passed=True, by="tester")
    state.node = Node.RESOLVED.value
    eng.store.save(state)
    assert eng.run("pop_subplan", session="s")["ok"]
    assert eng.state("s").stage_uid(1) == parent_uid


def test_identity_survives_the_sessions_of_one_order(eng):
    from agentctl.cli_common import _flush_order_effort

    plan = eng.write(with_order())
    eng.executing("s1", plan)
    first = eng.state("s1")
    _flush_order_effort(first, store=eng.store)
    eng.store.save(first)
    eng.open("s2", plan)
    assert eng.run("approve", session="s2", by="agent")["ok"]
    second = eng.state("s2")
    assert [second.stage_uid(i) for i in (1, 2)] == [first.stage_uid(i) for i in (1, 2)]


def test_self_approval_leaving_the_live_stage_unmatched_in_a_later_session_is_refused(eng):
    from agentctl.cli_common import _flush_order_effort

    plan = eng.write(with_order())
    eng.executing("s1", plan)
    first = eng.state("s1")
    _flush_order_effort(first, store=eng.store)
    eng.store.save(first)
    revised = with_order().replace('title = "Scaffold module"', 'title = "Another stage"')
    eng.open("s2", eng.write(revised))
    refused = eng.run("approve", session="s2", by="agent")
    assert refused["ok"] is False
    assert any("Scaffold module" in b for b in refused["data"]["blockers"])
    assert eng.run("approve", session="s2", by="user")["ok"]
    order = order_digest(load_plan(plan, strict=False))
    assert seg.stage_segment(first.stage_uid(1)) not in oa.effort_identity(order)["armings"]


def test_replan_inserting_a_stage_ahead_keeps_the_live_stage_matched(eng):
    from agentctl.cli_common import _flush_order_effort

    plan = eng.write(with_order())
    eng.executing("s1", plan)
    eng.passed("s1", 1)
    first = eng.state("s1")
    uid = first.stage_uid(2)
    stage_arming(first, 2)["booked"]["usd"] = 2.5
    _flush_order_effort(first, store=eng.store)
    eng.store.save(first)
    eng.open("s2", eng.write(with_order(inserted_ahead())))
    assert eng.run("approve", session="s2", by="agent")["ok"]
    second = eng.state("s2")
    assert second.stage_uid(3) == uid
    arming = stage_arming(second, 3)
    assert arming["carried"]["usd"] == 2.5 and arming["booked"]["usd"] == 0.0


def test_two_session_carry_keeps_identity_budget_and_actual_and_counts_each_row_once(eng):
    from agentctl.cli_common import _flush_order_effort, _refresh_spend

    plan = eng.write(with_order())
    eng.executing("s1", plan)
    first = eng.state("s1")
    rows = [row(plan, 2.0, time.time())]
    _refresh_spend(first, eng.store, rows, plan)
    _flush_order_effort(first, store=eng.store)
    eng.store.save(first)
    budget = stage_arming(first, 1)["budget"]
    eng.open("s2", plan)
    eng.run("approve", session="s2", by="agent")
    second = eng.state("s2")
    carried = stage_arming(second, 1)
    adopted_at = carried["opened_at"]
    planning = armings(second, seg.PLANNING)[0]
    assert second.stage_uid(1) == first.stage_uid(1)
    assert carried["budget"] == budget and carried["carried"]["usd"] == 2.0
    between = row(plan, 0.25, (planning["opened_at"] + adopted_at) / 2, spawner="descendant")
    after = row(plan, 0.5, adopted_at + 1)
    _refresh_spend(second, eng.store, [*rows, between, after], plan)
    assert carried["booked"]["usd"] == 0.5, "only the row written after the adoption is the stage's"
    assert planning["booked"]["usd"] == 0.25, "written between the sessions' acts, booked once"
    assert seg.arming_actual(carried)["usd"] == 2.5
    assert total(first)["usd"] + total(second)["usd"] == pytest.approx(2.75)


def test_second_session_continues_the_planning_arming(eng):
    from agentctl.cli_common import _flush_order_effort

    plan = eng.write(with_order())
    eng.approved("s1", plan)
    first = eng.state("s1")
    armings(first, seg.PLANNING)[-1]["booked"]["usd"] = 1.25
    _flush_order_effort(first, store=eng.store)
    eng.store.save(first)
    eng.open("s2", plan)
    eng.run("approve", session="s2", by="agent")
    planning = armings(eng.state("s2"), seg.PLANNING)
    assert len(planning) == 1
    assert planning[0]["carried"]["usd"] == 1.25 and planning[0]["booked"]["usd"] == 0.0


def test_an_order_less_plan_carries_nothing(eng):
    plan = eng.write(TWO_STAGE)
    eng.executing("s1", plan)
    eng.open("s2", plan)
    eng.run("approve", session="s2", by="user")
    assert not any(a["carried"]["usd"] for a in armings(eng.state("s2")))


# --- arming and booking in the engine ---------------------------------------------------


def test_stage_arming_opens_at_the_engines_selection(eng):
    eng.approved("s", eng.write(with_order()))
    state = eng.state("s")
    assert [a["segment"] for a in armings(state)] == [seg.PLANNING, seg.stage_segment(state.stage_uid(1))]
    first = stage_arming(state, 1)
    assert first["opened_by"] == seg.ACT_APPROVE_BY_USER and first["title"] == "Scaffold module"
    planning = armings(state, seg.PLANNING)[0]
    assert planning["spans"][-1]["end"] == first["spans"][0]["start"] == first["opened_at"]
    eng.run("partition", session="s")
    eng.run("next_stage", session="s")
    assert armings(eng.state("s")) == armings(state)


def test_arming_opens_at_selection_with_the_user_approved_or_declared_tier(eng):
    large = with_order().replace('executor = "spawn:developer"', 'executor = "spawn:developer"\ncost_tier = "large"', 1)
    eng.approved("s", eng.write(large))
    eng.run("partition", session="s")
    eng.run("next_stage", session="s")
    state = eng.state("s")
    arming = stage_arming(state, 1)
    assert arming["budget_source"] == seg.SOURCE_USER_APPROVED and arming["declared_tier"] == "large"
    assert arming["budget"] == effort.stage_budget("large", effort.Thresholds())
    state.effort_segments["approved_tiers"].clear()
    eng.store.save(state)
    assert eng.passed("s", 1)
    declared = stage_arming(eng.state("s"), 2)
    assert declared["budget_source"] == seg.SOURCE_DECLARED
    assert declared["budget"] == effort.stage_budget(declared["declared_tier"], effort.Thresholds())


def test_stage_actual_includes_linked_descendant_rows_and_minutes(eng):
    plan = eng.write(with_order())
    eng.executing("s", plan)
    state = eng.state("s")
    now = time.time()
    state.effort_child_runs = [{"started": now - 600, "ended": now, "stage_index": 1,
                                "child_session": "child", "dispatcher_pid": 1}]
    backdate(state, armings(state, seg.PLANNING)[0], now - 800)
    backdate(state, stage_arming(state, 1), now - 700)
    effort.refresh_subtree_spend(state, [row(plan, 4.0, now - 5, spawner="child")], ["child"], plan)
    effort.book_child_runs(state, now)
    actual = seg.arming_actual(stage_arming(state, 1))
    assert actual["usd"] == 4.0 and actual["minutes"] == pytest.approx(10.0)


def test_unfolded_parent_owns_nothing_its_substages_book(eng):
    eng.executing("s", eng.write(with_order()))
    child = eng.write(TWO_STAGE)
    eng.run("push_subplan", session="s", plan=child, task="child", originating_stage=1)
    eng.run("classify", session="s", architectural=True, changed_lines=200, wall_clock_min=60)
    eng.run("plan", session="s")
    eng.run("submit_plan", session="s", plan=child)
    eng.run("approve", session="s", by="user")
    eng.run("partition", session="s")
    eng.run("next_stage", session="s")
    state = eng.state("s")
    parent = seg.open_arming_of(state.effort_segments, seg.stage_segment(state.stage_uids["#1"]))
    effort.refresh_spend(state, [row(child, 3.0, time.time())], child)
    assert stage_arming(state, 1)["booked"]["usd"] == 3.0
    assert parent["booked"]["usd"] == 0.0


def test_closing_arming_opens_at_last_settlement_without_a_budget(eng):
    eng.executing("s", eng.write(with_order()))
    eng.passed("s", 1)
    assert not armings(eng.state("s"), seg.CLOSING)
    eng.run("next_stage", session="s")
    eng.passed("s", 2)
    state = eng.state("s")
    closing = armings(state, seg.CLOSING)
    assert len(closing) == 1 and closing[0]["budget"] is None and closing[0]["closed_at"] is None
    assert closing[0]["budget_source"] == seg.SOURCE_UNBUDGETED


def test_gap_after_settlement_books_to_the_next_current_segment(eng):
    from agentctl.cli_common import stamp_and_judge

    eng.executing("s", eng.write(with_order()))
    state = eng.state("s")
    first_uid = state.stage_uid(1)
    eng.passed("s", 1)
    state = eng.state("s")
    settled = seg.find_arming(state.effort_segments, armings(state, seg.stage_segment(first_uid))[0]["arming_id"])
    settled_at = settled["closed_at"]
    backdate(state, armings(state, seg.PLANNING)[0], settled_at - 400)
    backdate(state, settled, settled_at - 300)
    state.last_user_prompt_ts = settled_at - 60
    eng.store.save(state)
    assert stamp_and_judge("s", settled_at + 60, store=eng.store)
    state = eng.state("s")
    assert armings(state, seg.stage_segment(first_uid))[0]["booked"]["minutes"] == pytest.approx(1.0, abs=0.05)
    assert stage_arming(state, 2)["booked"]["minutes"] == pytest.approx(1.0, abs=0.05)


def test_gap_within_the_clock_skew_of_a_settlement_is_not_split():
    ledger, a, b = ledger_of_two()
    seg.book_interval(ledger, 199.5, 240.0, 1.0, "gap")
    assert a["booked"]["minutes"] == 0.0 and b["booked"]["minutes"] == 1.0


def test_every_replan_books_to_exactly_one_owner(eng):
    from agentctl.cli_common import _ensure_stage_arming, _rematch_stage_uids, _refresh_spend

    plan = eng.write(with_order())
    eng.executing("s", plan)
    state = eng.state("s")
    live = stage_arming(state, 1)
    t = live["opened_at"]
    new = load_plan(eng.write(with_order(inserted_ahead())), strict=False)
    _refresh_spend(state, eng.store, [row(plan, 1.0, t + 1)], plan)
    _rematch_stage_uids(state, new.stages)
    state.stages = new.stages
    state.plan_path = plan
    _refresh_spend(state, eng.store, [row(plan, 1.0, t + 1), row(plan, 2.0, t + 2)], plan)
    assert live["booked"]["usd"] == 3.0
    added = _ensure_stage_arming(state, state.stage(1), t + 3, "next_stage")
    _refresh_spend(state, eng.store,
                   [row(plan, 1.0, t + 1), row(plan, 2.0, t + 2), row(plan, 4.0, t + 4)], plan)
    assert added["booked"]["usd"] == 4.0 and live["booked"]["usd"] == 3.0
    assert total(state)["usd"] == 7.0


def test_outcome_cost_reconciles_the_rows_and_is_never_the_only_source(eng):
    plan = eng.write(with_order())
    eng.executing("s", plan)
    state = eng.state("s")
    uid = state.stage_uid(1)
    log = eng.log(row(plan, 1.5, stage_arming(state, 1)["opened_at"] + 0.5, stage=1))
    eng.passed("s", 1, log)
    state = eng.state("s")
    booked = armings(state, seg.stage_segment(uid))[0]["booked"]["usd"]
    assert booked == 1.5 == state.stage(1).outcome.cost_usd
    assert cost.attribute_stage(json.loads("[]"), plan, 1)["cost_usd"] in (0, 0.0, None)
    state.stage(1).outcome.cost_usd = 99.0
    assert total(state)["usd"] == 1.5, "an outcome's cost is read for a report, never booked"


def test_conservation_over_planning_replanned_unfolded_stages_and_closing(eng):
    from agentctl.cli_common import _rematch_stage_uids, _refresh_spend

    plan = eng.write(with_order())
    eng.open("s", plan)
    state = eng.state("s")
    planning_arming = state.effort_segments["armings"][0]
    backdate(state, planning_arming, planning_arming["opened_at"] - 1000)
    eng.store.save(state)
    rows = []

    def ingest(state, path, usd, ts):
        rows.append(row(path, usd, ts))
        return _refresh_spend(state, eng.store, [r for r in rows if r["plan_path"] == path], path)

    state = eng.state("s")
    ingest(state, plan, 1.0, time.time() - 500)
    planning = armings(state, seg.PLANNING)[0]
    assert planning["booked"]["usd"] == 1.0
    eng.store.save(state)
    eng.run("approve", session="s", by="user")
    eng.run("partition", session="s")
    eng.run("next_stage", session="s")

    state = eng.state("s")
    first = stage_arming(state, 1)
    written_while_live = first["opened_at"] + 0.01
    new = load_plan(eng.write(with_order(inserted_ahead())), strict=False)
    _rematch_stage_uids(state, new.stages)
    state.stages = new.stages
    eng.store.save(state)
    state = eng.state("s")
    assert state.stage_uid(2) is not None
    state.stage(2).outcome.status = StageStatus.ACTIVE.value
    state.stage(1).outcome.status = StageStatus.PASSED.value
    state.current_stage = 2
    eng.store.save(state)

    child = eng.write(TWO_STAGE)
    eng.run("push_subplan", session="s", plan=child, task="child", originating_stage=2)
    eng.run("classify", session="s", architectural=True, changed_lines=200, wall_clock_min=60)
    eng.run("plan", session="s")
    eng.run("submit_plan", session="s", plan=child)
    eng.run("approve", session="s", by="user")
    eng.run("partition", session="s")
    eng.run("next_stage", session="s")
    state = eng.state("s")
    substage = stage_arming(state, 1)
    ingest(state, child, 2.0, time.time())
    assert substage["booked"]["usd"] == 2.0
    state.stage(1).outcome.status = StageStatus.PASSED.value
    state.stage(2).outcome.status = StageStatus.PASSED.value
    state.current_stage = None
    state.resolution = GateRecord("resolution", armed=True, passed=True, by="tester")
    state.node = Node.RESOLVED.value
    eng.store.save(state)
    eng.run("pop_subplan", session="s")

    state = eng.state("s")
    ingest(state, plan, 4.0, written_while_live)
    assert seg.find_arming(state.effort_segments, first["arming_id"])["booked"]["usd"] == 4.0
    state.stage(3).outcome.status = StageStatus.PASSED.value
    from agentctl.cli_common import _settle_stage_arming

    _settle_stage_arming(state, state.stage(3), seg.ACT_PASSING_RECORD_RESULT, time.time())
    closing = armings(state, seg.CLOSING)[0]
    ingest(state, plan, 8.0, time.time() + 1)
    assert closing["booked"]["usd"] == 8.0
    conserved = seg.conserve(state.effort_segments)
    assert conserved["total"]["usd"] == sum(r["cost_usd"] for r in rows) == 15.0
    assert sum(a["usd"] for a in conserved["segments"].values()) == 15.0
    assert [line["segment"] for line in seg.timeline(state.effort_segments)][0] == seg.PLANNING


def test_row_at_the_first_approval_instant_books_to_the_first_stage_and_the_one_before_to_planning():
    ledger = {"session_floor": 100.0}
    planning = seg.open_arming(ledger, seg.PLANNING, None, seg.SOURCE_UNBUDGETED, 100.0, "t")
    seg.close_arming(ledger, planning["arming_id"], "t", 200.0, 5.0)
    first = seg.open_arming(ledger, "stage:a", {"usd": 1.0, "minutes": 10.0}, seg.SOURCE_DECLARED, 200.0, "t")
    assert seg.book(ledger, 199.999, 1.0, 0.0, "row") is planning
    assert seg.book(ledger, 200.0, 2.0, 0.0, "row") is first
    assert planning["booked"]["usd"] == 1.0 and first["booked"]["usd"] == 2.0
    assert seg.conserve(ledger)["total"]["usd"] == 3.0


def test_parked_arming_spans_own_rows_by_write_time():
    ledger = {"session_floor": 100.0}
    budget = {"usd": 1.0, "minutes": 10.0}
    live = seg.open_arming(ledger, "stage:l", budget, seg.SOURCE_DECLARED, 100.0, "t")
    seg.book(ledger, 110.0, 1.0, 0.0, "r1")
    seg.park_arming(ledger, live["arming_id"], 120.0)
    ahead = seg.open_arming(ledger, "stage:n", budget, seg.SOURCE_DECLARED, 120.0, "t")
    assert seg.current_arming(ledger) is ahead
    assert seg.book(ledger, 115.0, 2.0, 0.0, "r1-booked-during-the-parking") is live
    assert seg.book(ledger, 120.0, 0.0, 0.0, "at-the-parking-instant") is ahead

    seg.close_arming(ledger, ahead["arming_id"], "t", 140.0, 5.0)
    resumed = seg.resume_arming(ledger, live["arming_id"], 140.0)
    assert resumed is live
    assert [(s["start"], s["end"], s["end_act"]) for s in live["spans"]] == [
        (100.0, 120.0, "parked"), (140.0, None, None)]
    assert seg.book(ledger, 130.0, 4.0, 0.0, "r2-written-during-the-parking") is ahead
    assert seg.book(ledger, 150.0, 8.0, 0.0, "r3-written-after-the-resume") is live
    assert live["booked"]["usd"] == 1.0 + 2.0 + 8.0 and ahead["booked"]["usd"] == 4.0
    assert seg.current_arming(ledger) is seg.book(ledger, 160.0, 0.0, 0.0, "at-the-act-instant")

    kept = [dict(s) for s in live["spans"]]
    seg.park_arming(ledger, live["arming_id"], 170.0)
    seg.resume_arming(ledger, live["arming_id"], 180.0)
    assert len(live["spans"]) == 3 and live["spans"][0] == kept[0]
    assert live["spans"][1]["start"] == kept[1]["start"] and live["spans"][1]["end"] == 170.0
    assert live["spans"][2] == {"start": 180.0, "end": None, "end_act": None}
    assert seg.conserve(ledger)["total"]["usd"] == 15.0


def test_handoff_resumes_parked_arming_through_engine_acts(eng):
    from agentctl.cli_common import _refresh_spend, _rematch_stage_uids

    plan = eng.write(with_order())
    eng.executing("s", plan)
    eng.passed("s", 1)
    state = eng.state("s")
    live_uid = state.stage_uid(2)
    live = stage_arming(state, 2)
    assert live is not None and live["opened_by"] == seg.ACT_PASSING_RECORD_RESULT
    rows = []

    def write(usd):
        rows.append(row(plan, usd, time.time()))

    write(1.0)

    new = load_plan(eng.write(with_order(inserted_ahead())), strict=False)
    _rematch_stage_uids(state, new.stages)
    state.stages = new.stages
    state.stage(2).outcome.status = StageStatus.PASSED.value
    eng.store.save(state)
    state = eng.state("s")
    assert state.stage_uid(3) == live_uid and state.ready_stages()[0].index == 1

    eng.run("next_stage", session="s")
    state = eng.state("s")
    ahead = stage_arming(state, 1)
    assert ahead["opened_by"] == "next_stage"
    live = seg.find_arming(state.effort_segments, live["arming_id"])
    parked_at = ahead["opened_at"]
    assert live["closed_at"] is None and live["spans"][0]["end"] == parked_at
    assert live["spans"][0]["end_act"] == "parked"
    write(2.0)

    eng.passed("s", 1)
    state = eng.state("s")
    live = seg.find_arming(state.effort_segments, live["arming_id"])
    ahead = seg.find_arming(state.effort_segments, ahead["arming_id"])
    assert len(live["spans"]) == 2 and live["spans"][1]["end"] is None
    assert live["spans"][1]["start"] == ahead["closed_at"] == ahead["spans"][0]["end"]
    assert ahead["spans"][0]["start"] == parked_at
    assert [a["arming_id"] for a in armings(state, seg.stage_segment(live_uid))] == [live["arming_id"]]
    write(4.0)

    before = armings(state)
    eng.run("next_stage", session="s")
    assert armings(eng.state("s")) == before, "selecting the handed-to stage opens nothing"

    state = eng.state("s")
    _refresh_spend(state, eng.store, rows, plan)
    live = seg.find_arming(state.effort_segments, live["arming_id"])
    ahead = seg.find_arming(state.effort_segments, ahead["arming_id"])
    assert live["booked"]["usd"] == 1.0 + 4.0 and ahead["booked"]["usd"] == 2.0
    conserved = seg.conserve(state.effort_segments)
    assert conserved["total"]["usd"] == sum(r["cost_usd"] for r in rows) == 7.0


def test_user_approved_drop_of_armed_stage_closes_its_arming():
    budget = {"usd": 1.0, "minutes": 10.0}

    def drop_with(follower):
        ledger = {"session_floor": 100.0}
        before = seg.open_arming(ledger, "stage:k", budget, seg.SOURCE_DECLARED, 100.0, "t")
        seg.close_arming(ledger, before["arming_id"], "t", 150.0, 5.0)
        dropped = seg.open_arming(ledger, "stage:a", budget, seg.SOURCE_DECLARED, 150.0, "t")
        seg.close_arming(ledger, dropped["arming_id"], seg.ACT_APPROVE_BY_USER, 200.0, 5.0)
        next_arming = seg.open_arming(ledger, *follower, 200.0, seg.ACT_APPROVE_BY_USER)
        assert seg.book(ledger, 160.0, 1.0, 0.0, "ra-booked-after-the-drop") is dropped
        assert seg.book(ledger, 210.0, 2.0, 0.0, "rb") is next_arming
        assert dropped["closed_at"] == 200.0
        assert [(s["end"], s["end_act"]) for s in dropped["spans"]] == [(200.0, "closed")]
        assert not [a for a in ledger["armings"] if a["segment"] == "stage:a" and a["closed_at"] is None]
        assert seg.conserve(ledger)["total"]["usd"] == 3.0

    drop_with(("stage:b", budget, seg.SOURCE_DECLARED))
    drop_with((seg.CLOSING, None, seg.SOURCE_UNBUDGETED))


def test_user_approved_drop_of_a_parked_stage_closes_it_and_opens_nothing():
    budget = {"usd": 1.0, "minutes": 10.0}
    ledger = {"session_floor": 100.0}
    parked = seg.open_arming(ledger, "stage:l", budget, seg.SOURCE_DECLARED, 100.0, "t")
    seg.park_arming(ledger, parked["arming_id"], 120.0)
    active = seg.open_arming(ledger, "stage:n", budget, seg.SOURCE_DECLARED, 120.0, "t")
    seg.close_arming(ledger, parked["arming_id"], seg.ACT_APPROVE_BY_USER, 200.0, 5.0)
    assert parked["closed_at"] == 200.0
    assert [(s["end"], s["end_act"]) for s in parked["spans"]] == [(120.0, "parked")]
    assert seg.current_arming(ledger) is active
    assert seg.book(ledger, 210.0, 1.0, 0.0, "after-the-drop") is active
    assert not [a for a in ledger["armings"] if a["segment"] == seg.CLOSING]
    assert not [a for a in ledger["armings"] if a["segment"] == "stage:l" and a["closed_at"] is None]


def test_two_sessions_same_plan_path_carried_arming_counts_interval_row_once(eng):
    from agentctl.cli_common import _flush_order_effort, _refresh_spend

    plan = eng.write(with_order())
    eng.executing("s1", plan)
    first = eng.state("s1")
    carried_from = stage_arming(first, 1)["opened_at"]
    between = row(plan, 3.0, time.time())
    _refresh_spend(first, eng.store, [between], plan)
    _flush_order_effort(first, store=eng.store)
    eng.store.save(first)

    eng.open("s2", plan)
    assert eng.run("approve", session="s2", by="agent")["ok"]
    second = eng.state("s2")
    floor = second.effort_segments["session_floor"]
    adopted = stage_arming(second, 1)
    assert carried_from < between["ts"] < floor
    assert floor == armings(second, seg.PLANNING)[0]["opened_at"] != adopted["opened_at"]
    _refresh_spend(second, eng.store, [between], plan)
    assert total(second)["usd"] == 0.0
    assert total(first)["usd"] == 3.0
    assert total(first)["usd"] + total(second)["usd"] == between["cost_usd"]


# --- the single current stage -----------------------------------------------------------


def two_independent(eng: Eng, sid: str = "s") -> None:
    text = with_order().replace("depends_on = [1]", "depends_on = []")
    eng.executing(sid, eng.write(text))


def test_each_current_stage_writer_refuses_a_second_current_stage(eng):
    from agentctl import cli_control, cli_replan

    two_independent(eng)
    refused = eng.run("next_stage", session="s")
    assert refused["ok"] is False and refused["data"]["active_stage"] == 1
    assert eng.state("s").current_stage == 1

    state = eng.state("s")
    state.stage(2).outcome.status = StageStatus.ACTIVE.value
    ctx = SimpleNamespace(
        state=state, store=eng.store, args=None, new=None, reopening=set(), echo_advice=[], tier=None,
        closing_waiver=None, trigger_exit=None, replan_cause={}, prev_plan_sha256="",
        new_plan_sha256="", change_class="substantive")
    refusal = cli_replan._replan_apply_substantive(ctx)
    assert refusal.ok is False and refusal.data["active_stage"] == 2

    cli_control._restore_current_stage(state)
    assert state.current_stage is None, "two ACTIVE stages restore to none"

    plan = eng.write(with_order())
    eng.open("a", plan)
    parked = eng.state("a")
    parked.stage(1).outcome.status = StageStatus.ACTIVE.value
    eng.store.save(parked)
    refused = eng.run("approve", session="a", by="user")
    assert refused["ok"] is False and refused["data"]["active_stage"] == 1
    assert eng.state("a").node == Node.PLAN_READY.value


# --- schema 62 --------------------------------------------------------------------------


def test_schema_61_state_loads_under_schema_62(eng):
    plan = eng.write(with_order())
    eng.open("s", plan)
    state = eng.state("s")
    state.effort_actuals = {"spend_usd": 1.0, "active_minutes": 2.0}
    eng.store.save(state)
    eng.to_v61("s")
    loaded = eng.state("s")
    assert loaded.schema_version == 61
    assert loaded.effort_actuals == {"spend_usd": 1.0, "active_minutes": 2.0}
    assert loaded.effort_migrated["armed"] is False


def test_state_created_at_schema_62_is_never_stamped(eng):
    eng.open("s", eng.write(with_order()))
    assert eng.state("s").effort_migrated is None


def test_migration_assigns_every_stage_an_identity(eng):
    eng.executing("s", eng.write(with_order()))
    child = eng.write(TWO_STAGE)
    eng.run("push_subplan", session="s", plan=child, task="child", originating_stage=1)
    eng.run("classify", session="s", architectural=True, changed_lines=200, wall_clock_min=60)
    eng.run("plan", session="s")
    eng.run("submit_plan", session="s", plan=child)
    eng.to_v61("s")
    state = eng.state("s")
    assert state.stage_uids["#1"] and state.stage_uids["#2"]
    assert state.stage_uid(1) and state.stage_uid(2)
    assert len(set(state.stage_uids.values())) == 4


def test_migration_instant_is_stamped_at_the_first_saved_load_only(eng, monkeypatch):
    eng.open("s", eng.write(with_order()))
    path = eng.to_v61("s")
    before = path.read_text(encoding="utf-8")
    first_look = eng.state("s").effort_migrated["instant"]
    assert path.read_text(encoding="utf-8") == before, "a read-only load persists nothing"
    time.sleep(0.01)
    state = eng.state("s")
    assert state.effort_migrated["instant"] > first_look
    instant = state.effort_migrated["instant"]
    eng.store.save(state)
    time.sleep(0.01)
    assert eng.state("s").effort_migrated["instant"] == instant


def seeded_v61(eng: Eng, *, armed: bool, spend=4.0, minutes=20.0) -> tuple[str, str]:
    plan = eng.write(with_order())
    if armed:
        eng.executing("s", plan)
    else:
        eng.open("s", plan)
    state = eng.state("s")
    state.effort_actuals = {"spend_usd": spend, "active_minutes": minutes}
    state.effort_spend_seen = {plan: spend}
    eng.store.save(state)
    eng.to_v61("s")
    return "s", plan


def test_migration_opens_stretch_and_planning_armings(eng):
    sid, plan = seeded_v61(eng, armed=False)
    state = eng.state(sid)
    stretch, planning = armings(state)
    instant = state.effort_migrated["instant"]
    assert (stretch["opened_by"], stretch["budget_source"], stretch["opened_at"]) == (
        seg.OPENED_BY_MIGRATION, seg.SOURCE_STRETCH, 0.0)
    assert stretch["closed_at"] == instant and stretch["segment"] == seg.PLANNING
    assert planning["opened_at"] == instant and planning["budget_source"] == seg.SOURCE_UNBUDGETED
    assert planning["closed_at"] is None
    effort.refresh_spend(state, [row(plan, 4.0, instant - 1000), row(plan, 1.0, instant - 10)], plan)
    assert stretch["booked"]["usd"] == 5.0 and planning["booked"]["usd"] == 0.0


def test_migration_seeds_premigration_actuals_as_booked(eng):
    sid, _ = seeded_v61(eng, armed=False)
    stretch, planning = armings(eng.state(sid))
    assert stretch["booked"] == {"usd": 4.0, "minutes": 20.0}
    assert stretch["carried"] == {"usd": 0.0, "minutes": 0.0}
    assert planning["booked"] == {"usd": 0.0, "minutes": 0.0}

    other = Eng(eng.tmp / "armed")
    other.root.mkdir(parents=True)
    other.executing("a", other.write(with_order()))
    child = other.write(TWO_STAGE)
    other.run("push_subplan", session="a", plan=child, task="child", originating_stage=1)
    state = other.state("a")
    now = time.time()
    state.plan_stack[0].effort_actuals = {"spend_usd": 3.0, "active_minutes": 5.0}
    state.effort_actuals = {"spend_usd": 1.0, "active_minutes": 2.0}
    state.effort_child_runs = [{"started": now - 600, "ended": now - 300, "stage_index": 1,
                                "child_session": "c", "dispatcher_pid": 1}]
    other.store.save(state)
    other.to_v61("a")
    migrated = other.state("a")
    assert migrated.effort_migrated["armed"] is True
    window = seg.window_arming(migrated.effort_segments)
    assert window["segment"] == seg.PLANNING and window["opened_at"] == 0.0
    assert window["booked"]["usd"] == 4.0
    assert window["booked"]["minutes"] == pytest.approx(5.0 + 2.0 + 5.0)
    effort.book_child_runs(migrated, now)
    assert total(migrated)["minutes"] == pytest.approx(12.0), "the closed run books once"
    seg.book(migrated.effort_segments, now, 1.0, 1.0, "row")
    assert total(migrated) == pytest.approx({"usd": 5.0, "minutes": 13.0})


def test_refresh_after_migration_does_not_rebook_seeded_rows(eng):
    sid, plan = seeded_v61(eng, armed=False)
    state = eng.state(sid)
    instant = state.effort_migrated["instant"]
    seeded = [row(plan, 4.0, instant - 100)]
    effort.refresh_spend(state, seeded, plan)
    assert total(state)["usd"] == 4.0
    eng.store.save(state)
    state = eng.state(sid)
    effort.refresh_spend(state, seeded, plan)
    assert total(state)["usd"] == 4.0
    effort.refresh_spend(state, [*seeded, row(plan, 1.5, instant + 1)], plan)
    assert total(state)["usd"] == 5.5


def test_pre_approval_rows_of_a_migrated_session_book_to_the_planning_line(eng):
    sid, plan = seeded_v61(eng, armed=False, spend=0.0, minutes=0.0)
    state = eng.state(sid)
    state.effort_spend_seen = {}
    instant = state.effort_migrated["instant"]
    effort.refresh_spend(state, [row(plan, 2.0, instant - 50), row(plan, 3.0, instant + 1)], plan)
    assert seg.conserve(state.effort_segments)["segments"] == {
        seg.PLANNING: {"usd": 5.0, "minutes": 0.0}}


def test_migrated_planning_session_stretch_is_booked_and_never_judged(eng):
    sid, _ = seeded_v61(eng, armed=False, spend=500.0, minutes=5000.0)
    stretch = armings(eng.state(sid))[0]
    assert stretch["budget"] is None and stretch["booked"]["usd"] == 500.0
    assert seg.judge(stretch, 5.0, {"usd": 8.0, "minutes": 60.0}) == []


def test_migration_window_arming_owns_bookings_before_switch(eng):
    sid, plan = seeded_v61(eng, armed=True)
    state = eng.state(sid)
    window = seg.window_arming(state.effort_segments)
    assert window["budget_source"] == seg.SOURCE_WINDOW and window["opened_by"] == seg.OPENED_BY_MIGRATION
    effort.refresh_spend(state, [row(plan, 4.0, 10.0), row(plan, 2.0, time.time())], plan)
    assert window["booked"]["usd"] == 6.0
    assert len(armings(state)) == 1


def test_window_arming_open_blocks_stage_and_closing_armings(eng):
    sid, _ = seeded_v61(eng, armed=True)
    eng.passed(sid, 1)
    state = eng.state(sid)
    assert len(armings(state)) == 1
    assert not armings(state, seg.CLOSING)
    eng.run("next_stage", session=sid)
    eng.passed(sid, 2)
    state = eng.state(sid)
    assert len(armings(state)) == 1 and not state.effort_segments.get("retier")


def test_classify_on_an_auto_started_v61_state_opens_no_second_planning_arming(eng):
    eng.run("start", session="s", task="t", goal="g", done_criterion="dc")
    eng.to_v61("s")
    eng.run("classify", session="s", architectural=True, changed_lines=200, wall_clock_min=60)
    state = eng.state("s")
    open_ones = [a for a in armings(state, seg.PLANNING) if a["closed_at"] is None]
    assert len(open_ones) == 1


def test_cost_rows_read_iso_timestamps():
    assert effort.row_written_at({"ts": "2026-01-01T00:00:00+00:00"}) == pytest.approx(1767225600.0)
    assert effort.row_written_at({"ts": 5.0}) == 5.0
    assert effort.row_written_at({}) == float("inf")
