"""Stage 6: discharging a `revise` PlanReview concern via a recorded, attributed
risk acceptance (schema 28/29) — the alternative to editing the plan to make the
concern go away. `concern_id` is the key a RiskAcceptance binds to, but the id
alone is not the binding: `concern_text` (schema 29) pins the acceptance to the
concern's prose at record time, and discharge requires that text still match the
concern currently at that id (see gates._concern_discharged) — a rephrased or
replaced concern at the same positional id stops discharging rather than
silently rebinding. An acceptance is also bound to the exact plan version via
the same meta/stage-digest snapshot a PlanReview itself carries, so an
acceptance recorded against superseded bytes does not silently keep clearing a
`revise` verdict. Every LIVE (non-stale, text-matching) acceptance must appear
in the essence's coverage block, checked by the SAME containment machinery
(plugins_premise.coverage_block / coverage_block_missing_lines) that already
gates order-coverage.

Group 1 locks the core discharge logic directly against gates.py (mirroring
test_plan_review_scope.py's Group 1, including the stage-scoped case — stage 6's
deliberate decision to support acceptance symmetrically at both scopes — and the
concern-text rebinding defect this stage's fix closes). Group 2 locks
cmd_risk_accept's CLI-level validation and its end-to-end gate effect, including
the stage-scoped route. Group 3 locks the essence-block extension: the widened
generator lines (concern text/basis/risk alongside scope/concern_id/author),
their staleness-filtering, and premise_blockers' existing essence-containment
check now catching an omitted or stale-format accepted-risk line."""
from __future__ import annotations

import hashlib
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, gates, plugins
from agentctl import plugins_premise as pp
from agentctl import premise
from agentctl.plan import load_plan, plan_meta_digest, plan_stage_digests
from agentctl.premise import OrderElement
from agentctl.state import Node, PlanPresentation, PlanReview, RiskAcceptance, SessionState


def ns(**kw):
    return Namespace(**kw)


def _subst(**kw) -> SessionState:
    kw.setdefault("plan_path", "/plan.toml")
    return SessionState(session_id="s", task_id="t", weight_class="SUBSTANTIVE",
                        plan_verified=True, **kw)


def _sha256_file(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")


@pytest.fixture
def presentation_on(monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_PRESENTATION", "1")


def _whole_review(plan_path, doc, **kw) -> PlanReview:
    kw.setdefault("verdict", "revise")
    kw.setdefault("reviewer", "thinker")
    kw.setdefault("concerns", ["missing test coverage for the new branch"])
    kw.setdefault("concern_ids", ["c-tests"])
    return PlanReview(
        plan_path=str(plan_path), scope="",
        reviewed_meta_digest=plan_meta_digest(doc),
        reviewed_stage_keys={str(k): v for k, v in plan_stage_digests(doc).items()},
        **kw,
    )


def _stage_review(plan_path, doc, index, **kw) -> PlanReview:
    kw.setdefault("verdict", "revise")
    kw.setdefault("reviewer", "thinker")
    kw.setdefault("concerns", ["the retitle hides a scope change"])
    kw.setdefault("concern_ids", ["c-retitle"])
    return PlanReview(
        plan_path=str(plan_path), scope=f"stage:{index}",
        reviewed_meta_digest=plan_meta_digest(doc),
        reviewed_stage_keys={str(k): v for k, v in plan_stage_digests(doc).items()},
        **kw,
    )


def _acceptance(scope, concern_id, concern_text, plan_path, doc, **kw) -> RiskAcceptance:
    kw.setdefault("basis", "the team reviewed the gap and accepts the trade")
    kw.setdefault("risk", "a regression in the untested branch ships unnoticed")
    kw.setdefault("author", "fedor")
    return RiskAcceptance(
        scope=scope, concern_id=concern_id, concern_text=concern_text, plan_path=str(plan_path),
        meta_digest=plan_meta_digest(doc),
        stage_keys={str(k): v for k, v in plan_stage_digests(doc).items()},
        **kw,
    )


def _to_plan_ready(store, sid, plan_path):
    cli.cmd_start(ns(session=sid, task="demo", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan_path), store=store)


# --- 1. core discharge logic (gates.py) ---------------------------------------

def test_review_with_no_acceptances_behaves_exactly_as_before(gate_on):
    """Locks the pre-existing behavior byte-identical: an empty risk_acceptances
    list (every session before this stage, and any session that never accepts a
    risk) must produce the same blocking message as today."""
    s = _subst(plan_review=PlanReview("/plan.toml", "revise", "thinker"))
    assert s.risk_acceptances == []
    blockers = gates.plan_review_blockers(s, "/plan.toml")
    assert blockers and "revise" in blockers[0]


def test_acceptance_clears_its_concern_with_no_plan_edit(gate_on, tmp_path, fixtures_dir):
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    doc = load_plan(str(plan_path))
    review = _whole_review(plan_path, doc)
    acceptance = _acceptance("", "c-tests", "missing test coverage for the new branch", plan_path, doc)
    s = _subst(plan_path=str(plan_path), plan_review=review, risk_acceptances=[acceptance])
    assert gates.plan_review_blockers(s, str(plan_path)) == []


def test_acceptance_bound_to_superseded_plan_version_does_not_clear(gate_on, tmp_path, fixtures_dir):
    """The acceptance was recorded against the OLD bytes; the plan's GOAL then
    moved (plan_meta_digest covers goal/done_criterion/criterion_type/weight_class/
    repo_root/order — a whole-scope acceptance's own staleness rule, mirroring
    the whole-plan review it answers, cares about exactly this and nothing about
    stage bodies; see _risk_acceptance_stale), with no new acceptance recorded
    against the new bytes. Live proof of the "drop the plan-version binding"
    mutation — a stale acceptance falls back to the same `revise`-blocked message
    as no acceptance at all, never to a "stale" message of its own (the
    underlying review record itself is not stale — only unattested `pass`/
    `revise` verdicts never check content hash at this layer; see
    _plan_review_content_stale)."""
    plan_path = tmp_path / "plan.toml"
    original = (fixtures_dir / "plan_two_stage_substantive.toml").read_text()
    plan_path.write_text(original)
    doc0 = load_plan(str(plan_path))
    review = _whole_review(plan_path, doc0)
    acceptance = _acceptance("", "c-tests", "missing test coverage for the new branch", plan_path, doc0)

    plan_path.write_text(original.replace(
        'goal = "Demonstrate the full two-stage coordination cycle"',
        'goal = "Demonstrate the full two-stage coordination cycle, revised"',
    ))
    s = _subst(plan_path=str(plan_path), plan_review=review, risk_acceptances=[acceptance])
    blockers = gates.plan_review_blockers(s, str(plan_path))
    assert blockers and "revise" in blockers[0]


def test_acceptance_bound_to_a_different_concern_id_does_not_discharge(gate_on, tmp_path, fixtures_dir):
    """Live proof of the "let an acceptance clear a concern it is NOT bound to"
    mutation: a right-scope, wrong-id acceptance must not discharge."""
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    doc = load_plan(str(plan_path))
    review = _whole_review(plan_path, doc)  # concern_ids=["c-tests"]
    acceptance = _acceptance("", "c-other", "an unrelated concern text", plan_path, doc)
    s = _subst(plan_path=str(plan_path), plan_review=review, risk_acceptances=[acceptance])
    blockers = gates.plan_review_blockers(s, str(plan_path))
    assert blockers and "revise" in blockers[0]


def test_acceptance_bound_to_a_different_scope_does_not_discharge(gate_on, tmp_path, fixtures_dir):
    """Same mutation, the other axis: a right-id, wrong-scope acceptance (it
    answers stage:1's concern, not the whole plan's) must not discharge."""
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    doc = load_plan(str(plan_path))
    review = _whole_review(plan_path, doc)  # scope=""
    acceptance = _acceptance("stage:1", "c-tests", "missing test coverage for the new branch", plan_path, doc)
    s = _subst(plan_path=str(plan_path), plan_review=review, risk_acceptances=[acceptance])
    blockers = gates.plan_review_blockers(s, str(plan_path))
    assert blockers and "revise" in blockers[0]


def test_override_still_requires_a_distinct_reviewer_alongside_acceptances(gate_on, tmp_path, fixtures_dir):
    """The override branch and its distinct-reviewer rule are untouched: even
    with a live acceptance recorded (a second, independent route to clear the
    gate), the reviewer who issued the `revise` still cannot override themselves."""
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    doc = load_plan(str(plan_path))
    review = _whole_review(plan_path, doc, reviewer="thinker")
    acceptance = _acceptance("", "c-tests", "missing test coverage for the new branch", plan_path, doc)
    s = _subst(plan_path=str(plan_path), plan_review=review, risk_acceptances=[acceptance])

    self_override = PlanReview(str(plan_path), "override", "thinker", note="self-stamp", scope="")
    blockers = gates._plan_review_verdict_blockers(self_override, state=s, doc=doc)
    assert blockers == []  # override never consults acceptances at all
    # the CLI layer is what refuses a same-reviewer override (cli.cmd_plan_review);
    # gates._plan_review_verdict_blockers only sees a completed override record.
    # Confirmed separately, end to end, in test_risk_accept_and_override_are_independent_routes.


def test_stage_scoped_concern_is_discharged_by_a_stage_scoped_acceptance(gate_on, tmp_path, fixtures_dir):
    """The stage 6 decision: acceptance discharges a STAGE-scoped concern too,
    not only a whole-plan one — symmetric support via the shared
    _plan_review_verdict_blockers, threaded with state/doc from both call sites."""
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    doc0 = load_plan(str(plan_path))
    whole = _whole_review(plan_path, doc0, verdict="pass", concerns=[], concern_ids=[],
                           plan_sha256=_sha256_file(plan_path))

    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive_stage1_retitled.toml").read_text())
    doc1 = load_plan(str(plan_path))
    stage1 = _stage_review(plan_path, doc1, 1)
    acceptance = _acceptance("stage:1", "c-retitle", "the retitle hides a scope change", plan_path, doc1)

    s = _subst(plan_path=str(plan_path), plan_review=whole,
               plan_stage_reviews={"stage:1": stage1},
               risk_acceptances=[acceptance])
    assert gates.plan_review_blockers(s, str(plan_path)) == []


def test_stage_scoped_acceptance_does_not_discharge_a_different_stage(gate_on, tmp_path, fixtures_dir):
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    doc0 = load_plan(str(plan_path))
    whole = _whole_review(plan_path, doc0, verdict="pass", concerns=[], concern_ids=[],
                           plan_sha256=_sha256_file(plan_path))

    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive_stage1_retitled.toml").read_text())
    doc1 = load_plan(str(plan_path))
    stage1 = _stage_review(plan_path, doc1, 1)
    acceptance = _acceptance("stage:2", "c-retitle", "the retitle hides a scope change", plan_path, doc1)  # wrong scope

    s = _subst(plan_path=str(plan_path), plan_review=whole,
               plan_stage_reviews={"stage:1": stage1},
               risk_acceptances=[acceptance])
    blockers = gates.plan_review_blockers(s, str(plan_path))
    assert blockers and "revise" in blockers[0]


def test_rerecorded_review_with_a_different_concern_at_the_same_id_does_not_discharge(
        gate_on, tmp_path, fixtures_dir):
    """The exact Finding-1 defect this stage's fix closes: re-recording a review
    at the same scope on BYTE-IDENTICAL plan content is not a plan edit, so
    acceptance staleness (which only tracks plan-version digests) never fires on
    its own. Without a concern-text check, the acceptance recorded for "missing
    tests" would silently discharge a completely different, never-accepted
    concern that happens to land at the same positional id "c-tests" in the
    re-recorded review."""
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    doc = load_plan(str(plan_path))
    review = _whole_review(plan_path, doc)  # concerns=["missing test coverage..."], concern_ids=["c-tests"]
    acceptance = _acceptance("", "c-tests", "missing test coverage for the new branch", plan_path, doc)
    s = _subst(plan_path=str(plan_path), plan_review=review, risk_acceptances=[acceptance])
    assert gates.plan_review_blockers(s, str(plan_path)) == []

    s.plan_review = _whole_review(
        plan_path, doc,
        concerns=["a hardcoded credential ships in the default config"],
        concern_ids=["c-tests"],
    )
    blockers = gates.plan_review_blockers(s, str(plan_path))
    assert blockers and "revise" in blockers[0]


def test_rephrased_concern_at_the_same_id_stops_discharging(gate_on, tmp_path, fixtures_dir):
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    doc = load_plan(str(plan_path))
    review = _whole_review(plan_path, doc)
    acceptance = _acceptance("", "c-tests", "missing test coverage for the new branch", plan_path, doc)
    s = _subst(plan_path=str(plan_path), plan_review=review, risk_acceptances=[acceptance])
    assert gates.plan_review_blockers(s, str(plan_path)) == []

    s.plan_review = _whole_review(
        plan_path, doc,
        concerns=["missing test coverage for the new error branch"],
        concern_ids=["c-tests"],
    )
    blockers = gates.plan_review_blockers(s, str(plan_path))
    assert blockers and "revise" in blockers[0]


def test_identical_rerecorded_review_still_discharges(gate_on, tmp_path, fixtures_dir):
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    doc = load_plan(str(plan_path))
    review = _whole_review(plan_path, doc)
    acceptance = _acceptance("", "c-tests", "missing test coverage for the new branch", plan_path, doc)
    s = _subst(plan_path=str(plan_path), plan_review=review, risk_acceptances=[acceptance])
    assert gates.plan_review_blockers(s, str(plan_path)) == []

    s.plan_review = _whole_review(plan_path, doc)  # identical concerns/concern_ids
    assert gates.plan_review_blockers(s, str(plan_path)) == []


def test_whitespace_only_concern_difference_still_discharges(gate_on, tmp_path, fixtures_dir):
    """_normalize_string reuse is load-bearing: only whitespace/case moved, the
    concern is the same concern, and the acceptance must keep discharging it."""
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    doc = load_plan(str(plan_path))
    review = _whole_review(plan_path, doc)
    acceptance = _acceptance("", "c-tests", "missing test coverage for the new branch", plan_path, doc)
    s = _subst(plan_path=str(plan_path), plan_review=review, risk_acceptances=[acceptance])
    assert gates.plan_review_blockers(s, str(plan_path)) == []

    s.plan_review = _whole_review(
        plan_path, doc,
        concerns=["  missing   test coverage  for the new branch "],
        concern_ids=["c-tests"],
    )
    assert gates.plan_review_blockers(s, str(plan_path)) == []


def test_acceptance_with_empty_concern_text_does_not_discharge(gate_on, tmp_path, fixtures_dir):
    """Fail-closed: an acceptance recorded with no concern_text at all (e.g. a
    legacy schema-28 record) never discharges, even with a perfectly matching
    scope/concern_id and a live plan version."""
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    doc = load_plan(str(plan_path))
    review = _whole_review(plan_path, doc)
    acceptance = _acceptance("", "c-tests", "", plan_path, doc)
    s = _subst(plan_path=str(plan_path), plan_review=review, risk_acceptances=[acceptance])
    blockers = gates.plan_review_blockers(s, str(plan_path))
    assert blockers and "revise" in blockers[0]


# --- 2. cmd_risk_accept: CLI validation and end-to-end gate effect -------------

def test_risk_accept_refuses_an_unknown_concern_id(store, fixtures_dir, gate_on):
    sid = "ra-badid"
    plan_path = str(fixtures_dir / "plan_two_stage_substantive.toml")
    _to_plan_ready(store, sid, plan_path)
    cli.cmd_plan_review(ns(session=sid, target=None, scope=None, verdict="revise",
                           reviewer="thinker", concerns=["missing tests"],
                           concern_ids=["c-tests"], note="", plan_digest=None), store=store)
    d = cli.cmd_risk_accept(ns(session=sid, scope=None, concern_id="c-nope",
                                basis="the team accepts the gap", risk="a regression ships",
                                author="fedor"), store=store)
    assert d.ok is False
    assert "not among" in d.detail
    assert store.load(sid).risk_acceptances == []


def test_risk_accept_refuses_a_placeholder_basis(store, fixtures_dir, gate_on):
    sid = "ra-placeholder"
    plan_path = str(fixtures_dir / "plan_two_stage_substantive.toml")
    _to_plan_ready(store, sid, plan_path)
    cli.cmd_plan_review(ns(session=sid, target=None, scope=None, verdict="revise",
                           reviewer="thinker", concerns=["missing tests"],
                           concern_ids=["c-tests"], note="", plan_digest=None), store=store)
    d = cli.cmd_risk_accept(ns(session=sid, scope=None, concern_id="c-tests",
                                basis="n/a", risk="a regression ships",
                                author="fedor"), store=store)
    assert d.ok is False
    assert "placeholder" in d.detail
    assert store.load(sid).risk_acceptances == []


def test_risk_accept_refuses_when_no_review_exists_at_that_scope(store, fixtures_dir, gate_on):
    sid = "ra-noreview"
    plan_path = str(fixtures_dir / "plan_two_stage_substantive.toml")
    _to_plan_ready(store, sid, plan_path)
    d = cli.cmd_risk_accept(ns(session=sid, scope=None, concern_id="c-tests",
                                basis="the team accepts the gap", risk="a regression ships",
                                author="fedor"), store=store)
    assert d.ok is False
    assert "no thinker review recorded" in d.detail
    assert store.load(sid).risk_acceptances == []


def test_risk_accept_clears_the_gate_end_to_end(store, fixtures_dir, gate_on):
    sid = "ra-ok"
    plan_path = str(fixtures_dir / "plan_two_stage_substantive.toml")
    _to_plan_ready(store, sid, plan_path)
    cli.cmd_plan_review(ns(session=sid, target=None, scope=None, verdict="revise",
                           reviewer="thinker", concerns=["missing tests"],
                           concern_ids=["c-tests"], note="", plan_digest=None), store=store)
    d = cli.cmd_approve(ns(session=sid, by="user"), store=store)
    assert d.node != Node.APPROVED.value  # still revise-blocked, no plan edit made

    d = cli.cmd_risk_accept(ns(session=sid, scope=None, concern_id="c-tests",
                                basis="the team reviewed and accepts the gap",
                                risk="a regression in the untested branch ships unnoticed",
                                author="fedor"), store=store)
    assert d.ok is True
    assert len(store.load(sid).risk_acceptances) == 1

    d = cli.cmd_approve(ns(session=sid, by="user"), store=store)
    assert d.node == Node.APPROVED.value


def test_risk_accept_scope_stage_clears_the_gate_end_to_end(store, fixtures_dir, tmp_path, gate_on):
    """The whole-plan `pass` is recorded first, against the ORIGINAL bytes; stage 1
    then actually moves (a stage-scoped review only comes into play for a stage
    that moved since the whole review's own baseline — a stage-scoped record
    against an unmoved stage is simply never consulted, see
    _plan_review_blockers_coverage), and the fresh stage:1 `revise` is recorded
    against the NEW bytes, at the same plan_path the whole review still binds to."""
    sid = "ra-stage-ok"
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    _to_plan_ready(store, sid, str(plan_path))
    cli.cmd_plan_review(ns(session=sid, target=None, scope=None, verdict="pass",
                           reviewer="thinker", concerns=None, concern_ids=None, note="",
                           plan_digest=_sha256_file(plan_path)), store=store)

    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive_stage1_retitled.toml").read_text())
    cli.cmd_plan_review(ns(session=sid, target=None, scope="stage:1", verdict="revise",
                           reviewer="thinker", concerns=["the retitle hides a scope change"],
                           concern_ids=["c-retitle"], note="", plan_digest=None), store=store)
    d = cli.cmd_approve(ns(session=sid, by="user"), store=store)
    assert d.node != Node.APPROVED.value

    d = cli.cmd_risk_accept(ns(session=sid, scope="stage:1", concern_id="c-retitle",
                                basis="the team reviewed and accepts the gap",
                                risk="an unvalidated input reaches stage 2 unnoticed",
                                author="fedor"), store=store)
    assert d.ok is True

    d = cli.cmd_approve(ns(session=sid, by="user"), store=store)
    assert d.node == Node.APPROVED.value


def test_risk_accept_and_override_are_independent_routes(store, fixtures_dir, gate_on):
    """Confirms end to end (through the CLI, not just the gates-level check
    above) that override's distinct-reviewer rule survives acceptance's
    existence as an unrelated, independent way to clear a `revise` verdict."""
    sid = "ra-override"
    plan_path = str(fixtures_dir / "plan_two_stage_substantive.toml")
    _to_plan_ready(store, sid, plan_path)
    cli.cmd_plan_review(ns(session=sid, target=None, scope=None, verdict="revise",
                           reviewer="thinker", concerns=["missing tests"],
                           concern_ids=["c-tests"], note="", plan_digest=None), store=store)
    d = cli.cmd_plan_review(ns(session=sid, target=None, scope=None, verdict="override",
                               reviewer="thinker", concerns=None, concern_ids=None,
                               note="self-stamp", plan_digest=None), store=store)
    assert d.ok is False and "distinct reviewer" in d.detail

    d = cli.cmd_risk_accept(ns(session=sid, scope=None, concern_id="c-tests",
                                basis="the team reviewed and accepts the gap",
                                risk="a regression in the untested branch ships unnoticed",
                                author="fedor"), store=store)
    assert d.ok is True
    d = cli.cmd_approve(ns(session=sid, by="user"), store=store)
    assert d.node == Node.APPROVED.value


# --- 3. the essence coverage block: generator, staleness, and the gate --------

def test_render_coverage_block_appends_accepted_risk_lines_sorted_deterministically():
    elements = [OrderElement(id="O1", element="e", disposition="covered", stage=1, reason="")]
    text = premise.render_coverage_block(
        elements, 1,
        accepted_risks=[
            ("stage:2", "c-b", "some\n concern", "a  basis", "some risk", "eve"),
            ("", "c-a", "another concern", "another basis", "another risk", "alice"),
        ],
    )
    assert text.splitlines()[-2:] == [
        "- accepted risk: scope '' concern 'c-a' ('another concern') — accepted by alice: "
        "basis 'another basis', risk 'another risk'",
        "- accepted risk: scope 'stage:2' concern 'c-b' ('some concern') — accepted by eve: "
        "basis 'a basis', risk 'some risk'",
    ]


def test_render_coverage_block_with_no_accepted_risks_is_unchanged():
    elements = [OrderElement(id="O1", element="e", disposition="covered", stage=1, reason="")]
    assert (premise.render_coverage_block(elements, 1)
            == premise.render_coverage_block(elements, 1, accepted_risks=[])
            == premise.render_coverage_block(elements, 1, accepted_risks=None))


def test_coverage_block_excludes_a_stale_accepted_risk(tmp_path, fixtures_dir):
    """Staleness mirrors how the review at that scope would itself be judged: a
    whole-scope acceptance survives an unrelated stage edit; a stage-scoped one
    does not survive an edit to ITS OWN stage."""
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    doc0 = load_plan(str(plan_path))
    stage_acceptance = _acceptance("stage:1", "c-retitle", "the retitle hides a scope change", plan_path, doc0)
    whole_acceptance = _acceptance("", "c-tests", "missing test coverage for the new branch", plan_path, doc0)

    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive_stage1_retitled.toml").read_text())
    doc1 = load_plan(str(plan_path))
    state = SessionState(session_id="s", task_id="t", weight_class="SUBSTANTIVE",
                         plan_path=str(plan_path),
                         risk_acceptances=[stage_acceptance, whole_acceptance])
    plugins.activate(state, "premise")
    bag = state.plugins["premise"]
    block = pp.coverage_block(state, bag, doc=doc1)
    assert "c-tests" in block
    assert "c-retitle" not in block


def _premise_state(plan_path, doc):
    state = SessionState(session_id="s", task_id="t", weight_class="SUBSTANTIVE",
                         plan_path=str(plan_path))
    plugins.activate(state, "premise")
    bag = state.plugins["premise"]
    bag["order_elements"] = [{
        "id": "O1", "element": "the order this plan answers",
        "disposition": "covered", "stage": 1, "reason": "",
    }]
    bag["enumerated"] = True
    bag["enumerated_at"] = pp._plan_content_digest(doc)
    return state, bag


def test_premise_blockers_blocks_essence_missing_a_live_accepted_risk_line(
        tmp_path, fixtures_dir, presentation_on):
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    doc = load_plan(str(plan_path))
    state, bag = _premise_state(plan_path, doc)
    state.risk_acceptances = [_acceptance("", "c-tests", "missing test coverage for the new branch", plan_path, doc)]
    state.plan_presentations = [PlanPresentation(
        plan_path=str(plan_path), kind="essence", plan_sha256="x",
        rendering_sha256="y", rendering_text="Summary of the plan.", presented_ts=0.0,
    )]
    blockers = pp.premise_blockers(state, bag)
    assert any("does not carry the current scope-coverage block" in b for b in blockers)
    assert any("c-tests" in b for b in blockers)


def test_premise_blockers_allows_essence_that_carries_the_accepted_risk_line(
        tmp_path, fixtures_dir, presentation_on):
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    doc = load_plan(str(plan_path))
    state, bag = _premise_state(plan_path, doc)
    state.risk_acceptances = [_acceptance("", "c-tests", "missing test coverage for the new branch", plan_path, doc)]
    block = pp.coverage_block(state, bag, doc=doc)
    state.plan_presentations = [PlanPresentation(
        plan_path=str(plan_path), kind="essence", plan_sha256="x",
        rendering_sha256="y", rendering_text=f"Summary of the plan.\n\n{block}", presented_ts=0.0,
    )]
    assert pp.premise_blockers(state, bag) == []


def test_premise_blockers_rejects_essence_carrying_only_the_old_accepted_risk_format(
        tmp_path, fixtures_dir, presentation_on):
    """An essence carrying the pre-stage-6.1 line shape (scope/concern_id/author
    only, no concern text/basis/risk) for its one accepted-risk line — everything
    else in the block present and correct — no longer satisfies the live coverage
    block: the extra fields are load-bearing content the approval gate needs to
    see the trade, not decoration a reformatting-tolerant check can wave through."""
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    doc = load_plan(str(plan_path))
    state, bag = _premise_state(plan_path, doc)
    state.risk_acceptances = [_acceptance("", "c-tests", "missing test coverage for the new branch",
                                           plan_path, doc)]
    block = pp.coverage_block(state, bag, doc=doc)
    current_line = block.splitlines()[-1]
    old_style_line = "- accepted risk: scope '' concern 'c-tests' — accepted by fedor"
    assert current_line != old_style_line
    stale_block = block.replace(current_line, old_style_line)
    state.plan_presentations = [PlanPresentation(
        plan_path=str(plan_path), kind="essence", plan_sha256="x",
        rendering_sha256="y",
        rendering_text=f"Summary of the plan.\n\n{stale_block}", presented_ts=0.0,
    )]
    blockers = pp.premise_blockers(state, bag)
    assert any("does not carry the current scope-coverage block" in b for b in blockers)
    assert any(current_line in b for b in blockers)
