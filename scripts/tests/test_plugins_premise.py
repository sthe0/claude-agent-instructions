"""The question-provenance plugin + stage_question_key: the plan_approval-gate arm.

Proves three things the ledger plugin's `resolution` arm has no analogue for:

  1. `plan.stage_question_key` is a per-stage digest covering the FULL stage
     definition INCLUDING the principle and supplies (which stage_carry_key omits),
     and it is invariant to an edit of any OTHER stage — so a question bound to
     `stage:<n>.principle` is invalidated exactly when stage n's principle changes,
     never by an unrelated stage edit.
  2. `premise` auto-activates for EVERY SUBSTANTIVE session on weight_class ALONE —
     the gap-2 arming fix — so an ordinary engineering plan (deliverable_kind unset)
     still gets the gate, unlike the reasoning-only ledger plugin.
  3. the `plan_approval` gate blocks while any question is open or the enumeration
     cross-check has not run against the CURRENT plan content, and a TOML
     comment-only edit (invisible to tomllib) does not re-block a discharged
     enumeration.
"""
from __future__ import annotations

from argparse import Namespace

import pytest

from agentctl import cli, plan, plugins
from agentctl import plugins_premise as pp
from agentctl.state import (
    Actor,
    Criterion,
    Means,
    Principle,
    SessionState,
    Stage,
    Subject,
    Supply,
    WeightClass,
)


@pytest.fixture(autouse=True)
def _premise_armed(monkeypatch):
    """Override conftest's suite-wide AGENTCTL_PREMISE=0 force-off: this module is
    the one place that exercises the real arming predicate, so it deletes the knob
    and lets the plain weight_class logic decide (substantive arms, small-change
    does not). A module-local autouse fixture runs after the conftest one, so this
    delenv wins for every test here."""
    monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)


def _new_state(sid="s", **kw):
    return SessionState(session_id=sid, task_id="t", **kw)


def _stage(*, index=1, title="Scaffold", principle=None, supplies=(), method="build it"):
    """A fully-populated Stage for exercising stage_question_key. Every field the
    key reads is set explicitly so a test can mutate exactly one and observe."""
    return Stage(
        index=index,
        title=title,
        subject=Subject(material="spec", result="module exists", invariants="imports stay clean"),
        means=Means(means="Edit mod.py", method=method),
        actor=Actor(executor="spawn:developer", capability_required="python"),
        criterion=Criterion(
            criterion_type="measurable",
            done_criterion="pytest green",
            verify_command="pytest -q",
            expected_exit=0,
        ),
        principle=principle,
        conditions="repo checked out",
        supplies=list(supplies),
    )


def _principle(statement="idempotent registration is safe",
               derivation="the docstring says a re-register replaces last-wins, so idempotence holds"):
    return Principle(
        statement=statement,
        source="plugins.register docstring",
        derivation=derivation,
        confidence="high",
        refutation="a second register raised instead of last-wins",
    )


# --- stage_question_key: covers the principle, stable across unrelated edits ----

def test_stage_question_key_changes_when_principle_changes():
    base = _stage(principle=_principle("idempotent registration is safe"))
    changed = _stage(principle=_principle("registration must raise on a duplicate name"))
    assert plan.stage_question_key(base) != plan.stage_question_key(changed)
    # and it is deterministic for identical content (survives being recomputed)
    assert plan.stage_question_key(base) == plan.stage_question_key(
        _stage(principle=_principle("idempotent registration is safe"))
    )


def test_stage_question_key_changes_when_principle_derivation_changes():
    # A derivation-only rewrite must move the key: a question bound to stage:<n>.principle
    # was answered against the OLD inference and must be re-examined when it changes.
    base = _stage(principle=_principle(derivation="the docstring states last-wins, so idempotence holds"))
    changed = _stage(principle=_principle(derivation="the type signature guarantees no duplicate, so idempotence holds"))
    assert plan.stage_question_key(base) != plan.stage_question_key(changed)


def test_stage_question_key_stable_across_unrelated_stage_edit():
    # stage 1 is fixed; stage 2 is edited. stage 1's key must not move, because the key
    # is a per-stage digest — an unrelated stage's edit cannot invalidate a question
    # bound to THIS stage. (F6: only the OWN bound stage's key matters.)
    stage1 = _stage(index=1, title="Scaffold", principle=_principle())
    before = plan.stage_question_key(stage1)

    # two genuinely different sibling stage-2 definitions — differing in title, method,
    # principle and supplies. The key IS content-sensitive (it tells them apart)...
    sib_a = _stage(index=2, title="Add tests", method="write pytest", principle=_principle("x"))
    sib_b = _stage(index=2, title="Add tests (revised)", method="write more pytest",
                   principle=_principle("y"), supplies=[Supply(on=1, element="result")])
    assert plan.stage_question_key(sib_a) != plan.stage_question_key(sib_b)

    # ...yet neither sibling's content feeds stage 1's digest, so editing stage 2
    # (sib_a -> sib_b) never moves stage 1's key.
    assert plan.stage_question_key(stage1) == before


# --- auto-activation: SUBSTANTIVE alone, deliverable_kind irrelevant (gap-2 fix) -

def _classify(store, sid, *, weight_kwargs, chat=False, architectural=True):
    cli.cmd_start(Namespace(session=sid, task="demo", goal="g", done_criterion="dc",
                            criterion_type="measurable", recursion_depth=0), store=store)
    return cli.cmd_classify(Namespace(
        session=sid, chat=chat, tracker_key=None, architectural=architectural,
        external_effect=False, new_dependency=False, public_api_change=False,
        deliverable_kind="", **weight_kwargs,
    ), store=store)


def test_auto_activates_for_substantive_without_deliverable_kind(store):
    # unit: the predicate fires on weight_class alone, with deliverable_kind unset
    substantive = _new_state(weight_class=WeightClass.SUBSTANTIVE.value, deliverable_kind="")
    assert pp._auto_activate(substantive) is True

    # e2e through classify: a SUBSTANTIVE session that never names a deliverable_kind
    # still gets a premise bag (the arming gap the ledger plugin left open).
    _classify(store, "pr-sub", weight_kwargs=dict(changed_lines=200, files=5, wall_clock_min=60))
    state = store.load("pr-sub")
    assert state.weight_class == WeightClass.SUBSTANTIVE.value
    assert state.deliverable_kind == ""
    assert "premise" in state.plugins
    assert state.plugins["premise"]["enumerated"] is False


def test_does_not_auto_activate_for_small_change(store):
    # unit: a SMALL_CHANGE session is never armed
    small = _new_state(weight_class=WeightClass.SMALL_CHANGE.value, deliverable_kind="")
    assert pp._auto_activate(small) is False

    # e2e: a change small enough to route SMALL_CHANGE gets no premise bag
    _classify(store, "pr-small", chat=False, architectural=False,
              weight_kwargs=dict(changed_lines=5, files=1, wall_clock_min=5))
    state = store.load("pr-small")
    assert state.weight_class == WeightClass.SMALL_CHANGE.value
    assert "premise" not in state.plugins


# --- the plan_approval gate blocks on an open question / un-run enumeration ------

def test_gate_blocks_open_question():
    state = _new_state()
    plugins.activate(state, "premise")
    # isolate the OPEN-question blocker from the enumeration blocker
    state.plugins["premise"]["enumerated"] = True
    state.plugins["premise"]["questions"] = [
        {"id": "q1", "target": "plan.goal", "question": "is the goal even reachable?"},
    ]
    blockers = plugins.plugin_gate_blockers(state, "plan_approval")
    assert blockers
    assert any("open" in b for b in blockers)
    # closing it (disposed as assumed with its required fields) clears the gate
    state.plugins["premise"]["questions"] = [
        {"id": "q1", "target": "plan.goal", "question": "is the goal even reachable?",
         "disposition": "assumed", "own_research": "read the tracker thread",
         "basis": "confirmed reachable by the reporter", "risk": "reporter may be wrong"},
    ]
    assert plugins.plugin_gate_blockers(state, "plan_approval") == []


def test_gate_blocks_when_not_enumerated():
    state = _new_state()
    plugins.activate(state, "premise")  # fresh bag: enumerated=False, no questions
    blockers = plugins.plugin_gate_blockers(state, "plan_approval")
    assert blockers == [f"[premise] {pp._ENUMERATE_NOT_RUN}"]
    # the same gate name that the ledger plugin gates (resolution) is untouched here
    assert plugins.plugin_gate_blockers(state, "resolution") == []


def test_gate_blocks_stale_enumerated_at(fixtures_dir):
    # with a submitted plan, an enumeration that ran against DIFFERENT content is stale
    plan_path = str(fixtures_dir / "plan_two_stage.toml")
    state = _new_state(plan_path=plan_path)
    plugins.activate(state, "premise")
    state.plugins["premise"]["enumerated"] = True
    state.plugins["premise"]["enumerated_at"] = "a-digest-of-some-earlier-plan"
    blockers = plugins.plugin_gate_blockers(state, "plan_approval")
    assert blockers == [f"[premise] {pp._ENUMERATE_STALE}"]

    # stamping the enumeration against the CURRENT content clears the gate
    current = pp._plan_content_digest(plan.load_plan(plan_path))
    state.plugins["premise"]["enumerated_at"] = current
    assert plugins.plugin_gate_blockers(state, "plan_approval") == []


def test_comment_only_plan_edit_does_not_reblock_enumeration(tmp_path, fixtures_dir):
    # a TOML comment is invisible to tomllib, so a comment-only edit leaves the
    # content digest byte-identical — a discharged enumeration must not re-block.
    src = (fixtures_dir / "plan_two_stage.toml").read_text(encoding="utf-8")
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text(src, encoding="utf-8")

    digest_before = pp._plan_content_digest(plan.load_plan(str(plan_path)))
    state = _new_state(plan_path=str(plan_path))
    plugins.activate(state, "premise")
    state.plugins["premise"]["enumerated"] = True
    state.plugins["premise"]["enumerated_at"] = digest_before
    assert plugins.plugin_gate_blockers(state, "plan_approval") == []

    # append a pure comment line and reload — tomllib drops it, so the digest holds
    plan_path.write_text(src + "\n# a note for a future reader, no field change\n", encoding="utf-8")
    digest_after = pp._plan_content_digest(plan.load_plan(str(plan_path)))
    assert digest_after == digest_before
    blockers = plugins.plugin_gate_blockers(state, "plan_approval")
    assert not any(pp._ENUMERATE_STALE in b for b in blockers)
    assert blockers == []
