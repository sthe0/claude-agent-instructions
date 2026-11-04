"""A parent stage's wall-clock includes the time its linked child ran, counted exactly once.

*Difficulty removed:* the wall-clock actual is the sum of capped prompt gaps, so while a
dispatched child works (the parent blocked in `dispatch`, no prompt arriving) the clock
books at most one capped gap — a long delegated overrun stays invisible to the divergence
trigger, and a gap that DID straddle the child would be booked by the hook AND by the
child's own interval if both were summed naively.

`cmd_dispatch` records an interval per linked spawn in `effort_child_runs` (persisted
before the blocking call, closed after it returns or raises); `effort.actual` adds the
union of those intervals to `active_minutes`; the prompt hook books only the part of a gap
the intervals do not cover; and an interval left open by a dead dispatcher is closed at its
child's last state write. The scenarios drive the real commands (`effort-check`,
`dispatch`) and the real hook. Behavioural assertions are exact or banded equalities whose
message starts `BEHAVIOUR:`; setup assertions deliberately do not carry that prefix.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, cli_dispatch, effort
from agentctl.config import Thresholds
from agentctl.dispatch import RunResult
from agentctl.state import SessionState

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = SCRIPTS_DIR / "hook-engine-start.py"
MIN = 60.0


@pytest.fixture
def fixtures_dir(venued_fixtures_dir):
    return venued_fixtures_dir


def ns(**kw):
    return Namespace(**kw)


@pytest.fixture(autouse=True)
def _no_replan_authorization_gate(monkeypatch):
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")


def _to_executing(store, sid, plan):
    cli.cmd_start(ns(session=sid, task="demo-two-stage", goal="", done_criterion="",
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


@pytest.fixture
def parent(store, fixtures_dir):
    _to_executing(store, "P", str(fixtures_dir / "plan_two_stage.toml"))
    return "P"


def _run(started, ended, *, child="C", pid=None, stage=1):
    return {"child_session": child, "stage_index": stage, "started": started,
            "ended": ended, "dispatcher_pid": pid if pid is not None else os.getpid()}


def _put(store, sid, runs=None, **actuals):
    """Set the parent's child runs and effort actuals on disk. `effort_child_runs` is set
    as a plain attribute so the same setup runs on a tree that has no such field."""
    state = store.load(sid)
    if runs is not None:
        state.effort_child_runs = runs
    state.effort_actuals.update(actuals)
    store.save(state)


def _scales(store, sid):
    d = cli.cmd_effort_check(ns(session=sid, cost_log=None), store=store)
    assert d.ok is True, d.detail
    return d.data


def _wall_minutes(store, sid):
    row = next(s for s in _scales(store, sid)["scales"] if s["scale"] == effort.SCALE_WALL_CLOCK)
    return row["actual"]


def _dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _load_hook():
    spec = importlib.util.spec_from_file_location("hook_engine_start_wall_clock", HOOK_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _hook_state(monkeypatch, tmp_path, session, **fields):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(tmp_path / "root"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    state_dir = tmp_path / "root" / "agentctl" / "state"
    state_dir.mkdir(parents=True)
    path = state_dir / f"{session}.json"
    path.write_text(json.dumps(fields), encoding="utf-8")
    return path


def test_child_run_wall_clock_rolls_up(store, parent):
    base = time.time() - 7200
    own, ran = 3.0, 10.0
    _put(store, parent, [_run(base, base + ran * MIN)], active_minutes=own)
    minutes = _wall_minutes(store, parent)
    assert minutes == pytest.approx(own + ran), (
        f"BEHAVIOUR: the wall-clock actual is the session's own {own} active minutes plus "
        f"the {ran} minutes its child ran = {own + ran}, got {minutes}"
    )


def test_prompt_gap_covered_by_child_run_counted_once(store, parent, monkeypatch, tmp_path):
    hook = _load_hook()
    gap, lead, ran = 25.0, 2.0, 20.0
    previous = time.time() - gap * MIN
    run_start = previous + lead * MIN
    runs = [_run(run_start, run_start + ran * MIN)]
    path = _hook_state(monkeypatch, tmp_path, "S", last_user_prompt_ts=previous,
                       effort_actuals={"active_minutes": 0.0}, effort_child_runs=runs)
    now = previous + gap * MIN
    hook._stamp_prompt_ts("S", now)
    booked = json.loads(path.read_text(encoding="utf-8"))["effort_actuals"]["active_minutes"]
    assert booked == pytest.approx(gap - ran), (
        f"BEHAVIOUR: of a {gap}-minute prompt gap, {ran} minutes lie inside a child run and "
        f"are counted by the run itself, so the hook books only the other {gap - ran}, "
        f"got {booked}"
    )
    total = booked + effort.union_seconds([(run_start, run_start + ran * MIN)]) / MIN
    assert total == pytest.approx(gap), (
        f"BEHAVIOUR: own minutes plus the child run equal the real elapsed {gap}, got {total}"
    )

    def _broken(*_a, **_k):
        raise RuntimeError("helper unavailable")

    monkeypatch.setattr(effort, "child_run_intervals", _broken)
    path.write_text(json.dumps({"last_user_prompt_ts": previous,
                                "effort_actuals": {"active_minutes": 0.0},
                                "effort_child_runs": runs}), encoding="utf-8")
    hook._stamp_prompt_ts("S", now)
    fallback = json.loads(path.read_text(encoding="utf-8"))["effort_actuals"]["active_minutes"]
    assert fallback == pytest.approx(gap), (
        f"BEHAVIOUR: a covered-time helper that fails leaves the whole gap booked "
        f"(covered = 0), got {fallback}"
    )


def test_grandchild_run_not_added_to_parent(store, parent):
    base = time.time() - 7200
    _put(store, parent, [_run(base, base + 10 * MIN)])
    grandchild = SessionState(session_id="C", task_id="c-task",
                              parent_link={"parent_session": "P", "stage_index": 1, "depth": 1})
    grandchild.effort_child_runs = [_run(base + MIN, base + 4 * MIN, child="G")]
    store.save(grandchild)
    minutes = _wall_minutes(store, parent)
    assert minutes == pytest.approx(10.0), (
        f"BEHAVIOUR: the parent counts only its own dispatch (10.0 minutes); the child's "
        f"own run of a grandchild, inside that span, adds nothing, got {minutes}"
    )


def test_running_child_counts_until_now(store, parent):
    started = time.time() - 5 * MIN
    _put(store, parent, [_run(started, None, pid=os.getpid())])
    minutes = _wall_minutes(store, parent)
    assert 4.9 < minutes < 5.6, (
        f"BEHAVIOUR: a child still running (its dispatcher alive) counts from its start "
        f"to now = about 5.0 minutes, got {minutes}"
    )


def test_wall_clock_divergence_fires_on_child_overrun(store, parent):
    assert _scales(store, parent)["would_fire"] is None, "setup: a fresh session must not fire"
    estimate = store.load(parent).effort_estimate[effort.SCALE_WALL_CLOCK]
    overrun = Thresholds().effort_divergence_multiple() + 1
    base = time.time() - 24 * 3600
    _put(store, parent, [_run(base, base + overrun * estimate * MIN)])
    fired = _scales(store, parent)["would_fire"]
    assert fired == effort.SCALE_WALL_CLOCK, (
        f"BEHAVIOUR: a child that ran {overrun}x the plan's wall-clock estimate "
        f"({estimate} min), past the divergence multiple, fires the wall-clock divergence, "
        f"got {fired!r}"
    )


def test_session_without_child_runs_keeps_base_minutes(store, parent):
    _put(store, parent, [], active_minutes=7.5)
    minutes = _wall_minutes(store, parent)
    assert minutes == pytest.approx(7.5), (
        f"BEHAVIOUR: a session that dispatched no linked child keeps its own active "
        f"minutes unchanged (7.5), got {minutes}"
    )


def _child(store, sid, *, written, born=None, parent="P", stage=1, runs=None):
    """A linked child's state file, last written at `written`; `born` (a history stamp)
    dates its creation earlier when it is a leftover from an older dispatch; `runs` are
    the child runs it recorded itself."""
    state = SessionState(session_id=sid, task_id=f"{sid}-task",
                         parent_link={"parent_session": parent, "stage_index": stage, "depth": 1})
    if runs:
        state.effort_child_runs = runs
    if born is not None:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(born))
        state.history = [{"event": "start", "ts": stamp}]
    store.save(state)
    os.utime(store.path(sid), (written, written))


def test_orphaned_open_child_run_is_closed(store, parent):
    started = time.time() - 3600
    ran = 5.0
    _child(store, "C", written=started + ran * MIN)
    dead = _dead_pid()
    _put(store, parent, [_run(started, None, child=None, pid=dead)])
    minutes = _wall_minutes(store, parent)
    assert minutes == pytest.approx(ran), (
        f"BEHAVIOUR: an open run (its child not yet attributed) whose dispatcher died "
        f"ends at its child's last state write ({ran} minutes after it began), not at "
        f"now, got {minutes}"
    )
    store.path("C").unlink()
    _child(store, "E", written=started - 10 * MIN)
    _put(store, parent, [_run(started, None, child=None, pid=dead)])
    unfound = _wall_minutes(store, parent)
    assert unfound == pytest.approx(0.0), (
        f"BEHAVIOUR: a dead run whose child cannot be found (the only candidate was last "
        f"written before the run began) ends at its start and adds 0.0, got {unfound}"
    )


def test_crashed_dispatcher_run_is_counted_once_across_hook_and_effort_check(
        store, parent, monkeypatch):
    hook = _load_hook()
    monkeypatch.setattr(hook.config_root, "resolve_agentctl_state_file",
                        lambda sid: store.path(sid))
    base = time.time() - 3600
    prompt_before, child_wrote, prompt_after = base - 1, base + 60, base + 70
    _child(store, "C", written=child_wrote)
    _put(store, parent, [_run(base, None, child=None, pid=_dead_pid())],
         active_minutes=0.0)
    state = store.load(parent)
    state.last_user_prompt_ts = prompt_before
    store.save(state)
    hook._stamp_prompt_ts(parent, prompt_after)
    total = _wall_minutes(store, parent)
    elapsed = (prompt_after - prompt_before) / MIN
    assert total == pytest.approx(elapsed), (
        f"BEHAVIOUR: the session was busy from the prompt to the next prompt "
        f"({elapsed:.4f} min) and the dispatcher died mid-run: the hook books the gap "
        f"outside the child's span and effort-check the span, once, got {total:.4f}"
    )
    run = store.load(parent).effort_child_runs[0]
    assert (run["child_session"], run["ended"]) == ("C", child_wrote), (
        f"BEHAVIOUR: the hook leaves the dead run closed at its child's last write, got {run}"
    )


def test_prompt_gap_during_live_dispatch_books_only_before_the_run(monkeypatch, tmp_path):
    hook = _load_hook()
    gap, lead = 10.0, 2.0
    previous = time.time() - gap * MIN
    run = _run(previous + lead * MIN, None, child=None, pid=os.getpid())
    path = _hook_state(monkeypatch, tmp_path, "S", last_user_prompt_ts=previous,
                       effort_actuals={"active_minutes": 0.0}, effort_child_runs=[run])
    hook._stamp_prompt_ts("S", previous + gap * MIN)
    booked = json.loads(path.read_text(encoding="utf-8"))["effort_actuals"]["active_minutes"]
    assert booked == pytest.approx(lead), (
        f"BEHAVIOUR: a prompt arriving while the dispatcher is alive covers the run up to "
        f"the prompt, so only the {lead} minutes before the run are booked, got {booked}"
    )


def test_discovery_skips_an_older_orphan_and_attributed_sessions(store):
    from agentctl import child_runs
    started = time.time() - 3600
    _child(store, "old", born=started - 600, written=started + 30 * MIN)
    _child(store, "taken", written=started + 20 * MIN)
    _child(store, "real", written=started + 40 * MIN)
    run = {"child_session": None, "stage_index": 1, "started": started, "ended": None,
           "dispatcher_pid": 1}
    claimed = {"child_session": "taken", "stage_index": 1, "started": started - 100,
               "ended": started - 50, "dispatcher_pid": 1}
    found = child_runs.discover_child(store.root, "P", run, [claimed, run])
    assert found == "real", (
        f"BEHAVIOUR: a session born before the run began (a leftover of an earlier "
        f"dispatch that is still writing) and one already attributed to another run are "
        f"both passed over, got {found!r}"
    )


def _spawn_stub(store, seen, *, make_child=True, fail=False, child_id="C", orphan=None,
                report=False, load_fails=False, monkeypatch=None):
    """A stand-in for the blocking spawn. `orphan` names a second linked session born
    before the real child; `report` puts the child's transcript on stderr; `load_fails`
    makes the first state load after the spawn raise."""
    def stub(stage, plan_path, **_kw):
        raw = json.loads(store.path("P").read_text(encoding="utf-8"))
        seen["runs"] = raw.get("effort_child_runs")
        stamped = store.load("P")
        stamped.last_user_prompt_ts = 4242.0
        stamped.user_prompt_count = 9
        stamped.effort_actuals["active_minutes"] = 1.25
        store.save(stamped)
        if orphan:
            _child(store, orphan, written=time.time() + 2, stage=stage.index)
        if make_child:
            _child(store, child_id, written=time.time() + 5, stage=stage.index)
        if load_fails:
            real_load = store.load
            calls = []

            def flaky(session):
                if not calls:
                    calls.append(session)
                    raise ValueError("state unreadable")
                return real_load(session)
            monkeypatch.setattr(store, "load", flaky)
        if fail:
            raise RuntimeError("spawn crashed")
        stderr = f"spawn-specialist: transcript=/x/{child_id}.jsonl\n" if report else ""
        return RunResult(0, stdout="COMPLETED: done\n", stderr=stderr)
    return stub


def _dispatch_ready(store, tmp_path):
    from test_dispatch_effort import _plan_with_cost_tier, _to_executing as to_exec
    to_exec(store, "P", _plan_with_cost_tier(tmp_path, "small"))


def _dispatch(store):
    return cli.cmd_dispatch(ns(session="P", budget="medium", complexity="medium",
                               dry_run=False), store=store, runner=lambda argv: RunResult(0))


def test_dispatch_records_child_run_around_spawn(store, tmp_path, monkeypatch):
    _dispatch_ready(store, tmp_path)
    seen = {}
    monkeypatch.setattr(cli_dispatch, "dispatch_stage", _spawn_stub(store, seen))
    _dispatch(store)
    open_runs = seen["runs"] or []
    assert len(open_runs) == 1 and open_runs[0]["ended"] is None \
        and open_runs[0]["dispatcher_pid"] == os.getpid(), (
        f"BEHAVIOUR: before the blocking spawn runs, the dispatch is already on disk as "
        f"one open run owned by this process, got {open_runs}"
    )
    after = store.load("P")
    run = after.effort_child_runs[0]
    assert run["ended"] is not None and run["ended"] >= run["started"] \
        and run["child_session"] == "C", (
        f"BEHAVIOUR: after the spawn returns the run is closed and names its child, got {run}"
    )
    assert (after.last_user_prompt_ts, after.user_prompt_count,
            after.effort_actuals["active_minutes"]) == (4242.0, 9, 1.25), (
        "BEHAVIOUR: the prompt stamps the hook wrote while the spawn ran survive the "
        "closing save"
    )

    crash_seen = {}
    monkeypatch.setattr(cli_dispatch, "dispatch_stage",
                        _spawn_stub(store, crash_seen, fail=True, child_id="D"))
    with pytest.raises(RuntimeError):
        _dispatch(store)
    crashed = store.load("P").effort_child_runs[-1]
    assert crashed["child_session"] == "D" and crashed["ended"] is not None, (
        f"BEHAVIOUR: a spawn that raises still closes its run on its own child, got {crashed}"
    )


def test_reported_child_session_wins_over_an_unrelated_linked_session(
        store, tmp_path, monkeypatch):
    _dispatch_ready(store, tmp_path)
    monkeypatch.setattr(cli_dispatch, "dispatch_stage", _spawn_stub(
        store, {}, child_id="real", orphan="stray", report=True))
    _dispatch(store)
    named = store.load("P").effort_child_runs[0]["child_session"]
    assert named == "real", (
        f"BEHAVIOUR: the child the spawn reported (its transcript id) is the run's child "
        f"even when an earlier-born linked session of the same stage exists, got {named!r}"
    )


def test_unreported_child_is_the_earliest_born_after_the_run_began(
        store, tmp_path, monkeypatch):
    _dispatch_ready(store, tmp_path)
    monkeypatch.setattr(cli_dispatch, "dispatch_stage", _spawn_stub(
        store, {}, child_id="real", orphan="stray"))
    _dispatch(store)
    named = store.load("P").effort_child_runs[0]["child_session"]
    assert named == "stray", (
        f"BEHAVIOUR: with no reported id the earliest-born linked session since the run "
        f"began is taken, got {named!r}"
    )


@pytest.mark.parametrize("spawn_fails", [False, True])
def test_unreadable_state_at_close_still_closes_the_run(
        store, tmp_path, monkeypatch, spawn_fails):
    _dispatch_ready(store, tmp_path)
    monkeypatch.setattr(cli_dispatch, "dispatch_stage", _spawn_stub(
        store, {}, fail=spawn_fails, load_fails=True, monkeypatch=monkeypatch))
    if spawn_fails:
        with pytest.raises(RuntimeError, match="spawn crashed"):
            _dispatch(store)
    else:
        _dispatch(store)
    run = json.loads(store.path("P").read_text(encoding="utf-8"))["effort_child_runs"][0]
    assert run["child_session"] == "C" and run["ended"] is not None, (
        f"BEHAVIOUR: a state file that cannot be read when the run closes does not stop "
        f"the close, nor replace a spawn's own exception, got {run}"
    )


def test_unlinked_dispatch_records_no_child_run(store, tmp_path, monkeypatch):
    _dispatch_ready(store, tmp_path)
    seen = {}
    monkeypatch.setattr(cli_dispatch, "dispatch_stage", _spawn_stub(store, seen, make_child=False))
    _dispatch(store)
    runs = json.loads(store.path("P").read_text(encoding="utf-8")).get("effort_child_runs", [])
    assert runs == [], (
        f"BEHAVIOUR: a spawn that made no linked child leaves no run behind, got {runs}"
    )


@pytest.mark.parametrize("flush_order", [("P", "C"), ("C", "P")])
def test_grandchild_run_is_not_booked_twice_in_the_order_window(
        store, fixtures_dir, tmp_path, monkeypatch, flush_order):
    from test_parent_effort_rollup import _shared_order_with_grandchild
    _shared_order_with_grandchild(store, fixtures_dir, tmp_path, monkeypatch)
    base = time.time() - 7200
    parent_ran, grandchild_ran = 10.0, 3.0
    _put(store, "P", [_run(base, base + parent_ran * MIN, child="C")])
    _put(store, "C", [_run(base + MIN, base + (1 + grandchild_ran) * MIN, child="G")])
    for sid in flush_order:
        state = store.load(sid)
        cli._flush_order_effort(state, store=store)
        store.save(state)
    window = cli.order_approvals.get("shared-order")["effort_since_user_approval"]["wall_clock"]
    assert window == pytest.approx(parent_ran), (
        f"BEHAVIOUR: the parent's run spans the child and the grandchild, so the shared "
        f"order window holds its {parent_ran} minutes once, whichever session flushes "
        f"first, got {window}"
    )


def _order_window_wall(store, fixtures_dir, tmp_path, monkeypatch, flush_order, runs_of):
    """Flush the sessions of the parent-child-grandchild shared order in `flush_order`
    after giving each session in `runs_of` its child runs; the order window's wall-clock."""
    from test_parent_effort_rollup import _shared_order_with_grandchild
    _shared_order_with_grandchild(store, fixtures_dir, tmp_path, monkeypatch)
    for sid, runs in runs_of.items():
        _put(store, sid, runs)
    for sid in flush_order:
        state = store.load(sid)
        cli._flush_order_effort(state, store=store)
        store.save(state)
    return cli.order_approvals.get("shared-order")["effort_since_user_approval"]["wall_clock"]


@pytest.mark.parametrize("flush_order", [("P", "C"), ("C", "P")])
def test_child_run_without_a_parent_run_is_booked_by_the_child(
        store, fixtures_dir, tmp_path, monkeypatch, flush_order):
    base = time.time() - 7200
    grandchild_ran = 3.0
    window = _order_window_wall(store, fixtures_dir, tmp_path, monkeypatch, flush_order,
                                {"C": [_run(base, base + grandchild_ran * MIN, child="G")]})
    assert window == pytest.approx(grandchild_ran), (
        f"BEHAVIOUR: the linked child was spawned with no dispatch recorded by its parent, "
        f"so no parent run covers its {grandchild_ran}-minute run of the grandchild and the "
        f"child books it itself, got {window}"
    )


@pytest.mark.parametrize("flush_order", [("P", "C"), ("C", "P")])
def test_child_run_beyond_the_parent_run_is_booked_once(
        store, fixtures_dir, tmp_path, monkeypatch, flush_order):
    base = time.time() - 7200
    window = _order_window_wall(store, fixtures_dir, tmp_path, monkeypatch, flush_order, {
        "P": [_run(base, base + 1 * MIN, child="C")],
        "C": [_run(base + 1 * MIN, base + 4 * MIN, child="G")],
    })
    assert window == pytest.approx(4.0), (
        f"BEHAVIOUR: the parent's run of the child was closed after 1 minute (its dispatcher "
        f"died) but the child ran the grandchild until minute 4: the parent books minute "
        f"0-1 and the child the 3 minutes no parent run covers, 4.0 in all, got {window}"
    )


def test_dead_run_ends_at_the_latest_thing_its_child_did(store, parent):
    started = time.time() - 3600
    wrote, ran_until = started + 10 * MIN, started + 30 * MIN
    _child(store, "C", written=wrote, runs=[_run(started, ran_until, child="G")])
    _put(store, parent, [_run(started, None, child="C", pid=_dead_pid())])
    minutes = _wall_minutes(store, parent)
    assert minutes == pytest.approx(30.0), (
        f"BEHAVIOUR: a run whose dispatcher died ends no earlier than the end of the "
        f"child's own run of its grandchild (30 minutes in), not at the child's older file "
        f"write (10 minutes), got {minutes}"
    )


def test_unreadable_candidate_state_keeps_a_dead_run_open_for_a_while(store):
    from agentctl import child_runs
    store.root.mkdir(parents=True, exist_ok=True)
    started = time.time() - 3600
    half_written = store.path("C")
    half_written.write_text('{"parent_link": {', encoding="utf-8")
    os.utime(half_written, (started + 60, started + 60))
    now = started + 90
    fresh = [_run(started, None, child=None, pid=_dead_pid())]
    child_runs.close_dead_runs(fresh, store.root, "P", now=now)
    assert fresh[0]["ended"] is None, (
        f"BEHAVIOUR: a state file that may be the run's child but cannot be parsed just now "
        f"leaves the run open for the next read, got {fresh[0]}"
    )
    stale = [_run(started, None, child=None, pid=_dead_pid())]
    child_runs.close_dead_runs(stale, store.root, "P", now=now + 3600)
    assert stale[0]["ended"] == started, (
        f"BEHAVIOUR: a file left unreadable for long is a broken file, not a half-written "
        f"one, and the run ends at its start, got {stale[0]}"
    )


def _hooked_dispatch(store, monkeypatch, before_hook=lambda: None):
    """Dispatch stage 1 of the plan the caller made ready with a spawn that runs
    `before_hook()`, makes the child NEW and lets the real prompt hook stamp a prompt a
    minute after the last one; the parent's state afterwards."""
    hook = _load_hook()
    monkeypatch.setattr(hook.config_root, "resolve_agentctl_state_file",
                        lambda sid: store.path(sid))

    def stub(stage, plan_path, **_kw):
        before_hook()
        _child(store, "NEW", written=time.time() + 5, stage=stage.index)
        stamped = store.load("P")
        stamped.last_user_prompt_ts = time.time() - MIN
        store.save(stamped)
        hook._stamp_prompt_ts("P", time.time())
        return RunResult(0, stdout="COMPLETED: done\n", stderr="")
    monkeypatch.setattr(cli_dispatch, "dispatch_stage", stub)
    _dispatch(store)
    return store.load("P")


def test_redispatch_after_a_dead_dispatcher_keeps_the_old_run_apart(
        store, tmp_path, monkeypatch):
    old_started = time.time() - 3 * 3600
    _dispatch_ready(store, tmp_path)
    state = store.load("P")
    state.effort_child_runs = [_run(old_started, None, child=None, pid=_dead_pid())]
    store.save(state)
    after = _hooked_dispatch(store, monkeypatch)
    assert len(after.effort_child_runs) == 2, (
        f"BEHAVIOUR: the new dispatch's run is kept next to the earlier one, got "
        f"{after.effort_child_runs}"
    )
    old, new = after.effort_child_runs
    assert (old["child_session"], old["ended"]) == (None, old_started), (
        f"BEHAVIOUR: the earlier run's dispatcher died and its child never existed: it ends "
        f"at its start, and the child of the new dispatch is not taken for it, got {old}"
    )
    assert new["child_session"] == "NEW" and new["ended"] is not None, (
        f"BEHAVIOUR: the new run belongs to the child the new dispatch made, got {new}"
    )


def test_redispatch_keeps_a_run_closed_while_the_spawn_ran(store, tmp_path, monkeypatch):
    _dispatch_ready(store, tmp_path)
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    old_started = time.time() - 3600
    old_end = old_started + 5 * MIN
    state = store.load("P")
    state.effort_child_runs = [_run(old_started, None, child=None, pid=other.pid)]
    store.save(state)
    _child(store, "D", written=old_end, born=old_started)

    def other_dispatcher_dies():
        other.kill()
        other.wait()

    try:
        after = _hooked_dispatch(store, monkeypatch, other_dispatcher_dies)
    finally:
        other.kill()
        other.wait()
    assert len(after.effort_child_runs) == 2, (
        f"BEHAVIOUR: both dispatches' runs are kept, got {after.effort_child_runs}"
    )
    old, new = after.effort_child_runs
    assert old["child_session"] == "D" and old["ended"] == pytest.approx(old_end), (
        f"BEHAVIOUR: the other dispatcher died while this spawn ran and the hook closed its "
        f"run at its child's last write; this dispatch's closing save keeps that close "
        f"instead of reopening the run, got {old}"
    )
    assert new["child_session"] == "NEW", (
        f"BEHAVIOUR: with the other run's child already known, the new run is attributed to "
        f"the session this dispatch made, got {new}"
    )


def test_prompt_stamped_by_the_hook_after_the_cli_saved_survives_the_close(
        store, tmp_path, monkeypatch):
    _dispatch_ready(store, tmp_path)
    state = store.load("P")
    state.effort_actuals["active_minutes"] = 0.0
    store.save(state)
    hook = _load_hook()
    monkeypatch.setattr(hook.config_root, "resolve_agentctl_state_file",
                        lambda sid: store.path(sid))
    lead = 2.0

    def stub(stage, plan_path, **_kw):
        _child(store, "C", written=time.time() + 5, stage=stage.index)
        stamped = store.load("P")
        stamped.last_user_prompt_ts = time.time() - lead * MIN
        store.save(stamped)
        hook._stamp_prompt_ts("P", time.time() + 1)
        return RunResult(0, stdout="COMPLETED: done\n", stderr="")
    monkeypatch.setattr(cli_dispatch, "dispatch_stage", stub)
    _dispatch(store)
    after = store.load("P")
    booked = after.effort_actuals["active_minutes"]
    assert booked == pytest.approx(lead, abs=0.1), (
        f"BEHAVIOUR: the CLI saved its open run first and the hook then booked the gap "
        f"before it ({lead} minutes, the live run covers the last second): the CLI's closing "
        f"save keeps that, got {booked}"
    )
    run = after.effort_child_runs[0]
    assert run["child_session"] == "C" and run["ended"] is not None, (
        f"BEHAVIOUR: and it still closes the run on its child, got {run}"
    )


def _stamp_during_dispatch(store, monkeypatch, *, make_child, linking_off=False,
                           ahead_min=5.0, behind_min=10.0):
    """Dispatch stage 1 with a spawn during which the real prompt hook stamps a prompt that
    ends `ahead_min` minutes after the dispatch began and follows one `behind_min` minutes
    before it; returns `(runs seen on disk before the spawn, active minutes afterwards)`."""
    hook = _load_hook()
    monkeypatch.setattr(hook.config_root, "resolve_agentctl_state_file",
                        lambda sid: store.path(sid))
    if linking_off:
        monkeypatch.setenv("AGENTCTL_CHILD_LINKING", "0")
    state = store.load("P")
    state.effort_actuals["active_minutes"] = 0.0
    store.save(state)
    seen = {}

    def stub(stage, plan_path, **_kw):
        seen["runs"] = json.loads(store.path("P").read_text(encoding="utf-8")).get(
            "effort_child_runs") or []
        stamped = store.load("P")
        stamped.last_user_prompt_ts = time.time() - behind_min * MIN
        store.save(stamped)
        hook._stamp_prompt_ts("P", time.time() + ahead_min * MIN)
        if make_child:
            _child(store, "C", written=time.time() + 5, stage=stage.index)
        return RunResult(0, stdout="COMPLETED: done\n", stderr="")
    monkeypatch.setattr(cli_dispatch, "dispatch_stage", stub)
    _dispatch(store)
    return seen["runs"], store.load("P").effort_actuals["active_minutes"]


def test_dispatch_with_linking_off_leaves_the_prompt_gap_as_on_base(store, tmp_path, monkeypatch):
    _dispatch_ready(store, tmp_path)
    runs, booked = _stamp_during_dispatch(store, monkeypatch, make_child=False, linking_off=True)
    assert runs == [], (
        f"BEHAVIOUR: a dispatch that cannot be linked records no run, not even while the "
        f"spawn blocks, got {runs}"
    )
    assert booked == pytest.approx(15.0, abs=0.1), (
        f"BEHAVIOUR: with linking off a prompt during the spawn books its whole 15-minute "
        f"gap, as it does on a tree with no child runs, got {booked}"
    )


def test_spawn_that_turns_out_unlinked_loses_no_prompt_gap(store, tmp_path, monkeypatch):
    _dispatch_ready(store, tmp_path)
    runs, booked = _stamp_during_dispatch(store, monkeypatch, make_child=False)
    assert len(runs) == 1, f"setup: a dispatch that may link opens a run, got {runs}"
    assert booked == pytest.approx(15.0, abs=0.1), (
        f"BEHAVIOUR: the 5 minutes of the gap the hook left to the open run are booked when "
        f"the spawn made no linked child and the run is dropped, 15 in all, got {booked}"
    )
    assert store.load("P").effort_child_runs == [], "setup: the unlinked run is dropped"


def test_linked_dispatch_does_not_book_the_covered_part_of_the_gap_again(
        store, tmp_path, monkeypatch):
    _dispatch_ready(store, tmp_path)
    _, booked = _stamp_during_dispatch(store, monkeypatch, make_child=True)
    assert booked == pytest.approx(10.0, abs=0.1), (
        f"BEHAVIOUR: with a linked child the run owns the last 5 minutes of the gap, so "
        f"only the 10 minutes before the run are booked, got {booked}"
    )


def _half_written_state(store):
    store.root.mkdir(parents=True, exist_ok=True)
    store.path("HALF").write_text('{"parent_link": {', encoding="utf-8")


def test_held_open_dead_run_covers_nothing_in_the_hook(monkeypatch, tmp_path):
    hook = _load_hook()
    gap, lead = 10.0, 2.0
    previous = time.time() - gap * MIN
    started = previous + lead * MIN
    path = _hook_state(monkeypatch, tmp_path, "S", last_user_prompt_ts=previous,
                       effort_actuals={"active_minutes": 0.0},
                       effort_child_runs=[_run(started, None, child=None, pid=_dead_pid())])
    (path.parent / "HALF.json").write_text('{"parent_link": {', encoding="utf-8")
    hook._stamp_prompt_ts("S", time.time())
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["effort_child_runs"][0]["ended"] is None, "setup: the dead run is held open"
    booked = saved["effort_actuals"]["active_minutes"]
    assert booked == pytest.approx(gap, abs=0.1), (
        f"BEHAVIOUR: a run whose dispatcher is dead covers nothing even while it is held "
        f"open for an unreadable candidate, so the whole {gap}-minute gap is booked, got {booked}"
    )


def test_held_open_dead_run_adds_nothing_to_the_wall_clock(store, parent):
    started = time.time() - 3600
    _half_written_state(store)
    own = 3.0
    _put(store, parent, [_run(started, None, child=None, pid=_dead_pid())], active_minutes=own)
    minutes = _wall_minutes(store, parent)
    assert minutes == pytest.approx(own), (
        f"BEHAVIOUR: a dead dispatcher's run held open for an unreadable candidate counts "
        f"nothing until it is closed (its end is not yet known), got {minutes}"
    )


@pytest.mark.parametrize("flush_order", [("P", "C"), ("C", "P")])
def test_child_run_partly_beyond_the_parent_run_is_booked_once(
        store, fixtures_dir, tmp_path, monkeypatch, flush_order):
    base = time.time() - 7200
    window = _order_window_wall(store, fixtures_dir, tmp_path, monkeypatch, flush_order, {
        "P": [_run(base, base + 1 * MIN, child="C")],
        "C": [_run(base + 0.5 * MIN, base + 4 * MIN, child="G")],
    })
    assert window == pytest.approx(4.0), (
        f"BEHAVIOUR: the parent's run of the child covers minutes 0-1, the child's own run "
        f"minutes 0.5-4: the half minute they share is booked once, 4.0 in all, got {window}"
    )


def test_unreadable_parent_state_leaves_the_childs_wall_clock_flush_for_later(
        store, fixtures_dir, tmp_path, monkeypatch):
    from test_parent_effort_rollup import _shared_order_with_grandchild
    _shared_order_with_grandchild(store, fixtures_dir, tmp_path, monkeypatch)
    base = time.time() - 7200
    _put(store, "P", [_run(base, base + 4 * MIN, child="C")])
    _put(store, "C", [_run(base + 1 * MIN, base + 3 * MIN, child="G")])

    def flush_c():
        state = store.load("C")
        cli._flush_order_effort(state, store=store)
        store.save(state)
        window = cli.order_approvals.get("shared-order")["effort_since_user_approval"]
        return window["wall_clock"], state.order_effort_flushed["wall_clock"]

    assert flush_c() == (0.0, 0.0), "setup: the parent's run covers all of C's run"
    _put(store, "C", [_run(base + 1 * MIN, base + 3 * MIN, child="G"),
                      _run(base + 5 * MIN, base + 6 * MIN, child="G2")])
    readable = store.path("P").read_text(encoding="utf-8")
    store.path("P").write_text('{"effort_child_runs": [', encoding="utf-8")
    window, watermark = flush_c()
    assert (window, watermark) == (0.0, 0.0), (
        f"BEHAVIOUR: while the parent's state file cannot be read just now (it may be "
        f"mid-save) the child cannot tell what its parent's run covers, so it flushes no "
        f"wall-clock and keeps its watermark, got window {window}, watermark {watermark}"
    )
    store.path("P").write_text(readable, encoding="utf-8")
    window, _ = flush_c()
    assert window == pytest.approx(1.0), (
        f"BEHAVIOUR: once the parent's state reads again the child books the 1 minute no "
        f"parent run covers, got {window}"
    )


def test_gap_covered_by_a_closed_run_and_an_open_run_that_is_dropped_counts_once(
        monkeypatch, tmp_path):
    hook = _load_hook()
    gap = 20.0
    previous = time.time() - gap * MIN
    closed = _run(previous + 1 * MIN, previous + 10 * MIN, child="A")
    open_run = _run(previous + 11 * MIN, None, child=None)
    path = _hook_state(monkeypatch, tmp_path, "S", last_user_prompt_ts=previous,
                       effort_actuals={"active_minutes": 0.0},
                       effort_child_runs=[closed, open_run])
    hook._stamp_prompt_ts("S", previous + gap * MIN)
    saved = json.loads(path.read_text(encoding="utf-8"))
    booked = saved["effort_actuals"]["active_minutes"]
    from agentctl import child_runs
    lost = child_runs.gap_loss_min(saved["effort_child_runs"], open_run)
    total = booked + lost + effort.union_seconds([(closed["started"], closed["ended"])]) / MIN
    assert total == pytest.approx(gap, abs=0.1), (
        f"BEHAVIOUR: of a {gap}-minute gap a closed run covers 9 minutes and an open run 9 "
        f"more; if the open run is dropped as unlinked only its own 9 are booked back, so "
        f"booked + booked back + the closed run = the real {gap}, got {total}"
    )


def test_first_wall_clock_flush_with_unreadable_parent_does_not_seed_above_the_settled_value(
        store, fixtures_dir, tmp_path, monkeypatch):
    from test_parent_effort_rollup import _shared_order_with_grandchild
    _shared_order_with_grandchild(store, fixtures_dir, tmp_path, monkeypatch)
    base = time.time() - 7200
    _put(store, "P", [_run(base, base + 4 * MIN, child="C")])
    _put(store, "C", [_run(base + 1 * MIN, base + 3 * MIN, child="G"),
                      _run(base + 5 * MIN, base + 6 * MIN, child="G2")])
    state = store.load("C")
    state.order_effort_flushed = None
    store.save(state)
    readable = store.path("P").read_text(encoding="utf-8")
    store.path("P").write_text('{"effort_child_runs": [', encoding="utf-8")
    state = store.load("C")
    cli._flush_order_effort(state, store=store)
    store.save(state)
    store.path("P").write_text(readable, encoding="utf-8")
    state = store.load("C")
    cli._flush_order_effort(state, store=store)
    window = cli.order_approvals.get("shared-order")["effort_since_user_approval"]["wall_clock"]
    assert window == pytest.approx(1.0), (
        f"BEHAVIOUR: the first flush came while the parent's state could not be read, so it "
        f"seeds nothing above the settled value; once it reads, the 1 minute of the child's "
        f"runs that no parent run covers is booked, got {window}"
    )
