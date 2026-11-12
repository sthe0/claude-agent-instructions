"""Is each of a stage's `conditions` marked by how it is secured?

The defect (group D2): a premise about the world is adopted at planning without a probe, and the
stage built on it fails when the premise meets reality; meanwhile `conditions` holds conduct norms
("edits only in the worktree", "no push") that belong in `invariants`. ADR-0005 open item 4
records the means-conditions securing relation this module implements.

Format. One condition per line starting with ``- `` (no such line: the whole text is one
condition); no text before the first ``- `` line. Each condition ends with exactly one securing
mark, and the mark ends the condition -- no line continues a condition after its mark:

* ``[probe: <command or observation> => <observed result>]`` -- checked, holds;
* ``[probe-revised: <command> => <observed result>; was: <premise as first written>]`` -- a probe
  falsified the first premise and the condition was rewritten to the true state;
* ``[means: stage <N>]`` -- secured by stage N's means; N must be a transitive `depends_on`
  ancestor;
* ``[risk: <why the premise is accepted unprobed>]`` -- an explicit risk acceptance.

Parsing the mark is parsing an agent-authored structural tag, not classifying meaning, so it sits
inside the carve-out of regex-not-for-semantic-classification. Whether a line is a world state or
a conduct norm is NOT decided here; the refusal text and the planner docs carry that.

Gated by the `conditions-securing` flag (config.md), decided once per session at its first
accepted submission (`SessionState.conditions_securing_required`), so a plan already in flight
when the flag landed is exempt.
"""

from __future__ import annotations

import re
from typing import NamedTuple

MARK_KINDS = ("probe", "probe-revised", "means", "risk")
# The fixed phrase every securing refusal carries; the engine tells its own refusals apart by it.
REFUSAL_TAG = "`conditions` not secured"

_MARK = re.compile(r"\[(probe-revised|probe|means|risk):(.*)\]\s*$", re.DOTALL)
_PROBE = re.compile(r"^(.*?)=>(.*)$", re.DOTALL)
_REVISED = re.compile(r"^(.*?)=>(.*?);\s*was:(.*)$", re.DOTALL)
_MEANS = re.compile(r"^\s*stage\s+(\d+)\s*$", re.IGNORECASE)


class Condition(NamedTuple):
    text: str
    mark_kind: str | None
    fields: dict


_MARK_OPEN = re.compile(r"\[(?:probe-revised|probe|means|risk):")


def _items(conditions: str) -> tuple[list[str], list[list[str]]]:
    """(non-blank lines before the first `- ` line, each condition's non-blank lines)."""
    lines = [line.strip() for line in conditions.splitlines()]
    if not any(line.startswith("- ") for line in lines):
        whole = [line for line in lines if line]
        return [], [whole] if whole else []
    preamble: list[str] = []
    items: list[list[str]] = []
    for stripped in lines:
        if stripped.startswith("- "):
            items.append([stripped[2:].strip()])
        elif stripped:
            (items[-1] if items else preamble).append(stripped)
    return preamble, items


def _split(conditions: str) -> list[str]:
    return [" ".join(item) for item in _items(conditions)[1]]


def grammar_problems(conditions: str | None) -> list[str]:
    """Layout violations the mark parser would otherwise read past: text before the first `- `
    line, a condition with more than one mark, and lines continuing a condition after its mark."""
    preamble, items = _items(conditions or "")
    problems: list[str] = []
    if preamble:
        problems.append(f"text before the first `- ` condition line {preamble[0][:80]!r}")
    for item in items:
        label = " ".join(item)[:80]
        marks = sum(len(_MARK_OPEN.findall(line)) for line in item)
        if marks > 1:
            problems.append(f"condition {label!r} carries {marks} marks; exactly one is allowed")
        elif any(_MARK_OPEN.search(line) for line in item[:-1]):
            problems.append(
                f"condition {label!r}: a line follows its mark; the mark must end the condition "
                f"(move the text before the mark, or start a new `- ` condition)"
            )
    return problems


def _fields(kind: str, body: str) -> dict:
    if kind == "probe":
        m = _PROBE.match(body)
        return {"probe": m.group(1).strip(), "observed": m.group(2).strip()} if m else {}
    if kind == "probe-revised":
        m = _REVISED.match(body)
        if not m:
            return {}
        return {"probe": m.group(1).strip(), "observed": m.group(2).strip(), "was": m.group(3).strip()}
    if kind == "means":
        m = _MEANS.match(body)
        return {"stage": int(m.group(1))} if m else {}
    return {"reason": body.strip()}


def parse_marks(conditions: str | None) -> list[Condition]:
    """Each condition of `conditions` with its trailing securing mark (kind None when unmarked)."""
    out: list[Condition] = []
    for item in _split(conditions or ""):
        m = _MARK.search(item)
        if not m:
            out.append(Condition(item, None, {}))
            continue
        kind = m.group(1)
        out.append(Condition(item[: m.start()].strip(), kind, _fields(kind, m.group(2))))
    return out


def ancestors(stage_index: int, depends_on: dict[int, list[int]]) -> set[int]:
    """Transitive `depends_on` ancestors of `stage_index`."""
    seen: set[int] = set()
    stack = list(depends_on.get(stage_index, ()))
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        stack.extend(depends_on.get(n, ()))
    return seen


def securing_problems(stage, stage_ancestors: set[int]) -> list[str]:
    """The securing violations of one stage's `conditions`, as author-readable strings."""
    problems: list[str] = grammar_problems(stage.conditions)
    for cond in parse_marks(stage.conditions):
        label = cond.text[:80] or "(empty)"
        if cond.mark_kind is None and _MARK_OPEN.search(cond.text):
            problems.append(f"condition {label!r}: text follows its mark; the mark must end the line")
        elif cond.mark_kind is None:
            problems.append(f"unmarked condition {label!r}")
        elif cond.mark_kind == "probe" and not (cond.fields.get("probe") and cond.fields.get("observed")):
            problems.append(f"condition {label!r}: [probe: <command> => <observed result>] needs both halves")
        elif cond.mark_kind == "probe-revised" and not (
            cond.fields.get("probe") and cond.fields.get("observed") and cond.fields.get("was")
        ):
            problems.append(
                f"condition {label!r}: [probe-revised: <command> => <observed result>; was: <first premise>] "
                f"needs the probe, its result and a non-empty was:"
            )
        elif cond.mark_kind == "means":
            n = cond.fields.get("stage")
            if n is None:
                problems.append(f"condition {label!r}: [means: stage <N>] needs a stage number")
            elif n not in stage_ancestors:
                problems.append(
                    f"condition {label!r}: [means: stage {n}] names a stage that is not a "
                    f"transitive depends_on ancestor of stage {stage.index}"
                )
        elif cond.mark_kind == "risk" and not cond.fields.get("reason"):
            problems.append(f"condition {label!r}: [risk: <reason>] needs a reason")
    if not problems:
        return []
    return [
        f"stage {stage.index} {REFUSAL_TAG}: {'; '.join(problems)}. Each condition is a "
        f"state of the world, one per `- ` line, ending with exactly one of [probe: cmd => result], "
        f"[probe-revised: cmd => result; was: first premise], [means: stage N] (a depends_on "
        f"ancestor) or [risk: reason]. Conduct norms (no push, edit only in the worktree, ...) "
        f"belong in `invariants`, not `conditions`."
    ]


def is_securing_problem(problem: str) -> bool:
    return REFUSAL_TAG in problem


def probe_revisions(stages) -> list[dict]:
    """Every `probe-revised` mark in `stages`, as firing-row observables."""
    out: list[dict] = []
    for stage in stages:
        for cond in parse_marks(stage.conditions):
            if cond.mark_kind == "probe-revised" and cond.fields.get("was"):
                out.append({
                    "stage": stage.index, "stage_title": stage.title, "condition": cond.text,
                    "probe": cond.fields["probe"], "observed": cond.fields["observed"],
                    "was": cond.fields["was"],
                })
    return out
