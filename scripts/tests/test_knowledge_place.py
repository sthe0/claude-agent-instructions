"""The знание place, the submission seam that requires it, and the destination axis.

Defect 1 of the SMD act-modelling rework: the engine had no functional place for what a
stage must already KNOW. Knowledge was reachable only as a `supplies` element with no
element to supply, and the one place that looked like it — `Normalization.level` — was a
scale of how generally a lesson is written down, not of what the lesson repairs, so
"re-norm the знание" had nowhere to land except by polluting that scale.

Three claims are under test here, and each has a control that goes red if the production
side is reverted:

  1. `knowledge` (plus its two ref projections, `material_refs` / `knowledge_refs`) is a
     required place of a SUBSTANTIVE stage — required at the SUBMISSION SEAM, never in
     `parse_plan`'s `if strict:` branches. The loader must stay exactly as permissive as
     it was, or every live session re-reading a plan it already accepted starts failing.
     `test_submission_seam_refuses_what_the_loader_still_accepts` is the hinge: it asserts
     BOTH directions over one fixture, so moving the requirement into the loader turns it
     red on its first assertion rather than passing quietly.

  2. Every new field enters all FOUR change-decision functions in this same commit —
     `stage_carry_key`, `_apply_refined_stage_fields`, `diff_plans`' prose tuple,
     `stage_question_key`. A field added to the struct alone is invisible to the engine:
     a knowledge-only correction (exactly the edit an overcome-difficulty replan makes
     when the fault addressed знание) would diff to "no_change" and be silently dropped.
     The identity direction matters just as much — a stage declaring NONE of the three
     must produce the byte-identical schema-23 `stage_question_key`, because that digest
     is persisted in `Question.disposed_at_key` and compared across processes.

  3. `NORMALIZATION_DESTINATIONS` is its own closed axis, sharing no member with
     `NORMALIZATION_LEVELS`, and `--destination` is orthogonal to `--level`.
"""
from __future__ import annotations

import hashlib
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, text_shape
from agentctl.plan import (
    PlanError,
    diff_plans,
    knowledge_place,
    load_plan,
    stage_carry_key,
    stage_question_key,
)
from agentctl.state import (
    NORMALIZATION_DESTINATIONS,
    NORMALIZATION_LEVELS,
    Actor,
    Criterion,
    Means,
    Node,
    Stage,
    StageStatus,
    Subject,
)
from agentctl.submission import submission_violations, validate_submission

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CORPUS_DIR = FIXTURES / "plan_corpus"
NO_KNOWLEDGE_PLAN = FIXTURES / "submission" / "stage_without_knowledge.toml"


def ns(**kw):
    return Namespace(**kw)


# A minimal SUBSTANTIVE plan that passes today's submission grade. `knowledge_block` is
# spliced rather than baked in so every test below can knock out exactly one place and
# leave the rest of the plan identical — the difference between a control that proves a
# requirement and one that proves the plan was malformed in some unrelated way.
_PLAN = """
[meta]
task_id = "kp"
goal = "g"
done_criterion = "dc"
criterion_type = "measurable"
weight_class = "substantive"
external_research = "checked the engine's own call sites; no prior art applies"

[[stage]]
index = 1
title = "{title}"
executor = "in_thread"
expected_result_image = "i"
criterion_type = "measurable"
done_criterion = "d"
verify_command = "pytest -q"
material = "m"
means = "bash"
method = "run"
conditions = "c"
invariants = "n"
capability_required = "cap"
{knowledge_block}
[stage.principle]
statement = "s"
source = "src"
derivation = "der"
confidence = "high"
refutation = "r"
"""

_FULL_KNOWLEDGE_BLOCK = (
    'material_refs = ["scripts/agentctl/plan.py"]\n'
    'knowledge_refs = ["scripts/agentctl/submission.py"]\n'
    'knowledge = "how the submission seam differs from the loader"\n'
)


def _write_plan(path: Path, *, knowledge_block=_FULL_KNOWLEDGE_BLOCK, title="s1") -> str:
    path.write_text(
        _PLAN.format(knowledge_block=knowledge_block, title=title), encoding="utf-8"
    )
    return str(path)


def _stage(*, knowledge=None, material_refs=(), knowledge_refs=()) -> Stage:
    return Stage(
        index=1, title="s1",
        subject=Subject(material="m", result="i", invariants="n",
                        material_refs=list(material_refs),
                        knowledge_refs=list(knowledge_refs)),
        means=Means(means="bash", method="run"),
        actor=Actor(executor="in_thread", capability_required="cap"),
        criterion=Criterion(criterion_type="measurable", done_criterion="d",
                            verify_command="pytest -q"),
        conditions="c",
        knowledge=knowledge,
    )


def _to_plan_ready(store, sid, plan):
    cli.cmd_start(ns(session=sid, task="kp", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    return cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)


# --- the vocabulary ---------------------------------------------------------


def test_element_names_admits_knowledge_as_a_supply_and_question_target():
    """`knowledge` must be in the vocabulary a Supply.element and a Question.target are
    validated against, or the place cannot be supplied by an earlier stage at all — which
    is the alternative the submission grade offers to declaring it locally."""
    assert "knowledge" in text_shape.ELEMENT_NAMES


def test_element_names_keeps_every_pre_existing_element():
    """Guards the addition against being a REPLACEMENT: the eight-element vocabulary the
    plan ontology already depends on must survive intact."""
    assert {
        "material", "result", "invariants", "means", "method", "executor",
        "capability", "criterion", "done_criterion", "principle", "conditions",
    } <= set(text_shape.ELEMENT_NAMES)


# --- claim 1: the seam, not the loader --------------------------------------


def test_submission_seam_refuses_what_the_loader_still_accepts():
    """THE HINGE. One fixture, both directions: `load_plan(strict=True)` accepts a
    substantive stage with no `knowledge`, and `validate_submission` refuses the same
    bytes. Red on the first assertion if the requirement is ever moved into
    `parse_plan`'s `if strict:` branches — which would make it retroactive over every
    plan a live session re-reads."""
    doc = load_plan(str(NO_KNOWLEDGE_PLAN), strict=True)
    assert doc.stages[0].knowledge is None
    with pytest.raises(PlanError):
        validate_submission(doc)


def test_submission_seam_reports_every_missing_place_at_once(tmp_path):
    """`submission_violations` returns a LIST, not the first failure: the approve seam
    answers with a Directive, and one round trip should show the author everything to
    fix rather than one place per attempt."""
    plan = _write_plan(tmp_path / "bare.toml", knowledge_block="")
    problems = submission_violations(load_plan(plan))
    assert len(problems) == 3
    assert any("knowledge'" in p for p in problems)
    assert any("material_refs" in p for p in problems)
    assert any("knowledge_refs" in p for p in problems)


def test_submission_seam_is_vacuous_for_a_plan_that_is_not_substantive(tmp_path):
    """Keyed on the PLAN's own [meta] weight_class — the same key
    `plan._validate_substantive_stage` uses — so a plan that does not declare itself
    substantive is held to neither grade. Without this the requirement would reach every
    small-change plan in the corpus."""
    plan = tmp_path / "small.toml"
    plan.write_text(
        _PLAN.format(knowledge_block="", title="s1").replace(
            'weight_class = "substantive"', 'weight_class = "small_change"'
        ),
        encoding="utf-8",
    )
    assert submission_violations(load_plan(str(plan))) == []


def test_submission_seam_a_refuses_at_submit_plan(tmp_path, store):
    """Seam (a): first entry. The refusal stays at PLANNING with the approval gate
    unarmed, exactly like every other submit-time refusal."""
    plan = _write_plan(tmp_path / "bare.toml", knowledge_block="")
    d = _to_plan_ready(store, "seam-a", plan)
    assert d.ok is False
    assert d.action == "fix_plan"
    assert d.node == Node.PLANNING.value
    assert any("knowledge" in p for p in d.data.get("problems", []))


def test_submission_seam_accepts_a_knowledge_supply_edge_instead_of_a_local_place(
    tmp_path, store
):
    """The knowledge check runs AFTER supplies are built, so an incoming
    `element = "knowledge"` edge from an earlier stage fills the place. Evaluating it
    against the raw TOML table instead would reject the very composition the vocabulary
    addition exists to enable."""
    plan = tmp_path / "supplied.toml"
    plan.write_text(
        _PLAN.format(knowledge_block=_FULL_KNOWLEDGE_BLOCK, title="s1")
        + """
[[stage]]
index = 2
title = "s2"
executor = "in_thread"
expected_result_image = "i2"
criterion_type = "measurable"
done_criterion = "d2"
verify_command = "pytest -q"
material = "m2"
material_refs = ["scripts/agentctl/cli.py"]
knowledge_refs = ["scripts/agentctl/state.py"]
means = "bash"
method = "run"
conditions = "c"
invariants = "n"
capability_required = "cap"

[[stage.supplies]]
on = 1
element = "knowledge"

[stage.principle]
statement = "s2"
source = "src2"
derivation = "der2"
confidence = "high"
refutation = "r2"
""",
        encoding="utf-8",
    )
    doc = load_plan(str(plan))
    assert doc.stages[1].knowledge is None
    assert submission_violations(doc) == []
    assert _to_plan_ready(store, "seam-supply", str(plan)).ok is True


def test_approve_refresh_refuses_an_in_place_edit_as_a_directive_not_an_exception(
    tmp_path, store
):
    """Seam (c). PLAN_READY is deliberately plan-mutable (the plan-review cycle answers a
    REVISE verdict by editing the file), so the bytes approve is about to attest to may
    never have been through submission. The refusal MUST be a Directive: an exception
    escaping approve strands the session at PLAN_READY with the gate armed and no edge
    back. The same plan approves once corrected — the GREEN direction of the same
    control."""
    plan_path = tmp_path / "plan.toml"
    _write_plan(plan_path)
    assert _to_plan_ready(store, "seam-c", str(plan_path)).ok is True

    _write_plan(plan_path, knowledge_block="")  # the in-place edit
    d = cli.cmd_approve(ns(session="seam-c", by="user"), store=store)
    assert d.ok is False
    assert d.action == "fix_plan"
    assert d.node == Node.PLAN_READY.value
    assert any("knowledge" in p for p in d.data.get("problems", []))
    # submit-plan ARMED the gate; the refused approve must leave it unpassed rather than
    # recording an approval for bytes that never went through submission.
    assert store.load("seam-c").approval.passed is False

    _write_plan(plan_path)  # corrected
    d2 = cli.cmd_approve(ns(session="seam-c", by="user"), store=store)
    assert d2.ok is True
    assert store.load("seam-c").node == Node.APPROVED.value
    assert store.load("seam-c").approval.passed is True


def test_approve_refresh_leaves_the_session_untouched_when_it_refuses(tmp_path, store):
    """A rejected approve must not also be a half-applied edit: violations are computed
    BEFORE any mutation, so the live stage still carries the bytes it was submitted
    with."""
    plan_path = tmp_path / "plan.toml"
    _write_plan(plan_path)
    _to_plan_ready(store, "seam-c2", str(plan_path))

    plan_path.write_text(
        _PLAN.format(knowledge_block="", title="RETITLED"), encoding="utf-8"
    )
    cli.cmd_approve(ns(session="seam-c2", by="user"), store=store)
    assert store.load("seam-c2").stage(1).title == "s1"


def test_approve_refresh_reports_an_unloadable_plan_rather_than_swallowing_it(
    tmp_path, store
):
    """The old silent return let approve pass on the stale pre-edit cache — the
    "attests to a plan it never actually executes" failure the refresh exists to
    remove."""
    plan_path = tmp_path / "plan.toml"
    _write_plan(plan_path)
    _to_plan_ready(store, "seam-c3", str(plan_path))
    plan_path.write_text("this is not toml = = =", encoding="utf-8")

    d = cli.cmd_approve(ns(session="seam-c3", by="user"), store=store)
    assert d.ok is False
    assert any("cannot load the plan" in p for p in d.data.get("problems", []))


# --- claim 1b: the digest of the accepted bytes ------------------------------


def _digest(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_plan_digest_is_stamped_at_submit_plan(tmp_path, store):
    plan = _write_plan(tmp_path / "plan.toml")
    _to_plan_ready(store, "dig-a", plan)
    assert store.load("dig-a").plan_digest == _digest(plan)


def test_plan_digest_is_not_stamped_when_submission_refuses(tmp_path, store):
    """The field records the bytes that were ACCEPTED. Stamping a refused plan would
    make the digest attest to something the session never took on."""
    plan = _write_plan(tmp_path / "plan.toml", knowledge_block="")
    _to_plan_ready(store, "dig-x", plan)
    assert store.load("dig-x").plan_digest is None


def test_plan_digest_follows_an_in_place_edit_through_approve(tmp_path, store):
    """Seam (c) restamps: approve attests to the bytes on disk NOW, not the bytes
    submit-plan saw."""
    plan_path = tmp_path / "plan.toml"
    _write_plan(plan_path)
    _to_plan_ready(store, "dig-c", str(plan_path))
    before = store.load("dig-c").plan_digest

    _write_plan(plan_path, title="edited at PLAN_READY")
    cli.cmd_approve(ns(session="dig-c", by="user"), store=store)
    after = store.load("dig-c").plan_digest
    assert after != before
    assert after == _digest(plan_path)


# --- claim 2: every new field enters all four change-decision functions -------


def test_carry_key_moves_when_knowledge_moves():
    """`stage_carry_key` decides whether a substantive replan KEEPS a stage's PASSED
    outcome. A stage whose знание was rewritten is no longer the stage that passed."""
    assert stage_carry_key(_stage(knowledge="a")) != stage_carry_key(_stage(knowledge="b"))


def test_carry_key_moves_when_material_refs_move():
    assert stage_carry_key(_stage(material_refs=["a"])) != stage_carry_key(
        _stage(material_refs=["b"])
    )


def test_carry_key_moves_when_knowledge_refs_move():
    assert stage_carry_key(_stage(knowledge_refs=["a"])) != stage_carry_key(
        _stage(knowledge_refs=["b"])
    )


def test_question_key_moves_when_knowledge_moves():
    """`knowledge` is a legal Question.target, so a question disposed against the old
    знание must be invalidated when it is rewritten."""
    assert stage_question_key(_stage(knowledge="a")) != stage_question_key(
        _stage(knowledge="b")
    )


def test_question_key_moves_when_material_refs_move():
    """`material_refs` is `material`'s structural projection — redrawing the refs
    changes what a question about the material was answered against, even when the prose
    `material` is untouched."""
    assert stage_question_key(_stage(material_refs=["a"])) != stage_question_key(
        _stage(material_refs=["b"])
    )


def test_question_key_moves_when_knowledge_refs_move():
    assert stage_question_key(_stage(knowledge_refs=["a"])) != stage_question_key(
        _stage(knowledge_refs=["b"])
    )


def test_question_key_does_not_collide_across_the_three_new_fields():
    """Why the знание place contributes as ONE grouped element rather than three
    independent conditional splices: spliced field-by-field, (knowledge='x', refs empty)
    and (knowledge=None, material_refs=['x']) both flatten to ('x',) and become the same
    key."""
    keys = {
        stage_question_key(_stage(knowledge="x")),
        stage_question_key(_stage(material_refs=["x"])),
        stage_question_key(_stage(knowledge_refs=["x"])),
    }
    assert len(keys) == 3


def test_knowledge_place_is_empty_when_nothing_is_declared():
    """The identity that makes the schema-23 freeze below possible."""
    assert knowledge_place(_stage()) == ()
    assert knowledge_place(_stage(knowledge="x")) != ()


@pytest.mark.parametrize(
    "plan_path", sorted(CORPUS_DIR.glob("*.toml")), ids=lambda p: p.name
)
def test_question_key_is_byte_identical_to_schema_23_across_the_frozen_corpus(plan_path):
    """THE IDENTITY DIRECTION, frozen against the stage-1 corpus fixture. Every plan in
    the corpus predates this stage and therefore declares none of the three new fields;
    each must still produce exactly the digest it produced before the place existed.

    The schema-23 formula is restated here deliberately rather than imported: importing
    the production function would make the test compare it against itself. A `... or ""`
    default, or an unconditional contribution, turns this red for all 55 plans — which is
    the point, because `stage_question_key` is persisted in `Question.disposed_at_key` and
    a changed digest lights a spurious `plan_approval` blocker in every live session.
    """
    # Imported locally to mark it as deliberate: the restated formula below needs the
    # production normalizer, but nothing else in this module should reach for a private.
    from agentctl.plan import _normalize_string

    doc = load_plan(str(plan_path), strict=False)
    for s in doc.stages:
        assert knowledge_place(s) == (), (
            f"{plan_path.name} stage {s.index} declares a new field; the corpus is "
            "frozen and must not be edited — see test_frozen_plan_compat.py"
        )
        principle = s.principle
        principle_tuple = (
            (principle.statement, principle.source, principle.derivation,
             principle.confidence, principle.refutation)
            if principle is not None else None
        )
        payload = repr((
            s.actor.executor,
            s.actor.capability_required,
            tuple(sorted(s.depends_on)),
            s.criterion.done_criterion,
            s.criterion.criterion_type,
            s.criterion.verify_command,
            s.criterion.expected_exit,
            s.title,
            s.subject.material,
            s.subject.result,
            s.subject.invariants,
            s.means.means,
            s.means.method,
            s.conditions,
            principle_tuple,
            tuple((sup.on, sup.element, sup.artifact) for sup in s.supplies),
            _normalize_string(s.criterion.verify_venue),
            _normalize_string(s.criterion.verify_kind),
            s.criterion.landed,
            *((_normalize_string(s.criterion.verify_venue_at_final),)
              if s.criterion.verify_venue_at_final else ()),
        ))
        schema_23 = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        assert stage_question_key(s) == schema_23


def test_diff_plans_calls_a_knowledge_only_edit_a_refinement(tmp_path):
    """Without the знание place in `diff_plans`' prose tuple, a knowledge-only correction
    — the exact edit an overcome-difficulty replan makes when the fault addressed знание —
    diffs to "no_change" and the corrected знание is silently dropped."""
    old = load_plan(_write_plan(tmp_path / "old.toml"))
    new = load_plan(
        _write_plan(
            tmp_path / "new.toml",
            knowledge_block=(
                'material_refs = ["scripts/agentctl/plan.py"]\n'
                'knowledge_refs = ["scripts/agentctl/submission.py"]\n'
                'knowledge = "REVISED: the loader is re-entered, the seam is not"\n'
            ),
        )
    )
    assert diff_plans(old, new) == "refinement"


def test_diff_plans_calls_a_material_refs_only_edit_a_refinement(tmp_path):
    old = load_plan(_write_plan(tmp_path / "old.toml"))
    new = load_plan(
        _write_plan(
            tmp_path / "new.toml",
            knowledge_block=(
                'material_refs = ["scripts/agentctl/cli.py"]\n'
                'knowledge_refs = ["scripts/agentctl/submission.py"]\n'
                'knowledge = "how the submission seam differs from the loader"\n'
            ),
        )
    )
    assert diff_plans(old, new) == "refinement"


def test_refs_projection_reaches_live_state_through_the_approve_refresh(tmp_path, store):
    """The fourth change-decision function, `_apply_refined_stage_fields`: a knowledge-only
    edit made at plan-mutable PLAN_READY must reach the live stage, or dispatch runs
    against a stage whose знание place is stale against the bytes approve attested to."""
    plan_path = tmp_path / "plan.toml"
    _write_plan(plan_path)
    _to_plan_ready(store, "refs-live", str(plan_path))

    _write_plan(
        plan_path,
        knowledge_block=(
            'material_refs = ["scripts/agentctl/state.py"]\n'
            'knowledge_refs = ["scripts/agentctl/gates.py"]\n'
            'knowledge = "REVISED after review"\n'
        ),
    )
    assert cli.cmd_approve(ns(session="refs-live", by="user"), store=store).ok is True

    live = store.load("refs-live").stage(1)
    assert live.knowledge == "REVISED after review"
    assert live.subject.material_refs == ["scripts/agentctl/state.py"]
    assert live.subject.knowledge_refs == ["scripts/agentctl/gates.py"]


def test_refs_projection_round_trips_through_the_toml_loader(tmp_path):
    """The flat stage shape must actually parse the two refs lists — a field defined on
    the dataclass but never read from the TOML would make every test above pass on
    hand-built Stages while every real plan silently carried empty lists."""
    doc = load_plan(_write_plan(tmp_path / "plan.toml"))
    assert doc.stages[0].subject.material_refs == ["scripts/agentctl/plan.py"]
    assert doc.stages[0].subject.knowledge_refs == ["scripts/agentctl/submission.py"]
    assert doc.stages[0].knowledge == "how the submission seam differs from the loader"


def test_refs_projection_survives_the_state_round_trip(tmp_path, store):
    """`Stage.from_dict` must read both refs and `knowledge` back, or every field above
    is lost the moment the session is persisted and re-loaded."""
    _to_plan_ready(store, "refs-rt", _write_plan(tmp_path / "plan.toml"))
    s = store.load("refs-rt").stage(1)
    assert s.knowledge == "how the submission seam differs from the loader"
    assert s.subject.material_refs == ["scripts/agentctl/plan.py"]
    assert s.subject.knowledge_refs == ["scripts/agentctl/submission.py"]


def test_carry_key_reset_re_arms_a_passed_stage_whose_knowledge_was_rewritten(
    tmp_path, store
):
    """The two claims meeting: because `knowledge` is in `stage_carry_key`, a PASSED
    stage whose знание was rewritten at PLAN_READY is reset to PENDING for
    re-verification instead of carrying an outcome that no longer attests to it."""
    plan_path = tmp_path / "plan.toml"
    _write_plan(plan_path)
    _to_plan_ready(store, "carry-key-rearm", str(plan_path))
    state = store.load("carry-key-rearm")
    state.stage(1).outcome.status = StageStatus.PASSED.value
    store.save(state)

    _write_plan(
        plan_path,
        knowledge_block=(
            'material_refs = ["scripts/agentctl/plan.py"]\n'
            'knowledge_refs = ["scripts/agentctl/submission.py"]\n'
            'knowledge = "REVISED"\n'
        ),
    )
    cli.cmd_approve(ns(session="carry-key-rearm", by="user"), store=store)
    assert (
        store.load("carry-key-rearm").stage(1).outcome.status == StageStatus.PENDING.value
    )


# --- claim 3: destination is its own axis ------------------------------------


def test_destination_axis_shares_no_member_with_the_level_axis():
    """The two answer different questions — `--level` how generally the record is written
    down, `--destination` what is being repaired — so an overlapping member would let a
    reader collapse them back into one scale, which is the defect this axis removes."""
    assert not (set(NORMALIZATION_DESTINATIONS) & set(NORMALIZATION_LEVELS))
    assert tuple(NORMALIZATION_LEVELS) == ("note", "leaf", "principle")


def test_destination_covers_both_halves_of_обеспечение():
    """ресурсное (материал / средство) and нормативное (норма / способ), plus знание —
    the place upstream of both. A destination set missing знание would leave the very
    re-norming this stage exists to make recordable with nowhere to land."""
    assert set(NORMALIZATION_DESTINATIONS) == {
        "материал", "средство", "норма", "способ", "знание",
    }


def _to_normalize_ready(store, sid, plan):
    cli.cmd_start(ns(session=sid, task="kp", goal="g", done_criterion="dc",
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
    cli.cmd_record_result(ns(session=sid, status="failed", actual="boom"), store=store)
    cli.cmd_declare(ns(session=sid, expected="e", actual="a", mismatch="m"), store=store)
    cli.cmd_investigate(ns(session=sid, localized_expectation="le", localized_actual="la",
                           hypotheses=["h1", "h2"]), store=store)
    cli.cmd_critique(ns(session=sid, functional_ground="fg", replanning_task="rt",
                        failure_address="нормативное"), store=store)


def test_submission_seam_b_refuses_on_the_new_side_of_replan(tmp_path, store):
    """Seam (b): the single `_load(args.plan)` before `diff_plans`, so all three diff
    outcomes are covered by ONE check. Placing it after the diff would let a `no_change`
    replan re-materialize live stages from unvalidated bytes — "unchanged" is no reason
    to let an unvalidated plan in, because the comparison baseline is the snapshot, not
    the file."""
    sid = "seam-b"
    _to_normalize_ready(store, sid, _write_plan(tmp_path / "plan.toml"))
    cli.cmd_normalize(ns(session=sid, factor="f", level="note", destination="знание"),
                      store=store)

    corrected = _write_plan(tmp_path / "corrected.toml", knowledge_block="")
    d = cli.cmd_replan(ns(session=sid, plan=corrected), store=store)
    assert d.ok is False
    assert d.action == "fix_plan"
    assert any("knowledge" in p for p in d.data.get("problems", []))
    assert store.load(sid).node == Node.DIAGNOSING.value


def test_plan_digest_follows_the_new_bytes_through_replan(tmp_path, store):
    sid = "dig-b"
    plan = _write_plan(tmp_path / "plan.toml")
    _to_normalize_ready(store, sid, plan)
    cli.cmd_normalize(ns(session=sid, factor="f", level="note", destination="знание"),
                      store=store)
    assert store.load(sid).plan_digest == _digest(plan)

    corrected = _write_plan(
        tmp_path / "corrected.toml",
        knowledge_block=(
            'material_refs = ["scripts/agentctl/plan.py"]\n'
            'knowledge_refs = ["scripts/agentctl/submission.py"]\n'
            'knowledge = "REVISED by the replan"\n'
        ),
    )
    cli.cmd_replan(ns(session=sid, plan=corrected), store=store)
    assert store.load(sid).plan_digest == _digest(corrected)


@pytest.mark.parametrize("destination", NORMALIZATION_DESTINATIONS)
@pytest.mark.parametrize("level", NORMALIZATION_LEVELS)
def test_destination_is_accepted_at_every_level(tmp_path, store, destination, level):
    """Orthogonality, exhaustively: every destination is recordable at every level. A
    dependency between the two would be the collapse back into one scale."""
    sid = f"dest-{level}-{destination}"
    _to_normalize_ready(store, sid, _write_plan(tmp_path / "plan.toml"))
    d = cli.cmd_normalize(
        ns(session=sid, factor="f", level=level, destination=destination), store=store
    )
    assert d.ok is True
    norm = store.load(sid).difficulty.normalization
    assert norm.level == level
    assert norm.destination == destination


def test_destination_rejects_a_non_member(tmp_path, store):
    """Closed vocabulary: a free-text destination would make the axis unusable for any
    aggregate reading, which is the only reason to record it as a field at all."""
    _to_normalize_ready(store, "dest-bad", _write_plan(tmp_path / "plan.toml"))
    d = cli.cmd_normalize(
        ns(session="dest-bad", factor="f", level=None, destination="знанне"), store=store
    )
    assert d.ok is False
    assert "--destination" in d.detail
    assert store.load("dest-bad").difficulty.normalization is None


def test_destination_is_reachable_from_the_command_line():
    """The axis is only usable if `normalize --destination` actually parses — an
    in-code-only validation would leave the field unreachable from the CLI the
    coordinator drives."""
    args = cli.build_parser().parse_args(
        ["normalize", "--session", "s", "--factor", "f", "--destination", "знание"]
    )
    assert args.destination == "знание"
    assert args.level is None  # orthogonal: neither implies the other


def test_destination_may_be_omitted(tmp_path, store):
    """Back-compat: every caller that predates the axis passes no --destination, and the
    normalization gate must still clear for them."""
    _to_normalize_ready(store, "dest-none", _write_plan(tmp_path / "plan.toml"))
    d = cli.cmd_normalize(ns(session="dest-none", factor="f", level="note"), store=store)
    assert d.ok is True
    assert store.load("dest-none").difficulty.normalization.destination is None
