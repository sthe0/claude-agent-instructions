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
import os
import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

# The largest a file may be and still be read as a candidate transcript, and the
# bound the read itself is given.
#
# MEASURED, not guessed, and deliberately three orders of magnitude above the
# permission-document cap in the gate: transcripts are legitimately huge. The
# largest in the local corpus is 60.1 MB / 23 598 rows (the same file the 0.29 s
# prefilter measurement above was taken on), so 512 MiB leaves an ~8.9x margin.
#
# WHY THERE IS A BOUND AT ALL. `armed()` used to hand the path straight to
# `Path.read_text`, which reads whatever it is given, whole. `transcript_path`
# arrives in an untrusted hook payload, so `transcript_path=/dev/zero` was an
# unbounded read: under a memory limit that is a `MemoryError` (caught below,
# hence UNREADABLE, hence a deny), and with no limit the hook process is
# OOM-killed outright and emits no verdict at all. The same shape was measured
# on the GATE's target-file read and fixed there; this is the same class one
# route over, and it is fixed the same way -- establish what the path is, then
# read a bounded amount.
_MAX_TRANSCRIPT_BYTES = 512 * 1024 * 1024

# DECOMPILED FROM THE INSTALLED CLIENT BUNDLE, NOT SAMPLED FROM TRANSCRIPTS:
# these three, and only these three, express an actual permission judgement
# about the denied call. The vocabulary comes from the two functions that emit
# `toolDenialKind`, and the mapping is restated here so the claim can be
# re-derived without any document outside this repository:
#
#     the permission mapper:  behavior === "ask"              -> "user-rejected"
#                             auto-mode classifier, reason A   -> "automode-unavailable"
#                             auto-mode classifier, reason B   -> "automode-parsing-error"
#                             auto-mode classifier, otherwise  -> "automode-blocked"
#                             otherwise                        -> "permission-rule"
#     the abort mapper:       abort/interrupt paths            -> "cancelled" | "interrupted"
#
# Seven values, four of them below. To re-verify against a newer client, find
# the two mappers by the literal string "toolDenialKind" in the installed
# bundle rather than by symbol name -- the minifier renames them between
# builds. A client that ADDS an arming kind silently narrows this set, which
# fails toward ALLOW; that is a named limit in the gate's module docstring.
#
# `permission-rule` being the OTHERWISE branch is also why the gate needs its
# conjunct (c): every deny-capable PreToolUse hook in this repo lands in that
# one bucket, and the field that would tell a harness-allowlist refusal from a
# hook-origin deny is discarded before the transcript row is written.
_ARMING_KINDS = frozenset({
    "permission-rule",   # a hard deny -- the harness allowlist or one of this repo's own hooks
    "user-rejected",     # the harness's interactive allow/deny prompt was answered "no"
    "automode-blocked",  # the auto-mode classifier judged the call unsafe and blocked it
})

# The four that do NOT arm, as DATA rather than as a comment. Both halves are
# named so the enumeration can be audited for completeness: an exclusion
# expressed only by omission cannot be tested, and cannot be distinguished from
# an oversight by anyone reading later.
_NON_ARMING_KINDS = frozenset({
    "cancelled",              # user/system ABORT -- no permission judgement about the call.
    "interrupted",            # ditto; arming on either would turn an Esc keypress into a
                              # lock on the settings surface.
    "automode-unavailable",   # classifier INFRASTRUCTURE failure, not a judgement.
    "automode-parsing-error",  # ditto.
})

# Every value the client bundle can emit -- the seven from the two mappers
# transcribed above, same decompiled source, not sampled. Kept as its own
# literal, so the completeness check in the tests is a real comparison against
# the decompiled vocabulary rather than a tautology restating the two sets
# above.
#
# The check lives in the tests, NOT in a module-level `assert`: this module is
# imported by a PreToolUse hook on every tool call, an import-time assert would
# take the hook down rather than report, and `assert` is stripped under -O
# anyway. The test is where a broken enumeration should be caught.
_ALL_DENIAL_KINDS = frozenset({
    "permission-rule", "user-rejected", "automode-blocked", "automode-unavailable",
    "automode-parsing-error", "cancelled", "interrupted",
})

# A per-line prefilter checked before json.loads: the overwhelming majority of
# transcript rows are neither denials nor tool_use blocks, and this substring
# check is what buys the worst case MEASURED at 0.29 s for one full walk of the
# largest transcript in the local corpus (60.1 MB, 23 598 rows), against the
# 5 s PreToolUse budget -- rather than parsing every row. To re-measure: time
# `armed()` over the largest file under `~/.claude-agent/projects/*/*.jsonl`.
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
    `tool_use` block, OR when the block it resolved to carried either field
    with the wrong JSON type (see `_call_fields`) -- the denial still arms;
    only the call is unknown. These annotations are ESTABLISHED by
    `_call_fields`, not merely asserted here: consumers may rely on them."""
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


def _call_fields(block: dict) -> tuple[str | None, dict[str, Any] | None]:
    """`(tool_name, tool_input)` of one `tool_use` block, WITH THE TYPES
    `DeniedCall` DECLARES -- or the modelled "call unknown" state if either
    field is not the type it claims to be.

    A transcript is a file on disk written by another process, so its content
    is untrusted input exactly as the hook payload is. `DeniedCall`'s
    annotations (`str | None`, `dict | None`) used to be a claim nobody
    established: both fields were stored straight off `json.loads`, so any JSON
    type could reach them, and each consumer met a shape its own code did not
    model. BOTH resulting defects were measured, and they fail in OPPOSITE
    directions -- which is why the coercion belongs here, at the parse
    boundary, rather than as a check at either use site:

      * a non-dict `input` (`["x"]`, `"str"`, `7`, `True`) reached
        `permission_entry_match.covers()`, whose `None`/`{}` rescue does not
        catch a TRUTHY non-dict, and raised `AttributeError` out of the gate's
        `decide()` into its catch-all -- a DENY over a transcript the gate merely
        failed to model. The radius is narrower than an earlier draft of this
        docstring claimed ("every call in the session"): measured, a benign
        `Edit` and a benign `Bash` in the same session still ALLOW, because
        `covers()` is only reached by a call the gate has already judged to widen
        a permission surface. Every SELF-GRANT-SHAPED call in such a session
        denies, benign ones included -- bad, and worth this boundary, but not the
        session-wide lock;
      * a non-str `name` (`7`, `["Read"]`, `{"a": 1}`, `True`) did not raise at
        all. It compares unequal to every entry's tool, so a denial that the
        added entry really would have covered read as "covered by nothing" and
        the widening was ALLOWED. That one is a silent hole in the gate itself,
        and no crash anywhere would have revealed it.

    Collapsing to `(None, None)` puts both on the state this module already
    models and documents -- the denial arms, the call it denied is unknown --
    which the caller already fails toward covering. No consumer needs a new
    branch, and a future field added to `DeniedCall` gets the same treatment by
    being parsed here rather than by every reader remembering."""
    name, tool_input = block.get("name"), block.get("input")
    if not isinstance(name, str) or not isinstance(tool_input, dict):
        return None, None
    return name, tool_input


def _read_transcript(transcript_path: Path | str) -> str | None:
    """The transcript's text, or `None` for every way it could not be read.

    ESTABLISH WHAT THE PATH IS, THEN READ IT -- the one parse boundary for this
    module's only external input, and the reason the caller never sees an
    unbounded read. Three findings all collapse to `None`, because this module's
    caller needs only "could not look" out of them, and each is fail-CLOSED (the
    gate's `_ON_ERROR`), never a quiet NOT_ARMED:

      * not a REGULAR file. A directory, a socket, a device or a FIFO is not a
        session transcript. This is also where the hang and the OOM live: a FIFO
        blocks in `open()` until someone writes, and a character device yields
        bytes without end. `stat` answers both without opening anything.
      * larger than `_MAX_TRANSCRIPT_BYTES`, or yielding more than that many
        characters despite what `stat` reported. The second bound is not
        redundant with the first: a procfs file is `S_ISREG` and reports
        `st_size` 0 while yielding content indefinitely, so the size gate alone
        would pass it straight into an unbounded read. It also closes the window
        between the `stat` and the `open`.
      * anything else that raises -- ENOENT, EACCES, a NUL byte in the path
        (`ValueError`, not an `OSError`), a decode failure the replace handler
        somehow does not absorb.

    A truncated read is deliberately NOT returned as text: dropping the tail of a
    transcript drops its NEWEST rows, which is exactly where the denial that arms
    the session is, so it would fail toward NOT_ARMED -- a silent ALLOW. Reading
    one byte past the cap is how that case is told from a file that merely fits.
    """
    try:
        st = os.stat(transcript_path)
        if not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_TRANSCRIPT_BYTES:
            return None
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(_MAX_TRANSCRIPT_BYTES + 1)
    except Exception:
        return None
    if len(text) > _MAX_TRANSCRIPT_BYTES:
        return None
    return text


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
    a file whose attempted rows NEVER parsed, or anything `_read_transcript`
    would not read at all (not a regular file, over its size cap) is
    UNREADABLE -- the caller must be able to tell "no denial" from "could not
    look", because those two demand opposite fail behaviour. Never raises, and
    never blocks or reads without bound: the path is a payload field like any
    other and goes through `_read_transcript` before a byte is read."""
    text = _read_transcript(transcript_path)
    if text is None:
        return UNREADABLE

    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        # A ZERO-ROW FILE IS DELIBERATELY FUSED WITH "COULD NOT LOOK", and it is the one
        # place where the two are mechanically indistinguishable -- the read SUCCEEDED, so
        # by the letter of "I looked and found nothing" this would be NOT_ARMED. It is not,
        # and the basis is not the read: a live session's transcript is never empty by the
        # time any tool call fires, because the rows that carry the user's message and the
        # agent's own turn are already on disk. So an empty file does not mean "this session
        # had no denial"; it means the path we were handed is not the transcript we think it
        # is -- a wrong path, or one truncated under us -- which is exactly the "could not
        # look" state, reached by a route that happens not to raise.
        #
        # This does NOT contradict the not-yet-flushed paragraph in the module docstring,
        # which is about a file WITH rows that is merely missing the newest one; there the
        # read genuinely did see the session. Here it saw nothing of it at all.
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
                calls_by_uuid[row_uuid] = _call_fields(blocks[0])

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
