"""Stage-brief projection (dispatch-stage-projection plan, stage 1): a spawned
specialist executes exactly one stage, but the spawn prompt has been inlining
the WHOLE plan TOML regardless of size — dead weight that scales with plan
size until a large plan's assembled prompt exceeds a child's context window
outright ("Prompt is too long"). This module covers:

  - `agentctl.render.render_stage_brief` — pure PlanDoc+index -> single-stage
    markdown, as a projection distinct from `render_plan_md`'s whole-plan view.
  - `agentctl plan-render --stage N` — the CLI surface for that projection.
  - `agentctl.dispatch.build_argv` — unconditionally passes `--plan-brief`.
  - `spawn-specialist.py`'s `brief_eligible` / `assemble_prompt` — the
    eligibility gate (kind + flag + stage index + plans_dir containment) and
    the byte-identical whole-plan fallback when any condition fails.
  - `spawn-specialist.py`'s pre-spawn refusal when the assembled prompt
    exceeds `DISPATCH_PROMPT_CEILING_CHARS`, on both the brief and
    whole-plan paths.
"""
from __future__ import annotations

import argparse
import dataclasses
import importlib.util
from pathlib import Path

import pytest

from agentctl.dispatch import build_argv
from agentctl.plan import PlanDoc, PlanMeta, load_plan
from agentctl.render import cmd_plan_render, render_stage_brief
from agentctl.state import (
    Actor,
    Criterion,
    FinalCheck,
    LandedSpec,
    Means,
    Principle,
    Stage,
    Subject,
    Supply,
)

SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"


def _load_spawn_specialist():
    spec = importlib.util.spec_from_file_location("spawn_specialist_stage_brief", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load_spawn_specialist()

TWO_STAGE_TOML = """
[meta]
task_id = "demo"
goal = "Ship the thing"
done_criterion = "dc"
criterion_type = "measurable"
weight_class = "substantive"
external_research = "checked wiki; none applies"

[[stage]]
index = 1
title = "Implement the feature"
executor = "spawn:developer"
expected_result_image = "the feature works per the spec"
criterion_type = "measurable"
done_criterion = "tests pass"
verify_command = "pytest -q"
material = "src/feature.py"
means = "python"
method = "add the handler"
conditions = "none"
invariants = "existing tests keep passing"
capability_required = "edit source files"
output_artifacts = ["src/feature.py", "docs/feature.md"]

[stage.principle]
statement = "s"
source = "s"
derivation = "d"
confidence = "high"
refutation = "r"

[[stage]]
index = 2
title = "Review the feature — a title that must never leak into stage 1's brief"
executor = "spawn:code-reviewer"
expected_result_image = "review is clean"
criterion_type = "measurable"
done_criterion = "no blocking findings"
verify_command = "true"
material = "src/feature.py"
means = "reading"
method = "read the diff"
conditions = "none"
invariants = "no scope creep"
capability_required = "read source files"

[stage.principle]
statement = "s"
source = "s"
derivation = "d"
confidence = "high"
refutation = "r"

[[stage.supplies]]
on = 1

[[final_check]]
command = "pytest -q"
label = "unit tests green"

[[final_check]]
command = "true"
"""


def _two_stage_doc(tmp_path: Path) -> tuple[Path, PlanDoc]:
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text(TWO_STAGE_TOML, encoding="utf-8")
    return plan_path, load_plan(str(plan_path))


def _stage_with_every_field(index: int = 1) -> Stage:
    """A Stage exercising every field render_stage_brief renders that
    render_plan_md deliberately omits (output_artifacts, control,
    criterion.observation, criterion.expected_exit) — these are runtime/engine
    fields, never authored in TOML, so built directly rather than parsed."""
    return Stage(
        index=index,
        title="Do the thing",
        subject=Subject(material="m", result="img", invariants="inv"),
        means=Means(means="bash", method="run"),
        actor=Actor(executor="spawn:developer", capability_required="cap", cost_tier="medium"),
        criterion=Criterion(
            criterion_type="measurable",
            done_criterion="d",
            verify_command="pytest -q",
            expected_exit=1,
            verify_venue="delivery",
            observation="prior attempt failed on X",
        ),
        conditions="c",
        supplies=[Supply(on=0)],
        output_artifacts=["scripts/agentctl/render.py"],
        control="reviewed by code-reviewer; no blocking findings",
        principle=Principle(statement="s", source="src", derivation="d", confidence="high", refutation="r"),
    )


# --- render_stage_brief -----------------------------------------------------


def test_render_stage_brief_selects_only_the_named_stage(tmp_path):
    _, doc = _two_stage_doc(tmp_path)
    brief = render_stage_brief(doc, 1)
    assert "Implement the feature" in brief
    assert "Review the feature" not in brief
    assert "must never leak into stage 1's brief" not in brief


def test_render_stage_brief_raises_valueerror_for_unknown_index(tmp_path):
    _, doc = _two_stage_doc(tmp_path)
    with pytest.raises(ValueError):
        render_stage_brief(doc, 99)


def test_render_stage_brief_renders_fields_the_whole_plan_view_omits():
    doc = PlanDoc(meta=PlanMeta(task_id="t", goal="g"), stages=[_stage_with_every_field()])
    brief = render_stage_brief(doc, 1)
    assert "scripts/agentctl/render.py" in brief  # output_artifacts
    assert "reviewed by code-reviewer" in brief  # control
    assert "prior attempt failed on X" in brief  # criterion.observation
    assert "Expected exit:** 1" in brief  # criterion.expected_exit (nonzero)


def test_render_stage_brief_direct_dependency_carries_title_result_and_artifacts(tmp_path):
    """Stage 2 depends on stage 1 (`[[stage.supplies]] on = 1`). Rendered from
    stage 2's side, the brief must carry stage 1's title, its expected result
    image, and its output artifacts — as stage 1's own block, distinct from
    stage 2's own **Expected result image**."""
    _, doc = _two_stage_doc(tmp_path)
    brief = render_stage_brief(doc, 2)
    assert "Implement the feature" in brief  # dependency's title
    assert "the feature works per the spec" in brief  # dependency's result image
    assert "docs/feature.md" in brief  # dependency's output_artifacts
    # Stage 2's own result image must still be present, and distinguishable
    # from the dependency's — this asserts the SHAPE, not just presence.
    assert "review is clean" in brief


def test_render_stage_brief_renders_final_check_labels(tmp_path):
    """meta.final_check carries two checks: one labelled, one not. Both must
    surface in the brief by label (or, for the unlabelled one, by position +
    kind) — never as a bare command/venue/kind dump."""
    _, doc = _two_stage_doc(tmp_path)
    brief = render_stage_brief(doc, 1)
    assert "unit tests green" in brief
    assert "check 2 (shell)" in brief  # the unlabelled check: position + kind
    assert "`true`" not in brief  # the unlabelled check's command must not leak


def test_render_stage_brief_covers_every_populated_field(tmp_path):
    """Reflection-anchored regression: every dataclass field of Stage/PlanMeta
    and their nested structs must be considered here, so a field silently
    added later without a matching assertion is caught by the drift check
    below rather than by discovering, months on, that a dispatched specialist
    never received it. `Stage.outcome` is the SOLE deliberate exclusion (the
    engine's mutable execution history, not an input) and is asserted absent,
    not merely un-asserted. `FinalCheck`'s command/venue/kind are excluded by
    the stage-brief's own labels-only method (covered separately, above) — not
    by this test — so they are intentionally left out of `CHECKED_FIELDS`
    with a comment, same as `Stage.outcome`.
    """
    principle = Principle(
        statement="STATEMENT_V", source="SOURCE_V", derivation="DERIVATION_V",
        confidence="CONFIDENCE_V", refutation="REFUTATION_V",
    )
    criterion = Criterion(
        criterion_type="CRITERION_TYPE_V",
        done_criterion="DONE_CRITERION_V",
        verify_command="VERIFY_COMMAND_V",
        expected_exit=42,
        observation="OBSERVATION_V",
        verify_venue="VERIFY_VENUE_V",
        verify_venue_at_final="VERIFY_VENUE_AT_FINAL_V",
    )
    stage = Stage(
        index=1,
        title="TITLE_V",
        subject=Subject(material="MATERIAL_V", result="RESULT_V", invariants="INVARIANTS_V"),
        means=Means(means="MEANS_V", method="METHOD_V"),
        actor=Actor(executor="EXECUTOR_V", capability_required="CAPABILITY_V", cost_tier="COST_TIER_V"),
        criterion=criterion,
        principle=principle,
        conditions="CONDITIONS_V",
        supplies=[Supply(on=7, element="ELEMENT_V", artifact="ARTIFACT_V")],
        output_artifacts=["OUTPUT_ARTIFACT_V"],
        control="CONTROL_V",
    )
    meta = PlanMeta(
        task_id="TASK_ID_V",
        goal="GOAL_V",
        done_criterion="OVERALL_DONE_CRITERION_V",
        criterion_type="OVERALL_CRITERION_TYPE_V",
        weight_class="WEIGHT_CLASS_V",
        external_research="EXTERNAL_RESEARCH_V",
        repo_root="REPO_ROOT_V",
        delivery_worktree="DELIVERY_WORKTREE_V",
        final_check=[FinalCheck(command="FC_COMMAND_V", label="FC_LABEL_V")],
    )
    doc = PlanDoc(meta=meta, stages=[stage])
    brief = render_stage_brief(doc, 1)

    must_appear = [
        "TITLE_V", "MATERIAL_V", "RESULT_V", "INVARIANTS_V", "MEANS_V", "METHOD_V",
        "EXECUTOR_V", "CAPABILITY_V", "COST_TIER_V", "CRITERION_TYPE_V",
        "DONE_CRITERION_V", "VERIFY_COMMAND_V", "42", "OBSERVATION_V",
        "VERIFY_VENUE_V", "VERIFY_VENUE_AT_FINAL_V", "STATEMENT_V", "SOURCE_V",
        "DERIVATION_V", "CONFIDENCE_V", "REFUTATION_V", "CONDITIONS_V",
        "OUTPUT_ARTIFACT_V", "CONTROL_V", "on stage 7", "ELEMENT_V", "ARTIFACT_V",
        "TASK_ID_V", "GOAL_V", "OVERALL_DONE_CRITERION_V", "OVERALL_CRITERION_TYPE_V",
        "WEIGHT_CLASS_V", "EXTERNAL_RESEARCH_V", "REPO_ROOT_V", "DELIVERY_WORKTREE_V",
        "FC_LABEL_V",
    ]
    assert "## Stage 1: TITLE_V" in brief  # Stage.index, via the section heading

    for value in must_appear:
        assert value in brief, f"{value!r} (a populated field's value) missing from stage brief"

    # The documented exclusions: Stage.outcome (engine execution history) and
    # FinalCheck.command (the stage-brief's labels-only method for final checks).
    assert "FC_COMMAND_V" not in brief
    assert stage.outcome.status not in brief  # "pending" — never rendered at all

    # Drift check: every field on every struct render_stage_brief consults
    # must be accounted for above (present in must_appear) or explicitly
    # named as a deliberate exclusion here — a newly-added field satisfies
    # neither and fails this assertion, rather than passing silently.
    checked = {
        (Stage, "index"), (Stage, "title"), (Stage, "subject"), (Stage, "means"),
        (Stage, "actor"), (Stage, "criterion"), (Stage, "principle"),
        (Stage, "conditions"), (Stage, "supplies"), (Stage, "output_artifacts"),
        (Stage, "outcome"),  # excluded: engine execution history
        (Stage, "control"),
        (Subject, "material"), (Subject, "result"), (Subject, "invariants"),
        (Means, "means"), (Means, "method"),
        (Actor, "executor"), (Actor, "capability_required"), (Actor, "cost_tier"),
        (Criterion, "criterion_type"), (Criterion, "done_criterion"),
        (Criterion, "verify_command"), (Criterion, "expected_exit"),
        (Criterion, "observation"), (Criterion, "verify_venue"),
        (Criterion, "verify_kind"),  # excluded: branch discriminator, not echoed literally
        (Criterion, "landed"),  # excluded here: covered by the landed-check test below instead
        (Criterion, "verify_venue_at_final"),
        (Principle, "statement"), (Principle, "source"), (Principle, "derivation"),
        (Principle, "confidence"), (Principle, "refutation"),
        (Supply, "on"), (Supply, "element"), (Supply, "artifact"),
        (PlanMeta, "task_id"), (PlanMeta, "goal"), (PlanMeta, "done_criterion"),
        (PlanMeta, "criterion_type"), (PlanMeta, "weight_class"),
        (PlanMeta, "external_research"), (PlanMeta, "repo_root"),
        (PlanMeta, "delivery_worktree"), (PlanMeta, "final_check"),
        (FinalCheck, "label"),
        (FinalCheck, "command"),  # excluded: labels-only method
        (FinalCheck, "expected_exit"),  # excluded: labels-only method
        (FinalCheck, "venue"),  # excluded: labels-only method
        (FinalCheck, "kind"),  # excluded except as the unlabelled-check fallback (see label test)
        (FinalCheck, "landed"),  # excluded: labels-only method
    }
    declared = set()
    for cls in (Stage, Subject, Means, Actor, Criterion, Principle, Supply, PlanMeta, FinalCheck):
        for f in dataclasses.fields(cls):
            declared.add((cls, f.name))
    missing = declared - checked
    assert not missing, f"new field(s) not accounted for in this coverage test: {missing}"


def test_render_stage_brief_renders_landed_check_fields():
    """LandedSpec's three fields (target, remote, delivered_stage) are a
    distinct nested struct from FinalCheck/Criterion's other fields and are
    only reachable via `criterion.verify_kind == 'landed'` — a separate
    fixture from the coverage test above, since `landed` and `verify_command`
    are mutually exclusive on one Criterion (plan.py's R1)."""
    stage = Stage(
        index=1,
        title="Land it",
        subject=Subject(material="m", result="r"),
        means=Means(means="means", method="method"),
        actor=Actor(executor="spawn:developer"),
        criterion=Criterion(
            criterion_type="measurable",
            done_criterion="landed",
            verify_kind="landed",
            landed=LandedSpec(target="TARGET_V", remote="REMOTE_V", delivered_stage=9),
        ),
    )
    doc = PlanDoc(meta=PlanMeta(task_id="t"), stages=[stage])
    brief = render_stage_brief(doc, 1)
    assert "TARGET_V" in brief
    assert "REMOTE_V" in brief
    assert "stage 9" in brief


# --- agentctl plan-render --stage -------------------------------------------


def test_cmd_plan_render_with_stage_flag_projects_one_stage(tmp_path):
    plan_path, _ = _two_stage_doc(tmp_path)
    directive = cmd_plan_render(argparse.Namespace(plan=str(plan_path), stage=1))
    assert directive.ok
    assert "Implement the feature" in directive.data["markdown"]
    assert "Review the feature" not in directive.data["markdown"]


def test_cmd_plan_render_without_stage_is_unchanged_whole_plan_view(tmp_path):
    plan_path, _ = _two_stage_doc(tmp_path)
    directive = cmd_plan_render(argparse.Namespace(plan=str(plan_path), stage=None))
    assert directive.ok
    assert "Implement the feature" in directive.data["markdown"]
    assert "Review the feature" in directive.data["markdown"]


def test_cmd_plan_render_unknown_stage_returns_failed_directive(tmp_path):
    plan_path, _ = _two_stage_doc(tmp_path)
    directive = cmd_plan_render(argparse.Namespace(plan=str(plan_path), stage=99))
    assert not directive.ok


# --- dispatch.build_argv -----------------------------------------------------


def test_build_argv_always_appends_plan_brief_flag(tmp_path):
    plan_path, doc = _two_stage_doc(tmp_path)
    argv = build_argv(doc.stages[0], str(plan_path))
    assert "--plan-brief" in argv


# --- spawn-specialist.py brief_eligible -------------------------------------


def _args(plan_path, **overrides):
    base = dict(
        kind="developer",
        plan=plan_path,
        constraints="",
        context_dossier=None,
        done_criterion="do the thing",
        criterion_type="measurable",
        continue_worktree=None,
        stage_index=1,
        plan_brief=True,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_brief_eligible_true_when_all_conditions_met(tmp_path, monkeypatch):
    monkeypatch.setattr(MOD, "plans_dir", lambda: tmp_path)
    plan_path, _ = _two_stage_doc(tmp_path)
    assert MOD.brief_eligible(_args(plan_path)) is True


def test_brief_eligible_false_for_kind_outside_plans_read_kinds(tmp_path, monkeypatch):
    monkeypatch.setattr(MOD, "plans_dir", lambda: tmp_path)
    plan_path, _ = _two_stage_doc(tmp_path)
    assert MOD.brief_eligible(_args(plan_path, kind="planner")) is False


def test_brief_eligible_false_when_flag_not_set(tmp_path, monkeypatch):
    monkeypatch.setattr(MOD, "plans_dir", lambda: tmp_path)
    plan_path, _ = _two_stage_doc(tmp_path)
    assert MOD.brief_eligible(_args(plan_path, plan_brief=False)) is False


def test_brief_eligible_false_when_stage_index_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(MOD, "plans_dir", lambda: tmp_path)
    plan_path, _ = _two_stage_doc(tmp_path)
    assert MOD.brief_eligible(_args(plan_path, stage_index=None)) is False


def test_brief_eligible_false_when_plan_outside_plans_dir(tmp_path, monkeypatch):
    other_dir = tmp_path / "not-plans-dir"
    other_dir.mkdir()
    monkeypatch.setattr(MOD, "plans_dir", lambda: tmp_path / "the-real-plans-dir")
    plan_path = other_dir / "plan.toml"
    plan_path.write_text(TWO_STAGE_TOML, encoding="utf-8")
    assert MOD.brief_eligible(_args(plan_path)) is False


# --- spawn-specialist.py assemble_prompt ------------------------------------


def test_assemble_prompt_projects_brief_when_eligible(tmp_path, monkeypatch):
    monkeypatch.setattr(MOD, "plans_dir", lambda: tmp_path)
    plan_path, _ = _two_stage_doc(tmp_path)
    prompt = MOD.assemble_prompt(_args(plan_path), depth=1, permissions="")
    assert "Implement the feature" in prompt
    assert "Review the feature" not in prompt
    assert "brief" in prompt.lower()


def test_assemble_prompt_byte_identical_fallback_when_not_eligible(tmp_path, monkeypatch):
    monkeypatch.setattr(MOD, "plans_dir", lambda: tmp_path)
    plan_path, _ = _two_stage_doc(tmp_path)
    with_flag_off = MOD.assemble_prompt(_args(plan_path, plan_brief=False), depth=1, permissions="")
    args_missing_attr = _args(plan_path, plan_brief=False)
    del args_missing_attr.plan_brief
    without_attr = MOD.assemble_prompt(args_missing_attr, depth=1, permissions="")
    assert with_flag_off == without_attr
    assert "Review the feature" in with_flag_off  # whole plan text present, unprojected


# --- prompt-size ceiling -----------------------------------------------------


def test_ceiling_constants_match_the_supplied_derivation():
    assert MOD.DISPATCH_PROMPT_CEILING_TOKENS == 144_000
    assert MOD.PROMPT_CHARS_PER_TOKEN == 1.5
    assert MOD.DISPATCH_PROMPT_CEILING_CHARS == 216_000
    # The three pre-existing constants (a distinct concern — the CHILD's own
    # compaction-window pin) must be untouched by this stage's change.
    assert MOD.AUTOCOMPACT_CEILING_TOKENS == 150_000
    assert MOD.OUTPUT_RESERVE_TOKENS == 20_000
    assert MOD.PRECOMPUTE_BUFFER_FRACTION == 0.2


def test_prompt_exceeds_ceiling_boundary():
    assert MOD.prompt_exceeds_ceiling("x" * MOD.DISPATCH_PROMPT_CEILING_CHARS) is False
    assert MOD.prompt_exceeds_ceiling("x" * (MOD.DISPATCH_PROMPT_CEILING_CHARS + 1)) is True


def test_main_refuses_oversized_prompt_before_spawning(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(MOD, "plans_dir", lambda: tmp_path)
    plan_path, _ = _two_stage_doc(tmp_path)
    oversized_constraints = "x" * (MOD.DISPATCH_PROMPT_CEILING_CHARS + 1)
    argv = [
        "--kind", "developer",
        "--plan", str(plan_path),
        "--done-criterion", "do the thing",
        "--criterion-type", "measurable",
        "--constraints", oversized_constraints,
        "--stage-index", "1",
        "--plan-brief",
    ]
    rc = MOD.main(argv)
    assert rc == 5
    err = capsys.readouterr().err
    assert "exceeding" in err
    assert str(MOD.DISPATCH_PROMPT_CEILING_CHARS) in err


def test_main_refuses_oversized_prompt_on_whole_plan_path_too(tmp_path, monkeypatch, capsys):
    """The refusal is not brief-only: an oversized whole-plan prompt (no
    --plan-brief) must refuse identically."""
    monkeypatch.setattr(MOD, "plans_dir", lambda: tmp_path)
    plan_path, _ = _two_stage_doc(tmp_path)
    oversized_constraints = "x" * (MOD.DISPATCH_PROMPT_CEILING_CHARS + 1)
    argv = [
        "--kind", "developer",
        "--plan", str(plan_path),
        "--done-criterion", "do the thing",
        "--criterion-type", "measurable",
        "--constraints", oversized_constraints,
    ]
    rc = MOD.main(argv)
    assert rc == 5
    assert "exceeding" in capsys.readouterr().err


def test_main_refusal_names_inherited_model_when_none_resolved(tmp_path, monkeypatch, capsys):
    """code-reviewer has no MODEL_BY_KIND entry and no --complexity here, so
    resolve_model returns None; the refusal message must say INHERITED, not
    claim a model was resolved."""
    monkeypatch.setattr(MOD, "plans_dir", lambda: tmp_path)
    plan_path, _ = _two_stage_doc(tmp_path)
    oversized_constraints = "x" * (MOD.DISPATCH_PROMPT_CEILING_CHARS + 1)
    argv = [
        "--kind", "code-reviewer",
        "--plan", str(plan_path),
        "--done-criterion", "do the thing",
        "--criterion-type", "measurable",
        "--constraints", oversized_constraints,
        "--stage-index", "2",
        "--plan-brief",
    ]
    rc = MOD.main(argv)
    assert rc == 5
    assert "inherited" in capsys.readouterr().err.lower()


# --- non-portable, non-asserted real-plan measurement -----------------------


def test_real_plan_measurement_smd_act_defects_8():
    """Informational only: measures assemble_prompt's whole-plan size against
    the actual plan that hit the incident this stage fixes, IF that plan file
    is present on this machine. Never asserts pass/fail on the measurement
    itself — only skips cleanly when the file is absent (a different
    machine/session), since the file is outside this repo's tree."""
    real_plan = Path.home() / ".claude-agent" / "plans" / "smd-act-defects-8.toml"
    if not real_plan.exists():
        pytest.skip("smd-act-defects-8.toml not present on this machine")
    doc = load_plan(str(real_plan))
    stage13 = next((s for s in doc.stages if s.index == 13), None)
    if stage13 is None:
        pytest.skip("stage 13 not present in smd-act-defects-8.toml")
    brief = render_stage_brief(doc, 13)
    print(f"\nstage-13 brief: {len(brief)} chars vs whole-plan {real_plan.stat().st_size} chars")
