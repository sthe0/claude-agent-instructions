"""The requirement on the way of acting, separate from the sequence of operations.

Defect 3 of the SMD act-modelling rework. `Means` had ONE field where a plan has two
different things to say — its docstring said so in as many words, "the fixed instruments
(means) and the procedure over them (method)" — so the REQUIREMENT the transformation
must satisfy and the SEQUENCE of operations proposed for meeting it shared a place, and
the concrete one took it. Everything downstream then had to treat that one text as a
norm: an executor who read the code and saw a better order either followed the worse one
because it was written down, or quietly rewrote what he was being held to. Neither is
visible in the diff as what it is.

The remedy has three halves, and their simultaneity is what these tests pin as much as
any one of them:

  * STRUCTURAL: `procedure` is a place of its own, required of a substantive stage at the
    submission seam (never at the loader — submission.py's module docstring says why),
    and entering EVERY change-decision function in the same commit as the field.
  * JUDGED, and fail-open: a `procedure` that says nothing its `method` does not is
    refused. Normalized string equality is NOT the instrument — measured against this
    repo's own 200-stage corpus while the split was designed, it caught 0 — so a
    structural prefilter proposes and a model disposes (procedure.py), and no reachable
    judge means no refusal at all.
  * TYPED, on the replan side: `agentctl replan --renormalize` lets the executor replace
    the sequence on his own authority, and is refused the moment the edit reaches the
    method, the criterion, the result image, or the goal every stage-8 observation is
    compared against. Without that path the separation would be nominal — a procedure
    the executor may not touch without a re-approval is a norm whatever the field is
    called.

The refusal is asserted alongside the requirement for the reason test_preconditions.py
records: refusing a collapse while NOT requiring the second field would be the engine
telling an author to move a sentence into a place his grade never gave him.

One test here answers a question carried into this stage from stage 4: whether the light
renormalization path can be used to re-select `material_refs`/`knowledge_refs` and
thereby walk around the coverage gate stage 4 built. It cannot, and the test pins WHY —
the residual totality check runs over `plan.stage_question_key`, which carries both ref
lists through `plan.knowledge_place`.
"""
from __future__ import annotations

import dataclasses
import json
import tomllib
from argparse import Namespace
from copy import deepcopy
from pathlib import Path

import pytest

from agentctl import cli, gates, plan as plan_mod
from agentctl.dispatch import RunResult
from agentctl.plan import (
    PlanMeta,
    diff_plans,
    load_plan,
    procedure_place,
    stage_carry_key,
    stage_question_key,
)
from agentctl.procedure import OVERLAP_THRESHOLD, collapse_prefilter, overlap
from agentctl.state import (
    Actor,
    Criterion,
    Means,
    Principle,
    Stage,
    StageStatus,
    Subject,
    Supply,
)
from agentctl.submission import submission_violations

from conftest import SUBSTANTIVE_FINAL_CHECK, SUBSTANTIVE_ORDER
from dataclass_domain import leaf_paths

CORPUS = Path(__file__).resolve().parent / "fixtures" / "plan_corpus"

# A requirement: it says what the result must be an instance of, and nothing about the
# order in which anyone gets there.
METHOD = "Extend the existing submission enumerator rather than adding a parallel one; the new refusal lands in scripts/agentctl/submission.py beside its siblings."
# A sequence: ordered sub-actions, each naming what it touches.
PROCEDURE = "1. Read submission.py's per-stage loop. 2. Add the row to _SUBSTANTIVE_SUBMISSION_FIELDS. 3. Write its _WHY entry. 4. Run pytest -q on the tests directory."

# The distinctive substring of procedure.py's judge prompt — asserted absent from the
# argv of every call the seam makes when the two texts plainly differ.
_ASKED_MARKER = "PROCEDURE is the SEQUENCE of operations"

# The headroom procedure.py's CALIBRATION note claims between the measured worst honest
# pair (0.30) and OVERLAP_THRESHOLD (0.50): their midpoint. Not a second threshold — the
# prefilter never reads it — but the point at which "with room to spare" stops being true
# of the corpus and the note has to be rewritten rather than quietly outgrown.
_CLAIMED_HEADROOM_CEILING = 0.40

_DEFAULTS = {
    "weight_class": 'weight_class = "substantive"\n',
    "method": METHOD,
    "procedure": f'procedure = {json.dumps(PROCEDURE)}\n',
    "done_criterion": "d2",
    "result": "The seam refuses a plan whose sequence restates its requirement.",
    "material_refs": '["scripts/agentctl/submission.py"]',
    "title": "the stage under test",
}

_PLAN = """
[meta]
task_id = "rn"
goal = "g"
done_criterion = "dc"
criterion_type = "measurable"
{weight_class}external_research = "none applies"
""" + SUBSTANTIVE_ORDER + """
[[stage]]
index = 1
title = "the stage that goes first"
executor = "in_thread"
expected_result_image = "The parser reads the new key."
criterion_type = "measurable"
done_criterion = "d1"
verify_command = "pytest -q"
material = "m1"
means = "bash"
method = "Add the field to the typed struct, keeping the corpus loadable."
procedure = "1. Read state.py's Means. 2. Add the field with its default."
conditions = "The tree is clean."
preconditions = "The branch is checked out."
invariants = "n1"
capability_required = "cap"
material_refs = ["scripts/agentctl/state.py"]
knowledge_refs = ["scripts/agentctl/plan.py"]
knowledge = "how the loader stays lenient"
[stage.principle]
statement = "s"
source = "src"
derivation = "der"
confidence = "high"
refutation = "r"

[[stage]]
index = 2
title = {title}
executor = "in_thread"
expected_result_image = {result}
criterion_type = "measurable"
done_criterion = {done_criterion}
verify_command = "pytest -q"
material = "m2"
means = "bash"
method = {method}
{procedure}conditions = "The advisor is reachable."
preconditions = "Stage 1's field exists."
invariants = "the 55 frozen corpus plans keep loading byte-for-byte"
capability_required = "cap"
material_refs = {material_refs}
knowledge_refs = ["scripts/agentctl/procedure.py"]
knowledge = "where a submission requirement may bind"
[[stage.supplies]]
on = 1
element = "result"
[stage.principle]
statement = "s"
source = "src"
derivation = "der"
confidence = "high"
refutation = "r"
""" + SUBSTANTIVE_FINAL_CHECK


def ns(**kw):
    return Namespace(**kw)


def _write_plan(path: Path, **over) -> str:
    """Render the two-stage template, overriding stage 2's fields by keyword.

    `procedure` and `weight_class` are whole LINES (so passing "" omits the key
    entirely); every other override is a value the template JSON-quotes."""
    fields = dict(_DEFAULTS)
    fields.update(over)
    for key in ("method", "done_criterion", "result", "title"):
        fields[key] = json.dumps(fields[key])
    path.write_text(_PLAN.format(**fields), encoding="utf-8")
    return str(path)


def _judge(verdict: str):
    def run(argv, timeout=None):
        return RunResult(0, verdict + "\n", "")
    return run


def _judge_unavailable(argv, timeout=None):
    """The judge cannot be reached — the shape a missing/failing `claude -p` takes."""
    return RunResult(1, "", "no such model")


@pytest.fixture(autouse=True)
def _advisor_on(monkeypatch):
    """The seam resolves its judge through advisor.resolve_enabled, whose documented
    force-on knob is this env var. Set explicitly so these tests do not depend on the
    machine's config.md advisor-mode — and every command below is handed an explicit
    stub runner, so no test here can reach a live `claude -p`."""
    monkeypatch.setenv("AGENTCTL_ADVISOR", "1")


def _submit(store, plan_path, runner, session="rn"):
    cli.cmd_start(ns(session=session, task="rn", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=session, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=session), store=store)
    return cli.cmd_submit_plan(ns(session=session, plan=plan_path), store=store,
                               runner=runner)


def _approved(store, plan_path, session="rn"):
    d = _submit(store, plan_path, _judge_unavailable, session=session)
    assert d.ok is True, d.data
    cli.cmd_approve(ns(session=session, by="user"), store=store,
                    runner=_judge_unavailable)
    return store.load(session)


def _renormalize(store, plan_path, session="rn"):
    return cli.cmd_replan(
        ns(session=session, plan=plan_path, renormalize=True,
           coverage_waiver=None, normalization_waiver=None, cost_log=None),
        store=store, runner=_judge_unavailable,
    )


def _problems(d) -> list[str]:
    return list(d.data.get("problems", []))


# --- the structural half: the place is required, unconditionally ------------


def test_substantive_stage_without_procedure_is_refused_at_submission(store, tmp_path):
    """Without this refusal `procedure` is an optional key nobody fills, and the defect
    survives with a new field beside it: every stage still writing its sequence into
    `method`, where it reads as a norm.

    Refused at SUBMISSION and not at load, for the reason submission.py's module
    docstring gives — a loader-side requirement is retroactive over the 55 stored plans
    that predate the field. The session staying at PLANNING is half the assertion: a
    refusal that advanced the node would strand it with an armed gate and no edge back."""
    plan = _write_plan(tmp_path / "p.toml", procedure="")

    d = _submit(store, plan, _judge("DISTINCT"))

    assert d.ok is False
    assert d.action == "fix_plan"
    assert store.load("rn").node == "PLANNING"
    assert any("missing 'procedure'" in p and "stage 2" in p for p in _problems(d)), (
        _problems(d)
    )


def test_substantive_stage_without_method_is_refused_at_submission(store, tmp_path):
    """`method` is already a LOADER requirement, so the seam's row for it looks
    redundant — and is not. The seam also grades a plan that declares no weight class
    while the SESSION is substantive (submission.py's UNDECLARED branch), and on that
    path the loader is lenient by construction. Without the row, the one grade nobody
    declared is the one where the requirement on the way of acting is optional."""
    plan = _write_plan(tmp_path / "p.toml", weight_class="", method="")

    d = _submit(store, plan, _judge("DISTINCT"))

    assert d.ok is False
    assert any("missing 'method'" in p and "stage 2" in p for p in _problems(d)), (
        _problems(d)
    )


def test_procedure_is_absent_from_the_loader_requirement_set(tmp_path):
    """The requirement must NOT have leaked into `plan._validate_substantive_stage`. A
    plan omitting `procedure` has to LOAD — strictly, without raising — because
    in-session call sites re-read plans the session already accepted and attestations are
    computed over their exact bytes."""
    plan = _write_plan(tmp_path / "p.toml", procedure="")

    doc = load_plan(plan, strict=True)

    assert doc.stages[1].means.procedure == ""
    assert "procedure" not in plan_mod._SUBSTANTIVE_STAGE_FIELDS


# --- the judged half: a prefilter proposes, a model disposes, silence accepts ---


def test_a_procedure_that_only_restates_the_method_is_refused(store, tmp_path):
    """The message is the load-bearing part. An author told only "this is wrong" learns
    nothing; the message must say which of the two things he wrote twice and what each
    place is for, because the whole point of the field is that they are governed
    differently."""
    plan = _write_plan(tmp_path / "p.toml",
                       procedure=f'procedure = {json.dumps(METHOD)}\n')

    d = _submit(store, plan, _judge("SAME"))

    assert d.ok is False
    collapses = [p for p in _problems(d) if "says nothing `method` does not" in p]
    assert len(collapses) == 1, _problems(d)
    assert "stage 2" in collapses[0]
    assert "--renormalize" in collapses[0]


def test_the_collapse_refusal_fails_open_when_the_judge_is_unavailable(store, tmp_path):
    """Fail-open, and this is the direction that matters: the verdict REFUSES a plan.
    The same bytes the test above turns away must submit here with no judge to ask —
    byte-identical to the check not existing. A prefilter never refuses on its own."""
    plan = _write_plan(tmp_path / "p.toml",
                       procedure=f'procedure = {json.dumps(METHOD)}\n')
    assert collapse_prefilter(METHOD, METHOD), "fixture drift: this no longer prefilters"

    d = _submit(store, plan, _judge_unavailable)

    assert d.ok is True
    assert d.marker == "PLAN-READY"
    assert not [p for p in _problems(d) if "says nothing `method` does not" in p]

    # The same property one layer down, where it is a defaults question: a caller that
    # passes no judge at all gets the list it got before this check existed.
    doc = load_plan(plan)
    assert not [p for p in submission_violations(doc, session_weight_class="substantive")
                if "says nothing `method` does not" in p]


def test_a_distinct_procedure_never_reaches_the_judge(store, tmp_path):
    """Both stubs, and the first is the sharper claim. Under a judge stubbed to answer
    SAME about everything, a genuine method/procedure pair still submits — so the
    acceptance comes from the prefilter declining to ASK, not from a lenient answer.
    A widened prefilter that started asking about ordinary pairs would put every
    substantive plan's fate in a model's hands.

    (The seam runs other judges on the same runner, so the assertion is that no call
    carried THIS question, not that no call was made.)"""
    plan = _write_plan(tmp_path / "p.toml")

    asked: list[str] = []

    def counting_judge(argv, timeout=None):
        asked.extend(argv)
        return RunResult(0, "SAME\n", "")

    d = _submit(store, plan, counting_judge)
    assert d.ok is True, _problems(d)
    assert not [a for a in asked if _ASKED_MARKER in a], (
        "the prefilter asked a model about a plainly distinct method/procedure pair"
    )

    assert _submit(store, plan, _judge_unavailable, session="rn2").ok is True


def test_the_overlap_threshold_clears_the_corpus_null_case():
    """The threshold's justification, re-measured on every run rather than asserted in a
    comment. No corpus of labelled COLLAPSES exists — the field is new, so nothing has
    been written in it to label — but the NULL case is measurable and is what a false
    positive costs: two DIFFERENT places of one stage, written honestly by one author
    about one subject matter. Every (method, means/conditions/invariants/material) pair
    in the frozen corpus is such a case.

    A corpus pair reaching the threshold would mean the prefilter has started summoning
    a judge about honest prose; this turns red instead. It is a calibration, not a
    guarantee: the recall direction stays unmeasured, exactly as procedure.py says.

    Two assertions, because the threshold's justification is not "no pair reaches 0.50"
    but "the worst honest pair is 0.30, so 0.50 clears every measured one WITH ROOM TO
    SPARE". A corpus that drifted to 0.49 would keep the first assertion green while
    making that sentence false, and nothing would say so — so the headroom the comment
    claims is pinned too, at the midpoint between the measured worst and the threshold."""
    worst = 0.0
    worst_at = ""
    pairs = 0
    for path in sorted(CORPUS.glob("*.toml")):
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
        for stage in doc.get("stage", []):
            method = stage.get("method", "") or ""
            if not method.strip():
                continue
            for other in ("means", "conditions", "invariants", "material"):
                text = stage.get(other, "") or ""
                if not text.strip():
                    continue
                pairs += 1
                score = overlap(method, text)
                if score > worst:
                    worst, worst_at = score, f"{path.name}:{stage['index']}.{other}"
    # The corpus is frozen at 800 such pairs; the floor is a drift guard, so that a
    # measurement over an empty or half-read corpus cannot pass as a clean one.
    assert pairs >= 700, f"corpus shrank: only {pairs} null-case pairs"
    print(f"\ncorpus null case: {pairs} pairs, max overlap {worst:.4f} at {worst_at}")
    assert worst < OVERLAP_THRESHOLD, (
        f"the highest honest pair in the corpus ({worst:.2f} at {worst_at}) reaches the "
        f"prefilter threshold {OVERLAP_THRESHOLD} — either the threshold is too low or "
        f"the corpus gained a stage whose method and {worst_at.split('.')[-1]} say the "
        f"same thing"
    )
    assert worst < _CLAIMED_HEADROOM_CEILING, (
        f"the highest honest pair in the corpus ({worst:.2f} at {worst_at}) is past "
        f"{_CLAIMED_HEADROOM_CEILING}, so the threshold {OVERLAP_THRESHOLD} no longer "
        f"clears the measured null case 'with room to spare' as procedure.py's "
        f"CALIBRATION note claims — re-measure and rewrite that note, or raise the "
        f"threshold, before treating this corpus as a calibration"
    )


# --- the standing obligation: one new field, every change decision ----------


def _stage(tag, *, procedure="", vvaf=None, preconditions=None):
    return Stage(
        index=2,
        title=f"title-{tag}",
        subject=Subject(material="m", result="r", invariants="i"),
        means=Means(means="bash", method="run", procedure=procedure),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(criterion_type="measurable", done_criterion="d",
                            verify_command="pytest -q", expected_exit=0,
                            verify_venue_at_final=vvaf),
        conditions="c",
        preconditions=preconditions,
        supplies=[Supply(on=1)],
    )


def test_procedure_enters_every_change_decision_function(tmp_path):
    """A field that enters one change decision and not the others is how a correction
    gets silently dropped. FIVE decisions here, not the four `preconditions` had, and the
    fifth is the one this stage could most easily have missed: `_operative_surface` and
    `diff_plans` are different functions answering different questions, and extending
    either does nothing for the other. Without `diff_plans`, a procedure-only edit diffs
    as `no_change` and the renormalization branch is unreachable by construction; without
    `_operative_surface`, a replan that removes a difficulty by re-sequencing cannot
    satisfy the CHANGE half of the coverage gate.

    The two encoding properties are asserted with them, because they are what makes the
    addition safe on live sessions: UNDECLARED contributes nothing (stage_question_key is
    persisted in Question.disposed_at_key and compared across processes), and the value
    is NESTED so it cannot flatten into the conditional splice beside it."""
    absent, declared = _stage("a"), _stage("a", procedure=PROCEDURE)

    # (0) declared-only: absent contributes nothing at all to any key
    assert procedure_place(absent) == ()
    assert len(stage_carry_key(declared)) == len(stage_carry_key(absent)) + 1

    # (0b) no collision with the other conditionally-spliced fields — a real defect
    # this caught, not a formality: nesting alone stops a value FLATTENING into its
    # neighbours, and does nothing about two independently-conditional splices reducing
    # to the same element. Untagged, a stage that MOVED one sentence from
    # `preconditions` to `procedure` carried its PASSED outcome forward untouched.
    assert stage_carry_key(_stage("a", procedure="delivery")) != stage_carry_key(
        _stage("a", vvaf="delivery")
    )
    assert stage_carry_key(_stage("a", procedure="delivery")) != stage_carry_key(
        _stage("a", preconditions="delivery")
    )
    assert stage_question_key(_stage("a", procedure="delivery")) != stage_question_key(
        _stage("a", preconditions="delivery")
    )

    # (1) carry-forward: a re-sequenced stage may not keep its PASSED outcome
    assert stage_carry_key(absent) != stage_carry_key(declared)
    # (2) question staleness: an answer given against the old sequence is stale
    assert stage_question_key(absent) != stage_question_key(declared)

    old = load_plan(_write_plan(tmp_path / "old.toml"))
    new = deepcopy(old)
    new.stages[1].means.procedure = PROCEDURE + " 5. Commit by path."
    # (3) refinement vs no_change: the edit must not vanish in the diff
    assert diff_plans(old, new) == "refinement"
    # (4) the coverage gate's CHANGE half: a re-sequencing is a real change
    assert gates._operative_surface(old) != gates._operative_surface(new)
    # …and the same collision, in the surface's own encoding: its components are
    # per-stage strings, so an untagged procedure would compare equal to a
    # `verify_venue_at_final` carrying the same word.
    as_venue, as_procedure = deepcopy(old), deepcopy(old)
    as_venue.stages[1].means.procedure = ""
    as_venue.stages[1].criterion.verify_venue_at_final = "delivery"
    as_procedure.stages[1].means.procedure = "delivery"
    assert gates._operative_surface(as_venue) != gates._operative_surface(as_procedure)
    # (5) approve/replan re-materialization: the live stage must track the plan bytes
    live = _stage("a")
    cli._apply_refined_stage_fields(live, declared)
    assert live.means.procedure == declared.means.procedure


# --- the typed replan branch ------------------------------------------------


def test_renormalize_accepts_a_procedure_only_edit(store, tmp_path):
    """The whole point of the split: replacing the sequence costs no re-approval. The
    live stage must track the new bytes (or the session executes a sequence nobody can
    read), `plan_path` must follow them, and the event must be logged as `renormalize`
    and NOT `replan` — `effort.replan_count` fires the divergence trigger at three, and
    an executor exercising the authority the plan gave him is not a norm revision."""
    plan = _write_plan(tmp_path / "p.toml")
    _approved(store, plan)

    resequenced = _write_plan(
        tmp_path / "q.toml",
        procedure='procedure = "1. Write the _WHY entry first. 2. Add the row to _SUBSTANTIVE_SUBMISSION_FIELDS. 3. Read the per-stage loop in submission.py. 4. Run pytest -q."\n',
    )
    d = _renormalize(store, resequenced)

    assert d.ok is True, d.data
    state = store.load("rn")
    assert state.plan_path == resequenced
    assert "Write the _WHY entry first" in state.stage(2).means.procedure
    events = [h.get("event") for h in state.history]
    assert "renormalize" in events
    assert "replan" not in events


@pytest.mark.parametrize("over,expected", [
    ({"method": "Rewrite the enumerator from scratch in a new module."}, "means.method"),
    ({"done_criterion": "the reviewer says it reads well"}, "criterion.done_criterion"),
    ({"result": "The seam accepts every plan."}, "subject.result"),
])
def test_renormalize_refuses_an_edit_that_reaches_a_norm(store, tmp_path, over, expected):
    """The requirement, the criterion and the result image are the customer's and the
    planner's. If the light path could carry any of them, the review and approval a
    replan re-arms would be a formality anyone could route around by adding the flag —
    and the field split would have bought a bypass rather than a separation.

    The refusal names the field, because "not a renormalization" tells an author nothing
    about which of his edits was the one that cost him a replan."""
    plan = _write_plan(tmp_path / "p.toml")
    _approved(store, plan)

    d = _renormalize(store, _write_plan(tmp_path / "q.toml", **over))

    assert d.ok is False
    assert d.action == "replan"
    blockers = d.data["blockers"]
    assert any(expected in b for b in blockers), blockers
    assert store.load("rn").plan_path == plan


def test_renormalize_refuses_a_material_refs_re_selection(store, tmp_path):
    """The question stage 4 carried into this one: can the light path re-select
    `material_refs`/`knowledge_refs` and so walk around the coverage gate that makes a
    re-selected material visible?

    It cannot, and the mechanism is worth naming because no refusal in
    `_RENORM_PROTECTED` lists the refs. The RESIDUAL check does it: the old stage is
    copied, only its `means.procedure` is set to the new value, and
    `plan.stage_question_key` of that transplant must equal the new stage's — and that
    key carries both ref lists through `plan.knowledge_place`. The same residual is why
    the named list's incompleteness is harmless in general; this is one instance of it."""
    plan = _write_plan(tmp_path / "p.toml")
    _approved(store, plan)

    d = _renormalize(store, _write_plan(
        tmp_path / "q.toml",
        material_refs='["scripts/agentctl/submission.py", "scripts/agentctl/gates.py"]',
    ))

    assert d.ok is False
    assert any("other than `means.procedure`" in b for b in d.data["blockers"]), (
        d.data["blockers"]
    )


def test_the_residual_catches_a_field_no_named_refusal_lists(store, tmp_path):
    """The named refusals are for the MESSAGE; the residual is for the guarantee. `title`
    is in no protected row, and an edit to it is still not a renormalization — as would
    be an edit to a field added long after this test was written, which is the property
    that cannot be tested directly and is why the check is a totality check rather than a
    longer list."""
    plan = _write_plan(tmp_path / "p.toml")
    _approved(store, plan)

    d = _renormalize(store, _write_plan(tmp_path / "q.toml", title="a better name"))

    assert d.ok is False
    assert any("other than `means.procedure`" in b for b in d.data["blockers"])


def test_the_prose_diff_distinguishes_its_two_bare_conditional_splices(tmp_path):
    """The same collision `procedure_place`'s tag closed, at the one other site that has
    it. `diff_plans._prose` splices FIVE conditional components, not the three the two
    persisted keys do, and its two extra ones — `verify_venue_at_final` and `cost_tier` —
    were both bare strings, so a plan that MOVED a value from one to the other diffed as
    `no_change` and the correction was dropped.

    Honest about its own reach: with both fields declared under a STRICT load the values
    cannot collide, because the two vocabularies are disjoint (delivery/repo_root vs
    small/medium/large) and `parse_plan` enforces both. What reaches this is a lenient
    load — the replan and renormalization BASELINE, deliberately lenient so a snapshot
    frozen under an older schema still reads. So this is a latent case closed at the cost
    of a tag, not a bug anyone met; the values below are out of vocabulary for that
    reason, and the tag is in `_prose` only because retagging the persisted keys would
    flip every already-disposed question."""
    old = load_plan(_write_plan(tmp_path / "old.toml"))
    as_venue, as_tier = deepcopy(old), deepcopy(old)
    as_venue.stages[1].criterion.verify_venue_at_final = "collide"
    as_tier.stages[1].actor.cost_tier = "collide"

    assert diff_plans(as_venue, as_tier) == "refinement"


def _edited(src: str, dst: Path, old: str, new: str) -> str:
    """The template renders one plan; a [meta] edit is a substring swap on its bytes.

    `_write_plan` overrides stage 2's fields only, and every case below edits the plan
    LEVEL — the half that had no residual until this stage's review found the gap."""
    text = Path(src).read_text(encoding="utf-8")
    assert old in text, f"the template no longer contains {old!r}"
    dst.write_text(text.replace(old, new, 1), encoding="utf-8")
    return str(dst)


#: The shape a hand-maintained ONE-LEVEL expansion used before this stage's recursive
#: fix — `criterion` expanded into `Criterion`'s own fields, but `criterion.landed`
#: (itself a dataclass) stayed a single opaque leaf, never further expanded. No longer
#: used by the totality test below; kept only so the regression test right after it can
#: show concretely what the shallower domain would have missed.
_STAGE_CLUSTERS = {
    "subject": Subject, "means": Means, "actor": Actor,
    "criterion": Criterion, "principle": Principle,
}

#: Every leaf field of a Stage's DEFINITION, and where the renormalization residual
#: (`gates._renorm_stage_residual`) picks it up — or why it is deliberately outside.
#: Expanded by `dataclass_domain.leaf_paths`, genuinely recursively rather than the one
#: level a hand-written cluster map gave: `criterion.landed` and `outcome` are both
#: dataclass-typed fields the traversal descends INTO, contributing their own leaves
#: (`criterion.landed.target` etc., `outcome.status` etc.) rather than stopping at the
#: struct boundary — as does `supplies`, a `list[Supply]`, contributing `supplies.on`
#: etc. `control` stays a single leaf: it is a plain `str | None`, nothing to descend
#: into. Values are prose for whoever the test turns red on, never read by the assert.
_STAGE_RESIDUAL_COVERAGE = {
    "index": "outside: the key the two plans' stages are MATCHED on — a change here is "
             "an added or removed stage, refused by its own message above",
    "title": "stage_question_key",
    "conditions": "stage_question_key",
    "preconditions": "stage_question_key, via preconditions_place",
    "knowledge": "stage_question_key, via knowledge_place",
    "supplies.on": "stage_question_key (and depends_on, which projects from it)",
    "supplies.element": "stage_question_key (and depends_on, which projects from it)",
    "supplies.artifact": "stage_question_key (and depends_on, which projects from it)",
    "output_artifacts": "the residual's own splice — outside stage_question_key because "
                        "no Question.target names it, inside here because re-declaring "
                        "what a stage produces moves which green a check can reach",
    "outcome.status": "outside: the mutable execution RECORD, not the definition — a "
                      "plan doc loaded from TOML carries its defaults, and leaving the "
                      "live copy alone is what this whole path is for",
    "outcome.actual": "outside: the mutable execution RECORD, same reason",
    "outcome.fail_digests": "outside: the mutable execution RECORD, same reason",
    "outcome.cost_usd": "outside: the mutable execution RECORD, same reason",
    "outcome.duration_ms": "outside: the mutable execution RECORD, same reason",
    "outcome.spawn_count": "outside: the mutable execution RECORD, same reason",
    "outcome.delivered_head": "outside: the mutable execution RECORD, same reason",
    "control": "outside: the control attestation record-result stamps, same reason",
    "subject.material": "stage_question_key",
    "subject.result": "stage_question_key",
    "subject.invariants": "stage_question_key",
    "subject.material_refs": "stage_question_key, via knowledge_place",
    "subject.knowledge_refs": "stage_question_key, via knowledge_place",
    "means.means": "stage_question_key",
    "means.method": "stage_question_key",
    "means.procedure": "the one field the transplant sets — the residual's subject",
    "actor.executor": "stage_question_key",
    "actor.capability_required": "stage_question_key",
    "actor.cost_tier": "the residual's own splice — outside stage_question_key because "
                       "no Question.target names it, inside here because the dispatch "
                       "budget and the effort estimate read it as a norm",
    "criterion.criterion_type": "stage_question_key",
    "criterion.done_criterion": "stage_question_key",
    "criterion.verify_command": "stage_question_key",
    "criterion.expected_exit": "stage_question_key",
    "criterion.verify_venue": "stage_question_key",
    "criterion.verify_kind": "stage_question_key",
    "criterion.landed.target": "stage_question_key, via the whole LandedSpec object",
    "criterion.landed.delivered_stage": "stage_question_key, via the whole LandedSpec "
                                        "object",
    "criterion.landed.remote": "stage_question_key, via the whole LandedSpec object",
    "criterion.verify_venue_at_final": "stage_question_key",
    "criterion.observation": "outside: what a reviewer actually SAW — an execution "
                             "record, and the one this path most exists to preserve",
    "principle.statement": "stage_question_key",
    "principle.source": "stage_question_key",
    "principle.derivation": "stage_question_key",
    "principle.confidence": "stage_question_key",
    "principle.refutation": "stage_question_key",
}


def test_the_stage_residual_exhausts_the_stage_s_field_set():
    """THE STAGE-SIDE TOTALITY CLAIM, made true. `renormalization_blockers` says its
    residual covers every field of a stage's definition, so an edit a new sequence does
    not account for is refused whether or not anyone listed it. That claim was FALSE as
    first written: the residual was `stage_question_key` alone, whose scope is what a
    `Question.target` may legally name, and two engine-consumed fields sit outside it —
    `actor.cost_tier` (the dispatch budget and the effort-divergence estimate) and
    `output_artifacts` (what the reachability lint reads as produced). Both passed the
    light path.

    The domain is derived by `dataclass_domain.leaf_paths`, genuinely recursively — an
    earlier draft expanded `_STAGE_CLUSTERS` one level by hand and so never reached
    `criterion.landed`'s own three fields, `outcome`'s seven, or `supplies`'s three;
    a LandedSpec-only mutation would have passed that shallower version's own totality
    check while never being exercised by anything downstream. The membership map itself
    is still hand-written, as `order_place`'s is and for the same reason, so like
    `test_order_place_exhausts_the_order_s_field_set` this is what makes the claim true
    rather than restating it: add a field to Stage at any depth and this goes red until
    someone decides, in writing, which side it is on."""
    leaves = set(leaf_paths(Stage))

    assert leaves == set(_STAGE_RESIDUAL_COVERAGE), (
        f"a Stage field is in neither the renormalization residual nor its named "
        f"exclusions: {sorted(leaves - set(_STAGE_RESIDUAL_COVERAGE))} unaccounted, "
        f"{sorted(set(_STAGE_RESIDUAL_COVERAGE) - leaves)} listed but gone. Decide "
        f"whether an edit to it is a re-sequencing (it is not, almost always), splice "
        f"it into gates._renorm_stage_residual, and record the decision here"
    )


def test_the_one_level_expansion_would_have_missed_a_landedspec_only_mutation():
    """The regression this stage's recursive switch guards against, made concrete. The
    OLD domain (`_STAGE_CLUSTERS` expanded one level by hand) named `criterion.landed`
    as a single leaf — so a hand-maintained coverage map only had to account for the
    LandedSpec object AS A WHOLE, and a change touching only `LandedSpec.remote` (say)
    was never separately demanded of anyone. The new domain forces exactly that: each of
    LandedSpec's three fields is its own leaf, individually listed in
    `_STAGE_RESIDUAL_COVERAGE`."""
    old_one_level_domain = set()
    for f in dataclasses.fields(Stage):
        cluster = _STAGE_CLUSTERS.get(f.name)
        old_one_level_domain |= ({f"{f.name}.{g.name}" for g in dataclasses.fields(cluster)}
                                 if cluster else {f.name})
    new_domain = set(leaf_paths(Stage))

    assert "criterion.landed" in old_one_level_domain
    assert "criterion.landed" not in new_domain
    assert {"criterion.landed.target", "criterion.landed.delivered_stage",
            "criterion.landed.remote"} <= new_domain


def test_the_meta_residual_exhausts_plan_meta_s_field_set():
    """THE PLAN-SIDE TOTALITY CLAIM, in the shape `test_order_place_exhausts_the_order_s_
    field_set` established — an arity check, because `_meta_place`'s members are
    normalized and no longer carry their field names.

    The stage half had a residual from the start; the meta half was four named rows plus
    the final-check and order surfaces, and a bare enumeration is what a light path must
    not rest on. `weight_class` was the field that showed it: the grade the whole
    approval spine keys on, in no row, so an offered plan re-declaring a substantive
    session's plan as `small_change` read as a re-sequencing."""
    fields = {f.name for f in dataclasses.fields(PlanMeta)}
    place = gates._meta_place(PlanMeta(task_id="t"))

    assert len(place) == len(fields), (
        f"_meta_place carries {len(place)} member(s) for {len(fields)} PlanMeta field(s) "
        f"({sorted(fields)}). A field added to PlanMeta and not listed there is outside "
        f"the renormalization residual: an executor can edit it under --renormalize and "
        f"the engine will call it a re-sequencing. Add its normalized form to "
        f"gates._meta_place and pin the new count here"
    )


@pytest.mark.parametrize("old,new,expected", [
    # the named rows — each carries its own message, and this is what a revert of the
    # [meta] block takes down (before these, deleting the whole block left the suite green)
    ('goal = "g"', 'goal = "a different goal"', "`goal`"),
    ('done_criterion = "dc"', 'done_criterion = "the reviewer likes it"',
     "`done_criterion`"),
    ('text = "the fixture plan meets the substantive grade"',
     'text = "the fixture plan meets some other grade entirely"', "[meta.order]"),
    ('command = "true"', 'command = "false"', "`final_check`"),
    # the residual — fields no row names, and the reason the rows may stay a short list
    ('weight_class = "substantive"', 'weight_class = "small_change"', "[meta] something"),
    ('external_research = "none applies"',
     'external_research = "searched the wiki; nothing applies"', "[meta] something"),
])
def test_renormalize_refuses_an_edit_to_the_plan_level(store, tmp_path, old, new,
                                                       expected):
    """The [meta] half of the refusal, pinned. Nothing here was covered by a test before
    this stage's review: the whole block could be deleted from `renormalization_blockers`
    and the suite stayed green, which for a gate is the same as not having it.

    The last two rows are the residual's own, and `weight_class` is why the residual
    exists. It is the plan's GRADE — what decides that this session needs an approval
    gate at all — and it sat in no named row, so a plan offered with
    `weight_class = "small_change"` and one re-sequenced stage passed as a
    renormalization. An executor could downgrade the plan out of the spine under the one
    branch built to be usable without asking anyone."""
    plan = _write_plan(tmp_path / "p.toml")
    _approved(store, plan)

    d = _renormalize(store, _edited(plan, tmp_path / "q.toml", old, new))

    assert d.ok is False, d.data
    assert any(expected in b for b in d.data["blockers"]), d.data["blockers"]
    assert store.load("rn").plan_path == plan


def test_renormalize_refuses_a_stage_field_outside_the_question_key(store, tmp_path):
    """`cost_tier` is the concrete case must-fix 1 was found on: engine-consumed (the
    spawn budget, and the per-stage term of the effort-divergence estimate) but outside
    `stage_question_key`, because no `Question.target` may name it. Re-tiering a stage
    from small to large moves the norm a stage's overrun is measured against, and it is
    not a sequence of operations by any reading."""
    plan = _write_plan(tmp_path / "p.toml")
    _approved(store, plan)

    d = _renormalize(store, _edited(plan, tmp_path / "q.toml",
                                    'capability_required = "cap"\nmaterial_refs',
                                    'capability_required = "cap"\ncost_tier = "large"\n'
                                    'material_refs'))

    assert d.ok is False, d.data
    assert any("other than `means.procedure`" in b for b in d.data["blockers"]), (
        d.data["blockers"]
    )


def test_renormalize_refuses_an_output_artifacts_re_declaration(store, tmp_path):
    """`output_artifacts` is the residual's OTHER splice, and had no behavioral test of
    its own before this stage's review — only `cost_tier`'s case above did, so a revert
    of the `output_artifacts` line in `_renorm_stage_residual` would have stayed green.

    Engine-consumed the same way `cost_tier` is: the verify-command reachability lint
    reads it as what a stage produces, and no `Question.target` may name it, so
    `stage_question_key` alone would wave a re-declaration through as a re-sequencing."""
    plan = _write_plan(tmp_path / "p.toml")
    _approved(store, plan)

    d = _renormalize(store, _edited(plan, tmp_path / "q.toml",
                                    'capability_required = "cap"\nmaterial_refs',
                                    'capability_required = "cap"\n'
                                    'output_artifacts = ["scripts/agentctl/gates.py"]\n'
                                    'material_refs'))

    assert d.ok is False, d.data
    assert any("other than `means.procedure`" in b for b in d.data["blockers"]), (
        d.data["blockers"]
    )


def test_renormalize_refuses_a_plan_that_fails_submission(store, tmp_path):
    """The early return the branch opens with, pinned: the offered plan is held to the
    SAME submission grade as any other, so the light path cannot be the way a plan
    missing a required place gets into a session. Here stage 2 offers no `procedure` at
    all — under a branch whose entire subject is the procedure.

    The refusal is `fix_plan`, not `replan`: nothing about a norm was touched, so telling
    the author to re-arm a review would name the wrong repair."""
    plan = _write_plan(tmp_path / "p.toml")
    _approved(store, plan)

    d = _renormalize(store, _write_plan(tmp_path / "q.toml", procedure=""))

    assert d.ok is False
    assert d.action == "fix_plan"
    assert any("procedure" in p for p in _problems(d)), _problems(d)
    assert store.load("rn").plan_path == plan


def test_renormalize_is_not_blocked_by_the_closure_preconditions(store, tmp_path):
    """WHERE the branch sits in `cmd_replan`, pinned. The normalization and
    failure-address gates are preconditions of CLOSING a difficulty — of leaving the
    DIAGNOSING cycle — and this path explicitly does not leave it. Behind them, an
    executor inside an open difficulty had to buy a re-norming (or spend a
    `--normalization-waiver`, which would then be logged against a call that discharged
    nothing) before he could reorder his own operations: the exact toll the field split
    exists to remove, reimposed at the one moment re-sequencing is most likely.

    `difficulty_blockers` stays ABOVE the branch and is not the same case: it refuses to
    let a whole plan be offered while the difficulty record is still incomplete, which is
    how a difficulty gets replanned away instead of worked through."""
    plan = _write_plan(tmp_path / "p.toml")
    _approved(store, plan)
    cli.cmd_partition(ns(session="rn", m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session="rn"), store=store)
    cli.cmd_record_result(ns(session="rn", status="failed", actual="boom"), store=store)
    cli.cmd_declare(ns(session="rn", expected="e", actual="a", mismatch="m"), store=store)
    cli.cmd_investigate(ns(session="rn", localized_expectation="le", localized_actual="la",
                           hypotheses=["h1", "h2"]), store=store)
    cli.cmd_critique(ns(session="rn", functional_ground="fg", replanning_task="rt",
                        failure_address="нормативное"), store=store)
    assert gates.normalization_blockers(store.load("rn")), (
        "the closure gate must be armed, or this test pins nothing"
    )

    d = _renormalize(store, _write_plan(
        tmp_path / "q.toml",
        procedure='procedure = "1. Read the seam. 2. Add the row. 3. Run pytest -q."\n'))

    assert d.ok is True, d.data
    state = store.load("rn")
    assert state.node == "DIAGNOSING", "a renormalization does not leave the cycle"
    assert "normalization_waived" not in [h.get("event") for h in state.history]


def test_renormalize_backfills_a_legacy_session_s_approved_snapshot(store, tmp_path):
    """Successive renormalizations are measured against the APPROVED bytes — that is what
    stops a norm edit being walked to in small steps, each step passing because it is
    small. On a session with no snapshot the baseline falls back to `plan_path`, which
    this branch rewrites on every accepted call, so on exactly those sessions the walk
    was open. The branch now backfills the snapshot before rewriting, as cmd_replan's
    no_change branch already does."""
    plan = _write_plan(tmp_path / "p.toml")
    state = _approved(store, plan)
    state.plan_snapshot_path = None
    state.plan_snapshot_hash = None
    store.save(state)

    d = _renormalize(store, _write_plan(
        tmp_path / "q.toml",
        procedure='procedure = "1. Read the seam. 2. Add the row. 3. Run pytest -q."\n'))
    assert d.ok is True, d.data
    snap = store.load("rn").plan_snapshot_path
    assert snap and Path(snap).read_text(encoding="utf-8") == Path(plan).read_text(
        encoding="utf-8"), "the snapshot must hold the APPROVED bytes, not the new ones"

    # …and the baseline is now those bytes: a second call may still only re-sequence.
    d2 = _renormalize(store, _write_plan(tmp_path / "r.toml", title="a better name"))
    assert d2.ok is False
    assert any("other than `means.procedure`" in b for b in d2.data["blockers"])


def test_renormalize_leaves_a_passed_stage_s_recorded_observation_alone(store, tmp_path):
    """A renormalization copies ONLY `means.procedure` onto the live stages — never
    `_apply_refined_stage_fields`. That is what keeps the branch's own claim true of the
    live state, and what protects the recorded comparison stage 8 requires: an
    observation records what the reviewer actually SAW, and no amount of re-sequencing
    the operations ahead makes what he saw untrue."""
    plan = _write_plan(tmp_path / "p.toml")
    state = _approved(store, plan)
    stage = state.stage(1)
    stage.outcome.status = StageStatus.PASSED.value
    stage.criterion.observation = "ran pytest -q on scripts/tests; 4226 passed"
    store.save(state)

    d = _renormalize(store, _write_plan(
        tmp_path / "q.toml",
        procedure='procedure = "1. Add the row. 2. Write the _WHY entry. 3. Run the suite."\n',
    ))

    assert d.ok is True, d.data
    after = store.load("rn").stage(1)
    assert after.outcome.status == StageStatus.PASSED.value
    assert after.criterion.observation == "ran pytest -q on scripts/tests; 4226 passed"


def test_renormalize_refuses_a_plan_that_adds_a_stage(store, tmp_path):
    """Re-sequencing the operations INSIDE a stage is the executor's; re-cutting the work
    into stages is the plan's. Without this the flag would admit a whole new stage — with
    its own criterion, its own actor and its own budget — under the heading of a
    sequence edit."""
    plan = _write_plan(tmp_path / "p.toml")
    _approved(store, plan)

    text = Path(_write_plan(tmp_path / "q.toml")).read_text(encoding="utf-8")
    added = tmp_path / "r.toml"
    added.write_text(text.replace(
        SUBSTANTIVE_FINAL_CHECK,
        """
[[stage]]
index = 3
title = "a stage nobody approved"
executor = "in_thread"
expected_result_image = "something new happens"
criterion_type = "measurable"
done_criterion = "d3"
verify_command = "pytest -q"
material = "m3"
means = "bash"
method = "Add the new module beside its siblings."
procedure = "1. Write the module. 2. Run the suite."
conditions = "c"
preconditions = "p"
invariants = "i"
capability_required = "cap"
material_refs = ["scripts/agentctl/render.py"]
knowledge_refs = ["scripts/agentctl/cli.py"]
knowledge = "k"
[[stage.supplies]]
on = 2
element = "result"
[stage.principle]
statement = "s"
source = "src"
derivation = "der"
confidence = "high"
refutation = "r"
""" + SUBSTANTIVE_FINAL_CHECK,
    ), encoding="utf-8")

    d = _renormalize(store, str(added))

    assert d.ok is False
    assert any("may not add or remove a stage" in b for b in d.data["blockers"]), (
        d.data["blockers"]
    )


def test_a_replan_without_the_flag_is_unchanged(store, tmp_path):
    """The branch is opt-in and reached only through the flag, so every existing replan
    — and every existing test of one — must be byte-identical. A procedure-only edit
    submitted WITHOUT `--renormalize` is an ordinary refinement, gates and all."""
    plan = _write_plan(tmp_path / "p.toml")
    _approved(store, plan)

    d = cli.cmd_replan(
        ns(session="rn", plan=_write_plan(
            tmp_path / "q.toml",
            procedure='procedure = "1. Add the row. 2. Write the _WHY entry. 3. Run the suite."\n'),
           coverage_waiver=None, normalization_waiver=None, cost_log=None),
        store=store, runner=_judge_unavailable,
    )

    assert d.ok is True
    assert "renormalize" not in [h.get("event") for h in store.load("rn").history]
