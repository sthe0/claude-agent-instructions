"""R9: re-norming outside DIAGNOSING, content-addressed plan versions on every
submit/replan, re-norming counts in the quality row, and a scorecard section.

Widen-don't-fork (the plan's stated method): `normalize` keeps one event name
with an `in_diagnosis` flag so every existing reader sees both flavors;
snapshots reuse the content-addressed writer pattern; counts are derived from
history, not new mutable state. No new gate: recording is optional and cheap
by construction (REQ11) -- a replan with no --normalize-factor still succeeds,
merely carrying a non-blocking hint.
"""
from __future__ import annotations

import importlib.util
import json
from argparse import Namespace
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent


def ns(**kw):
    return Namespace(**kw)


def _load_scorecard_module():
    spec = importlib.util.spec_from_file_location(
        "policy_scorecard_under_test_r9", SCRIPTS_DIR / "policy-scorecard.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _to_executing_stage1(store, sid, plan):
    from agentctl import cli
    cli.cmd_start(ns(session=sid, task="r9-demo", goal="", done_criterion="",
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


def _to_diagnosing(store, sid, plan):
    """Drive a session to a completed DIAGNOSING cycle (declared, investigated,
    critiqued -- but not yet normalized), so a DIAGNOSING-branch normalize call
    can be exercised and diffed against the outside-DIAGNOSING branch."""
    from agentctl import cli
    _to_executing_stage1(store, sid, plan)
    cli.cmd_record_result(ns(session=sid, status="failed", actual="boom"), store=store)
    cli.cmd_declare(ns(session=sid, expected="e", actual="a", mismatch="m"), store=store)
    cli.cmd_investigate(ns(session=sid, localized_expectation="le", localized_actual="la",
                           hypotheses=["h1", "h2"]), store=store)
    cli.cmd_critique(ns(session=sid, functional_ground="fg", replanning_task="rt",
                        failure_address="нормативное"), store=store)


@pytest.fixture(autouse=True)
def _no_replan_authorization_gate(monkeypatch):
    """Mirrors test_replan.py: this module exercises bare refinement/no_change
    replans without a user-facing diff presentation; that gate's own scoping is
    covered in test_replan_authorization.py, not here."""
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")


# --- (a) normalize outside DIAGNOSING -----------------------------------------

def test_normalize_outside_diagnosing_is_recorded(store, fixtures_dir):
    from agentctl import cli
    from agentctl.state import Node

    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_executing_stage1(store, "r9-a1", plan)

    d = cli.cmd_normalize(ns(session="r9-a1", factor="flaky CI runner", level="leaf",
                             destination=None), store=store)

    assert d.ok is True
    assert d.action == "continue"
    state = store.load("r9-a1")
    assert state.node == Node.EXECUTING.value
    assert state.difficulty is None  # never touched -- there is none to touch
    events = [h for h in state.history if h.get("event") == "normalize"]
    assert len(events) == 1
    assert events[0]["in_diagnosis"] is False
    assert events[0]["factor"] == "flaky CI runner"


def test_normalize_diagnosing_path_byte_identical_no_in_diagnosis_key(store, fixtures_dir):
    """Invariant: the DIAGNOSING branch's log call stays exactly as it was
    before R9 -- no `in_diagnosis` key at all (not even True), so a legacy
    reader that never heard of the key still sees the same event shape."""
    from agentctl import cli
    from agentctl.state import Node

    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_diagnosing(store, "r9-a2", plan)

    d = cli.cmd_normalize(ns(session="r9-a2", factor="reproducible cause", level="leaf",
                             destination=None), store=store)

    assert d.ok is True and d.action == "replan"
    state = store.load("r9-a2")
    assert state.node == Node.DIAGNOSING.value
    events = [h for h in state.history if h.get("event") == "normalize"]
    assert len(events) == 1
    assert "in_diagnosis" not in events[0]
    assert state.difficulty.normalization.factor == "reproducible cause"


# --- (b)/(a) --normalize-factor on replan -------------------------------------

def test_replan_normalize_factor_records_in_same_call(store, fixtures_dir):
    from agentctl import cli

    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_executing_stage1(store, "r9-b1", plan)

    d = cli.cmd_replan(ns(session="r9-b1", plan=refined, normalize_factor="stale fixture",
                          normalize_level="note"), store=store)

    assert d.ok is True
    state = store.load("r9-b1")
    events = [h for h in state.history if h.get("event") == "normalize"]
    assert len(events) == 1
    assert events[0]["factor"] == "stale fixture"
    assert events[0]["in_diagnosis"] is False
    # the replan itself still went through -- no new gate on this flag's absence/presence
    replans = [h for h in state.history if h.get("event") == "replan"]
    assert len(replans) == 1


# --- (b) every submitted plan version is content-addressed --------------------

def test_every_submitted_plan_version_is_snapshotted(store, fixtures_dir):
    from agentctl import cli

    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    sid = "r9-c1"
    _to_executing_stage1(store, sid, plan)
    cli.cmd_replan(ns(session=sid, plan=refined), store=store)

    state = store.load(sid)
    submit_events = [h for h in state.history if h.get("event") == "submit_plan"]
    replan_events = [h for h in state.history if h.get("event") == "replan"]
    assert submit_events and submit_events[0].get("plan_sha256")
    assert replan_events and replan_events[0].get("plan_sha256")
    assert replan_events[0].get("prev_plan_sha256")

    root = store.root
    for digest in (submit_events[0]["plan_sha256"], replan_events[0]["plan_sha256"],
                   replan_events[0]["prev_plan_sha256"]):
        snap = root / f"plan-version-{digest[:16]}.toml"
        assert snap.exists(), f"missing snapshot for digest {digest}"


# --- (b) replan event carries both digests, each resolvable -------------------

def test_replan_event_carries_prev_and_new_digest(store, fixtures_dir):
    from agentctl import cli

    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    sid = "r9-d1"
    _to_executing_stage1(store, sid, plan)

    cli.cmd_replan(ns(session=sid, plan=refined), store=store)

    state = store.load(sid)
    event = [h for h in state.history if h.get("event") == "replan"][-1]
    prev_digest = event["prev_plan_sha256"]
    new_digest = event["plan_sha256"]
    assert prev_digest and new_digest and prev_digest != new_digest

    root = store.root
    assert (root / f"plan-version-{prev_digest[:16]}.toml").exists()
    assert (root / f"plan-version-{new_digest[:16]}.toml").exists()
    # the new snapshot's bytes are literally the refined plan's bytes
    assert (root / f"plan-version-{new_digest[:16]}.toml").read_bytes() == \
        Path(refined).read_bytes()


# --- (c) quality row counts -----------------------------------------------------

def _drive_to_resolved_with_normalizations(store, sid, plan, refined):
    from agentctl import cli
    from conftest import STAGE_OBSERVATIONS

    cli.cmd_start(ns(session=sid, task="r9-quality", goal="g", done_criterion="dc",
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

    # one normalize outside DIAGNOSING, in-thread
    cli.cmd_normalize(ns(session=sid, factor="pre-existing gap", level="note",
                         destination=None), store=store)
    # one plain replan carrying its own normalize-factor
    cli.cmd_replan(ns(session=sid, plan=refined, normalize_factor="second cause",
                      normalize_level=None), store=store)

    for observation in STAGE_OBSERVATIONS[:2]:
        cli.cmd_next_stage(ns(session=sid), store=store)
        cli.cmd_record_result(ns(session=sid, status="passed", actual="ok",
                                 control="reviewed: ok", observation=observation,
                                 cost_log=None), store=store)
    cli.cmd_verify_final(ns(session=sid), store=store)
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="searched", note=""),
                          store=store)
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="skipped",
                             note="test fixture, nothing to record"), store=store)
    return cli.cmd_resolve(ns(session=sid, by="user", quality=5, quality_by="user-confirmed",
                              quality_note=None, cost_log=None), store=store)


def test_quality_row_counts_normalizations(store, fixtures_dir):
    from agentctl import cli

    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    d = _drive_to_resolved_with_normalizations(store, "r9-e1", plan, refined)
    assert d.ok is True

    rows = [json.loads(line) for line in cli.TASK_QUALITY_LOG.read_text(encoding="utf-8")
           .splitlines() if line.strip()]
    row = rows[-1]
    assert row["n_normalizations"] == 2
    assert row["n_normalizations_outside_diagnosis"] == 2
    assert row["n_renormalize_replans"] == 0


def test_quality_row_old_rows_without_new_fields_still_load(tmp_path):
    """Invariant: a pre-R9 quality row lacking the three new keys must not
    error when read back -- json.loads on the raw line is all `resolve`
    itself ever assumed, so this is really testing the reader side (the
    scorecard aggregate below), not cli.py's writer."""
    legacy_line = json.dumps({"ts": "2026-01-01T00:00:00Z", "task_id": "t", "session": "s",
                              "quality": 4, "quality_by": "user", "quality_note": None,
                              "resolved_by": "user", "instructions_head": None,
                              "n_stages": 1, "n_failed_stage_results": 0, "n_replans": 0,
                              "n_difficulty_records": 0, "spawn_count": 0,
                              "total_cost_usd": 0.0})
    path = tmp_path / "legacy-quality.jsonl"
    path.write_text(legacy_line + "\n", encoding="utf-8")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows[0].get("n_normalizations", 0) == 0


# --- (d) non-blocking replan hint ----------------------------------------------

def test_replan_directive_hints_normalize_factor(store, fixtures_dir):
    from agentctl import cli

    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    sid = "r9-f1"
    _to_executing_stage1(store, sid, plan)

    d = cli.cmd_replan(ns(session=sid, plan=refined), store=store)

    assert d.ok is True  # no new blocker on the flag's absence (REQ11)
    advisories = d.data.get("advisories", [])
    assert any("--normalize-factor" in a for a in advisories)


def test_replan_directive_no_hint_when_normalize_factor_given(store, fixtures_dir):
    from agentctl import cli

    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    sid = "r9-f2"
    _to_executing_stage1(store, sid, plan)

    d = cli.cmd_replan(ns(session=sid, plan=refined, normalize_factor="cause",
                          normalize_level=None), store=store)

    assert d.ok is True
    advisories = d.data.get("advisories", [])
    assert not any("--normalize-factor" in a for a in advisories)


# --- (c) scorecard re-norming section --------------------------------------------

def test_scorecard_reports_renorm_coverage(monkeypatch, tmp_path):
    import datetime as dt

    ps = _load_scorecard_module()
    monkeypatch.setattr(ps, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(ps, "TASK_QUALITY_LEDGER", tmp_path / "task-quality.jsonl")
    monkeypatch.setattr(ps, "projects_roots", lambda: [tmp_path / "projects"])
    monkeypatch.setattr(ps, "GATE_LOGS", (tmp_path / "no-gate-log.jsonl",))
    monkeypatch.setattr(ps, "REPO_ROOT", tmp_path / "no-instrepo")
    monkeypatch.setattr(ps, "SPAWN_LEDGER", tmp_path / "no-spawn-ledger.jsonl")

    now = dt.datetime.now(dt.timezone.utc)
    ts = now.isoformat()

    def quality_row(session, quality, n_normalizations):
        return {"ts": ts, "task_id": "t", "session": session, "quality": quality,
                "quality_by": "user", "quality_note": None, "resolved_by": "user",
                "instructions_head": None, "n_stages": 1, "n_failed_stage_results": 0,
                "n_replans": 0, "n_difficulty_records": 0, "spawn_count": 0,
                "total_cost_usd": 0.0, "n_normalizations": n_normalizations,
                "n_normalizations_outside_diagnosis": n_normalizations,
                "n_renormalize_replans": 0}

    ps.TASK_QUALITY_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with ps.TASK_QUALITY_LEDGER.open("w", encoding="utf-8") as f:
        f.write(json.dumps(quality_row("s1", 5, 1)) + "\n")
        f.write(json.dumps(quality_row("s2", 3, 0)) + "\n")

    out = ps.scorecard({}, days=7, project=None)

    assert "## Re-norming" in out
    assert "Coverage" in out
    assert "50%" in out
    assert "(1/2)" in out


def test_scorecard_renorm_section_skips_rows_missing_new_fields_gracefully(monkeypatch, tmp_path):
    """Invariant: a pre-R9 quality row lacking n_normalizations must not crash
    the scorecard -- it is counted as uncovered (0), not excluded."""
    import datetime as dt

    ps = _load_scorecard_module()
    monkeypatch.setattr(ps, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(ps, "TASK_QUALITY_LEDGER", tmp_path / "task-quality.jsonl")
    monkeypatch.setattr(ps, "projects_roots", lambda: [tmp_path / "projects"])
    monkeypatch.setattr(ps, "GATE_LOGS", (tmp_path / "no-gate-log.jsonl",))
    monkeypatch.setattr(ps, "REPO_ROOT", tmp_path / "no-instrepo")
    monkeypatch.setattr(ps, "SPAWN_LEDGER", tmp_path / "no-spawn-ledger.jsonl")

    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    legacy_row = {"ts": ts, "task_id": "t", "session": "s1", "quality": 4,
                  "quality_by": "user", "quality_note": None, "resolved_by": "user",
                  "instructions_head": None, "n_stages": 1, "n_failed_stage_results": 0,
                  "n_replans": 0, "n_difficulty_records": 0, "spawn_count": 0,
                  "total_cost_usd": 0.0}
    ps.TASK_QUALITY_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    ps.TASK_QUALITY_LEDGER.write_text(json.dumps(legacy_row) + "\n", encoding="utf-8")

    out = ps.scorecard({}, days=7, project=None)  # must not raise

    assert "## Re-norming" in out
    assert "0%" in out


def test_scorecard_renorm_section_absent_ledger_degrades_gracefully(tmp_path, monkeypatch):
    ps = _load_scorecard_module()
    monkeypatch.setattr(ps, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(ps, "TASK_QUALITY_LEDGER", tmp_path / "task-quality.jsonl")
    monkeypatch.setattr(ps, "projects_roots", lambda: [tmp_path / "projects"])
    monkeypatch.setattr(ps, "GATE_LOGS", (tmp_path / "no-gate-log.jsonl",))
    monkeypatch.setattr(ps, "REPO_ROOT", tmp_path / "no-instrepo")
    monkeypatch.setattr(ps, "SPAWN_LEDGER", tmp_path / "no-spawn-ledger.jsonl")

    out = ps.scorecard({}, days=7, project=None)

    assert "## Re-norming" in out
    assert "no task-quality rows found" in out
