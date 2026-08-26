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
    exceeds `dispatch_prompt_ceiling_chars`, on both the brief and
    whole-plan paths.
"""
from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import re
import subprocess
import sys
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
        subject=Subject(material="MATERIAL_V", result="RESULT_V", invariants="INVARIANTS_V",
                        material_refs=["MATERIAL_REF_V"], knowledge_refs=["KNOWLEDGE_REF_V"]),
        means=Means(means="MEANS_V", method="METHOD_V", procedure="PROCEDURE_V"),
        actor=Actor(executor="EXECUTOR_V", capability_required="CAPABILITY_V", cost_tier="COST_TIER_V"),
        criterion=criterion,
        principle=principle,
        knowledge="KNOWLEDGE_V",
        preconditions="PRECONDITIONS_V",
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
        "MATERIAL_REF_V", "KNOWLEDGE_V", "KNOWLEDGE_REF_V", "PROCEDURE_V", "PRECONDITIONS_V",
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
        (Stage, "control"), (Stage, "knowledge"), (Stage, "preconditions"),
        (Subject, "material"), (Subject, "result"), (Subject, "invariants"),
        (Subject, "material_refs"), (Subject, "knowledge_refs"),
        (Means, "means"), (Means, "method"), (Means, "procedure"),
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
        # excluded: the typed order is the ROOT's material at the approval gate — it is
        # plan-level, grows with the customer's requirement count, and the executor of one
        # stage receives its own requirement through that stage's own fields plus
        # meta.goal/done_criterion. Carrying it would restore the size coupling this
        # projection exists to break.
        (PlanMeta, "order"),
        (FinalCheck, "label"),
        (FinalCheck, "command"),  # excluded: labels-only method
        (FinalCheck, "expected_exit"),  # excluded: labels-only method
        (FinalCheck, "venue"),  # excluded: labels-only method
        (FinalCheck, "kind"),  # excluded except as the unlabelled-check fallback (see label test)
        (FinalCheck, "landed"),  # excluded: labels-only method
        # LandedSpec is reachable only through Criterion.landed and FinalCheck.landed,
        # both excluded above. It is walked anyway: otherwise a field added to
        # LandedSpec escapes both this drift check and the dedicated landed-check
        # test, which asserts three field values by name and so cannot notice a fourth.
        (LandedSpec, "target"), (LandedSpec, "remote"), (LandedSpec, "delivered_stage"),
    }
    declared = set()
    for cls in (Stage, Subject, Means, Actor, Criterion, Principle, Supply, PlanMeta,
                FinalCheck, LandedSpec):
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


# --- the twelve-stage fixture (assertions 3, 4, 7) ---------------------------
#
# TWO_STAGE_TOML cannot carry these assertions. With two stages and one edge,
# "carries only the active stage and its direct dependencies" is indistinguishable
# from "carries everything", there is no stage reachable only transitively and
# none wholly unreachable, and a size bound stated as a fraction of the whole
# plan is met by a renderer that drops nothing. Twelve stages with the shape
# below make each of those a real discrimination.
#
# The dependency shape, read from the ACTIVE stage (7):
#   direct           3, 5            (>= 2, so "carries its dependency" is plural)
#   transitive-only  1 (via 3), 4 (via 5)   -- reachable, never carried
#   unreachable      2, 6, 8, 9, 10, 11, 12
#
# Every stage's title, expected result image and method is genuinely distinct
# prose, and every free-text field additionally carries the stage's own unique
# token. The token is what makes the exclusion assertions MECHANICAL: a
# non-dependency stage is asserted absent by token (total absence), and a
# dependency by walking its parsed fields and asserting every token-bearing
# value that is not one of the three carried ones is absent. Neither samples.
#
# Padding is per-stage unique (it embeds the stage's own token) rather than one
# shared filler, so it cannot itself satisfy an absence assertion by belonging
# to nobody. It exists to equalise the raw block sizes: the size bounds compare
# the brief against the ACTIVE stage's raw slice, which is only meaningful when
# no stage is anomalously large or small.

_TWELVE_STAGE_COUNT = 12
TWELVE_ACTIVE_INDEX = 7

# stage index -> the indices it declares `[[stage.supplies]] on = ...` for
_TWELVE_DEPENDS_ON = {3: (1,), 5: (4,), 7: (3, 5), 9: (7,), 11: (10,)}

_TWELVE_TITLES = {
    1: "Read the failing dispatch transcript end to end",
    2: "Draft the migration note for downstream consumers",
    3: "Extract the size measurement into a reusable probe",
    4: "Pin the borrowed client constants against the installed bundle",
    5: "Teach the renderer to project a single stage",
    6: "Retire the hand-written markdown twin of the plan",
    7: "Wire the projection into the spawn path behind an eligibility gate",
    8: "Backfill the operator runbook for an oversized prompt",
    9: "Measure the projected prompt against the real incident plan",
    10: "Sweep the remaining callers of the whole-plan renderer",
    11: "Record the incident as an experience leaf",
    12: "Hand the branch to review with its evidence attached",
}

_TWELVE_RESULTS = {
    1: "the transcript's token accounting is written down",
    2: "downstream consumers know what changed and when",
    3: "the probe measures a prompt without a live spawn",
    4: "each borrowed constant cites the bundle string it came from",
    5: "one stage renders without the other eleven",
    6: "the markdown twin is gone and nothing references it",
    7: "an eligible dispatch carries a projection and an ineligible one does not",
    8: "an operator reading the refusal knows what to shrink",
    9: "the measurement is printed and never asserted upon",
    10: "no caller reaches the whole-plan renderer by accident",
    11: "the incident is retrievable by a future search",
    12: "the review request is open and self-justifying",
}

_TWELVE_METHODS = {
    1: "replay the transcript and total the prompt bytes per section",
    2: "write the note against the public interface, not the diff",
    3: "lift the measurement into a helper the tests can call",
    4: "read the installed bundle and quote the literal string beside each value",
    5: "walk the stage's fields and emit the non-empty ones",
    6: "delete the file and follow every inbound reference",
    7: "split the eligibility predicate so the resolved path survives",
    8: "add the runbook entry beside the refusal's own message",
    9: "skip cleanly when the incident plan is absent from this machine",
    10: "grep for the renderer's name and triage each hit",
    11: "search for an analogous leaf before minting a new one",
    12: "open the review request and attach the measurement output",
}

_TWELVE_FINAL_CHECKS = """
[[final_check]]
label = "the twelve-stage projection suite is green FCONEKEY"
command = "pytest -q -k FCONEKEY"
expected_exit = 77

[[final_check]]
command = "scripts/FCTWOKEY-smoke.sh"

[[final_check]]
kind = "landed"
label = "the branch is contained in trunk FCTHREEKEY"

[final_check.landed]
target = "trunk-FCTHREEKEY"
remote = "origin-FCTHREEKEY"
delivered_stage = 12
"""


def _twelve_token(index: int) -> str:
    """The stage's unique token. Fixed width so no token is a substring of
    another (MK1KEY would sit inside MK11KEY; MK01KEY does not sit inside any)."""
    return f"MK{index:02d}KEY"


def _twelve_stage_block(index: int, pad: int) -> str:
    tok = _twelve_token(index)
    padding = f" {tok}-" + "y" * pad if pad else ""
    lines = [
        "[[stage]]",
        f"index = {index}",
        f'title = "{_TWELVE_TITLES[index]} ({tok})"',
        'executor = "spawn:developer"',
        f'expected_result_image = "{_TWELVE_RESULTS[index]} ({tok})"',
        'criterion_type = "measurable"',
        f'done_criterion = "the {tok} check reports zero defects"',
        f'verify_command = "pytest -q -k {tok}"',
        f'material = "src/{tok}/module.py"',
        f'means = "the {tok} toolchain"',
        f'method = "{_TWELVE_METHODS[index]} ({tok}).{padding}"',
        f'conditions = "the {tok} preconditions are met"',
        f'invariants = "nothing outside {tok} changes"',
        f'capability_required = "edit files under {tok}"',
        f'output_artifacts = ["out/{tok}-report.md", "out/{tok}-log.txt"]',
        "",
        "[stage.principle]",
        f'statement = "a {tok} projection carries only what its executor needs"',
        f'source = "the {tok} incident"',
        f'derivation = "induced from the {tok} incident"',
        'confidence = "high"',
        f'refutation = "a {tok} executor asks for a field the brief omitted"',
        "",
    ]
    for dep in _TWELVE_DEPENDS_ON.get(index, ()):
        lines += ["[[stage.supplies]]", f"on = {dep}", ""]
    return "\n".join(lines) + "\n"


def _twelve_meta_block(pad: int) -> str:
    filler = " METAFILL-" + "z" * pad if pad else ""
    lines = [
        "[meta]",
        'task_id = "twelve"',
        'goal = "Stop the spawn prompt from scaling with plan size"',
        'done_criterion = "a dispatched specialist receives only its own stage"',
        'criterion_type = "measurable"',
        'weight_class = "substantive"',
        f'external_research = "read the installed client bundle; nothing else applies.{filler}"',
        "",
    ]
    return "\n".join(lines) + "\n"


def _twelve_stage_toml() -> str:
    """Twelve stages whose raw TOML blocks are equal in length (hence trivially
    inside the 10% band the size assertions need), and a meta block sized to
    three quarters of one stage (inside [0.5x, 1.0x] of the mean)."""
    bare = {k: _twelve_stage_block(k, 0) for k in range(1, _TWELVE_STAGE_COUNT + 1)}
    target = max(len(b) for b in bare.values()) + 600
    stages = {
        k: _twelve_stage_block(k, target - len(b) - len(f" {_twelve_token(k)}-"))
        for k, b in bare.items()
    }
    meta_bare = _twelve_meta_block(0)
    meta = _twelve_meta_block(int(target * 0.75) - len(meta_bare) - len(" METAFILL-"))
    body = "".join(stages[k] for k in range(1, _TWELVE_STAGE_COUNT + 1))
    return meta + body + _TWELVE_FINAL_CHECKS.lstrip("\n")


def _twelve_stage_doc(tmp_path: Path) -> tuple[Path, PlanDoc]:
    plan_path = tmp_path / "twelve.toml"
    plan_path.write_text(_twelve_stage_toml(), encoding="utf-8")
    return plan_path, load_plan(str(plan_path))


def _stage_by_index(doc: PlanDoc, index: int) -> Stage:
    return next(s for s in doc.stages if s.index == index)


# A TOP-LEVEL TOML table header: `[meta]`, `[[stage]]`, `[[final_check]]`. A
# dotted header (`[stage.principle]`, `[[stage.supplies]]`, `[final_check.landed]`)
# is a SUB-table of the block it sits in and must not end that block — which is
# why this matches a name with no dot rather than any line starting with "[".
_TOP_LEVEL_HEADER = re.compile(r"^\[\[?[A-Za-z_][A-Za-z0-9_-]*\]\]?$")


def _raw_top_level_blocks(plan_text: str) -> list[tuple[str, str]]:
    """(table name, raw source slice) for every top-level table, in file order.
    A block runs from its own header line to the line before the next top-level
    header, or to end of file when none follows."""
    blocks: list[tuple[str, str]] = []
    name: str | None = None
    buf: list[str] = []
    for line in plan_text.splitlines(keepends=True):
        if _TOP_LEVEL_HEADER.match(line.strip()):
            if name is not None:
                blocks.append((name, "".join(buf)))
            name = line.strip().strip("[]")
            buf = [line]
        elif name is not None:
            buf.append(line)
    if name is not None:
        blocks.append((name, "".join(buf)))
    return blocks


def _raw_stage_slice(plan_text: str, stage_index: int) -> str:
    for name, block in _raw_top_level_blocks(plan_text):
        if name == "stage" and re.search(rf"^index = {stage_index}$", block, re.M):
            return block
    raise AssertionError(f"no [[stage]] block with index {stage_index}")


def _raw_meta_slice(plan_text: str) -> str:
    for name, block in _raw_top_level_blocks(plan_text):
        if name == "meta":
            return block
    raise AssertionError("no [meta] block")


def _token_bearing_values(obj: object, token: str) -> list[str]:
    """Every string anywhere inside `obj` (walked by dataclass reflection, so a
    field added later is walked without editing this test) that carries `token`.
    Shared vocabulary — "measurable", "spawn:developer", "high" — carries no
    token and is deliberately not collected: it is not the stage's own body."""
    found: list[str] = []

    def walk(node: object) -> None:
        if dataclasses.is_dataclass(node):
            for f in dataclasses.fields(node):
                walk(getattr(node, f.name))
        elif isinstance(node, (list, tuple, set)):
            for item in node:
                walk(item)
        elif isinstance(node, str) and token in node:
            found.append(node)

    walk(obj)
    return found


def _reachable_scalars(node: object) -> list[str]:
    """Every scalar under `node` rendered as the text it would appear as —
    dataclass fields walked reflectively so a field added to FinalCheck or
    LandedSpec is covered without editing the caller. Booleans and None are
    skipped: they have no literal surface in the rendered brief."""
    out: list[str] = []
    if node is None or isinstance(node, bool):
        return out
    if dataclasses.is_dataclass(node):
        for f in dataclasses.fields(node):
            out.extend(_reachable_scalars(getattr(node, f.name)))
    elif isinstance(node, (list, tuple, set)):
        for item in node:
            out.extend(_reachable_scalars(item))
    elif isinstance(node, str):
        if node:
            out.append(node)
    elif isinstance(node, int):
        out.append(str(node))
    return out


def test_twelve_stage_fixture_holds_the_shape_its_assertions_rest_on(tmp_path):
    """The fixture is itself load-bearing: every exclusion and size assertion
    below is only a discrimination if this shape holds. Asserted here rather
    than assumed, so a later edit that flattens the shape fails HERE with a
    readable reason instead of quietly weakening the tests that use it."""
    plan_path, doc = _twelve_stage_doc(tmp_path)
    raw = plan_path.read_text(encoding="utf-8")

    assert len(doc.stages) == _TWELVE_STAGE_COUNT
    active = _stage_by_index(doc, TWELVE_ACTIVE_INDEX)

    # (a) textual distinctness: no two stages share a title, result image or
    #     method, and each stage's token appears in no other stage's source.
    for field_map in (_TWELVE_TITLES, _TWELVE_RESULTS, _TWELVE_METHODS):
        assert len(set(field_map.values())) == _TWELVE_STAGE_COUNT
    for k in range(1, _TWELVE_STAGE_COUNT + 1):
        others = [_raw_stage_slice(raw, j) for j in range(1, _TWELVE_STAGE_COUNT + 1) if j != k]
        assert all(_twelve_token(k) not in o for o in others)

    # (b) the dependency shape: >= 2 direct, >= 1 transitive-only, >= 1 unreachable.
    direct = set(active.depends_on)
    assert len(direct) >= 2
    reachable = set()
    frontier = list(direct)
    while frontier:
        i = frontier.pop()
        if i in reachable:
            continue
        reachable.add(i)
        frontier.extend(_stage_by_index(doc, i).depends_on)
    transitive_only = reachable - direct
    unreachable = {s.index for s in doc.stages} - reachable - {TWELVE_ACTIVE_INDEX}
    assert len(transitive_only) >= 1
    assert len(unreachable) >= 1

    # (c) the 10% size band: every stage's raw slice sits within 10% of the mean.
    sizes = [len(_raw_stage_slice(raw, k)) for k in range(1, _TWELVE_STAGE_COUNT + 1)]
    mean = sum(sizes) / len(sizes)
    assert min(sizes) >= mean * 0.9
    assert max(sizes) <= mean * 1.1

    # (d) meta sits in [0.5x, 1.0x] of that mean — big enough that a brief which
    #     merely dropped meta could not pass the size bounds, small enough that
    #     meta alone cannot dominate them.
    meta_size = len(_raw_meta_slice(raw))
    assert mean * 0.5 <= meta_size <= mean * 1.0

    # (e) no meta leak: meta carries no stage's token, title, result or method,
    #     so a stage-body string found in the brief came from a stage.
    meta_raw = _raw_meta_slice(raw)
    for k in range(1, _TWELVE_STAGE_COUNT + 1):
        assert _twelve_token(k) not in meta_raw
        assert _TWELVE_TITLES[k] not in meta_raw
        assert _TWELVE_RESULTS[k] not in meta_raw
        assert _TWELVE_METHODS[k] not in meta_raw

    # (f) the final checks the label-only assertion needs: >= 2, at least one
    #     unlabelled, at least one landed.
    checks = doc.meta.final_check
    assert len(checks) >= 2
    assert any(not fc.label for fc in checks)
    assert any(fc.kind == "landed" and fc.landed is not None for fc in checks)


def test_twelve_stage_brief_carries_only_the_active_stage_and_its_dependencies(tmp_path):
    """Assertion (3). Split by dependency class and enumerated from the PARSED
    plan, never sampled: no other stage's method text appears anywhere; a
    non-dependency contributes nothing at all (its token is absent outright);
    a direct dependency contributes exactly title + expected result image +
    output artifacts and no further field of its own."""
    _, doc = _twelve_stage_doc(tmp_path)
    brief = render_stage_brief(doc, TWELVE_ACTIVE_INDEX)
    active = _stage_by_index(doc, TWELVE_ACTIVE_INDEX)
    direct = set(active.depends_on)

    for stage in doc.stages:
        if stage.index == TWELVE_ACTIVE_INDEX:
            continue
        token = _twelve_token(stage.index)
        assert stage.means.method not in brief, f"stage {stage.index}'s method leaked"

        if stage.index not in direct:
            # Includes the transitively-reachable-only stages: reachable is not
            # carried, only DIRECT is.
            assert token not in brief, f"stage {stage.index} leaked into the brief"
            continue

        carried = {stage.title, stage.subject.result, *stage.output_artifacts}
        for value in carried:
            assert value in brief, f"dependency {stage.index} lost {value!r}"
        for value in _token_bearing_values(stage, token):
            if value in carried:
                continue
            assert value not in brief, (
                f"dependency {stage.index} contributed {value!r}, which is not one "
                f"of its three carried fields"
            )

    # ...and the active stage itself is whole.
    for value in _token_bearing_values(active, _twelve_token(TWELVE_ACTIVE_INDEX)):
        assert value in brief, f"the active stage lost {value!r}"


def test_twelve_stage_brief_is_bounded_by_the_terms_a_projection_carries(tmp_path):
    """Assertion (4), both bounds. The first is the coarse one the change
    exists for (the brief is a small fraction of the plan); the second is the
    tight one — the brief may not exceed the sum of the terms a projection
    actually carries, which no renderer silently inlining a second stage can
    satisfy, however small that stage is."""
    plan_path, doc = _twelve_stage_doc(tmp_path)
    raw = plan_path.read_text(encoding="utf-8")
    brief = render_stage_brief(doc, TWELVE_ACTIVE_INDEX)
    active = _stage_by_index(doc, TWELVE_ACTIVE_INDEX)

    assert len(brief) < len(raw) / 5

    carried_terms = len(_raw_meta_slice(raw))
    carried_terms += 4 * len(_raw_stage_slice(raw, TWELVE_ACTIVE_INDEX))
    for dep_index in sorted(active.depends_on):
        dep = _stage_by_index(doc, dep_index)
        carried_terms += len(dep.title) + len(dep.subject.result)
        carried_terms += sum(len(a) for a in dep.output_artifacts)
    carried_terms += sum(len(fc.label) for fc in doc.meta.final_check)
    assert len(brief) < carried_terms


def test_twelve_stage_brief_carries_final_checks_by_label_only(tmp_path):
    """Assertion (7). Every check surfaces, and every field of it other than
    that label — enumerated by reflection from the parsed check, not named
    here — is absent, split by label emptiness: a labelled check renders as its
    label alone, an unlabelled one as position + kind and nothing more.

    Absence is asserted against the final-verification SECTION and excuses only
    values another check legitimately contributes (one unlabelled check's kind
    is the whole of that set here). A blanket whole-brief assertion would be
    unsatisfiable for a shared enum value like "shell" and would say nothing
    about which check put it there."""
    _, doc = _twelve_stage_doc(tmp_path)
    brief = render_stage_brief(doc, TWELVE_ACTIVE_INDEX)
    checks = doc.meta.final_check

    section = brief[brief.index("## Final verification"):]
    section_lines = section.splitlines()

    legitimate = {fc.label for fc in checks if fc.label}
    legitimate |= {fc.kind for fc in checks if not fc.label}

    for i, fc in enumerate(checks, start=1):
        expected = f"- {fc.label}" if fc.label else f"- check {i} ({fc.kind})"
        assert expected in section_lines, f"final check {i} not rendered as {expected!r}"

        for f in dataclasses.fields(fc):
            if f.name == "label":
                continue
            for value in _reachable_scalars(getattr(fc, f.name)):
                if value in legitimate:
                    continue
                assert value not in section, (
                    f"final check {i}'s {f.name} contributed {value!r} to the brief"
                )


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


# --- the symlink pair (assertion 9) ------------------------------------------
#
# The gate dereferences BOTH sides before deciding containment, and only the
# pair proves it: a literal-prefix implementation passes the refusing half and
# fails the admitting one, while an implementation testing containment of the
# as-given path passes the admitting half and hands the child a path under no
# directory it was granted. Either half alone is satisfied by a prefix test.
#
# The refusing half is not hypothetical. This repository carries a
# resolution-confirmed incident in which a path-based read-only guard was
# defeated because the directory it exempted was itself a symlink into the
# checkout it protected; the recorded remedy is exactly "dereference before
# deciding" (memory-global/leaves/experience/
# 2026-08-12-symlinked-write-path-defeats-path-based-guard.md). Prose is what
# that incident already had, so this pins it with a probe.


@pytest.mark.skipif(
    not hasattr(Path, "symlink_to"), reason="platform without symlink support"
)
def test_symlink_inside_plans_dir_pointing_out_gets_the_whole_plan(tmp_path, monkeypatch):
    """Half (i): the path LOOKS contained and its target is not. A literal
    string-prefix containment check admits this and hands a plans_dir()-only
    child a projection sourced from a file it may not open."""
    plans = (tmp_path / "plans").resolve()
    plans.mkdir()
    outside = (tmp_path / "elsewhere").resolve()
    outside.mkdir()
    real_plan = outside / "plan.toml"
    real_plan.write_text(TWO_STAGE_TOML, encoding="utf-8")

    link = plans / "plan.toml"
    link.symlink_to(real_plan)
    monkeypatch.setattr(MOD, "plans_dir", lambda: plans)

    assert MOD.brief_plan_path(_args(link)) is None
    prompt = MOD.assemble_prompt(_args(link), depth=1, permissions="")
    assert "Review the feature" in prompt  # whole plan text, unprojected
    assert "projected" not in prompt  # and no pointer line


@pytest.mark.skipif(
    not hasattr(Path, "symlink_to"), reason="platform without symlink support"
)
def test_symlink_outside_plans_dir_pointing_in_gets_the_brief(tmp_path, monkeypatch):
    """Half (ii): the path LOOKS uncontained and its target is contained. The
    child may open the target, so it earns the brief — and the pointer must
    name the target, never the outside spelling it was reached by, which is the
    one path plans_dir() does not grant."""
    plans = (tmp_path / "plans").resolve()
    plans.mkdir()
    outside = (tmp_path / "elsewhere").resolve()
    outside.mkdir()
    real_plan = plans / "plan.toml"
    real_plan.write_text(TWO_STAGE_TOML, encoding="utf-8")

    link = outside / "plan.toml"
    link.symlink_to(real_plan)
    monkeypatch.setattr(MOD, "plans_dir", lambda: plans)

    assert MOD.brief_plan_path(_args(link)) == real_plan
    prompt = MOD.assemble_prompt(_args(link), depth=1, permissions="")
    assert "Implement the feature" in prompt
    assert "Review the feature" not in prompt  # projected, not the whole plan
    assert f"`{real_plan}`" in prompt  # pointer names the target...
    assert str(link) not in prompt  # ...never the outside spelling


# --- spawn-specialist.py assemble_prompt ------------------------------------


def test_assemble_prompt_projects_brief_when_eligible(tmp_path, monkeypatch):
    monkeypatch.setattr(MOD, "plans_dir", lambda: tmp_path)
    plan_path, _ = _two_stage_doc(tmp_path)
    prompt = MOD.assemble_prompt(_args(plan_path), depth=1, permissions="")
    assert "Implement the feature" in prompt
    assert "Review the feature" not in prompt
    assert "brief" in prompt.lower()


def test_assemble_prompt_pointer_names_the_resolved_plan_path(tmp_path, monkeypatch):
    """The pointer is the child's ONLY route back to what the projection left
    out, so it must name a path the child can actually open: the resolved one,
    which is what plans_dir() grants, never the spelling that happened to
    arrive on argv. Pinned on the pointer's LITERAL text — a substring test for
    the resolved path alone passes on a prompt that also carries the argv
    spelling somewhere else."""
    real_dir = (tmp_path / "plans").resolve()
    real_dir.mkdir()
    monkeypatch.setattr(MOD, "plans_dir", lambda: real_dir)
    plan_path, _ = _two_stage_doc(real_dir)

    # An indirect spelling of the SAME file: resolve() collapses it, so a
    # pointer built from `args.plan` and one built from the resolved path are
    # distinguishable in the rendered text.
    argv_spelling = real_dir / "sub" / ".." / plan_path.name
    (real_dir / "sub").mkdir()

    prompt = MOD.assemble_prompt(_args(argv_spelling), depth=1, permissions="")
    expected = (
        f"## Working plan — stage 1 brief (projected; the full plan lives at "
        f"`{plan_path.resolve()}`, not inlined here)"
    )
    assert expected in prompt
    assert str(argv_spelling) not in prompt


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


def test_ceiling_inputs_are_the_ones_the_derivation_names():
    """The ceiling's INPUTS — never its output digit. Asserting the digit is
    what locked a hand-measured 144000 into the tree as if it were derived; a
    client release that moves a borrowed term must move the ceiling with it,
    and a test pinning the result would then fail on the correct new value."""
    # ours (the child's own compaction-window pin — untouched by this change)
    assert MOD.AUTOCOMPACT_CEILING_TOKENS == 150_000
    assert MOD.PRECOMPUTE_BUFFER_FRACTION == 0.2
    # borrowed from the installed client bundle
    assert MOD.OUTPUT_RESERVE_TOKENS == 20_000
    assert MOD.CLIENT_TRIGGER_FLOOR_MARGIN_TOKENS == 13_000
    # the roster floor standing in for the client's `model max` term
    assert MOD.MODEL_FLOOR_WINDOW_TOKENS == 200_000


def test_ceiling_reproduces_both_steps_of_the_client_trigger():
    window = min(MOD.MODEL_FLOOR_WINDOW_TOKENS, MOD.SPAWN_AUTOCOMPACT_WINDOW_TOKENS)
    usable = window - MOD.OUTPUT_RESERVE_TOKENS
    expected = min(
        round(usable * (1 - MOD.PRECOMPUTE_BUFFER_FRACTION)),
        usable - MOD.CLIENT_TRIGGER_FLOOR_MARGIN_TOKENS,
    )
    assert MOD.dispatch_prompt_ceiling_tokens("sonnet") == expected
    assert MOD.dispatch_prompt_ceiling_chars("sonnet") == int(
        expected * MOD.PROMPT_CHARS_PER_TOKEN
    )


def test_ceiling_errs_toward_refusing_early():
    """Direction of safety, the property the derivation exists to hold —
    stated as inequalities so it survives any re-measure of the inputs."""
    ceiling = MOD.dispatch_prompt_ceiling_tokens("sonnet")
    # (a) the min() binds BELOW the fraction-only reading of the trigger: a
    #     guard built from our three constants alone would be too generous.
    fraction_only = round(
        (MOD.SPAWN_AUTOCOMPACT_WINDOW_TOKENS - MOD.OUTPUT_RESERVE_TOKENS)
        * (1 - MOD.PRECOMPUTE_BUFFER_FRACTION)
    )
    assert ceiling < fraction_only
    # (b) the ceiling sits below the child's own pinned compaction window.
    assert ceiling < MOD.SPAWN_AUTOCOMPACT_WINDOW_TOKENS
    # (c) the char divisor sits below the 1.744 chars/token measured on the
    #     failed dispatch, so the char budget is smaller than the measurement
    #     would allow rather than larger.
    assert MOD.PROMPT_CHARS_PER_TOKEN < 1.744


def test_ceiling_for_an_inherited_model_equals_the_resolved_one(tmp_path):
    """Assertion (11)'s derivation half: an unresolved model (child inherits
    the parent's) must fall back to the roster floor, never to no ceiling and
    never to zero. Both mistakes pass every other assertion in this module."""
    assert MOD.dispatch_prompt_ceiling_tokens(None) == MOD.dispatch_prompt_ceiling_tokens("sonnet")
    assert MOD.dispatch_prompt_ceiling_chars(None) > 0


def test_prompt_exceeds_ceiling_boundary():
    ceiling = MOD.dispatch_prompt_ceiling_chars(None)
    assert MOD.prompt_exceeds_ceiling("x" * ceiling) is False
    assert MOD.prompt_exceeds_ceiling("x" * (ceiling + 1)) is True


def test_main_refuses_oversized_prompt_before_spawning(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(MOD, "plans_dir", lambda: tmp_path)
    plan_path, _ = _two_stage_doc(tmp_path)
    oversized_constraints = "x" * (MOD.dispatch_prompt_ceiling_chars(None) + 1)
    argv = [
        "--kind", "developer",
        "--plan", str(plan_path),
        "--done-criterion", "do the thing",
        "--criterion-type", "measurable",
        "--constraints", oversized_constraints,
        "--complexity", "medium",
        "--effort", "medium",
        "--stage-index", "1",
        "--plan-brief",
    ]
    rc = MOD.main(argv)
    assert rc == 5
    err = capsys.readouterr().err
    assert "exceeding" in err
    assert str(MOD.dispatch_prompt_ceiling_chars("sonnet")) in err


def test_main_refuses_oversized_prompt_on_whole_plan_path_too(tmp_path, monkeypatch, capsys):
    """The refusal is not brief-only: an oversized whole-plan prompt (no
    --plan-brief) must refuse identically."""
    monkeypatch.setattr(MOD, "plans_dir", lambda: tmp_path)
    plan_path, _ = _two_stage_doc(tmp_path)
    oversized_constraints = "x" * (MOD.dispatch_prompt_ceiling_chars(None) + 1)
    argv = [
        "--kind", "developer",
        "--plan", str(plan_path),
        "--done-criterion", "do the thing",
        "--criterion-type", "measurable",
        "--constraints", oversized_constraints,
        "--complexity", "medium",
        "--effort", "medium",
    ]
    rc = MOD.main(argv)
    assert rc == 5
    assert "exceeding" in capsys.readouterr().err


def test_main_requires_complexity_or_model_no_inherit_fallback(tmp_path, monkeypatch, capsys):
    """--complexity and --model are a required, mutually exclusive pair
    (build_parser's model_group) — omitting both is an argparse-level refusal
    before main() ever runs, not a silent inherit-the-parent-model fallback."""
    monkeypatch.setattr(MOD, "plans_dir", lambda: tmp_path)
    plan_path, _ = _two_stage_doc(tmp_path)
    argv = [
        "--kind", "code-reviewer",
        "--plan", str(plan_path),
        "--done-criterion", "do the thing",
        "--criterion-type", "measurable",
        "--stage-index", "2",
        "--plan-brief",
    ]
    with pytest.raises(SystemExit) as excinfo:
        MOD.main(argv)
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "--complexity" in err and "--model" in err
    assert "required" in err.lower()


# --- the refusal never reaches the launch site (assertion 10) ----------------


class _ChildLaunched(Exception):
    """Raised by the sentinel standing in for `proc_tree.launch_supervised`."""


@pytest.mark.parametrize(
    "path_argv, path_name",
    [
        (["--stage-index", "1", "--plan-brief"], "brief"),
        ([], "whole-plan"),
    ],
    ids=["brief", "whole-plan"],
)
def test_oversized_prompt_never_reaches_the_launch_site(
    tmp_path, monkeypatch, capsys, path_argv, path_name
):
    """The refusal's whole point is that no child is started, and `rc == 5`
    does not establish that. A return code is a claim made after the fact: a
    `main()` that spawned, measured, killed the child and then returned 5
    satisfies it exactly as well as one that refused before spawning, and so
    does one whose ceiling check drifts below the launch in a later refactor.
    Only the launch site itself can answer, so this substitutes a sentinel for
    `proc_tree.launch_supervised` — the single call that starts the child — and
    asserts it was never called, on BOTH the brief and whole-plan paths.

    The sentinel raises as well as records: were it reached, main() would
    otherwise carry a stand-in return value into its post-launch code and fail
    somewhere unrelated, reporting the wrong defect.

    Known residual: the sentinel binds to `proc_tree.launch_supervised` by name,
    so a rewrite that starts the child some other way (a bare `subprocess.Popen`)
    goes unobserved here. That is a supervision regression in its own right and
    is guarded where supervision is tested; naming it beats implying this test
    covers it.
    """
    monkeypatch.setattr(MOD, "plans_dir", lambda: tmp_path)
    plan_path, _ = _two_stage_doc(tmp_path)

    calls: list[tuple] = []

    def sentinel(*args, **kwargs):
        calls.append((args, kwargs))
        raise _ChildLaunched(f"a child was launched on the {path_name} path")

    # `proc_tree` is a module object shared with any other importer; monkeypatch
    # restores the real attribute at teardown.
    monkeypatch.setattr(MOD.proc_tree, "launch_supervised", sentinel)

    argv = [
        "--kind", "developer",
        "--plan", str(plan_path),
        "--done-criterion", "do the thing",
        "--criterion-type", "measurable",
        "--constraints", "x" * (MOD.dispatch_prompt_ceiling_chars(None) + 1),
        "--complexity", "medium",
        "--effort", "medium",
        *path_argv,
    ]
    rc = MOD.main(argv)

    assert calls == [], f"the {path_name} refusal launched a child before refusing"
    # Secondary: the refusal is the reason nothing launched, not an unrelated
    # early exit (a missing plan, an unparseable stage) that also never spawns.
    assert rc == 5
    assert "exceeding" in capsys.readouterr().err


# --- verify command's own -k selection, term by term (assertion 8) ---------
#
# pytest's exit code distinguishes only the empty TOTAL selection, never an
# empty term inside a disjunction: a suite run through
# `-k 'a or b or c'` reports green so long as ANY term matches something, so a
# keyword silently misspelled in a later rename removes its own share of
# coverage while the run as a whole keeps passing. Checked here by
# COLLECTION, one keyword at a time, never inferred from a green run of the
# six-term selection as a whole.

_VERIFY_COMMAND_KEYWORDS = (
    "stage_brief",
    "plan_brief",
    "prompt_continuity",
    "dispatch_semantics",
    "dispatch_cwd",
    "spawn_specialist_compose",
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _collected_node_ids(keyword: str) -> list[str]:
    """Every test node id a real `--collect-only -k <keyword>` selects under
    scripts/tests — a subprocess, not a parse of a green run's summary line,
    since a green run's exit code cannot tell an empty disjunction term from
    a populated one."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "scripts/tests", "-q", "--collect-only", "-k", keyword],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if "::test_" in line]


def test_stage_brief_and_plan_brief_keywords_collect_at_least_one_test():
    """The two LOAD-BEARING terms of the verify command's `-k` selection —
    both collected zero tests before this stage's work landed, because the
    tests they name are the ones this stage itself writes. Both are satisfied
    INSIDE this module: `stage_brief` matches by this file's own module name
    (`test_stage_brief`), `plan_brief` matches
    `test_build_argv_always_appends_plan_brief_flag` above. Recorded per
    keyword so a future reader sees which term proves what."""
    counts = {kw: len(_collected_node_ids(kw)) for kw in ("stage_brief", "plan_brief")}
    for kw, n in counts.items():
        assert n >= 1, f"load-bearing keyword {kw!r} collects zero tests: {counts}"
    print(f"\nload-bearing keyword coverage: {counts}")


def test_verify_command_keyword_selection_is_non_vacuous_term_by_term():
    """All six terms of the verify command's own `-k` selection. The other
    four (`prompt_continuity`, `dispatch_semantics`, `dispatch_cwd`,
    `spawn_specialist_compose`) are regression guards against a later rename
    — each already collects tests today, in its own sibling module
    (test_spawn_prompt_continuity.py, test_dispatch_semantics.py,
    test_dispatch_cwd.py, test_spawn_specialist_compose.py)."""
    counts = {kw: len(_collected_node_ids(kw)) for kw in _VERIFY_COMMAND_KEYWORDS}
    for kw, n in counts.items():
        assert n >= 1, (
            f"keyword {kw!r} in the verify command's -k selection collects zero tests: {counts}"
        )
    print(f"\nverify-command keyword coverage: {counts}")


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
