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

import hashlib
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


def test_normalize_outside_diagnosing_refused_at_resolved(store, fixtures_dir):
    """normalize's docstring (and the stage-1 plan image) says CLASSIFIED to
    RESOLUTION: a record filed after `resolve` never reaches the quality row, so
    it must be refused rather than silently accepted and lost."""
    from agentctl import cli
    from conftest import STAGE_OBSERVATIONS

    plan = str(fixtures_dir / "plan_two_stage.toml")
    sid = "r9-a3"
    _to_executing_stage1(store, sid, plan)
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
    cli.cmd_resolve(ns(session=sid, by="user", quality=5, quality_by="user-confirmed",
                       quality_note=None, cost_log=None), store=store)

    d = cli.cmd_normalize(ns(session=sid, factor="too late", level=None, destination=None),
                          store=store)

    assert d.ok is False
    state = store.load(sid)
    assert not [h for h in state.history if h.get("event") == "normalize"]


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


def test_replan_normalize_factor_in_diagnosing_satisfies_the_gate(store, fixtures_dir):
    """Finding 1: inside DIAGNOSING, --normalize-factor must satisfy
    gates.normalization_blockers in the SAME call -- not merely log an event that
    the gate then ignores, refusing the replan it was passed to unblock."""
    from agentctl import cli
    from agentctl.state import Node

    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    sid = "r9-b2"
    _to_diagnosing(store, sid, plan)

    d = cli.cmd_replan(ns(session=sid, plan=refined, normalize_factor="flaky harness",
                          normalize_level="leaf"), store=store)

    assert d.ok is True, d.detail
    state = store.load(sid)
    assert state.node == Node.VERIFYING.value  # DIAGNOSING closed, not still blocked
    events = [h for h in state.history if h.get("event") == "normalize"]
    assert len(events) == 1
    assert "in_diagnosis" not in events[0]  # matches cmd_normalize's own DIAGNOSING shape
    assert events[0]["factor"] == "flaky harness"


def test_replan_normalize_factor_and_renormalize_refused_together(store, fixtures_dir):
    """The narrow incompatibility check itself (a renormalization by contract touches
    no norm): refused before any state is touched, so this alone says nothing about
    a LATER gate refusing a --normalize-factor replan -- see the next test."""
    from agentctl import cli

    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    sid = "r9-b3"
    _to_executing_stage1(store, sid, plan)

    d1 = cli.cmd_replan(ns(session=sid, plan=refined, normalize_factor="dup check",
                           normalize_level=None, renormalize=True,
                           coverage_waiver=None), store=store)
    assert d1.ok is False  # --normalize-factor + --renormalize refused together

    state = store.load(sid)
    assert not [h for h in state.history if h.get("event") == "normalize"]


def test_replan_normalize_factor_not_double_logged_on_refused_retry(store, fixtures_dir, monkeypatch):
    """Required change B (round-2 review, defect A): drive a DIAGNOSING
    --normalize-factor replan against a plan whose retitled stage moves the
    order bag's covering-stage key. It reaches the bag save inside cmd_replan's
    plan_approval-plugin block -- the exact save defect A rode an orphaned
    normalization onto disk across -- and is THEN refused by the plan_approval
    plugin gate itself, strictly past that save and strictly
    before the single logging point at the end of cmd_replan. Pre-fix,
    state.difficulty.normalization was set (and gates.normalization_blockers
    satisfied) before this save ran, so the refusal left a persisted record with
    no matching normalize event, and a retry that dropped the now-redundant-
    looking flag never logged one either. Post-fix, the gate is satisfied by the
    pending --normalize-factor ARGUMENT instead, so nothing is written to
    state.difficulty until past every refusal -- the save that does happen here
    must carry no orphaned normalization and no normalize event."""
    from agentctl import cli
    from agentctl.state import Node

    monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)

    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    sid = "r9-b3b"

    cli.cmd_start(ns(session=sid, task="r9-demo", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    assert "premise" in store.load(sid).plugins  # gate really is live

    cli.cmd_order_raise(ns(session=sid, id="O1", element="the order this plan answers"),
                        store=store)
    cli.cmd_order_dispose(ns(session=sid, id="O1", as_="covered", stage=1, reason=""),
                          store=store)
    assert cli.cmd_approve(ns(session=sid, by="user"), store=store).ok is True
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)

    cli.cmd_record_result(ns(session=sid, status="failed", actual="boom"), store=store)
    cli.cmd_declare(ns(session=sid, expected="e", actual="a", mismatch="m"), store=store)
    cli.cmd_investigate(ns(session=sid, localized_expectation="le", localized_actual="la",
                           hypotheses=["h1", "h2"]), store=store)
    cli.cmd_critique(ns(session=sid, functional_ground="fg", replanning_task="rt",
                        failure_address="нормативное"), store=store)

    # `refined` retitles stage 1, moving O1's covering-stage key (#123), so the
    # plan_approval plugin gate refuses it on the order bag -- strictly past
    # the save in cmd_replan and strictly before the single logging point at its
    # end, the save-then-refuse-after ordering defect A persisted an orphan across.
    d1 = cli.cmd_replan(ns(session=sid, plan=refined, normalize_factor="dup check",
                           normalize_level=None), store=store)
    assert d1.ok is False
    assert d1.action == "close_questions"
    assert any("[premise]" in b for b in d1.data.get("blockers", []))

    state = store.load(sid)
    assert state.node == Node.DIAGNOSING.value
    assert not [h for h in state.history if h.get("event") == "normalize"]
    assert state.difficulty.normalization is None

    # re-cover against the refined plan by name
    d2b = cli.cmd_order_dispose(ns(session=sid, id="O1", as_="covered", stage=1,
                                   reason="", plan=refined), store=store)
    assert d2b.ok is True

    d3 = cli.cmd_replan(ns(session=sid, plan=refined, normalize_factor="dup check",
                           normalize_level=None), store=store)
    assert d3.ok is True, d3.detail
    state = store.load(sid)
    events = [h for h in state.history if h.get("event") == "normalize"]
    assert len(events) == 1


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


# The prior two tests only ever drive the "refinement" kind. The version snapshot
# is written unconditionally, above every kind branch -- cover the other two
# `cmd_replan` kinds plus the separate `_renormalize_replan` path.
_DIGEST_REPLAN_KINDS = {
    "no_change": "plan_two_stage.toml",
    "substantive": "plan_two_stage_substantive.toml",
}


@pytest.mark.parametrize("kind", sorted(_DIGEST_REPLAN_KINDS))
def test_replan_snapshots_both_plan_versions_on_every_kind(store, fixtures_dir, kind):
    from agentctl import cli
    from agentctl.plan import diff_plans, load_plan

    plan = str(fixtures_dir / "plan_two_stage.toml")
    new_plan = str(fixtures_dir / _DIGEST_REPLAN_KINDS[kind])
    sid = f"r9-digest-{kind}"
    _to_executing_stage1(store, sid, plan)
    assert diff_plans(load_plan(Path(plan), strict=False), load_plan(Path(new_plan))) == kind

    d = cli.cmd_replan(ns(session=sid, plan=new_plan), store=store)
    assert d.ok is True, d.detail

    prev_digest = hashlib.sha256(Path(plan).read_bytes()).hexdigest()
    new_digest = hashlib.sha256(Path(new_plan).read_bytes()).hexdigest()
    root = store.root
    assert (root / f"plan-version-{prev_digest[:16]}.toml").exists()
    assert (root / f"plan-version-{new_digest[:16]}.toml").exists()


def test_renormalize_snapshots_both_plan_versions(store, tmp_path):
    from test_renormalization import _approved, _renormalize, _write_plan

    plan = _write_plan(tmp_path / "p.toml")
    _approved(store, plan)
    resequenced = _write_plan(
        tmp_path / "q.toml",
        procedure='procedure = "1. Reorder the steps. 2. Run the tests."\n',
    )

    d = _renormalize(store, resequenced)

    assert d.ok is True, d.data
    state = store.load("rn")
    events = [h for h in state.history if h.get("event") == "renormalize"]
    assert len(events) == 1
    prev_digest = hashlib.sha256(Path(plan).read_bytes()).hexdigest()
    new_digest = hashlib.sha256(Path(resequenced).read_bytes()).hexdigest()
    root = store.root
    assert (root / f"plan-version-{prev_digest[:16]}.toml").exists()
    assert (root / f"plan-version-{new_digest[:16]}.toml").exists()


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


def test_replan_directive_no_hint_when_diagnosing_normalize_already_recorded(store, fixtures_dir):
    """Finding 5: a separate `normalize` call satisfying the DIAGNOSING gate
    already recorded a normalization for this cycle -- the ensuing replan must
    not claim "no normalize event recorded", which the gate itself refutes."""
    from agentctl import cli

    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    sid = "r9-f3"
    _to_diagnosing(store, sid, plan)
    cli.cmd_normalize(ns(session=sid, factor="separately recorded", level=None,
                         destination=None), store=store)

    d = cli.cmd_replan(ns(session=sid, plan=refined), store=store)

    assert d.ok is True, d.detail
    advisories = d.data.get("advisories", [])
    assert not any("--normalize-factor" in a for a in advisories)


# --- (c) scorecard re-norming section --------------------------------------------

@pytest.fixture
def scorecard(monkeypatch, tmp_path):
    """The six ledger/root patches every scorecard test in this section needs,
    pointed at an empty tmp_path so no real machine state leaks in."""
    ps = _load_scorecard_module()
    monkeypatch.setattr(ps, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(ps, "TASK_QUALITY_LEDGER", tmp_path / "task-quality.jsonl")
    monkeypatch.setattr(ps, "projects_roots", lambda: [tmp_path / "projects"])
    monkeypatch.setattr(ps, "GATE_LOGS", (tmp_path / "no-gate-log.jsonl",))
    monkeypatch.setattr(ps, "REPO_ROOT", tmp_path / "no-instrepo")
    monkeypatch.setattr(ps, "SPAWN_LEDGER", tmp_path / "no-spawn-ledger.jsonl")
    return ps


def test_scorecard_reports_renorm_coverage(scorecard, tmp_path):
    import datetime as dt

    ps = scorecard
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
    # (c): the quality-mean split, not only coverage -- s1 (quality 5) has a
    # normalization, s2 (quality 3) doesn't.
    assert "with normalization: **5.0**" in out
    assert "without: **3.0**" in out


def test_scorecard_renorm_section_skips_rows_missing_new_fields_gracefully(scorecard, tmp_path):
    """Invariant: an older quality row lacking n_normalizations must not crash
    the scorecard -- it is counted as uncovered (0), not excluded."""
    import datetime as dt

    ps = scorecard
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


def test_scorecard_renorm_section_absent_ledger_degrades_gracefully(scorecard):
    out = scorecard.scorecard({}, days=7, project=None)

    assert "## Re-norming" in out
    assert "no task-quality rows found" in out
