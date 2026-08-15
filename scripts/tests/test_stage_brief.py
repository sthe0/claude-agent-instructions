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
import importlib.util
from pathlib import Path

import pytest

from agentctl.dispatch import build_argv
from agentctl.plan import PlanDoc, PlanMeta, load_plan
from agentctl.render import cmd_plan_render, render_stage_brief
from agentctl.state import Actor, Criterion, Means, Principle, Stage, Subject, Supply

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
