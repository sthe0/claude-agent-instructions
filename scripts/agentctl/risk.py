"""Risk bets: how a stage's declared risk decides its elaboration, timing and review depth.

A bound (risk-shaped) session gives every stage a `[stage.bet]`. `derive` turns the four
levels into the elaboration the stage owes, when it owes it and how deeply it is reviewed;
the author's own `elaboration` / `review` can only raise those, never lower them. The same
table is the floor for the severity rule: bound, a blocking concern blocks only when it is
tagged `axis:R` or `[bet:<n>]` (`concern_blocks`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from .plan import concern_axis

LEVELS = ("low", "medium", "high")
DETECTIONS = ("loud", "silent")
RECOVERIES = ("cheap", "costly", "irreversible")

REMOVE_UNCERTAINTY = "remove_uncertainty"
STAGING = "staging"
DISCRIMINATING_CONTROL = "discriminating_control"
ELABORATIONS = (REMOVE_UNCERTAINTY, STAGING, DISCRIMINATING_CONTROL)
DONE_CHECK = "done_check"

BEFORE_APPROVE = "before_approve"
AT_LAUNCH = "at_launch"

SKELETON, STAGE, NEIGHBOURS = "skeleton", "stage", "neighbours"
DEPTHS = (SKELETON, STAGE, NEIGHBOURS)

CALLED_FIELD = {
    REMOVE_UNCERTAINTY: "knowledge",
    STAGING: "procedure",
    DISCRIMINATING_CONTROL: "negative_control",
    DONE_CHECK: "verify_command",
}

BET_REQUIRED_KEYS = ("difficulty", "likelihood", "damage", "detection", "recovery")

_ELABORATION_RE = re.compile(r"\b(" + "|".join(ELABORATIONS) + r")\b")
_LEADING_WORD_RE = re.compile(r"^\s*([A-Za-z_]+)")
_BET_TAG_RE = re.compile(r"\[bet:(\d+)\]")


class BetError(ValueError):
    """A `[stage.bet]` that does not follow the grammar."""


@dataclass(frozen=True)
class Bet:
    difficulty: str
    likelihood: str
    damage: str
    detection: str
    recovery: str
    elaboration: tuple[str, ...] = ()
    review: str | None = None


@dataclass(frozen=True)
class Derived:
    elaboration: tuple[str, ...]
    timing: str
    depth: str

    @property
    def called_fields(self) -> tuple[str, ...]:
        names = self.elaboration or (DONE_CHECK,)
        return tuple(CALLED_FIELD[name] for name in names)


def _leading_word(value: object, allowed: tuple[str, ...], key: str) -> str:
    found = _LEADING_WORD_RE.match(str(value or ""))
    word = found.group(1).lower() if found else ""
    if word not in allowed:
        raise BetError(f"bet {key} must start with one of {'|'.join(allowed)}, got {str(value)[:40]!r}")
    return word


def parse_bet(raw: Mapping) -> Bet:
    """A `[stage.bet]` table as a `Bet`. The four levels are read from the leading word of
    their value, so `likelihood = "medium: new rules"` keeps its explanation; `elaboration`
    names the elaboration kinds it asks for and `review` the depth, both optional."""
    if not isinstance(raw, Mapping):
        raise BetError("bet must be a table")
    for key in BET_REQUIRED_KEYS:
        if not str(raw.get(key) or "").strip():
            raise BetError(f"bet is missing {key!r}")
    declared = raw.get("elaboration") or ()
    text = " ".join(map(str, declared)) if isinstance(declared, (list, tuple)) else str(declared)
    review = raw.get("review")
    return Bet(
        difficulty=str(raw["difficulty"]).strip(),
        likelihood=_leading_word(raw["likelihood"], LEVELS, "likelihood"),
        damage=_leading_word(raw["damage"], LEVELS, "damage"),
        detection=_leading_word(raw["detection"], DETECTIONS, "detection"),
        recovery=_leading_word(raw["recovery"], RECOVERIES, "recovery"),
        elaboration=tuple(n for n in ELABORATIONS if n in _ELABORATION_RE.findall(text)),
        review=_leading_word(review, DEPTHS, "review") if str(review or "").strip() else None,
    )


def _timing(elaboration: tuple[str, ...]) -> str:
    return BEFORE_APPROVE if {STAGING, DISCRIMINATING_CONTROL} & set(elaboration) else AT_LAUNCH


def derive(likelihood: str, damage: str, detection: str, recovery: str) -> Derived:
    """The elaboration, timing and depth the four levels call for, before any author raise."""
    likely = LEVELS.index(likelihood) >= LEVELS.index("medium")
    damaging = LEVELS.index(damage) >= LEVELS.index("medium")
    heavy = damage == "high" or recovery == "irreversible"
    called = {
        REMOVE_UNCERTAINTY: likely,
        STAGING: heavy,
        DISCRIMINATING_CONTROL: detection == "silent" and (likely or damaging),
    }
    elaboration = tuple(n for n in ELABORATIONS if called[n])
    if heavy:
        depth = NEIGHBOURS
    else:
        depth = STAGE if elaboration else SKELETON
    return Derived(elaboration, _timing(elaboration), depth)


def effective(bet: Bet) -> Derived:
    """The derived call with the author's `elaboration` and `review` applied: levels only
    raise, so a declared kind joins the derived ones and a declared depth never lowers."""
    base = derive(bet.likelihood, bet.damage, bet.detection, bet.recovery)
    elaboration = tuple(n for n in ELABORATIONS if n in base.elaboration or n in bet.elaboration)
    depth = max(base.depth, bet.review or SKELETON, key=DEPTHS.index)
    return Derived(elaboration, _timing(elaboration), depth)


def bet_raised(old: Bet, new: Bet) -> bool:
    """True when `new` rates the stage's risk above `old` on any axis: a higher likelihood,
    damage or recovery cost, a silent detection where it was loud, or an elaboration kind
    or review depth the effective call did not have before."""
    before, after = effective(old), effective(new)
    return (
        LEVELS.index(new.likelihood) > LEVELS.index(old.likelihood)
        or LEVELS.index(new.damage) > LEVELS.index(old.damage)
        or RECOVERIES.index(new.recovery) > RECOVERIES.index(old.recovery)
        or (old.detection, new.detection) == ("loud", "silent")
        or bool(set(after.elaboration) - set(before.elaboration))
        or DEPTHS.index(after.depth) > DEPTHS.index(before.depth)
    )


def field_present(stage, field: str) -> bool:
    if field == "knowledge":
        return bool((stage.knowledge or "").strip())
    if field == "procedure":
        return bool((stage.means.procedure or "").strip())
    if field == "negative_control":
        return bool(stage.criterion.negative_control or stage.criterion.negative_control_waiver)
    return bool((stage.criterion.verify_command or "").strip())


def missing_called_fields(stage, derived: Derived) -> list[str]:
    return [f for f in derived.called_fields if not field_present(stage, f)]


def bet_problems(doc) -> list[str]:
    """What a bound, non-skeleton plan owes: a well-formed bet on every stage, and for a
    stage whose elaboration is due before approval, exactly the fields that elaboration calls
    for. A stage that elaborates at launch owes no more than its bet."""
    problems = []
    for stage in sorted(doc.stages, key=lambda s: s.index):
        raw = doc.bets.get(stage.index)
        if raw is None:
            problems.append(f"stage {stage.index}: a bound plan needs a [stage.bet] on every stage")
            continue
        try:
            derived = effective(parse_bet(raw))
        except BetError as exc:
            problems.append(f"stage {stage.index}: {exc}")
            continue
        if derived.timing == BEFORE_APPROVE:
            problems += [
                f"stage {stage.index}: its bet calls for {field!r} before approval"
                for field in missing_called_fields(stage, derived)
            ]
    return problems


def bet_tag(body: str) -> int | None:
    """The stage index a concern's `[bet:<n>]` tag names, or None when it carries no tag."""
    found = _BET_TAG_RE.search(body or "")
    return int(found.group(1)) if found else None


def concern_blocks(body: str) -> bool:
    """Bound, a blocking concern blocks only when tagged as a chain defect (`axis:R`) or as
    a stage's underrated bet (`[bet:<n>]`); any other is a note."""
    return concern_axis(body) == "R" or bet_tag(body) is not None


TAG_INSTRUCTION = (
    "Tag every concern that must block: write `axis:R` for a chain defect (a hand-off that "
    "is not real, a stage that does not reach the goal), or `[bet:<n>]` (the stage's index) "
    "when stage <n>'s risk bet is understated for what it can hide. Put the tag before the "
    "part token. A blocking concern without either tag is recorded as a note."
)


def tag_instruction(bound: bool) -> str:
    """The reviewer's tagging instruction: empty for an unbound session, whose concerns keep
    the severity they were written with."""
    return TAG_INSTRUCTION if bound else ""
