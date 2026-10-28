"""Measured minutes and the "Where the time goes" section of improvement-scan.

Every expected number is worked out by hand in the comment beside it. The shared scenario has
three top-level sessions A, B, C aged 0, 7 and 14 days at a fixed clock with half-life 7, so
their weights are 1, 1/2, 1/4 (V1 = 7/4, V2 = 21/16, n_eff = 7/3):

    session  wall  sha   user:wait  model:gap  tool:Read  tool:Edit  unattributed  mcp:solo
    A        10    x     4          4          2          -          -             1
    B        20    x     5          10         4          2          1             -
    C        30    y     10         16         3          6          1             -

(minutes). tool:Read is present in all three (n_eff 7/3, SE defined); tool:Edit in B and C only
(weights 1/2 and 1/4: n_eff 9/5 = 1.8 < 2, SE undefined); mcp:solo in A only (n_eff 1).

The negative control is time_profile_mutants.py: this file must fail under each
improvement-scan mutant.
"""
from __future__ import annotations

import importlib.util
import json
import math
import sys
import types
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import improvement_scan_loss as loss  # noqa: E402
from lib import time_profile, time_profile_uncertainty as unc  # noqa: E402
from time_profile_mutants import time_profile_mutant  # noqa: F401,E402  (autouse fixture)


def _load_scan():
    spec = importlib.util.spec_from_file_location(
        "improvement_scan_time_tests", SCRIPTS_DIR / "improvement-scan.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


scan = _load_scan()

DAY = 86_400_000
MIN = 60_000
NOW = 1_800_000_000_000


def prof(sid, age_days, wall_min, partition, *, sha=None, child_of=None):
    last = NOW - int(age_days * DAY)
    return time_profile.SessionProfile(
        session_id=sid, transcript=f"/x/{sid}.jsonl", first_ts_ms=last - wall_min * MIN,
        last_ts_ms=last, partition={k: v * MIN for k, v in partition.items()}, overlays={},
        coverage={}, core_sha=sha, child_of=child_of, kind="main",
    )


def scenario():
    return [
        prof("A", 0, 10, {"user:wait": 4, "model:gap": 4, "tool:Read": 2, "mcp:solo": 1}, sha="x"),
        prof("B", 7, 20, {"user:wait": 5, "model:gap": 10, "tool:Read": 4, "tool:Edit": 2,
                          "unattributed": 1}, sha="x"),
        prof("C", 14, 30, {"user:wait": 10, "model:gap": 16, "tool:Read": 3, "tool:Edit": 6,
                           "unattributed": 1}, sha="y"),
    ]


def scenario_aggregate():
    return time_profile.aggregate(scenario(), half_life_days=7, now_ms=NOW)


def item(minutes=None, sessions=("s1", "s2", "s3"), mechanism=None, family=""):
    return types.SimpleNamespace(
        signatures=("sig",), minutes_per_occurrence=minutes, mechanism=mechanism, family=family,
        loss_measurement={"window_days": 7, "sessions": list(sessions)},
    )


def rows_of(items, measured):
    return loss.build_lanes(items, measured)["loss"]


# ----------------------------------------------------------------- measured minutes

def test_measured_minutes_are_the_conditional_decayed_figures():
    measured = loss.measured_minutes(scenario_aggregate().rows)

    # tool:Read: (1*2 + 1/2*4 + 1/4*3) / (7/4) = 4.75 / 1.75 = 19/7;
    # reliability variance 19/14 (sum w*dev^2 = 133/98 over V1 - V2/V1 = 1), SE^2 = (19/14) / (7/3) = 57/98
    mean, se, n_eff = measured["tool:Read"]
    assert mean == pytest.approx(19 / 7)
    assert se == pytest.approx(math.sqrt(57 / 98))
    assert n_eff == pytest.approx(7 / 3)

    # tool:Edit: (1/2*2 + 1/4*6) / (3/4) = 2.5 / 0.75 = 10/3; n_eff = (3/4)^2 / (5/16) = 1.8 -> no SE
    mean, se, n_eff = measured["tool:Edit"]
    assert (mean, se) == (pytest.approx(10 / 3), None)
    assert n_eff == pytest.approx(1.8)

    # mcp:solo: one session
    assert measured["mcp:solo"] == (pytest.approx(1.0), None, pytest.approx(1.0))


def test_measured_replaces_the_guess_and_the_basis_says_so():
    [row] = rows_of({"r": item(minutes=9.0, mechanism="tool:Read")},
                    loss.measured_minutes(scenario_aggregate().rows))

    # 3 sessions x 19/7 min, window 7 days = 1 week, precision unjudged = 1
    assert row["min_per_occurrence"] == pytest.approx(19 / 7)
    assert row["min_per_week"] == pytest.approx(57 / 7)
    assert row["minutes_source"] == "measured"
    assert row["basis"] == (
        "3 session(s) in 7d x measured tool:Read 2.71 +/- 0.763 min/occurrence "
        "(decayed, n_eff 2.3; whole-mechanism time, an upper bound on removable loss); "
        "precision unjudged (upper bound)"
    )


@pytest.mark.parametrize("mechanism", [
    "tool:Missing",  # no figure for the key
    "tool:Edit",     # n_eff 1.8: the SE is undefined
    "mcp:solo",      # n_eff 1
    None,            # no mechanism
])
def test_the_guess_is_used_when_there_is_no_measured_figure_with_an_error(mechanism):
    [row] = rows_of({"r": item(minutes=5.0, mechanism=mechanism)},
                    loss.measured_minutes(scenario_aggregate().rows))

    assert row["min_per_week"] == 15.0  # 3 sessions x 5 min
    assert row["min_per_occurrence"] == 5.0
    assert row["minutes_source"] == "guess"
    assert "5 min/occurrence" in row["basis"] and "measured" not in row["basis"]


def test_a_measured_item_is_loss_ready_without_a_guess():
    measured = loss.measured_minutes(scenario_aggregate().rows)
    lanes = loss.build_lanes(
        {"with": item(mechanism="tool:Read"), "without": item(mechanism="tool:Edit")}, measured
    )

    assert [r["ref"] for r in lanes["loss"]] == ["with"]
    assert lanes["loss"][0]["min_per_week"] == pytest.approx(57 / 7)
    assert [r["ref"] for r in lanes["awaiting"]] == ["without"]  # n_eff 1.8: nothing to charge


def test_a_board_without_mechanisms_ranks_as_before_apart_from_the_source():
    items = {"a": item(minutes=4.0, sessions=("s1", "s2")), "b": item(minutes=2.0)}
    measured = loss.measured_minutes(scenario_aggregate().rows)

    before = loss.build_lanes(items)
    with_profile = loss.build_lanes(items, measured)

    assert with_profile == before
    # a: 2 sessions x 4 min = 8; b: 3 sessions x 2 min = 6
    assert [(r["ref"], r["min_per_week"], r["minutes_source"]) for r in before["loss"]] == [
        ("a", 8.0, "guess"), ("b", 6.0, "guess"),
    ]


def test_a_family_is_measured_only_when_every_ready_member_is():
    measured = loss.measured_minutes(scenario_aggregate().rows)

    def family_row(*members):
        items = {f"m{i}": m for i, m in enumerate(members)}
        [row] = rows_of(items, measured)
        return row

    read = dict(mechanism="tool:Read", family="f")
    # s1, s2, s3 each cost 19/7 (s3 is shared and charged at its costliest member, 19/7 > 2);
    # s4 costs the guess 2: 3 * 19/7 + 2 = 71/7
    mixed = family_row(item(sessions=("s1", "s2", "s3"), **read),
                       item(minutes=2.0, sessions=("s3", "s4"), family="f"))
    assert mixed["minutes_source"] == "mixed"
    assert mixed["min_per_week"] == pytest.approx(71 / 7)

    both = family_row(item(sessions=("s1", "s2"), **read), item(sessions=("s3",), **read))
    assert both["minutes_source"] == "measured"
    assert both["min_per_week"] == pytest.approx(3 * 19 / 7)

    neither = family_row(item(minutes=2.0, sessions=("s1",), family="f"),
                         item(minutes=3.0, sessions=("s2",), family="f"))
    assert neither["minutes_source"] == "guess"
    assert neither["min_per_week"] == 5.0


# ----------------------------------------------------------------------- time sinks

def test_time_sinks_follow_heavy_keys_with_mean_se_share_and_n_eff():
    agg = scenario_aggregate()
    keys = unc.heavy_keys(agg, "partition")
    sinks = loss.time_sinks(agg, scenario(), keys)

    # per-session means over all three sessions, divided by V1 = 7/4:
    # model:gap (4 + 5 + 4)/1.75 = 52/7, user:wait (4 + 2.5 + 2.5)/1.75 = 36/7,
    # tool:Read 19/7, tool:Edit (0 + 1 + 1.5)/1.75 = 10/7, mcp:solo 1/1.75 = 4/7;
    # unattributed is the floor (0.75/1.75 = 3/7) and not a row
    assert [s["key"] for s in sinks] == ["model:gap", "user:wait", "tool:Read", "tool:Edit", "mcp:solo"]
    assert [s["mean"] for s in sinks] == pytest.approx([52 / 7, 36 / 7, 19 / 7, 10 / 7, 4 / 7])
    # the per-session mean runs over every session (zeros included), so n_eff is 7/3 for each row
    assert all(s["n_eff"] == pytest.approx(7 / 3) and s["se"] is not None for s in sinks)

    # share of agent-active time for model:gap: active = wall - user:wait = 6, 15, 20 min,
    # (1*4 + .5*10 + .25*16) / (1*6 + .5*15 + .25*20) = 13 / 18.5 = 26/37
    assert sinks[0]["share_of_active"]["mean"] == pytest.approx(26 / 37)


def test_dominant_sha_is_the_largest_summed_decay_weight():
    def dominant(profiles):
        agg = time_profile.aggregate(profiles, half_life_days=7, now_ms=NOW)
        return loss.time_sinks(agg, profiles, ["tool:Read"])[0]["core_sha"]

    cell = {"tool:Read": 1}
    # x: A (weight 1) + B (1/2) = 1.5 against y: C (1/4)
    assert dominant([prof(s, a, 10, cell, sha=h) for s, a, h in
                     (("A", 0, "x"), ("B", 7, "x"), ("C", 14, "y"))]) == "x"
    # y is the most recent (2^(-1/7) = 0.906), but x outweighs it: 2^(-2/7) + 2^(-3/7) = 1.563
    assert dominant([prof("Y", 1, 10, cell, sha="y"), prof("X1", 2, 10, cell, sha="x"),
                     prof("X2", 3, 10, cell, sha="x")]) == "x"
    # x has three sessions but 3 * 2^-3 = 0.375 < y's single 2^-1 = 0.5
    assert dominant([prof("Y", 7, 10, cell, sha="y")] +
                    [prof(f"X{i}", 21, 10, cell, sha="x") for i in range(3)]) == "y"


def test_dominant_sha_tie_goes_to_the_most_recent_session():
    profiles = [prof("OLD", 0, 10, {"tool:Read": 1}, sha="old"),
                prof("NEW", 0, 10, {"tool:Read": 1}, sha="new")]
    profiles[1] = time_profile.SessionProfile(
        **{**profiles[1].__dict__, "last_ts_ms": NOW + 5000, "first_ts_ms": NOW + 5000 - 10 * MIN})
    # the clock precedes both, so both weigh 1 and only the later last_ts decides
    agg = time_profile.aggregate(profiles, half_life_days=7, now_ms=NOW - DAY)

    assert loss.time_sinks(agg, profiles, ["tool:Read"])[0]["core_sha"] == "new"


def test_a_session_without_a_sha_counts_as_unknown_and_children_are_left_out():
    profiles = [prof("A", 0, 10, {"tool:Read": 1}),
                prof("K", 0, 10, {"tool:Read": 1}, sha="kid", child_of="A")]
    agg = time_profile.aggregate(profiles, half_life_days=7, now_ms=NOW)

    assert loss.time_sinks(agg, profiles, ["tool:Read"])[0]["core_sha"] == "unknown"


# ------------------------------------------------------------------ mechanism field

@pytest.mark.parametrize("value", ["unattributed", "tool:Read", "wait:background", "node:APPROVED",
                                   "mcp:srv", "skill:tdd", "hook:hook-a.py", "judge:plan-review"])
def test_a_profile_key_is_a_valid_mechanism(value):
    assert scan.parse_mechanism("r", value) == value


@pytest.mark.parametrize("value", ["", None])
def test_empty_mechanism_clears(value):
    assert scan.parse_mechanism("r", value) == ""


@pytest.mark.parametrize("value", ["bogus", "Tool:Read", "tools:Read", 5, ["tool:Read"]])
def test_an_unknown_mechanism_is_rejected_naming_the_item(value):
    with pytest.raises(ValueError, match="item 'r': 'mechanism'"):
        scan.parse_mechanism("r", value)


def _item(**over):
    base = dict(
        classification="narrow", score=None, rank=None, source_digest="d", title="an item",
        functional_ground="a ground", recommended_next_step="planner", severity="medium",
        severity_labeled=True, cost_to_resolve="small", old_score=1.0,
    )
    base.update(over)
    return scan.PriorBoardItem(**base)


def _classified(**over):
    return {"breadth": "narrow", "cost_to_resolve": "small", "in_flight": "none",
            "recommended_next_step": "planner", "severity": "low", "title": "t",
            "functional_ground": "a ground nobody else shares", **over}


def test_mechanism_is_supplied_carried_and_cleared_at_the_classify_step():
    empty = scan._empty_board()
    board, _f, _n = scan.classify_and_score(empty, {"r": _classified(mechanism="tool:Read")}, [])
    assert board.items["r"].mechanism == "tool:Read"

    again, _f, _n = scan.classify_and_score(board, {"r": _classified()}, [])
    assert again.items["r"].mechanism == "tool:Read"  # absent: kept

    cleared, _f, _n = scan.classify_and_score(board, {"r": _classified(mechanism="")}, [])
    assert cleared.items["r"].mechanism == ""

    with pytest.raises(ValueError, match="'mechanism'"):
        scan.classify_and_score(empty, {"r": _classified(mechanism="nonsense")}, [])


def test_mechanism_survives_the_board_file_and_an_old_board_has_none(tmp_path):
    path = tmp_path / "board.json"
    board = scan.PriorBoard(schema=scan.BOARD_SCHEMA, generated_at="t",
                            items={"r": _item(mechanism="model:gap"), "q": _item()})
    scan.write_board(board, path)

    items = scan.load_prior_board(path).items
    assert (items["r"].mechanism, items["q"].mechanism) == ("model:gap", "")

    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["items"]["r"]["mechanism"]
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert scan.load_prior_board(path).items["r"].mechanism == ""


def test_mechanism_amendment_on_a_carried_item_sets_clears_and_rejects(tmp_path, capsys):
    board = tmp_path / "board.json"
    store = tmp_path / "findings.jsonl"
    scan.write_board(scan.PriorBoard(scan.BOARD_SCHEMA, "t", {"r": _item(title="kept")}), board)

    def amend(change):
        classifications = tmp_path / "classifications.json"
        classifications.write_text(json.dumps({"items": {"r": change}}), encoding="utf-8")
        return scan.main(["backlog", "--board-state", str(board), "--classifications",
                          str(classifications), "--store", str(store)])

    assert amend({"mechanism": "tool:Read"}) == 0
    assert scan.load_prior_board(board).items["r"].mechanism == "tool:Read"
    assert scan.load_prior_board(board).items["r"].title == "kept"

    assert amend({"mechanism": ""}) == 0
    assert scan.load_prior_board(board).items["r"].mechanism == ""

    capsys.readouterr()
    assert amend({"mechanism": "nonsense"}) == 2
    assert "'mechanism'" in capsys.readouterr().err


# --------------------------------------------------------------------------- report

def equal_weight_sessions():
    """Three sessions ending at the same instant, so they weigh the same at any clock:
    the decayed mean is the plain mean and n_eff is 3."""
    last = 1_772_000_000_000
    cells = [
        {"model:gap": 6, "tool:Read": 2, "user:wait": 4},
        {"model:gap": 8, "tool:Read": 4, "user:wait": 4},
        {"model:gap": 7, "tool:Read": 3, "user:wait": 4},
    ]
    return [
        time_profile.SessionProfile(
            session_id=f"S{i}", transcript=f"/x/S{i}.jsonl", first_ts_ms=last - 30 * MIN, last_ts_ms=last,
            partition={k: v * MIN for k, v in cell.items()}, overlays={}, coverage={},
            core_sha="abc", child_of=None, kind="main",
        )
        for i, cell in enumerate(cells)
    ]


class Report:
    def __init__(self, tmp_path, monkeypatch, profiles):
        self.board = tmp_path / "board.json"
        self.store = tmp_path / "findings.jsonl"
        self.state = tmp_path / "profile-state"
        (self.state / "sessions").mkdir(parents=True)
        self.profiles = profiles
        monkeypatch.setattr(
            scan.time_profile, "profile_all",
            lambda state_dir=None, **_kw: time_profile.ProfileRun(profiles=list(self.profiles)),
        )
        scan.write_board(scan.PriorBoard(scan.BOARD_SCHEMA, "t", {
            "mech": _item(title="mechanism item", signatures=("SIG",), mechanism="tool:Read",
                          minutes_per_occurrence=9.0, minutes_basis="guess",
                          loss_measurement={"window_days": 7, "sessions": ["a", "b", "c"]}),
            "plain": _item(title="plain item", signatures=("SIG2",), minutes_per_occurrence=2.0,
                           loss_measurement={"window_days": 7, "sessions": ["a"]}),
        }), self.board)

    def argv(self, *extra):
        return ["report", "--store", str(self.store), "--board", str(self.board), *extra]

    def run(self, capsys, *extra):
        assert scan.main(self.argv(*extra)) == 0
        return capsys.readouterr().out

    def json(self, capsys, *extra):
        return json.loads(self.run(capsys, "--format", "json", *extra))


def test_report_json_charges_the_measured_mean_and_lists_time_sinks(tmp_path, monkeypatch, capsys):
    report = Report(tmp_path, monkeypatch, equal_weight_sessions())
    payload = report.json(capsys, "--time-profile-state", str(report.state))

    # tool:Read 2, 4, 3 min at equal weights: mean 3, n_eff 3, SE^2 = (sum dev^2 = 2) / (3 - 1) / 3 = 1/3
    rows = {r["ref"]: r for r in payload["loss"]}
    assert rows["mech"]["minutes_source"] == "measured"
    assert rows["mech"]["min_per_occurrence"] == pytest.approx(3.0)
    assert rows["mech"]["min_per_week"] == pytest.approx(9.0)  # 3 sessions x 3 min / 1 week, not the guess 27
    assert "measured tool:Read 3 +/- 0.577 min/occurrence" in rows["mech"]["basis"]
    assert (rows["plain"]["minutes_source"], rows["plain"]["min_per_week"]) == ("guess", 2.0)

    # means model:gap (6 + 8 + 7)/3 = 7, user:wait 4, tool:Read 3: decreasing, no unattributed floor
    sinks = payload["time_sinks"]
    assert [s["key"] for s in sinks] == ["model:gap", "user:wait", "tool:Read"]
    assert [s["mean"] for s in sinks] == pytest.approx([7.0, 4.0, 3.0])
    assert [s["se"] for s in sinks] == pytest.approx([math.sqrt(1 / 3), 0.0, math.sqrt(1 / 3)])
    assert all(s["n_eff"] == pytest.approx(3.0) and s["core_sha"] == "abc" for s in sinks)


def test_report_md_has_the_section_right_after_the_loss_lane(tmp_path, monkeypatch, capsys):
    report = Report(tmp_path, monkeypatch, equal_weight_sessions())
    md = report.run(capsys, "--time-profile-state", str(report.state))

    assert md.index("## Measured loss") < md.index("## Where the time goes")
    section = md.split("## Where the time goes")[1].split("\n## ")[0]
    rows = [line for line in section.splitlines() if "**" in line]
    assert [line.split("|")[1].strip() for line in rows] == ["model:gap", "user:wait", "tool:Read"]
    assert "**7.00** ± 0.58" in rows[0] and "| 3.0 | abc |" in rows[0]


@pytest.mark.parametrize("flag", [False, True])
def test_report_on_an_empty_profile_state_says_so_and_raises_nothing(tmp_path, monkeypatch, capsys, flag):
    report = Report(tmp_path, monkeypatch, [])
    extra = ["--time-profile-state", str(report.state)] if flag else []

    payload = report.json(capsys, *extra)
    assert payload["time_sinks"] == []
    assert {r["minutes_source"] for r in payload["loss"]} == {"guess"}
    md = report.run(capsys, *extra)
    assert "## Where the time goes" in md and "No sessions are profiled" in md


def test_report_without_a_profile_cache_does_not_profile(tmp_path, monkeypatch, capsys):
    report = Report(tmp_path, monkeypatch, equal_weight_sessions())
    (report.state / "sessions").rmdir()

    def forbidden(*_a, **_k):
        raise AssertionError("a cold report must not start a profiling pass")

    monkeypatch.setattr(scan.time_profile, "profile_all", forbidden)
    assert report.json(capsys, "--time-profile-state", str(report.state))["time_sinks"] == []


def test_a_bare_time_profile_state_flag_means_the_default_directory(tmp_path, monkeypatch, capsys):
    seen = []
    monkeypatch.setenv("TIME_PROFILE_STATE_DIR", str(tmp_path / "default-state"))
    monkeypatch.setattr(scan, "_time_profile_run", lambda state_dir: seen.append(state_dir))
    store = str(tmp_path / "findings.jsonl")

    assert scan.main(["report", "--store", store, "--time-profile-state", "--format", "json"]) == 0
    assert scan.main(["report", "--store", store, "--format", "json"]) == 0

    assert seen == [str(tmp_path / "default-state"), None]


def test_the_report_functions_stay_free_of_transport_modules():
    from ast_purity import impure_names

    for fn in (scan._cmd_report, scan._render_markdown, scan._render_json, scan._render_time_sinks_md,
               scan._time_profile_run, loss.time_sinks, loss.measured_minutes):
        assert impure_names(fn) == set()
