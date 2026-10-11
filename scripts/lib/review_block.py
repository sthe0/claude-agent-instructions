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
LIST_ITEM_RE = re.compile(r"^\s*(?:[-*]\s|\d+[.)]\s)")
# `q:`, `Q :`, `Q2:` — a reviewer's near-miss of the `Q:` prefix.
_NUMBERED_QUESTION_RE = re.compile(r"^q\d*\s*:", re.IGNORECASE)


class QuestionFieldError(ValueError):
    """The customer-questions field has a line the parser cannot place — raised by
    ``ReviewBlock.questions`` so a reply is refused instead of losing the question."""


def starts_list_item(raw: str) -> bool:
    """A bullet or numbered item at column 0 — a new item, never the continuation of the
    line above it."""
    return LIST_ITEM_RE.match(raw) is not None and raw == raw.lstrip()

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


def _body_start(verdict_at: int, digest_at: int | None) -> int:
    """Index of the first entry after both the verdict and the digest line."""
    return max(verdict_at, -1 if digest_at is None else digest_at) + 1


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
        start = _body_start(self.verdict_at, self.digest_at)
        end = len(self.entries) if self.questions_at is None else self.questions_at
        return list(self.entries[start:end])

    @property
    def questions(self) -> list[str]:
        """The customer questions after the field header, in order; a block without
        the field, or with `Customer questions: none`, has none.

        Once the field starts, every line up to the closing marker is the header, a
        `Q:` line, or the continuation of a `Q:` line (a line that is neither a new
        question nor a column-0 list item). Anything else raises
        ``QuestionFieldError`` — a question written on the header line, an item
        without `Q:`, a concern after the field — rather than being dropped, so a
        reply that asked the customer something never records an empty list."""
        for (kind, _), raw in self.entries[:_body_start(self.verdict_at, self.digest_at)]:
            if kind in ("questions", "question"):
                raise QuestionFieldError(
                    f"the customer-questions field ({raw.strip()[:80]!r}) comes before the "
                    f"`{plan.VERDICT_MARKER}` / `{plan.PLAN_DIGEST_MARKER}` lines: write it "
                    "after the concerns")
        if self.questions_at is None:
            return []
        found: list[str] = []
        header_seen = declared_none = False
        for (kind, value), raw in self.entries[self.questions_at:]:
            text = raw.strip()
            if kind == "review":
                break
            if text.startswith("```"):
                continue
            if kind == "questions":
                if header_seen:
                    raise QuestionFieldError(
                        f"a second `{plan.CUSTOMER_QUESTIONS_MARKER}` line: "
                        "the reply carries one customer-questions field")
                header_seen = True
                if plan.declares_no_questions(value):
                    declared_none = True
                elif value.strip():
                    raise QuestionFieldError(
                        f"a question on the `{plan.CUSTOMER_QUESTIONS_MARKER}` line "
                        f"({value.strip()[:80]!r}): write the header bare and each "
                        f"question on its own `{plan.CUSTOMER_QUESTION_MARKER} <question>` line")
            elif kind == "question":
                if declared_none:
                    raise QuestionFieldError(
                        f"`{plan.CUSTOMER_QUESTIONS_MARKER} {plan.CUSTOMER_QUESTIONS_NONE}` "
                        "followed by questions: write the header bare, then the "
                        f"`{plan.CUSTOMER_QUESTION_MARKER}` lines, or `none` alone")
                found.append(value)
            elif kind == "other" and not found and plan.declares_no_questions(text):
                declared_none = True
            elif kind == "other" and found and _NUMBERED_QUESTION_RE.match(text):
                raise QuestionFieldError(
                    f"a second question written as {text[:80]!r}: start each question "
                    f"with exactly `{plan.CUSTOMER_QUESTION_MARKER}` so two questions are "
                    "never recorded as one")
            elif kind == "other" and found and not starts_list_item(raw):
                found[-1] = f"{found[-1]} {clean_value(text)}"
            elif kind == "condition":
                raise QuestionFieldError(
                    f"a condition-prefixed concern after the customer-questions field "
                    f"({text[:80]!r}): concerns come before `{plan.CUSTOMER_QUESTIONS_MARKER}`")
            else:
                raise QuestionFieldError(
                    f"a line in the customer-questions field that is not a "
                    f"`{plan.CUSTOMER_QUESTION_MARKER}` question ({text[:80]!r}): "
                    f"write each question as `{plan.CUSTOMER_QUESTION_MARKER} <question>`")
        questions = [q.strip() for q in found]
        if any(not q for q in questions):
            raise QuestionFieldError(
                f"an empty `{plan.CUSTOMER_QUESTION_MARKER}` line in the customer-questions field")
        return questions

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
    header = plan.CUSTOMER_QUESTIONS_HEADER_RE.match(cleaned)
    if header is not None:
        return "questions", strip(header.group("rest"))
    for kind, marker in (
        ("review", plan.REVIEW_MARKER),
        ("verdict", plan.VERDICT_MARKER),
        ("digest", plan.PLAN_DIGEST_MARKER),
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
    after = _body_start(verdict_at, digest_at)
    questions_at = next(
        (i for i, ((k, _), _) in enumerate(entries)
         if i >= after and k in ("questions", "question")),
        None,
    )
    return ReviewBlock(entries, verdict_at, digest_at, questions_at)
