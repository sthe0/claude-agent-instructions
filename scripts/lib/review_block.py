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

import re
from dataclasses import dataclass

from agentctl import plan
from lib import planner_plan_check

NUMBERING_RE = re.compile(r"^\d+[.)]\s*")

Classified = tuple[tuple[str, str], str]


@dataclass(frozen=True)
class ReviewBlock:
    """The lines after the block's ``REVIEW:`` line, each as ``((kind, value), raw)``."""

    entries: tuple[Classified, ...]
    verdict_at: int
    digest_at: int | None

    @property
    def verdict(self) -> str:
        return self.entries[self.verdict_at][0][1]

    @property
    def digest(self) -> str | None:
        return None if self.digest_at is None else self.entries[self.digest_at][0][1]

    @property
    def lines(self) -> list[str]:
        return [raw for _, raw in self.entries]

    @property
    def region(self) -> list[Classified]:
        """What follows both the verdict and the digest line: the concerns."""
        start = max(self.verdict_at, -1 if self.digest_at is None else self.digest_at) + 1
        return list(self.entries[start:])


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
    ):
        if cleaned.startswith(marker):
            value = strip(cleaned[len(marker):])
            if kind == "review" and value not in ("pass", "revise", ""):
                return "other", ""
            return kind, value
    for marker in plan.CONDITION_MARKERS:
        if cleaned.startswith(marker):
            leading = lead_clean(raw)
            return "condition", f"{marker} {clean_value(leading[len(marker):])}".rstrip()
    return "other", ""


def _carries_other_marker(raw: str) -> bool:
    found = planner_plan_check.MARKER_RE.match(planner_plan_check.strip_decoration(raw))
    return found is not None and found.group(1) != plan.REVIEW_MARKER.rstrip(":")


def find_terminal_review_block(text: str) -> ReviewBlock | None:
    """The message's terminal REVIEW block, or ``None``.

    Terminal: the last ``REVIEW:`` line that a ``Verdict:`` line follows, with no
    other return-marker line after it (a COMPLETED/REPLAN/... line later means the
    message is labelled by that marker and the REVIEW block was only quoted). A
    block with no verdict line is not a block."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    classified = [(classify_line(ln), ln) for ln in lines]
    anchor = None
    for i, ((kind, _), _) in enumerate(classified):
        if kind == "review" and any(k == "verdict" for (k, _), _ in classified[i + 1:]):
            anchor = i
    if anchor is None:
        return None
    entries = tuple(classified[anchor + 1:])
    if any(_carries_other_marker(raw) for _, raw in entries):
        return None
    verdict_at = next(i for i, ((k, _), _) in enumerate(entries) if k == "verdict")
    digest_at = next((i for i, ((k, _), _) in enumerate(entries) if k == "digest"), None)
    return ReviewBlock(entries, verdict_at, digest_at)
