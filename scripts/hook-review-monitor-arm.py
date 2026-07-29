#!/usr/bin/env python3
"""PostToolUse(Bash) hook: arm the review poller the moment a command opens a
review — no agent action required.

Difficulty removed: `hook-review-mergeable-guardian.py` (the Stop guard) can only
*notice* at turn end that a review the session authored has no monitor, and then
ask the agent to arm one by hand. That leaves the mechanism one forgettable step
short of autopilot: the nudge lands in context, the agent is mid-task, and the
review still sits unwatched. Whether a monitor should exist is fully decidable
from the tool call itself — the command's create verb and the review URLs its
own output printed are all the evidence needed — so the launch belongs to the
machine, not to the model's recall. This hook is the ACT the guardian was only
ever able to advise.

Authorship is correlated PER REVIEW, exactly as the guardian correlates it per
review across a session — not per call. "One call carried a create verb AND
printed some review URL" is co-occurrence, and co-occurrence is not authorship:
it arms a poller for the unrelated review a `depends on …` line quoted, and for
the review that the read half of `pr create … && pr view <other>` merely looked
at. Each armed review must instead be tied to the create ACTION — named by its
id at the create verb, or the single unambiguous URL the call emitted. The exact
predicate and its deliberate under-fire bias are in `to_arm`.

Gated on a configured probe. Core ships no probe (which CLI reports a review's
state is platform-specific), and a poller launched without one can only append
PROBE_UNREADABLE to a log until its cap — worse than not launching, because it
also registers itself and thereby silences the guardian's honest nudge. With
`review_probe=` unset in agent-identity.local this hook does nothing at all and
the Stop guardian keeps advising a manual arm.

Fail-open and fast: a PostToolUse hook that raises or blocks is far worse than a
missed launch, so every path is wrapped and returns 0, and the poller is started
fully detached (own session, stdio detached) and never waited on.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import review_open_detect  # noqa: E402

MONITOR = Path(__file__).resolve().parent / "review-monitor.sh"


def result_text(response) -> str:
    """Whatever a Bash tool result printed, as one string.

    The payload's `tool_response` shape is harness-version-dependent — a bare
    string on some paths, a `{"stdout":…, "stderr":…}` dict on others, a list of
    content blocks elsewhere. Reading only one shape would make the hook silently
    dead against the others, so all three are flattened here.
    """
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        return "\n".join(v for v in response.values() if isinstance(v, str))
    if isinstance(response, list):
        parts = []
        for item in response:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _numeric_id(identity: str) -> str:
    """The platform-side review number of an identity (`host/repo/42` -> `42`),
    which is the form a create command names its review by."""
    return str(identity).rsplit("/", 1)[-1]


def to_arm(command: str, output: str) -> dict:
    """{identity -> url} for reviews this ONE call is shown to have OPENED and
    that have no monitor yet.

    The predicate, in full. Given a command carrying a create verb and the
    review URLs its own output printed:

    1. If the command NAMES review ids at the create verb
       (`review_open_detect.referenced_ids` — the id standing directly after
       the verb, as in `review publish 42`), only those are candidates, and
       only where the id resolves to exactly one of the printed URLs. A named
       id matching no printed URL, or two of them (the same number in two
       repos), arms nothing: the command told us which review it acted on and
       the output does not corroborate it.
    2. Otherwise the call must be unambiguous on its face — the output printed
       exactly ONE review URL and the command carries no read verb
       (`review_open_detect.reads_review`). That one URL is then the review the
       create produced.
    3. Anything else arms nothing.

    Why not simply "a create verb somewhere AND a review URL somewhere": that
    is co-occurrence, not authorship. It arms a poller for the unrelated review
    a `depends on …` line happened to quote, and for the review that the `view`
    half of `pr create … && pr view <other>` merely READ. Both are the same
    session-scope fallacy the Stop guardian already closed by correlating
    authorship per review rather than per session.

    The tradeoff is deliberately asymmetric. This predicate UNDER-fires: a
    create call whose output legitimately mentions a second review arms nothing
    at all, even though one of the two really was authored. That costs a
    missed autopilot, and the Stop guardian still advises arming one by hand.
    Over-firing costs far more — a poller for a review the agent only read
    produces a false "drive it to mergeable" nudge every session, which trains
    the agent to ignore the guard and takes the honest nudges down with it.

    Already-armed reviews are dropped against the monitored-reviews registry,
    whose entries persist past a monitor's exit — a review is armed once, ever,
    not once per session.
    """
    if not review_open_detect.opens_review(command):
        return {}
    found = review_open_detect.review_urls(output)
    if not found:
        return {}
    named = review_open_detect.referenced_ids(command)
    candidates: dict = {}
    if named:
        by_id: dict = {}
        for identity, url in found.items():
            by_id.setdefault(_numeric_id(identity), []).append((identity, url))
        for num in named:
            matches = by_id.get(num) or []
            if len(matches) == 1:
                candidates[matches[0][0]] = matches[0][1]
    elif len(found) == 1 and not review_open_detect.reads_review(command):
        candidates = dict(found)
    if not candidates:
        return {}
    armed_set = review_open_detect.armed_identities()
    return {i: u for i, u in candidates.items() if i not in armed_set}


def _state_dir() -> Path:
    """Durable dir `<agent-home>/state/review-monitor/`, holding each armed
    review's claim marker and its poller's log — mirroring the guardian's
    `state/review-guardian/`."""
    return review_open_detect.state_dir("review-monitor")


def _safe_name(identity: str) -> str:
    """A review identity as one filesystem-safe path component, or "".

    Returning "" for an unusable identity is load-bearing: every caller checks
    it before interpolating, so no path below is ever built from an empty
    variable (CLAUDE.md § Limits).
    """
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in str(identity))
    return safe.strip("-")[:120]


def _claim(identity: str) -> Path | None:
    """Claim `identity` for this process, or None if someone already holds it.

    Two review-opening calls seconds apart would otherwise both see an empty
    registry (a poller writes its entry only once it starts) and launch two
    monitors for one review. `O_CREAT | O_EXCL` makes the claim atomic, so the
    dedup does not depend on the loser noticing the winner in time.
    """
    safe = _safe_name(identity)
    if not safe:
        return None
    state = _state_dir()
    try:
        state.mkdir(parents=True, exist_ok=True)
        marker = state / (safe + ".armed")
        fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    except FileExistsError:
        return None
    except Exception:
        return None
    return state / (safe + ".out")


def _release(identity: str) -> None:
    """Drop a claim whose launch never happened, so a later review-opening call
    can try again. A claim that outlived a failed spawn would disable autopilot
    for that review permanently, and the registry — written by the poller that
    never started — would have nothing to fall back on."""
    safe = _safe_name(identity)
    if not safe:
        return
    try:
        (_state_dir() / (safe + ".armed")).unlink()
    except Exception:
        pass


def launch(url: str, out: Path, probe: str) -> bool:
    """Start the poller detached. The probe is passed explicitly rather than
    left to review-monitor.sh's own config default so that what this hook
    checked is exactly what runs."""
    try:
        with open(os.devnull, "wb") as devnull:
            subprocess.Popen(
                [
                    "bash", str(MONITOR),
                    "--review-id", url,
                    "--out", str(out),
                    "--probe", probe,
                ],
                stdin=subprocess.DEVNULL,
                stdout=devnull,
                stderr=devnull,
                start_new_session=True,
                cwd=str(MONITOR.parent),
            )
        return True
    except Exception:
        return False


def arm(payload: dict) -> list:
    """Arm a monitor for every review this call opened; return the URLs armed."""
    if payload.get("tool_name") != "Bash":
        return []
    probe = review_open_detect.review_probe()
    if not probe:
        return []
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command:
        return []
    pending = to_arm(command, result_text(payload.get("tool_response")))
    armed = []
    for identity, url in pending.items():
        out = _claim(identity)
        if out is None:
            continue
        if launch(url, out, probe):
            armed.append(url)
        else:
            _release(identity)
    return armed


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        armed = arm(payload)
    except Exception:
        return 0
    if armed:
        print(
            "[review-monitor] Armed a detached poller for the review you just "
            "opened: " + ", ".join(armed) + ". It logs CHECK_FAILED / "
            "NEW_COMMENTS / APPROVED / MERGED under "
            f"{_state_dir()}; read it when you are next woken and drive the "
            "review to mergeable (leaves/review-accompanies-code.md)."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
