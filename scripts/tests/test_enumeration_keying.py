"""Per-part keying of the enumeration cross-check.

The cross-check used to be recorded against ONE whole-plan digest, so any edit
anywhere marked the whole record stale: the re-run re-read every stage, and every
candidate it raised came back `raised`, discarding the dispositions the coordinator
had already recorded against parts nobody touched. The record is now keyed per part —
the plan's meta/order and one entry per stage — so a plan edit marks stale only the
parts whose digest moved, the re-run covers only those parts, and a candidate
dispositioned against an unchanged part keeps its disposition.

What must NOT move with it: the composite digest of an unchanged plan, which escapes,
launch windows and every persisted `enumerated_at` bind to; the closed escape-reason
set; and the stale branch's deliberate lack of an escape.
"""
from __future__ import annotations

import hashlib
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

from agentctl import cli, plan, plugins, plugins_premise, premise
from agentctl.plan import load_plan
from agentctl.state import SessionState

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _runner(stdout, *, returncode=0):
    calls: list[list[str]] = []

    def run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    run.calls = calls
    return run


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


def _enumerate(store, sid, run):
    return cli.cmd_question_enumerate(Namespace(session=sid), store=store, runner=run)


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


def test_an_escape_recorded_before_the_split_still_discharges():
    """A bag minted before per-part keying carries an escape bound to the composite and
    no part digests at all. It must keep discharging: the alternative is a fleet-wide
    re-arming of the very blocker the escape was recorded to clear."""
    doc = load_plan(FIXTURES / "plan_two_stage.toml")
    digest = plugins_premise._plan_content_digest(doc)
    bag = {
        "enumerated": True,
        "enumerated_at": digest,
        "enumerated_runner_ok": False,
        "enumerate_launch": 2,
        "enumerate_pass": 1,
        "escapes": [{"reason": premise.ESCAPE_ADVISOR_TIMEOUT, "content_digest": digest,
                     "enumerate_launch": 2, "enumerate_pass": 1}],
    }
    assert plugins_premise.escape_recorded(
        bag, digest, premise.ENUMERATION_RUNNER_FAILURE_REASONS) is True
    assert plugins_premise.stale_enumeration_parts(bag, doc) == (False, set())


def test_a_bag_minted_before_the_split_is_judged_whole():
    """The same legacy bag against CHANGED bytes: with no part digests to compare, the
    only honest answer is that no part is covered — never 'nothing moved'."""
    doc = load_plan(FIXTURES / "plan_two_stage.toml")
    bag = {"enumerated": True, "enumerated_at": "a-digest-of-other-bytes"}
    assert plugins_premise.stale_enumeration_parts(bag, doc) == (True, {1, 2})


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


# --- a stage edit marks only that stage ----------------------------------------

def test_editing_one_stage_marks_only_that_stage_stale(store, tmp_path):
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one"), (2, "img-two")])
    _state(store, plan_path=plan_path)
    _enumerate(store, "s", _runner(""))

    live = store.load("s")
    assert plugins_premise.stale_enumeration_parts(
        live.plugins["premise"], load_plan(plan_path)) == (False, set())

    _write_plan(plan_path, [(1, "img-one"), (2, "img-two-EDITED")])
    live = store.load("s")
    doc = load_plan(plan_path)
    assert plugins_premise.stale_enumeration_parts(live.plugins["premise"], doc) == (
        False, {2})
    assert plugins_premise.enumeration_run_scope(live.plugins["premise"], doc) == (
        False, {2})
    assert any("different plan content" in b
               for b in plugins_premise.premise_blockers(live, live.plugins["premise"]))


def test_a_meta_edit_scopes_the_re_run_to_the_whole_plan(store, tmp_path):
    """A moved goal re-opens every stage's fit to it, so the narrowing widens back to
    the whole plan rather than raising a `meta` part the stages are read apart from."""
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one"), (2, "img-two")])
    _state(store, plan_path=plan_path)
    _enumerate(store, "s", _runner(""))

    _write_plan(plan_path, [(1, "img-one"), (2, "img-two")], goal="a different goal")
    bag = _bag(store)
    doc = load_plan(plan_path)
    assert plugins_premise.stale_enumeration_parts(bag, doc) == (True, set())
    assert plugins_premise.enumeration_run_scope(bag, doc) == (True, {1, 2})


def test_a_narrowed_pass_reads_only_the_changed_stages(store, tmp_path):
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one"), (2, "img-two")])
    _state(store, plan_path=plan_path)
    _enumerate(store, "s", _runner(""))
    _write_plan(plan_path, [(1, "img-one"), (2, "img-two-EDITED")])

    run = _runner("stage:2.result\tis the edited image still checkable?")
    d = _enumerate(store, "s", run)
    prompt = run.calls[0][4]
    assert "stage 2 done" in prompt
    assert "stage 1 done" not in prompt
    assert d.data["whole_plan"] is False and d.data["stages"] == [2]
    assert plugins_premise.stale_enumeration_parts(_bag(store), load_plan(plan_path)) == (
        False, set())


# --- a disposition on an untouched stage survives the re-run --------------------

def test_a_disposed_candidate_on_an_untouched_stage_survives_the_re_run(store, tmp_path):
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one"), (2, "img-two")])
    _state(store, plan_path=plan_path)
    _enumerate(store, "s", _runner(
        "stage:1.means\twhy this tool?\nstage:2.result\twhat does done look like?"))
    assert [c["id"] for c in _bag(store)["candidates"]] == ["qenum-s1-1", "qenum-s2-1"]

    cli.cmd_question_candidate_dispose(
        Namespace(session="s", id="qenum-s1-1", as_="dismissed",
                  reason="the tool is fixed by the order", question=""),
        store=store)

    _write_plan(plan_path, [(1, "img-one"), (2, "img-two-EDITED")])
    _enumerate(store, "s", _runner("stage:2.result\tand now?"))

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


# --- the stale branch still has no escape ---------------------------------------

def test_the_stale_branch_still_admits_no_escape(store, tmp_path):
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one"), (2, "img-two")])
    _state(store, plan_path=plan_path)
    _enumerate(store, "s", _runner(""))
    _write_plan(plan_path, [(1, "img-one"), (2, "img-two-EDITED")])

    live = store.load("s")
    bag = live.plugins["premise"]
    digest = plugins_premise._plan_content_digest(load_plan(plan_path))
    bag["escapes"] = [
        {"reason": reason, "content_digest": digest,
         "enumerate_launch": int(bag.get("enumerate_launch") or 0),
         "enumerate_pass": int(bag.get("enumerate_pass") or 0)}
        for reason in premise.ENUMERATION_ESCAPE_REASONS
    ]
    assert plugins_premise._ENUMERATE_STALE in plugins_premise.premise_blockers(live, bag)
