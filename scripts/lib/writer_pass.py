"""Compute whether a publication body is BOUND to a preceding tech-writer pass.

Difficulty removed: the four recorded occurrences of unpolished text reaching
a ticket (TICKET-467, TICKET-495 x2, and TICKET-467's own
remediation trace) were each closed by hand, after the fact, because nothing
in the harness could answer "did tech-writer run before these bytes existed"
at the moment of publication. `lib.transcript_turns` exposes turn-boundary and
delivered-text primitives but no tool-invocation predicate, so this module is
a new primitive, not a borrowed one.

WHY A COMPUTED BINDING, NOT AN ATTESTATION. A field the coordinator writes
into a payload ("writer_pass: true") is a claim the same actor being gated
can mint — an honour system. A tech-writer invocation, by contrast, is an
entry the HARNESS itself writes into the session transcript as a `tool_use`
block; nothing the coordinator puts in a tool call's arguments can forge that
entry. So the binding this module computes consults exactly two inputs — the
resolved body bytes and the harness transcript — and nothing the caller
asserts about itself.

WITNESS. A transcript entry recording a tech-writer invocation, in the three
shapes the harness is known to write (see `_witness_shape`): a `Skill`
tool_use with `input.skill == "tech-writer"` (the evidenced primary shape —
13 such entries were found in a sample of this machine's own transcripts); an
`Agent`/`Task` tool_use with `input.subagent_type == "tech-writer"`; or a
`Bash` tool_use whose command spawns `spawn-specialist.py --kind tech-writer`
or names the tech-writer skill path directly (the shape a publishing process
uses when `Skill` is unavailable to it — see the scope note below).

BINDING. The normalized body (see `normalize`) is looked for in a
harness-recorded event at or after a witness, in two strengths:

- WRITER_OUTPUT: the body equals a witness's OWN `tool_result` content
  (its `tool_use_id` matches the witness's own id), or — only when the
  normalized body is at least `_MIN_CONTAINMENT_CHARS` long — is CONTAINED in
  it. This is a true authorship binding: the bytes are what the spawned
  writer itself returned. The containment floor exists because a short body
  is cheaply coincidental inside a longer tool_result; below the floor only
  exact equality counts.
- POST_WITNESS: the body EQUALS the full `content` of a `Write` tool_use, or
  the full `new_string` of an `Edit` tool_use, recorded after a witness.
  EQUALITY ONLY — containment is deliberately not applied here, because a
  `Write`/`Edit` event routinely carries bytes the published body is merely a
  FRAGMENT of (a sibling section, a larger document), and a containment match
  there would bind on coincidence rather than on the event having composed
  exactly these bytes. This is the shape the recorded compliant flow
  produces: tech-writer runs inline via `Skill`, and the coordinator itself
  then writes the polished body to a file with `Write` before publishing it.
- NONE otherwise.

THE PREDICATE IS EXISTENTIAL OVER WITNESSES, NEVER "LATEST WITNESS ONLY". A
body binds iff there EXISTS a witness at or before the body's composition
event — not iff the single most-recent witness precedes it. A
latest-witness implementation denies a body a genuinely earlier witness
covered, whenever a LATER witness also exists in the same transcript (a
second tech-writer pass for something else, later in the same session) —
false-denying exactly the multi-pass session this design exists to serve.
`transcript-two-witness.jsonl` in the test fixtures exists to catch this
specific inversion; a suite whose only witness assertion is a witness COUNT
of one cannot see the defect.

NO WITNESS CONSUMPTION. One witness backs every body composed after it, not
just the first. The recorded compliant trace (TICKET-467's own
remediation) shows exactly this: one `Skill`/tech-writer call, followed by
three `Write` calls producing three separate comment bodies — a
one-witness-one-body rule would deny two of the three.

SCOPE BOUNDARY: ONE TRANSCRIPT. The binding is computed over the ONE
transcript the PreToolUse payload names as the publishing process's own.
Subagent transcripts are separate files, and a witness held by a PARENT
process is invisible to a spawned CHILD that publishes directly. An empirical
scan of this machine's own transcripts found this costs nothing today (every
real publication observed traveled as a Bash command in a ROOT transcript,
consistent with `skills/tracker-management/SKILL.md` making publication a
root-coordinator responsibility) — but the scan is deliberately NOT widened
to follow a parent-transcript pointer the child would have to supply, because
that pointer is exactly the coordinator-authored input this design exists to
exclude. A spawned specialist that publishes directly needs its own inline
(or self-spawned) tech-writer pass; there is no other way to satisfy this
gate from inside a child transcript.

COST BOUND. The largest transcript observed on this machine is 27,241,348
bytes (~27 MB), so an unbounded scan is not an option inside a hook's
timeout. `bind` and `witnesses` read only the TAIL of the transcript
(`_SCAN_TAIL_BYTES`), discard a partial first line from that read, and
prefilter each raw line by substring before `json.loads`. `_SCAN_TAIL_BYTES`
derivation: measured wall-clock over the six committed fixtures (n=1 run of
200 iterations, ~2.1 MB scanned total per pass over
transcript-containment-trap.jsonl, transcript-discrimination.jsonl,
transcript-two-witness.jsonl, transcript-unwitnessed.jsonl,
transcript-witnessed.jsonl and transcript-writer-output-equality.jsonl) gave
a substring-prefiltered scan throughput of approximately 17.5 MB/s
(17,521,275 bytes per second; CPU-bound line iteration and JSON parsing only
— no model call, no judge, hence this stage's own cost tier is "medium"
rather than "large": the large-tier rule is for the runtime-debug tail of
model-latency sampling, and that ground is absent here). This session's
sandbox could not read or execute against the real ~27 MB transcript
directly (both `find` outside the allowed working directories and any
`python3 <script>.py` invocation were blocked here), so the measurement is
over the committed fixtures only — a residual to close by re-running this
measurement against a real large transcript on an unrestricted machine. Even
so, budgeting one quarter of a 20s hook timeout (5s) at a conservatively
lowered estimate of 10 MB/s (below the measured 17.5 MB/s, to leave headroom
for the untested large-file case and a slower disk/CPU) gives ~50 MB of
achievable scan depth; `_SCAN_TAIL_BYTES` is set well under that (8 MB). A
witness beyond that window yields `NO_WITNESS_IN_WINDOW`, not `NONE` — a
declared, documented limit, not a silent one (see "TWO NAMED FAILURE MODES"
below for exactly when this outcome, rather than plain `NONE`, applies).
Re-measure — ideally against a real large transcript — if the hook timeout
budget changes.

TWO NAMED FAILURE MODES, NOT ONE — PLUS PLAIN `NONE` STAYS PLAIN `NONE`.
`UNREADABLE` (the transcript path could not be opened at all — missing file,
permission error) is a MISSING OBSERVABLE and the caller must fail OPEN on
it, mirroring every other hook in this repo that reads a transcript.
`NO_WITNESS_IN_WINDOW` is reserved for the case the tail bound actually
narrowed what was scanned: the read was TRUNCATED (the transcript exceeds
`_SCAN_TAIL_BYTES`) and the truncated tail carries no witness — a witness
may have existed earlier in the file, outside the window, so this outcome is
an honestly-uncertain deny, not a claim the transcript is witnessless. A
transcript short enough to be read WHOLE that simply carries no witness at
all is plain `NONE` — the same outcome an equality/containment miss below
produces, and what the caller denies on either way. Both `NO_WITNESS_IN_WINDOW`
and plain `NONE` deny identically; the split exists only so a deny message
can say which is true, and so a fixture transcript small enough to be read
in full (as every committed fixture is) reports `NONE` rather than a
window-uncertainty outcome it never actually earned.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from . import transcript_turns

# See "COST BOUND" above for the derivation.
_SCAN_TAIL_BYTES = 8 * 1024 * 1024

# Below this normalized length, only exact equality binds WRITER_OUTPUT —
# containment is refused. Working estimate (per the plan's design record):
# a short acknowledgement-class reader-facing message ("Approved, thanks.",
# 17 chars once normalized) sits well under this floor, and a match of such a
# short string as a mere SUBSTRING of a longer tool_result is cheap
# coincidence, not evidence the tool_result composed it. 32 chars is roughly
# two short sentences' worth of margin above that example. Not measured
# against a larger corpus (none was available); revisit if a real
# false-positive/false-negative is observed.
_MIN_CONTAINMENT_CHARS = 32

WRITER_OUTPUT = "WRITER_OUTPUT"
POST_WITNESS = "POST_WITNESS"
NONE_STRENGTH = "NONE"
NO_WITNESS_IN_WINDOW = "NO_WITNESS_IN_WINDOW"
UNREADABLE = "UNREADABLE"

_BASH_KIND_FLAG_RE = re.compile(r"--kind[=\s]+tech-writer\b")
_BASH_SKILL_PATH_RE = re.compile(r"skills/(?:specializations/)?tech-writer(?:/SKILL\.md)?\b")


@dataclass(frozen=True)
class Witness:
    shape: str  # "skill" | "subagent" | "bash"
    timestamp: float | None
    line_index: int
    tool_use_id: str | None = None


@dataclass(frozen=True)
class Binding:
    strength: str
    witness: Witness | None = None
    evidence: str | None = None
    scanned_bytes: int = 0


def normalize(text: str) -> str:
    """NFC-normalize `text`, strip per-line trailing whitespace, collapse runs
    of blank lines to one, and strip leading/trailing blank lines.

    This is deliberately NOT the aggressive `agentctl.text_shape.
    normalize_for_match` (which casefolds and collapses ALL whitespace,
    destroying line structure) — the binding compares a BODY against a
    Write/tool_result's CONTENT, where line structure is part of what makes
    two texts the same document, not incidental formatting drift.
    """
    text = unicodedata.normalize("NFC", text or "")
    lines = [line.rstrip() for line in text.split("\n")]
    collapsed: list[str] = []
    prev_blank = False
    for line in lines:
        blank = line == ""
        if blank and prev_blank:
            continue
        collapsed.append(line)
        prev_blank = blank
    while collapsed and collapsed[0] == "":
        collapsed.pop(0)
    while collapsed and collapsed[-1] == "":
        collapsed.pop()
    return "\n".join(collapsed)


def _read_tail(path: Path, tail_bytes: int) -> tuple[str, int, bool]:
    """Read at most `tail_bytes` from the end of `path`, discarding a partial
    first line when the read did not start at byte 0. Raises OSError if the
    file cannot be opened -- callers translate that to UNREADABLE. The third
    return value is True iff the read was truncated (the file exceeded the
    bound) -- callers use it to tell an honestly-empty transcript (plain
    NONE) apart from a window that may have cut a witness off (see
    NO_WITNESS_IN_WINDOW in the module docstring)."""
    size = path.stat().st_size
    truncated = size > tail_bytes
    with path.open("rb") as fh:
        if truncated:
            fh.seek(size - tail_bytes)
            raw = fh.read()
            newline = raw.find(b"\n")
            raw = raw[newline + 1:] if newline != -1 else b""
        else:
            raw = fh.read()
    return raw.decode("utf-8", errors="replace"), len(raw), truncated


def _safe_json(line: str) -> dict | None:
    try:
        entry = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    return entry if isinstance(entry, dict) else None


def _content_blocks(message) -> list:
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    return content if isinstance(content, list) else []


def _witness_shape(item: dict) -> str | None:
    name = item.get("name")
    tool_input = item.get("input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    if name == "Skill" and tool_input.get("skill") == "tech-writer":
        return "skill"
    if name in ("Agent", "Task") and tool_input.get("subagent_type") == "tech-writer":
        return "subagent"
    if name == "Bash":
        command = tool_input.get("command")
        if isinstance(command, str):
            if "spawn-specialist.py" in command and _BASH_KIND_FLAG_RE.search(command):
                return "bash"
            if _BASH_SKILL_PATH_RE.search(command):
                return "bash"
    return None


@dataclass(frozen=True)
class _Scan:
    witnesses: list
    writes: list  # (line_index, ts, text)
    edits: list  # (line_index, ts, text)
    results_by_id: dict  # tool_use_id -> text
    scanned_bytes: int
    truncated: bool


def _scan(transcript_path: Path, tail_bytes: int) -> _Scan:
    text, scanned_bytes, truncated = _read_tail(Path(transcript_path), tail_bytes)
    witnesses_out: list[Witness] = []
    writes_out: list[tuple[int, float | None, str]] = []
    edits_out: list[tuple[int, float | None, str]] = []
    results_by_id: dict[str, str] = {}
    for idx, line in enumerate(text.splitlines()):
        if "tool_use" not in line and "tool_result" not in line:
            continue
        entry = _safe_json(line)
        if entry is None:
            continue
        ts_raw = entry.get("timestamp")
        ts = transcript_turns.iso_to_epoch(ts_raw) if isinstance(ts_raw, str) else None
        message = entry.get("message")
        for item in _content_blocks(message):
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "tool_use":
                shape = _witness_shape(item)
                if shape:
                    witnesses_out.append(
                        Witness(shape=shape, timestamp=ts, line_index=idx, tool_use_id=item.get("id"))
                    )
                if item.get("name") == "Write":
                    tool_input = item.get("input")
                    content = tool_input.get("content") if isinstance(tool_input, dict) else None
                    if isinstance(content, str):
                        writes_out.append((idx, ts, content))
                elif item.get("name") == "Edit":
                    tool_input = item.get("input")
                    new_string = tool_input.get("new_string") if isinstance(tool_input, dict) else None
                    if isinstance(new_string, str):
                        edits_out.append((idx, ts, new_string))
            elif itype == "tool_result":
                use_id = item.get("tool_use_id")
                if not isinstance(use_id, str) or not use_id:
                    continue
                inner = item.get("content")
                parts: list[str] = []
                if isinstance(inner, str):
                    parts.append(inner)
                elif isinstance(inner, list):
                    for sub in inner:
                        if isinstance(sub, dict) and sub.get("type") == "text":
                            parts.append(sub.get("text") or "")
                results_by_id[use_id] = "\n".join(parts)
    return _Scan(
        witnesses=witnesses_out,
        writes=writes_out,
        edits=edits_out,
        results_by_id=results_by_id,
        scanned_bytes=scanned_bytes,
        truncated=truncated,
    )


def witnesses(transcript_path, tail_bytes: int | None = None) -> list[Witness]:
    """Every tech-writer witness recorded inside the scanned tail of
    `transcript_path`, in file order. Raises OSError if the transcript cannot
    be opened -- callers that must fail open should catch it themselves."""
    return _scan(Path(transcript_path), tail_bytes or _SCAN_TAIL_BYTES).witnesses


def bind(body: str, transcript_path) -> Binding:
    """Compute the binding of `body` against the tech-writer witnesses and
    composed-text events recorded in `transcript_path`. Never raises: an
    unreadable transcript is reported as `Binding(strength=UNREADABLE)`."""
    path = Path(transcript_path)
    try:
        scan = _scan(path, _SCAN_TAIL_BYTES)
    except OSError:
        return Binding(strength=UNREADABLE)

    if not scan.witnesses:
        # A truncated (tail-only) read may have cut a witness off before the
        # scanned window -- that is an honestly-uncertain deny
        # (NO_WITNESS_IN_WINDOW). An UNtruncated read that found zero
        # witnesses is a genuinely witnessless transcript -- plain NONE, the
        # same outcome an equality/containment miss produces below.
        if scan.truncated:
            return Binding(strength=NO_WITNESS_IN_WINDOW, scanned_bytes=scan.scanned_bytes)
        return Binding(strength=NONE_STRENGTH, scanned_bytes=scan.scanned_bytes)

    normalized_body = normalize(body)
    contain_ok = len(normalized_body) >= _MIN_CONTAINMENT_CHARS

    # WRITER_OUTPUT: a witness's OWN tool_result. Self-referential, so the
    # witness trivially precedes-or-equals its own result -- no separate
    # existential scan needed here.
    for w in scan.witnesses:
        if not w.tool_use_id:
            continue
        result_text = scan.results_by_id.get(w.tool_use_id)
        if result_text is None:
            continue
        normalized_result = normalize(result_text)
        if normalized_result == normalized_body or (contain_ok and normalized_body in normalized_result):
            return Binding(
                strength=WRITER_OUTPUT,
                witness=w,
                evidence=f"tool_result of witness shape={w.shape} at line {w.line_index}",
                scanned_bytes=scan.scanned_bytes,
            )

    # POST_WITNESS: a Write/Edit whose full content/new_string equals the
    # body (equality only), preceded by ANY witness (existential, not
    # latest-witness -- see module docstring).
    for line_index, _ts, content in (*scan.writes, *scan.edits):
        if normalize(content) != normalized_body:
            continue
        preceding = [w for w in scan.witnesses if w.line_index <= line_index]
        if not preceding:
            continue
        nearest = max(preceding, key=lambda w: w.line_index)
        return Binding(
            strength=POST_WITNESS,
            witness=nearest,
            evidence=f"composed at line {line_index}, witness at line {nearest.line_index}",
            scanned_bytes=scan.scanned_bytes,
        )

    return Binding(strength=NONE_STRENGTH, scanned_bytes=scan.scanned_bytes)
