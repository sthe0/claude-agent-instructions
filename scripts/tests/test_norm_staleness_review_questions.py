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


RETIRED_KEYS = {
    "enumerated": True,
    "enumerated_runner_ok": False,
    "enumerated_runner_stderr": "advisor timed out after 480s",
    "enumerated_stage_at": {"1": "2000-01-01T00:00:00+00:00"},
    "enumerated_stage_elements": {"1": ["plan.goal"]},
    "enumerated_meta_at": "2000-01-01T00:00:00+00:00",
    "enumerate_launch": 3,
    "enumerate_launch_digest": "0" * 64,
    "enumerate_deadline": "2000-01-01T00:00:00+00:00",
    "escapes": [{"reason": "runner-unavailable", "content_digest": "0" * 64, "note": "n"}],
}


def test_a_bag_carrying_every_retired_key_loads_and_only_raised_candidates_gate_approve(
        store, session):
    """One bag with every key the retired enumeration wrote — a failed run, a lapsed
    deadline, per-stage coverage, an escape row. Covers the whole-bag round trip that
    the single-key tests above (a raised `qenum-` row; a never-run enumeration) do not,
    so it stands beside them rather than replacing either: they enter by one key, this
    by all of them."""
    state = store.load(SID)
    state.plugins["premise"].update(RETIRED_KEYS)
    store.save(state)
    assert _review(store, session, f"topo:{PAIR}", ["Which region ships first?"]).ok

    refused = _approve(store)
    assert refused.ok is False
    blockers = _blockers(refused)
    assert len(blockers) == 1 and "qrev-2-1-1" in blockers[0], blockers

    _dismiss(store, "qrev-2-1-1")
    allowed = _approve(store)
    assert allowed.ok is True, _blockers(allowed)
    kept = _bag(store)
    for key, value in RETIRED_KEYS.items():
        assert kept[key] == value, key


# --- (e) a reply field that cannot be placed is refused, never read as no questions ---

def _field_error(*lines) -> str:
    from lib.review_block import QuestionFieldError, find_terminal_review_block
    block = find_terminal_review_block(_reply(*lines))
    assert block is not None
    with pytest.raises(QuestionFieldError) as exc:
        block.questions
    return str(exc.value)


def test_a_question_on_the_header_line_is_refused_not_dropped():
    message = _field_error("Customer questions: Which region ships first?")
    assert "Which region ships first?" in message


@pytest.mark.parametrize("item", [
    "- Which region ships first?",
    "* Which region ships first?",
    "1. Which region ships first?",
    "2) Which region ships first?",
])
def test_a_list_item_without_q_is_refused_not_dropped(item):
    message = _field_error("Customer questions:", item)
    assert "Which region ships first?" in message


def test_a_list_item_after_a_q_line_is_refused_not_joined_to_it():
    message = _field_error("Customer questions:", "Q: Which region ships first?",
                           "- Is the legacy API in scope?")
    assert "Is the legacy API in scope?" in message


@pytest.mark.parametrize("concern", [
    "blocking: C2: the order's second requirement is uncovered",
    "note: C2: wording could be tighter",
])
def test_a_concern_after_the_questions_field_is_refused_not_counted(concern):
    """Decision: refuse, do not count. A concern written after the field would be read
    as a continuation of the last question or silently lost; the reply protocol puts
    concerns first, so the refusal names the placement and the reviewer re-states."""
    message = _field_error("Customer questions:", "Q: Which region ships first?", concern)
    assert "concern" in message and "before" in message


def test_none_followed_by_a_question_is_refused():
    _field_error("Customer questions: none", "Q: Which region ships first?")


def test_a_second_questions_header_is_refused():
    _field_error("Customer questions:", "Q: a?", "Customer questions:", "Q: b?")


def test_an_empty_q_line_is_refused():
    _field_error("Customer questions:", "Q:")


@pytest.mark.parametrize("header", [
    "Customer Questions:",
    "customer questions:",
    "CUSTOMER QUESTIONS:",
    "Customer question:",
    "**Customer questions:**",
    "**Customer questions**:",
    "`Customer questions`:",
    "## Customer questions:",
])
def test_a_decorated_or_re_cased_header_still_opens_the_field_on_a_pass(header):
    from lib.review_block import find_terminal_review_block
    block = find_terminal_review_block(_reply(
        "note: C2: wording could be tighter", header, "Q: Which region ships first?"))
    assert list(block.questions) == ["Which region ships first?"]
    assert [v for (k, v), _ in block.region if k == "condition"] == [
        "note: C2: wording could be tighter"]


@pytest.mark.parametrize("lines", [
    ("**Customer questions**: Which region ships first?",),
    ("customer questions: Which region ships first?",),
    ("Customer question: Which region ships first?",),
    ("customer questions:", "- Which region ships first?"),
])
def test_a_re_cased_header_with_an_inline_or_unprefixed_question_is_refused_on_a_pass(lines):
    message = _field_error(*lines)
    assert "Which region ships first?" in message


def test_a_re_cased_header_question_reaches_the_driver_on_a_pass(rig):
    from test_plan_review_topological import flags
    rc, out, recorded = _drive(rig, "Customer Questions:", "Q: Which region ships first?")
    assert recorded is not None, out
    assert flags(recorded, "--customer-question") == ["Which region ships first?"]


def test_a_questions_field_before_the_digest_line_is_refused_naming_the_placement():
    from lib.review_block import QuestionFieldError, find_terminal_review_block
    block = find_terminal_review_block("\n".join([
        "REVIEW:", "Verdict: pass", "Customer questions:", "Q: Which region ships first?",
        f"Plan digest: {'0' * 64}", "note: C2: wording could be tighter"]))
    assert block is not None
    with pytest.raises(QuestionFieldError) as exc:
        block.questions
    assert "before" in str(exc.value) and "Plan digest:" in str(exc.value)


def test_a_q_line_before_the_verdict_is_refused_naming_the_placement():
    from lib.review_block import QuestionFieldError, find_terminal_review_block
    block = find_terminal_review_block("\n".join([
        "REVIEW:", "Q: Which region ships first?", "Verdict: pass", f"Plan digest: {'0' * 64}"]))
    assert block is not None
    with pytest.raises(QuestionFieldError):
        block.questions


@pytest.mark.parametrize("lines", [
    ("Customer questions: None.",),
    ("Customer questions: (none)",),
    ("Customer questions: NONE",),
    ("**Customer questions:** none",),
    ("Customer questions:", "none"),
    ("Customer questions:", "None."),
    ("Customer questions:", "- none"),
    ("Customer questions:", "**none**"),
])
def test_a_decorated_none_is_no_questions(lines):
    from lib.review_block import find_terminal_review_block
    block = find_terminal_review_block(_reply(*lines))
    assert list(block.questions) == []


def test_a_decorated_none_does_not_stall_the_driver(rig):
    from test_plan_review_topological import flags
    rc, out, recorded = _drive(rig, "Customer questions:", "none")
    assert recorded is not None, out
    assert flags(recorded, "--customer-question") == []


def test_a_lone_none_line_followed_by_a_question_is_refused():
    _field_error("Customer questions:", "none", "Q: Which region ships first?")


@pytest.mark.parametrize("second", ["Q :", "q:", "Q1:", "Q2 :", "q3:"])
def test_a_near_miss_q_prefix_after_a_question_is_refused_not_merged(second):
    message = _field_error(
        "Customer questions:", "Q: Which region ships first?", f"{second} Is the legacy API in scope?")
    assert "Is the legacy API in scope?" in message


def test_a_near_miss_q_prefix_as_the_first_line_is_refused():
    _field_error("Customer questions:", "q: Which region ships first?")


def test_a_q_line_before_the_header_is_a_question_not_a_concern_continuation():
    from lib.review_block import find_terminal_review_block
    block = find_terminal_review_block(_reply(
        "note: C2: wording could be tighter",
        "Q: Which region ships first?",
        "Customer questions:",
        "Q: Is the legacy API in scope?"))
    assert list(block.questions) == ["Which region ships first?", "Is the legacy API in scope?"]
    assert [v for (k, v), _ in block.region if k == "condition"] == [
        "note: C2: wording could be tighter"]


def test_a_continuation_line_still_joins_its_question():
    from lib.review_block import find_terminal_review_block
    block = find_terminal_review_block(_reply(
        "Customer questions:", "Q: Which region", "ships first?", "Q: Is the legacy API in scope?"))
    assert list(block.questions) == ["Which region ships first?", "Is the legacy API in scope?"]


def test_the_marker_extractor_reading_a_refused_field_does_not_raise():
    from lib.review_block import find_terminal_review_block
    block = find_terminal_review_block(_reply("Customer questions: Which region ships first?"))
    assert block is not None
    assert block.verdict == "pass"


@pytest.mark.parametrize("lines,phrase", [
    (("Customer questions: Which region ships first?",), "Which region ships first?"),
    (("Customer questions:", "- Which region ships first?"), "Which region ships first?"),
    (("Customer questions:", "1. Which region ships first?"), "Which region ships first?"),
    (("Customer questions:", "Q: Which region ships first?",
      "blocking: C2: the order's second requirement is uncovered"), "concern"),
])
def test_driver_refuses_an_unplaceable_field_and_records_nothing(rig, lines, phrase):
    rc, out, recorded = _drive(rig, *lines)
    assert recorded is None, recorded
    assert rc != 0
    refusal = [ln for ln in out if ln.startswith("TOPO-REFUSED: pair=3-1")]
    assert refusal and phrase in refusal[0], out


def test_driver_refuses_a_revise_with_a_concern_after_the_field_and_records_nothing(
        rig, monkeypatch):
    from test_plan_review_topological import canonical_stdout, review_original
    rig.specs = {"3-1": {"stdout": canonical_stdout(review_original(
        "revise", rig.sha,
        ["blocking: C1: the first requirement is uncovered",
         "Customer questions:", "Q: Which region ships first?",
         "blocking: C2: the order's second requirement is uncovered"]))}}
    rc, out = rig.run()
    assert not rig.engine.verbs("plan-review")
    assert any(ln.startswith("TOPO-REFUSED: pair=3-1") for ln in out), out


# --- (f) the engine refuses an unreadable --customer-question before recording ------

def _state_slice(store) -> tuple:
    s = store.load(SID)
    return (s.plugins, s.history, s.plan_review, s.plan_stage_reviews, s.plan_pair_reviews)


@pytest.mark.parametrize("scope", [f"topo:{PAIR}", f"topo:{UNIT}", "stage:1", ""])
@pytest.mark.parametrize("bad,phrase", [
    ("", "blank"),
    ("   ", "blank"),
    ("two\nlines", "line"),
    ("Q: Which region ships first?", "Q:"),
    ("Customer questions: Which region ships first?", "Customer questions:"),
    ("none", "none"),
    ("(None.)", "none"),
    ("customer questions: Which region ships first?", "Customer questions:"),
    ("Customer question: Which region ships first?", "Customer questions:"),
])
def test_plan_review_refuses_an_unreadable_customer_question_and_changes_nothing(
        store, session, scope, bad, phrase):
    before = _state_slice(store)
    d = _review(store, session, scope, ["a fine question?", bad])
    assert d.ok is False
    assert phrase in d.detail, d.detail
    assert _state_slice(store) == before
    assert _qrev(store) == {}


# --- (g) the root's recording instructions name --customer-question ------------------

@pytest.fixture
def review_gates_armed(monkeypatch):
    """Override conftest's suite-wide plan/code-review force-offs so the review_dispatch
    observer's own arming predicate decides, as test_plugins_review_dispatch does."""
    for knob in ("AGENTCTL_PLAN_REVIEW", "AGENTCTL_CODE_REVIEW", "AGENTCTL_REVIEW_DISPATCH"):
        monkeypatch.delenv(knob, raising=False)


def _dispatch_state_for_whole_plan():
    from agentctl.state import Node
    from test_plugins_review_dispatch import _dispatch_state, _dev_stage
    state = _dispatch_state(_dev_stage())
    state.node = Node.PLAN_READY.value
    state.plan_path = "/tmp/some-plan.toml"
    return state


def _thinker_directive(state) -> dict:
    from agentctl import plugins
    from agentctl.directive import Directive
    fired = plugins.fire("submit_plan", state, Directive(True, state.node, "noop"))
    return next(p for p in fired if p["plugin"] == "review_dispatch"
                and p["action"] == "spawn_thinker_review")


def test_the_whole_plan_review_directive_names_customer_question(review_gates_armed):
    d = _thinker_directive(_dispatch_state_for_whole_plan())
    assert d["data"]["mode"] == "whole"
    assert "--customer-question" in d["detail"]
    assert "Customer questions:" in d["detail"]


def test_the_per_stage_review_directive_names_customer_question_on_every_scope(
        review_gates_armed, monkeypatch):
    from agentctl import gates
    real = gates.review_delta

    def two_scopes(*a, **kw):
        delta = dict(real(*a, **kw))
        delta["record_scope_args"] = ["--scope stage:1", "--scope stage:2"]
        return delta

    monkeypatch.setattr(gates, "review_delta", two_scopes)
    detail = _thinker_directive(_dispatch_state_for_whole_plan())["detail"]
    for scope in ("--scope stage:1", "--scope stage:2"):
        record = next(part for part in detail.split("; ") if scope in part)
        assert "--customer-question" in record, record


def test_the_walk_record_template_names_customer_question(capsys, tmp_path, fixtures_dir):
    import json
    root = str(tmp_path / "state")
    plan = str(fixtures_dir / "plan_two_stage.toml")
    for argv in (["start", "--session", "wq", "--task", "t", "--goal", "g",
                  "--done-criterion", "dc", "--criterion-type", "measurable"],
                 ["classify", "--session", "wq", "--architectural"],
                 ["plan", "--session", "wq"],
                 ["submit-plan", "--session", "wq", "--plan", plan]):
        cli.main(["--state-root", root, *argv])
        capsys.readouterr()
    cli.main(["--state-root", root, "plan-review-walk", "--session", "wq",
              "--target", plan, "--format", "json"])
    walk = json.loads(capsys.readouterr().out)["data"]
    records = [row["record"] for level in walk["levels"] for row in level if row["record"]]
    assert records
    assert all("--customer-question" in r for r in records), records


# --- (h) questions passed to an unarmed premise plugin are reported, not swallowed ---

def test_questions_with_the_premise_plugin_unarmed_surface_the_count(
        store, plan, monkeypatch):
    monkeypatch.setenv("AGENTCTL_PREMISE", "0")
    cli.cmd_start(ns(session=SID, task="demo", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=SID, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=SID), store=store)
    cli.cmd_submit_plan(ns(session=SID, plan=str(plan)), store=store)
    assert "premise" not in store.load(SID).plugins

    d = _review(store, plan, f"topo:{PAIR}", ["Which region ships first?", "Is it GA?"])
    assert d.ok, d.detail
    assert d.data["customer_questions_unrecorded"] == 2
    assert d.data["customer_question_ids"] == []


def test_no_unrecorded_count_when_the_plugin_is_armed_or_none_were_passed(store, session):
    armed = _review(store, session, f"topo:{PAIR}", ["Which region ships first?"])
    assert "customer_questions_unrecorded" not in armed.data
    none_passed = _review(store, session, "stage:1", None)
    assert "customer_questions_unrecorded" not in none_passed.data
