"""Resolve a control NAME against the plan that would contain it.

Two callers ask the same question — does the control this string names exist in this
plan? — over DIFFERENT admissible grammars. `check-order-coverage.py` asks it of every
`[meta.order.coverage]` entry, and a name outside its three forms is a FAILURE rather
than a silent skip. The question-materiality check at `question-raise` asks it of the
control a question claims to bear on, and needs two forms the coverage map never uses:
a stage's done_criterion, and a requirement of the order.

A second copy of the resolver would drift from the first; one resolver holding the
union of both grammar sets would silently widen the coverage verdict, admitting as
covered a control name that check has always rejected. So the admissible set is a
PARAMETER with no default: each caller names exactly the forms it accepts, and neither
can move the other's verdict.

A grammar matches by PREFIX, never by equality, so an author may append a trailing
free-prose parenthetical after the fixed form — e.g. "stage 12 landed assertion (full:
containment in main and origin/main, plus reachability of the delivered commit)".
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from .plan import PlanDoc


@dataclass(frozen=True)
class Grammar:
    form: str
    pattern: re.Pattern[str]
    resolve: Callable[[re.Match[str], PlanDoc, str], str | None]
    describe: Callable[[re.Match[str], PlanDoc], str]


def _stages_by_index(doc: PlanDoc) -> dict:
    return {s.index: s for s in doc.stages}


def _stage_verify_command(m, doc: PlanDoc, name: str) -> str | None:
    idx = int(m.group(1))
    stage = _stages_by_index(doc).get(idx)
    if stage is None:
        return f"{name!r}: no stage {idx}"
    if not stage.criterion.verify_command:
        return f"{name!r}: stage {idx} declares no verify_command"
    return None


def _final_check(m, doc: PlanDoc, name: str) -> str | None:
    idx = int(m.group(1))
    if idx < 1 or idx > len(doc.meta.final_check):
        return f"{name!r}: no final_check {idx} (plan has {len(doc.meta.final_check)})"
    return None


def _stage_landed_assertion(m, doc: PlanDoc, name: str) -> str | None:
    idx = int(m.group(1))
    stage = _stages_by_index(doc).get(idx)
    if stage is None:
        return f"{name!r}: no stage {idx}"
    if stage.criterion.verify_kind != "landed":
        return f"{name!r}: stage {idx} is not a landed-kind criterion"
    return None


def _stage_done_criterion(m, doc: PlanDoc, name: str) -> str | None:
    idx = int(m.group(1))
    stage = _stages_by_index(doc).get(idx)
    if stage is None:
        return f"{name!r}: no stage {idx}"
    if not stage.criterion.done_criterion:
        return f"{name!r}: stage {idx} declares no done_criterion"
    return None


def _order_requirement(m, doc: PlanDoc, name: str) -> str | None:
    req_id = m.group(1)
    order = doc.meta.order
    if order is None:
        return f"{name!r}: the plan declares no [meta.order]"
    if not any(r.id == req_id for r in order.requirements):
        return f"{name!r}: no requirement {req_id!r} in [meta.order]"
    return None


def _describe_stage_verify_command(m, doc: PlanDoc) -> str:
    stage = _stages_by_index(doc).get(int(m.group(1)))
    return stage.criterion.verify_command if stage else ""


def _describe_final_check(m, doc: PlanDoc) -> str:
    idx = int(m.group(1))
    checks = doc.meta.final_check
    if not 1 <= idx <= len(checks):
        return ""
    check = checks[idx - 1]
    return check.label or check.command


def _describe_stage_landed_assertion(m, doc: PlanDoc) -> str:
    stage = _stages_by_index(doc).get(int(m.group(1)))
    spec = stage.criterion.landed if stage else None
    if spec is None:
        return ""
    return (f"the commit stage {spec.delivered_stage} delivered is contained in "
            f"{spec.target} and {spec.remote}/{spec.target}")


def _describe_stage_done_criterion(m, doc) -> str:
    stage = _stages_by_index(doc).get(int(m.group(1)))
    return stage.criterion.done_criterion if stage else ""


def _describe_order_requirement(m, doc) -> str:
    order = doc.meta.order
    if order is None:
        return ""
    match = next((r for r in order.requirements if r.id == m.group(1)), None)
    return match.text if match else ""


STAGE_VERIFY_COMMAND = Grammar(
    "stage <n> verify_command",
    re.compile(r"^stage (\d+) verify_command\b"),
    _stage_verify_command,
    _describe_stage_verify_command,
)
FINAL_CHECK = Grammar(
    "final_check <n>",
    re.compile(r"^final_check (\d+)\b"),
    _final_check,
    _describe_final_check,
)
STAGE_LANDED_ASSERTION = Grammar(
    "stage <n> landed assertion",
    re.compile(r"^stage (\d+) landed assertion\b"),
    _stage_landed_assertion,
    _describe_stage_landed_assertion,
)
STAGE_DONE_CRITERION = Grammar(
    "stage <n> done_criterion",
    re.compile(r"^stage (\d+) done_criterion\b"),
    _stage_done_criterion,
    _describe_stage_done_criterion,
)
ORDER_REQUIREMENT = Grammar(
    "order requirement <id>",
    re.compile(r"^order requirement (\S+)"),
    _order_requirement,
    _describe_order_requirement,
)

# Widening this tuple widens what `check-order-coverage.py` accepts as a resolved
# control, on every plan already in the tree, with no other symptom.
COVERAGE_GRAMMARS = (STAGE_VERIFY_COMMAND, FINAL_CHECK, STAGE_LANDED_ASSERTION)

MATERIALITY_GRAMMARS = COVERAGE_GRAMMARS + (STAGE_DONE_CRITERION, ORDER_REQUIREMENT)

_COUNT_WORDS = ("no", "one", "two", "three", "four", "five")


def _count_word(n: int) -> str:
    return _COUNT_WORDS[n] if n < len(_COUNT_WORDS) else str(n)


def _match(name: str, grammars: tuple[Grammar, ...]):
    for grammar in grammars:
        m = grammar.pattern.match(name)
        if m:
            return grammar, m
    return None


def resolve_control(name: str, doc: PlanDoc, *,
                    grammars: tuple[Grammar, ...]) -> str | None:
    """None if `name` resolves against `doc` under one of `grammars`; otherwise the
    reason it does not. `grammars` has no default: a caller that does not state its
    admissible set does not get one."""
    matched = _match(name, grammars)
    if matched:
        grammar, m = matched
        return grammar.resolve(m, doc, name)

    forms = ", ".join(f"{g.form!r}" for g in grammars)
    return (
        f"{name!r}: matches none of the {_count_word(len(grammars))} accepted "
        f"control-name grammars ({forms}) — a name outside the grammar is a "
        f"failure, never a skip"
    )


def control_text(name: str, doc, *, grammars: "tuple[Grammar, ...]") -> str:
    """What the control `name` addresses actually says, for a reader who cannot
    dereference the address itself. "" when `name` does not resolve."""
    for grammar in grammars:
        m = grammar.pattern.match(name)
        if m:
            return grammar.describe(m, doc) if grammar.resolve(m, doc, name) is None else ""
    return ""
