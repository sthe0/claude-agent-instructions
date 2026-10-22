"""Review cycles of recorded agentctl sessions: cutting, act counting and booked minutes.

Difficulty removed: two reports (the review-cost calibration and the planning-cost
baseline) must count a review act, a review cycle and a cycle's active minutes the same
way, or their numbers cannot be compared. This module is the single home of that code.

A review cycle runs from a PLAN_READY arrival of a new plan sha256 (a `submit_plan` or a
`replan` event) to the next such arrival or to `approve`. An act is a fresh `plan_review` /
`plan_pair_review` event for which the engine's own cli._is_counted_review holds; carried
records, overrides and composition records are not acts. Minutes are the booked
`active_minutes` quantity, rebuilt from the user prompts of the session's transcript with
hook-engine-start.py's booking rule.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from agentctl import effort  # noqa: E402
from agentctl.cli import _is_counted_review  # noqa: E402

CAP_MINUTES = 30.0  # substantive-wall-clock-min, the cap hook-engine-start.py books a prompt gap at
REVIEW_EVENTS = ("plan_review", "plan_pair_review")
REVIEW_RECORD_EVENTS = REVIEW_EVENTS + ("plan_review_compose", "review_carried")


def parse_ts(value) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def is_act(event: dict) -> bool:
    return event.get("event") in REVIEW_EVENTS and _is_counted_review(
        str(event.get("verdict")), str(event.get("reviewer")))


def cut_cycles(history: list[dict]) -> tuple[list[dict], int]:
    """Review cycles of one session in order, and the acts that fall outside any cycle.

    A cycle opens on the first `submit_plan` (verified) or `replan` event whose
    plan_sha256 differs from the previous arrival's, and closes at the next such
    arrival or at `approve`. A same-sha event opens nothing, and so does a `replan` after
    the session's approval: that review loop is post-approval and not part of the population."""
    cycles: list[dict] = []
    current: dict | None = None
    previous_sha: str | None = None
    approved = False
    outside = 0

    def close(by: str, ts) -> None:
        nonlocal current
        if current is not None:
            current["closed_by"], current["t_close"], current["open"] = by, ts, False
            current = None

    for event in history:
        kind = event.get("event")
        sha = event.get("plan_sha256")
        if kind in ("submit_plan", "replan") and sha and sha != previous_sha and (
                kind == "replan" or event.get("verified")) and not (kind == "replan" and approved):
            approved = False
            ts = parse_ts(event.get("ts"))
            close("submit" if kind == "submit_plan" else "replan", ts)
            current = {"plan_sha": sha, "opened_by": "submit" if kind == "submit_plan" else "replan",
                       "closed_by": None, "open": True, "t_open": ts, "t_close": None, "acts": 0}
            cycles.append(current)
            previous_sha = sha
        elif kind == "approve":
            approved = True
            close("approve", parse_ts(event.get("ts")))
        elif is_act(event):
            if current is None:
                outside += 1
            else:
                current["acts"] += 1
    return cycles, outside


def book_prompts(prompts: list[float], child_runs, blocked: list[tuple[float, float]],
                 cap: float = CAP_MINUTES) -> list[tuple[float, float]]:
    """`(prompt ts, minutes booked at that prompt)` by hook-engine-start.py's rule: the
    first prompt books nothing; each later one books the gap since the previous prompt less
    the part a child run covers, capped, and nothing when the prompt lands while BLOCKED."""
    intervals = effort.child_run_intervals(child_runs, None)
    booked: list[tuple[float, float]] = []
    previous: float | None = None
    for ts in sorted(prompts):
        minutes = 0.0
        if previous is not None and ts > previous and not any(lo <= ts < hi for lo, hi in blocked):
            covered = effort.covered_seconds(intervals, previous, ts)
            minutes = min(max(0.0, (ts - previous) - covered) / 60.0, cap)
        booked.append((ts, minutes))
        previous = ts
    return booked


def cycle_minutes(booked: list[tuple[float, float]], cycle: dict) -> float | None:
    if cycle["t_open"] is None or (not cycle["open"] and cycle["t_close"] is None):
        return None
    hi = float("inf") if cycle["open"] else cycle["t_close"]
    return sum(m for ts, m in booked if cycle["t_open"] < ts <= hi)


def transcript_prompts(path: Path) -> list[float]:
    """Timestamps of the user-prompt turns of a Claude Code transcript: user-role lines that
    are neither tool results, meta lines, nor sidechain lines. A task-notification turn is
    kept: the UserPromptSubmit hook books it like a typed prompt."""
    out: list[float] = []
    with path.open("rb") as fh:
        for raw in fh:
            if b'"type":"user"' not in raw:
                continue
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if line.get("type") != "user" or line.get("isSidechain") or line.get("isMeta"):
                continue
            content = (line.get("message") or {}).get("content")
            if isinstance(content, list) and all(
                    isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
                continue
            ts = parse_ts(line.get("timestamp"))
            if ts is not None:
                out.append(ts)
    return out


def has_review_records(state: dict) -> bool:
    if any(e.get("event") in REVIEW_RECORD_EVENTS for e in state.get("history") or ()):
        return True
    return bool(state.get("plan_review") or state.get("plan_pair_reviews") or state.get("plan_stage_reviews"))


def load_sessions(snapshot: Path) -> list[tuple[str, dict, Path]]:
    sessions = []
    for path in sorted((snapshot / "state").glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("session_id") == path.stem and isinstance(data.get("history"), list):
            if has_review_records(data):
                sessions.append((path.stem, data, path))
    return sessions


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
