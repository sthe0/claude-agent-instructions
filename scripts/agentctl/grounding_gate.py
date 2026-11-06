"""The plan-side consumers of the grounding judge.

`grounding.py` refuses a stage whose `[stage.principle].source` does not name knowledge; this module
judges whether the knowledge it names supports the principle (stage 16's judge, `grounding_judge`) and
carries that verdict to every seam that admits or reviews plan bytes: submission, the essence, the
check-coverage pre-flight, the review bundles and `plan-review --verdict pass`.

Two parts, kept apart. The RULE part -- a stale given ground is refused (`given_problems`), an override
is shown to the user (`overrides_block`) -- is decided from the bytes and never waits on a model. The
MODEL part -- does the source support the principle, is an ontology distinction adequate, does an order
quotation say what a regularity attributes to it -- goes through the judge and fails open: a judge that
cannot look leaves a warning and never blocks.

`exempt_stages` is the one function that names the PASSED stages whose principle is unchanged; both
`cli._submission_problems` and `cli.cmd_plan_review` call it, so the two seams judge the same stages.
Callers reach every seam as `grounding_gate.<name>` so a control can replace one at its module attribute.
"""
from __future__ import annotations

import shlex
from pathlib import Path

from lib import leaf_forms
from lib import principle_provenance as PROV

from . import advisor, grounding, render
from . import grounding_judge as GJ

OVERRIDES_HEADER = "Grounding overrides (for approval):"
NO_OVERRIDES_LINE = "- none: no stage keeps a source the grounding judge found defective"
_SUBSTANTIVE = "SUBSTANTIVE"
_HEAD_CHARS = 1200


def judge_enabled(weight_class) -> bool:
    try:
        return advisor.resolve_enabled(weight_class)
    except Exception:
        return False


def exempt_stages(state, plan, baseline) -> frozenset[int]:
    """PASSED stages whose principle `plan` leaves as `baseline` had it: finished work is not judged again,
    an edited principle is. One function for submission and plan-review alike."""
    from . import cli  # local: cli is this module's only caller and imports it

    return cli._grounding_exempt_stages(state, baseline, plan)


def judged_stage(stage, exempt) -> bool:
    return stage.principle is not None and stage.index not in exempt


def stage_override(stage) -> str | None:
    return ((stage.principle.grounding_override if stage.principle is not None else "") or "").strip() or None


def stage_checked_paths(stage) -> list[str]:
    """The paths the stage names as read or checked: its knowledge and material refs and every token of its
    verify command. A given ground whose `given_source` is among them has been checked by the stage."""
    crit = getattr(stage, "criterion", None)
    command = getattr(crit, "verify_command", "") or ""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    subject = stage.subject
    return [p for p in [*(subject.knowledge_refs or []), *(subject.material_refs or []), *tokens] if p]


# --- the grounds a stage cites -----------------------------------------------------------------------


def _body_head(text: str) -> str:
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---\n", 3)
        body = text[end + 5:] if end >= 0 else text
    return " ".join(body.split())[:_HEAD_CHARS]


def _ground_fields(ground: dict) -> dict:
    return {k: v for k, v in ground.items() if k in GJ.GROUND_FIELDS or k == "last_verified"}


def _ground_of_ref(ref, corpus, adr_dir) -> dict | None:
    if ref.kind == grounding.ONTOLOGY_KIND:
        target = grounding.ontology_target(ref, corpus, adr_dir)
        if target is None:
            return None
        leaf = grounding.leaf_for(ref, corpus)
        if leaf is not None and leaf.path == target:
            return _ground_fields(GJ.ground_of(leaf))
        try:
            return {"distinction": _body_head(Path(target).read_text(encoding="utf-8"))}
        except OSError:
            return None
    leaf = grounding.leaf_for(ref, corpus)
    if leaf is None or not grounding.kind_matches(ref, leaf):
        return None
    if ref.kind == "regularity":
        return _ground_fields(PROV.given_ground(leaf, corpus) or GJ.ground_of(leaf))
    return _ground_fields(GJ.case_ground(leaf))


def stage_grounds(stage, corpus, adr_dir=None) -> list[dict]:
    """The ground of every resolvable ref of the stage's source, as the judge reads it."""
    adr_dir = grounding.engine_adr_dir() if adr_dir is None else adr_dir
    refs, _bad = grounding.parse_ground_refs(stage.principle.source)
    out = []
    for ref in refs:
        if ref.kind in grounding.REFUSED_KINDS:
            continue
        ground = _ground_of_ref(ref, corpus, adr_dir)
        if ground is not None:
            out.append({"ref": ref.text(), **ground})
    return out


# --- the stage-level judge ---------------------------------------------------------------------------


def _outcome(stage, grounds, corpus, runner) -> dict:
    p = stage.principle
    prescription = {"id": f"stage-{stage.index}", "question": "link", "principle": p.statement,
                    "grounded_in": [g["ref"] for g in grounds]}
    context = f"stage {stage.index}: {stage.title}\nderivation: {p.derivation}"
    return GJ.judge_grounding_link(
        prescription, grounds, GJ.candidate_grounds(corpus), runner=runner, cache=GJ.cache_dir(),
        checked_paths=stage_checked_paths(stage), context=context, override=stage_override(stage))


def _defects_of(outcome) -> list[str]:
    """The defects the model found; the rule defect is `given_problems`' alone."""
    return list(outcome.get("link_defects") or []) + [
        d for d in outcome.get("ground_defects") or [] if d not in GJ.RULE_DEFECTS]


def stage_link_problems(stage, grounds, corpus, *, runner, warnings) -> list[str]:
    """The refusals the judge gives one stage's link: its defects, unless the stage overrides them. A judge
    that cannot look adds a warning; only an outcome that says it blocks is a problem."""
    outcome = _outcome(stage, grounds, corpus, runner)
    where = f"stage {stage.index} [stage.principle].source"
    if outcome.get("status") != "ok":
        if outcome.get("warning"):
            warnings.append(f"stage {stage.index}: {outcome['warning']}")
        return [f"{where}: the grounding judge could not look and is set to block"] if outcome.get("blocking") else []
    defects = _defects_of(outcome)
    if not defects or outcome.get("overridden"):
        return []
    return [f"{where}: the grounding judge found the source does not support the principle "
            f"({', '.join(defects)}): {outcome.get('reason') or 'no reason given'}; cite grounds that do, or keep "
            "it with [stage.principle] grounding_override = '<reason>'"]


def given_problems(stage, grounds, *, today=None) -> list[str]:
    """Every cited ground that is an unverified given: refused by rule, with or without a judge, and not
    waivable by an override. A stage discharges one by naming the path it checked."""
    checked = stage_checked_paths(stage)
    return [f"stage {stage.index} [stage.principle].source: {g['ref']} is an unverified given (last verified "
            f"{g.get('last_verified') or 'never'}); verify it and name the path checked in knowledge_refs, "
            "material_refs or verify_command, or cite a ground that is not given"
            for g in grounds if not GJ.given_currency(g, checked_paths=checked, today=today)]


def order_quote_problems(doc, corpus, *, runner, warnings) -> list[str]:
    """For each order-derived regularity the plan introduces: does its quotation say what the statement
    attributes to it, and does it state a fact rather than a demand."""
    out = []
    for leaf in render.generalization_candidates(corpus.items, doc.meta.task_id):
        quote = PROV.order_quote(leaf.text) if leaf.area == "regularity" else None
        if not quote:
            continue
        result = GJ.judge_order_quote(quote, GJ.ground_of(leaf).get("statement", ""), runner=runner)
        name = f"{leaf.tier}:{leaf.area}:{leaf.slug}"
        if result.get("status") != "ok":
            if result.get("warning"):
                warnings.append(f"{name}: {result['warning']}")
            continue
        if GJ.UNFAITHFUL_QUOTE in result.get("defects", []):
            out.append(f"{name}: the order quotation does not say what the regularity attributes to it")
        if GJ.STATES_DEMAND in result.get("defects", []):
            out.append(f"{name}: the order quotation states a demand, not a fact; a demand stays a norm "
                       "and is not generalized into a regularity")
    return out


# --- the plan-level pass ------------------------------------------------------------------------------


def plan_problems(doc, exempt, *, runner, enabled, warnings=None, only=None, today=None) -> list[str]:
    """The grounding judge's problems for the stages outside `exempt` (and, when `only` is given, that one
    stage). The given rule always runs; the model part only when `enabled`. A stage whose source does not
    resolve is left to the structural check, which names it."""
    from . import cli

    warnings = [] if warnings is None else warnings
    if not grounding.is_substantive(doc):
        return []
    inputs = cli._grounding_inputs(doc)
    if not Path(inputs["global_leaves"]).is_dir():
        return []
    corpus = grounding.load_ground_corpus(**inputs)
    run = runner if runner is not None else advisor.subprocess_runner
    out: list[str] = []
    for stage in doc.stages:
        if not judged_stage(stage, exempt) or (only is not None and stage.index != only):
            continue
        if grounding.source_problems(stage.principle.source, corpus):
            continue
        grounds = stage_grounds(stage, corpus)
        out.extend(given_problems(stage, grounds, today=today))
        if enabled:
            out.extend(stage_link_problems(stage, grounds, corpus, runner=run, warnings=warnings))
    if enabled and only is None:
        out.extend(order_quote_problems(doc, corpus, runner=run, warnings=warnings))
    return out


def _verdict_line(stage, outcome) -> str:
    if outcome.get("status") != "ok":
        return f"- stage {stage.index}: not judged ({outcome.get('warning') or 'no verdict'})"
    defects = _defects_of(outcome)
    if not defects:
        return f"- stage {stage.index}: sound"
    note = f"overridden: {outcome.get('override')}" if outcome.get("overridden") else "defective"
    return f"- stage {stage.index}: {note} ({', '.join(defects)}) -- {outcome.get('reason') or 'no reason given'}"


def _verdict_lines(doc, exempt, runner) -> list[str]:
    from . import cli

    if not grounding.is_substantive(doc):
        return []
    inputs = cli._grounding_inputs(doc)
    if not Path(inputs["global_leaves"]).is_dir():
        return []
    corpus = grounding.load_ground_corpus(**inputs)
    lines = []
    for stage in doc.stages:
        if not judged_stage(stage, exempt) or grounding.source_problems(stage.principle.source, corpus):
            continue
        lines.append(_verdict_line(stage, _outcome(stage, stage_grounds(stage, corpus), corpus, runner)))
    return lines


def coverage_verdict_lines(doc, exempt, *, runner, enabled) -> list[str]:
    """One verdict line per judged stage, for the check-coverage pre-flight."""
    if not enabled:
        return []
    return _verdict_lines(doc, exempt, runner if runner is not None else advisor.subprocess_runner)


def _cache_only(argv, *, timeout, stdin):
    raise RuntimeError("cache-only: no model call")


def review_verdict_lines(doc) -> list[str]:
    """The verdicts already on file for the plan's stages, for a review bundle. Reads the judge's cache
    only: a bundle renders without a model call, and a stage never judged says so."""
    if not judge_enabled(_SUBSTANTIVE):
        return []
    return _verdict_lines(doc, frozenset(), _cache_only)


def review_pass_blockers(doc, exempt, *, runner, weight_class, warnings, only=None) -> list[str]:
    """The un-overridden defects that stand in the way of a `pass` verdict."""
    return plan_problems(doc, exempt, runner=runner, enabled=judge_enabled(weight_class),
                         warnings=warnings, only=only)


# --- the essence -------------------------------------------------------------------------------------


def overrides_block(doc) -> str:
    """Every grounding override of the plan with its reason, generated from the plan and never
    hand-maintained: present-plan requires this block's text inside an essence."""
    lines = [OVERRIDES_HEADER]
    for stage in doc.stages:
        reason = stage_override(stage)
        if reason is not None:
            lines.append(f"- stage {stage.index}: {reason} (source: {stage.principle.source})")
    if len(lines) == 1:
        lines.append(NO_OVERRIDES_LINE)
    return "\n".join(lines) + "\n"


def essence_overrides_problems(doc, text) -> list[str]:
    """A refusal when the essence does not contain the plan's grounding-overrides block."""
    if overrides_block(doc).strip() in text:
        return []
    return ["essence rendering omits the grounding-overrides block"]
