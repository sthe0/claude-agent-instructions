#!/usr/bin/env python3
"""UserPromptSubmit hook: nudge when live context size crosses a band.

Accumulated conversation context is the dominant Claude Code cost driver
(see memory-global/leaves/token-economy-plan.md, 2026-06-04 update: ~71% of
cache_read cost comes from session turns beyond #200; per-turn cache_read
scales with absolute context size). The behavioral rules in CLAUDE.md
§ Cost discipline (/clear between unrelated tasks, offload verbose work to
sub-agents) had no enforcement signal — this hook is that signal.

Live context size is read from the transcript: the latest assistant turn's
usage = input_tokens + cache_read_input_tokens + cache_creation_input_tokens
(≈ what /context reports). If usage is unavailable (very fresh session),
the hook stays silent — nudging a short session is pointless.

Throttled per band via a state file so it fires at most once per band per
session: a nudge that re-emits on every prompt would itself bloat the
context it warns about. Bands default to 120k and 250k tokens; override via
CC_CONTEXT_NUDGE_BANDS (comma-separated ints).

Exit 0 always; emits one stdout line (becomes additional context the agent
acts on — suggest /clear on a task switch, or delegate exploration).
"""
from __future__ import annotations

import json
import os
import sys

DEFAULT_BANDS = [120_000, 250_000]


def parse_bands() -> list[int]:
    raw = os.environ.get("CC_CONTEXT_NUDGE_BANDS", "")
    if not raw.strip():
        return DEFAULT_BANDS
    try:
        bands = sorted({int(x) for x in raw.split(",") if x.strip()})
        return bands or DEFAULT_BANDS
    except ValueError:
        return DEFAULT_BANDS


def live_context_tokens(transcript_path: str) -> int | None:
    """Latest assistant-turn usage = current context size, or None."""
    if not transcript_path or not os.path.isfile(transcript_path):
        return None
    last = None
    try:
        with open(transcript_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                usage = (obj.get("message") or {}).get("usage")
                if isinstance(usage, dict):
                    last = usage
    except OSError:
        return None
    if not last:
        return None
    return (
        int(last.get("input_tokens", 0) or 0)
        + int(last.get("cache_read_input_tokens", 0) or 0)
        + int(last.get("cache_creation_input_tokens", 0) or 0)
    )


def highest_band(tokens: int, bands: list[int]) -> int:
    """Index+1 of the highest band crossed; 0 if none."""
    crossed = 0
    for i, b in enumerate(bands):
        if tokens >= b:
            crossed = i + 1
    return crossed


def state_path(session_id: str) -> str:
    safe = "".join(c for c in (session_id or "nosession") if c.isalnum() or c in "-_")
    return f"/tmp/cc-context-nudge-{safe or 'nosession'}"


def already_fired(path: str) -> int:
    try:
        with open(path, encoding="utf-8") as fh:
            return int(fh.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def record_fired(path: str, band: int) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(band))
    except OSError:
        pass


def message(tokens: int, band_idx: int, n_bands: int) -> str:
    k = round(tokens / 1000)
    base = (
        f"[context-growth] Context ~{k}k tokens. "
        "If this prompt starts a different task, suggest `/clear` to the user "
        "before continuing. If it continues the current task, delegate verbose "
        "exploration (multi-file reads, log diving, broad search) to a sub-agent "
        "so only the conclusion returns, and avoid re-reading large files "
        "(CLAUDE.md § Cost discipline)."
    )
    if band_idx >= n_bands:
        base += (
            " Context is now very large — per-turn cache_read cost is high; "
            "strongly prefer `/clear` or a summarize-and-restart over carrying on."
        )
    return base


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    transcript_path = payload.get("transcript_path") or ""
    session_id = payload.get("session_id") or ""

    bands = parse_bands()
    tokens = live_context_tokens(transcript_path)
    if tokens is None:
        return 0

    band = highest_band(tokens, bands)
    if band == 0:
        return 0

    sp = state_path(session_id)
    if band <= already_fired(sp):
        return 0  # already nudged at this band (or higher) this session

    record_fired(sp, band)
    print(message(tokens, band, len(bands)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
