"""Recognise the REVIEW block a thinker's plan review ends with.

The block is a typed reply contract (thinker SKILL.md): a ``REVIEW:`` marker,
a ``Verdict:`` line, a ``Plan digest:`` line and condition-prefixed concerns.
Reading it is protocol parsing, not a judgement about free-text meaning, so
both the topological review driver and the marker extractor call
``find_terminal_review_block`` — one definition of "the message's REVIEW block"
instead of a deterministic driver parse next to a model guess that can disagree
with it.

Difficulty removed: the extractor's model call labelled verdict-bearing REVIEW
blocks PLAN-READY / REPLAN / COMPLETED / MALFORMED, so a pair the driver would
have parsed fine was refused for a mislabel.
"""
from __future__ import annotations

import functools
import re
from dataclasses import dataclass

from agentctl import plan
from lib import planner_plan_check

NUMBERING_RE = re.compile(r"^\d+[.)]\s*")

@functools.lru_cache(maxsize=None)
def _tagged_condition_re(markers: tuple[str, ...]) -> re.Pattern:
    severities = "|".join(plan.CONCERN_SEVERITIES)
    conditions = "|".join(re.escape(m) for m in markers)
    return re.compile(
        rf"^({severities}):[\s*`_]*"
        rf"({re.escape(plan.RESTATES_PREFIX)}[^\s*`]+[\s*`_]+)?"
        rf"({conditions})(.*)$",
        re.DOTALL,
    )

Classified = tuple[tuple[str, str], str]


@dataclass(frozen=True)
class ReviewBlock:
    """The lines after the block's ``REVIEW:`` line, each as ``((kind, value), raw)``."""

    entries: tuple[Classified, ...]
    verdict_at: int
    digest_at: int | None
    questions_at: int | None = None

    @property
    def verdict(self) -> str:
        return self.entries[self.verdict_at][0][1]

    @property
    def digest(self) -> str | None:
        return None if self.digest_at is None else self.entries[self.digest_at][0][1]

    @property
    def region(self) -> list[Classified]:
        """What follows both the verdict and the digest line, up to the
        customer-questions field: the concerns."""
        start = max(self.verdict_at, -1 if self.digest_at is None else self.digest_at) + 1
        end = len(self.entries) if self.questions_at is None else self.questions_at
        return list(self.entries[start:end])

    @property
    def questions(self) -> list[str]:
        """The customer questions after the field header, in order; a block without
        the field, or with `Customer questions: none`, has none. A line that is not a
        new `Q:` line continues the question before it, until the closing marker."""
        if self.questions_at is None:
            return []
        found: list[str] = []
        for (kind, value), raw in self.entries[self.questions_at:]:
            if kind == "review":
                break
            if kind == "question":
                found.append(value)
            elif kind == "other" and found and raw.strip() and not raw.strip().startswith("```"):
                found[-1] = f"{found[-1]} {clean_value(raw.strip())}"
        return [q for q in (q.strip() for q in found) if q]

    @property
    def followed_by_other_marker(self) -> bool:
        """A return-marker line other than REVIEW comes after the block — the message
        is labelled by that marker and the REVIEW block was only quoted."""
        return any(_carries_other_marker(raw) for _, raw in self.entries)


def lead_clean(raw: str) -> str:
    text = raw.lstrip(planner_plan_check._DECORATION_CHARS)
    text = NUMBERING_RE.sub("", text)
    return text.lstrip(planner_plan_check._DECORATION_CHARS)


def clean_value(text: str) -> str:
    trimmed = text.strip(planner_plan_check.CONCERN_VALUE_DECORATION_CHARS)
    return re.sub(r"\*+", "", trimmed.replace("`", ""))


def classify_line(raw: str) -> tuple[str, str]:
    """(kind, value) of one raw line; kind is review, verdict, digest, condition or other.
    A condition's value is its recorded concern text."""
    strip = planner_plan_check.strip_decoration
    cleaned = strip(NUMBERING_RE.sub("", strip(raw)))
    for kind, marker in (
        ("review", plan.REVIEW_MARKER),
        ("verdict", plan.VERDICT_MARKER),
        ("digest", plan.PLAN_DIGEST_MARKER),
        ("questions", plan.CUSTOMER_QUESTIONS_MARKER),
        ("question", plan.CUSTOMER_QUESTION_MARKER),
    ):
        if cleaned.startswith(marker):
            value = strip(cleaned[len(marker):])
            if kind == "review" and value not in ("pass", "revise", ""):
                return "other", ""
            return kind, value
    leading = lead_clean(raw)
    tagged = _tagged_condition_re(tuple(plan.CONDITION_MARKERS)).match(leading)
    if tagged is not None:
        restates = re.sub(r"[*`]", "", tagged.group(2) or "").strip()
        body = f"{tagged.group(3)} {clean_value(tagged.group(4))}".rstrip()
        return "condition", plan.format_concern(
            tagged.group(1), restates[len(plan.RESTATES_PREFIX):], body)
    for marker in plan.CONDITION_MARKERS:
        if cleaned.startswith(marker):
            return "condition", f"{marker} {clean_value(leading[len(marker):])}".rstrip()
    return "other", ""


def _carries_other_marker(raw: str) -> bool:
    found = planner_plan_check.MARKER_RE.match(planner_plan_check.strip_decoration(raw))
    return found is not None and found.group(1) != plan.REVIEW_MARKER.rstrip(":")


def find_terminal_review_block(text: str) -> ReviewBlock | None:
    """The message's terminal REVIEW block, or ``None``.

    Terminal: the last ``REVIEW:`` line that a ``Verdict:`` line follows. A block
    with no verdict line is not a block. Purely structural — whether a later marker
    line means the block was only quoted is ``ReviewBlock.followed_by_other_marker``,
    a question only the marker extractor asks."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    classified = [(classify_line(ln), ln) for ln in lines]
    anchor = None
    for i, ((kind, _), _) in enumerate(classified):
        if kind == "review" and any(k == "verdict" for (k, _), _ in classified[i + 1:]):
            anchor = i
    if anchor is None:
        return None
    entries = tuple(classified[anchor + 1:])
    verdict_at = next(i for i, ((k, _), _) in enumerate(entries) if k == "verdict")
    digest_at = next((i for i, ((k, _), _) in enumerate(entries) if k == "digest"), None)
    after = max(verdict_at, -1 if digest_at is None else digest_at) + 1
    questions_at = next(
        (i for kind in ("questions", "question")
         for i, ((k, _), _) in enumerate(entries) if i >= after and k == kind),
        None,
    )
    return ReviewBlock(entries, verdict_at, digest_at, questions_at)
