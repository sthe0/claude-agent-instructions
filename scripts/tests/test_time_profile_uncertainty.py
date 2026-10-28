"""Reconstruction bounds, cross-source agreement and the instrument / wait decision.

Every expected number is worked out by hand in the comment beside it. The shared fixture
`evidence_sessions` carries a spawn / Bash pair (27 s against 30 s: 10 %) and a judge / hook
pair (24 s against 30 s: 20 %), which calibrate the tool-interval source to 0.1 and the
model:gap source to 0.2; `unattributed` has no pair, so it moves its full 1 minute.
"""
from __future__ import annotations

import json
import os
import types
from pathlib import Path

import pytest

from lib import judge_ledger, time_profile, time_profile_uncertainty as unc
from lib.agent_report import time_profile as view
from time_profile_mutants import time_profile_mutant  # noqa: F401  (autouse fixture)

FIX = Path(__file__).resolve().parent / "fixtures" / "time_profile"
SCRIPTS = Path(__file__).resolve().parents[1]
MIN = 60_000
NOW = 1_800_000_000_000
TOOL_INTERVAL = "tool_interval_hidden_time"
VERDICTS = {unc.VERDICT_INSTRUMENT, unc.VERDICT_WAIT, unc.VERDICT_NONE}


def session(sid, partition, overlays=None, *, wall=10 * MIN, coverage=None):
    return time_profile.SessionProfile(
        session_id=sid, transcript=f"/x/{sid}.jsonl", first_ts_ms=NOW - wall, last_ts_ms=NOW,
        partition=dict(partition), overlays=dict(overlays or {}), coverage=dict(coverage or {}),
        core_sha=None, child_of=None, kind="main",
    )


def evidence_sessions(read, wait, gap, count=4):
    cells = {
        "tool:Read": read * MIN, "user:wait": wait * MIN, "model:gap": gap * MIN,
        "unattributed": MIN, unc.SPAWN_TOOL_KEY: 30_000, "hook:hook-turn-end-gate.py": 30_000,
    }
    overlays = {"spawn:developer": 27_000, "judge:acceptance": 24_000}
    return [session(f"E{i}", cells, overlays, wall=100 * MIN) for i in range(count)]


def report_of(profiles, ctx=None):
    agg = time_profile.aggregate(profiles, half_life_days=7, now_ms=NOW)
    return agg, unc.uncertainty(agg, profiles, ctx)


def component(report, key):
    return next(c for c in report.components if c["key"] == key)


def board_item(minutes, sessions, mechanism=None):
    return types.SimpleNamespace(
        signatures=("sig",), minutes_per_occurrence=minutes, mechanism=mechanism,
        loss_measurement={"window_days": 7, "sessions": sessions},
    )


def test_widening_touches_only_its_keys():
    cell = {"tool:Read": 100.0, "tool:Edit": 50.0, "model:gap": 200.0, "hook:h": 10.0,
            "user:wait": 30.0, "node:X": 40.0, "node:Y": 10.0, "unattributed": 20.0}
    receivers = unc.receivers_by_source(sorted(cell))
    none = dict.fromkeys(unc.AMBIGUITY_SOURCES, 0.0)

    assert unc.widen_cell(cell, receivers, none) == (cell, cell)

    # tool-interval at 0.5: Read 100 -> 50, Edit 50 -> 25, 75 moves to hook:h and user:wait
    lo, hi = unc.widen_cell(cell, receivers, {**none, TOOL_INTERVAL: 0.5})
    assert lo == {**cell, "tool:Read": 50.0, "tool:Edit": 25.0}
    assert hi == {**cell, "hook:h": 85.0, "user:wait": 105.0}

    # model:gap at 0.25: gap 200 -> 150, 50 moves to hook:h only
    lo, hi = unc.widen_cell(cell, receivers, {**none, "model_gap_unexposed_hooks": 0.25})
    assert lo == {**cell, "model:gap": 150.0}
    assert hi == {**cell, "hook:h": 60.0}

    # untimed engine events at 0.5 only add: lo stays, X 40 -> 60 and Y 10 -> 15 at hi
    lo, hi = unc.widen_cell(cell, receivers, {**none, "engine_untimed_events": 0.5})
    assert lo == cell
    assert hi == {**cell, "node:X": 60.0, "node:Y": 15.0}

    # unattributed at 1.0: 20 -> 0; every partition key except it gains 20, overlays stay put
    lo, hi = unc.widen_cell(cell, receivers, {**none, "unattributed": 1.0})
    assert lo == {**cell, "unattributed": 0.0}
    assert hi == {**cell, **{k: cell[k] + 20.0 for k in ("tool:Read", "tool:Edit", "model:gap", "hook:h", "user:wait")}}


def test_cross_source_discrepancies(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    t0 = NOW // 1000 - 600
    (state / "S1.json").write_text(json.dumps({
        "effort_actuals": {"active_minutes": 7}, "history": [{"ts": t0 + 60}, {"ts": t0 + 360}]}))
    (state / "S2.json").write_text(json.dumps({
        "effort_actuals": {"active_minutes": 9}, "history": [{"ts": t0 + 60}, {"ts": t0 + 420}]}))
    spawn_tool = unc.SPAWN_TOOL_KEY
    top = [
        session("S1", {"user:wait": 2 * MIN, spawn_tool: 3 * MIN, "hook:hook-turn-end-gate.py": MIN},
                {"spawn:developer": 144_000, "judge:acceptance": 30_000, "node:APPROVED": 4 * MIN}),
        session("S2", {"user:wait": MIN, spawn_tool: 2 * MIN, "hook:hook-turn-end-gate.py": 36_000},
                {"spawn:developer": 108_000, "judge:acceptance": 54_000, "node:APPROVED": 3 * MIN}),
        session("S3", {}, {"spawn:developer": MIN}),
    ]
    stats = unc.cross_source_pairs(top, time_profile.Ctx(agentctl_state_dir=state))

    # spawn vs Bash: S1 |144-180|/180 = 0.2, S2 |108-120|/120 = 0.1; S3 has a spawn but no Bash
    # row, which counts as 1.0 -> sorted 0.1, 0.2, 1.0: median 0.2, p90 is the 3rd value
    assert stats["spawn_vs_bash"]["n"] == 3
    assert stats["spawn_vs_bash"]["median"] == pytest.approx(0.2)
    assert stats["spawn_vs_bash"]["p90"] == pytest.approx(1.0)
    # judge vs hook: S1 |30-60|/60 = 0.5, S2 |54-36|/54 = 1/3; S3 has neither
    assert stats["judge_vs_hook"]["n"] == 2
    assert stats["judge_vs_hook"]["median"] == pytest.approx((0.5 + 1 / 3) / 2)
    assert stats["judge_vs_hook"]["p90"] == pytest.approx(0.5)
    # effort vs agent-active: S1 7 against 10 - 2 = 8 min -> 0.125; S2 9 against 10 - 1 -> 0;
    # S3 has no engine state at all, so the effort source never observed it
    assert stats["effort_vs_active"]["n"] == 2
    assert stats["effort_vs_active"]["median"] == pytest.approx(0.0625)
    assert stats["effort_vs_active"]["p90"] == pytest.approx(0.125)
    # node dwell vs engine span: S1 4 min against 5 -> 0.2; S2 3 against 6 -> 0.5
    assert stats["node_vs_engine_span"]["median"] == pytest.approx(0.35)
    assert stats["node_vs_engine_span"]["p90"] == pytest.approx(0.5)

    without_state = unc.cross_source_pairs(top, time_profile.Ctx())
    assert without_state["effort_vs_active"]["n"] == 0
    assert without_state["node_vs_engine_span"]["p90"] is None
    assert without_state["spawn_vs_bash"] == stats["spawn_vs_bash"]


def test_one_sided_pair_counts_as_full_discrepancy():
    assert unc._relative(3.0, 4.0) == pytest.approx(0.25)
    assert unc._relative(5.0, 0.0) == 1.0
    assert unc._relative(0.0, 5.0) == 1.0
    assert unc._relative(0.0, 0.0) is None

    # a judge ran for 30 s but its hook left no time in the transcript: |30-0|/30 = 1.0
    judged_only = session("J1", {}, {"judge:acceptance": 30_000})
    assert unc.cross_source_pairs([judged_only], time_profile.Ctx())["judge_vs_hook"] == {
        "n": 1, "median": 1.0, "p90": 1.0}
    # a session with neither is no sample
    assert unc.cross_source_pairs([session("J2", {})], time_profile.Ctx())["judge_vs_hook"]["n"] == 0


def test_judge_hook_keys_follow_the_judge_ledger():
    assert unc.JUDGE_HOOK_KEYS == {f"hook:{basename}" for basename in judge_ledger.HOOK_NAME_BY_BASENAME}
    assert unc.JUDGE_HOOK_KEYS
    assert all((SCRIPTS / basename).is_file() for basename in judge_ledger.HOOK_NAME_BY_BASENAME)


def test_unattributed_moves_its_full_minutes_without_a_pair():
    source = unc.AMBIGUITY_SOURCES["unattributed"]
    assert source["pairs"] == ()
    assert unc.source_fraction(source, {}) == 1.0


def test_heavy_keys_against_unattributed():
    cells = {
        "tool:A": 10, "model:gap": 6, "unattributed": 3, "tool:Eq": 3, "tool:B": 2,
        "tool:Bash:$X": 20, "node:N": 5, "spawn:s": 1,
    }
    agg = time_profile.aggregate(
        [session(f"H{i}", {k: v * MIN for k, v in cells.items() if not time_profile.is_overlay(k)},
                 {k: v * MIN for k, v in cells.items() if time_profile.is_overlay(k)}) for i in range(2)],
        half_life_days=7, now_ms=NOW)

    # strictly above the unattributed mean (3), shell-variable programs excluded, by decreasing mean
    assert unc.heavy_keys(agg, "partition") == ["tool:A", "model:gap"]
    assert unc.heavy_keys(agg, "overlay") == ["node:N"]


def test_verdict_instrument_names_source():
    # Read 40 and user:wait 36 are 4 apart; with se 0 they are ranked under +/-se alone, but the
    # tool-interval source (0.1 of Read's 40 = 4, plus 1 unattributed) closes the gap.
    profiles = evidence_sessions(read=40, wait=36, gap=20)
    _agg, report = report_of(profiles)

    assert report.fractions[TOOL_INTERVAL] == pytest.approx(0.1)
    assert report.fractions["model_gap_unexposed_hooks"] == pytest.approx(0.2)
    read, wait, gap = (component(report, k) for k in ("tool:Read", "user:wait", "model:gap"))
    assert read["interval"] == pytest.approx([36.0, 41.0])
    assert wait["interval"] == pytest.approx([36.0, 36.0 + 4.05 + 1.0])
    assert (read["verdict"], read["source"]) == (unc.VERDICT_INSTRUMENT, TOOL_INTERVAL)
    assert wait["verdict"] == unc.VERDICT_INSTRUMENT
    # gap [16, 21] sits clear of user:wait [36, ...]
    assert gap["verdict"] == unc.VERDICT_NONE
    assert report.overall == "instrument: tool:Read, user:wait"
    assert read["source"] in unc.AMBIGUITY_SOURCES


def test_verdict_wait_for_more_sessions():
    one = report_of(evidence_sessions(read=40, wait=36, gap=20, count=1))[1]
    assert {c["verdict"] for c in one.components} == {unc.VERDICT_WAIT}
    assert all(c["per_session"]["se"] is None and c["combined"] is None for c in one.components)
    assert one.overall == unc.VERDICT_WAIT

    # Two sessions: Read 40 / 30 -> mean 35, s^2 = 50, se 5; wait 36 / 34 -> mean 35, se 1.
    # Their +/-se bands [30, 40] and [34, 36] overlap: more sessions, not telemetry, settle it.
    two = report_of([
        session("W1", {"tool:Read": 40 * MIN, "user:wait": 36 * MIN, "unattributed": MIN}, wall=100 * MIN),
        session("W2", {"tool:Read": 30 * MIN, "user:wait": 34 * MIN, "unattributed": MIN}, wall=100 * MIN),
    ])[1]
    read = component(two, "tool:Read")
    assert read["per_session"]["se"] == pytest.approx(5.0)
    assert component(two, "user:wait")["per_session"]["se"] == pytest.approx(1.0)
    assert read["verdict"] == component(two, "user:wait")["verdict"] == unc.VERDICT_WAIT
    assert two.overall == unc.VERDICT_WAIT


def test_verdict_no_instrumentation():
    # Read 60 [54, 61], user:wait 30 [30, 37.05], model:gap 8 [6.4, 9]: pairwise disjoint.
    _agg, report = report_of(evidence_sessions(read=60, wait=30, gap=8))

    assert [c["key"] for c in report.components] == ["tool:Read", "user:wait", "model:gap"]
    assert component(report, "tool:Read")["interval"] == pytest.approx([54.0, 61.0])
    assert component(report, "user:wait")["interval"] == pytest.approx([30.0, 37.05])
    assert component(report, "model:gap")["interval"] == pytest.approx([6.4, 9.0])
    assert {c["verdict"] for c in report.components} == {unc.VERDICT_NONE}
    assert all(c["source"] is None for c in report.components)
    assert report.overall == unc.VERDICT_NONE


def test_loss_rank_flip_instruments():
    profiles = evidence_sessions(read=60, wait=30, gap=8)
    assert component(report_of(profiles)[1], "tool:Read")["verdict"] == unc.VERDICT_NONE

    # Two sessions each, 7-day window: linked item A costs 2 * 54 = 108 min/week at Read's lo and
    # 2 * 61 = 122 at its hi; the fixed item B costs 2 * 57 = 114 in between, so A passes B.
    board = {"A": board_item(1.0, ["a1", "a2"], mechanism="tool:Read"), "B": board_item(57.0, ["b1", "b2"])}
    ctx = time_profile.Ctx(board=board, rank_board=view.rank_board)
    _agg, report = report_of(profiles, ctx)
    read = component(report, "tool:Read")
    assert read["flipped_items"] == ["A"]
    assert (read["verdict"], read["source"]) == (unc.VERDICT_INSTRUMENT, TOOL_INTERVAL)
    assert component(report, "user:wait")["verdict"] == unc.VERDICT_NONE
    assert report.overall == "instrument: tool:Read"

    # B at 70 outranks A at both ends of Read's interval: no flip, no instrumentation.
    steady = {"A": board["A"], "B": board_item(70.0, ["b1", "b2"])}
    ctx = time_profile.Ctx(board=steady, rank_board=view.rank_board)
    assert component(report_of(profiles, ctx)[1], "tool:Read")["flipped_items"] == []

    # No ranking function, no flip test.
    assert component(report_of(profiles, time_profile.Ctx(board=board))[1], "tool:Read")["flipped_items"] == []


def test_wait_outranks_a_board_flip():
    # One session has no variance, so every verdict waits even where the board item would flip.
    board = {"A": board_item(1.0, ["a1", "a2"], mechanism="tool:Read"), "B": board_item(57.0, ["b1", "b2"])}
    ctx = time_profile.Ctx(board=board, rank_board=view.rank_board)
    report = report_of(evidence_sessions(read=60, wait=30, gap=8, count=1), ctx)[1]

    read = component(report, "tool:Read")
    assert read["flipped_items"] == ["A"]
    assert (read["verdict"], read["source"]) == (unc.VERDICT_WAIT, None)


def run_with(profiles):
    run = time_profile.ProfileRun()
    run.profiles = profiles
    return run


def test_render_json_per_heavy_component(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TIME_PROFILE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(time_profile, "_clock_ms", lambda: NOW)

    code = view.main(["--uncertainty", "--format", "json", "--half-life", "7"],
                     run=run_with(evidence_sessions(read=40, wait=36, gap=20)))
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert [c["key"] for c in payload["components"]] == ["tool:Read", "user:wait", "model:gap"]
    for c in payload["components"]:
        assert set(c["share_active"]) == set(c["per_session"]) == {"mean", "se", "n_eff"}
        assert len(c["interval"]) == len(c["combined"]) == 2
        assert c["verdict"] in VERDICTS
        assert (c["source"] in unc.AMBIGUITY_SOURCES) == (c["verdict"] == unc.VERDICT_INSTRUMENT)
    read = payload["components"][0]
    # Read 40 of agent-active 100 - 36 = 64 minutes, in all four sessions
    assert read["share_active"] == {"mean": 0.625, "se": 0.0, "n_eff": 4.0}
    assert read["per_session"] == {"mean": 40.0, "se": 0.0, "n_eff": 4.0}
    assert read["interval"] == [36.0, 41.0] and read["combined"] == [36.0, 41.0]
    assert payload["overall"] == "instrument: tool:Read, user:wait"


def test_render_md_per_heavy_component(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TIME_PROFILE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(time_profile, "_clock_ms", lambda: NOW)

    view.main(["--uncertainty", "--format", "md", "--half-life", "7"],
              run=run_with(evidence_sessions(read=40, wait=36, gap=20)))
    lines = capsys.readouterr().out.splitlines()

    start = lines.index("## tool:Read (partition)")
    block = lines[start:lines.index("", start)]
    assert "- share of active: 0.625 ± 0 (n_eff 4.0)" in block
    assert "- min/session: 40 ± 0 (n_eff 4.0)" in block
    assert "- reconstruction interval: [36, 41]" in block
    assert "- combined interval: [36, 41]" in block
    assert "- verdict: instrument (tool_interval_hidden_time)" in block
    assert "## model:gap (partition)" in lines
    assert "- verdict: no instrumentation needed" in lines[lines.index("## model:gap (partition)"):]
    assert lines[-1] == "overall: instrument: tool:Read, user:wait"


def test_uncertainty_defaults_to_md_and_rejects_other_modes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TIME_PROFILE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(time_profile, "_clock_ms", lambda: NOW)
    run = run_with(evidence_sessions(read=40, wait=36, gap=20))

    assert view.main(["--uncertainty", "--half-life", "7"], run=run) == 0
    assert capsys.readouterr().out.startswith("# time-profile uncertainty")

    for extra in (["--check"], ["--by-sha"], ["--format", "text"]):
        with pytest.raises(SystemExit) as exit_info:
            view.main(["--uncertainty", *extra], run=run)
        assert exit_info.value.code == 2
    capsys.readouterr()


def test_overall_recommendation_rule():
    def parts(*verdicts):
        return [{"key": k, "verdict": v} for k, v in zip("abc", verdicts)]

    instrument, wait, none = unc.VERDICT_INSTRUMENT, unc.VERDICT_WAIT, unc.VERDICT_NONE
    assert unc.overall_recommendation(parts(wait, instrument, instrument)) == "instrument: b, c"
    assert unc.overall_recommendation(parts(none, wait)) == "wait for more sessions"
    assert unc.overall_recommendation(parts(none, none)) == "no instrumentation needed"
    assert unc.overall_recommendation([]) == "wait for more sessions"


def test_coverage_labels():
    cells = {"tool:Read": 20 * MIN, "hook:hook-x.py": 5 * MIN, "unattributed": MIN}
    profiles = [
        session("C1", cells, {"node:APPROVED": 4 * MIN},
                coverage={"exposed_hook_ms": 120_000, "node_timed_events": 3, "node_untimed_events": 1,
                          "timed_events": 10, "untimed_events": 0}),
        session("C2", cells, {"node:APPROVED": 4 * MIN},
                coverage={"exposed_hook_ms": 60_000, "node_timed_events": 1, "node_untimed_events": 1,
                          "timed_events": 10, "untimed_events": 5}),
    ]
    _agg, report = report_of(profiles)

    # 180 s of exposed hook time; untimed engine events 2 of 6, untimed transcript events 5 of 25
    assert report.coverage["exposed_hook_minutes"] == pytest.approx(3.0)
    assert report.coverage["exposed_hook_label"] == "lower bound"
    assert report.coverage["node_untimed_share"] == pytest.approx(2 / 6)
    assert report.coverage["transcript_untimed_share"] == pytest.approx(0.2)
    assert component(report, "hook:hook-x.py")["coverage"] == {"label": "lower bound"}
    assert component(report, "node:APPROVED")["coverage"]["untimed_share"] == pytest.approx(2 / 6)


def test_writes_only_state_dir(tmp_path, monkeypatch, capsys):
    home, state = tmp_path / "home", tmp_path / "state"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TIME_PROFILE_STATE_DIR", str(state))
    before = snapshot(FIX)

    run = time_profile.profile_all(
        state_dir=state, projects_root=FIX / "projects", agentctl_state_dir=FIX / "agentctl_state",
        spawn_ledger=FIX / "ledgers" / "spawn.jsonl", judge_ledger_path=FIX / "ledgers" / "judge.jsonl",
        policy_ledger=FIX / "ledgers" / "policy.jsonl", commit_index=[(5, "old")],
    )
    assert view.main(["--uncertainty", "--format", "json", "--half-life", "7"], run=run) == 0
    json.loads(capsys.readouterr().out)

    assert snapshot(FIX) == before
    assert list(home.iterdir()) == []
    written = {p for p in tmp_path.rglob("*") if p.is_file()}
    assert all(state in p.parents for p in written)
    assert {state / "uncertainty-report.json", state / "uncertainty-report.md"} <= written
    assert json.loads((state / "uncertainty-report.json").read_text())["components"] is not None


def test_report_files_are_replaced_atomically(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("TIME_PROFILE_STATE_DIR", str(state))
    monkeypatch.setattr(time_profile, "_clock_ms", lambda: NOW)
    run = run_with(evidence_sessions(read=40, wait=36, gap=20))
    (state / "uncertainty-report.md").write_text("previous report\n")

    view.main(["--uncertainty", "--half-life", "7"], run=run)
    capsys.readouterr()
    assert (state / "uncertainty-report.md").read_text().startswith("# time-profile uncertainty")
    assert sorted(p.name for p in state.iterdir()) == ["uncertainty-report.json", "uncertainty-report.md"]

    # a failed rename leaves the previous report untouched and no temp file behind
    (state / "uncertainty-report.md").write_text("previous report\n")

    def refuse(src, dst):
        raise OSError("rename refused")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError):
        view.write_uncertainty_files({"components": []}, "new report")
    assert (state / "uncertainty-report.md").read_text() == "previous report\n"
    assert not [p for p in state.iterdir() if p.name.endswith(".tmp")]


def snapshot(root):
    return {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(root.rglob("*")) if p.is_file()}
