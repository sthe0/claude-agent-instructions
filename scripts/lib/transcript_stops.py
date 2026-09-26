"""Parse a harness transcript JSONL into its Bash tool_uses, classified by stop kind.

Difficulty removed: classifying a denied Bash call as a `materialization_defect`
(the grant existed; the child's own --settings/--add-dir materialization failed)
versus a genuine `planning_miss` (no grant covers this call, so asking the user is
correct) requires reading the ACTUAL transcript of what the harness denied and why
— not trusting a re-ask's own prose, which cannot tell the two apart. This module is
the one parser: given a transcript JSONL path, it yields one `BashToolUse` per Bash
`tool_use`/`tool_result` pair, classified into exactly one of five stop kinds —
`ran`, `permission-denial`, `hook-block`, `user-rejected`, `failed` — for
`grants.grant_covers_call` to then classify against a stage's effective grant set.

Stop-kind classification order matters (see `_classify`): `toolDenialKind` alone
cannot distinguish a `hook-block` from a `permission-denial`. The committed
fixture transcripts under `scripts/tests/fixtures/transcript_stops/` carry the
SAME `toolDenialKind: "permission-rule"` on both `permission-denial.jsonl` and
`hook-block.jsonl` — a permission-rule refusal and a `PreToolUse` hook's own
refusal are indistinguishable by that field alone. The only distinguishing signal
is the stop text itself: a hook's refusal is always prefixed
`<HookEvent>:<Tool> hook error:` (verified against the committed `hook-block.jsonl`
fixture's exact text), while a permission-rule refusal reads
`Permission to use <Tool> with command ... has been denied.` `user-rejected` IS
distinguishable by `toolDenialKind` alone (`"user-rejected"`), checked first because
it is the one unambiguous signal.

A `permission-denial` classification requires a POSITIVE match — either the
`has been denied` marker in the stop text, or `toolDenialKind == "permission-rule"`
— never a bare `is_error` fallback: an ordinary failing command (a non-zero exit
with no denial marker at all) classifies as `failed`, a fifth stop kind distinct
from all three denial-shaped kinds above.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

STOP_KINDS = ("ran", "permission-denial", "hook-block", "user-rejected", "failed")

_HOOK_ERROR_MARKER = "hook error:"
_PERMISSION_DENIAL_MARKER = "has been denied"
_PERMISSION_DENIAL_KINDS = frozenset({"permission-rule"})


@dataclass(frozen=True)
class BashToolUse:
    tool_use_id: str
    command: str
    line_no: int  # 1-indexed line of the tool_use (assistant) line in the transcript
    stop_kind: str  # one of STOP_KINDS
    stop_text: str | None  # the tool_result's text; None when stop_kind == "ran"


# Finding S4: `_classify_transcript_denials` needs file-tool denials classified
# too (an add_dir materialization failure denies Edit/Read/Write/NotebookEdit,
# never Bash), not just Bash — `ToolUse` generalizes `BashToolUse` with a
# `tool_name` field and a `file_path` (populated for the four file tools,
# `None` for Bash, mirroring `command`'s Bash-only/`None`-otherwise split).
_TRACKED_TOOLS = frozenset({"Bash", "Edit", "Read", "Write", "NotebookEdit"})


@dataclass(frozen=True)
class ToolUse:
    tool_use_id: str
    tool_name: str
    command: str | None  # populated for Bash, else None
    file_path: str | None  # populated for Edit/Read/Write/NotebookEdit, else None
    line_no: int
    stop_kind: str
    stop_text: str | None


def _result_text(content: object) -> str:
    """A tool_result's `content` is either a bare string (every fixture observed) or
    a list of content blocks (the shape other tool_result kinds use elsewhere in a
    transcript); normalize to one string by joining `type: "text"` blocks, so a
    future block-shaped Bash tool_result still classifies correctly rather than
    silently falling through to `permission-denial` for want of the hook-error
    marker text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") for block in content if isinstance(block, dict)
        )
    return ""


def _classify(denial_kind: object, is_error: bool, text: str) -> str:
    if not is_error:
        return "ran"
    if denial_kind == "user-rejected":
        return "user-rejected"
    if _HOOK_ERROR_MARKER in text:
        return "hook-block"
    if denial_kind in _PERMISSION_DENIAL_KINDS or _PERMISSION_DENIAL_MARKER in text:
        return "permission-denial"
    return "failed"


def parse_bash_tool_uses(path: str | Path) -> list[BashToolUse]:
    """Every Bash tool_use/tool_result pair in a transcript JSONL, in file order.

    A tool_use with no matching tool_result (the transcript ends mid-call, or a
    result on a still-open call never arrived) is dropped — there is nothing to
    classify a stop kind from. Non-Bash tool_uses are dropped entirely: this
    module's whole domain is Bash calls, the only tool `grants.grant_covers_call`'s
    coverage question is ever asked about for a stage's runtime denial.

    A malformed line (not valid JSON) is skipped rather than raised on: a real
    transcript file this module reads is produced by the harness itself, never
    hand-authored, so a single corrupt line should not blind the parser to every
    call around it."""
    bash_uses: dict[str, tuple[int, str]] = {}  # tool_use_id -> (line_no, command)
    out: list[BashToolUse] = []
    with Path(path).open(encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            entry_type = entry.get("type")
            content = entry.get("message", {}).get("content") or []
            if entry_type == "assistant":
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    if block.get("name") != "Bash":
                        continue
                    tool_use_id = block.get("id")
                    if not tool_use_id:
                        continue
                    command = block.get("input", {}).get("command", "")
                    bash_uses[tool_use_id] = (line_no, command)
            elif entry_type == "user":
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    tool_use_id = block.get("tool_use_id")
                    if tool_use_id not in bash_uses:
                        continue
                    use_line_no, command = bash_uses.pop(tool_use_id)
                    is_error = bool(block.get("is_error"))
                    text = _result_text(block.get("content"))
                    stop_kind = _classify(entry.get("toolDenialKind"), is_error, text)
                    out.append(BashToolUse(
                        tool_use_id=tool_use_id,
                        command=command,
                        line_no=use_line_no,
                        stop_kind=stop_kind,
                        stop_text=text if stop_kind != "ran" else None,
                    ))
    return out


def parse_tool_uses(path: str | Path) -> list[ToolUse]:
    """Every tracked tool_use/tool_result pair in a transcript JSONL, in file
    order — the same shape as `parse_bash_tool_uses`, generalized from
    Bash-only to `_TRACKED_TOOLS` (Bash, Edit, Read, Write, NotebookEdit) so a
    file-tool add_dir materialization denial classifies the same way a Bash
    one does (finding S4). Kept as a separate function from
    `parse_bash_tool_uses` rather than widening that one in place: an existing
    regression test pins `parse_bash_tool_uses` DROPPING a non-Bash tool_use
    even when it has a matching tool_result, so widening its own tool-name
    filter in place would silently invert an intentionally-pinned behavior."""
    uses: dict[str, tuple[int, str, str | None, str | None]] = {}
    out: list[ToolUse] = []
    with Path(path).open(encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            entry_type = entry.get("type")
            content = entry.get("message", {}).get("content") or []
            if entry_type == "assistant":
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    tool_name = block.get("name")
                    if tool_name not in _TRACKED_TOOLS:
                        continue
                    tool_use_id = block.get("id")
                    if not tool_use_id:
                        continue
                    tool_input = block.get("input", {})
                    if tool_name == "Bash":
                        command = tool_input.get("command", "")
                        file_path = None
                    else:
                        command = None
                        file_path = tool_input.get("file_path", "")
                    uses[tool_use_id] = (line_no, tool_name, command, file_path)
            elif entry_type == "user":
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    tool_use_id = block.get("tool_use_id")
                    if tool_use_id not in uses:
                        continue
                    use_line_no, tool_name, command, file_path = uses.pop(tool_use_id)
                    is_error = bool(block.get("is_error"))
                    text = _result_text(block.get("content"))
                    stop_kind = _classify(entry.get("toolDenialKind"), is_error, text)
                    out.append(ToolUse(
                        tool_use_id=tool_use_id,
                        tool_name=tool_name,
                        command=command,
                        file_path=file_path,
                        line_no=use_line_no,
                        stop_kind=stop_kind,
                        stop_text=text if stop_kind != "ran" else None,
                    ))
    return out
