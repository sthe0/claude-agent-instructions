#!/usr/bin/env python3
"""UserPromptSubmit hook: detect tracker references in the user's prompt
and emit a system-context reminder to invoke the `tracker-management`
skill.

Rule (CLAUDE.md § Recognizing when to delegate, tracker-management skill
trigger description): invoke the skill when the user mentions a ticket
key (`ABC-123`), the words "ticket"/"issue"/"tracker"/"тикет"/"тред",
asks to post/update/close a ticket, or links a tracker URL.

This hook lifts the keyword-detection part from agent recall to a
deterministic regex scan. The skill decision still belongs to the
agent — the hook only nudges.

It also performs a mount-hygiene check: when a ticket key is present and
the session sits in a *different* task-mount than the ticket's own, it
emits a second `[mount-check]` reminder. Rationale (root cause): cwd is
fixed at session/topic creation (a chat-bridge topic -> session_id -> cwd, or
`claude-task <TICKET>`); nothing re-evaluates it when a ticket key later
appears mid-conversation, so a session can keep operating on a ticket
from the wrong mount (observed: a `main`-mount session continuing
DEEPAGENT-440 instead of its dedicated mount). The rule part (does cwd's
mount match the ticket, and does a dedicated mount exist?) is
deterministically decidable -> mechanized here; the perception part
(should I actually relocate, given the remaining work?) stays with the
agent — the hook only nudges, never forces a `cd`.

Scope:
  - Reads the UserPromptSubmit JSON on stdin.
  - Scans `prompt` text for two patterns:
      1. Ticket key shape: `\\b[A-Z][A-Z0-9]{1,9}-\\d+\\b` (excluding a
         small allow-list of common false positives — ISO/RFC/UTF/etc.).
      2. Keywords: ticket / тикет / issue / tracker / тред.
  - When a ticket key is found, compares `cwd` against the task-mount
    root (`CLAUDE_TASK_MOUNT_ROOT`, else `~/task-mounts` if it exists,
    else the check is a complete no-op — keeping Core org-neutral, since
    the mount layout is a Yandex/arc convention).
  - Emits a stdout line on match — UserPromptSubmit stdout is appended
    to the model's system context for the upcoming turn.
  - Exit 0 always.
"""
from __future__ import annotations

import json
import os
import re
import sys

TICKET_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")
KEYWORDS_RE = re.compile(
    r"\b(ticket|тикет|issue|tracker|тред)\b",
    re.IGNORECASE | re.UNICODE,
)

# Common ticket-key-shaped strings that are not tracker keys.
FALSE_POSITIVES = {
    "COVID-19", "ISO-8601", "ISO-4217", "ISO-3166",
    "RFC-822", "RFC-2822", "RFC-5322", "RFC-7231",
    "UTF-8", "UTF-16", "UTF-32",
    "ECMA-262", "ECMA-402",
    "SHA-1", "SHA-256", "SHA-512", "MD-5",
    "HTTP-1", "HTTP-2", "HTTP-3",
}


def find_ticket_keys(prompt: str) -> list[str]:
    return sorted({k for k in TICKET_KEY_RE.findall(prompt) if k not in FALSE_POSITIVES})


def find_signals(prompt: str) -> list[str]:
    signals: list[str] = []
    keys = find_ticket_keys(prompt)
    if keys:
        signals.append(f"ticket key(s): {', '.join(keys)}")
    keywords = sorted({m.group(0).lower() for m in KEYWORDS_RE.finditer(prompt)})
    if keywords:
        signals.append(f"keyword(s): {', '.join(keywords)}")
    return signals


def mount_root() -> str | None:
    """Task-mount root, or None when the org convention is absent.

    org-neutral: active only when `CLAUDE_TASK_MOUNT_ROOT` is set or
    `~/task-mounts` exists; otherwise the whole mount check is a no-op.
    """
    root = os.environ.get("CLAUDE_TASK_MOUNT_ROOT") or os.path.expanduser("~/task-mounts")
    return root if root and os.path.isdir(root) else None


def mount_mismatches(keys: list[str], cwd: str | None) -> list[tuple[str, str, list[str]]]:
    """Ticket keys whose dedicated mount exists but cwd is in another mount.

    Returns (key, current_mount_segment, matching_mount_dirs). Empty when
    the check does not apply: no root, cwd outside the root, already in
    the ticket's mount, or no dedicated mount for the ticket.
    """
    root = mount_root()
    if not root or not cwd:
        return []
    prefix = root.rstrip(os.sep) + os.sep
    if not cwd.startswith(prefix):
        return []
    current = cwd[len(prefix):].split(os.sep, 1)[0]
    if not current:
        return []
    try:  # shallow, name-only — no recursion into the (FUSE-backed) mounts, no stat storm
        dirs = [e.name for e in os.scandir(root) if e.is_dir()]
    except OSError:
        return []
    out: list[tuple[str, str, list[str]]] = []
    for key in keys:
        if current.startswith(key):
            continue  # already in this ticket's mount
        matches = sorted(d for d in dirs if d.startswith(key))
        if matches:
            out.append((key, current, matches))
    return out


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    prompt = payload.get("prompt") or ""
    if not isinstance(prompt, str) or not prompt.strip():
        return 0

    signals = find_signals(prompt)
    if not signals:
        return 0

    print(
        f"[tracker-reminder] User prompt references a tracker — {'; '.join(signals)}. "
        f"Invoke the `tracker-management` skill (layered on top of normal coordination)."
    )

    cwd = payload.get("cwd")
    cwd = cwd if isinstance(cwd, str) else None
    for key, current, matches in mount_mismatches(find_ticket_keys(prompt), cwd):
        print(
            f"[mount-check] cwd is in mount '{current}', but {key} has dedicated "
            f"mount(s): {', '.join(matches)}. If this session will touch "
            f"ticket-local files/VCS, switch to the ticket mount "
            f"(`claude-task {key}`) or its own session — proceeding here risks "
            f"mixing task working-trees."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
