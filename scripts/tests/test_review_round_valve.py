"""The per-task `review_rounds` counter that feeds the plan-review round-release valve.

Difficulty: `plan_review_rounds` is zeroed at every approve/replan and a pair record
(`--scope topo:<pair>`) never advanced it, so a task that spent many reviewed plan
versions — an 18-pair topological review run in one real task — never reached the
`effort-replan-absolute` threshold and the valve stayed silent. `review_rounds` is a
per-task accumulator axis nothing resets, counted once per reviewed plan VERSION (pair
records, deduped by plan digest) or once per thinker record (whole-plan / stage).

The replay tests are selected by `-k replay` as the negative control against the tree
before this change, so every new symbol is reached through the CLI inside the test
body: on the old tree they fail on an assertion (pytest exit 1), not on an import.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, gates, task_accumulator
from agentctl.config import Thresholds
from agentctl.plan import load_plan, review_pairs

AGENTCTL_DIR = Path(__file__).resolve().parents[1] / "agentctl"


def ns(**kw):
    return Namespace(**kw)


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")


def _sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _plan(fixtures_dir, tmp_path, name="p.toml") -> str:
    dest = tmp_path / name
    dest.write_bytes((fixtures_dir / "plan_two_stage.toml").read_bytes())
    return str(dest)


def _new_version(plan, n) -> None:
    """Change the plan bytes so the plan digest differs from every earlier version."""
    p = Path(plan)
    p.write_text(p.read_text() + f"\n# revision {n}\n")


def _start(store, sid, task="demo"):
    cli.cmd_start(ns(session=sid, task=task, goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)


def _to_plan_ready(store, sid, plan, task="demo"):
    _start(store, sid, task)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)


def _pairs(plan):
    return review_pairs(load_plan(plan))


def _pair_record(store, sid, plan, pair, verdict="revise", reviewer="thinker", note=""):
    d = cli.cmd_plan_review(
        ns(session=sid, target=plan, scope=f"topo:{pair}", verdict=verdict,
           reviewer=reviewer, concerns=["a pair concern"] if verdict == "revise" else None,
           note=note, plan_digest=_sha(plan), regression_command=None),
        store=store)
    assert d.ok, d.detail
    return d


def _whole_record(store, sid, plan, verdict="revise"):
    d = cli.cmd_plan_review(
        ns(session=sid, target=plan, verdict=verdict, reviewer="thinker",
           concerns=["a concern"] if verdict == "revise" else None, note="",
           plan_digest=_sha(plan)),
        store=store)
    if verdict == "pass":
        assert d.ok, d.detail
    return d


def _rounds(sid, store):
    return task_accumulator.get(store.load(sid).task_id)["per_axis_totals"]["review_rounds"]


def _threshold() -> int:
    return Thresholds().effort_replan_absolute()


def _release_active(store, sid) -> bool:
    return gates.plan_review_round_release_active(store.load(sid))


# --- replay: the shape of the real task that never saw the valve ----------------

def test_replay_c5ceb718_shape(store, fixtures_dir, tmp_path, gate_on):
    """Many pair verdicts over `threshold` distinct plan versions, approve/replan
    resets in between: `plan_review_rounds` stays far below the threshold (pair records
    never advanced it) yet the task's review_rounds reaches it and the valve fires."""
    sid = "rv-replay"
    plan = _plan(fixtures_dir, tmp_path)
    _to_plan_ready(store, sid, plan)
    pairs = _pairs(plan)
    assert len(pairs) >= 2
    thr = _threshold()
    for version in range(thr):
        assert not _release_active(store, sid), f"valve fired early at version {version}"
        for pair in pairs:
            _pair_record(store, sid, plan, pair)
        if version < thr - 1:
            _new_version(plan, version)
            cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    state = store.load(sid)
    assert state.plan_review_rounds < thr
    assert state.review_rounds == thr
    assert _release_active(store, sid)


def test_replay_same_version_counts_one_round(store, fixtures_dir, tmp_path, gate_on):
    """Every pair of one plan version is ONE round, however many pairs are reviewed."""
    sid = "rv-one"
    plan = _plan(fixtures_dir, tmp_path)
    _to_plan_ready(store, sid, plan)
    for pair in _pairs(plan):
        _pair_record(store, sid, plan, pair)
    assert _rounds(sid, store) == 1
    assert store.load(sid).review_rounds == 1
    _new_version(plan, 1)
    for pair in _pairs(plan):
        _pair_record(store, sid, plan, pair)
    assert _rounds(sid, store) == 2


def test_replay_same_digest_two_stale_callers_count_once(store, fixtures_dir, tmp_path, gate_on):
    """Two sessions that each loaded the task's state before either wrote, and record a
    pair against the same plan digest, count one round: compare-and-increment runs
    under the accumulator's lock, not against the caller's stale snapshot."""
    plan = _plan(fixtures_dir, tmp_path)
    _to_plan_ready(store, "rv-a", plan, task="shared")
    _start(store, "rv-b", "shared")
    a, b = store.load("rv-a"), store.load("rv-b")
    digest = _sha(plan)
    for state in (a, b):
        cli._count_review_round(state, digest)
    assert a.review_rounds == 1 and b.review_rounds == 1
    assert task_accumulator.get("shared")["per_axis_totals"]["review_rounds"] == 1


def test_replay_release_stays_active_after_replan(store, fixtures_dir, tmp_path, gate_on):
    """approve/replan zero plan_review_rounds; review_rounds is not reset by them, so a
    spent valve stays spent across a replan."""
    sid = "rv-replan"
    plan = _plan(fixtures_dir, tmp_path)
    _to_plan_ready(store, sid, plan)
    thr = _threshold()
    for version in range(thr):
        _pair_record(store, sid, plan, _pairs(plan)[0])
        _new_version(plan, version)
    assert _release_active(store, sid)
    _whole_record(store, sid, plan, verdict="pass")
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    after_approve = store.load(sid)
    assert after_approve.plan_review_rounds == 0
    assert after_approve.review_rounds >= thr
    assert gates.plan_review_round_release_active(after_approve)


# --- replay: ts on history events ---------------------------------------------------

def test_replay_ts_events_carry_utc_timestamp(store, fixtures_dir, tmp_path, gate_on):
    sid = "rv-ts"
    plan = _plan(fixtures_dir, tmp_path)
    _to_plan_ready(store, sid, plan)
    history = store.load(sid).history
    assert history
    for event in history:
        ts = event.get("ts")
        assert ts, f"event without ts: {event}"
        parsed = dt.datetime.fromisoformat(ts)
        assert parsed.utcoffset() == dt.timedelta(0)


def test_replay_ts_old_events_stay_unstamped(store, tmp_path):
    """An event already in the file at load time is never stamped with the later save
    time; only events appended since the load are."""
    sid = "rv-ts-old"
    _start(store, sid)
    path = store.path(sid)
    import json
    raw = json.loads(path.read_text())
    for event in raw["history"]:
        event.pop("ts", None)
    path.write_text(json.dumps(raw))
    state = store.load(sid)
    state.log("note_after_load")
    store.save(state)
    history = store.load(sid).history
    assert all("ts" not in e for e in history[:-1])
    assert "ts" in history[-1]


@pytest.mark.parametrize("if_absent", [False, True])
def test_replay_ts_first_event_of_new_session(store, if_absent):
    sid = f"rv-ts-first-{if_absent}"
    cli.cmd_start(ns(session=sid, task="demo", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0, if_absent=if_absent),
                  store=store)
    first = store.load(sid).history[0]
    assert first["event"] == "start"
    assert dt.datetime.fromisoformat(first["ts"]).utcoffset() == dt.timedelta(0)
    cli.cmd_classify(ns(session=sid, chat=True, changed_lines=None, files=None,
                        wall_clock_min=None, tracker_key=None, architectural=False,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    assert "ts" in store.load(sid).history[-1]


# --- the counter's contract ----------------------------------------------------------

def test_review_rounds_survives_approve_and_replan_and_second_session(
        store, fixtures_dir, tmp_path, gate_on):
    sid = "rv-surv"
    plan = _plan(fixtures_dir, tmp_path)
    _to_plan_ready(store, sid, plan)
    _whole_record(store, sid, plan, verdict="pass")
    assert store.load(sid).review_rounds == 1
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    assert store.load(sid).review_rounds == 1
    _start(store, "rv-surv-2")
    assert store.load("rv-surv-2").review_rounds == 0  # mirror is filled at first _require
    cli.cmd_classify(ns(session="rv-surv-2", chat=True, changed_lines=None, files=None,
                        wall_clock_min=None, tracker_key=None, architectural=False,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    assert _rounds("rv-surv-2", store) == 1


def test_second_session_does_not_double_count_a_counted_digest(
        store, fixtures_dir, tmp_path, gate_on):
    plan = _plan(fixtures_dir, tmp_path)
    _to_plan_ready(store, "rv-d1", plan, task="dedupe")
    pair = _pairs(plan)[0]
    _pair_record(store, "rv-d1", plan, pair)
    _to_plan_ready(store, "rv-d2", plan, task="dedupe")
    _pair_record(store, "rv-d2", plan, pair)
    assert task_accumulator.get("dedupe")["per_axis_totals"]["review_rounds"] == 1
    task_accumulator.reset("dedupe")
    _pair_record(store, "rv-d2", plan, pair)
    assert task_accumulator.get("dedupe")["per_axis_totals"]["review_rounds"] == 1


def test_compose_override_and_refused_records_do_not_count(
        store, fixtures_dir, tmp_path, gate_on):
    sid = "rv-nocount"
    plan = _plan(fixtures_dir, tmp_path)
    _to_plan_ready(store, sid, plan)
    pair = _pairs(plan)[0]
    # an override is a decision, not a review round
    _pair_record(store, sid, plan, pair, verdict="override", reviewer="user", note="accepted")
    # the engine's own composition record is not a review
    assert not cli._is_counted_review("pass", cli.TOPOLOGICAL_COMPOSITION_REVIEWER)
    assert not cli._is_counted_review("override", "thinker")
    assert cli._is_counted_review("revise", "thinker")
    # a refused record (digest of other bytes) leaves the count where it was
    d = cli.cmd_plan_review(
        ns(session=sid, target=plan, scope=f"topo:{pair}", verdict="pass",
           reviewer="thinker", concerns=None, note="", plan_digest="0" * 64,
           regression_command=None),
        store=store)
    assert not d.ok
    assert _rounds(sid, store) == 0


def test_whole_plan_and_stage_records_each_add_one_and_a_pair_after_adds_nothing(
        store, fixtures_dir, tmp_path, gate_on):
    sid = "rv-kinds"
    plan = _plan(fixtures_dir, tmp_path)
    _to_plan_ready(store, sid, plan)
    _whole_record(store, sid, plan, verdict="revise")
    assert _rounds(sid, store) == 1
    _whole_record(store, sid, plan, verdict="revise")
    assert _rounds(sid, store) == 2  # a second thinker record on the same bytes is a round
    cli.cmd_plan_review(
        ns(session=sid, target=plan, scope="stage:1", verdict="revise", reviewer="thinker",
           concerns=["stage concern"], note="", plan_digest=_sha(plan)),
        store=store)
    assert _rounds(sid, store) == 3
    # the pair record lands on a digest the whole-plan record already counted
    _pair_record(store, sid, plan, _pairs(plan)[0])
    assert _rounds(sid, store) == 3


def test_valve_uses_the_larger_of_the_two_counters(gate_on):
    from agentctl.state import SessionState
    thr = _threshold()
    low = SessionState(session_id="s", task_id="t", weight_class="SUBSTANTIVE",
                       plan_path="/p.toml", plan_verified=True,
                       plan_review_rounds=thr - 1, review_rounds=0)
    assert not gates.plan_review_round_release_active(low)
    high = SessionState(session_id="s", task_id="t", weight_class="SUBSTANTIVE",
                        plan_path="/p.toml", plan_verified=True,
                        plan_review_rounds=0, review_rounds=thr)
    assert gates.plan_review_round_release_active(high)


def test_no_rule_of_three_wording_in_engine_sources():
    spellings = ("Rule-of-Three", "Rule of Three", "Rule-of Three", "Rule of-Three")
    offenders = [p.name for p in sorted(AGENTCTL_DIR.glob("*.py"))
                 if any(s in p.read_text(encoding="utf-8") for s in spellings)]
    assert offenders == []
