"""Stage 5: scope-aware plan-review coverage. A whole-plan review still covers
whatever it passed on as long as its recorded keys still match; a stage whose
key moved owes its own stage-scoped pass at the CURRENT key; a moved meta/order
always forces a fresh whole-plan review regardless of how current any
stage-scoped review is.

Group 1 locks the plan_stage_reviews-empty default byte-identical to
test_plan_review_gate.py's pre-scope assertions: the coverage branch
(gates._plan_review_blockers_coverage) only activates once a session has
recorded at least one stage-scoped review."""
from __future__ import annotations

import hashlib
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, gates
from agentctl.plan import load_plan, plan_meta_digest, plan_stage_digests
from agentctl.state import Node, PlanReview, SessionState


def ns(**kw):
    return Namespace(**kw)


def _subst(**kw) -> SessionState:
    kw.setdefault("plan_path", "/plan.toml")
    return SessionState(session_id="s", task_id="t", weight_class="SUBSTANTIVE",
                        plan_verified=True, **kw)


def _sha256_file(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _to_plan_ready(store, sid, plan):
    cli.cmd_start(ns(session=sid, task="demo", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)


def _whole_review(plan_path, doc, **kw) -> PlanReview:
    kw.setdefault("verdict", "pass")
    kw.setdefault("reviewer", "thinker")
    kw.setdefault("attest", True)
    attest = kw.pop("attest")
    return PlanReview(
        plan_path=str(plan_path), scope="",
        plan_sha256=(_sha256_file(plan_path) if attest else ""),
        reviewed_meta_digest=plan_meta_digest(doc),
        reviewed_stage_keys={str(k): v for k, v in plan_stage_digests(doc).items()},
        **kw,
    )


def _stage_review(plan_path, doc, index, **kw) -> PlanReview:
    kw.setdefault("verdict", "pass")
    kw.setdefault("reviewer", "thinker")
    kw.setdefault("attest", True)
    attest = kw.pop("attest")
    return PlanReview(
        plan_path=str(plan_path), scope=f"stage:{index}",
        plan_sha256=(_sha256_file(plan_path) if attest else ""),
        reviewed_meta_digest=plan_meta_digest(doc),
        reviewed_stage_keys={str(k): v for k, v in plan_stage_digests(doc).items()},
        **kw,
    )


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")


# --- 1. plan_stage_reviews-empty default: byte-identical to the pre-scope gate ---

def test_default_missing_review_blocks(gate_on):
    blockers = gates.plan_review_blockers(_subst(), "/plan.toml")
    assert blockers and "no thinker review" in blockers[0]


def test_default_path_stale_blocks(gate_on):
    s = _subst(plan_review=PlanReview("/OLD.toml", "pass", "thinker"))
    blockers = gates.plan_review_blockers(s, "/plan.toml")
    assert blockers and "stale" in blockers[0]


def test_default_content_stale_blocks(gate_on, tmp_path):
    plan = tmp_path / "plan.toml"
    plan.write_text("index = 1\n")
    s = _subst(plan_path=str(plan),
               plan_review=PlanReview(str(plan), "pass", "thinker", plan_sha256=_sha256_file(plan)))
    plan.write_text("index = 2\n")
    blockers = gates.plan_review_blockers(s, str(plan))
    assert blockers and "content" in blockers[0] and "changed since it was reviewed" in blockers[0]


def test_default_unattested_pass_blocks(gate_on):
    s = _subst(plan_review=PlanReview("/plan.toml", "pass", "thinker"))
    blockers = gates.plan_review_blockers(s, "/plan.toml")
    assert blockers and "not attested" in blockers[0]


def test_default_revise_blocks(gate_on):
    s = _subst(plan_review=PlanReview("/plan.toml", "revise", "thinker"))
    blockers = gates.plan_review_blockers(s, "/plan.toml")
    assert blockers and "revise" in blockers[0]


def test_default_override_clears(gate_on):
    s = _subst(plan_review=PlanReview("/plan.toml", "override", "fedor", note="deadlock"))
    assert gates.plan_review_blockers(s, "/plan.toml") == []


# --- 2. PlanReview.from_dict: legacy and malformed records -------------------

def test_from_dict_legacy_record_loads_as_whole_plan_review_with_empty_keys():
    legacy = {"plan_path": "/plan.toml", "verdict": "pass", "reviewer": "thinker"}
    pr = PlanReview.from_dict(legacy)
    assert pr.scope == ""
    assert pr.reviewed_meta_digest == ""
    assert pr.reviewed_stage_keys == {}


def test_from_dict_malformed_stage_keys_does_not_crash():
    malformed = {"plan_path": "/plan.toml", "verdict": "pass", "reviewer": "thinker",
                 "reviewed_stage_keys": "garbage"}
    pr = PlanReview.from_dict(malformed)
    assert pr.reviewed_stage_keys == {}


# --- 3. a stage-scoped pass clears the gate for its own moved stage only -----

def test_stage_scoped_pass_clears_gate_for_its_own_moved_stage(gate_on, tmp_path, fixtures_dir):
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    doc0 = load_plan(str(plan_path))
    whole = _whole_review(plan_path, doc0)

    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive_stage1_retitled.toml").read_text())
    doc1 = load_plan(str(plan_path))
    stage1 = _stage_review(plan_path, doc1, 1)

    state = _subst(plan_path=str(plan_path), plan_review=whole,
                   plan_stage_reviews={"stage:1": stage1})
    assert gates.plan_review_blockers(state, str(plan_path)) == []


def test_stage_scoped_pass_does_not_clear_a_different_moved_stage(gate_on, tmp_path, fixtures_dir):
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    doc0 = load_plan(str(plan_path))
    whole = _whole_review(plan_path, doc0)

    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive_stage1_retitled.toml").read_text())
    doc1 = load_plan(str(plan_path))
    stage2 = _stage_review(plan_path, doc1, 2)   # covers stage 2 -- not the stage that moved

    state = _subst(plan_path=str(plan_path), plan_review=whole,
                   plan_stage_reviews={"stage:2": stage2})
    blockers = gates.plan_review_blockers(state, str(plan_path))
    assert blockers and "stage 1" in blockers[0] and "--scope stage:1" in blockers[0]


# --- 4. an empty attestation on a stage-scoped pass binds nothing ------------

def test_stage_scoped_pass_without_attestation_blocks(gate_on, tmp_path, fixtures_dir):
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    doc0 = load_plan(str(plan_path))
    whole = _whole_review(plan_path, doc0)

    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive_stage1_retitled.toml").read_text())
    doc1 = load_plan(str(plan_path))
    stage1 = _stage_review(plan_path, doc1, 1, attest=False)

    state = _subst(plan_path=str(plan_path), plan_review=whole,
                   plan_stage_reviews={"stage:1": stage1})
    blockers = gates.plan_review_blockers(state, str(plan_path))
    assert blockers and "not attested" in blockers[0]


# --- 5. a meta/order edit demands a whole-plan review regardless of coverage -

def test_meta_edit_demands_whole_plan_review_even_with_current_stage_reviews(gate_on, tmp_path, fixtures_dir):
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    doc0 = load_plan(str(plan_path))
    whole = _whole_review(plan_path, doc0)
    stage_reviews = {f"stage:{i}": _stage_review(plan_path, doc0, i) for i in (1, 2, 3)}

    plan_path.write_text(plan_path.read_text().replace(
        'done_criterion = "all three stages PASSED and resolution confirmed"',
        'done_criterion = "all three stages PASSED"',
    ))

    state = _subst(plan_path=str(plan_path), plan_review=whole,
                   plan_stage_reviews=stage_reviews)
    blockers = gates.plan_review_blockers(state, str(plan_path))
    assert blockers and "meta/order changed" in blockers[0]


# --- 6. a stage-scoped override is attestation-free and needs a distinct reviewer

def test_stage_scoped_override_is_attestation_free_and_needs_distinct_reviewer(
        store, fixtures_dir, tmp_path, gate_on):
    sid = "sc-ov"
    plan = tmp_path / "plan.toml"
    plan.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    _to_plan_ready(store, sid, str(plan))
    cli.cmd_plan_review(ns(session=sid, target=None, scope=None, verdict="pass",
                           reviewer="thinker", concerns=None, note="",
                           plan_digest=_sha256_file(plan)), store=store)

    plan.write_text((fixtures_dir / "plan_two_stage_substantive_stage1_retitled.toml").read_text())
    cli.cmd_plan_review(ns(session=sid, target=None, scope="stage:1", verdict="revise",
                           reviewer="thinker", concerns=["title needs work"], note="",
                           plan_digest=None), store=store)

    d = cli.cmd_plan_review(ns(session=sid, target=None, scope="stage:1", verdict="override",
                               reviewer="thinker", concerns=None, note="user escape",
                               plan_digest=None), store=store)
    assert d.ok is False
    assert "distinct reviewer" in d.detail
    assert store.load(sid).plan_stage_reviews["stage:1"].verdict == "revise"  # untouched

    d = cli.cmd_plan_review(ns(session=sid, target=None, scope="stage:1", verdict="override",
                               reviewer="fedor", concerns=None, note="user escape",
                               plan_digest=None), store=store)
    assert d.ok is True
    state = store.load(sid)
    assert state.plan_stage_reviews["stage:1"].verdict == "override"
    assert state.plan_stage_reviews["stage:1"].plan_sha256 == ""  # attestation-free

    d = cli.cmd_approve(ns(session=sid, by="user"), store=store)
    assert d.node == Node.APPROVED.value


# --- 7. --scope validation ----------------------------------------------------

def test_scope_unrecognized_string_refused(store, fixtures_dir, gate_on):
    sid = "sc-bad1"
    _to_plan_ready(store, sid, str(fixtures_dir / "plan_two_stage_substantive.toml"))
    d = cli.cmd_plan_review(ns(session=sid, target=None, scope="bogus", verdict="pass",
                               reviewer="thinker", concerns=None, note="",
                               plan_digest=None), store=store)
    assert d.ok is False
    assert "not a recognized scope" in d.detail
    assert store.load(sid).plan_stage_reviews == {}


def test_scope_nonexistent_stage_refused(store, fixtures_dir, gate_on):
    sid = "sc-bad2"
    _to_plan_ready(store, sid, str(fixtures_dir / "plan_two_stage_substantive.toml"))
    d = cli.cmd_plan_review(ns(session=sid, target=None, scope="stage:99", verdict="pass",
                               reviewer="thinker", concerns=None, note="",
                               plan_digest=None), store=store)
    assert d.ok is False
    assert "no stage 99" in d.detail
    assert store.load(sid).plan_stage_reviews == {}


# --- 8. cmd_plan_review_delta's three outcomes --------------------------------

def test_delta_whole_plan_needed_when_nothing_reviewed(store, fixtures_dir, gate_on):
    sid = "delta1"
    _to_plan_ready(store, sid, str(fixtures_dir / "plan_two_stage_substantive.toml"))
    d = cli.cmd_plan_review_delta(ns(session=sid, plan=None), store=store)
    assert d.ok is True
    assert d.data["whole_plan"] is True
    assert d.data["stages"] == []


def test_delta_specific_stages_needed(store, fixtures_dir, tmp_path, gate_on):
    sid = "delta2"
    plan = tmp_path / "plan.toml"
    plan.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    _to_plan_ready(store, sid, str(plan))
    cli.cmd_plan_review(ns(session=sid, target=None, scope=None, verdict="pass",
                           reviewer="thinker", concerns=None, note="",
                           plan_digest=_sha256_file(plan)), store=store)

    plan.write_text((fixtures_dir / "plan_two_stage_substantive_stage1_retitled.toml").read_text())
    d = cli.cmd_plan_review_delta(ns(session=sid, plan=str(plan)), store=store)
    assert d.ok is True
    assert d.data["whole_plan"] is False
    assert d.data["stages"] == [1]


def test_delta_no_gap_when_fully_covered(store, fixtures_dir, tmp_path, gate_on):
    sid = "delta3"
    plan = tmp_path / "plan.toml"
    plan.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    _to_plan_ready(store, sid, str(plan))
    cli.cmd_plan_review(ns(session=sid, target=None, scope=None, verdict="pass",
                           reviewer="thinker", concerns=None, note="",
                           plan_digest=_sha256_file(plan)), store=store)
    d = cli.cmd_plan_review_delta(ns(session=sid, plan=str(plan)), store=store)
    assert d.ok is True
    assert d.data["whole_plan"] is False
    assert d.data["stages"] == []
    assert "no review gap" in d.detail


# --- 9. present-plan --kind essence under a stage-scoped review ---------------

def _write_rendering(tmp_path, text, name="rendering.txt") -> str:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_essence_allowed_when_stage_scoped_review_covers_the_moved_stage(
        store, fixtures_dir, tmp_path, gate_on):
    sid = "es-ok"
    plan = tmp_path / "plan.toml"
    plan.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    _to_plan_ready(store, sid, str(plan))
    cli.cmd_plan_review(ns(session=sid, target=None, scope=None, verdict="pass",
                           reviewer="thinker", concerns=None, note="",
                           plan_digest=_sha256_file(plan)), store=store)

    plan.write_text((fixtures_dir / "plan_two_stage_substantive_stage1_retitled.toml").read_text())
    cli.cmd_submit_plan(ns(session=sid, plan=str(plan)), store=store)
    cli.cmd_plan_review(ns(session=sid, target=None, scope="stage:1", verdict="pass",
                           reviewer="thinker", concerns=None, note="",
                           plan_digest=_sha256_file(plan)), store=store)

    rendering = _write_rendering(tmp_path, "Summary of the plan.")
    d = cli.cmd_present_plan(
        ns(session=sid, kind="essence", rendering_file=rendering, emit_skeleton=False),
        store=store,
    )
    assert d.ok is True
    assert [p.kind for p in store.load(sid).plan_presentations] == ["essence"]


def test_essence_blocked_when_moved_stage_lacks_its_own_review(
        store, fixtures_dir, tmp_path, gate_on):
    """A stage-scoped review for an untouched stage (2) puts the session into
    coverage mode; stage 1 then moves with no review of its own, so essence must
    be blocked with the granular per-stage message, not the legacy byte-hash one
    (which only fires while plan_stage_reviews is still empty)."""
    sid = "es-block"
    plan = tmp_path / "plan.toml"
    plan.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    _to_plan_ready(store, sid, str(plan))
    cli.cmd_plan_review(ns(session=sid, target=None, scope=None, verdict="pass",
                           reviewer="thinker", concerns=None, note="",
                           plan_digest=_sha256_file(plan)), store=store)
    cli.cmd_plan_review(ns(session=sid, target=None, scope="stage:2", verdict="pass",
                           reviewer="thinker", concerns=None, note="",
                           plan_digest=_sha256_file(plan)), store=store)

    plan.write_text((fixtures_dir / "plan_two_stage_substantive_stage1_retitled.toml").read_text())

    rendering = _write_rendering(tmp_path, "Summary of the plan.")
    d = cli.cmd_present_plan(
        ns(session=sid, kind="essence", rendering_file=rendering, emit_skeleton=False),
        store=store,
    )
    assert d.ok is False
    assert d.data["blockers"] == gates.plan_review_blockers(store.load(sid), str(plan))
    assert "stage 1" in d.data["blockers"][0] and "--scope stage:1" in d.data["blockers"][0]
    assert store.load(sid).plan_presentations == []


# --- 10. the fail-open/fail-closed postures survive coverage mode unweakened --

def test_unreadable_target_fails_open_even_with_stage_reviews_recorded(gate_on, tmp_path):
    """test_plan_review_gate.py locks the fail-open posture for the legacy,
    stage-reviews-empty path (load_plan itself never runs). This is the same
    posture with plan_stage_reviews non-empty: load_plan(target_plan) raises,
    plan_review_blockers falls back to _plan_review_blockers_whole exactly as
    the legacy path would — a stage-scoped record present does not turn a
    transient read error into a block."""
    missing = tmp_path / "gone.toml"
    whole = PlanReview(str(missing), "pass", "thinker", plan_sha256="deadbeef")
    stage1 = PlanReview(str(missing), "pass", "thinker", scope="stage:1",
                        plan_sha256="deadbeef")
    s = _subst(plan_path=str(missing), plan_review=whole,
               plan_stage_reviews={"stage:1": stage1})
    assert gates.plan_review_blockers(s, str(missing)) == []


def test_stage_scoped_attestation_cannot_substitute_for_the_whole_plans_own(gate_on, tmp_path, fixtures_dir):
    """The whole-plan review's own unattested-pass block is fail-CLOSED and is
    checked before any stage is even considered — a fully-attested stage-scoped
    pass cannot paper over an unattested whole-plan review."""
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    doc0 = load_plan(str(plan_path))
    whole = _whole_review(plan_path, doc0, attest=False)   # unattested pass

    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive_stage1_retitled.toml").read_text())
    doc1 = load_plan(str(plan_path))
    stage1 = _stage_review(plan_path, doc1, 1)              # fully attested

    state = _subst(plan_path=str(plan_path), plan_review=whole,
                   plan_stage_reviews={"stage:1": stage1})
    blockers = gates.plan_review_blockers(state, str(plan_path))
    assert blockers and "not attested" in blockers[0]
