#!/usr/bin/env python3
"""PreToolUse hook: deny a permission SELF-GRANT — widening the agent's own
permission surface in answer to a denial the agent itself just hit.

Difficulty removed: an agent that hits a permission-layer denial can clear it by
editing a `settings*.json` to add the missing `permissions.allow` entry, and then
proceed as if the call had been sanctioned. It has not been. The denial is the one
signal in the whole system that says NOBODY approved this specific action; answering
it with a self-grant turns the gate into a formality, and — worse than the one call —
the widened entry outlives the task, so every later session inherits a permission no
human ever agreed to. Prose said "never do this"; prose is not a gate.

THREE CONDITIONS, ALL OF THEM, OR THE CALL IS ALLOWED. The gate fires only when

  (a) the call WIDENS a permission surface — adds to `permissions.allow` or removes
      from `permissions.deny` (lib/permission_surface.py), and
  (b) this session's transcript carries a denial of an ARMING kind — one that
      expressed an actual permission judgement (lib/denial_arming.py), and
  (c) at least one entry being granted would have COVERED at least one such denied
      call (lib/permission_entry_match.py).

Condition (c) is what separates this from a file guard on settings.json. Editing a
permission surface is ordinary maintenance; editing it to grant yourself the thing you
were just refused is the act being caught. Without (c) the gate would refuse every
legitimate settings edit that happened to follow any denial in a long session.

The conjuncts are evaluated CHEAPEST FIRST: (a) is one file read plus a JSON diff, (b)
walks the transcript, (c) is pure string work. A call that touches no permission
surface — the overwhelming majority — returns None without the transcript ever being
opened.

WHAT EACH TOOL PATH CAN SEE.
  Edit  — the target is read from disk, `old_string`→`new_string` is applied, and the
          parsed before/after pair is diffed. Full precision: (c) is checkable.
  Write — the target is read from disk and diffed against the content being written.
          A Write to a path that does not exist is a CREATION, not a widening, and is
          allowed: there is no prior surface to widen, and a brand-new settings file
          is not how a denial gets cleared.
  Bash  — deliberately coarser. A command is not applied before it runs, so no
          before/after pair exists and there are no entries to test for relevance
          (R7). The path falls back to (a)+(b) alone: any write to a file that is
          TODAY a permission surface, while armed, is refused.

A relative path — a `file_path` on the Edit/Write paths, a write target on the Bash
path — is resolved against the payload's own `cwd`, not against
`git_cwd.effective_git_cwd` as the canon guard does. That helper exists to answer a
question this gate does not ask: which REPOSITORY a path belongs to, so that a canon
checkout can be told from a worktree. Here the target is a permission surface or it is
not, and repository membership does not enter the verdict.

NAMED RESIDUALS carried by this hook (R-numbers are the plan's):

  R7  The Bash path has no not-relevant branch — see above. It is bounded on two
      axes. AXIS 1: the lexer sees only the agent's own command text, so a write
      routed through a script the hook cannot read is not seen at all. AXIS 2: an
      untokenizable command. `bash_write_targets.command_write_targets` reports a
      parse failure as an EMPTY target list, byte-identical to a clean parse that
      found no write — so reading its result by truthiness alone would let one
      unbalanced quote disarm the whole limb toward ALLOW. This hook therefore
      pre-lexes the command itself (`_bash_widening`) and routes a parse failure to
      `_ON_ERROR`, never to the "no write target" branch. `bash_write_targets` is NOT
      changed to raise: its fail-open default is correct for its other consumer, the
      canon guard, and this gate does not get to alter another component's contract.

  R8  Relevance cannot narrow a COMPOUND Bash denial: `shlex.split` destroys the
      separator before `split_segments` ever sees a token, so the denied call can
      only be matched as a whole. Permanent at this layer; no lexer fix closes it.

  PHANTOM WRITE TARGET. Because `shlex` eats a newline as ordinary whitespace, a
  two-line command lexes as ONE segment: `cp a.txt b.txt` followed by
  `jq . settings.json` becomes a single `cp` whose last positional — its destination
  — is the settings file. Such a command can be refused though it writes nothing
  there. The escape is in the deny message: run the lines as separate calls, and each
  is judged on its own. This case is deliberately NOT pinned in the tests: its verdict
  depends on the lexer revision, and pinning it would plant a row that goes red on
  another stage's landing with nobody owning the fix.

INSTALLATION IS A SEPARATE STAGE. This file only decides; wiring it into the hook
settings is stage 6 of the plan, kept separate so that "denies correctly but is not
installed" and "is installed but denies wrongly" stay two distinguishable failures.

DENY is signaled with the PreToolUse permissionDecision JSON on stdout:
  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
   "permissionDecision": "deny", "permissionDecisionReason": "..."}}
"""
from __future__ import annotations

import json
import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import (  # noqa: E402
    bash_write_targets,
    denial_arming,
    permission_entry_match,
    permission_surface,
    shell_tokens,
)
from lib.denial_arming import Verdict  # noqa: E402

# What an error INSIDE this gate resolves to. FAIL-CLOSED: an error denies.
#
# The reasoning is asymmetric, which is why it is a decision and not a preference. A
# false deny costs one blocked call that the user can sanction in a sentence; a false
# allow is the exact outcome the gate exists to prevent, and it is silent and durable
# — the widened entry stays in the file. So every path that cannot reach a verdict —
# an untokenizable command, an unreadable transcript, a payload that did not parse, an
# unexpected exception — routes through here rather than falling through to allow.
#
# ONE constant, read at call time, governing every one of those paths uniformly. That
# is deliberately broader than the surface-touching calls the gate otherwise judges: a
# payload this hook cannot read could ITSELF be a self-grant, and a constant that
# governed only some errors would not be the error policy it claims to be. Both values
# are under test, so revisiting this is a one-line change and no test rots.
_ON_ERROR = "deny"

_FILE_TOOLS = ("Edit", "Write")

_RESPONSES = (
    "Three responses are legitimate here, and widening the surface yourself is not one of "
    "them: stop and ask the user; find a route to the goal that does not need the "
    "permission at all; or have the USER widen the surface deliberately, as their decision "
    "rather than as a side effect of yours."
)


@dataclass(frozen=True)
class _Widening:
    """Condition (a) satisfied: `surface` is a permission surface this call widens.

    `entries` are the entries being granted; `entries_known` says whether that list is
    the real one. On the Bash path it is False and `entries` empty — the command is not
    applied before it runs, so there is no before/after pair to diff and condition (c)
    has nothing to narrow with (R7).
    """
    surface: str
    entries: tuple[str, ...]
    entries_known: bool


def _load_json(text: str):
    try:
        return json.loads(text)
    except Exception:
        return None


def _read_text(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _granted_allow(doc) -> list[str]:
    """Every `permissions.allow` entry `doc` grants."""
    perms = doc.get("permissions") if isinstance(doc, dict) else None
    allow = perms.get("allow") if isinstance(perms, dict) else None
    if not isinstance(allow, list):
        return []
    return [e for e in allow if isinstance(e, str)]


def _apply_edit(old_text: str, tool_input: dict) -> str | None:
    old_string = tool_input.get("old_string")
    new_string = tool_input.get("new_string")
    if not isinstance(old_string, str) or not isinstance(new_string, str):
        return None
    if tool_input.get("replace_all"):
        return old_text.replace(old_string, new_string)
    return old_text.replace(old_string, new_string, 1)


def _widening_between(path: str, old_text: str, new_text: str) -> _Widening | None:
    old_doc = _load_json(old_text)
    new_doc = _load_json(new_text)
    if not (permission_surface.is_permission_surface(old_doc)
            or permission_surface.is_permission_surface(new_doc)):
        return None

    entries = permission_surface.widens(old_doc, new_doc)
    if entries is None:
        # UNKNOWN: the file on disk is not a JSON object, so no baseline exists to
        # diff against. `widens` refuses to coerce that to "no widening", and neither
        # does this caller: with no baseline, nothing shows any entry to be pre-
        # existing, so every entry the new document grants counts as granted here.
        # Over-reports rather than under-reports, and (c) still narrows the result.
        entries = _granted_allow(new_doc)
    if not entries:
        return None
    return _Widening(path, tuple(entries), True)


def _file_tool_widening(tool_name: str, tool_input: dict, cwd: str) -> _Widening | None:
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return None
    path = file_path if os.path.isabs(file_path) else os.path.join(cwd, file_path)

    old_text = _read_text(path)
    if old_text is None:
        return None  # nothing on disk to widen — a creation, not a widening

    if tool_name == "Write":
        new_text = tool_input.get("content")
        if not isinstance(new_text, str):
            return None
    else:
        new_text = _apply_edit(old_text, tool_input)
        if new_text is None:
            return None

    return _widening_between(path, old_text, new_text)


def _is_surface_on_disk(path: str) -> bool:
    text = _read_text(path)
    if text is None:
        return False
    return permission_surface.is_permission_surface(_load_json(text))


def _bash_widening(tool_input: dict, cwd: str) -> tuple[_Widening | None, bool]:
    """`(widening, tokenized)` for a Bash call.

    `tokenized` is False when the command text could not be lexed at all. That case
    MUST stay distinguishable: `command_write_targets` reports it as an empty target
    list, byte-identical to a clean parse that found no write, so a caller reading the
    result by truthiness lets one unbalanced quote reach the allow branch (R7 axis 2).
    The pre-lex below is over exactly the text the lexer sees — heredoc bodies stripped
    first — so the two agree on what "could not parse" means.
    """
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return None, True

    try:
        shlex.split(shell_tokens.strip_heredoc_bodies(command))
    except Exception:
        return None, False

    for target in bash_write_targets.command_write_targets(command, cwd):
        if _is_surface_on_disk(target):
            return _Widening(target, (), False), True
    return None, True


def _on_internal_error(detail: str) -> str | None:
    if _ON_ERROR != "deny":
        return None
    return (
        f"Refusing this call because the permission-self-grant gate could not evaluate it: "
        f"{detail}. This gate is fail-closed — an error it cannot resolve denies rather than "
        f"allows, because the call it was judging touches the agent's own permission surface, "
        f"and a widening that slips through on an error is exactly what the gate exists to "
        f"prevent. Do not retry a variant of this call: stop and ask the user."
    )


def _denial_phrase(denial, matched: str | None) -> str:
    if matched:
        return (f"a {denial.kind} denial of {denial.tool_name}, which the entry {matched!r} "
                f"you are adding would have permitted")
    if denial.tool_name is None:
        return (f"a {denial.kind} denial whose call this session's transcript no longer "
                f"resolves — an unresolved call counts as covered, because a widening cannot "
                f"be shown NOT to answer a denial whose call is unknown")
    return f"a {denial.kind} denial of {denial.tool_name}"


def _deny_msg(widening: _Widening, denial, matched: str | None, tool_name: str) -> str:
    if widening.entries_known:
        granted = "adding " + ", ".join(repr(e) for e in widening.entries)
    else:
        granted = ("writing that file, so which entries it grants is not visible before the "
                   "command runs")
    msg = (
        f"Refusing this {tool_name} call: it widens the agent's own permission surface "
        f"{widening.surface} — {granted} — and this session already hit "
        f"{_denial_phrase(denial, matched)}. A permission-layer denial is the one signal that "
        f"says nobody sanctioned this specific action; clearing it by granting yourself the "
        f"missing permission makes the gate a formality, and the widened entry outlives the "
        f"task. {_RESPONSES}"
    )
    if tool_name == "Bash":
        msg += (
            " This path is coarser than the Edit/Write paths by design: a command is not "
            "applied before it runs, so there is no before/after pair to diff and no way to "
            "ask whether what it grants is RELEVANT to the denial — while armed, any write to "
            "a permission surface is refused. One consequence worth knowing: a multi-line "
            "command can carry a PHANTOM write target, because the lexer eats the newline and "
            "`cp a.txt b.txt` followed by `jq . settings.json` becomes one segment whose last "
            "positional is the settings file. Run the lines as separate calls and each is "
            "judged on its own."
        )
    return msg


def decide(payload: dict) -> str | None:
    """Return a deny reason, or None to allow.

    The three conjuncts in cost order: (a) does this call widen a permission surface,
    (b) is the session armed by a denial that expressed a permission judgement, (c)
    would an entry being granted have covered one of those denied calls.
    """
    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    cwd = payload.get("cwd") or os.getcwd()

    # (a) — cheapest, and the one that lets the vast majority of calls out before the
    # transcript is ever opened.
    if tool_name in _FILE_TOOLS:
        widening = _file_tool_widening(tool_name, tool_input, cwd)
    elif tool_name == "Bash":
        widening, tokenized = _bash_widening(tool_input, cwd)
        if not tokenized:
            return _on_internal_error(
                "its command text could not be tokenized, so whether it writes a permission "
                "surface is unknown rather than known to be no"
            )
    else:
        return None
    if widening is None:
        return None

    # (b)
    arming = denial_arming.armed(payload.get("transcript_path") or "")
    if arming.verdict is Verdict.NOT_ARMED:
        return None
    if arming.verdict is Verdict.UNREADABLE:
        return _on_internal_error(
            "this session's transcript could not be read, so whether a permission denial "
            "preceded this widening is unknown rather than known to be no"
        )

    # (c) — only the Edit/Write paths can ask it; see R7 in the module docstring.
    if not widening.entries_known:
        return _deny_msg(widening, arming.denials[0], None, tool_name)
    for denial in arming.denials:
        if denial.tool_name is None:
            return _deny_msg(widening, denial, None, tool_name)  # fail toward covering
        for entry in widening.entries:
            if permission_entry_match.covers(entry, denial.tool_name, denial.tool_input or {}):
                return _deny_msg(widening, denial, entry, tool_name)
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("payload is not a JSON object")
        reason = decide(payload)
    except Exception as exc:
        reason = _on_internal_error(f"the gate raised {type(exc).__name__}")

    if reason:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
