#!/usr/bin/env python3
"""Stop hook: block the end of a turn that defers an AskUserQuestion to "next
message" via buttons but never arms the background timer that is supposed to
open that next turn.

Difficulty removed: the delivery-split rule (CLAUDE.md § Escalation —
"Long-artifact exception") requires that a long artifact or a turn that
already ran a tool deliver its `AskUserQuestion` on a FRESH turn, opened by a
`sleep 2` background timer's completion notification. CLAUDE.md states the
atomicity explicitly: "Arming the `sleep 2` timer and deferring the ask are
one atomic act — a prose promise to 'ask next turn' *without* the timer armed
in the **same** turn silently strands the ask, because no next turn ever
fires." That rule itself already covers the failure ("arm the timer"); it was
observed violated twice on 2026-07-09 anyway — the coordinator wrote the
deferral promise in prose but did not start the timer, so no next turn ever
opened and the promised ask never reached the user. This hook is the
structural backstop: it cannot un-strand the ask (the turn is already over by
the time Stop fires), but it makes the failure observable rather than silent.

Detection (current turn only — the slice of the transcript from the most
recent turn-boundary entry to the end; see lib.transcript_turns for the
boundary predicate). All three must hold to warn:
  1. The turn's assistant text contains a deferral-promise pattern (a small,
     tunable regex list below — RU+EN phrasings of "I'll ask via buttons next
     message").
  2. No timer was armed this turn: no backgrounded `Bash` tool_use whose
     command contains `sleep`, and no `ScheduleWakeup`/`CronCreate` tool_use.
  3. No `AskUserQuestion` tool_use was already emitted this turn (if the ask
     already fired, nothing was deferred).

Action: BLOCK — `{"decision": "block", "reason": ...}` on stdout, exit 0. An
earlier revision printed a plain warning and exited 0; that was a no-op. For a
Stop hook, exit-0 stdout is shown only to the human in transcript mode and
never reaches the model, so the guard could not constrain the actor it exists
to constrain. A block decision does reach the model and prevents the turn from
ending, giving it the chance to arm the timer or ask inline.

A blocking Stop hook can wedge a turn: the model may answer the block in prose
without calling a tool, in which case neither the `timer_armed` nor the
`ask_already_emitted` veto can ever fire and the block repeats forever. The
escalation is therefore bounded to ONE block per consecutive Stop streak — a
marker file under _DEFAULT_STATE_DIR (override with $ASK_DEFER_TIMER_STATE_DIR)
keyed by session id. The first blocking Stop creates the marker; the next Stop
for that session consumes it and stays silent, capping the worst case (a false
positive, or a stubborn model) at one extra turn.

Text that merely quotes or documents the promise phrasings — a code fence, an
inline code span, a blockquote — is stripped before matching, so working on
this hook does not trip it.

Any parse or filesystem failure fails open (silent, exit 0, no block): an
unreadable transcript, malformed stdin, or an unwritable state dir must never
wedge the workflow. Deterministic, offline, no network.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.transcript_turns import _content_items, _is_real_user_prompt  # noqa: E402
# The armed-timer / ask-emitted predicates live in ONE shared module so this warn
# hook and the resolution guardian in hook-turn-end-gate.py cannot drift apart.
from timer_arm_detect import ask_emitted as ask_already_emitted  # noqa: E402
from timer_arm_detect import timer_armed  # noqa: E402

# Deferral-promise phrasings (RU+EN). Two layers, both kept: the original flat
# list of whole phrasings, plus a promise-verb x ask-noun construction matched
# in either order. The construction is what catches the phrasings the flat list
# missed on 2026-07-09 ("переспрошу кнопками…", "вынесу его тебе кнопками").
# The verb is mandatory — a bare mention of buttons is not a promise.
_PROMISE_VERB = (
    r"(?:переспрошу|спрошу|задам|вынесу|пришлю|отправлю|уточню|приедут|придут|прилетят"
    r"|(?:i['’]?ll|i will|will)\s+(?:ask|send|surface))"
)
_ASK_NOUN = r"(?:кнопк\w*|вопрос\w*|button\w*|question\w*)"
# Same-line proximity: the two halves of a promise sit in one sentence. A
# turn-wide ".*" would pair a verb in one paragraph with a noun in another.
_NEAR = r"[^\n]{0,80}"

_PROMISE_PATTERNS = [
    r"кнопк\w*.*(следующ|next)",
    r"(следующ\w* сообщени\w*|next message).*(ask|вопрос|кнопк\w*|question)",
    r"задам.*(кнопк\w*|вопрос)",
    r"ask\w* .*next (turn|message)",
    r"buttons? next",
    rf"{_PROMISE_VERB}{_NEAR}{_ASK_NOUN}",
    rf"{_ASK_NOUN}{_NEAR}{_PROMISE_VERB}",
]
_PROMISE_RE = [
    re.compile(p, re.IGNORECASE | re.UNICODE | re.DOTALL) for p in _PROMISE_PATTERNS
]

# Quoted regions: text that discusses the phrasings rather than promising them.
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_BLOCKQUOTE_RE = re.compile(r"^\s*>.*$", re.MULTILINE)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")

_WARNING = (
    "[ask-defer-timer] This turn's text promises to ask via buttons next "
    "message, but no `sleep 2` background timer (or ScheduleWakeup/CronCreate) "
    "was armed this turn — no next turn will fire, so the ask is stranded. Per "
    "CLAUDE.md, arming the timer and deferring the ask are one atomic act. Do "
    "one of: (a) arm the timer now — a backgrounded `Bash` `sleep 2` — so the "
    "notification opens a fresh turn carrying the AskUserQuestion; or (b) drop "
    "the promise and ask nothing. If you were only quoting or discussing this "
    "phrasing rather than promising it, just end the turn again — this hook "
    "never blocks twice in a row."
)

# One-shot bound: marker file per session id. Overridable so tests never touch
# a real session's state.
_DEFAULT_STATE_DIR = "/tmp/cc-ask-defer-timer"
_SUPPRESSED_NOTE = "[ask-defer-timer] block suppressed by one-shot bound\n"


def _current_turn_entries(transcript_path: Path) -> list[dict] | None:
    """Entries of the current (ending) turn: everything after the most recent
    turn-boundary entry (see _is_real_user_prompt) up to the end of the
    transcript. None when the observable is unavailable (unreadable file, no
    entries, no boundary found) — callers must fail open."""
    try:
        lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None
    entries: list[dict] = []
    for line in lines:
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    if not entries:
        return None
    boundary_idx = None
    for i in range(len(entries) - 1, -1, -1):
        if _is_real_user_prompt(entries[i]):
            boundary_idx = i
            break
    if boundary_idx is None:
        return None
    return entries[boundary_idx + 1 :]


def _turn_assistant_text(entries: list[dict]) -> str:
    parts = []
    for entry in entries:
        if entry.get("type") != "assistant":
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        for item in _content_items(message):
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
    return "\n".join(parts)


def _strip_quoted(text: str) -> str:
    """Drop regions that quote rather than assert: fenced code blocks, inline
    code spans, blockquote lines."""
    text = _FENCE_RE.sub(" ", text)
    text = _BLOCKQUOTE_RE.sub(" ", text)
    return _INLINE_CODE_RE.sub(" ", text)


def has_deferral_promise(text: str) -> bool:
    stripped = _strip_quoted(text)
    return any(pattern.search(stripped) for pattern in _PROMISE_RE)


def should_warn(entries: list[dict]) -> bool:
    """Pure decision over one turn's entries."""
    if ask_already_emitted(entries):
        return False
    if not has_deferral_promise(_turn_assistant_text(entries)):
        return False
    return not timer_armed(entries)


def _marker_path(session_id: str) -> Path:
    state_dir = os.environ.get("ASK_DEFER_TIMER_STATE_DIR") or _DEFAULT_STATE_DIR
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id) or "_unknown"
    return Path(state_dir) / safe


def _consume_marker(marker: Path) -> bool:
    """True when a marker existed (and is now gone) — i.e. this session already
    spent its one block. Fails toward True: if we cannot tell, do not block."""
    try:
        if not marker.exists():
            return False
        marker.unlink(missing_ok=True)
        return True
    except OSError:
        return True


def _arm_marker(marker: Path) -> bool:
    """True when the marker was created and blocking is therefore bounded."""
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        return True
    except OSError:
        return False


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        return 0

    entries = _current_turn_entries(Path(transcript_path))
    if entries is None:
        return 0

    session_id = payload.get("session_id")
    marker = _marker_path(session_id if isinstance(session_id, str) else "")

    if not should_warn(entries):
        _consume_marker(marker)
        return 0

    if _consume_marker(marker):
        # Already blocked once for this streak; let the turn end. Stderr is
        # invisible to the model on a non-blocking Stop — this is for a human
        # reading a session review, and distinguishes the two silent paths.
        sys.stderr.write(_SUPPRESSED_NOTE)
        return 0

    if not _arm_marker(marker):
        return 0

    print(json.dumps({"decision": "block", "reason": _WARNING}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
