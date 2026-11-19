"""The method-statement rule: one rule text and one judge for every place a statement of the work stands.

Difficulty removed: the customer side of an order, a question to the user, a stage brief and a stage
description kept slipping in statements of HOW the product is made (a method, a means, a build) in
places where the reader needs the result named in the terms of their own activity, and every reviewer
read the rule differently. Whether a statement says how something is built is meaning, so a model
decides it (K4); a pattern may only pick candidates and never settles a verdict.

The judge is handed one statement and the place it stands in, and answers `method` (a decision on how
to act), `how_built` (a statement of how the product is built), `neither` (the result named in the
terms of the activity, which also covers an acceptance check with its reason) or `could_not_read`. What
a verdict means is fixed per place (`DEFECT_VERDICTS`): a defect where the reader is the customer or a
reader of the product; in place on the executor side (how built) and in a sub-plan (both), which are
the executor's own. `could_not_read` is a defect wherever a statement is judged: a statement nobody
read is not one that passed.

The binding-condition record (a condition the user or the agent sets on the work) and the question kind
`binding_conditions` are exempt by kind and never reach the judge; `binding_exempt` is the one place
that says so. An order in the old form (no customer side) is not judged at all.

Callers reach every seam as `method_statement.<name>` so a control can replace one at its module
attribute.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from . import grounding_judge as GJ
from . import plan as plan_mod

JUDGE_NAME = "method_statement"

METHOD = "method"
HOW_BUILT = "how_built"
NEITHER = "neither"
COULD_NOT_READ = "could_not_read"
VERDICTS = (METHOD, HOW_BUILT, NEITHER, COULD_NOT_READ)

CUSTOMER_SIDE = "customer_side"
EXECUTOR_SIDE = "executor_side"
QUESTION = "question"
BRIEF = "brief"
STAGE_DESCRIPTION = "stage_description"
SUB_PLAN = "sub_plan"
PLACES = (CUSTOMER_SIDE, EXECUTOR_SIDE, QUESTION, BRIEF, STAGE_DESCRIPTION, SUB_PLAN)

DEFECT_VERDICTS = {
    CUSTOMER_SIDE: frozenset({METHOD, HOW_BUILT}),
    EXECUTOR_SIDE: frozenset({METHOD}),
    QUESTION: frozenset({METHOD, HOW_BUILT}),
    BRIEF: frozenset({METHOD, HOW_BUILT}),
    STAGE_DESCRIPTION: frozenset({METHOD, HOW_BUILT}),
    SUB_PLAN: frozenset(),
}

BINDING_CONDITIONS_KIND = "binding_conditions"

RULE_TEXT = (
    "Method-statement rule. A statement of the work is one of: a METHOD (a decision on how to act: which "
    "approach, which steps, which instrument to choose), a HOW-BUILT statement (a statement of how the "
    "product is made or what it is made of), or NEITHER (the result the user needs, named in the terms of "
    "their own activity; an acceptance check with its reason is neither, even when it names a means, "
    "because it says how the result is observed and not how it is made). Where a statement stands "
    "decides what its verdict means: on the customer side of an order, in a question to the user, in a "
    "stage brief and in a stage description, both a method and a how-built statement are defects (name "
    "the product as the result the parent needs; what the parent requires of the build is set as a "
    "binding condition beside the text, which is not read as method); on the executor side of an order a "
    "method is a defect and a how-built statement is in place; in a sub-plan (means, method, procedure) "
    "both are in place, the method there being the executor's own. A statement you cannot read is "
    "reported as could-not-read, never passed."
)

TYPE_ENTRY_READING = (
    "Type-entry reading (order review): read the order's type entry against its recorded search. A "
    "listed result that should have counted as the type is a concern; so is a named resolving record "
    "that is not in fact the type. An explicit `no recorded type found` entry beside a recorded search "
    "that lists a matching record is a concern. Report each as a review concern, not as a verdict on a "
    "pattern."
)

_PROMPT = (
    "You judge one statement of the work against a rule. Answer with one JSON object "
    '{{"verdict": "method" | "how_built" | "neither" | "could_not_read", "reason": "<one sentence>"}} '
    "and nothing else.\n\n{rule}\n\nThe statement stands in the place `{place}`.\n\n"
    "Statement:\n{text}\n"
)


@dataclass(frozen=True)
class Statement:
    label: str
    place: str
    text: str


@dataclass(frozen=True)
class Finding:
    label: str
    place: str
    verdict: str
    reason: str


def binding_exempt() -> bool:
    """Whether the binding-condition record and the `binding_conditions` question kind stay out of the
    judge's hands. One seam, so a control can plant the defect of reading them."""
    return True


def default_runner():
    """The runner a call with none injected uses: the live host-model runner."""
    from . import advisor

    return advisor.subprocess_runner


def parse_reply(text: str) -> dict:
    obj = GJ._reply_object(text)
    verdict = obj.get("verdict")
    if verdict not in VERDICTS:
        raise ValueError(f"verdict {verdict!r} is not one of {', '.join(VERDICTS)}")
    return {"verdict": verdict, "reason": str(obj.get("reason", ""))}


def inputs_text(text: str, place: str) -> str:
    return json.dumps({"place": place, "text": text, "rule": RULE_TEXT}, sort_keys=True)


def judge(text: str, place: str, *, runner=None, cache=None) -> dict:
    """`{"verdict", "reason"}` for one statement in one place. A judge that cannot read yields
    `could_not_read`, a defect, and is not cached."""
    inputs = inputs_text(text, place)
    digest = hashlib.sha256(inputs.encode("utf-8")).hexdigest()
    cached = GJ._cache_read(cache, digest, inputs)
    if cached is not None:
        return {"verdict": cached["verdict"], "reason": cached.get("reason", "")}
    run = runner if runner is not None else default_runner()
    try:
        reply = GJ._ask(JUDGE_NAME, _PROMPT.format(rule=RULE_TEXT, place=place, text=text), run, parse_reply)
    except GJ.JudgeUnavailable as exc:
        return {"verdict": COULD_NOT_READ, "reason": str(exc)}
    if reply["verdict"] != COULD_NOT_READ:
        GJ._cache_write(cache, digest, {"status": "ok", "digest": digest, "inputs": inputs, **reply})
    return reply


def is_defect(verdict: str, place: str) -> bool:
    if verdict == COULD_NOT_READ:
        return place != SUB_PLAN
    return verdict in DEFECT_VERDICTS.get(place, frozenset())


def findings(statements, *, runner=None, cache=None) -> list[Finding]:
    """The verdict on every statement that is judged (a blank one and a sub-plan one are not)."""
    out = []
    for s in statements:
        if not (s.text or "").strip() or s.place == SUB_PLAN:
            continue
        reply = judge(s.text, s.place, runner=runner, cache=cache)
        out.append(Finding(s.label, s.place, reply["verdict"], reply["reason"]))
    return out


def defects(statements, *, runner=None, cache=None) -> list[Finding]:
    return [f for f in findings(statements, runner=runner, cache=cache) if is_defect(f.verdict, f.place)]


def defect_line(finding: Finding) -> str:
    if finding.verdict == COULD_NOT_READ:
        what = "the method-statement judge could not read it"
    elif finding.verdict == METHOD:
        what = "it states a method"
    else:
        what = "it states how the product is built"
    where = finding.place.replace("_", " ")
    return (f"{finding.label} ({where}): {what}" + (f" — {finding.reason}" if finding.reason else "")
            + "; name the result in the terms of the activity, and put a requirement on the build "
            "in a binding condition")


# --- the statements of each place ------------------------------------------------------------------


def order_statements(order) -> list[Statement]:
    """The text of a new-form order: its customer side and its executor side, and nothing kept beside
    it (method ideas, means conditions, the planning budget). An old-form order has none."""
    if not plan_mod.is_two_sided(order):
        return []
    side = order.customer_side
    out = [Statement(f"customer side {name}", CUSTOMER_SIDE, getattr(side, name))
           for name in ("step", "missing", "product")]
    executor = order.executor_side
    if executor is not None:
        out.append(Statement("executor side specifics", EXECUTOR_SIDE, executor.specifics))
    for r in order.requirements:
        out.append(Statement(f"requirement {r.id}", EXECUTOR_SIDE, f"{r.text}\n{r.derivation}".strip()))
    for p in order.proposals:
        out.append(Statement(f"proposal {p.id}", EXECUTOR_SIDE, f"{p.text}\n{p.critique}".strip()))
    for c in order.constraints:
        out.append(Statement(f"constraint {c.id}", EXECUTOR_SIDE, c.text))
    if not binding_exempt():
        for b in order.binding_conditions:
            out.append(Statement(f"binding condition {b.id}", CUSTOMER_SIDE,
                                 f"{b.text}\n{b.objection_text}".strip()))
    return out


def question_statements(text: str, kind: str, order) -> list[Statement]:
    """The question to the user as a statement; the `binding_conditions` kind is exempt."""
    if not plan_mod.is_two_sided(order):
        return []
    if kind == BINDING_CONDITIONS_KIND and binding_exempt():
        return []
    return [Statement("question", QUESTION, text)]


def stage_statements(stage, place: str) -> list[Statement]:
    """The prose of a stage description outside its sub-plan, labelled by field."""
    fields = (
        ("title", stage.title),
        ("result", stage.subject.result),
        ("material", stage.subject.material),
        ("invariants", stage.subject.invariants),
        ("knowledge", stage.knowledge),
        ("preconditions", stage.preconditions),
        ("done criterion", stage.criterion.done_criterion),
        ("observation", stage.criterion.observation),
    )
    return [Statement(f"stage {stage.index} {name}", place, text or "") for name, text in fields]


def brief_statements(doc, stage_index: int) -> list[Statement]:
    """The brief's own text, without the binding-condition records it carries beside it."""
    if not plan_mod.is_two_sided(doc.meta.order):
        return []
    stage = next((s for s in doc.stages if s.index == stage_index), None)
    if stage is None:
        return []
    out = stage_statements(stage, BRIEF)
    if not binding_exempt():
        for b in getattr(stage, "binding_conditions", ()) or ():
            out.append(Statement(f"stage {stage_index} binding condition {b.id}", BRIEF, b.text))
    return out


def plan_statements(doc, stages=None) -> list[Statement]:
    """Every stage description of a new-form plan (all stages, or those in `stages`)."""
    if not plan_mod.is_two_sided(doc.meta.order):
        return []
    out = []
    for stage in doc.stages:
        if stages is None or stage.index in stages:
            out.extend(stage_statements(stage, STAGE_DESCRIPTION))
    return out
