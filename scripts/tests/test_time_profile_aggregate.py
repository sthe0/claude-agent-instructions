"""Tests for the decayed aggregate and the `agent-report.py time-profile` view.

Hand-computed expectations use three synthetic sessions A, B, C aged 0, 7 and 14 days
at a fixed now_ms, so the half-life-7 weights are 1, 1/2, 1/4 (V1 = 7/4, V2 = 21/16,
n_eff = 7/3). The golden file pins the default JSON view over the committed fixtures
at a fixed clock; delete it to have the test write a fresh one (and fail once, to be reviewed).

The negative control is time_profile_mutants.py: this file must fail under each
stage-2 mutant.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from lib import agent_report, time_profile
from lib.agent_report import time_profile as view
from time_profile_mutants import time_profile_mutant  # noqa: F401  (autouse fixture)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIX = Path(__file__).resolve().parent / "fixtures" / "time_profile"
GOLDEN = FIX / "default_view.golden.json"
COMMIT_INDEX = [(1772366500, "newer"), (1772366400, "onsha"), (5, "old")]

DAY = 86_400_000
MIN = 60_000
FIGURE_KEYS = {"mean", "se", "n_eff"}


def iso_ms(value: str) -> int:
    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    return (dt.datetime.fromisoformat(value.replace("Z", "+00:00")) - epoch) // dt.timedelta(milliseconds=1)


NOW = iso_ms("2026-03-08T00:00:00Z")


def prof(sid, age_days, wall_ms, partition, overlays=None, *, sha=None, child_of=None, kind="main"):
    last = NOW - int(age_days * DAY)
    return time_profile.SessionProfile(
        session_id=sid, transcript=f"/x/{sid}.jsonl", first_ts_ms=last - wall_ms, last_ts_ms=last,
        partition=dict(partition), overlays=dict(overlays or {}), coverage={},
        core_sha=sha, child_of=child_of, kind=kind,
    )


def scenario():
    return [
        prof("A", 0, 10 * MIN, {"user:wait": 4 * MIN, "model:gap": 4 * MIN, "tool:Read": 2 * MIN},
             {"spawn:developer": MIN}, sha="x"),
        prof("B", 7, 20 * MIN, {"user:wait": 400_000, "wait:background": 200_000, "model:gap": 600_000},
             {"node:APPROVED": 30_000}, sha="x"),
        prof("C", 14, 30 * MIN, {"user:wait": 10 * MIN, "model:gap": 16 * MIN, "tool:Read": 4 * MIN},
             {"judge:acceptance": 12_000}, sha="y"),
    ]


def agg(profiles=None, **over):
    kwargs = {"half_life_days": 7, "now_ms": NOW}
    kwargs.update(over)
    return time_profile.aggregate(scenario() if profiles is None else profiles, **kwargs)


def row(result, key):
    return next(r for r in result.rows if r["key"] == key)


def close(figure, mean, se, n_eff):
    assert figure["mean"] == pytest.approx(mean, rel=1e-9)
    if se is None:
        assert figure["se"] is None
    else:
        assert figure["se"] == pytest.approx(se, rel=1e-9)
    assert figure["n_eff"] == pytest.approx(n_eff, rel=1e-9)


def fixture_run(tmp_path):
    return time_profile.profile_all(
        state_dir=tmp_path / "state",
        projects_root=FIX / "projects",
        agentctl_state_dir=FIX / "agentctl_state",
        spawn_ledger=FIX / "ledgers" / "spawn.jsonl",
        judge_ledger_path=FIX / "ledgers" / "judge.jsonl",
        policy_ledger=FIX / "ledgers" / "policy.jsonl",
        commit_index=COMMIT_INDEX,
    )


def run_view(capsys, run, *args):
    code = view.main(list(args), run=run)
    return code, capsys.readouterr().out


# ----------------------------------------------------------------------- weights and config


def test_decay_weights_follow_age_at_the_injected_now():
    result = agg([prof("A", 0, 10 * MIN, {"model:gap": 10 * MIN}), prof("B", 7, 20 * MIN, {"model:gap": 20 * MIN})])
    wall = result.totals["wall_minutes"]
    assert wall["mean"] == pytest.approx((1 * 10 + 0.5 * 20) / 1.5)
    assert wall["n_eff"] == pytest.approx(1.5 ** 2 / 1.25)


def test_a_session_dated_after_now_gets_full_weight():
    result = agg([prof("A", -3, 10 * MIN, {"model:gap": 10 * MIN}), prof("B", 0, 30 * MIN, {"model:gap": 30 * MIN})])
    assert result.totals["wall_minutes"]["mean"] == pytest.approx(20.0)


def write_config(tmp_path, value):
    path = tmp_path / "config.md"
    path.write_text(f"| Key | Value | Meaning |\n|---|---|---|\n| `time-profile-half-life-days` | `{value}` | x |\n")
    return path


def test_default_half_life_follows_config_md(tmp_path, monkeypatch):
    monkeypatch.setattr(time_profile, "CONFIG_PATH", write_config(tmp_path, 14))
    assert time_profile.half_life_days() == 14.0
    result = time_profile.aggregate(
        [prof("A", 0, 10 * MIN, {"model:gap": 10 * MIN}), prof("B", 14, 20 * MIN, {"model:gap": 20 * MIN})],
        now_ms=NOW)
    assert result.half_life_days == 14.0
    assert result.totals["wall_minutes"]["mean"] == pytest.approx((10 + 0.5 * 20) / 1.5)


def test_an_explicit_half_life_overrides_config(tmp_path, monkeypatch):
    monkeypatch.setattr(time_profile, "CONFIG_PATH", write_config(tmp_path, 14))
    pair = [prof("A", 0, 10 * MIN, {"model:gap": 10 * MIN}), prof("B", 14, 20 * MIN, {"model:gap": 20 * MIN})]
    result = time_profile.aggregate(pair, half_life_days=7, now_ms=NOW)
    assert result.half_life_days == 7
    assert result.totals["wall_minutes"]["mean"] == pytest.approx((10 + 0.25 * 20) / 1.25)


@pytest.mark.parametrize("value", ["0", "-3", "soon"])
def test_an_unusable_config_row_is_an_error_not_a_default(tmp_path, value):
    with pytest.raises(ValueError):
        time_profile.half_life_days(write_config(tmp_path, value))


def test_a_missing_config_row_is_an_error(tmp_path):
    path = tmp_path / "config.md"
    path.write_text("| `other` | `1` | x |\n")
    with pytest.raises(ValueError):
        time_profile.half_life_days(path)


def test_both_config_rows_exist_with_value_7():
    assert "| `time-profile-half-life-days` | `7` |" in (REPO_ROOT / "config.md").read_text()
    assert "| `time-profile-half-life-days` | `7` |" in (REPO_ROOT / "docs/operations/config-rationale.md").read_text()
    assert time_profile.half_life_days() == 7.0


def test_the_view_half_life_option_overrides_config(capsys):
    run = time_profile.ProfileRun(profiles=scenario())
    code, out = run_view(capsys, run, "--format", "json", "--half-life", "3")
    assert code == 0
    assert json.loads(out)["half_life_days"] == 3


# --------------------------------------------------------------------------- the estimates


def test_weighted_mean_se_and_n_eff_against_hand_computed_values():
    # Read minutes 2, 0, 4 under weights 1, 1/2, 1/4: mean 3 / 1.75, s^2 = 2.857142857 / 1.0.
    close(row(agg(), "tool:Read")["minutes_per_session"], 12 / 7, 1.1065666703449803, 7 / 3)


def test_absent_keys_are_zero_padded_in_the_per_session_mean():
    # only B (weight 1/2) has 200000 ms of wait:background: mean 0.5 * (10/3) / 1.75;
    # s^2 = (1*m^2 + 0.5*(10/3 - m)^2 + 0.25*m^2) / 1.0
    mean = 0.5 * (200_000 / MIN) / 1.75
    variance = 1.0 * mean ** 2 + 0.5 * (200_000 / MIN - mean) ** 2 + 0.25 * mean ** 2
    close(row(agg(), "wait:background")["minutes_per_session"], mean, (variance / (7 / 3)) ** 0.5, 7 / 3)


def test_minutes_when_present_averages_only_the_sessions_with_the_key():
    present = row(agg(), "tool:Read")["minutes_when_present"]
    assert present["mean"] == pytest.approx((1 * 2 + 0.25 * 4) / 1.25)
    assert present["n_eff"] == pytest.approx(1.25 ** 2 / (1 + 0.0625))
    assert present["se"] is None


def test_occurrence_rate_is_the_decayed_share_of_sessions_with_the_key():
    close(row(agg(), "tool:Read")["occurrence_rate"], 1.25 / 1.75, row(agg(), "tool:Read")["occurrence_rate"]["se"], 7 / 3)
    assert row(agg(), "tool:Read")["occurrence_rate"]["se"] > 0


def test_share_of_wallclock_is_the_ratio_of_decayed_sums():
    assert row(agg(), "tool:Read")["share_of_wallclock"]["mean"] == pytest.approx(
        (1 * 120_000 + 0.25 * 240_000) / (1 * 600_000 + 0.5 * 1_200_000 + 0.25 * 1_800_000))


def test_share_of_active_excludes_user_wait_and_background_wait_from_the_denominator():
    # active ms: A 360000, B 1200000-400000-200000 = 600000, C 1200000
    assert row(agg(), "tool:Read")["share_of_active"]["mean"] == pytest.approx(
        (120_000 + 0.25 * 240_000) / (360_000 + 0.5 * 600_000 + 0.25 * 1_200_000))
    totals = agg().totals
    assert totals["active_minutes"]["mean"] == pytest.approx((6 + 0.5 * 10 + 0.25 * 20) / 1.75)


def test_overlay_keys_get_the_same_figures_and_are_marked_overlay():
    result = agg()
    for key in ("spawn:developer", "node:APPROVED", "judge:acceptance"):
        found = row(result, key)
        assert found["kind"] == "overlay"
        assert {name for name, _ in view.FIGURES} <= set(found)
        assert all(FIGURE_KEYS == set(found[name]) for name, _ in view.FIGURES)
    assert row(result, "tool:Read")["kind"] == "partition"
    assert row(result, "spawn:developer")["share_of_wallclock"]["mean"] == pytest.approx(
        60_000 / (600_000 + 0.5 * 1_200_000 + 0.25 * 1_800_000))


def test_overlay_shares_are_not_forced_to_sum_to_one():
    result = agg()
    total = sum(r["share_of_wallclock"]["mean"] for r in result.rows if r["kind"] == "partition")
    assert total == pytest.approx(1.0)
    assert sum(r["share_of_wallclock"]["mean"] for r in result.rows) > 1.0


def test_replacement_values_change_the_means_under_unchanged_weights():
    base = agg()
    swapped = agg(values={"A": {"model:gap": 8 * MIN, "tool:Read": 2 * MIN}})
    assert row(swapped, "model:gap")["minutes_per_session"]["mean"] != row(base, "model:gap")["minutes_per_session"]["mean"]
    assert row(swapped, "model:gap")["minutes_per_session"]["n_eff"] == pytest.approx(
        row(base, "model:gap")["minutes_per_session"]["n_eff"])
    assert "spawn:developer" not in {r["key"] for r in swapped.rows}


def test_the_active_denominator_is_recomputed_from_replacement_values():
    # A without user:wait is fully active (600000 ms): (120000 + 60000) / (600000 + 300000 + 300000)
    swapped = agg(values={"A": {"model:gap": 8 * MIN, "tool:Read": 2 * MIN}})
    assert row(swapped, "tool:Read")["share_of_active"]["mean"] == pytest.approx(180_000 / 1_200_000)
    assert row(swapped, "tool:Read")["share_of_wallclock"]["mean"] == pytest.approx(
        row(agg(), "tool:Read")["share_of_wallclock"]["mean"])


def test_zero_sessions_return_empty_rows_without_raising():
    result = agg([])
    assert (result.sessions, result.rows, result.children, result.totals) == (0, [], [], {})


def test_se_is_null_below_two_effective_sessions_and_never_zero():
    only = row(agg([prof("A", 0, 10 * MIN, {"tool:Read": 2 * MIN, "model:gap": 8 * MIN})]), "tool:Read")
    for name, _ in view.FIGURES:
        assert only[name]["se"] is None
    assert row(agg(), "wait:background")["minutes_when_present"]["se"] is None
    assert row(agg(), "wait:background")["minutes_per_session"]["se"] is not None


def test_two_equally_weighted_sessions_have_n_eff_two_and_a_defined_se():
    pair = [prof("A", 3, 10 * MIN, {"model:gap": 10 * MIN}), prof("B", 3, 30 * MIN, {"model:gap": 30 * MIN})]
    close(agg(pair).totals["wall_minutes"], 20.0, 10.0, 2.0)


def test_children_are_excluded_from_top_level_and_reported_under_their_kind():
    child = prof("K", 0, 5 * MIN, {"model:gap": 5 * MIN, "tool:Edit": 99}, child_of="A", kind="developer")
    with_child = agg(scenario() + [child])
    alone = agg()
    assert (with_child.sessions, with_child.rows, with_child.totals) == (alone.sessions, alone.rows, alone.totals)
    assert [(c["kind"], c["sessions"]) for c in with_child.children] == [("developer", 1)]
    assert with_child.children[0]["wall_minutes"]["mean"] == pytest.approx(5.0)
    assert alone.children == []


def test_by_sha_groups_the_sessions_of_each_core_sha():
    groups = time_profile.aggregate_by_sha(scenario(), half_life_days=7, now_ms=NOW)
    assert {sha: g.sessions for sha, g in groups.items()} == {"x": 2, "y": 1}
    assert groups["x"].totals["wall_minutes"]["mean"] == pytest.approx((10 + 0.5 * 20) / 1.5)
    assert groups["y"].rows and all(r["minutes_per_session"]["se"] is None for r in groups["y"].rows)
    unnamed = time_profile.aggregate_by_sha([prof("U", 0, MIN, {"model:gap": MIN})], half_life_days=7, now_ms=NOW)
    assert list(unnamed) == ["unknown"]


def test_a_shell_variable_program_key_is_flagged_not_ranked_as_a_mechanism():
    result = agg([prof("A", 0, 10 * MIN, {"tool:Bash:$A": 5 * MIN, "tool:Bash:git": 5 * MIN})])
    assert row(result, "tool:Bash:$A")["flag"] == "shell-variable-program"
    assert row(result, "tool:Bash:git")["flag"] is None


def test_rows_are_ordered_by_decayed_minutes_then_key():
    keys = [r["key"] for r in agg().rows]
    means = [r["minutes_per_session"]["mean"] for r in agg().rows]
    assert means == sorted(means, reverse=True)
    assert keys[0] == "model:gap"


# ------------------------------------------------------------------------ key vocabulary


def test_every_fixture_key_is_in_the_closed_vocabulary(tmp_path):
    run = fixture_run(tmp_path)
    keys = {k for p in run.profiles for k in {**p.partition, **p.overlays}}
    assert keys
    for key in keys:
        assert key.startswith(time_profile.KEY_PREFIXES) or key == time_profile.UNATTRIBUTED, key
    result = time_profile.aggregate(run.profiles, half_life_days=7, now_ms=NOW)
    assert {r["key"] for r in result.rows} <= keys


def test_the_vocabulary_constants_are_the_declared_ones():
    assert time_profile.KEY_PREFIXES == ("user:", "wait:", "model:", "tool:", "skill:", "mcp:", "hook:", "node:",
                                         "spawn:", "judge:")
    assert time_profile.UNATTRIBUTED == "unattributed"
    assert set(time_profile.COUNT_FIELDS) == {"n_eff", "sessions", "profiled", "half_life_days"}


# ------------------------------------------------------------- the JSON shape, the golden


def figure_violations(node, path="$"):
    """Numeric leaves that are neither a figure's mean/se/n_eff nor a COUNT_FIELDS field,
    and figure objects that do not have all three members."""
    bad = []
    if isinstance(node, dict):
        if FIGURE_KEYS & set(node):
            if set(node) != FIGURE_KEYS:
                bad.append(f"{path}: figure members {sorted(node)}")
            for member in FIGURE_KEYS & set(node):
                if node[member] is not None and (isinstance(node[member], bool) or not isinstance(node[member], (int, float))):
                    bad.append(f"{path}.{member}: not a number")
            return bad
        for key, value in node.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if key not in time_profile.COUNT_FIELDS:
                    bad.append(f"{path}.{key}: bare number")
            else:
                bad.extend(figure_violations(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                bad.append(f"{path}[{i}]: bare number")
            else:
                bad.extend(figure_violations(value, f"{path}[{i}]"))
    return bad


def test_the_enumerator_rejects_a_bare_estimate_and_an_incomplete_figure():
    assert figure_violations({"rows": [{"share": 0.3}]})
    assert figure_violations({"rows": [{"share": {"mean": 1.0, "se": None}}]})
    assert not figure_violations({"sessions": 2, "rows": [{"share": {"mean": 1.0, "se": None, "n_eff": 1.0}}]})


def view_json(capsys, run, *args):
    code, out = run_view(capsys, run, "--format", "json", *args)
    assert code == 0
    return json.loads(out)


def test_every_numeric_leaf_of_the_json_view_is_a_figure_member_or_a_count(capsys):
    run = time_profile.ProfileRun(profiles=scenario())
    assert figure_violations(view_json(capsys, run, "--by-sha")) == []


def test_the_json_view_has_the_documented_top_level(capsys):
    payload = view_json(capsys, time_profile.ProfileRun(profiles=scenario()))
    assert {"half_life_days", "run", "rows", "sessions", "totals", "children"} <= set(payload)
    assert set(payload["run"]) == {"profiled", "skipped", "reparsed"}
    assert payload["run"]["profiled"] == 3
    assert "groups" not in payload
    assert {g["core_sha"]: g["sessions"] for g in view_json(
        capsys, time_profile.ProfileRun(profiles=scenario()), "--by-sha")["groups"]} == {"x": 2, "y": 1}


def relative_paths(payload):
    payload["run"]["reparsed"] = [str(Path(p).relative_to(REPO_ROOT)) for p in payload["run"]["reparsed"]]
    return payload


def test_golden_default_view_on_the_fixtures(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(time_profile, "_clock_ms", lambda: NOW)
    monkeypatch.setattr(time_profile, "half_life_days", lambda config_path=None: 7.0)
    payload = relative_paths(view_json(capsys, fixture_run(tmp_path)))
    assert figure_violations(payload) == []
    if not GOLDEN.exists():
        GOLDEN.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        pytest.fail(f"{GOLDEN.name} did not exist and was written; review it and rerun")
    golden = json.loads(GOLDEN.read_text())
    assert payload == golden
    assert not any(isinstance(v, str) and v.startswith("/") for v in golden["run"]["reparsed"])


# --------------------------------------------------------------------------- text render


def test_text_render_prints_every_estimate_with_se_and_n_eff(capsys):
    code, out = run_view(capsys, time_profile.ProfileRun(profiles=scenario()))
    assert code == 0
    detail = [line for line in out.splitlines() if line.startswith("    ")]
    assert len(detail) == 5 * len(agg().rows)
    assert all("±" in line and "n_eff" in line for line in detail)
    assert "± n/a (n_eff" in out
    assert "children" not in out


def test_text_render_lists_skipped_sessions_with_their_reasons(capsys):
    run = time_profile.ProfileRun(profiles=scenario(), skipped=[time_profile.Skipped("s1", "/x/s1.jsonl", "empty")])
    _, out = run_view(capsys, run)
    assert "skipped 1" in out
    assert "skipped s1: empty" in out


# ----------------------------------------------------------------------------- --check


def test_check_passes_on_sound_data_and_prints_the_counts(capsys):
    code, out = run_view(capsys, time_profile.ProfileRun(profiles=scenario()), "--check")
    assert code == 0
    assert "profiled: 3, skipped: 0" in out


def test_check_fails_on_zero_profiled_sessions(capsys):
    code, out = run_view(capsys, time_profile.ProfileRun(), "--check")
    assert code == 1
    assert "profiled: 0, skipped: 0" in out
    assert "no session was profiled" in out


def test_check_fails_on_a_conservation_breaking_session(capsys):
    broken = prof("BAD", 0, 10 * MIN, {"model:gap": 10 * MIN - 1})
    code, out = run_view(capsys, time_profile.ProfileRun(profiles=scenario() + [broken]), "--check")
    assert code == 1
    assert "BAD: partition does not sum to wall-clock (error -1 ms)" in out


def test_check_fails_on_an_estimate_without_se_although_n_eff_is_two_or_more(capsys, monkeypatch):
    monkeypatch.setattr(time_profile, "figure", lambda mean, se, n_eff: {"mean": mean, "se": None, "n_eff": n_eff})
    code, out = run_view(capsys, time_profile.ProfileRun(profiles=scenario()), "--check")
    assert code == 1
    assert "se is null although n_eff" in out


def test_check_fails_on_an_estimate_missing_a_member(capsys, monkeypatch):
    monkeypatch.setattr(time_profile, "figure", lambda mean, se, n_eff: {"mean": mean, "se": se})
    code, out = run_view(capsys, time_profile.ProfileRun(profiles=scenario()), "--check")
    assert code == 1
    assert "lacks n_eff" in out


def test_check_lists_skipped_sessions_and_fails_above_the_skip_fraction(capsys):
    run = time_profile.ProfileRun(
        profiles=scenario(),
        skipped=[time_profile.Skipped("s1", "/x/s1.jsonl", "empty"),
                 time_profile.Skipped("s2", "/x/s2.jsonl", "unparseable", "ValueError: boom")])
    code, out = run_view(capsys, run, "--check")
    assert code == 1
    assert "profiled: 3, skipped: 2" in out
    assert "skipped s1: empty" in out
    assert "skipped s2: unparseable (ValueError: boom)" in out
    assert "2 of 5 sessions skipped" in out
    code, _ = run_view(capsys, run, "--check", "--max-skip-fraction", "0.5")
    assert code == 0


def test_check_passes_on_the_committed_fixtures(tmp_path, capsys):
    code, out = run_view(capsys, fixture_run(tmp_path), "--check")
    assert code == 0, out
    assert "profiled: 3, skipped: 0" in out


def test_the_view_is_registered_and_its_help_runs(capsys):
    assert agent_report.VIEWS["time-profile"] == "time_profile"
    with pytest.raises(SystemExit) as stop:
        agent_report.run("time-profile", ["--help"])
    assert stop.value.code == 0
    assert "--by-sha" in capsys.readouterr().out
