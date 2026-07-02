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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import config_root  # noqa: E402

LAND_BRANCH_SCRIPT = Path(__file__).resolve().parent / "land-branch.py"
LAND_BRANCH_TIMEOUT_SEC = 5
GIT_TIMEOUT_SEC = 5

# Branch names that are trunk / shared — a push to these needs explicit
# confirmation, so we do NOT nudge a proactive push for them.
SHARED_BRANCH_RE = re.compile(
    r"^(trunk|master|main|develop|release[-/]|releases[-/]|stable)",
    re.IGNORECASE,
)

BRANCH_HYGIENE_HINT = (
    "Also — a working branch is cleanly landable into trunk: run "
    "`python3 scripts/land-branch.py --check` to preview, then bundle a "
    "land+delete option into the SAME resolution AskUserQuestion (ref-only "
    "ff; trunk-push needs explicit confirmation). Don't leave the branch "
    "hanging."
)

UNPUSHED_BRANCH_HINT = (
    "Also — the current working branch has commits not on its remote "
    "(unpushed / ahead of upstream, or no upstream yet). Per CLAUDE.md "
    "§ On task resolution, deliver committed work to its terminal VCS state "
    "proactively: bundle a **push** option into the SAME resolution "
    "AskUserQuestion, recommended-first — pushing a personal / working "
    "branch is pre-authorized (§ Acting without asking #4). Never leave the "
    "push as a passive 'tell me if you want to push'."
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


def resolution_gate_open(session_id: str) -> bool:
    """True iff an agentctl state file says node==RESOLUTION and the resolution
    gate has not passed. Missing/corrupt state -> False (fall back to prose)."""
    if not session_id:
        return False
    path = config_root.resolve_agentctl_state_file(session_id)
    if path is None:
        return False
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


def _git(repo_dir: str, *args: str) -> str | None:
    """Best-effort `git -C repo_dir <args>` -> stripped stdout, or None on any
    failure (non-zero exit, timeout, git absent, exception)."""
    try:
        proc = subprocess.run(
            ["git", "-C", repo_dir, *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SEC,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def unpushed_branch_hint(repo_dir: str) -> str | None:
    """Best-effort UNPUSHED_BRANCH_HINT when the current branch is a personal
    working branch (not trunk/shared) carrying commits that are not on its
    remote — either ahead of its upstream, or with no upstream configured
    while local commits exist. Any failure degrades silently to None so the
    hook never breaks a resolution turn."""
    branch = _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD")
    if not branch or branch == "HEAD":  # empty repo / detached HEAD
        return None
    if SHARED_BRANCH_RE.match(branch):
        return None
    # Upstream configured? If so, compare ahead-count; else treat any local
    # commit as unpushed.
    upstream = _git(repo_dir, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream:
        ahead = _git(repo_dir, "rev-list", "--count", "@{u}..HEAD")
        if ahead and ahead.isdigit() and int(ahead) > 0:
            return UNPUSHED_BRANCH_HINT
        return None
    # No upstream — is there at least one commit to push?
    head = _git(repo_dir, "rev-list", "--count", "HEAD")
    if head and head.isdigit() and int(head) > 0:
        return UNPUSHED_BRANCH_HINT
    return None


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
        else:
            # Only nudge the plain push when the branch is not cleanly
            # landable (else the landable hint already covers delivery).
            try:
                push_hint = unpushed_branch_hint(repo_dir)
            except Exception:
                push_hint = None
            if push_hint:
                print(push_hint)
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
