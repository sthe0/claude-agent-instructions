"""The order part of a spawn's brief, and the size line that decides when it is required.

A brief above the size line must carry the five parts of an order: the place the work
serves, what is missing there, the product, the acceptance checks and the binding
conditions. This module is pure: it judges size and parts from values the spawner reads
(the executed stage, the explicit `--declared-size` / `--size-flags`, the brief text) and
returns the verdict; the refusal itself is raised by `spawn-specialist.assemble_prompt`.

Size sources (part one of the delegation work builds the first and the third):

  1. the executed stage's own declaration (`--stage-index` naming a stage whose executor is
     `spawn:<kind>`), judged by `plan.stage_above_threshold`;
  2. the session's class for a spawn naming `--session` (handed planning) — a later part;
  3. explicit `--declared-size` / `--size-flags`, judged by `classify.above_threshold`.

Explicit inputs only RAISE the class inside a stage's own declaration; they never lower
it. Outside a stage they decide alone, and a spawn that declares nothing is small.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import classify, gates, plan

PART_HEADINGS = (
    ("place", "Place:", "place"),
    ("missing", "What is missing:", "what is missing"),
    ("product", "Product:", "product"),
    ("checks", "Acceptance checks:", "acceptance checks"),
    ("conditions", "Binding conditions:", "binding conditions"),
)

_HEADING_RES = tuple(
    (key, re.compile(rf"^[ \t]*(?:#{{1,6}}[ \t]+)?(?:[-*][ \t]+)?\**{re.escape(heading)}\**[ \t]*(.*)$"))
    for key, heading, _name in PART_HEADINGS)

NONE_TEXT = "none"


@dataclass(frozen=True)
class SizeVerdict:
    """Whether the spawn is above the size line, and from which source: `stage` (the executed
    stage's declaration, raised or not by explicit inputs), `explicit` (explicit inputs alone)
    or `undeclared` (nothing declared: small)."""
    above: bool
    source: str


def size_flag_names() -> tuple[str, ...]:
    """The accepted `--size-flags` spellings, the ones `classify.above_threshold` reads."""
    return tuple(classify.SIZE_FLAGS)


def explicit_declared(declared_size: "int | None", size_flags) -> bool:
    return declared_size is not None or bool(size_flags)


def resolve_size(*, stage, declared_size: "int | None", size_flags, thr) -> SizeVerdict:
    """The one size computation of a spawn. `stage` is the executed stage (None when the spawn
    does not execute one). Never reads a threshold of its own: the line is `thr`'s."""
    explicit = explicit_declared(declared_size, size_flags)
    raised = explicit and classify.above_threshold(declared_size, tuple(size_flags or ()), None, thr)
    if stage is not None:
        return SizeVerdict(plan.stage_above_threshold(stage, thr) or raised, "stage")
    return SizeVerdict(raised, "explicit" if explicit else "undeclared")


def headed_sections(text: str) -> "dict[str, str]":
    """The five order parts a brief text states as headed sections ('Place:', 'What is
    missing:', 'Product:', 'Acceptance checks:', 'Binding conditions:'): each part's text is
    what follows its heading up to the next of the five headings. A heading may be a markdown
    heading, bullet or bold. A part stated twice reads as its last statement."""
    found: dict[str, list[str]] = {}
    current: "str | None" = None
    for line in (text or "").splitlines():
        for key, pattern in _HEADING_RES:
            m = pattern.match(line)
            if m:
                current = key
                found[key] = [m.group(1)]
                break
        else:
            if current is not None:
                found[current].append(line)
    return {key: "\n".join(lines).strip() for key, lines in found.items()}


def binding_condition_lines(stage, how_kept, decisions, inherited) -> list[str]:
    """The binding conditions a stage-scoped spawn is handed: the stage description's own, then
    each inherited condition a how-kept entry names, every text read through
    `gates.current_condition_text` (a condition its setter changed shows its new text). A how-kept
    condition whose text cannot be read here (`inherited` is None: no readable session) is named
    with the way it is kept, so the brief still says it exists."""
    rows = gates.with_current_text(stage.binding_conditions, decisions)
    lines = [f"- {r['id']}: {r['text']}" for r in rows if str(r.get("text", "")).strip()]
    seen = {r.get("id") for r in rows}
    by_id = {r["id"]: r for r in gates.with_current_text(inherited or (), decisions)}
    for held in how_kept:
        if held.condition in seen:
            continue
        seen.add(held.condition)
        row = by_id.get(held.condition)
        if row is not None:
            lines.append(f"- {row['id']} (inherited): {row['text']}")
        else:
            lines.append(f"- {held.condition} (inherited; its text is not readable here): "
                         f"kept by: {held.how}")
    return lines


def order_parts(headed: "dict[str, str]", *, stage=None, condition_lines=()) -> "dict[str, str]":
    """The five parts of the brief. A new-form executed stage states place, product, acceptance
    checks and binding conditions itself (an explicitly empty condition list reads as `none`); a
    part the stage leaves blank or unstated, and always 'what is missing', comes from the headed
    sections of the brief text, so the refusal's way on (add the headed section) reaches it."""
    parts = {key: headed.get(key, "") for key, _h, _n in PART_HEADINGS}
    if stage is not None and stage.place is not None:
        stated = {
            "place": (stage.place or "").strip(),
            "product": (stage.subject.result or "").strip(),
            "checks": (stage.criterion.done_criterion or "").strip(),
            "conditions": "\n".join(condition_lines) if condition_lines
            else (NONE_TEXT if stage.binding_conditions_stated else ""),
        }
        parts.update({key: text for key, text in stated.items() if text})
    return parts


def order_part_violations(parts: "dict[str, str]") -> list[str]:
    """The names of the parts a brief above the line lacks: a part that is absent or blank. An
    explicit `none` is text and is accepted."""
    return [name for key, _heading, name in PART_HEADINGS if not parts.get(key, "").strip()]


def refusal_text(violations: list[str], verdict: SizeVerdict, *, stage_index: "int | None") -> str:
    """The refusal a brief above the line gets, naming the missing parts and the way on."""
    where = (f"the declaration of stage {stage_index}" if verdict.source == "stage"
             else "the explicit --declared-size / --size-flags")
    headings = ", ".join(f"'{h}'" for _k, h, _n in PART_HEADINGS)
    return (
        f"this brief is above the size line ({where}) and does not carry the order parts: "
        f"{', '.join(violations)}. Add each as a headed section ({headings}) to --constraints, "
        f"--context-dossier or --done-criterion; an explicit 'none' is accepted, an absent part is "
        f"not. If the work is small, say so with --declared-size 0 and no --size-flags; that "
        f"lowers only a spawn that executes no stage declaring a larger size."
    )


def order_section_lines(parts: "dict[str, str]") -> list[str]:
    """The brief section a stage-scoped spawn carries: the five parts as the stage states them."""
    lines = ["## The order this stage carries", ""]
    for key, heading, _name in PART_HEADINGS:
        lines += [heading, parts[key].strip(), ""]
    return lines
