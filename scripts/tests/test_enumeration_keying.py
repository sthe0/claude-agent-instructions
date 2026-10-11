"""Per-part keying of the plan digest, and what survives of it after the enumerator's
retirement.

The plan's content digest is keyed per part -- the meta/order and one entry per stage --
so `plan.changed_parts` can say which parts moved. The standalone enumeration that used
to consume that is retired (amendments-2.md E3); what remains pinned here is the
composite digest every persisted `enumerated_at` binds to, `changed_parts` against a
supplied baseline, and the `qenum-` upsert that keeps a disposition on an untouched stage.
"""
from __future__ import annotations

import hashlib
from argparse import Namespace
from pathlib import Path

from agentctl import cli, plan, plugins, plugins_premise
from agentctl.plan import load_plan
from agentctl.state import SessionState

FIXTURES = Path(__file__).resolve().parent / "fixtures"


_STAGE_TMPL = """\
[[stage]]
index = {i}
title = "Stage {i}"
executor = "spawn:developer"
expected_result_image = "{img}"
criterion_type = "measurable"
done_criterion = "stage {i} done{tail}"
depends_on = {deps}
output_artifacts = ["s{i}.py"]
"""


def _write_plan(path, stages, *, goal="exercise per-part enumeration keying"):
    body = [
        "[meta]",
        'task_id = "demo-keying"',
        f'goal = "{goal}"',
        'done_criterion = "all stages PASSED"',
        'criterion_type = "measurable"',
        "",
    ]
    prev = None
    for i, img in stages:
        deps = "[]" if prev is None else f"[{prev}]"
        body.append(_STAGE_TMPL.format(i=i, img=img, deps=deps, tail=""))
        prev = i
    path.write_text("\n".join(body), encoding="utf-8")
    return path


def _state(store, sid="s", *, plan_path):
    state = SessionState(session_id=sid, task_id="t")
    plugins.activate(state, "premise")
    state.plan_path = str(plan_path)
    store.save(state)
    return state


def _bag(store, sid="s"):
    return store.load(sid).plugins["premise"]


# --- the composite is the compatibility contract --------------------------------

def test_the_composite_digest_reproduces_the_pre_split_value():
    """The value below is this fixture's digest under the pre-split derivation — the
    one every live session's escape rows, launch window and `enumerated_at` were
    written against. Recomposing the digest from per-stage parts must not move it, so
    the pin is a literal, and the second assertion says where the literal came from:
    the payload expression is the one `_plan_content_digest` carried before the split
    (agentctl @ 90a6f08). Editing the fixture invalidates the pair, not just the code."""
    doc = load_plan(FIXTURES / "plan_two_stage.toml")
    assert plan.plan_content_digest(doc) == (
        "16d4cb1479155b598093362b1a136cb773c378251f299374dbc8083f429277d3"
    )

    pre_split_payload = repr((
        doc.meta.goal,
        doc.meta.done_criterion,
        doc.meta.criterion_type,
        doc.meta.weight_class,
        doc.meta.repo_root,
        tuple(sorted((s.index, plan.stage_question_key(s)) for s in doc.stages)),
    ) + plan.order_place(doc.meta))
    assert plan.plan_content_digest(doc) == hashlib.sha256(
        pre_split_payload.encode("utf-8")).hexdigest()


# --- changed_parts takes its baseline as a parameter ----------------------------

def test_changed_parts_compares_against_a_supplied_baseline(tmp_path):
    """The baseline is an argument, not something read out of a premise bag: the same
    comparison has to serve a plan review's own recorded keys."""
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one"), (2, "img-two")])
    doc = load_plan(plan_path)
    baseline = {"meta": plan.plan_meta_digest(doc),
                "stages": {str(i): d for i, d in plan.plan_stage_digests(doc).items()}}
    assert plan.changed_parts(doc, baseline) == (False, set())

    baseline["stages"]["2"] = "moved"
    assert plan.changed_parts(doc, baseline) == (False, {2})

    baseline["meta"] = "moved"
    assert plan.changed_parts(doc, baseline) == (True, {2})


# --- a disposition on an untouched stage survives a narrowed upsert ---------------

def _apply(store, sid, plan_path, pairs, *, parts=None):
    """The `qenum-` upsert the standalone enumeration used to drive -- called directly,
    since no engine path runs it for a new plan any more."""
    state = store.load(sid)
    cli._apply_enumeration_result(
        state.plugins["premise"], load_plan(plan_path), plan_path, pairs, True,
        parts=parts, preserve_disposition=True)
    store.save(state)


def test_a_disposed_candidate_on_an_untouched_stage_survives_the_re_run(store, tmp_path):
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one"), (2, "img-two")])
    _state(store, plan_path=plan_path)
    _apply(store, "s", plan_path, [("stage:1.means", "why this tool?"),
                                   ("stage:2.result", "what does done look like?")])
    assert [c["id"] for c in _bag(store)["candidates"]] == ["qenum-s1-1", "qenum-s2-1"]

    cli.cmd_question_candidate_dispose(
        Namespace(session="s", id="qenum-s1-1", as_="dismissed",
                  reason="the tool is fixed by the order", question=""),
        store=store)

    _write_plan(plan_path, [(1, "img-one"), (2, "img-two-EDITED")])
    _apply(store, "s", plan_path, [("stage:2.result", "and now?")], parts=(False, {2}))

    candidates = {c["id"]: c for c in _bag(store)["candidates"]}
    assert candidates["qenum-s1-1"]["disposition"] == "dismissed"
    assert candidates["qenum-s1-1"]["reason"] == "the tool is fixed by the order"
    assert candidates["qenum-s2-1"]["disposition"] == "raised"
    assert candidates["qenum-s2-1"]["statement"] == "[stage:2.result] and now?"


def test_a_candidate_raised_under_the_old_id_scheme_keeps_its_disposition(store, tmp_path):
    """A session carried across the change holds `qenum-N` candidates. The pass that
    re-raises the same statement takes the row over under its new id instead of
    leaving the operator with both — one of them dispositioned, one of them not."""
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one")])
    state = _state(store, plan_path=plan_path)
    state.plugins["premise"]["candidates"] = [
        {"id": "qenum-1", "statement": "[stage:1.means] why this tool?",
         "disposition": "dismissed", "reason": "answered in the order", "question": ""}]
    store.save(state)

    doc = load_plan(plan_path)
    bag = store.load("s").plugins["premise"]
    cli._apply_enumeration_result(
        bag, doc, plan_path, [("stage:1.means", "why this tool?")], True,
        preserve_disposition=True)

    assert [c["id"] for c in bag["candidates"]] == ["qenum-s1-1"]
    assert bag["candidates"][0]["disposition"] == "dismissed"


# --- a plan edit under a discharged legacy record blocks nothing -------------------

def test_a_plan_edit_under_a_legacy_enumeration_record_raises_no_staleness_blocker(
        store, tmp_path):
    """The staleness blocker (an enumeration recorded against other plan bytes) is
    retired with the enumerator: nothing re-runs, so nothing is left to be stale."""
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one"), (2, "img-two")])
    _state(store, plan_path=plan_path)
    _apply(store, "s", plan_path, [])
    _write_plan(plan_path, [(1, "img-one"), (2, "img-two-EDITED")])

    live = store.load("s")
    bag = live.plugins["premise"]
    assert bag["enumerated_at"] != plugins_premise._plan_content_digest(load_plan(plan_path))
    blockers = plugins_premise.premise_blockers(live, bag)
    assert not any("different plan content" in b or "cross-check" in b
                   for b in blockers), blockers
