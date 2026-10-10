"""Customer questions ride on every review act (norm-staleness stage 5, R1 as amended by
E3, and R7).

A review act -- a unit, a pair, a stage, or the whole plan -- returns plan remarks
(verdict, C1-C4 concerns) AND the questions only the customer can decide. The engine
records each question as a premise candidate `qrev-<review id>-<N>` addressed to the
review id; the standalone question enumerator, its detached worker, its runner-health
gate and its escape are retired for new plans while a legacy `qenum-` bag still loads
and validates.

New symbols are imported inside the tests so this file stays collectable (and fails by
assertion, not by collection error) on a tree that predates them.
"""
from __future__ import annotations

import hashlib
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli
from agentctl.plan import load_plan

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skills" / "specializations" / "thinker" / "SKILL.md"
SUBSTANTIVE = "plan_two_stage_substantive.toml"
SID = "rq"
PAIR = "2-1"
UNIT = "unit:base"


def ns(**kw):
    return Namespace(**kw)


def _sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def _premise_armed(monkeypatch, tmp_path):
    """Override conftest's suite-wide AGENTCTL_PREMISE=0 force-off so the premise
    plugin arms on the SUBSTANTIVE classification, as in production. The thinker-review
    gate stays off (conftest) so approve is refused or allowed by the premise gate
    alone."""
    monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
    monkeypatch.setenv(cli.ESCALATION_LEDGER_ENV, str(tmp_path / "escalations.jsonl"))


def _build(store, sid, plan: Path):
    cli.cmd_start(ns(session=sid, task="demo", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=str(plan)), store=store)
    assert "premise" in store.load(sid).plugins


def _cover_the_order(store, sid, stage=1):
    cli.cmd_order_raise(ns(session=sid, id="O1", element="the order this plan answers"),
                        store=store)
    cli.cmd_order_dispose(ns(session=sid, id="O1", as_="covered", stage=stage, reason=""),
                          store=store)


@pytest.fixture
def plan(fixtures_dir, tmp_path) -> Path:
    path = tmp_path / "plan.toml"
    path.write_text((fixtures_dir / SUBSTANTIVE).read_text())
    return path


@pytest.fixture
def session(store, plan):
    _build(store, SID, plan)
    _cover_the_order(store, SID)
    return plan


def _review(store, plan, scope, questions=None, *, verdict="pass", concerns=None):
    return cli.cmd_plan_review(
        ns(session=SID, target=None, scope=scope, verdict=verdict, reviewer="thinker",
           concerns=concerns, concern_ids=None, note="", regression_command=None,
           plan_digest=_sha(plan), customer_questions=questions), store=store)


def _bag(store) -> dict:
    return store.load(SID).plugins["premise"]


def _qrev(store) -> dict:
    return {c["id"]: c for c in _bag(store).get("candidates", [])
            if c["id"].startswith("qrev-")}


def _approve(store):
    return cli.cmd_approve(ns(session=SID, by="user"), store=store)


def _blockers(directive) -> list:
    return (directive.data or {}).get("blockers") or []


def _dismiss(store, cid, reason="the customer already answered this"):
    d = cli.cmd_question_candidate_dispose(
        ns(session=SID, id=cid, as_="dismissed", reason=reason, question=""), store=store)
    assert d.ok, d.detail
    return d


# --- (a) the protocol text -------------------------------------------------------

def test_engine_rendered_unit_bundle_names_the_customer_questions_field(plan):
    from agentctl.render import render_unit_review_bundle
    text = render_unit_review_bundle(load_plan(str(plan)), UNIT, plan_sha256=_sha(plan))
    assert "Customer questions:" in text
    assert "Q: <question>" in text


def test_engine_rendered_pair_bundle_names_the_customer_questions_field(plan):
    from agentctl.render import render_pair_review_bundle
    text = render_pair_review_bundle(
        load_plan(str(plan)), PAIR, plan_sha256=_sha(plan), view_dir="/view")
    assert "Customer questions:" in text
    assert "Q: <question>" in text


def test_every_review_reply_line_of_the_thinker_skill_carries_the_field():
    lines = [ln for ln in SKILL.read_text().splitlines() if "REVIEW:" in ln]
    assert len(lines) >= 2, lines
    missing = [ln for ln in lines if "Customer questions:" not in ln]
    assert not missing, missing


def test_marker_constants_sit_beside_the_review_marker():
    from agentctl import plan as plan_mod
    assert plan_mod.CUSTOMER_QUESTIONS_MARKER == "Customer questions:"
    assert plan_mod.CUSTOMER_QUESTION_MARKER == "Q:"
    assert plan_mod.REVIEW_MARKER == "REVIEW:"


# --- (a) parsing the reply -------------------------------------------------------

def _reply(*lines) -> str:
    return "\n".join(["REVIEW:", "Verdict: pass", f"Plan digest: {'0' * 64}", *lines])


def test_review_block_parser_returns_the_questions_after_the_concerns():
    from lib.review_block import find_terminal_review_block
    block = find_terminal_review_block(_reply(
        "note: C2: wording could be tighter",
        "Customer questions:",
        "Q: Which region ships first?",
        "Q: Is the legacy API in scope?"))
    assert block is not None
    assert list(block.questions) == ["Which region ships first?", "Is the legacy API in scope?"]


def test_review_block_without_the_field_parses_as_no_questions():
    from lib.review_block import find_terminal_review_block
    block = find_terminal_review_block(_reply("note: C2: wording could be tighter"))
    assert block is not None
    assert list(block.questions) == []


def test_review_block_with_none_parses_as_no_questions():
    from lib.review_block import find_terminal_review_block
    block = find_terminal_review_block(_reply("Customer questions: none"))
    assert block is not None
    assert list(block.questions) == []


# --- (b) recording: one candidate per question, addressed to the review id ---------

@pytest.mark.parametrize("scope,review_id", [
    (f"topo:{UNIT}", UNIT),
    (f"topo:{PAIR}", PAIR),
    ("stage:1", "stage:1"),
    ("", "whole"),
])
def test_a_review_act_records_its_question_as_a_raised_candidate(
        store, session, scope, review_id):
    d = _review(store, session, scope, ["Which region ships first?"])
    assert d.ok, d.detail
    cand = _qrev(store).get(f"qrev-{review_id}-1")
    assert cand is not None, sorted(_qrev(store))
    assert cand["statement"] == "Which region ships first?"
    assert cand["disposition"] == "raised"
    assert cand["target"] == review_id
    assert d.data["customer_question_ids"] == [f"qrev-{review_id}-1"]


def test_the_whole_plan_review_is_named_qrev_whole(store, session):
    _review(store, session, "", ["a?", "b?"])
    assert sorted(_qrev(store)) == ["qrev-whole-1", "qrev-whole-2"]


def test_approve_is_refused_until_the_review_question_is_dispositioned(store, session):
    assert _review(store, session, f"topo:{PAIR}", ["Which region ships first?"]).ok
    refused = _approve(store)
    assert refused.ok is False
    assert any("qrev-2-1-1" in b for b in _blockers(refused)), _blockers(refused)

    _dismiss(store, "qrev-2-1-1")
    allowed = _approve(store)
    assert allowed.ok is True, _blockers(allowed)


def test_re_recording_a_question_keeps_its_disposition_and_a_new_one_takes_the_next_number(
        store, session):
    _review(store, session, f"topo:{PAIR}", ["Which region ships first?"])
    _dismiss(store, "qrev-2-1-1")

    again = _review(store, session, f"topo:{PAIR}",
                    ["Which region ships first?", "Is the legacy API in scope?"])
    assert again.ok, again.detail
    rows = _qrev(store)
    assert rows["qrev-2-1-1"]["disposition"] == "dismissed"
    assert rows["qrev-2-1-1"]["reason"] == "the customer already answered this"
    assert rows["qrev-2-1-2"]["statement"] == "Is the legacy API in scope?"
    assert rows["qrev-2-1-2"]["disposition"] == "raised"
    assert again.data["customer_question_ids"] == ["qrev-2-1-1", "qrev-2-1-2"]


def test_questions_of_another_review_id_are_untouched(store, session):
    _review(store, session, f"topo:{PAIR}", ["pair question?"])
    _dismiss(store, "qrev-2-1-1")
    _review(store, session, "stage:1", ["stage question?"])
    rows = _qrev(store)
    assert rows["qrev-2-1-1"]["disposition"] == "dismissed"
    assert rows["qrev-stage:1-1"]["disposition"] == "raised"


def test_a_block_without_the_field_records_no_candidate(store, session):
    d = _review(store, session, f"topo:{PAIR}", None)
    assert d.ok, d.detail
    assert _qrev(store) == {}
    assert not d.data.get("customer_question_ids")


def test_a_plan_remark_raises_no_candidate(store, session):
    d = _review(store, session, f"topo:{PAIR}", None,
                concerns=["note: C2: wording could be tighter"])
    assert d.ok, d.detail
    assert _qrev(store) == {}
    assert _bag(store)["candidates"] == []


# --- (b) the topological driver passes each parsed question through plan-review ---

@pytest.fixture
def rig(tmp_path, monkeypatch, capsys):
    from test_plan_review_topological import FakeEngine, Rig, _load
    drv = _load("plan_review_topological_review_questions", "plan-review-topological.py")
    monkeypatch.setattr(drv, "COST_LOG", tmp_path / "costs.jsonl")
    monkeypatch.setenv("AGENTCTL_TOPO_UNITS_DIR", str(tmp_path / "topo"))
    plan_path = tmp_path / "driver-plan.toml"
    plan_path.write_text("plan bytes\n", encoding="utf-8")
    return Rig(drv, tmp_path, monkeypatch, capsys, FakeEngine([["3-1"]]), plan_path)


def _drive(rig, *reply_lines):
    from test_plan_review_topological import canonical_stdout, review_original
    rig.specs = {"3-1": {"stdout": canonical_stdout(
        review_original("pass", rig.sha, reply_lines))}}
    rc, out = rig.run()
    recorded = rig.engine.verbs("plan-review")
    return rc, out, (recorded[0] if recorded else None)


def test_driver_passes_each_parsed_question_to_plan_review(rig):
    from test_plan_review_topological import flags
    rc, out, recorded = _drive(
        rig, "Customer questions:", "Q: Which region ships first?", "Q: Is the legacy API in scope?")
    assert recorded is not None, out
    assert flags(recorded, "--customer-question") == [
        "Which region ships first?", "Is the legacy API in scope?"]
    assert flags(recorded, "--verdict") == ["pass"]


def test_driver_passes_no_question_flag_for_none(rig):
    from test_plan_review_topological import flags
    rc, out, recorded = _drive(rig, "Customer questions: none")
    assert recorded is not None, out
    assert flags(recorded, "--customer-question") == []


# --- (c) the standalone enumeration is retired for new plans ----------------------

def _enumerator_spawns(monkeypatch) -> list:
    spawned: list = []
    real = subprocess.Popen

    class Recorder(real):
        def __init__(self, argv, *a, **kw):
            if "question-enumerate" in " ".join(map(str, argv)):
                spawned.append(list(argv))
            super().__init__(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", Recorder)
    return spawned


def test_submit_plan_launches_no_enumerator_and_writes_no_deadline(
        store, plan, monkeypatch):
    spawned = _enumerator_spawns(monkeypatch)
    _build(store, SID, plan)
    bag = _bag(store)
    assert spawned == []
    assert not bag.get("enumerate_deadline")
    assert bag["enumerated"] is False


def test_replan_launches_no_enumerator_and_writes_no_deadline(
        store, fixtures_dir, monkeypatch):
    _build(store, SID, fixtures_dir / "plan_two_stage.toml")
    _cover_the_order(store, SID)
    assert _approve(store).ok is True
    cli.cmd_partition(ns(session=SID, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=SID), store=store)
    spawned = _enumerator_spawns(monkeypatch)

    d = cli.cmd_replan(
        ns(session=SID, plan=str(fixtures_dir / SUBSTANTIVE)), store=store)
    assert d.ok is True, d.detail
    assert spawned == []
    assert not _bag(store).get("enumerate_deadline")


def test_approve_is_not_blocked_by_an_enumeration_that_never_ran(store, session):
    assert _bag(store)["enumerated"] is False
    d = _approve(store)
    assert d.ok is True, _blockers(d)
    assert not any("enumerat" in b for b in _blockers(d))


@pytest.mark.parametrize("command,extra", [
    ("question-enumerate", {"plan": None}),
    ("question-enumerate-escape", {"plan": None, "reason": "runner-unavailable", "note": "n"}),
])
def test_the_retired_verbs_exit_2_and_leave_state_unchanged(store, session, command, extra):
    from types import SimpleNamespace
    handler = {"question-enumerate": "cmd_question_enumerate",
               "question-enumerate-escape": "cmd_question_enumerate_escape"}[command]
    calls: list = []

    def runner(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="plan.goal\tq?", stderr="")

    before = store.load(SID)
    with pytest.raises(SystemExit) as exc:
        getattr(cli, handler)(
            ns(session=SID, command=command, **extra), store=store, runner=runner)
    assert exc.value.code == 2
    assert calls == []
    after = store.load(SID)
    assert after.plugins == before.plugins
    assert after.history == before.history


# --- (d) a legacy bag still loads, validates and gates ----------------------------

def _legacy_candidate(**over) -> dict:
    return {"id": "qenum-1", "statement": "is the goal actually agreed?",
            "disposition": "raised", "reason": "", "question": "", "target": "plan.goal",
            **over}


def test_a_legacy_raised_enumeration_candidate_blocks_approve_until_dispositioned(
        store, session):
    state = store.load(SID)
    state.plugins["premise"]["candidates"].append(_legacy_candidate())
    state.plugins["premise"]["enumerated"] = True
    store.save(state)

    refused = _approve(store)
    assert refused.ok is False
    assert any("qenum-1" in b for b in _blockers(refused)), _blockers(refused)

    _dismiss(store, "qenum-1", reason="agreed")
    assert _approve(store).ok is True


def test_a_legacy_failed_enumeration_run_no_longer_blocks_approve(store, session):
    state = store.load(SID)
    bag = state.plugins["premise"]
    bag.update({"enumerated": True, "enumerated_runner_ok": False,
                "enumerated_runner_stderr": "advisor timed out after 480s",
                "enumerate_deadline": "2000-01-01T00:00:00+00:00"})
    store.save(state)

    d = _approve(store)
    assert d.ok is True, _blockers(d)
    assert not any("enumerat" in b for b in _blockers(d))
    kept = _bag(store)
    assert kept["enumerated_runner_ok"] is False
    assert kept["enumerated_runner_stderr"] == "advisor timed out after 480s"
