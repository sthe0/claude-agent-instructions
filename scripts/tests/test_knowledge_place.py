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

from agentctl import cli, plugins, text_shape
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

from conftest import SUBSTANTIVE_FINAL_CHECK, SUBSTANTIVE_ORDER

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
""" + SUBSTANTIVE_ORDER + """
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
preconditions = "p"
invariants = "n"
capability_required = "cap"
{knowledge_block}
[stage.principle]
statement = "s"
source = "src"
derivation = "der"
confidence = "high"
refutation = "r"
""" + SUBSTANTIVE_FINAL_CHECK

_FULL_KNOWLEDGE_BLOCK = (
    'material_refs = ["scripts/agentctl/plan.py"]\n'
    'knowledge_refs = ["scripts/agentctl/submission.py"]\n'
    'knowledge = "how the submission seam differs from the loader"\n'
)


def _write_plan(path: Path, *, knowledge_block=_FULL_KNOWLEDGE_BLOCK, title="s1",
                suffix="", material="m", declare_weight_class=True,
                weight_class="substantive") -> str:
    text = _PLAN.format(knowledge_block=knowledge_block, title=title)
    if material != "m":
        text = text.replace('material = "m"\n', f'material = "{material}"\n')
    if not declare_weight_class:
        text = text.replace('weight_class = "substantive"\n', "")
    elif weight_class != "substantive":
        text = text.replace('weight_class = "substantive"',
                            f'weight_class = "{weight_class}"')
    path.write_text(text + suffix, encoding="utf-8")
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
preconditions = "p"
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
    assert store.load("dig-a").accepted_plan_digest == _digest(plan)


def test_plan_digest_is_not_stamped_when_submission_refuses(tmp_path, store):
    """The field records the bytes that were ACCEPTED. Stamping a refused plan would
    make the digest attest to something the session never took on."""
    plan = _write_plan(tmp_path / "plan.toml", knowledge_block="")
    _to_plan_ready(store, "dig-x", plan)
    assert store.load("dig-x").accepted_plan_digest is None


def test_plan_digest_follows_an_in_place_edit_through_approve(tmp_path, store,
                                                              monkeypatch):
    """Seam (c) restamps: approve attests to the bytes on disk NOW, not the bytes
    submit-plan saw — but only once approve has decided to accept them.

    The BLOCKED half is the discriminating one. Seam (c) lives inside the cache refresh,
    which runs BEFORE the plan_approval gate by contract, so a stamp placed there names the
    edited bytes to every blocker that follows — and a blocked approve then carries a digest
    for a plan the session refused. Like the seam-(b) control, the persisted value cannot
    show it (a blocked approve never saves), so the digest is read AT the plan_approval gate
    row, where the engine has not yet decided anything."""
    plan_path = tmp_path / "plan.toml"
    _write_plan(plan_path)
    _to_plan_ready(store, "dig-c", str(plan_path))
    before = store.load("dig-c").accepted_plan_digest

    _write_plan(plan_path, title="edited at PLAN_READY")

    seen = {}
    real = cli._log_gate

    def spy(state, gate, blockers, *, passed):
        seen.setdefault(gate, state.accepted_plan_digest)
        return real(state, gate, blockers, passed=passed)

    monkeypatch.setattr(cli, "_log_gate", spy)

    # an empty approver: a blocker composed AFTER the refresh, so it exercises exactly the
    # window between seam (c) and the gate's refusal
    blocked = cli.cmd_approve(ns(session="dig-c", by=""), store=store)
    assert blocked.ok is False
    assert seen["plan_approval"] == before
    assert store.load("dig-c").accepted_plan_digest == before

    monkeypatch.setattr(cli, "_log_gate", real)
    cli.cmd_approve(ns(session="dig-c", by="user"), store=store)
    after = store.load("dig-c").accepted_plan_digest
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
    assert store.load(sid).accepted_plan_digest == _digest(plan)

    corrected = _write_plan(
        tmp_path / "corrected.toml",
        knowledge_block=(
            'material_refs = ["scripts/agentctl/plan.py"]\n'
            'knowledge_refs = ["scripts/agentctl/submission.py"]\n'
            'knowledge = "REVISED by the replan"\n'
        ),
    )
    cli.cmd_replan(ns(session=sid, plan=corrected), store=store)
    assert store.load(sid).accepted_plan_digest == _digest(corrected)


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


# --- the seams' own invariants, review round 4 --------------------------------


def test_a_refused_replan_leaves_the_accepted_digest_unchanged(tmp_path, store,
                                                               monkeypatch):
    """`accepted_plan_digest` names bytes the session TOOK. Seam (b) stamped before the
    critique-coverage gate, so a replan the engine went on to refuse had already moved the
    digest onto the rejected file — a wrong answer to "which plan is this session running",
    and one that could never surface as a crash.

    The persisted assertion alone does NOT discriminate: the coverage refusal returns
    without `store.save`, so today the premature stamp is discarded on the way out. That is
    precisely the reviewer's point — the invariant rested on the ABSENCE of a save between
    the stamp and the refusal, which no test guarded and the next edit would have broken
    silently. So the control reads the live session AT the coverage gate, where the engine
    has not yet decided to accept anything."""
    sid = "dig-refused"
    plan = _write_plan(tmp_path / "plan.toml")
    _to_normalize_ready(store, sid, plan)
    cli.cmd_normalize(ns(session=sid, factor="f", level="note", destination="знание"),
                      store=store)
    # a similarity the corrected plan does not carry -> the coverage gate refuses
    cli.cmd_critique(ns(session=sid, functional_ground="fg", replanning_task="rt",
                        failure_address="нормативное",
                        invariants_to_preserve=["the seam never tightens the loader"],
                        differences_to_remove=[]), store=store)

    seen = {}
    real = cli._log_gate

    def spy(state, gate, blockers, *, passed):
        seen.setdefault(gate, state.accepted_plan_digest)
        return real(state, gate, blockers, passed=passed)

    monkeypatch.setattr(cli, "_log_gate", spy)

    corrected = _write_plan(tmp_path / "corrected.toml", title="a different title")
    d = cli.cmd_replan(ns(session=sid, plan=corrected), store=store)

    assert d.ok is False
    assert d.data.get("coverage_blockers")
    assert seen["replan_coverage"] == _digest(plan)
    assert store.load(sid).accepted_plan_digest == _digest(plan)


def test_a_no_change_replan_moves_plan_path_to_the_new_file(tmp_path, store):
    """"no_change" names the DIFF, not the file. The branch re-materializes every live
    stage, final_check and the venue from `args.plan`, so plan_path must follow those bytes
    too — otherwise the session runs one file's content while every later fresh load (the
    premise gate, the next replan's baseline) reads another, and the digest stamped on
    `args.plan` names bytes plan_path does not point at."""
    sid = "no-change-path"
    plan = _write_plan(tmp_path / "plan.toml")
    _to_normalize_ready(store, sid, plan)
    cli.cmd_normalize(ns(session=sid, factor="f", level="note", destination="знание"),
                      store=store)

    # same parsed content, different bytes and a different path: comment-only, so
    # diff_plans sees no_change while the two files are genuinely distinguishable
    corrected = _write_plan(tmp_path / "corrected.toml",
                            suffix="\n# a comment tomllib never surfaces as a field\n")
    d = cli.cmd_replan(ns(session=sid, plan=corrected), store=store)

    assert d.ok is True
    after = store.load(sid)
    assert after.plan_path == corrected
    assert after.accepted_plan_digest == _digest(corrected) != _digest(plan)


def test_a_no_change_replan_backfills_a_legacy_snapshot_from_the_new_file(tmp_path, store):
    """The one place the new `plan_path` assignment REORDERS an existing write. A legacy
    session with no approved-plan snapshot backfills one on this branch, and
    `_snapshot_approved_plan` reads `state.plan_path` — so the backfill now freezes
    `args.plan`'s bytes rather than the previous file's. That is the coherent reading (the
    session was just re-materialized from those bytes, and the snapshot is the NEXT replan's
    comparison baseline), but it is a behaviour nothing else asserts."""
    sid = "no-change-backfill"
    plan = _write_plan(tmp_path / "plan.toml")
    _to_normalize_ready(store, sid, plan)
    cli.cmd_normalize(ns(session=sid, factor="f", level="note", destination="знание"),
                      store=store)
    legacy = store.load(sid)
    legacy.plan_snapshot_path = None      # a session that predates the snapshot field
    legacy.plan_snapshot_hash = None
    store.save(legacy)

    corrected = _write_plan(tmp_path / "corrected.toml",
                            suffix="\n# a comment tomllib never surfaces as a field\n")
    d = cli.cmd_replan(ns(session=sid, plan=corrected), store=store)

    assert d.ok is True
    after = store.load(sid)
    assert after.plan_snapshot_hash == _digest(corrected)
    assert Path(after.plan_snapshot_path).read_bytes() == Path(corrected).read_bytes()


@pytest.fixture
def refresh_probe_plugin():
    """A plan_approval gate whose verdict depends on state REFRESHED at approve time.
    No production plugin reads refreshed state today (premise re-loads the file itself),
    so the widened ordering could only be asserted with a probe."""
    name = "kp_refresh_probe"
    plugins.register(plugins.Plugin(
        name=name,
        gates={"plan_approval": lambda state, bag: (
            [] if state.stage(1).title == "REFRESHED" else ["stage 1 is not REFRESHED"]
        )},
    ))
    yield name
    plugins.REGISTRY.pop(name, None)


def test_plan_approval_blockers_see_the_refreshed_state(tmp_path, store,
                                                        refresh_probe_plugin):
    """The contract widened at seam (c): not just the submission REFUSAL but the whole
    cache refresh precedes `_log_gate`, so every plan_approval blocker — core, plugin,
    review, presentation — judges the post-refresh session. That is the intent (the gate
    must judge the bytes it is about to attest to, not the pre-edit cache), and it is
    asserted here rather than left as an accident of statement order."""
    sid = "approve-refresh-order"
    plan_path = tmp_path / "plan.toml"
    _write_plan(plan_path)
    _to_plan_ready(store, sid, str(plan_path))
    state = store.load(sid)
    plugins.activate(state, refresh_probe_plugin)
    store.save(state)

    before = cli.cmd_approve(ns(session=sid, by="user"), store=store)
    assert any("not REFRESHED" in b for b in before.data.get("blockers", []))

    _write_plan(plan_path, title="REFRESHED")
    after = cli.cmd_approve(ns(session=sid, by="user"), store=store)
    assert not any("not REFRESHED" in b for b in after.data.get("blockers", []))


def test_a_substantive_session_refuses_a_plan_that_declares_no_weight_class(tmp_path,
                                                                            store):
    """The grade was author-opt-in: `weight_class` is optional free text, so a plan that
    simply omits it escaped the submission requirements entirely — while the gate right
    beside seam (a) (verify_command_reachability) armed on the SESSION's class. Silence is
    what the two disagreed about, so silence is what is refused."""
    plan = _write_plan(tmp_path / "plan.toml", knowledge_block="",
                       declare_weight_class=False)
    # the loader is untouched by this: it still accepts the same bytes
    assert load_plan(plan).meta.weight_class is None

    d = _to_plan_ready(store, "wc-omitted", plan)
    assert d.ok is False
    assert any("weight_class is not declared" in p for p in d.data.get("problems", []))
    assert store.load("wc-omitted").node == Node.PLANNING.value


def test_a_declared_non_substantive_plan_is_not_held_to_the_substantive_grade(tmp_path,
                                                                             store):
    """The other half of the contract, and the reason the seam is NOT widened to arm on
    the session: the grade itself stays keyed on the plan, in agreement with
    `plan._validate_substantive_stage`. A plan that says what it is has said enough."""
    text = Path(_write_plan(tmp_path / "plan.toml", knowledge_block="",
                            declare_weight_class=False)).read_text(encoding="utf-8")
    plan = tmp_path / "small.toml"
    plan.write_text(text.replace("[meta]\n", '[meta]\nweight_class = "small_change"\n'),
                    encoding="utf-8")
    d = _to_plan_ready(store, "wc-small", str(plan))
    assert d.ok is True
    assert store.load("wc-small").node == Node.PLAN_READY.value


def test_the_seam_stays_a_pure_function_of_its_arguments(tmp_path, store):
    """The session's class is threaded in as a VALUE, not a SessionState: with no session
    supplied the seam reduces EXACTLY to the plan-declared behaviour, so every caller that
    has no session (tooling, tests, validate_submission on a bare doc) is unchanged."""
    doc = load_plan(_write_plan(tmp_path / "plan.toml", knowledge_block="",
                                declare_weight_class=False))
    assert submission_violations(doc) == []
    assert submission_violations(doc, session_weight_class="SMALL_CHANGE") == []
    problems = submission_violations(doc, session_weight_class="SUBSTANTIVE")
    # ONE round trip: the missing declaration AND every place the grade will require once
    # it is declared. Returning the declaration alone would make the author fix one layer,
    # resubmit, and only then learn about the next — the two-round-trip shape the module's
    # own contract ("every violation at once") exists to rule out.
    assert "weight_class is not declared" in problems[0]
    assert len(problems) == 4
    assert any("knowledge'" in p for p in problems[1:])
    assert any("material_refs" in p for p in problems[1:])
    assert any("knowledge_refs" in p for p in problems[1:])


def test_an_empty_refs_list_is_reported_as_the_empty_case(tmp_path, store):
    """`material_refs = []` is falsy, so it is refused like an absent key — correct, but
    the author reading "missing 'material_refs'" over a key they can see in their own file
    has no way to tell which. The reason names the empty case explicitly."""
    doc = load_plan(_write_plan(
        tmp_path / "plan.toml",
        knowledge_block=('material_refs = []\n'
                         'knowledge_refs = ["scripts/agentctl/submission.py"]\n'
                         'knowledge = "k"\n'),
    ))
    problems = submission_violations(doc)
    assert len(problems) == 1
    assert "material_refs" in problems[0]
    assert "empty list" in problems[0]


def test_the_approve_refresh_carries_material(tmp_path, store):
    """`subject.material` is read by no change-decision function, so the refresh could
    leave it pinned to the pre-edit bytes while every sibling prose field tracked the file
    — a discrepancy in what `status` renders with no reason behind it."""
    sid = "refresh-material"
    plan_path = tmp_path / "plan.toml"
    _write_plan(plan_path)
    _to_plan_ready(store, sid, str(plan_path))
    assert store.load(sid).stage(1).subject.material == "m"

    _write_plan(plan_path, material="REWRITTEN at PLAN_READY")
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    assert store.load(sid).stage(1).subject.material == "REWRITTEN at PLAN_READY"


# --- the seams' own invariants, review round 5 --------------------------------


def test_a_state_written_before_the_digest_rename_still_loads():
    """`SessionState.from_dict` ends in `cls(**data)` and filters nothing, so renaming a
    persisted field without a migration turns every state.json written by the previous
    version into a `TypeError` on load with no recovery edge. The value carries over rather
    than being dropped: it is not recomputable from anything else on the state."""
    from agentctl.state import SessionState

    raw = SessionState(session_id="pre-rename", task_id="t").to_dict()
    raw.pop("accepted_plan_digest")
    raw["plan_digest"] = "deadbeef"  # exactly how the previous commit spelled it

    back = SessionState.from_dict(raw)
    assert back.accepted_plan_digest == "deadbeef"
    assert not hasattr(back, "plan_digest")


@pytest.mark.parametrize("declared", ["chat", "small_change", "substantive"])
def test_every_member_of_the_closed_vocabulary_is_accepted(tmp_path, declared):
    """The refusal below is of values OUTSIDE the vocabulary. A plan declaring any member
    is making a claim its author can be held to — including `small_change`, which twelve
    fixtures use deliberately."""
    doc = load_plan(_write_plan(tmp_path / f"{declared}.toml", weight_class=declared))
    assert submission_violations(doc, session_weight_class="SUBSTANTIVE") == []


def test_a_misspelled_weight_class_is_refused_rather_than_read_as_non_substantive(tmp_path):
    """The declaration is the sole load-bearing key of the whole substantive grade, and it
    was unvalidated free text: `"substantiv"` read as "not substantive" at BOTH enumerators
    and escaped the grade silently, while `_UNDECLARED` stayed quiet because the key is
    there. A typo is nobody's claim, so it is refused rather than honoured."""
    doc = load_plan(_write_plan(tmp_path / "typo.toml", weight_class="substantiv"))
    problems = submission_violations(doc)
    assert len(problems) == 1
    assert "'substantiv'" in problems[0]
    assert "'small_change'" in problems[0]  # the vocabulary is named, not just refused


def test_a_weight_class_with_whitespace_is_refused_where_the_enumerators_would_split(
    tmp_path,
):
    """`"substantive "` is the case the two halves of one grade disagree about: this seam
    normalizes, and `plan._validate_substantive_stage` — a bare `.lower()` on the loader
    path this module must keep lenient — does not. Refusing it here keeps both armed on the
    same set of plans without touching the loader. The field violations ride along because
    the seam DOES read the value as substantive, which pins the normalization too."""
    plan = _write_plan(tmp_path / "spaced.toml", knowledge_block="",
                       weight_class="substantive ")
    assert load_plan(plan).meta.weight_class == "substantive "  # the loader is untouched

    problems = submission_violations(load_plan(plan))
    assert "whitespace" in problems[0]
    assert len(problems) == 4


def test_seam_b_threads_the_session_class_through_replan(tmp_path, store):
    """Seam (b)'s `session_weight_class=state.weight_class`. Pinned only as a pure function
    and at seam (a), the argument could be deleted from either of the other two call sites
    with the whole suite still green — so a substantive session could replan onto a plan
    that declares nothing and escape the grade at the very seam a difficulty routes through."""
    sid = "seam-b-wc"
    _to_normalize_ready(store, sid, _write_plan(tmp_path / "plan.toml"))
    cli.cmd_normalize(ns(session=sid, factor="f", level="note", destination="знание"),
                      store=store)

    # complete in every other way: the ONLY thing the corrected plan does not say is what
    # class it is, so the refusal can come from nowhere else
    corrected = _write_plan(tmp_path / "corrected.toml", declare_weight_class=False)
    d = cli.cmd_replan(ns(session=sid, plan=corrected), store=store)

    assert d.ok is False
    assert d.action == "fix_plan"
    assert any("weight_class is not declared" in p for p in d.data.get("problems", []))
    assert store.load(sid).plan_path != corrected


def test_seam_c_threads_the_session_class_and_still_answers_with_a_directive(tmp_path,
                                                                            store):
    """Seam (c)'s half of the same threading — and, at the widened seam, the never-raise
    invariant re-pinned: approve is where the plan_approval gate is armed, so a PlanError
    escaping here would strand the session at PLAN_READY with no edge back."""
    sid = "seam-c-wc"
    plan_path = tmp_path / "plan.toml"
    _write_plan(plan_path)
    _to_plan_ready(store, sid, str(plan_path))

    # the in-place edit the plan-review cycle makes at PLAN_READY — here it drops the
    # declaration the session was classified under
    _write_plan(plan_path, declare_weight_class=False)
    d = cli.cmd_approve(ns(session=sid, by="user"), store=store)

    assert d.ok is False
    assert d.action == "fix_plan"
    assert any("weight_class is not declared" in p for p in d.data.get("problems", []))
    assert store.load(sid).node == Node.PLAN_READY.value


def test_the_session_digest_and_the_reviewer_attested_digest_stay_distinct():
    """Two unrelated things were spelled `plan_digest`: the session's own record of the
    bytes it accepted, and the digest a REVIEWER attests to at plan-review. The session
    field is renamed; the user-facing flag is deliberately NOT."""
    from agentctl.state import SessionState

    assert "accepted_plan_digest" in SessionState.__dataclass_fields__
    assert "plan_digest" not in SessionState.__dataclass_fields__
    args = cli.build_parser().parse_args(
        ["plan-review", "--session", "s", "--verdict", "pass", "--reviewer", "thinker",
         "--plan-digest", "abc"]
    )
    assert args.plan_digest == "abc"
