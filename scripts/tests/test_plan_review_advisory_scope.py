"""Stage 3 (issue #266): a stage-scoped review's out-of-scope concerns are
advisory, not blocking. `classify_concerns` (gates.py) partitions a review's
concerns by whether their leading structural part token (`stage:<n>`, `meta:`,
`order:`) matches the review's own scope; a whole-plan scope treats everything
as in-scope. `PlanReview` persists the split (`in_scope_concern_ids` /
`out_of_scope_concern_ids`, schema 38); `_plan_review_verdict_blockers` blocks a
`revise` only on its in-scope concerns, preserving legacy (unclassified) records'
all-blocking behaviour. `gates.review_delta` is the shared helper two call sites
(`plugins_review_dispatch._obs_submit_plan`, `cmd_replan`'s non-round-release
refusal) now use instead of hardcoded whole-plan prose; `plan-render --stage`
takes a single int or a CSV list.

Group 1 locks classify_concerns directly. Group 2 locks the PlanReview schema
default/legacy-load behaviour. Group 3 locks _plan_review_verdict_blockers via
the public gates.plan_review_blockers entry point (the
test_plan_review_accepted_risk.py end-to-end pattern: a whole-plan pass bound to
the ORIGINAL fixture plus a stage-scoped review bound to the RETITLED fixture,
composed in one SessionState). Group 4 locks gates.review_delta. Group 5 locks
the two review_delta call sites. Group 6 locks plan-render's --stage CSV form."""
from __future__ import annotations

import hashlib
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, gates, plugins
from agentctl import plugins_review_dispatch as prd
from agentctl.directive import Directive
from agentctl.plan import load_plan, plan_meta_digest, plan_stage_digests
from agentctl.render import cmd_plan_render
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
    kw.setdefault("concerns", [])
    kw.setdefault("concern_ids", [])
    return PlanReview(
        plan_path=str(plan_path), scope="",
        plan_sha256=_sha256_file(plan_path),
        reviewed_meta_digest=plan_meta_digest(doc),
        reviewed_stage_keys={str(k): v for k, v in plan_stage_digests(doc).items()},
        **kw,
    )


def _stage_review(plan_path, doc, index, **kw) -> PlanReview:
    kw.setdefault("verdict", "revise")
    kw.setdefault("reviewer", "thinker")
    return PlanReview(
        plan_path=str(plan_path), scope=f"stage:{index}",
        plan_sha256=_sha256_file(plan_path),
        reviewed_meta_digest=plan_meta_digest(doc),
        reviewed_stage_keys={str(k): v for k, v in plan_stage_digests(doc).items()},
        **kw,
    )


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")


# --- 1. classify_concerns --------------------------------------------------

def test_classify_concerns_whole_plan_scope_everything_in_scope():
    ids = ["c1", "c2"]
    concerns = ["stage:2: needs work", "untagged worry"]
    in_scope, out_of_scope = gates.classify_concerns("", ids, concerns)
    assert in_scope == ids
    assert out_of_scope == []


@pytest.mark.parametrize("concern,expect_in_scope", [
    ("stage:2: this belongs to the other stage", False),
    ("meta: the done_criterion is unreachable", False),
    ("order: this order element is uncovered", False),
    ("stage:1: this is about my own stage", True),
    ("untagged worry with no leading token", True),
    ("Risk: an unrecognized word-colon prefix", True),
    ("Note: another unrecognized word-colon prefix", True),
])
def test_classify_concerns_stage_scope_partitions_by_leading_token(concern, expect_in_scope):
    in_scope, out_of_scope = gates.classify_concerns("stage:1", ["c1"], [concern])
    if expect_in_scope:
        assert in_scope == ["c1"] and out_of_scope == []
    else:
        assert in_scope == [] and out_of_scope == ["c1"]


@pytest.mark.parametrize("scope,concern_token", [
    ("stage:01", "stage:1"),
    ("stage:1", "stage:01"),
])
def test_classify_concerns_stage_token_padding_reciprocal(scope, concern_token):
    """A zero-padded stage token (`stage:01`) and its bare form (`stage:1`) name
    the SAME stage index, in either direction — `classify_concerns` must compare
    the parsed index, not the raw token string, or a reviewer's own padding
    choice would silently reclassify its concern as out-of-scope."""
    in_scope, out_of_scope = gates.classify_concerns(scope, ["c1"], [f"{concern_token}: about my own stage"])
    assert in_scope == ["c1"] and out_of_scope == []


# --- 2. PlanReview schema: default-empty + legacy load ---------------------

def test_planreview_new_fields_default_empty():
    pr = PlanReview("/plan.toml", "revise", "thinker")
    assert pr.in_scope_concern_ids == []
    assert pr.out_of_scope_concern_ids == []


def test_legacy_plan_review_dict_without_new_fields_loads():
    legacy = {"plan_path": "/plan.toml", "verdict": "revise", "reviewer": "thinker",
              "concerns": ["something"], "concern_ids": ["c1"]}
    pr = PlanReview.from_dict(legacy)
    assert pr.in_scope_concern_ids == []
    assert pr.out_of_scope_concern_ids == []


# --- 3. _plan_review_verdict_blockers via gates.plan_review_blockers -------
# Same pattern as test_plan_review_accepted_risk.py: a whole-plan PASS bound to
# the ORIGINAL fixture, a stage-scoped REVISE bound to the RETITLED fixture,
# composed in one SessionState so _plan_review_blockers_coverage's whole-plan
# precondition is met and the per-stage branch (which calls
# _plan_review_verdict_blockers) is reached.

def test_out_of_scope_revise_does_not_block(gate_on, tmp_path, fixtures_dir):
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    doc0 = load_plan(str(plan_path))
    whole = _whole_review(plan_path, doc0)

    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive_stage1_retitled.toml").read_text())
    doc1 = load_plan(str(plan_path))
    concerns = ["stage:2: this belongs to the other stage", "meta: unrelated worry"]
    ids = ["c1", "c2"]
    in_scope, out_of_scope = gates.classify_concerns("stage:1", ids, concerns)
    stage1 = _stage_review(plan_path, doc1, 1, concerns=concerns, concern_ids=ids,
                           in_scope_concern_ids=in_scope, out_of_scope_concern_ids=out_of_scope)

    s = _subst(plan_path=str(plan_path), plan_review=whole,
               plan_stage_reviews={"stage:1": stage1})
    assert out_of_scope == ids
    assert gates.plan_review_blockers(s, str(plan_path)) == []


def test_stage_scoped_revise_with_one_in_scope_concern_blocks(gate_on, tmp_path, fixtures_dir):
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    doc0 = load_plan(str(plan_path))
    whole = _whole_review(plan_path, doc0)

    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive_stage1_retitled.toml").read_text())
    doc1 = load_plan(str(plan_path))
    concerns = ["stage:2: out of scope", "the retitle hides a scope change"]
    ids = ["c1", "c2"]
    in_scope, out_of_scope = gates.classify_concerns("stage:1", ids, concerns)
    stage1 = _stage_review(plan_path, doc1, 1, concerns=concerns, concern_ids=ids,
                           in_scope_concern_ids=in_scope, out_of_scope_concern_ids=out_of_scope)

    s = _subst(plan_path=str(plan_path), plan_review=whole,
               plan_stage_reviews={"stage:1": stage1})
    assert in_scope == ["c2"] and out_of_scope == ["c1"]
    blockers = gates.plan_review_blockers(s, str(plan_path))
    assert blockers and "revise" in blockers[0]


def test_legacy_unclassified_revise_still_blocks_on_every_concern(gate_on, tmp_path, fixtures_dir):
    """A PlanReview predating R3 has BOTH new lists empty even though `concerns`
    is non-empty — the `classified` flag in _plan_review_verdict_blockers must
    read that as "not yet classified" and fall back to blocking on every
    concern id, exactly as pre-R3 code did."""
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    doc0 = load_plan(str(plan_path))
    whole = _whole_review(plan_path, doc0)

    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive_stage1_retitled.toml").read_text())
    doc1 = load_plan(str(plan_path))
    stage1 = _stage_review(plan_path, doc1, 1,
                           concerns=["stage:2: would be out of scope if classified"],
                           concern_ids=["c1"])  # in_scope/out_of_scope left at their [] default

    s = _subst(plan_path=str(plan_path), plan_review=whole,
               plan_stage_reviews={"stage:1": stage1})
    blockers = gates.plan_review_blockers(s, str(plan_path))
    assert blockers and "revise" in blockers[0]


def test_whole_plan_scope_revise_blocks_on_any_concern(gate_on, tmp_path, fixtures_dir):
    """A whole-plan (scope="") revise is unaffected by the advisory-scope split:
    classify_concerns treats everything as in-scope for scope="", so a
    whole-plan revise with a stage-tagged concern still blocks."""
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    doc0 = load_plan(str(plan_path))
    ids = ["c1"]
    concerns = ["stage:2: something to fix"]
    in_scope, out_of_scope = gates.classify_concerns("", ids, concerns)
    whole = PlanReview(
        plan_path=str(plan_path), verdict="revise", reviewer="thinker",
        scope="", concerns=concerns, concern_ids=ids,
        in_scope_concern_ids=in_scope, out_of_scope_concern_ids=out_of_scope,
        plan_sha256=_sha256_file(plan_path),
        reviewed_meta_digest=plan_meta_digest(doc0),
        reviewed_stage_keys={str(k): v for k, v in plan_stage_digests(doc0).items()},
    )
    s = _subst(plan_path=str(plan_path), plan_review=whole)
    blockers = gates.plan_review_blockers(s, str(plan_path))
    assert blockers and "revise" in blockers[0]


def test_out_of_scope_revise_recorded_via_cli_reports_not_blocking_note(store, fixtures_dir, gate_on):
    """End-to-end through cmd_plan_review: a stage-scoped revise whose only
    concern is out-of-scope both stores the classification on the record AND
    surfaces the 'out-of-scope findings recorded, not blocking' phrasing the
    success Directive adds. The whole-plan review here is an OVERRIDE rather
    than a pass, so it never lands in state.plan_review_passes (only a PASS
    does) and the stage-scoped revise that follows is NOT a post-pass revise —
    reaching cmd_plan_review's normal record-and-clear path instead of the
    terminal-pass note path (see gates.plan_review_prior_pass)."""
    sid = "oos-cli"
    plan = fixtures_dir / "plan_two_stage_substantive.toml"
    _to_plan_ready(store, sid, str(plan))
    cli.cmd_plan_review(ns(session=sid, target=None, scope=None, verdict="override",
                           reviewer="fedor", concerns=None, note="whole-plan escape",
                           plan_digest=None), store=store)
    d = cli.cmd_plan_review(ns(session=sid, target=None, scope="stage:1", verdict="revise",
                               reviewer="thinker", concerns=["stage:2: not my stage"],
                               note="", plan_digest=None), store=store)
    assert d.ok is True
    assert "out-of-scope findings recorded, not blocking" in d.detail
    stage1 = store.load(sid).plan_stage_reviews["stage:1"]
    assert stage1.out_of_scope_concern_ids == ["c0"]
    assert stage1.in_scope_concern_ids == []
    assert gates.plan_review_blockers(store.load(sid), str(plan)) == []


def test_regression_evidence_ignores_out_of_scope_concern(gate_on, tmp_path, fixtures_dir):
    """A stage-scoped review's regression command is presumed to exercise only
    its own scope -- an OUT-OF-SCOPE concern naming a part that DID change
    (stage:2, changed since the pass) must not count as evidence for a
    stage:1-scoped review, even though plan.changed_parts confirms stage:2
    moved. This is what distinguishes the R3 scope filter
    (gates._plan_review_regression_evidence) from the bare R1 changed-parts
    check: without the filter this would wrongly return True."""
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    doc0 = load_plan(str(plan_path))
    whole = _whole_review(plan_path, doc0)

    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive_stage1and2_retitled.toml").read_text())
    doc1 = load_plan(str(plan_path))
    concerns = ["stage:2: this belongs to the other stage"]
    ids = ["c1"]
    in_scope, out_of_scope = gates.classify_concerns("stage:1", ids, concerns)
    assert in_scope == [] and out_of_scope == ["c1"]
    stage1 = _stage_review(plan_path, doc1, 1, concerns=concerns, concern_ids=ids,
                           in_scope_concern_ids=in_scope, out_of_scope_concern_ids=out_of_scope,
                           regression_command="pytest -q", regression_exit=1)

    assert gates._plan_review_regression_evidence(whole, stage1, doc1) is False


# --- 4. gates.review_delta ---------------------------------------------------

def test_review_delta_helper(gate_on, tmp_path, fixtures_dir):
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    doc0 = load_plan(str(plan_path))
    whole = _whole_review(plan_path, doc0)

    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive_stage1_retitled.toml").read_text())
    doc1 = load_plan(str(plan_path))
    s = _subst(plan_path=str(plan_path), plan_review=whole)

    delta = gates.review_delta(s, doc1, str(plan_path))
    assert delta == {
        "whole_plan": False,
        "stages": [1],
        "scopes": ["stage:1"],
        "render_command": f"agentctl plan-render --plan {plan_path} --stage 1",
        "record_scope_args": ["--scope stage:1"],
    }


def test_review_delta_helper_whole_plan_when_nothing_reviewed(gate_on, tmp_path, fixtures_dir):
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    doc = load_plan(str(plan_path))
    s = _subst(plan_path=str(plan_path))
    delta = gates.review_delta(s, doc, str(plan_path))
    assert delta["whole_plan"] is True
    assert delta["stages"] == []
    assert delta["scopes"] == []
    assert delta["render_command"] == f"agentctl plan-render --plan {plan_path}"
    assert delta["record_scope_args"] == []


def test_review_delta_helper_unloadable_doc_falls_back_to_whole_plan(gate_on, tmp_path, fixtures_dir):
    """`doc=None` (an unloadable target plan) must not raise -- both call sites
    used to hand-build this fallback dict themselves; now review_delta produces
    it internally so neither call site needs its own doc=None branch."""
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    s = _subst(plan_path=str(plan_path))
    delta = gates.review_delta(s, None, str(plan_path))
    assert delta == {
        "whole_plan": True,
        "stages": [],
        "scopes": [],
        "render_command": f"agentctl plan-render --plan {plan_path}",
        "record_scope_args": [],
    }


# --- 5. the two review_delta call sites -------------------------------------

def test_obs_submit_plan_data_carries_scoped_delta(gate_on, tmp_path, fixtures_dir):
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    doc0 = load_plan(str(plan_path))
    whole = _whole_review(plan_path, doc0)

    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive_stage1_retitled.toml").read_text())
    state = _subst(plan_path=str(plan_path), plan_review=whole,
                   node=Node.PLAN_READY.value)
    plugins.activate(state, "review_dispatch")
    directive = Directive(True, state.node, "noop")
    fired = plugins.fire("submit_plan", state, directive)
    matches = [p for p in fired if p["plugin"] == "review_dispatch"
               and p["action"] == "spawn_thinker_review"]
    assert len(matches) == 1
    data = matches[0]["data"]
    assert data["whole_plan"] is False
    assert data["stages"] == [1]
    assert data["scopes"] == ["stage:1"]
    assert data["render_command"] == f"agentctl plan-render --plan {plan_path} --stage 1"
    assert "--scope stage:1" in matches[0]["detail"]


def test_obs_submit_plan_data_carries_multi_stage_scoped_delta(gate_on, tmp_path, fixtures_dir):
    """When MULTIPLE stages moved, the detail must name one `--scope stage:<n>`
    record command per stage, not a single instruction that (by omitting
    --scope, or naming only one stage) would imply whole-plan or single-stage
    coverage."""
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    doc0 = load_plan(str(plan_path))
    whole = _whole_review(plan_path, doc0)

    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive_stage1and2_retitled.toml").read_text())
    state = _subst(plan_path=str(plan_path), plan_review=whole,
                   node=Node.PLAN_READY.value)
    plugins.activate(state, "review_dispatch")
    directive = Directive(True, state.node, "noop")
    fired = plugins.fire("submit_plan", state, directive)
    matches = [p for p in fired if p["plugin"] == "review_dispatch"
               and p["action"] == "spawn_thinker_review"]
    assert len(matches) == 1
    data = matches[0]["data"]
    assert data["whole_plan"] is False
    assert data["stages"] == [1, 2]
    assert data["scopes"] == ["stage:1", "stage:2"]
    detail = matches[0]["detail"]
    assert "--scope stage:1" in detail
    assert "--scope stage:2" in detail


def test_post_approval_replan_refusal_carries_delta(store, fixtures_dir, tmp_path, gate_on):
    sid = "replan-delta"
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    _to_plan_ready(store, sid, str(plan_path))
    cli.cmd_plan_review(ns(session=sid, target=None, scope=None, verdict="pass",
                           reviewer="thinker", concerns=None, note="",
                           plan_digest=_sha256_file(plan_path)), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)

    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive_stage1_retitled.toml").read_text())
    d = cli.cmd_replan(ns(session=sid, plan=str(plan_path)), store=store)
    assert d.ok is False
    delta = d.data["review_delta"]
    assert delta["whole_plan"] is False
    assert delta["stages"] == [1]
    assert delta["scopes"] == ["stage:1"]
    assert delta["render_command"] == f"agentctl plan-render --plan {plan_path} --stage 1"
    assert "--scope stage:1" in d.detail
    assert delta["render_command"] in d.detail


def test_post_approval_replan_refusal_carries_multi_stage_delta(store, fixtures_dir, tmp_path, gate_on):
    """When MULTIPLE stages moved, the refusal message must name one
    `plan-review ... --scope stage:<n>` command per stage, not a single
    whole-plan-implying instruction."""
    sid = "replan-multi-delta"
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    _to_plan_ready(store, sid, str(plan_path))
    cli.cmd_plan_review(ns(session=sid, target=None, scope=None, verdict="pass",
                           reviewer="thinker", concerns=None, note="",
                           plan_digest=_sha256_file(plan_path)), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)

    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive_stage1and2_retitled.toml").read_text())
    d = cli.cmd_replan(ns(session=sid, plan=str(plan_path)), store=store)
    assert d.ok is False
    delta = d.data["review_delta"]
    assert delta["whole_plan"] is False
    assert delta["stages"] == [1, 2]
    assert delta["scopes"] == ["stage:1", "stage:2"]
    assert "plan-review --target " + str(plan_path) + " --scope stage:1" in d.detail
    assert "plan-review --target " + str(plan_path) + " --scope stage:2" in d.detail


# --- 6. plan-render --stage accepts a single int or a CSV list -------------

def test_parse_stage_arg_accepts_bare_int():
    from agentctl.render import _parse_stage_arg

    assert _parse_stage_arg(3) == [3]


def test_parse_stage_arg_accepts_single_str():
    from agentctl.render import _parse_stage_arg

    assert _parse_stage_arg("3") == [3]


def test_parse_stage_arg_accepts_csv_str():
    from agentctl.render import _parse_stage_arg

    assert _parse_stage_arg("3,1") == [1, 3]


def test_parse_stage_arg_none_stays_none():
    from agentctl.render import _parse_stage_arg

    assert _parse_stage_arg(None) is None


@pytest.mark.parametrize("raw", ["abc", "", "1,abc", "1,"])
def test_plan_render_stage_arg_type_rejects_malformed(raw):
    import argparse

    from agentctl.render import plan_render_stage_arg_type

    with pytest.raises(argparse.ArgumentTypeError):
        plan_render_stage_arg_type(raw)


@pytest.mark.parametrize("raw", ["3", "3,5"])
def test_plan_render_stage_arg_type_accepts_wellformed(raw):
    from agentctl.render import plan_render_stage_arg_type

    assert plan_render_stage_arg_type(raw) == raw


def test_plan_render_csv_stage_renders_both_headings(fixtures_dir):
    plan_path = fixtures_dir / "plan_two_stage_substantive.toml"
    directive = cmd_plan_render(ns(plan=str(plan_path), stage="1,2"))
    assert directive.ok
    md = directive.data["markdown"]
    assert "Scaffold module" in md
    assert "Add tests" in md
    assert "Wire CI" not in md


def test_plan_render_single_int_stage_byte_identical_to_pre_csv_shape(fixtures_dir):
    """The CSV form must not change the single-stage rendering shape at all --
    assert directly against render_stage_brief, the pre-CSV function itself,
    not just against another cmd_plan_render call."""
    from agentctl.render import render_stage_brief

    plan_path = fixtures_dir / "plan_two_stage_substantive.toml"
    doc = load_plan(str(plan_path))
    single = cmd_plan_render(ns(plan=str(plan_path), stage=1))
    via_str = cmd_plan_render(ns(plan=str(plan_path), stage="1"))
    assert single.ok and via_str.ok
    assert single.data["markdown"] == render_stage_brief(doc, 1)
    assert via_str.data["markdown"] == render_stage_brief(doc, 1)


def test_plan_render_without_stage_is_unchanged_whole_plan_view(fixtures_dir):
    """No --stage must not change the whole-plan rendering shape at all --
    assert directly against render_plan_md, the pre-CSV function itself."""
    from agentctl.render import render_plan_md

    plan_path = fixtures_dir / "plan_two_stage_substantive.toml"
    doc = load_plan(str(plan_path))
    directive = cmd_plan_render(ns(plan=str(plan_path), stage=None))
    assert directive.ok
    assert directive.data["markdown"] == render_plan_md(doc)
