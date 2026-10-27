"""One transcript scan for "an AskUserQuestion carrying a marker was asked after an engine event".

The resolution ask and the content-waiver ask both refuse unless such an ask exists; each keeps its
own refusal texts, escape and extra preconditions and builds its blockers from this scan.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple

from . import transcript_turns


class Ask(NamedTuple):
    tool_input: dict
    ts: float | None
    answered: bool
    # The `toolUseResult.answers` of every result entry holding exactly one tool_result for
    # this ask; an entry holding several results contributes none (fail-closed consent).
    sole_answers: tuple


class AskScan(NamedTuple):
    marked: list[Ask]
    after: list[Ask]


def ask_after_event(transcript_path: Path | None, marker: str, anchor: float) -> AskScan | None:
    """Every AskUserQuestion with an option carrying `marker`, in file order (`marked`), and
    those timestamped after the engine event stamp `anchor` (`after`; a missing timestamp is
    never after). None iff the transcript cannot be located or read."""
    if transcript_path is None:
        return None
    try:
        lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None
    asks: dict[str, list] = {}
    for line in lines:
        try:
            entry = json.loads(line)
        except Exception:
            continue
        message = entry.get("message") if isinstance(entry, dict) else None
        if not isinstance(message, dict):
            continue
        blocks = [b for b in transcript_turns._content_items(message) if isinstance(b, dict)]
        results = [b for b in blocks if b.get("type") == "tool_result" and not b.get("is_error")]
        for block in blocks:
            call_id = block.get("id")
            if (block.get("type") == "tool_use" and block.get("name") == "AskUserQuestion"
                    and isinstance(call_id, str) and isinstance(block.get("input"), dict)
                    and transcript_turns.has_marker_option(block["input"], marker)):
                ts = entry.get("timestamp")
                epoch = transcript_turns.iso_to_epoch(ts) if isinstance(ts, str) else None
                seen = asks.setdefault(call_id, [block["input"], epoch, False, []])
                if seen[1] is None:
                    seen[1] = epoch
        for block in results:
            seen = asks.get(block.get("tool_use_id"))
            if seen is None:
                continue
            seen[2] = True
            if len(results) == 1:
                tool_result = entry.get("toolUseResult")
                seen[3].append(tool_result.get("answers") if isinstance(tool_result, dict) else None)
    marked = [Ask(inp, ts, answered, tuple(answers)) for inp, ts, answered, answers in asks.values()]
    return AskScan(marked, [a for a in marked if a.ts is not None and a.ts > anchor])
