"""question-raise / -research / -dispose / -rebind / -retire / -list / -check: the
CLI surface for the premise (question-provenance) bag. premise.py's pure
validate_questions is the closure check and plugins_premise.premise_blockers is the
plan_approval gate — these seven commands only read/write the bag and (for -check)
report the SAME blockers the gate reports.

Covers: raise is permissive (a malformed target is stored, not argparse-rejected);
research records the own-research attempt as a separate act; dispose refuses
`--to escalated` with empty own_research at the CLI AND the gate refuses it too;
dispose stamps disposed_at_key from the BOUND stage's current key, PER ENTRY, so a
later dispose of a different question never launders the first one's stamp; rebind
re-stamps to a changed stage's current key (clearing blocker 12); retire clears the
dangling-edge blocker (rule 2); and question-check's blocker list is byte-identical
to the gate's.
"""
from __future__ import annotations

from argparse import Namespace

from agentctl import cli, plugins, plugins_premise, premise
from agentctl.plan import load_plan, stage_question_key
from agentctl.state import SessionState


# --- helpers -------------------------------------------------------------------

def _state(store, sid="s", *, plan_path=None):
    state = SessionState(session_id=sid, task_id="t")
    plugins.activate(state, "premise")
    if plan_path is not None:
        state.plan_path = str(plan_path)
    store.save(state)
    return state


def _raise(store, sid, *, id, target, question="?"):
    return cli.cmd_question_raise(
        Namespace(session=sid, id=id, target=target, question=question), store=store)


def _research(store, sid, *, id, attempted):
    return cli.cmd_question_research(
        Namespace(session=sid, id=id, attempted=attempted), store=store)


def _dispose(store, sid, *, id, to, answer="", source="", derivation="", basis="", risk=""):
    return cli.cmd_question_dispose(Namespace(
        session=sid, id=id, to=to, answer=answer, source=source,
        derivation=derivation, basis=basis, risk=risk), store=store)


def _rebind(store, sid, *, id, reason):
    return cli.cmd_question_rebind(
        Namespace(session=sid, id=id, confirm_still_valid=reason), store=store)


def _retire(store, sid, *, id, reason):
    return cli.cmd_question_retire(Namespace(session=sid, id=id, reason=reason), store=store)


def _check(store, sid):
    return cli.cmd_question_check(Namespace(session=sid), store=store)


def _questions(store, sid):
    return store.load(sid).plugins["premise"]["questions"]


def _q(store, sid, qid):
    return next(q for q in _questions(store, sid) if q["id"] == qid)


_STAGE_TMPL = """\
[[stage]]
index = {i}
title = "Stage {i}"
executor = "spawn:developer"
expected_result_image = "{img}"
criterion_type = "measurable"
done_criterion = "stage {i} done"
depends_on = {deps}
output_artifacts = ["s{i}.py"]
"""


def _write_plan(path, stages):
    """stages: list of (index, expected_result_image). Each stage depends on the
    previous one so the graph validates; any TRAILING stage can be dropped by
    simply omitting it (nothing depends on it)."""
    body = [
        "[meta]",
        'task_id = "demo-premise"',
        'goal = "exercise the question-provenance CLI"',
        'done_criterion = "all stages PASSED"',
        'criterion_type = "measurable"',
        "",
    ]
    prev = None
    for i, img in stages:
        deps = "[]" if prev is None else f"[{prev}]"
        body.append(_STAGE_TMPL.format(i=i, img=img, deps=deps))
        prev = i
    path.write_text("\n".join(body), encoding="utf-8")
    return path


# --- the plugin-inactive guard (mirrors ledger's) ------------------------------

def test_question_raise_refused_when_plugin_inactive(store):
    state = SessionState(session_id="s", task_id="t")
    store.save(state)
    d = _raise(store, "s", id="Q1", target="plan.goal")
    assert d.ok is False and d.action == "noop" and "not active" in d.detail
    assert "premise" not in store.load("s").plugins


# --- raise is permissive -------------------------------------------------------

def test_question_raise_is_permissive(store):
    _state(store)
    # a malformed target is STORED, not rejected — the moment-of-arising record is
    # never lost to an argparse rejection; the GATE reports the bad target.
    d = _raise(store, "s", id="Q1", target="not-a-legal-target", question="is X true?")
    assert d.ok is True
    stored = _q(store, "s", "Q1")
    assert stored["target"] == "not-a-legal-target"
    assert stored["disposition"] == "open"
    # and the gate DOES surface it as unparseable
    blockers = plugins_premise.premise_blockers(store.load("s"), store.load("s").plugins["premise"])
    assert any("unparseable" in b and "Q1" in b for b in blockers)


# --- research records the own-research attempt as a separate act ----------------

def test_question_research_records_attempt(store):
    _state(store)
    _raise(store, "s", id="Q1", target="plan.goal")
    d = _research(store, "s", id="Q1", attempted="checked the ADR and the two prior runs")
    assert d.ok is True
    assert _q(store, "s", "Q1")["own_research"] == "checked the ADR and the two prior runs"
    # research does NOT disposition
    assert _q(store, "s", "Q1")["disposition"] == "open"


# --- dispose: escalate-without-research refused at CLI AND at gate --------------

def test_dispose_escalated_refused_without_own_research(store):
    _state(store)
    _raise(store, "s", id="Q1", target="plan.goal")

    # (1) the CLI fast-fail
    d = _dispose(store, "s", id="Q1", to="escalated", answer="ask the user")
    assert d.ok is False and d.action == "noop"
    assert "question-research" in d.detail
    # the disposition did NOT land — the question is still open
    assert _q(store, "s", "Q1")["disposition"] == "open"

    # (2) the GATE is the real authority: an escalated question with no own_research
    # blocks in premise.validate_questions regardless of how it got there.
    smuggled = premise.Question(
        id="Q1", target="plan.goal", question="?", disposition="escalated",
        answer="ask the user", own_research="")
    gate_blockers = premise.validate_questions([smuggled], stage_keys={})
    assert any("own research must precede escalation" in b for b in gate_blockers)


# --- dispose stamps the BOUND stage's current key ------------------------------

def test_dispose_stamps_bound_stage_key(store, tmp_path):
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one"), (2, "img-two")])
    _state(store, plan_path=plan_path)
    _raise(store, "s", id="Q1", target="stage:1.result")
    _research(store, "s", id="Q1", attempted="derived from the source")
    d = _dispose(store, "s", id="Q1", to="researched",
                 answer="yes", source="ADR-0001", derivation="follows from §3")
    assert d.ok is True

    doc = load_plan(str(plan_path))
    stage1 = next(s for s in doc.stages if s.index == 1)
    assert _q(store, "s", "Q1")["disposed_at_key"] == stage_question_key(stage1)
    assert _q(store, "s", "Q1")["disposed_at_key"] != ""


def test_dispose_goal_target_stamps_empty_key(store, tmp_path):
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one")])
    _state(store, plan_path=plan_path)
    _raise(store, "s", id="Q1", target="plan.goal")
    _research(store, "s", id="Q1", attempted="x")
    _dispose(store, "s", id="Q1", to="assumed", basis="stated by user", risk="none")
    # plan.goal has no per-stage key to bind to -> ""
    assert _q(store, "s", "Q1")["disposed_at_key"] == ""


# --- the laundering regression: a later dispose never restamps an earlier one ---

def test_key_stamp_not_laundered_by_later_dispose(store, tmp_path):
    plan_path = _write_plan(
        tmp_path / "plan.toml", [(1, "img-one"), (2, "img-two"), (3, "img-three")])
    _state(store, plan_path=plan_path)

    _raise(store, "s", id="Q1", target="stage:1.result")
    _research(store, "s", id="Q1", attempted="r1")
    _dispose(store, "s", id="Q1", to="researched", answer="a1", source="src1",
             derivation="follows for 1")
    key_q1_original = _q(store, "s", "Q1")["disposed_at_key"]
    assert key_q1_original != ""

    # edit stage 3 (NOT stage 1) — the plan bytes change, but Q1 is bound to stage 1
    _write_plan(plan_path, [(1, "img-one"), (2, "img-two"), (3, "img-three-EDITED")])

    # dispose a SECOND question bound to a different stage
    _raise(store, "s", id="Q2", target="stage:2.result")
    _research(store, "s", id="Q2", attempted="r2")
    _dispose(store, "s", id="Q2", to="researched", answer="a2", source="src2",
             derivation="follows for 2")

    # v1's defect (a bag-level plan_sha256 restamped on every add) would have moved
    # Q1's key here. The per-entry stamp is untouched.
    assert _q(store, "s", "Q1")["disposed_at_key"] == key_q1_original
    # and it still matches stage 1's (unchanged) key
    doc = load_plan(str(plan_path))
    stage1 = next(s for s in doc.stages if s.index == 1)
    assert _q(store, "s", "Q1")["disposed_at_key"] == stage_question_key(stage1)


# --- rebind clears blocker 12 (bound stage definition changed) ------------------

def test_rebind_clears_key_blocker(store, tmp_path):
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one"), (2, "img-two")])
    _state(store, plan_path=plan_path)
    _raise(store, "s", id="Q1", target="stage:1.result")
    _research(store, "s", id="Q1", attempted="r1")
    _dispose(store, "s", id="Q1", to="researched", answer="a1", source="src1",
             derivation="follows")

    # change stage 1's definition -> Q1's stamped key no longer matches (blocker 12)
    _write_plan(plan_path, [(1, "img-one-EDITED"), (2, "img-two")])
    blockers_before = _check(store, "s").data["blockers"]
    assert any("definition\nchanged" in b or "definition changed" in b for b in blockers_before)

    d = _rebind(store, "s", id="Q1", reason="re-read against the new stage 1; still holds")
    assert d.ok is True

    blockers_after = _check(store, "s").data["blockers"]
    assert not any("definition changed" in b for b in blockers_after)
    # the stamp now matches the CURRENT stage 1 key
    doc = load_plan(str(plan_path))
    stage1 = next(s for s in doc.stages if s.index == 1)
    assert _q(store, "s", "Q1")["disposed_at_key"] == stage_question_key(stage1)


# --- retire clears the dangling-edge blocker (rule 2) --------------------------

def test_retire_clears_dangling_blocker(store, tmp_path):
    plan_path = _write_plan(
        tmp_path / "plan.toml", [(1, "img-one"), (2, "img-two"), (3, "img-three")])
    _state(store, plan_path=plan_path)
    _raise(store, "s", id="Q1", target="stage:3.result")
    _research(store, "s", id="Q1", attempted="r1")
    _dispose(store, "s", id="Q1", to="researched", answer="a1", source="src1",
             derivation="follows")

    # drop stage 3 — Q1's target is now dangling
    _write_plan(plan_path, [(1, "img-one"), (2, "img-two")])
    blockers_before = _check(store, "s").data["blockers"]
    assert any("dangling" in b and "Q1" in b for b in blockers_before)

    d = _retire(store, "s", id="Q1", reason="stage 3 was removed in the last replan")
    assert d.ok is True
    assert _q(store, "s", "Q1")["disposition"] == "retired"

    blockers_after = _check(store, "s").data["blockers"]
    assert not any("dangling" in b for b in blockers_after)


# --- question-check is byte-identical to the gate ------------------------------

def test_question_check_matches_gate_blockers(store, tmp_path):
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one"), (2, "img-two")])
    state = _state(store, plan_path=plan_path)
    # a spread of states: one open, one researched-but-stale, one clean-ish
    _raise(store, "s", id="Q1", target="stage:1.result")  # open
    _raise(store, "s", id="Q2", target="not-a-target")     # unparseable

    live = store.load("s")
    gate = plugins_premise.premise_blockers(live, live.plugins["premise"])
    check = _check(store, "s")
    assert check.data["blockers"] == gate
    # ok tracks emptiness of the blocker list
    assert check.ok is (len(gate) == 0)
