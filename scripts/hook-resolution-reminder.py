#!/usr/bin/env python3
"""UserPromptSubmit hook: nudge the agent to ask for explicit resolution
when the user's prompt is brief gratitude — ambiguous between "thanks
for the work" and "task is over".

At the resolution gate, also checks whether the working branch is
cleanly landable into trunk (`land-branch.py --check`) and, if so,
appends a branch-hygiene line so a confirmed-resolved task doesn't
leave an unmerged branch behind. The check is best-effort and silent
on any failure — it never blocks or alters the gate nudge itself.

Safety net for the prose rule in CLAUDE.md § On task resolution:
the agent should close substantive tasks proactively when the plan's
`## Final verification` has passed (recap + explicit ask). If that
proactive close was missed and the user replies with bare gratitude,
this hook prevents the agent from treating "спасибо" / "thanks" as
silent confirmation.

Recurring failure mode this addresses (see experience leaf
2026-05-25-resolution-gate-confirm-before-record): agent finishes
work, user thanks, agent closes without writing the experience leaf
or asking for resolution.

Matches either:
  (a) Brief gratitude: ≤ MAX_WORDS tokens AND a gratitude keyword.
  (b) Resolution meta-question: ≤ META_MAX_WORDS tokens AND a gratitude
      keyword AND a meta-keyword about asking / being resolved / done.
      Catches prompts like "спасибо, почему не спрашиваеш решена ли
      задача?" where the user explicitly reminds the agent of the gate
      but the prompt is too long for (a).

Detection is intentionally permissive — false positives (extra
reminder when the user is fine) are cheap; false negatives (silent
miss of a resolution gate) are expensive.

State-aware path: when an agentctl session is being driven and the
engine is parked at the resolution gate (node == RESOLUTION and
resolution.passed is falsy), the nudge fires regardless of the
user's phrasing — the gate is objectively open, so the agent must
not close without an explicit confirmation. Sessions with no state
file fall back to the gratitude/meta heuristics above (prose mode).

Exit 0 always; emit stdout (becomes additional system context).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

STATE_ROOT = Path.home() / ".claude" / "agentctl" / "state"
LAND_BRANCH_SCRIPT = Path(__file__).resolve().parent / "land-branch.py"
LAND_BRANCH_TIMEOUT_SEC = 5

BRANCH_HYGIENE_HINT = (
    "Also — a working branch is cleanly landable into trunk: run "
    "`python3 scripts/land-branch.py --check` to preview, then bundle a "
    "land+delete option into the SAME resolution AskUserQuestion (ref-only "
    "ff; trunk-push needs explicit confirmation). Don't leave the branch "
    "hanging."
)

MAX_WORDS = 6
META_MAX_WORDS = 20

# Brief gratitude keywords across languages. Excludes "ok"/"good" (too
# common as mid-task acknowledgments) and "done" (often used by the
# agent / user about completing a sub-step, not the task).
GRATITUDE_RE = re.compile(
    r"\b(thanks|thank\s*you|thx|спасибо|спс|пасиба|merci|"
    r"perfect|идеально|отлично|cool|круто|super|супер|"
    r"great|превосходно|nice|wonderful|amazing|excellent|"
    r"окей|👍|🙏|❤️|💯|🎉)\b",
    re.IGNORECASE | re.UNICODE,
)
# Meta-keywords signaling a question about the resolution gate itself
# (the user pointing at "why didn't you ask if it's done?"). Paired with
# a gratitude keyword to keep the false-positive rate low.
META_RE = re.compile(
    r"(спрашивае(ш|шь)|спросил[аи]?|почему\s+не|реш(ен|ён)а?|"
    r"закрыт[аоы]?|готов[оаы]?|закры|ask(ed|ing)?|"
    r"why\s+(didn'?t|not|haven'?t|aren'?t|don'?t)|"
    r"resolved|done|finished|closed|ready)",
    re.IGNORECASE | re.UNICODE,
)
WORD_RE = re.compile(r"\w+", re.UNICODE)


def is_brief_gratitude(prompt: str) -> bool:
    words = WORD_RE.findall(prompt)
    if not words or len(words) > MAX_WORDS:
        return False
    return bool(GRATITUDE_RE.search(prompt))


def is_resolution_meta_question(prompt: str) -> bool:
    words = WORD_RE.findall(prompt)
    if not words or len(words) > META_MAX_WORDS:
        return False
    return bool(GRATITUDE_RE.search(prompt) and META_RE.search(prompt))


def _safe(session_id: str) -> str:
    safe = "".join(c for c in (session_id or "") if c.isalnum() or c in "-_")
    return safe or "nosession"


def resolution_gate_open(session_id: str) -> bool:
    """True iff an agentctl state file says node==RESOLUTION and the resolution
    gate has not passed. Missing/corrupt state -> False (fall back to prose)."""
    if not session_id:
        return False
    path = STATE_ROOT / f"{_safe(session_id)}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if data.get("node") != "RESOLUTION":
        return False
    resolution = data.get("resolution") or {}
    return not bool(resolution.get("passed"))


def landable_branch_hint(repo_dir: str) -> str | None:
    """Best-effort: BRANCH_HYGIENE_HINT if `land-branch.py --check` reports
    LANDABLE in repo_dir, else None. Any failure (missing script, timeout,
    non-zero exit, exception) degrades silently to None."""
    try:
        proc = subprocess.run(
            [sys.executable, str(LAND_BRANCH_SCRIPT), "--check", "-C", repo_dir],
            capture_output=True,
            text=True,
            timeout=LAND_BRANCH_TIMEOUT_SEC,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return BRANCH_HYGIENE_HINT


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    if resolution_gate_open(payload.get("session_id") or ""):
        print(
            "[resolution-reminder] The agentctl session is parked at the "
            "resolution gate (node=RESOLUTION, not yet passed). Per CLAUDE.md "
            "§ On task resolution, do NOT close the task on this message "
            "regardless of its wording. Give a one-line recap "
            "(`Requested: X. Delivered: Y.`) and ask the user to confirm "
            "explicitly via AskUserQuestion, then run `agentctl resolve "
            "--by <user>` only after an unambiguous confirmation."
        )
        repo_dir = payload.get("cwd") or str(Path(__file__).resolve().parent)
        try:
            hint = landable_branch_hint(repo_dir)
        except Exception:
            hint = None
        if hint:
            print(hint)
        return 0

    prompt = payload.get("prompt") or ""
    if not isinstance(prompt, str) or not prompt.strip():
        return 0
    if not (is_brief_gratitude(prompt) or is_resolution_meta_question(prompt)):
        return 0
    print(
        "[resolution-reminder] User prompt is brief gratitude — ambiguous "
        "between 'thanks for the work' and 'task is resolved'. Per "
        "CLAUDE.md § On task resolution, do NOT treat bare gratitude as "
        "confirmation. If the plan's Final verification has passed, close "
        "with a one-line recap (`Requested: X. Delivered: Y.`) and ask "
        "`Considered resolved?` explicitly. Otherwise continue the work."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
