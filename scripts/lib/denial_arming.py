"""Was this session ARMED with a permission-layer denial the self-grant gate
must react to -- or could its transcript not even be read?

Difficulty removed: mechanizing "never widen a permission surface in answer
to a denial you yourself hit" needs a first structural condition -- did THIS
session carry a denial that expressed an actual permission judgement? --
resolved down to the exact tool call that was denied, so a caller (stage 5)
can test whether the widening it is about to allow would have covered that
call. This module answers only that; it knows nothing about permission-
surface documents, entry matching, or hooks.

THREE OUTCOMES, NOT TWO. A judge that can only say "armed" or "not armed"
collapses "I looked and found nothing" into the same value as "I could not
look at all" -- and those two demand OPPOSITE fail behaviour from a caller
built on a deny-by-default gate: NOT_ARMED must let a self-grant proceed
un-gated, UNREADABLE must not. `Arming.verdict` is one of three `Verdict`
members, never a bool.

RESOLUTION IS STRUCTURAL, NEVER TEXTUAL. Each denial is resolved through the
row's own `sourceToolAssistantUUID` against a `tool_use` block in the
assistant message it names -- typed structure, never the free-text
`toolUseResult`/`reason` strings. Parsing free text to classify meaning for a
hard block is the named anti-pattern
(memory-global/leaves/regex-not-for-semantic-classification.md) and this
module never does it. An unresolvable uuid still yields an ARMED
`DeniedCall` whose `tool_name`/`tool_input` are `None`: the denial still
arms, only the call it denied is unknown, and a caller treats an unknown
call as covering (fail-toward-covering, stage 5).

A denial not yet flushed to disk (residual R5) is deliberately NOT folded
into UNREADABLE: it simply is not among the rows read, so the transcript is
read cleanly end to end and the result is NOT_ARMED. Basis: the self-
granting call cannot be issued before the agent has received the denial,
which is a full model round-trip downstream of the write -- by which time
the row is flushed.

ARMING IS PER-AGENT (residual R3): `armed()` reads exactly the transcript it
is given, so a denial in a parent session never arms a spawned agent reading
its own, separate transcript.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

# Decompiled from the installed client bundle (PROVENANCE § 5 of
# hook-permission-self-grant-provenance.md), not sampled: these three, and
# only these three, express an actual permission judgement about the denied
# call. The other four `toolDenialKind` values are named here, in comments,
# rather than left to omission -- a future reader must see that they were
# considered and excluded, not forgotten.
_ARMING_KINDS = frozenset({
    "permission-rule",   # a hard deny -- the harness allowlist or one of this repo's own hooks
    "user-rejected",     # the harness's interactive allow/deny prompt was answered "no"
    "automode-blocked",  # the auto-mode classifier judged the call unsafe and blocked it
    # EXCLUDED: "cancelled" and "interrupted" are user/system ABORTS carrying no
    # permission judgement at all -- arming on them would turn every Esc keypress
    # into a lock on the settings surface.
    # EXCLUDED: "automode-unavailable" and "automode-parsing-error" are classifier
    # INFRASTRUCTURE failures, not judgements about the call.
})

# A per-line prefilter checked before json.loads: the overwhelming majority of
# transcript rows are neither denials nor tool_use blocks, and this substring
# check is what buys the measured 0.29 s worst case on a 23 598-row transcript
# (PROVENANCE § 1) rather than parsing every row.
_DENIAL_MARKER = '"toolDenialKind"'
_TOOL_USE_MARKER = '"tool_use"'


def _looks_like_a_row(line: str) -> bool:
    """A cheap structural shape test, no `json.loads`: does this line even
    LOOK like one JSONL object?

    Readability must be judged over the WHOLE file, but the prefilter above
    parses only the ~0.1% of rows that could carry a denial or a call. Judging
    readability from the prefiltered lines alone means a wholly-corrupt
    transcript whose garbage happens to contain neither marker substring is
    never attempted at all, so nothing "fails to parse" and the verdict comes
    back NOT_ARMED -- "I looked and found nothing" for a file that could not be
    read. That is the exact collapse this module's third value exists to
    prevent, reintroduced by an optimization. This test restores the whole-file
    judgement at O(n) string cost, without giving up the 0.29 s prefilter."""
    stripped = line.strip()
    return stripped.startswith("{") and stripped.endswith("}")


class Verdict(Enum):
    """The three honest answers -- see the module docstring."""
    ARMED = "ARMED"
    NOT_ARMED = "NOT_ARMED"
    UNREADABLE = "UNREADABLE"


@dataclass(frozen=True)
class DeniedCall:
    """The call one arming denial denied. `tool_name`/`tool_input` are both
    `None` when `sourceToolAssistantUUID` did not resolve to exactly one
    `tool_use` block -- the denial still arms; only the call is unknown."""
    kind: str
    tool_name: str | None
    tool_input: dict[str, Any] | None


@dataclass(frozen=True)
class Arming:
    """`verdict` is `NOT_ARMED`/`UNREADABLE` with `denials` empty, or `ARMED`
    carrying every arming denial found, in transcript order -- never just the
    first (see the module docstring: relevance is decided against entries
    this module does not know, so a first-hit shortcut could miss the denial
    a widening actually answers)."""
    verdict: Verdict
    denials: tuple[DeniedCall, ...] = ()


NOT_ARMED = Arming(Verdict.NOT_ARMED)
UNREADABLE = Arming(Verdict.UNREADABLE)


def _tool_use_blocks(message: Any) -> list[dict]:
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]


def _resolve(calls_by_uuid: dict[str, tuple[Any, Any]], source_uuid: Any) -> tuple[Any, Any]:
    if not isinstance(source_uuid, str):
        return None, None
    return calls_by_uuid.get(source_uuid, (None, None))


def armed(transcript_path: Path | str) -> Arming:
    """Read `transcript_path` once and answer ARMED / NOT_ARMED / UNREADABLE.

    One pass, per the stage's method: a per-line substring prefilter decides
    which lines are even attempted with `json.loads`; a per-line JSON error
    on an attempted line is tolerated and counted, but a missing file, an
    empty file, a file NO line of which even has the shape of a JSONL row, or
    a file whose attempted rows NEVER parsed is UNREADABLE --
    the caller must be able to tell "no denial" from "could not look",
    because those two demand opposite fail behaviour. Never raises: any
    read/parse failure resolves to one of the three `Arming` values."""
    path = Path(transcript_path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return UNREADABLE

    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return UNREADABLE
    if not any(_looks_like_a_row(line) for line in lines):
        return UNREADABLE  # whole-file readability -- see `_looks_like_a_row`

    # uuid of an assistant row -> (tool_name, tool_input) of its SOLE tool_use
    # block. A row with zero or with more than one tool_use block is not
    # stored -- with more than one there is no signal here for which block a
    # later denial names, so a denial resolving to it stays unresolved rather
    # than guessing.
    calls_by_uuid: dict[str, tuple[Any, Any]] = {}
    pending: list[tuple[str, Any]] = []  # (toolDenialKind, sourceToolAssistantUUID)
    attempted = failed = 0

    for line in lines:
        if _DENIAL_MARKER not in line and _TOOL_USE_MARKER not in line:
            continue
        attempted += 1
        try:
            entry = json.loads(line)
        except Exception:
            failed += 1
            continue
        if not isinstance(entry, dict):
            failed += 1
            continue

        kind = entry.get("toolDenialKind")
        if isinstance(kind, str):
            pending.append((kind, entry.get("sourceToolAssistantUUID")))

        if entry.get("type") == "assistant":
            row_uuid = entry.get("uuid")
            blocks = _tool_use_blocks(entry.get("message"))
            if isinstance(row_uuid, str) and len(blocks) == 1:
                calls_by_uuid[row_uuid] = (blocks[0].get("name"), blocks[0].get("input"))

    if attempted and failed == attempted:
        return UNREADABLE

    denials = [
        DeniedCall(kind=kind, tool_name=(t := _resolve(calls_by_uuid, source_uuid))[0], tool_input=t[1])
        for kind, source_uuid in pending
        if kind in _ARMING_KINDS
    ]
    if not denials:
        return NOT_ARMED
    return Arming(Verdict.ARMED, tuple(denials))
