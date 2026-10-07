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
from agentctl.state import Node, SessionState
from conftest import STAGE_OBSERVATIONS

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
           reviewer=reviewer, concerns=["blocking: a pair concern"] if verdict == "revise" else None,
           note=note, plan_digest=_sha(plan), regression_command=None),
        store=store)
    assert d.ok, d.detail
    return d


def _whole_record(store, sid, plan, verdict="revise"):
    d = cli.cmd_plan_review(
        ns(session=sid, target=plan, verdict=verdict, reviewer="thinker",
           concerns=["blocking: a concern"] if verdict == "revise" else None, note="",
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


def _race_two_counters(add, root, monkeypatch):
    """Release two threads past a barrier into `add(..., count_if_digest_differs=)` for the
    SAME digest, with the accumulator's parse step stretched so a read that happens
    before the other caller's write is a stale read. Returns the stored total."""
    import threading
    import time

    real_coerce = task_accumulator._coerce

    def slow_coerce(raw, task_id):
        out = real_coerce(raw, task_id)
        time.sleep(0.15)
        return out

    monkeypatch.setattr(task_accumulator, "_coerce", slow_coerce)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def caller():
        try:
            barrier.wait(timeout=5)
            add("race", "review_rounds", 1, count_if_digest_differs="d" * 64, root=root)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=caller) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not errors, errors
    monkeypatch.setattr(task_accumulator, "_coerce", real_coerce)
    return task_accumulator.get("race", root=root)["per_axis_totals"]["review_rounds"]


def _split_read_then_write_add(task_id, axis, count, *, root, count_if_digest_differs, **_):
    """The defective shape the real `add` must not have: the digest is read OUTSIDE the
    lock and the decision is made on that read, then the increment is written under it."""
    path = task_accumulator._path(task_id, root)
    seen = (task_accumulator._coerce(path.read_text(encoding="utf-8"), task_id)
            if path.exists() else task_accumulator._empty(task_id))
    with task_accumulator._FileLock(path):
        data = (task_accumulator._coerce(path.read_text(encoding="utf-8"), task_id)
                if path.exists() else task_accumulator._empty(task_id))
        if seen["review_rounds_last_sha256"] != count_if_digest_differs:
            data["per_axis_totals"][axis] += count
        data["review_rounds_last_sha256"] = count_if_digest_differs
        task_accumulator._write_atomic(path, data)


def test_replay_same_digest_two_stale_callers_count_once(tmp_path, monkeypatch):
    """Two callers released together on one digest — each holding a snapshot taken before
    either wrote — count ONE round: compare-and-increment is a single locked step."""
    assert _race_two_counters(task_accumulator.add, tmp_path, monkeypatch) == 1


def test_replay_race_harness_catches_a_split_read_then_write(tmp_path, monkeypatch):
    """The control for the test above: the same harness over a read-then-write variant
    counts the round twice, so the single-round result is not an artefact of timing."""
    assert _race_two_counters(_split_read_then_write_add, tmp_path, monkeypatch) == 2


def _refined_plan(fixtures_dir, tmp_path) -> str:
    dest = tmp_path / "refined.toml"
    dest.write_bytes((fixtures_dir / "plan_two_stage_refined.toml").read_bytes())
    return str(dest)


def _drive_to_executing(store, sid, plan, *, record_pass=True) -> None:
    if record_pass:
        _whole_record(store, sid, plan, verdict="pass")
    d = cli.cmd_approve(ns(session=sid, by="user"), store=store)
    assert d.ok, d.detail
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)
    assert store.load(sid).node == Node.EXECUTING.value


def test_replay_release_stays_active_after_replan(store, fixtures_dir, tmp_path, gate_on):
    """Approve and a real replan zero plan_review_rounds; review_rounds is not reset by
    either, so a valve that has fired stays fired across them."""
    sid = "rv-replan"
    plan = _plan(fixtures_dir, tmp_path)
    _to_plan_ready(store, sid, plan)
    thr = _threshold()
    for version in range(thr):
        _pair_record(store, sid, plan, _pairs(plan)[0])
        if version < thr - 1:
            _new_version(plan, version)
            cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    assert _release_active(store, sid)
    _drive_to_executing(store, sid, plan)
    after_approve = store.load(sid)
    assert after_approve.plan_review_rounds == 0
    assert after_approve.review_rounds >= thr
    assert gates.plan_review_round_release_active(after_approve)

    refined = _refined_plan(fixtures_dir, tmp_path)
    cli.cmd_plan_review(ns(session=sid, verdict="pass", reviewer="thinker", concerns=None,
                           note="", target=refined, plan_digest=_sha(refined)), store=store)
    d = cli.cmd_replan(ns(session=sid, plan=refined), store=store)
    assert d.ok, d.detail
    assert [e for e in store.load(sid).history if e["event"] == "replan"]
    after_replan = store.load(sid)
    assert after_replan.plan_review_rounds == 0
    assert after_replan.review_rounds >= thr
    assert gates.plan_review_round_release_active(after_replan)


# --- replay: ts on history events ---------------------------------------------------

def _assert_utc_ts(event) -> None:
    ts = event.get("ts")
    assert ts, f"event without ts: {event}"
    assert dt.datetime.fromisoformat(ts).utcoffset() == dt.timedelta(0)


def test_replay_ts_events_carry_utc_timestamp(store, fixtures_dir, tmp_path, gate_on):
    """submit_plan, plan_pair_review, replan and verify_final — driven by real commands —
    and every other event on the way each carry an ISO-8601 UTC `ts`."""
    sid = "rv-ts"
    plan = _plan(fixtures_dir, tmp_path)
    _to_plan_ready(store, sid, plan)
    _pair_record(store, sid, plan, _pairs(plan)[0])
    _drive_to_executing(store, sid, plan)
    refined = _refined_plan(fixtures_dir, tmp_path)
    cli.cmd_plan_review(ns(session=sid, verdict="pass", reviewer="thinker", concerns=None,
                           note="", target=refined, plan_digest=_sha(refined)), store=store)
    d = cli.cmd_replan(ns(session=sid, plan=refined), store=store)
    assert d.ok, d.detail
    for observation in STAGE_OBSERVATIONS[:2]:
        cli.cmd_record_result(ns(session=sid, status="passed", actual="ok",
                                 control="reviewed: ok", observation=observation),
                              store=store)
        cli.cmd_next_stage(ns(session=sid), store=store)
    d = cli.cmd_verify_final(ns(session=sid), store=store)
    assert d.ok, d.detail
    history = store.load(sid).history
    assert {"submit_plan", "plan_pair_review", "replan", "verify_final"} <= {e["event"] for e in history}
    for event in history:
        _assert_utc_ts(event)


def test_replay_ts_copies_of_a_loaded_state_never_stamp_old_events(store, tmp_path):
    """The boundary between old and new events travels with every way a state object is
    rebuilt: a JSON round-trip or a `dataclasses.replace` copy of an unstamped, loaded
    state must not give its pre-existing events the save time."""
    import dataclasses
    import json

    sid = "rv-ts-copy"
    _start(store, sid)
    path = store.path(sid)
    raw = json.loads(path.read_text())
    for event in raw["history"]:
        event.pop("ts", None)
    path.write_text(json.dumps(raw))
    loaded = store.load(sid)
    copies = (
        SessionState.from_json(loaded.to_json()),
        dataclasses.replace(loaded, goal="copy"),
    )
    assert "_loaded_history_len" not in loaded.to_json()
    for copy in copies:
        assert copy._loaded_history_len == len(loaded.history)
        copy.log("note_after_copy")
        store.save(copy)
        history = store.load(sid).history
        assert all("ts" not in e for e in history if e["event"] != "note_after_copy")
        assert "ts" in history[-1]


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
    _drive_to_executing(store, sid, plan, record_pass=False)
    assert store.load(sid).review_rounds == 1
    refined = _refined_plan(fixtures_dir, tmp_path)
    cli.cmd_plan_review(ns(session=sid, verdict="pass", reviewer="thinker", concerns=None,
                           note="", target=refined, plan_digest=_sha(refined)), store=store)
    assert store.load(sid).review_rounds == 2
    d = cli.cmd_replan(ns(session=sid, plan=refined), store=store)
    assert d.ok, d.detail
    assert store.load(sid).review_rounds == 2
    _start(store, "rv-surv-2")
    assert store.load("rv-surv-2").review_rounds == 0  # mirror is filled at first _require
    cli.cmd_classify(ns(session="rv-surv-2", chat=True, changed_lines=None, files=None,
                        wall_clock_min=None, tracker_key=None, architectural=False,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    assert _rounds("rv-surv-2", store) == 2


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
           concerns=["blocking: stage concern"], note="", plan_digest=_sha(plan)),
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
