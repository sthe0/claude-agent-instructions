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
from . import sub_plan

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
    "Method-statement rule. A statement of the work is labelled by the terms it is stated in, never by "
    "who said it: a statement of the user's is labelled the same way as one of the agent's. It is NEITHER "
    "only when it is stated in terms of the customer's own activity, is external to the executor's "
    "planned activity, and stays valid whatever the executor chooses (the result the customer needs; an "
    "acceptance check with its reason is the customer's own activity of accepting the result, so it is "
    "neither even when it names a means). Stated in terms of the product's internals (how the product is "
    "made or what it is made of) it is a HOW-BUILT statement. Stated in terms of the executor's process "
    "or means (a decision on how to act: which approach, which steps, which instrument to choose) it is "
    "a METHOD. Where a statement stands "
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


def defect_row(place: str) -> list[str]:
    """What a verdict means at `place`: the verdicts that are defects there. Two places with the same row
    read a statement the same way, so one verdict serves both."""
    return sorted(DEFECT_VERDICTS.get(place, frozenset()))


def inputs_text(text: str, place: str) -> str:
    """Everything a stored verdict rests on: the statement, what a verdict means where it stands (the
    defect row, not the place's name), the rule, the prompt wrapper and the judge model."""
    return json.dumps({"row": defect_row(place), "text": text, "rule": RULE_TEXT, "prompt": _PROMPT,
                       "model": GJ._JUDGE_MODEL}, sort_keys=True)


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


def findings(statements, *, runner=None, cache=None, stop_on_unread=False) -> list[Finding]:
    """The verdict on every statement that is judged (a blank one and a sub-plan one are not). With
    `stop_on_unread` the list ends at the first statement the judge could not read: a judge that did not
    answer once is not asked again."""
    out = []
    for s in statements:
        if not (s.text or "").strip() or s.place == SUB_PLAN:
            continue
        reply = judge(s.text, s.place, runner=runner, cache=cache)
        out.append(Finding(s.label, s.place, reply["verdict"], reply["reason"]))
        if stop_on_unread and reply["verdict"] == COULD_NOT_READ:
            break
    return out


def defects(statements, *, runner=None, cache=None, stop_on_unread=False) -> list[Finding]:
    return [f for f in findings(statements, runner=runner, cache=cache, stop_on_unread=stop_on_unread)
            if is_defect(f.verdict, f.place)]


def cache_dir():
    """Where the production sites keep a read verdict, by the digest of `inputs_text`: the same statement
    read the same way, by the same prompt and model, is judged once."""
    return GJ.agentctl_dir() / "method-statement-cache"


def site_defects(statements, *, runner=None, stop_on_unread=False) -> list[Finding]:
    """The defects in `statements`, as every production site asks for them: judged through the digest
    cache, `runner` being the method judge's own (the live host-model runner when none)."""
    return defects(statements, runner=runner, cache=cache_dir(), stop_on_unread=stop_on_unread)


_UNREAD_ADVICE = {
    CUSTOMER_SIDE: "repeat the order review",
    EXECUTOR_SIDE: "repeat the order review or the executor revision",
    QUESTION: "raise the question again",
    BRIEF: "repeat the dispatch",
    STAGE_DESCRIPTION: "repeat the plan review",
}
_NAME_THE_RESULT = "name the result in the terms of the activity, and put a requirement on the build in a binding condition"
_METHOD_ADVICE = {
    CUSTOMER_SIDE: "how the work is done is the executor's to decide; state the result the customer needs instead",
    QUESTION: "how the work is done is the executor's to decide; ask about the result in the customer's terms",
    EXECUTOR_SIDE: "a method belongs to the executor's sub-plan (means, method, procedure), not to this side",
    BRIEF: "a method belongs to the stage's sub-plan (means, method, procedure); name the result here",
    STAGE_DESCRIPTION: "a method belongs to the stage's sub-plan (means, method, procedure); name the result here",
}


def _advice(finding: Finding) -> str:
    if finding.verdict == COULD_NOT_READ:
        return f"the judge did not answer; {_UNREAD_ADVICE.get(finding.place, 'repeat the review')}"
    if finding.verdict == METHOD:
        return _METHOD_ADVICE.get(finding.place, _NAME_THE_RESULT)
    return _NAME_THE_RESULT


def defect_line(finding: Finding) -> str:
    if finding.verdict == COULD_NOT_READ:
        what = "the method-statement judge could not read it"
    elif finding.verdict == METHOD:
        what = "it states a method"
    else:
        what = "it states how the product is built"
    where = finding.place.replace("_", " ")
    return (f"{finding.label} ({where}): {what}" + (f" — {finding.reason}" if finding.reason else "")
            + f"; {_advice(finding)}")


# --- the statements of each place ------------------------------------------------------------------


def order_statements(order, condition_decisions=()) -> list[Statement]:
    """The text of a new-form order: its customer side and its executor side, and nothing kept beside
    it (method ideas, means conditions, the planning budget). An old-form order has none. A binding
    condition reads as the root level's `condition_decisions` leave it (`sub_plan.current_condition_text`)."""
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
            text, _ = sub_plan.current_condition_text({"id": b.id, "text": b.text}, condition_decisions)
            out.append(Statement(f"binding condition {b.id}", CUSTOMER_SIDE,
                                 f"{text}\n{b.objection_text}".strip()))
    return out


def question_statements(text: str, kind: str, order, *, sourced: bool = False) -> list[Statement]:
    """The question to the user as a statement; the `binding_conditions` kind is exempt. `order` is the
    session's own and `sourced` says the session has an order draft or a plan: a session with neither is
    judged, one with either is judged only when that order is in the new form."""
    if sourced and not plan_mod.is_two_sided(order):
        return []
    if kind == BINDING_CONDITIONS_KIND and binding_exempt():
        return []
    return [Statement("question", QUESTION, text)]


def stage_statements(stage, place: str) -> list[Statement]:
    """The prose of a stage description, labelled by field: every prose field of the stage, `place`
    among them, and never the sub-plan (means, method, procedure, conditions on means), which is the
    executor's own record. The binding conditions the stage carries beside it are read as statements
    only when `binding_exempt` is lifted."""
    fields = (
        ("title", stage.title),
        ("place", stage.place),
        ("result", stage.subject.result),
        ("material", stage.subject.material),
        ("invariants", stage.subject.invariants),
        ("knowledge", stage.knowledge),
        ("preconditions", stage.preconditions),
        ("done criterion", stage.criterion.done_criterion),
        ("observation", stage.criterion.observation),
    )
    out = [Statement(f"stage {stage.index} {name}", place, text or "") for name, text in fields]
    if not binding_exempt():
        for b in stage.binding_conditions:
            out.append(Statement(f"stage {stage.index} binding condition {b['id']}", place, b["text"]))
    return out


def description_statements(doc, stage_index: int) -> list[Statement]:
    """The stage description of `stage_index`, at the place stage_description. Empty for a plan on an
    order in the old form, which is not judged."""
    if not plan_mod.is_two_sided(doc.meta.order):
        return []
    stage = next((s for s in doc.stages if s.index == stage_index), None)
    return [] if stage is None else stage_statements(stage, STAGE_DESCRIPTION)


def brief_statements(doc, stage_index: int) -> list[Statement]:
    """The brief's own text: the stage's prose fields, never its binding conditions."""
    if not plan_mod.is_two_sided(doc.meta.order):
        return []
    stage = next((s for s in doc.stages if s.index == stage_index), None)
    return [] if stage is None else stage_statements(stage, BRIEF)


def plan_statements(doc, stages=None) -> list[Statement]:
    """Every stage description of a new-form plan (all stages, or those in `stages`)."""
    out = []
    for stage in doc.stages:
        if stages is None or stage.index in stages:
            out.extend(description_statements(doc, stage.index))
    return out
