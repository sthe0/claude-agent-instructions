"""The thinker-review gate: approve/replan are blocked on a substantive session
until a thinker review bound to the exact plan version is recorded (pass or a
user override). Covers block/pass/stale/override/scope-off/backward-compat
in-process, plus four live spine walks driving the real CLI as a subprocess.

The suite defaults the gate OFF (conftest `_plan_review_gate_off_by_default`);
every test here re-enables it explicitly (setenv "1" in-process, or the subprocess
env), so these are the definitive regression lock for the gate's wiring."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, gates
from agentctl.state import Node, PlanReview, SessionState, StageStatus

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def ns(**kw):
    return Namespace(**kw)


@pytest.fixture
def gate_on(monkeypatch):
    """Re-arm the gate (overriding the suite-wide force-off) for a substantive
    session, independent of config advisor-mode."""
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")


def _subst(**kw) -> SessionState:
    return SessionState(session_id="s", task_id="t", weight_class="SUBSTANTIVE",
                        plan_path="/plan.toml", plan_verified=True, **kw)


# --- unit-level gate semantics (a,c,d,e,f) ----------------------------------

def test_a_missing_review_blocks(gate_on):
    s = _subst()
    blockers = gates.plan_review_blockers(s, s.plan_path)
    assert blockers and "no thinker review" in blockers[0]


def test_b_bound_pass_clears(gate_on):
    s = _subst(plan_review=PlanReview("/plan.toml", "pass", "thinker"))
    assert gates.plan_review_blockers(s, "/plan.toml") == []


def test_c_stale_review_blocks(gate_on):
    s = _subst(plan_review=PlanReview("/OLD.toml", "pass", "thinker"))
    blockers = gates.plan_review_blockers(s, "/plan.toml")
    assert blockers and "stale" in blockers[0]


def test_d_revise_blocks(gate_on):
    s = _subst(plan_review=PlanReview("/plan.toml", "revise", "thinker"))
    blockers = gates.plan_review_blockers(s, "/plan.toml")
    assert blockers and "revise" in blockers[0]


def test_e_override_with_reviewer_and_note_passes(gate_on):
    s = _subst(plan_review=PlanReview("/plan.toml", "override", "fedor", note="deadlock"))
    assert gates.plan_review_blockers(s, "/plan.toml") == []


def test_f_override_missing_reviewer_or_note_blocks(gate_on):
    s1 = _subst(plan_review=PlanReview("/plan.toml", "override", "", note="x"))
    s2 = _subst(plan_review=PlanReview("/plan.toml", "override", "fedor", note=""))
    assert "requires a non-empty reviewer" in gates.plan_review_blockers(s1, "/plan.toml")[0]
    assert "requires a non-empty note" in gates.plan_review_blockers(s2, "/plan.toml")[0]


# --- scope-off (g,h) ---------------------------------------------------------

def test_g_small_change_gate_vacuous(gate_on):
    """With the gate's own env unset, activation is weight-scoped: a SMALL_CHANGE
    session never activates it."""
    os.environ.pop("AGENTCTL_PLAN_REVIEW", None)
    s = SessionState(session_id="s", task_id="t", weight_class="SMALL_CHANGE",
                     plan_path="/plan.toml")
    assert gates.plan_review_active(s) is False
    assert gates.plan_review_blockers(s, "/plan.toml") == []


def test_h_force_off_env_makes_gate_vacuous(monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "0")
    s = _subst()  # no review recorded
    assert gates.plan_review_active(s) is False
    assert gates.plan_review_blockers(s, s.plan_path) == []


def test_i_advisor_kill_switch_does_not_disable_gate(monkeypatch):
    """AGENTCTL_ADVISOR=0 (the advisory judge's cost knob) must not silently
    defeat the mandatory review gate: with the gate's own env unset, a
    SUBSTANTIVE session keeps the gate active regardless of advisor state."""
    monkeypatch.delenv("AGENTCTL_PLAN_REVIEW", raising=False)
    monkeypatch.setenv("AGENTCTL_ADVISOR", "0")
    s = _subst()  # no review recorded
    assert gates.plan_review_active(s) is True
    assert gates.plan_review_blockers(s, s.plan_path)  # blocks: no review yet


# --- in-process approve / replan wiring (b-integration, i2) ------------------

def _to_plan_ready(store, sid, plan):
    cli.cmd_start(ns(session=sid, task="demo", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)


def test_approve_blocked_then_passes_after_review(store, fixtures_dir, gate_on):
    sid = "apr"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(store, sid, plan)
    d = cli.cmd_approve(ns(session=sid, by="user"), store=store)
    assert d.node == Node.PLAN_READY.value  # blocked
    cli.cmd_plan_review(ns(session=sid, verdict="pass", reviewer="thinker",
                           concerns=None, note="", target=None), store=store)
    d = cli.cmd_approve(ns(session=sid, by="user"), store=store)
    assert d.node == Node.APPROVED.value


def test_review_recorded_binds_to_current_plan_path(store, fixtures_dir, gate_on):
    sid = "bind"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(store, sid, plan)
    cli.cmd_plan_review(ns(session=sid, verdict="pass", reviewer="thinker",
                           concerns=None, note="", target=None), store=store)
    assert store.load(sid).plan_review.plan_path == plan


def test_override_by_same_reviewer_refused(store, fixtures_dir, gate_on):
    """The reviewer whose `revise` blocks the plan cannot override themselves —
    override is the USER's escape hatch. The refusal happens before the record
    is overwritten, so the blocking `revise` (and its author) survive."""
    sid = "selfov"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(store, sid, plan)
    cli.cmd_plan_review(ns(session=sid, verdict="revise", reviewer="thinker",
                           concerns=None, note="", target=None), store=store)
    d = cli.cmd_plan_review(ns(session=sid, verdict="override", reviewer="thinker",
                               concerns=None, note="self-stamp", target=None), store=store)
    assert d.ok is False and "distinct reviewer" in d.detail
    assert store.load(sid).plan_review.verdict == "revise"  # record preserved
    d = cli.cmd_approve(ns(session=sid, by="user"), store=store)
    assert d.node == Node.PLAN_READY.value  # still blocked
    cli.cmd_plan_review(ns(session=sid, verdict="override", reviewer="fedor",
                           concerns=None, note="user escape", target=None), store=store)
    d = cli.cmd_approve(ns(session=sid, by="user"), store=store)
    assert d.node == Node.APPROVED.value


# --- backward-compat / round-trip (j,k) --------------------------------------

def test_j_round_trips_plan_review():
    s = _subst(plan_review=PlanReview("/plan.toml", "pass", "thinker",
                                      concerns=["c1"], note="n"))
    assert SessionState.from_dict(json.loads(json.dumps(s.to_dict()))) == s


def test_k_legacy_no_plan_review_key_loads():
    """A serialized state predating the plan_review field (key absent) loads with
    plan_review defaulted to None — the additive-optional migration contract."""
    raw = _subst().to_dict()
    raw.pop("plan_review", None)
    s = SessionState.from_dict(raw)
    assert s.plan_review is None


def test_legacy_schema11_plan_review_fixture_loads():
    """A schema-11-tagged state carrying a plan_review (as written before schema-12
    first-classed the field) must load unchanged under schema-12 code."""
    raw = json.loads((FIXTURES / "legacy_schema11_plan_review.json").read_text())
    assert raw["schema_version"] == 11
    s = SessionState.from_dict(raw)
    assert s.plan_review is not None
    assert s.plan_review.verdict == "pass"
    assert s.plan_review.plan_path == raw["plan_review"]["plan_path"]


# --- content-hash staleness (issue #16) --------------------------------------
# Path binding alone lets an in-place rewrite of the same-path plan inherit a PASS
# granted to different bytes. The review now also records the reviewed plan's
# sha256; the gate recomputes it and blocks a content drift (fail-open on I/O).

def _sha256_file(p) -> str:
    import hashlib
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _subst_planned(plan, review) -> SessionState:
    """A substantive session whose plan_path is a real on-disk file (needed for the
    content-hash gate, which reads the target's bytes)."""
    return SessionState(session_id="s", task_id="t", weight_class="SUBSTANTIVE",
                        plan_path=str(plan), plan_verified=True, plan_review=review)


def test_content_hash_mismatch_blocks_as_stale(gate_on, tmp_path):
    """A passing review bound to a plan file's bytes goes STALE when the SAME path is
    rewritten in place — path binding alone would keep the stale PASS valid."""
    plan = tmp_path / "plan.toml"
    plan.write_text("index = 1\n")
    s = _subst_planned(plan, PlanReview(str(plan), "pass", "thinker",
                                        plan_sha256=_sha256_file(plan)))
    assert gates.plan_review_blockers(s, str(plan)) == []   # unchanged: clears
    plan.write_text("index = 2\n")                          # in-place rewrite
    blockers = gates.plan_review_blockers(s, str(plan))
    assert blockers and "changed since it was reviewed" in blockers[0]


def test_content_hash_unreadable_target_fails_open(gate_on, tmp_path):
    """If the target file cannot be read at gate time, the hash check degrades to the
    prior path-only binding (a transient read error never wedges the gate)."""
    missing = tmp_path / "gone.toml"
    s = _subst_planned(missing, PlanReview(str(missing), "pass", "thinker",
                                           plan_sha256="deadbeef"))
    assert gates.plan_review_blockers(s, str(missing)) == []  # unreadable -> path-only


def test_content_hash_empty_stored_hash_is_path_only(gate_on, tmp_path):
    """A PlanReview with an empty plan_sha256 (a legacy record) keeps clearing the
    gate on path binding alone, even when the file exists and could be hashed."""
    plan = tmp_path / "plan.toml"
    plan.write_text("x")
    s = _subst_planned(plan, PlanReview(str(plan), "pass", "thinker", plan_sha256=""))
    assert gates.plan_review_blockers(s, str(plan)) == []


def test_cmd_plan_review_records_hash_and_inplace_rewrite_blocks_approve(
        store, fixtures_dir, tmp_path, gate_on):
    """End-to-end: cmd_plan_review stamps the reviewed plan's sha256; an in-place
    rewrite of the same-path plan then blocks approve as content-stale."""
    sid = "hash"
    plan = tmp_path / "plan.toml"
    plan.write_text((fixtures_dir / "plan_two_stage.toml").read_text())
    _to_plan_ready(store, sid, str(plan))
    cli.cmd_plan_review(ns(session=sid, verdict="pass", reviewer="thinker",
                           concerns=None, note="", target=None), store=store)
    assert store.load(sid).plan_review.plan_sha256 == _sha256_file(plan)
    # in-place rewrite of the SAME path -> the recorded review is now content-stale
    plan.write_text(plan.read_text() + "\n# tweak\n")
    d = cli.cmd_approve(ns(session=sid, by="user"), store=store)
    assert d.node == Node.PLAN_READY.value  # blocked, not APPROVED
    assert any("changed since it was reviewed" in b for b in d.data["blockers"])


def test_j2_round_trips_plan_review_with_hash():
    """The new plan_sha256 field survives a to_dict/from_dict round-trip."""
    s = _subst(plan_review=PlanReview("/plan.toml", "pass", "thinker",
                                      concerns=["c1"], note="n", plan_sha256="ab12"))
    assert SessionState.from_dict(json.loads(json.dumps(s.to_dict()))) == s


def test_legacy_schema12_plan_review_fixture_loads(gate_on):
    """A schema-12-tagged state whose plan_review predates plan_sha256 loads under
    schema-13 code with plan_sha256='' and its gate degrades to path-only binding."""
    raw = json.loads((FIXTURES / "legacy_schema12_plan_review.json").read_text())
    assert raw["schema_version"] == 12
    assert "plan_sha256" not in raw["plan_review"]
    s = SessionState.from_dict(raw)
    assert s.plan_review is not None
    assert s.plan_review.plan_sha256 == ""
    # empty stored hash -> path-only binding, so a matching target still clears
    assert gates.plan_review_blockers(s, s.plan_review.plan_path) == []


# --- four live spine walks (subprocess, gate ON) -----------------------------

def _run(state_root: Path, *args: str, gate: str = "1"):
    env = dict(os.environ, AGENTCTL_PLAN_REVIEW=gate, AGENTCTL_ADVISOR="0")
    proc = subprocess.run(
        [sys.executable, "-m", "agentctl", "--state-root", str(state_root), *args],
        cwd=str(SCRIPTS_DIR), env=env, capture_output=True, text=True,
    )
    return proc


def _classify_plan_submit(root, sid, plan, *, small=False):
    _run(root, "start", "--session", sid, "--task", "t", "--goal", "g",
         "--done-criterion", "dc", "--criterion-type", "measurable")
    if small:
        _run(root, "classify", "--session", sid, "--changed-lines", "5", "--files", "1")
    else:
        _run(root, "classify", "--session", sid, "--changed-lines", "200",
             "--files", "5", "--architectural")
    _run(root, "plan", "--session", sid)
    _run(root, "submit-plan", "--session", sid, "--plan", plan)


def test_spine_walk_construction_deny_then_pass(tmp_path):
    root = tmp_path / "st"
    plan = str(FIXTURES / "plan_two_stage.toml")
    _classify_plan_submit(root, "c1", plan)
    denied = _run(root, "approve", "--session", "c1", "--by", "user")
    assert '"ok": false' in denied.stdout and "plan-review" in denied.stdout.lower()
    _run(root, "plan-review", "--session", "c1", "--verdict", "pass", "--reviewer", "thinker")
    ok = _run(root, "approve", "--session", "c1", "--by", "user")
    assert '"APPROVED"' in ok.stdout


def test_spine_walk_replan_deny_then_pass(tmp_path):
    root = tmp_path / "st"
    plan = str(FIXTURES / "plan_two_stage.toml")
    refined = str(FIXTURES / "plan_two_stage_refined.toml")
    _classify_plan_submit(root, "r1", plan)
    _run(root, "plan-review", "--session", "r1", "--verdict", "pass", "--reviewer", "thinker")
    _run(root, "approve", "--session", "r1", "--by", "user")
    _run(root, "partition", "--session", "r1")
    _run(root, "next-stage", "--session", "r1")
    _run(root, "record-result", "--session", "r1", "--status", "failed", "--actual", "boom")
    _run(root, "declare", "--session", "r1", "--expected", "e", "--actual", "a", "--mismatch", "m")
    _run(root, "investigate", "--session", "r1", "--localized-expectation", "le",
         "--localized-actual", "la", "--hypothesis", "h1", "--hypothesis", "h2")
    _run(root, "critique", "--session", "r1", "--functional-ground", "fg",
         "--replanning-task", "rt")
    denied = _run(root, "replan", "--session", "r1", "--plan", refined)
    assert '"ok": false' in denied.stdout and "plan-review" in denied.stdout.lower()
    _run(root, "plan-review", "--session", "r1", "--verdict", "pass",
         "--reviewer", "thinker", "--target", refined)
    ok = _run(root, "replan", "--session", "r1", "--plan", refined)
    assert '"ok": true' in ok.stdout


def test_spine_walk_override_unblocks_revise(tmp_path):
    root = tmp_path / "st"
    plan = str(FIXTURES / "plan_two_stage.toml")
    _classify_plan_submit(root, "o1", plan)
    _run(root, "plan-review", "--session", "o1", "--verdict", "revise", "--reviewer", "thinker")
    denied = _run(root, "approve", "--session", "o1", "--by", "user")
    assert '"ok": false' in denied.stdout
    _run(root, "plan-review", "--session", "o1", "--verdict", "override",
         "--reviewer", "fedor", "--note", "thinker stuck; authored escape")
    ok = _run(root, "approve", "--session", "o1", "--by", "user")
    assert '"APPROVED"' in ok.stdout
    # the override is written to the gate log
    log = (root / "gate-log.jsonl")
    if log.exists():
        assert any(r.get("gate") == "plan_review"
                   for r in (json.loads(x) for x in log.read_text().splitlines() if x))


def test_spine_walk_scope_off_small_change_unaffected(tmp_path):
    root = tmp_path / "st"
    plan = str(FIXTURES / "plan_two_stage.toml")
    # gate env unset entirely: a small-change session must never activate it.
    env = dict(os.environ, AGENTCTL_ADVISOR="0")
    env.pop("AGENTCTL_PLAN_REVIEW", None)

    def run(*args):
        return subprocess.run([sys.executable, "-m", "agentctl", "--state-root",
                               str(root), *args], cwd=str(SCRIPTS_DIR), env=env,
                              capture_output=True, text=True)

    run("start", "--session", "s1", "--task", "t", "--goal", "g",
        "--done-criterion", "dc", "--criterion-type", "measurable")
    c = run("classify", "--session", "s1", "--changed-lines", "5", "--files", "1")
    assert "small change" in c.stdout and "execute_in_thread" in c.stdout
