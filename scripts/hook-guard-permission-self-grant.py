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

WHAT THE GATE COULD NOT ESTABLISH IS NOT A "NO". Conjunct (a) is THREE-valued, never
two. A target ABSENT from disk is an established fact — nothing there to widen, so the
call is a creation and is allowed. A target that exists but could NOT be read, a
command that could not be tokenized, and a payload missing the fields this gate reads
are none of them facts about the call: they are UNKNOWN, the gate either did not look
or looked and could not tell. An UNKNOWN (a) neither allows nor denies on its own —
`UNKNOWN AND False` is still False — so it falls through to (b), and a session whose
(b) is anything other than NOT_ARMED (armed, or a transcript that could not be read)
pays `_ON_ERROR`. Denying every UNKNOWN outright would refuse innocent calls in sessions
carrying no denial at all; allowing them is the hole this gate exists to close. This is
the one case that reaches the transcript without (a) being settled, so the cost claim
above holds for every call whose (a) the gate could answer.

WHAT THAT CHOICE RESTS ON, stated so it can be attacked rather than assumed. Routing an
UNKNOWN (a) through (b) TRANSFERS the burden onto (b): before, an unresolved (a) denied
whatever (b) said; now a NOT_ARMED verdict is enough to allow. So the gate's soundness
on this path is exactly `lib/denial_arming` never reporting NOT_ARMED for a session that
does carry a real arming denial. That condition has THREE routes to failure, not one, and
vocabulary completeness answers only the first. `_ARMING_KINDS` is decompiled from the
client rather than sampled from observed transcripts, so the gate holds AGAINST A CORRECT
TRANSCRIPT; a client that adds an arming denial kind silently narrows it, failing toward
ALLOW. The other two are the ways the transcript is not the session, and both are named in
`lib/denial_arming`: R5, a denial not yet flushed to disk is not among the rows read, so
the file is read cleanly end to end and comes back NOT_ARMED; and R3, arming is per-agent,
so a parent's denial never arms a spawned agent reading its own transcript. Before this
routing an unresolved (a) denied whatever (b) said — so R5 and R3 became load-bearing on
this path for the first time HERE, which is why they are restated rather than left one
file away.

WHAT EACH TOOL PATH CAN SEE.
  Edit  — the target is read from disk, `old_string`→`new_string` is applied, and the
          parsed before/after pair is diffed. Full precision: (c) is checkable.
  Write — the target is read from disk and diffed against the content being written.
          A Write to a path ABSENT from disk is a CREATION, not a widening, and is
          allowed: there is no prior surface to widen, and a brand-new settings file
          is not how a denial gets cleared. A path that exists but cannot be read is a
          different fact and takes the UNKNOWN route above, not this one.
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
      UNKNOWN, never to the "no write target" branch. `bash_write_targets` is NOT
      changed to raise: its fail-open default is correct for its other consumer, the
      canon guard, and this gate does not get to alter another component's contract.

  WIDENING LEVERS THIS GATE DOES NOT SEE. `permissions.allow` and `permissions.deny`
  are not the only way a settings document widens what the agent may do.
  `permissions.defaultMode` is a live lever on this fleet — `settings/base.json`
  carries `"auto"` and `benchmark-profile/settings.json` carries `"acceptEdits"` — as
  are `permissions.additionalDirectories` and `permissions.ask`. Flipping
  `defaultMode` widens the surface without adding a single allow entry, so conjunct
  (a) never fires and this gate never sees the call. Closing it means teaching
  `lib/permission_surface.widens` to report those levers; the fix belongs there, not
  here, and is out of scope for this stage. Named so that "the gate allowed it"
  cannot later be read as "the gate judged it safe".

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
from enum import Enum
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
# an unreadable target, an untokenizable command, an unreadable transcript, a payload
# missing the fields this gate reads, an unexpected exception — routes through here
# rather than falling through to allow.
#
# ONE constant, read at call time, governing every one of those paths uniformly. Both
# values are under test, so revisiting this is a one-line change and no test rots.
#
# WHAT IS NOT AN ERROR, and so never reaches this constant. An UNMODELLED tool exits on
# tool name alone, before any payload field is looked at: a gate that denied tools it
# does not model would be far outside its remit, and its input shape is not this hook's
# business. And an error that leaves conjunct (a) UNKNOWN is not resolved here until
# (b) comes back as anything other than NOT_ARMED — see the docstring: `UNKNOWN AND
# False` is False, and denying an unreadable payload in a session that carries no
# permission denial at all would refuse calls that cannot be self-grants by
# construction. Two things still deny unconditionally, and both are meant to. Stdin that
# is not a JSON object at all: there is not even a transcript path to consult. And any
# exception that escapes `decide()` into `main()`'s catch-all — which is a BUG IN THE
# GATE, not a shape of the call, because every string the gate reads out of the payload
# comes through `_str_field` and every field inside `tool_input` is type-checked where it
# is read. Keeping malformed payloads off that path is what lets the catch-all stay the
# backstop it claims to be rather than a second, unmodelled deny route.
_ON_ERROR = "deny"

_FILE_TOOLS = ("Edit", "Write")
_MODELLED_TOOLS = _FILE_TOOLS + ("Bash",)

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


@dataclass(frozen=True)
class _Unknown:
    """Conjunct (a) could not be evaluated — NOT the same value as "it is not a widening".

    `detail` completes the sentence "the gate could not evaluate this call: ...". A
    caller must route this through (b) first and only then to `_ON_ERROR`; see `decide`.
    """
    detail: str


class _Read(Enum):
    """Why `_read_text` returned no text. The two are never collapsed into one value.

    They demand OPPOSITE behaviour from a deny-by-default gate. ABSENT is an established
    fact about the path — nothing is there, so nothing can be widened, and the call is a
    creation. UNREADABLE is the absence of any fact — the gate could not look. Returning
    one `None` for both made "I could not look" answer "I looked and found nothing", on
    the ALLOW side, which is precisely what this gate must not do. Same three-valued
    shape, and same reason for it, as `lib/denial_arming.Verdict`.
    """
    ABSENT = "absent"
    UNREADABLE = "unreadable"


def _load_json(text: str):
    try:
        return json.loads(text)
    except Exception:
        return None


def _read_text(path: str) -> str | _Read:
    """The file's text, or WHICH of the two no-text cases happened.

    ENOENT alone does not mean ABSENT: a dangling symlink and a path under a
    non-directory both raise it while something IS on the path, so the lexical existence
    check decides. Everything else an OS read can fail with — EACCES, EISDIR, EIO — is
    UNREADABLE by construction.

    `ValueError` is caught alongside `OSError` and is not an afterthought: a path
    carrying an embedded NUL raises it BEFORE the syscall, so it never was an `OSError`.
    Uncaught it would escape the three-valued design entirely and reach `main()`'s
    catch-all, which denies unconditionally — turning an unreadable target into a deny in
    a session with no permission denial at all, the one outcome routing UNKNOWN through
    (b) exists to prevent. `os.path.lexists` returns False on such a path, so it lands on
    UNREADABLE.
    """
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError) as exc:
        if isinstance(exc, (FileNotFoundError, NotADirectoryError)) and not os.path.lexists(path):
            return _Read.ABSENT
        return _Read.UNREADABLE


def _granted_allow(doc) -> list[str]:
    """Every `permissions.allow` entry `doc` grants."""
    perms = doc.get("permissions") if isinstance(doc, dict) else None
    allow = perms.get("allow") if isinstance(perms, dict) else None
    if not isinstance(allow, list):
        return []
    return [e for e in allow if isinstance(e, str)]


def _apply_edit(old_text: str, tool_input: dict) -> str | _Unknown:
    old_string = tool_input.get("old_string")
    new_string = tool_input.get("new_string")
    if not isinstance(old_string, str) or not isinstance(new_string, str):
        return _Unknown(
            "this Edit call carries no old_string/new_string pair of strings, so the text it "
            "would produce — and with it whether the call widens a permission surface — is "
            "unknown rather than known to be no"
        )
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


def _file_tool_widening(tool_name: str, tool_input: dict, cwd: str) -> _Widening | _Unknown | None:
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return _Unknown(
            f"this {tool_name} call carries no file_path string, so which file it would "
            f"write — and with it whether that file is a permission surface — is unknown "
            f"rather than known to be no"
        )
    path = file_path if os.path.isabs(file_path) else os.path.join(cwd, file_path)

    old_text = _read_text(path)
    if old_text is _Read.ABSENT:
        return None  # nothing on disk to widen — a creation, not a widening
    if old_text is _Read.UNREADABLE:
        return _Unknown(
            f"the file it would write, {path}, could not be read, so whether "
            f"this call widens the permission surface already there is unknown rather than "
            f"known to be no"
        )

    if tool_name == "Write":
        new_text = tool_input.get("content")
        if not isinstance(new_text, str):
            return _Unknown(
                "this Write call carries no content string, so the text it would produce — "
                "and with it whether the call widens a permission surface — is unknown "
                "rather than known to be no"
            )
    else:
        new_text = _apply_edit(old_text, tool_input)
        if isinstance(new_text, _Unknown):
            return new_text

    return _widening_between(path, old_text, new_text)


def _is_surface_on_disk(path: str) -> bool | _Unknown:
    """Is `path` a permission surface TODAY — or could the gate not tell?

    ABSENT answers the question (nothing on the path is not a permission surface);
    UNREADABLE does not answer it, and must not be reported as a "no".
    """
    text = _read_text(path)
    if text is _Read.ABSENT:
        return False
    if text is _Read.UNREADABLE:
        return _Unknown(
            f"the file it would write, {path}, could not be read, so whether "
            f"that file is a permission surface is unknown rather than known to be no"
        )
    return permission_surface.is_permission_surface(_load_json(text))


def _bash_widening(tool_input: dict, cwd: str) -> _Widening | _Unknown | None:
    """Conjunct (a) for a Bash call: a widening, UNKNOWN, or None for "writes no surface".

    An untokenizable command MUST stay distinguishable from a clean parse:
    `command_write_targets` reports both as an empty target list, so a caller reading
    the result by truthiness lets one unbalanced quote reach the allow branch (R7 axis
    2). The pre-lex below is over exactly the text the lexer sees — heredoc bodies
    stripped first — so the two agree on what "could not parse" means.

    A definite surface among the targets outranks an unreadable one: the loop keeps
    looking after an UNKNOWN target and only falls back to it if no target settles the
    question, so one unreadable path cannot downgrade a deny into an `_ON_ERROR`.
    """
    command = tool_input.get("command")
    if not isinstance(command, str):
        return _Unknown(
            "this Bash call carries no command string, so whether it writes a permission "
            "surface is unknown rather than known to be no"
        )
    if not command.strip():
        return None

    try:
        shlex.split(shell_tokens.strip_heredoc_bodies(command))
    except Exception:
        return _Unknown(
            "its command text could not be tokenized, so whether it writes a permission "
            "surface is unknown rather than known to be no"
        )

    unknown = None
    for target in bash_write_targets.command_write_targets(command, cwd):
        is_surface = _is_surface_on_disk(target)
        if isinstance(is_surface, _Unknown):
            unknown = unknown or is_surface
        elif is_surface:
            return _Widening(target, (), False)
    return unknown


def _on_internal_error(detail: str) -> str | None:
    if _ON_ERROR != "deny":
        return None
    return (
        f"Refusing this call because the permission-self-grant gate could not evaluate it: "
        f"{detail}. This gate is fail-closed — an error it cannot resolve denies rather than "
        f"allows, because the call it was judging may touch the agent's own permission surface, "
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


def _str_field(payload: dict, key: str, default: str) -> str:
    """A string field of the payload, or `default` if it is absent, empty, OR NOT A STRING.

    The payload is untrusted input, and `payload.get(k) or default` — the idiom this
    replaces — guards absent and empty but silently hands a wrong-typed value onward, where
    it raises `TypeError` deep inside `pathlib` and reaches `main()`'s catch-all. That
    catch-all denies UNCONDITIONALLY: a deny in a session with no permission denial at all,
    which is the outcome routing UNKNOWN through (b) exists to prevent.

    So the rule is uniform rather than per-field: EVERY string the gate reads out of the
    payload comes through here. A crash inside the gate should mean a bug in the gate, not
    a shape of the call — and once every field is modelled, `main()`'s catch-all is the
    backstop for a real bug that it claims to be.
    """
    value = payload.get(key)
    return value if isinstance(value, str) and value else default


def decide(payload: dict) -> str | None:
    """Return a deny reason, or None to allow.

    The three conjuncts in cost order: (a) does this call widen a permission surface,
    (b) is the session armed by a denial that expressed a permission judgement, (c)
    would an entry being granted have covered one of those denied calls.

    (a) is three-valued: an UNKNOWN does not short-circuit either way, it falls through
    to (b), and pays `_ON_ERROR` only if the session turns out to be armed.
    """
    # Tool dispatch FIRST, before any payload field is read: an unmodelled tool is not
    # this gate's business whatever shape its input has, and must never be judged.
    tool_name = _str_field(payload, "tool_name", "")
    if tool_name not in _MODELLED_TOOLS:
        return None

    cwd = _str_field(payload, "cwd", os.getcwd())

    # (a) — cheapest, and the one that lets the vast majority of calls out before the
    # transcript is ever opened.
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        widening = _Unknown(
            f"this {tool_name} call's tool_input is not an object, so nothing about what it "
            f"would write can be read out of it and whether it widens a permission surface "
            f"is unknown rather than known to be no"
        )
    elif tool_name == "Bash":
        widening = _bash_widening(tool_input, cwd)
    else:
        widening = _file_tool_widening(tool_name, tool_input, cwd)
    if widening is None:
        return None

    # (b)
    # A non-string transcript_path lands on "" and so on the already-modelled UNREADABLE
    # verdict below — the honest answer, since a payload that names no readable transcript
    # leaves (b) exactly as unknown as one whose transcript will not open.
    arming = denial_arming.armed(_str_field(payload, "transcript_path", ""))
    if arming.verdict is Verdict.NOT_ARMED:
        # Whatever (a) came to, the conjunction is False: an UNKNOWN widening in a session
        # that carries no permission denial cannot be an answer to one.
        return None
    if arming.verdict is Verdict.UNREADABLE:
        return _on_internal_error(
            "this session's transcript could not be read, so whether a permission denial "
            "preceded this call is unknown rather than known to be no"
        )
    if isinstance(widening, _Unknown):
        # Armed, and (a) unresolved: a self-grant cannot be ruled out. Fail closed.
        return _on_internal_error(widening.detail)

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
