#!/usr/bin/env python3
"""Stop hook: nudge when a run / graph URL appeared in this session's tool
output but was never surfaced to the user in an assistant text message.

Difficulty (recurring, recorded 2026-07-28): the agent launches an external
job on an orchestration / CI platform, records its run URL in a scratch /
recovery file or merely sees it in tool output, but never puts it in a
user-facing chat message as a clickable link — so the user has to ask
"where's the link?" every task. The prose rule already exists twice
(CLAUDE.md long-running-jobs; leaves/outcome-format.md point 3) yet kept
being missed, because it is framed as a *report-time* artifact rather than a
*launch-turn* obligation, and because writing the URL to a durable poller
file was conflated with reporting it. This hook is the structural (turn-end)
guard for that already-decided rule: it is deterministically decidable from
the transcript whether a run URL the agent SAW ever reached the user.

Vendor-neutral by design: it does not hard-code any platform host. A URL
qualifies as a "run URL" when its path carries a run / job / graph segment
(the generic surface shared by orchestration and CI systems), on any host.

Mechanism (mirrors the sibling *-reminder.py hooks):
  - Read this session's transcript.
  - Collect run-URL identities that appear in TOOL OUTPUT (tool_result
    blocks) — "seen".
  - Collect the identities that appear in ASSISTANT TEXT — "surfaced". Only a
    `type=="text"` block under a `role=="assistant"` message counts as
    surfacing to the user; a `thinking` block, a tool_use input, or a curl
    that posts the URL elsewhere does NOT (the chat message is the audience
    this rule protects).
  - Nudge for every seen-but-not-surfaced identity.

Fail-open advisory: exit 0 always; emit stdout (becomes additional system
context). Never blocks a turn. Detection is intentionally permissive — a
false positive (an extra reminder) is cheap; a silent miss is the expensive
failure this removes.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import transcript_read  # noqa: E402

# Any http(s) URL, minus trailing punctuation / markdown / quote delimiters.
_URL_RE = re.compile(r"https?://[^\s)\]}>\"'`]+", re.IGNORECASE)

# A URL is a "run URL" when its PATH contains one of these run/job/graph
# segments — the generic surface shared by orchestration and CI platforms.
# Vendor-neutral: no host is hard-coded, so an org-specific platform is
# matched without naming it here.
_RUN_SEGMENT_RE = re.compile(
    r"/(graph|flow|pipeline|execution|workflow|runs?|jobs?|tasks?|builds?)(/|$)",
    re.IGNORECASE,
)

# Trailing view-suffix segments stripped when normalizing to a run IDENTITY,
# so the same run compared across "/graph" (tool output) and a bare link
# (surfaced) collapses to one identity.
_VIEW_SUFFIX_RE = re.compile(r"/(graph|status|view|details|state)/?$", re.IGNORECASE)

MAX_LISTED = transcript_read.MAX_LISTED


def _identity(url: str) -> str | None:
    """Normalized run identity for a URL, or None if it is not a run URL."""
    m = re.match(r"(https?://[^?#]*)", url, re.IGNORECASE)
    core = (m.group(1) if m else url).rstrip("/")
    # Path part only (after the host) for the run-segment test.
    path_m = re.match(r"https?://[^/]+(/.*)?$", core, re.IGNORECASE)
    path = (path_m.group(1) or "") if path_m else ""
    if not _RUN_SEGMENT_RE.search(path):
        return None
    ident = _VIEW_SUFFIX_RE.sub("", core).rstrip("/")
    return ident.lower()


def _run_ids(text: str) -> dict:
    """Map {run_identity -> a representative URL} for run URLs found in text."""
    out: dict = {}
    if not text:
        return out
    for m in _URL_RE.finditer(text):
        url = m.group(0)
        ident = _identity(url)
        if ident and ident not in out:
            out[ident] = url
    return out


# Transcript parsing is shared with the sibling Stop hooks — see
# transcript_read.py for why it lives in an importable module.
_iter_transcript = transcript_read.iter_transcript
_assistant_text = transcript_read.assistant_text
_tool_result_text = transcript_read.tool_result_text


def analyze(entries) -> dict:
    """Return {identity -> sample_url} for run URLs seen in tool output but
    never present in any assistant text block."""
    seen: dict = {}
    surfaced: set = set()
    for entry in entries:
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant":
            surfaced |= set(_run_ids(_assistant_text(msg)).keys())
        elif role == "user":
            for ident, url in _run_ids(_tool_result_text(msg)).items():
                seen.setdefault(ident, url)
    return {ident: url for ident, url in seen.items() if ident not in surfaced}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    # Loop guard: never nudge twice in one stop cycle.
    if payload.get("stop_hook_active"):
        return 0
    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        return 0
    path = Path(transcript_path).expanduser()
    if not path.is_file():
        return 0
    try:
        unsurfaced = analyze(list(_iter_transcript(path)))
    except Exception:
        return 0
    if not unsurfaced:
        return 0
    urls = list(unsurfaced.values())[:MAX_LISTED]
    listed = ", ".join(urls)
    extra = "" if len(unsurfaced) <= MAX_LISTED else f" (+{len(unsurfaced) - MAX_LISTED} more)"
    print(
        "[run-url-surfaced] A launched run/graph URL appeared in tool output "
        "this session but was NEVER surfaced to the user in a chat message: "
        f"{listed}{extra}. Per CLAUDE.md long-running-jobs and "
        "leaves/outcome-format.md point 3, surface the run URL to the user as "
        "a CLICKABLE link (the run/graph itself, not an output folder) — a "
        "scratch/recovery file is not surfacing. Give it now, then continue."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
