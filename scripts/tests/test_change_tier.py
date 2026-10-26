"""Field classification, change tier and frozen PASSED stages at the replan seam.

One total, fail-closed table classifies every leaf of a stage (and the plan's meta, order
and final_check) as bookkeeping (B), final-venue (F), check-only (C), operative (O),
interface (I) or meta (M); `plan.change_tier` turns two plan versions into a per-stage
and a plan tier from it. A finished (PASSED) stage is changed only by an edit that cannot
change what it delivered (B/F/C); an operative or interface edit to it is refused unless
the replan names it with `--reopen`.

New names (`STAGE_LEAF_CLASS`, `change_tier`, `leaf_class`, the reader
`cli._read_plan_version_snapshot`) are reached inside the test bodies, so a test fails on
a tree that lacks them instead of failing the whole module at collection.
"""
import hashlib
import json
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli
from agentctl.plan import diff_plans, load_plan, stage_carry_key
from agentctl.state import Node, Stage, StageStatus
from conftest import STAGE_OBSERVATIONS
from dataclass_domain import leaf_paths

_STAGE1_LAST_LINE = 'output_artifacts = ["mod.py"]'
_STAGE2_LAST_LINE = 'output_artifacts = ["tests/test_mod.py"]'


@pytest.fixture(autouse=True)
def _no_replan_authorization_gate(monkeypatch):
    """The replan-authorization gate has its own suite (test_replan_authorization.py);
    here an applied non-substantive replan must not wait on a diff presentation."""
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")


def ns(**kw):
    return Namespace(**kw)


def _stage1(text, *lines):
    assert text.count(_STAGE1_LAST_LINE) == 1, "fixture no longer ends stage 1 as expected"
    return text.replace(_STAGE1_LAST_LINE, "\n".join((_STAGE1_LAST_LINE, *lines)))


def _stage2(text, *lines):
    assert text.count(_STAGE2_LAST_LINE) == 1, "fixture no longer ends stage 2 as expected"
    return text.replace(_STAGE2_LAST_LINE, "\n".join((_STAGE2_LAST_LINE, *lines)))


def _plan_file(tmp_path, text, name):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _doc(tmp_path, text, name="doc.toml"):
    return load_plan(_plan_file(tmp_path, text, name))


def _base(fixtures_dir):
    return (fixtures_dir / "plan_two_stage.toml").read_text(encoding="utf-8")


def _to_executing_stage1(store, sid, plan):
    cli.cmd_start(ns(session=sid, task="demo-two-stage", goal="", done_criterion="",
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


def _pass_stage(store, sid, index):
    cli.cmd_record_result(ns(session=sid, status="passed", actual="ok",
                             control="reviewed: ok",
                             observation=STAGE_OBSERVATIONS[index - 1]), store=store)


def _passed_session(store, sid, plan, *, stages=1):
    _to_executing_stage1(store, sid, plan)
    _pass_stage(store, sid, 1)
    if stages == 2:
        cli.cmd_next_stage(ns(session=sid), store=store)
        _pass_stage(store, sid, 2)
    state = store.load(sid)
    assert state.stage(1).outcome.status == StageStatus.PASSED.value
    return state


def _says(directive):
    return f"{directive.detail} {json.dumps(directive.data, default=str)}"


# --- the class table ----------------------------------------------------------------

_BOOKKEEPING = {"actor.cost_tier", "amendments.date", "amendments.text", "amendments.reason"}
_FINAL_VENUE = {"criterion.verify_venue_at_final"}
_CHECK_ONLY = {"criterion.verify_command", "criterion.expected_exit", "criterion.verify_venue",
               "criterion.negative_control", "criterion.negative_control_waiver",
               "criterion.verify_reads"}


def test_stage_field_classification_is_total():
    """Every leaf of Stage is classed, once, and the named classes hold exactly the
    leaves the design names — a new Stage field fails HERE until someone classes it."""
    from agentctl.plan import STAGE_LEAF_CLASS

    assert set(STAGE_LEAF_CLASS) == set(leaf_paths(Stage)), (
        "a Stage leaf is unclassified or a classified leaf is gone: "
        f"{sorted(set(leaf_paths(Stage)) ^ set(STAGE_LEAF_CLASS))}")
    assert set(STAGE_LEAF_CLASS.values()) <= {"B", "F", "C", "O", "I"}
    by_class = {c: {p for p, k in STAGE_LEAF_CLASS.items() if k == c} for c in "BFCOI"}
    assert by_class["B"] == _BOOKKEEPING
    assert by_class["F"] == _FINAL_VENUE
    assert _CHECK_ONLY <= by_class["C"]
    assert {"renorms", "means.method", "actor.capability_required"} <= by_class["O"]
    assert {"output_artifacts", "supplies.on", "actor.executor", "grants.allow.rule",
            "effects.path"} <= by_class["I"]


def test_unclassified_stage_field_is_never_bookkeeping():
    from agentctl.plan import TIER_OF_CLASS, leaf_class

    for unknown in ("a_field_added_tomorrow", "criterion.a_new_leaf", ""):
        assert leaf_class(unknown) == "I"
        assert TIER_OF_CLASS[leaf_class(unknown)] == 3


# --- #74: the cells diff_plans could not see ----------------------------------------

def test_capability_required_edit_is_not_no_change(fixtures_dir, tmp_path):
    base = _base(fixtures_dir)
    old = _doc(tmp_path, _stage1(base, 'capability_required = "a developer"'), "old.toml")
    new = _doc(tmp_path, _stage1(base, 'capability_required = "a reviewer"'), "new.toml")
    assert diff_plans(old, new) != "no_change"


def test_supplies_artifact_edit_is_not_no_change(fixtures_dir, tmp_path):
    base = _base(fixtures_dir)
    old = _doc(tmp_path, _stage2(base, "[[stage.supplies]]", "on = 1",
                                 'artifact = "the module"'), "old.toml")
    new = _doc(tmp_path, _stage2(base, "[[stage.supplies]]", "on = 1",
                                 'artifact = "the module and its tests"'), "new.toml")
    assert diff_plans(old, new) != "no_change"
    old_element = _doc(tmp_path, _stage2(base, "[[stage.supplies]]", "on = 1",
                                         'element = "result"'), "old_e.toml")
    new_element = _doc(tmp_path, _stage2(base, "[[stage.supplies]]", "on = 1",
                                         'element = "method"'), "new_e.toml")
    assert diff_plans(old_element, new_element) != "no_change"


# --- amendments: a retrospective field no change decision reads ---------------------

def test_amendments_edit_moves_no_change_decision_key(fixtures_dir, tmp_path, store):
    from agentctl.plan import change_tier
    from agentctl.stage_norm import StageNorm

    base = _base(fixtures_dir)
    amended = _stage1(base, "[[stage.amendments]]", 'date = "2026-10-17"',
                      'text = "the check also runs the linter"',
                      'reason = "the first run missed a style defect"')
    old = _doc(tmp_path, base, "old.toml")
    new = _doc(tmp_path, amended, "new.toml")

    assert [a.text for a in new.stages[0].amendments] == ["the check also runs the linter"]
    assert diff_plans(old, new) == "no_change"
    assert [stage_carry_key(s) for s in old.stages] == [stage_carry_key(s) for s in new.stages]
    assert (StageNorm.from_stage(old.stages[0], doc=old).carry_digest()
            == StageNorm.from_stage(new.stages[0], doc=new).carry_digest())
    change = change_tier(old, new)
    assert change.tier == 1
    assert change.stages[1].moved == (("amendments.date", "B"), ("amendments.reason", "B"),
                                      ("amendments.text", "B"))


def test_amendments_edit_keeps_review_and_premise_keys(fixtures_dir, tmp_path):
    from agentctl.plan import (_reviewed_stage_keys, pair_content, review_pairs,
                               stage_question_key, unit_currency_hash)

    base = _base(fixtures_dir)
    amended = _stage1(base, "[[stage.amendments]]", 'date = "2026-10-17"',
                      'text = "the check also runs the linter"',
                      'reason = "the first run missed a style defect"')
    old = _doc(tmp_path, base, "old.toml")
    new = _doc(tmp_path, amended, "new.toml")

    assert not old.stages[0].amendments
    assert [a.text for a in new.stages[0].amendments] == ["the check also runs the linter"]
    assert [stage_question_key(s) for s in old.stages] == [stage_question_key(s) for s in new.stages]
    assert [_reviewed_stage_keys(s) for s in old.stages] == [_reviewed_stage_keys(s) for s in new.stages]
    units = ["unit:base"] + [f"unit:{s.index}" for s in old.stages]
    assert [unit_currency_hash(old, u) for u in units] == [unit_currency_hash(new, u) for u in units]
    pairs = review_pairs(old)
    assert pairs and review_pairs(new) == pairs
    assert [pair_content(old, p) for p in pairs] == [pair_content(new, p) for p in pairs]


# --- the tier ------------------------------------------------------------------------

def test_change_tier_assigns_each_class_its_tier(fixtures_dir, tmp_path):
    from agentctl.plan import change_tier

    base_text = _base(fixtures_dir)
    base = _doc(tmp_path, base_text, "base.toml")
    assert change_tier(base, base).tier == 0
    assert change_tier(base, base).stages[1].moved == ()

    def tier_of(*lines, text=None):
        edited = text if text is not None else _stage1(base_text, *lines)
        return change_tier(base, _doc(tmp_path, edited, "edit.toml"))

    bookkeeping = tier_of('cost_tier = "large"')
    assert (bookkeeping.tier, bookkeeping.stages[1].moved) == (1, (("actor.cost_tier", "B"),))

    final_venue = tier_of('verify_venue_at_final = "delivery"')
    assert (final_venue.tier, final_venue.stages[1].moved) == (
        1, (("criterion.verify_venue_at_final", "F"),))

    check = tier_of("expected_exit = 1")
    assert (check.tier, check.stages[1].moved) == (2, (("criterion.expected_exit", "C"),))

    operative = tier_of('method = "build it in two passes"')
    assert (operative.tier, operative.stages[1].moved) == (2, (("means.method", "O"),))

    renorms = tier_of('renorms = ["/tmp/norm-file.md"]')
    assert renorms.stages[1].moved == (("renorms", "O"),)
    assert renorms.tier == 2, "a renorms-only edit changes what the stage builds"

    criterion = tier_of(text=base_text.replace(
        "python -c 'import mod' exits 0", "python -c 'import mod' exits 0 cleanly"))
    assert (criterion.tier, criterion.stages[1].moved) == (2, (("criterion.done_criterion", "O"),))

    interface = tier_of(text=base_text.replace(
        _STAGE1_LAST_LINE, 'output_artifacts = ["mod.py", "extra.py"]'))
    assert (interface.tier, interface.stages[1].moved) == (3, (("output_artifacts", "I"),))

    meta = tier_of(text=base_text.replace("Demonstrate the full", "Demonstrate the whole"))
    assert meta.tier == 4 and meta.meta == ("goal",)

    mixed = tier_of(text=_stage2(_stage1(base_text, 'cost_tier = "large"'),
                                 'method = "write the tests first"'))
    assert mixed.tier == 2
    assert mixed.stages[1].tier == 1 and mixed.stages[2].tier == 2, "the plan's tier is the maximum"

    reshaped = tier_of(text=base_text.split("[[stage]]\nindex = 2")[0])
    assert reshaped.tier == 4, "a changed stage set is a meta change"


def test_change_tier_matches_diff_plans_kind_outside_the_74_cells(fixtures_dir, tmp_path):
    """The tier never replaces the approval kind: wherever the two are comparable they agree
    on the extremes — an identical plan is tier 0 and no_change, a substantive kind is never
    below tier 2."""
    from agentctl.plan import change_tier

    names = sorted(p.name for p in fixtures_dir.glob("plan_two_stage*.toml"))
    docs = {n: load_plan(str(fixtures_dir / n)) for n in names}
    for old_name, old in docs.items():
        for new_name, new in docs.items():
            tier = change_tier(old, new).tier
            kind = diff_plans(old, new)
            where = f"{old_name} -> {new_name}"
            assert (tier == 0) <= (kind == "no_change"), where
            assert (kind == "substantive") <= (tier >= 2), where

    # The kinds as the base returns them (none of these pairs touches a #74 cell), from
    # the plain plan to every fixture and back for the neighbours of the new cells.
    base = docs["plan_two_stage.toml"]
    kinds_from_base = {
        "plan_two_stage.toml": "no_change",
        "plan_two_stage_donecriterion_changed.toml": "substantive",
        "plan_two_stage_finalcheck.toml": "refinement",
        "plan_two_stage_finalcheck_changed.toml": "refinement",
        "plan_two_stage_means.toml": "refinement",
        "plan_two_stage_means_changed.toml": "refinement",
        "plan_two_stage_refined.toml": "refinement",
        "plan_two_stage_substantive.toml": "substantive",
        "plan_two_stage_substantive_stage1_method_changed.toml": "substantive",
        "plan_two_stage_substantive_stage1_retitled.toml": "substantive",
        "plan_two_stage_substantive_stage1and2_method_changed.toml": "substantive",
        "plan_two_stage_substantive_stage1and2_retitled.toml": "substantive",
        "plan_two_stage_verifyfix.toml": "refinement",
        "plan_two_stage_verifyfix_changed.toml": "refinement",
        "plan_two_stage_verifyfix_grantgrowth.toml": "substantive",
        "plan_two_stage_verifyfix_grantgrowth_changed.toml": "substantive",
        "plan_two_stage_verifyfix_reporoot.toml": "refinement",
    }
    assert set(kinds_from_base) == set(names), "a new fixture needs an expected kind here"
    assert {n: diff_plans(base, docs[n]) for n in names} == kinds_from_base
    assert diff_plans(docs["plan_two_stage_substantive.toml"],
                      docs["plan_two_stage_substantive_stage1_method_changed.toml"]) == "refinement"
    assert diff_plans(docs["plan_two_stage_verifyfix.toml"],
                      docs["plan_two_stage_verifyfix_changed.toml"]) == "refinement"
    assert diff_plans(docs["plan_two_stage_verifyfix_grantgrowth.toml"],
                      docs["plan_two_stage_verifyfix_grantgrowth_changed.toml"]) == "substantive"


# --- the snapshot the tier's old side is read from ------------------------------------

def test_submit_and_applied_replan_write_a_content_addressed_snapshot(
        store, venued_fixtures_dir):
    sid = "snap1"
    plan = venued_fixtures_dir / "plan_two_stage.toml"
    refined = venued_fixtures_dir / "plan_two_stage_refined.toml"
    _to_executing_stage1(store, sid, str(plan))
    state_dir = store.path(sid).parent
    first = state_dir / f"plan-version-{hashlib.sha256(plan.read_bytes()).hexdigest()[:16]}.toml"
    assert first.read_bytes() == plan.read_bytes()

    cli.cmd_replan(ns(session=sid, plan=str(refined)), store=store)
    second = state_dir / f"plan-version-{hashlib.sha256(refined.read_bytes()).hexdigest()[:16]}.toml"
    assert second.read_bytes() == refined.read_bytes()


def test_plan_version_snapshot_reader_returns_bytes_by_digest(store, venued_fixtures_dir):
    sid = "snap2"
    plan = venued_fixtures_dir / "plan_two_stage.toml"
    _to_executing_stage1(store, sid, str(plan))
    state = store.load(sid)
    digest = hashlib.sha256(plan.read_bytes()).hexdigest()

    assert cli._read_plan_version_snapshot(store, state, digest) == plan.read_bytes()
    assert cli._read_plan_version_snapshot(store, state, "0" * 64) is None


# --- PASSED stages at the applied-replan seam -----------------------------------------

def test_operative_edit_on_passed_stage_is_refused_without_reopen(
        store, venued_fixtures_dir, tmp_path):
    sid = "frozen1"
    plan = venued_fixtures_dir / "plan_two_stage.toml"
    _passed_session(store, sid, str(plan))
    edited = _plan_file(tmp_path, _stage1(plan.read_text(), 'method = "build it in two passes"'),
                        "edited.toml")

    d = cli.cmd_replan(ns(session=sid, plan=edited), store=store)

    assert not d.ok
    assert "--reopen" in _says(d) and "stage 1" in _says(d)
    state = store.load(sid)
    assert state.stage(1).outcome.status == StageStatus.PASSED.value
    assert state.stage(1).means.method != "build it in two passes", "nothing was applied"
    assert state.plan_path == str(plan)


def test_reopen_rearms_the_stage_and_its_passed_dependents(
        store, venued_fixtures_dir, tmp_path):
    sid = "frozen2"
    plan = venued_fixtures_dir / "plan_two_stage.toml"
    _passed_session(store, sid, str(plan), stages=2)
    edited = _plan_file(tmp_path, _stage1(plan.read_text(), 'method = "build it in two passes"'),
                        "edited.toml")

    d = cli.cmd_replan(ns(session=sid, plan=edited, reopen=[1]), store=store)

    assert d.ok, _says(d)
    assert d.data["reopened_stages"] == [1, 2]
    state = store.load(sid)
    assert [state.stage(i).outcome.status for i in (1, 2)] == [StageStatus.PENDING.value] * 2
    assert state.stage(1).means.method == "build it in two passes"


def test_verify_venue_at_final_edit_keeps_a_passed_stage_passed(
        store, venued_fixtures_dir, tmp_path):
    """#305: only the venue verify-final re-runs the check in moved, so the delivered
    work stands — even when the same replan is substantive for another reason."""
    sid = "frozen3"
    plan = venued_fixtures_dir / "plan_two_stage.toml"
    _passed_session(store, sid, str(plan))
    changed = (venued_fixtures_dir / "plan_two_stage_donecriterion_changed.toml").read_text()
    edited = _plan_file(tmp_path, _stage1(changed, 'verify_venue_at_final = "delivery"'),
                        "edited.toml")

    d = cli.cmd_replan(ns(session=sid, plan=edited), store=store)

    assert d.marker == "PLAN-READY", _says(d)
    state = store.load(sid)
    assert state.stage(1).outcome.status == StageStatus.PASSED.value
    assert state.stage(1).criterion.verify_venue_at_final == "delivery"


_FROZEN_STAGE_EDITS = {
    "operative": lambda text: _stage1(text, 'method = "build it in two passes"'),
    "interface": lambda text: text.replace(_STAGE1_LAST_LINE,
                                           'output_artifacts = ["mod.py", "extra.py"]'),
}


@pytest.mark.parametrize("leaf_class", sorted(_FROZEN_STAGE_EDITS))
def test_frozen_stage_is_refused_without_reopen_on_a_substantive_replan_too(
        leaf_class, store, venued_fixtures_dir, tmp_path):
    """The freeze does not depend on the replan's kind: a replan that is substantive for
    another reason (the overall done criterion moved) still names --reopen for a PASSED
    stage whose operative or interface leaf it edits, and applies it once reopened."""
    sid = f"frozen-substantive-{leaf_class}"
    plan = venued_fixtures_dir / "plan_two_stage.toml"
    _passed_session(store, sid, str(plan))
    changed = (venued_fixtures_dir / "plan_two_stage_donecriterion_changed.toml").read_text()
    edited = _plan_file(tmp_path, _FROZEN_STAGE_EDITS[leaf_class](changed), "edited.toml")

    refused = cli.cmd_replan(ns(session=sid, plan=edited), store=store)

    assert not refused.ok and "--reopen" in _says(refused) and "stage 1" in _says(refused)
    state = store.load(sid)
    assert state.stage(1).outcome.status == StageStatus.PASSED.value
    assert state.plan_path == str(plan), "nothing was applied"

    reopened = cli.cmd_replan(ns(session=sid, plan=edited, reopen=[1]), store=store)

    assert reopened.ok, _says(reopened)
    assert reopened.data["reopened_stages"] == [1]
    assert store.load(sid).stage(1).outcome.status == StageStatus.PENDING.value


def test_frozen_stage_refusal_is_logged_as_a_gate(
        store, venued_fixtures_dir, tmp_path, monkeypatch):
    log = tmp_path / "gate-log.jsonl"
    monkeypatch.setattr(cli, "GATE_LOG", log)
    sid = "frozen-log"
    plan = venued_fixtures_dir / "plan_two_stage.toml"
    _passed_session(store, sid, str(plan))
    edited = _plan_file(tmp_path, _stage1(plan.read_text(), 'method = "build it in two passes"'),
                        "edited.toml")

    assert not cli.cmd_replan(ns(session=sid, plan=edited), store=store).ok

    rows = [json.loads(line) for line in log.read_text().splitlines()]
    frozen = [r for r in rows if r["gate"] == "frozen_stage"]
    assert len(frozen) == 1 and frozen[0]["passed"] is False
    assert "stage 1" in " ".join(frozen[0]["blockers"])


def test_reopen_naming_a_stage_that_is_not_passed_is_disclosed(
        store, venued_fixtures_dir, tmp_path):
    sid = "reopen-ignored"
    plan = venued_fixtures_dir / "plan_two_stage.toml"
    _passed_session(store, sid, str(plan))
    edited = _plan_file(tmp_path, _stage1(plan.read_text(), "expected_exit = 1"), "edited.toml")

    d = cli.cmd_replan(ns(session=sid, plan=edited, reopen=[2]), store=store)

    assert d.ok, _says(d)
    assert d.data["reopen_ignored"] == [2] and "reopened_stages" not in d.data
    assert "ignored" in d.detail
    assert store.load(sid).stage(1).outcome.status == StageStatus.PASSED.value

    unknown = cli.cmd_replan(ns(session=sid, plan=edited, reopen=[9]), store=store)

    assert not unknown.ok and "no such stage" in _says(unknown)


def test_check_only_edit_on_passed_stage_keeps_it_passed(store, venued_fixtures_dir, tmp_path):
    sid = "frozen4"
    plan = venued_fixtures_dir / "plan_two_stage.toml"
    _passed_session(store, sid, str(plan))
    edited = _plan_file(tmp_path, _stage1(plan.read_text(), "expected_exit = 1"), "edited.toml")

    d = cli.cmd_replan(ns(session=sid, plan=edited), store=store)

    assert d.ok, _says(d)
    state = store.load(sid)
    assert state.stage(1).outcome.status == StageStatus.PASSED.value
    assert state.stage(1).criterion.expected_exit == 1


def test_operative_edit_on_pending_stage_is_applied_as_today(
        store, venued_fixtures_dir, tmp_path):
    sid = "pending1"
    plan = venued_fixtures_dir / "plan_two_stage.toml"
    _to_executing_stage1(store, sid, str(plan))
    edited = _plan_file(tmp_path, _stage1(plan.read_text(), 'method = "build it in two passes"'),
                        "edited.toml")

    d = cli.cmd_replan(ns(session=sid, plan=edited), store=store)

    assert d.ok and d.action == "continue", _says(d)
    assert store.load(sid).stage(1).means.method == "build it in two passes"


def test_without_a_snapshot_routing_is_unchanged(store, venued_fixtures_dir, tmp_path):
    """No approved-plan snapshot means no known old side: the tier is unknown and the
    replan applies as on the base, even for an operative edit to a PASSED stage."""
    sid = "nosnap"
    plan = venued_fixtures_dir / "plan_two_stage.toml"
    state = _passed_session(store, sid, str(plan))
    state.plan_snapshot_path = None
    store.save(state)
    edited = _plan_file(tmp_path, _stage1(plan.read_text(), 'method = "build it in two passes"'),
                        "edited.toml")

    d = cli.cmd_replan(ns(session=sid, plan=edited), store=store)

    assert d.ok, _says(d)
    state = store.load(sid)
    assert state.stage(1).outcome.status == StageStatus.PASSED.value
    assert state.stage(1).means.method == "build it in two passes"


def test_dispatch_never_moves_a_carry_key_field(store, venued_fixtures_dir):
    """#48, verify-only: dispatching a stage leaves every stage's carry key as the plan
    declared it (the companion `test_replan.py::test_refresh_covers_every_carry_key_field`
    pins the other half)."""
    from agentctl.dispatch import RunResult

    sid = "dispatch48"
    plan = venued_fixtures_dir / "plan_two_stage.toml"
    _to_executing_stage1(store, sid, str(plan))
    before = [stage_carry_key(s) for s in store.load(sid).stages]
    cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
                        dry_run=False, constraints=""), store=store,
                     runner=lambda argv, **kw: RunResult(0, stdout="COMPLETED: done\n"))
    assert [stage_carry_key(s) for s in store.load(sid).stages] == before


def test_change_tier_of_a_split_plan_is_decided_on_the_resolved_plan(
        store, venued_fixtures_dir, tmp_path):
    """The tier and the frozen-stage check read the plan with its linked files resolved:
    a field held in a file moves the tier exactly as the same edit inline does, an
    untouched split plan is tier 0, and the PASSED stage's operative edit is refused."""
    from agentctl import plan_refs
    from agentctl.plan import change_tier

    inline_text = (venued_fixtures_dir / "plan_two_stage.toml").read_text(encoding="utf-8")
    index = tmp_path / "split" / "plan.toml"
    index.parent.mkdir()
    index.write_text(inline_text, encoding="utf-8")
    field_file = Path(plan_refs.plan_externalize(index, stage=1, field="done_criterion")["field_file"])

    sid = "split1"
    _passed_session(store, sid, str(index))
    baseline = cli._baseline_plan_for_tier(store, store.load(sid))
    assert baseline is not None, "the snapshot of a split plan resolves its linked files"
    assert change_tier(baseline, load_plan(str(index))).tier == 0

    field_file.write_text(field_file.read_text(encoding="utf-8") + " cleanly", encoding="utf-8")
    plan_refs.seal(index)
    split_tier = change_tier(baseline, load_plan(str(index)))
    inline_tier = change_tier(
        _doc(tmp_path, inline_text, "inline-base.toml"),
        _doc(tmp_path, inline_text.replace(
            "python -c 'import mod' exits 0", "python -c 'import mod' exits 0 cleanly"),
            "inline-edit.toml"))
    assert (split_tier.tier, split_tier.stages[1].moved) == (
        inline_tier.tier, inline_tier.stages[1].moved) == (2, (("criterion.done_criterion", "O"),))

    d = cli.cmd_replan(ns(session=sid, plan=str(index)), store=store)

    assert not d.ok and "--reopen" in _says(d) and "stage 1" in _says(d), _says(d)
    assert store.load(sid).stage(1).outcome.status == StageStatus.PASSED.value
