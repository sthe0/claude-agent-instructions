"""The routes by which a sub-plan's method review is written, walked from the refusal to the lifted
blocker through the verbs a linked child (and the user's process) run.

A new-form sub-plan is refused at presentation and at the start of a decided-later stage until
the engine holds a method review of the record as it stands. Each refusal names a command; the
tests here run exactly that command (`gates.METHOD_REVIEW_WAY`) over a real linked child and
check that the blocker is gone, and check each route that must not write one (a stage-scoped
pass, one pair's pass, a revise) leaves it standing. Nothing here reaches a model judge or a model
CLI: the reviewer's verdict is the recorder verb's argument.
"""
from __future__ import annotations

import hashlib
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, gates, sub_plan
from agentctl.plan import load_plan, review_ids
from order_agreement_mutants import mutant  # noqa: F401  (autouse fixture)

import test_subplan_presentation_dispatch as base
from test_subplan_presentation_dispatch import (  # noqa: F401  (fixtures)
    CHILD, PARENT, Contour, _start_blockers, contour, home_store, ns,
)


def _sha(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _review(contour, verdict, *, scope=None, reviewer="thinker", target=None):
    plan = target or contour.plan
    argv = ["plan-review", "--verdict", verdict, "--reviewer", reviewer, "--target", plan]
    if scope:
        argv += ["--scope", scope]
    if verdict == "pass":
        argv += ["--plan-digest", _sha(plan)]
    if verdict == "revise":
        argv += ["--concern", "blocking: the method skips the migration"]
    code, body, err = contour.own(*argv)
    return code, body, err


def _method_reviewed(contour) -> bool:
    record = sub_plan.read(contour.store.load(PARENT), 1, contour.store)
    return sub_plan.method_review_recorded(record)


def _blocked(contour) -> bool:
    return any("method review" in line for line in _start_blockers(contour.store, sid=PARENT))


def _submitted_unreviewed(contour):
    assert contour.submit(reviewed=False)["ok"] is True
    assert not _method_reviewed(contour) and _blocked(contour)
    refusal = [line for line in _start_blockers(contour.store, sid=PARENT) if "method review" in line]
    assert all(gates.METHOD_REVIEW_WAY in line for line in refusal), refusal


def test_the_whole_plan_review_the_refusal_names_writes_the_method_review_and_lifts_the_blocker(contour):
    _submitted_unreviewed(contour)

    code, body, err = _review(contour, "pass")
    assert code == 0 and body.get("ok") is True, (body, err)

    assert _method_reviewed(contour) and not _blocked(contour)
    record = sub_plan.read(contour.store.load(PARENT), 1, contour.store)
    [review] = record.reviews
    assert review["kind"] == sub_plan.METHOD_REVIEW_KIND and review["by"] == "thinker"
    assert review["bound_to"] == sub_plan.approval_digest(record)


@pytest.mark.parametrize("gate", ["0", "1"], ids=["gate-off", "gate-on"])
def test_the_recorder_writes_the_method_review_whether_or_not_the_plan_review_gate_asks_for_one(
        contour, monkeypatch, gate):
    """A tier or a session on which the plan-review gate does not ask for a thinker's review still
    owes the method review of a new-form sub-plan: the recorder verb writes it as before, so the
    refusal's way on exists there too."""
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", gate)
    _submitted_unreviewed(contour)
    code, body, err = _review(contour, "pass")
    assert body.get("ok") is True, (body, err)
    assert _method_reviewed(contour) and not _blocked(contour)


def test_a_verdict_asking_for_a_revision_writes_no_method_review(contour):
    _submitted_unreviewed(contour)
    code, body, err = _review(contour, "revise")
    assert contour.child_state().plan_review.verdict == "revise", (body, err)
    assert not _method_reviewed(contour) and _blocked(contour)


def test_a_stage_scoped_pass_and_one_pairs_pass_leave_the_method_review_owed(contour):
    """A stage-scoped review and a single pair's review judge a part of the sub-plan; only a pass
    of the whole sub-plan file (the whole-plan verb with `--plan-digest`) is the method review."""
    _submitted_unreviewed(contour)
    doc = load_plan(contour.plan)
    _review(contour, "pass", scope="stage:1")
    assert contour.child_state().plan_stage_reviews["stage:1"].verdict == "pass"
    assert not _method_reviewed(contour) and _blocked(contour)

    ids = review_ids(doc)
    _review(contour, "pass", scope=f"topo:{ids[0]}")
    assert ids[0] in contour.child_state().plan_pair_reviews
    assert not _method_reviewed(contour) and _blocked(contour)


def _review_every_pair(contour):
    ids = review_ids(load_plan(contour.plan))
    assert ids, "the walk of a sub-plan with stages is never empty"
    for rid in ids:
        code, body, err = _review(contour, "pass", scope=f"topo:{rid}")
        assert body.get("ok") is True, (rid, body, err)


def _composed_by_verb(contour):
    return contour.own("plan-review-compose", "--target", contour.plan)[1]


def _composed_by_driver(contour):
    """The composition the driver `plan-review-topological.py` runs at the end of its walk
    (`Driver.compose`), reaching the engine through the child's channel as `--own` does."""
    import importlib.util as util
    import sys

    spec = util.spec_from_file_location("plan_review_topological_under_test",
                                        base.SCRIPTS / "plan-review-topological.py")
    driver_module = util.module_from_spec(spec)
    sys.modules[spec.name] = driver_module
    spec.loader.exec_module(driver_module)
    driver_module.run_agentctl = lambda argv, env, script=None: contour.own(*argv)[1]
    driver = driver_module.Driver(Namespace(pairs=None, view_root=None), contour.plan, {}, own=True)
    verdict = driver.compose()
    return {"ok": verdict == "pass"}


def test_the_pairwise_route_writes_no_method_review_and_the_whole_plan_review_it_names_lifts_the_blocker(
        contour):
    """No pair or unit bundle shows `meta.sub_plan`, so the composition of the pairs (the verb or
    the driver's own) records a pass of the plan-review gate and writes no method review; the
    refusal then still names the way on, the thinker's whole-plan review of the file, and that
    verb lifts it. Before every pair is recorded the composition is refused."""
    _submitted_unreviewed(contour)
    ids = review_ids(load_plan(contour.plan))

    assert _composed_by_verb(contour).get("ok") is False and _blocked(contour)
    for rid in ids:
        _review(contour, "pass", scope=f"topo:{rid}")
    assert _composed_by_verb(contour).get("ok") is True
    assert not _method_reviewed(contour) and _blocked(contour)
    assert sub_plan.read(contour.store.load(PARENT), 1, contour.store).reviews == []

    code, body, err = _review(contour, "pass")
    assert body.get("ok") is True, (body, err)
    assert _method_reviewed(contour) and not _blocked(contour)


@pytest.mark.parametrize("compose", [_composed_by_verb, _composed_by_driver],
                         ids=["the-verb", "the-drivers-own-composition"])
def test_a_method_rewritten_in_place_is_not_reviewed_by_a_composition(contour, compose):
    """The executor rewrites only the method of the file the reviewers read, at the same path, and
    resubmits it. The pair and unit digests do not cover `meta.sub_plan`, so every pair is still
    current and the composition succeeds; it must not stamp a method review bound to a method no
    reviewer was shown."""
    _submitted_unreviewed(contour)
    _review_every_pair(contour)
    assert compose(contour)["ok"] is True
    code, body, err = _review(contour, "pass")
    assert body.get("ok") is True and _method_reviewed(contour)

    path = Path(contour.plan)
    path.write_text(path.read_text(encoding="utf-8").replace(
        "split the module", "another way of splitting the module"), encoding="utf-8")
    with contour.as_child():
        assert contour.child.submit(contour.plan).get("ok") is True
    record = sub_plan.read(contour.store.load(PARENT), 1, contour.store)
    assert record.method == "another way of splitting the module"
    assert not _method_reviewed(contour) and _blocked(contour)

    _review_every_pair(contour)
    composed = compose(contour)
    assert composed["ok"] is True, composed
    assert not _method_reviewed(contour) and _blocked(contour)
    record = sub_plan.read(contour.store.load(PARENT), 1, contour.store)
    assert [r for r in record.reviews if r["by"] == cli.TOPOLOGICAL_COMPOSITION_REVIEWER] == []


def test_the_users_override_with_a_digest_writes_the_method_review_and_one_without_writes_none(contour):
    """The way on names the user's override with `--plan-digest`. Without the digest nothing says
    which bytes were read, so the override clears the plan-review gate and writes no method
    review, and the way on says so."""
    assert "writes no method review" in gates.METHOD_REVIEW_WAY
    _submitted_unreviewed(contour)

    def override(digest):
        return cli.cmd_plan_review(
            Namespace(session=PARENT, target=contour.plan, scope=None, verdict="override",
                      reviewer="user", concerns=None, note="the user accepts the method",
                      plan_digest=digest, regression_command=None),
            store=contour.store)

    result = override("")
    assert result.ok, result.detail
    assert not _method_reviewed(contour) and _blocked(contour)

    result = override(_sha(contour.plan))
    assert result.ok, result.detail
    assert _method_reviewed(contour) and not _blocked(contour)
    [review] = sub_plan.read(contour.store.load(PARENT), 1, contour.store).reviews
    assert review["verdict"] == "override" and review["by"] == "user"
    assert review["bound_to"] == sub_plan.approval_digest(
        sub_plan.read(contour.store.load(PARENT), 1, contour.store))


def test_a_record_carrying_the_method_review_is_refused_with_the_way_the_reviewer_has(contour):
    """The refusal of a record that carries its own method review names the one way the engine
    writes it, not a verb that does not."""
    text = contour.text()
    carried = text.replace(
        "[meta.sub_plan]",
        "[meta.sub_plan]\nreviews = [{kind = \"method\", verdict = \"pass\", by = \"self\"}]", 1)
    assert carried != text, "the fixture's sub-plan table is the one the replacement names"
    result = contour.submit(carried, reviewed=False)
    assert result.get("ok") is False
    assert gates.METHOD_REVIEW_WAY in str(result)


def test_a_review_the_parent_records_for_its_childs_sub_plan_is_written_into_the_childs_record(contour):
    """The parent session runs `plan-review` over the child's sub-plan file: the record lives in
    the child's contour (the link), and the engine writes the review there."""
    _submitted_unreviewed(contour)
    result = cli.cmd_plan_review(
        Namespace(session=PARENT, target=contour.plan, scope=None, verdict="pass", reviewer="thinker",
                  concerns=None, note="", plan_digest=_sha(contour.plan), regression_command=None),
        store=contour.store)
    assert result.ok, result.detail
    assert _method_reviewed(contour) and not _blocked(contour)


def test_a_sub_plan_rewritten_after_its_review_is_owed_a_new_one(contour):
    """The method review is bound to the record as it stood: a resubmitted sub-plan with another
    method loses it, and the same refusal names the same way."""
    assert contour.submit()["ok"] is True
    assert _method_reviewed(contour) and not _blocked(contour)
    rewritten = contour.text("another way of splitting the module")
    contour.plans += 1
    path = contour.child.write(rewritten, name=f"child-{contour.plans}.toml")
    with contour.as_child():
        assert contour.child.submit(path).get("ok") is True
    assert not _method_reviewed(contour) and _blocked(contour)


@pytest.fixture
def store(home_store):
    return home_store


def test_the_whole_plan_review_of_a_sub_plan_built_in_a_pushed_frame_writes_the_method_review(
        store, venued_fixtures_dir, tmp_path):
    """The frame carrier: the sub-plan is submitted in a pushed frame of the same session; the
    thinker's pass of its file is recorded through `plan-review` and the record held for the
    frame carries the review."""
    assert base._frame_submission(store, venued_fixtures_dir, tmp_path).ok
    state = store.load(PARENT)
    record = sub_plan.SubPlanRecord.from_dict(state.sub_plan_records["0:1"])
    assert not sub_plan.method_review_recorded(record)
    plan = state.plan_path
    result = cli.cmd_plan_review(
        Namespace(session=PARENT, target=plan, scope=None, verdict="pass", reviewer="thinker",
                  concerns=None, note="", plan_digest=_sha(plan), regression_command=None),
        store=store)
    assert result.ok, result.detail
    record = sub_plan.SubPlanRecord.from_dict(store.load(PARENT).sub_plan_records["0:1"])
    assert sub_plan.method_review_recorded(record)
    assert record.reviews[0]["bound_to"] == sub_plan.approval_digest(record)

    again = tmp_path / "frame-again.toml"
    again.write_text(Path(plan).read_text(encoding="utf-8").replace(
        "split the module", "another way of splitting the module"), encoding="utf-8")
    resubmitted = cli.cmd_submit_plan(Namespace(session=PARENT, plan=str(again)), store=store)
    assert resubmitted.ok, resubmitted.detail
    record = sub_plan.SubPlanRecord.from_dict(store.load(PARENT).sub_plan_records["0:1"])
    assert record.method == "another way of splitting the module"
    assert not sub_plan.method_review_recorded(record) and record.reviews == []
