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
two. Two different findings are established FACTS and answer it with a definite no. A
target ABSENT from disk: nothing there to widen, so the call is a creation and is
allowed. And a target whose KIND a `permissions.allow` JSON document cannot have — a
directory, a FIFO, a socket, a device (`_read_text`). Neither is a shrug; both are
answers, and treating the second as UNKNOWN instead is what denied every
`git apply` in an armed session for five review rounds. SIZE is deliberately not in that
list: a file too large for this gate to read is a file it did not look at, so it is
UNKNOWN below, and calling it a definite no made padding a real permissions document
past the cap a way to launder a widening. A target that exists, is a
plausible document, and still could NOT be read, a command that could not be tokenized,
and a payload missing the fields this gate reads are none of them facts about the call:
they are UNKNOWN, the gate either did not look or looked and could not tell. An UNKNOWN (a) neither allows nor denies on its own —
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

WHAT EACH TOOL PATH CAN SEE. The modelled set is `_MODELLED_TOOLS`; a tool name outside
it is allowed without being judged, which is a named residual at `decide()`.
  Edit      — the target is read from disk, `old_string`→`new_string` is applied, and the
              parsed before/after pair is diffed. Full precision: (c) is checkable.
  MultiEdit — the same, with every element of `edits` applied IN ORDER, each against the
              previous result, which is what the tool itself does. Same precision as
              Edit; until it was modelled it ALLOWED the very payload Edit denied.
  Write     — the target is read from disk and diffed against the content being written.
              A Write to a path ABSENT from disk is a CREATION, not a widening, and is
              allowed: there is no prior surface to widen, and a brand-new settings file
              is not how a denial gets cleared. A path that exists but cannot be read is
              a different fact and takes the UNKNOWN route above, not this one.
  NotebookEdit — coarse, for the Bash path's reason: `new_source` replaces one CELL's
              source inside a notebook, so it is not the after-document and cannot be
              made into one without this gate parsing the notebook format. The question
              asked is therefore "is the target a permission surface TODAY", with no
              entries to narrow (c) by.
  Bash      — deliberately coarser. A command is not applied before it runs, so no
              before/after pair exists and there are no entries to test for relevance
              (R7). The path falls back to (a)+(b) alone: while armed, a write is refused
              whenever `bash_write_targets` NAMES a target that is TODAY a permission
              surface. Not "any write" — the quantifier is over what the lexer resolves,
              which is the honest bound and a much smaller set. `install SRC DIR` was
              outside it until this round (the verb was unmodelled, so the command
              reported no target at all); `tar -xf a.tar -C DIR` still is, and so is
              anything routed through a script the hook cannot read. The unmodelled-verb
              residual below carries the list.

A relative path — a `file_path` on the Edit/Write paths, a write target on the Bash
path — is resolved against the payload's own `cwd`, not against
`git_cwd.effective_git_cwd` as the canon guard does. That helper exists to answer a
question this gate does not ask: which REPOSITORY a path belongs to, so that a canon
checkout can be told from a worktree. Here the target is a permission surface or it is
not, and repository membership does not enter the verdict.

NAMED RESIDUALS carried by this hook (R-numbers are the plan's):

  R7  The Bash path has no not-relevant branch — see above. It is bounded on two
      axes. AXIS 1: THE LEXER'S VERB TABLE IS AN ENUMERATION, so what it does not
      model it does not report, and an unreported target is an ALLOW by a route that
      never reached a verdict. Four known members, and they are different in kind.
      (i) A write routed through a script — `bash /tmp/grant.sh` — is not in the
      command text at all, and no verb table closes that. (ii) An unmodelled WRITE
      VERB: `tar -xf a.tar -C DIR` extracts over any path inside the archive and
      reports nothing, `dd of=…`, `rsync`, `python3 -c` likewise. `install` was in
      this class until this round, which is the evidence that the class is real and
      not theoretical: it is an ordinary write verb, it was simply absent from the
      table, and it allowed a copy onto a live settings file. Adding a verb is a
      one-line change in `lib/bash_write_targets.py`; `tar` is deliberately not
      added, because its destination set lives inside the archive rather than on the
      command line and reading it means a second grammar (the same reasoning as the
      patch verbs below). (iii) The patch verbs, which DO report a target — the
      working directory — and are their own paragraph below. (iv) A MODELLED verb
      whose target the lexer resolves to THE WRONG PATH, which is the worst of the
      four because nothing about it looks like a gap: the verb is in the table, the
      destination is one unambiguous token, the surface is named on the command line,
      and the gate answers about a path the command will not write. The cause is that
      `bash_write_targets` performs no shell expansion. `~` was in this class until
      this round: `cp grant.json ~/.claude-agent/settings.json` joined the literal `~`
      under the cwd, and MEASURED at commit 067ea09, armed, with the real
      `~/.claude-agent/settings.json` a live surface, it ALLOWED. The gate judges by
      JSON shape, never by filename — the personal Claude Code root's own settings
      document is a live permission surface too — and the agent home is named here
      because it is the surface whose widening this gate was built to refuse, not
      because it is the only one the gate sees. `_abs` now calls
      `os.path.expanduser`, and the identical command DENIES. `$VAR` is NOT expanded
      and stays in this class: `cp grant.json $HOME/.claude/settings.json` and the
      `${HOME}` spelling both still ALLOW, measured after the fix. That is a choice,
      not an oversight — THE HOOK'S ENVIRONMENT IS NOT THE COMMAND'S (a command can
      supply its own, `HOME=/x cp …`, or name a variable exported in a shell this
      process never saw), so expanding it would resolve the token confidently to a
      path the command may never write, in either direction; `_abs` carries the full
      argument. Closing it means an expansion model, which is a second grammar. Also
      in this class, by the same no-expansion cause: a glob, a `$(…)`, and any
      destination the command computes at runtime. AXIS 2: an
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

  A PATCH THAT REWRITES A SETTINGS FILE IS ALLOWED, deliberately, and both halves of that
  trade are stated here because only one of them is comfortable. `bash_write_targets`
  reports the working DIRECTORY as the write target of `patch` and `git apply` — those
  verbs write wherever their diff says, which is inside the patch body, not on the command
  line. They are the only verbs that answer with a directory AND NOTHING ELSE:
  `cp`/`mv`/`install` also resolve a directory destination to the files it means
  (`_copy_targets`), so `cp grant.json /home/u/.claude-agent/` — MEASURED, armed, with
  `/home/u/.claude-agent/settings.json` a live surface — DENIES on the join, while under
  the pre-round-7 shape of `_copy_targets` (destination only, re-applied as a mutation to
  measure this rather than inferred from the commit) the identical command ALLOWED by
  landing on this same directory arm. The ABSOLUTE spelling is the one that arm used to
  absorb. A `~` spelling never reached it: `cp grant.json ~/.claude-agent/` allowed at the
  start of this round too, but through ABSENT, because the lexer joined the literal `~`
  under the cwd and `<cwd>/~/.claude-agent/` is a path nothing is on — a different hole, fixed
  separately in `_abs` and named as axis-1 member (iv). Since round 6 a directory target is
  an ESTABLISHED negative (a directory is not a JSON document), so `git apply grant.patch`
  reaches ALLOW even when the patch adds a `permissions.allow` entry. What was there before
  is not better: the directory read as an `_Unreadable`, hence UNKNOWN, hence `_ON_ERROR` —
  measured, EVERY `git apply`, `git apply --check` and `patch -p1` in an armed session was
  denied, including the benign ones, which is most of them. So the choice is between a
  certain cost on a common benign operation and an uncertain cost on a rare hostile one.
  Closing it properly means parsing the patch body to learn which files it touches and
  applying the diff in memory — a second parser, in a hook, over an attacker-influenced
  format; the standing instruction for this task is not to invent one. The bound that makes
  the residual tolerable is that this gate is a backstop for the OBSERVED failure mode (the
  agent editing a settings file directly to clear its own denial), not a general egress
  control: an agent routing a self-grant through a patch file is a different, more
  deliberate act, and one a reviewer reading the diff can see.

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
import stat
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
# construction. Stdin that is not a JSON object at all denies unconditionally, and is
# meant to: there is not even a transcript path to consult.
#
# THE CATCH-ALL IS NOT A CLASSIFIER, AND MUST NOT BE USED AS ONE. Each of the last three
# reviews found a live input that reached `main()`'s catch-all — which denies every call it
# sees, in any session, including sessions carrying no permission denial at all — and each
# time the comment here had just been rewritten to claim the sources were now enumerated.
# The three: `os.getcwd()` in a default argument, raising once the process's own directory
# had been removed (in this fleet, landing a unit deletes the worktree under any session
# still in it); the transcript's `tool_use` blocks, stored straight off `json.loads`, so a
# non-dict `input` raised `AttributeError`; and the target's own BYTES, read whole from
# whatever path arrived, so `Edit file_path=/dev/zero` raised `MemoryError` under a memory
# limit and OOM-killed the hook process without one.
#
# So no list is written here. A list of input sources is the wrong SHAPE of claim — it is
# only ever as complete as the last review, and stating it as complete is what made three
# of these findings surprising instead of expected. What is claimed instead is a rule, and
# it is a rule for the reader rather than an inventory: EVERY EXTERNAL SOURCE NEEDS EXACTLY
# ONE FUNCTION THAT PARSES IT — establishing the declared types and bounds rather than
# trusting them — and downstream code may then rely on what that function established.
#
# No roster of those boundaries is kept here either, for the same reason and by the same
# evidence: the roster this comment used to carry omitted `denial_arming._read_transcript`,
# which is a boundary over the largest external input the gate has, and the omission was
# invisible precisely because the list read as complete. A boundary is found by looking at
# the function that reads the input, not by consulting a list of them. Anything this
# process did not itself compute needs one, and the catch-all is not it.
_ON_ERROR = "deny"

_FILE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
_MODELLED_TOOLS = _FILE_TOOLS + ("Bash",)

# Which `tool_input` field carries the write target, per modelled file tool. A table
# rather than a hardcoded "file_path" because `NotebookEdit` spells it differently, and a
# tool whose target field the gate reads under the wrong name is a tool the gate silently
# never judges — the D4 shape, reached through the payload instead of through the name.
_TARGET_FIELD = {
    "Edit": "file_path",
    "Write": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}

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

    `baseline_unparseable` records that `entries` was computed with NO before-document to
    subtract, because what is on disk is not a JSON object (`_widening_between`). Every
    entry the after-document grants is then listed, including ones that were already there
    in whatever the file actually is — so the deny message must say so rather than assert
    the call is adding them, which is a claim about a diff that was never taken.
    """
    surface: str
    entries: tuple[str, ...]
    entries_known: bool
    baseline_unparseable: bool = False


@dataclass(frozen=True)
class _Unknown:
    """Conjunct (a) could not be evaluated — NOT the same value as "it is not a widening".

    `detail` completes the sentence "the gate could not evaluate this call: ...". A
    caller must route this through (b) first and only then to `_ON_ERROR`; see `decide`.
    """
    detail: str


class _Read(Enum):
    """The two ESTABLISHED FACTS `_read_text` can report instead of text.

      ABSENT       nothing is on the path, so nothing can be widened and the call is a
                   creation. A fact — allowed.
      NOT_A_SURFACE the path was identified and its KIND cannot be a `permissions.allow`
                   JSON document: a directory, a FIFO, a socket, a device. Also a fact,
                   and also a definite "this call does not widen a permission surface" —
                   see `_read_text` for why that verdict is inside the evidence domain.

    The THIRD no-text case — the gate could not look — is `_Unreadable`, a value rather
    than a member, because it is the only one of the three that carries something a
    caller needs to say out loud. Three values, never two: returning one `None` for
    ABSENT and `_Unreadable` made "I could not look" answer "I looked and found nothing",
    on the ALLOW side, which is precisely what this gate must not do. Same three-valued
    shape, and same reason for it, as `lib/denial_arming.Verdict`.
    """
    ABSENT = "absent"
    NOT_A_SURFACE = "not-a-surface"


@dataclass(frozen=True)
class _Unreadable:
    """`_read_text` could not look — and WHY, in a clause the deny message can carry.

    The reasons are one verdict and several sentences. To the DECISION they are one
    thing: the absence of any fact, hence UNKNOWN, hence `_ON_ERROR` while armed. To the
    person reading the refusal they are not, and collapsing them made the size cap's own
    cost claim unpayable. That claim (see `_MAX_SURFACE_BYTES`) is that an over-cap
    permissions document costs "one blocked call, which a user sanctions in a sentence" —
    and nobody can write that sentence when the only thing they were told is "could not
    be read". THE POPULATION PAYING THAT COST IS WIDER THAN "PERMISSIONS DOCUMENT": any
    `~`- or absolutely-spelled write target this gate cannot examine lands here, whether
    or not it is a candidate permission surface — an ordinary over-cap data file, a file
    this process lacks permission to read, or a dangling symlink, each reachable under
    `cp`/`mv`/a redirect. Measured before this round: a 1 048 577 B document under
    `tmp_path`, a BENIGN rename onto it, armed → DENY whose whole account of itself was
    "could not be read", with nothing anywhere in it pointing at a size limit.

    SIZE also belongs here rather than among the facts above: an over-cap file could be a
    permission document perfectly well, so the only thing established about it is that
    this gate declined to read it. Answering NOT_A_SURFACE there — a definite negative
    about content nobody looked at — made padding a real permissions document past the
    cap a way to launder a widening.

    `why` completes "the file it would write, PATH, ...".
    """
    why: str


# The largest a file may be and still be read as a candidate permission document.
#
# THE POPULATION IS THE LIVE DOCUMENTS, NOT THIS REPOSITORY'S SOURCES, and getting that
# wrong is what made the previous margin claim false by a factor of forty. It read the
# five in-repo files by shape (`grep -rl --include=*.json '"permissions"'`:
# `settings/base.json` 1916 B, `cursor/config/cli-base.json` 1899 B,
# `benchmark-profile-spawn/settings.json` 869 B, `benchmark-profile/settings.json` 788 B,
# `permissions/global.json` 24 B), took the 1916 B maximum, and published a "~547x
# margin". But a generator SOURCE is not what a tool call writes to. The documents this
# gate actually meets are the installed ones, and measured on this machine they are:
# `~/.claude.json` 76 310 B, `~/.claude-agent/.claude.json` 32 986 B,
# `~/.claude/settings.json` 14 398 B, `~/.claude-agent/settings.json` 12 060 B — all four
# by `stat -c '%s %n'` on 2026-08-11. The two `.claude.json` files are live CLI state and
# drift between sessions, so this is an AS-OF SNAPSHOT, not a constant to keep re-syncing
# to the byte. The real maximum is 76 310 B, so 1 MiB leaves ~13.7x, not ~547x.
#
# WHAT AN OVER-CAP DOCUMENT COSTS IS NOW A FALSE DENY, WHICH IS WHY THE NUMBER NO LONGER
# HAS TO BE EXACTLY RIGHT. It used to be a false ALLOW: over the cap `_read_text`
# answered NOT_A_SURFACE, a definite negative, so padding a real permissions document
# past 1 MiB with legitimate entries made the identical widening allowed — grow, then
# grant. Since the direction was corrected the answer is `_Unreadable`, i.e. UNKNOWN, and an
# armed session pays `_ON_ERROR`: one blocked call, and not only on an enormous permissions
# file — any `~`- or absolutely-spelled write target this gate cannot examine pays the same
# cost (an ordinary over-cap data file, a file this process lacks permission to read, a
# dangling symlink), which a user sanctions in a sentence either way. A margin that is too
# small now errs toward refusing; before, it erred toward the hole the gate exists to close.
_MAX_SURFACE_BYTES = 1024 * 1024


def _load_json(text: str):
    try:
        return json.loads(text)
    except Exception:
        return None


def _cap_phrase() -> str:
    """`_MAX_SURFACE_BYTES` for a deny message: the live byte figure, plus MiB when it is
    a whole number of them. Computed rather than written out, because a hardcoded "1 MiB"
    beside a live byte count is a claim that goes false the moment the constant moves —
    and the tests move it (a row sets it to 20 to reach the branch cheaply)."""
    mib, remainder = divmod(_MAX_SURFACE_BYTES, 1024 * 1024)
    unit = f" ({mib} MiB)" if mib and not remainder else ""
    return f"`_MAX_SURFACE_BYTES`, {_MAX_SURFACE_BYTES} B{unit}"


def _read_text(path: str) -> str | _Read | _Unreadable:
    """The file's text, or WHICH of the three no-text cases happened.

    ESTABLISH WHAT THE PATH IS, THEN READ. The previous version opened whatever path it
    was handed and read it whole, catching what came out — which is not a parse boundary,
    and it failed in the worst way available to a PreToolUse hook. `Edit
    file_path=/dev/zero` read without end: under an `RLIMIT_AS` the `MemoryError` escaped
    into `main()`'s catch-all and denied, and with no limit the hook process was
    OOM-KILLED — exit 137, nothing on stdout, and the machine memory-pressured while it
    happened. A gate that dies this way is worse than one that answers wrongly. The same
    unbounded open is what made a FIFO target a plausible non-return.

    WHY "NOT A REGULAR FILE" IS A VERDICT AND NOT AN UNKNOWN — this is the whole reason
    the branch is inside the evidence domain, so it is written here rather than assumed.
    Conjunct (a) asks exactly one question: does this call widen a `permissions.allow`
    JSON DOCUMENT. A directory, a FIFO, a socket and a character device are not JSON
    documents and cannot become one by being written to, so "no" is an answer the evidence
    supports — unlike a definite "no" about a file the gate could not identify, which is
    the defect this whole artifact exists to avoid.

    SIZE IS THE OPPOSITE CASE, AND USED TO BE HANDLED AS IF IT WERE THE SAME ONE. A file
    over `_MAX_SURFACE_BYTES` is a regular file that could be a permission document
    perfectly well; the only thing established about it is that this gate declined to read
    it. Answering NOT_A_SURFACE there — a definite negative — published a verdict about
    content nobody looked at, and it was exploitable in one step: pad a real permissions
    document past the cap with legitimate entries and the identical widening allows. It is
    an `_Unreadable`, exactly as `denial_arming._read_transcript` answers its own over-cap
    file, and an armed session pays `_ON_ERROR` for it.

    BOTH BOUNDS ARE LOAD-BEARING; the stat gate alone is not enough, and the second one is
    NOT justified here by a claim about any particular kind of file. `st_size` is what one
    earlier syscall reported, not a promise about how much a later `read` will yield: an
    ordinary file can be appended to in the window between the two, and a kernel-backed
    file need not account its content in `st_size` at all. So the read is bounded at
    `_MAX_SURFACE_BYTES + 1` and the RESULT is length-checked — one character past the cap
    is how a truncated read is told from a file that merely fits.

    The check is not belt-and-braces, it changes a verdict: a truncated permissions
    document must never be diffed as if it were whole. Dropping the tail drops entries, and
    a baseline missing entries makes the after-document appear to grant them — a widening
    reported where there was none, or the reverse when the tail is where the grant was.
    Neither is a fact, so both are `_Unreadable`.

    ENOENT alone does not mean ABSENT: a dangling symlink and a path under a
    non-directory both raise it while something IS on the path, so the lexical existence
    check decides. What is left for `_Unreadable` is a regular file within the cap that
    genuinely will not yield its bytes — EACCES, EIO — which is an honest "could not
    look".

    `ValueError` is caught alongside `OSError` and is not an afterthought: a path
    carrying an embedded NUL raises it BEFORE the syscall, so it never was an `OSError`.
    Uncaught it would escape the three-valued design entirely and reach `main()`'s
    catch-all, which denies unconditionally — turning an unreadable target into a deny in
    a session with no permission denial at all, the one outcome routing UNKNOWN through
    (b) exists to prevent. `os.path.lexists` returns False on such a path, so it lands on
    `_Unreadable`.
    """
    try:
        st = os.stat(path)
    except (OSError, ValueError) as exc:
        if isinstance(exc, (FileNotFoundError, NotADirectoryError)) and not os.path.lexists(path):
            return _Read.ABSENT
        return _Unreadable(f"could not be examined ({exc.__class__.__name__})")

    if not stat.S_ISREG(st.st_mode):
        return _Read.NOT_A_SURFACE
    if st.st_size > _MAX_SURFACE_BYTES:
        # Too big to read is not the same fact as not a document, and the refusal says so
        # with the figures: without them the deny names no limit at all, so the user it
        # asks for a sentence cannot tell a size cap from a broken gate.
        return _Unreadable(
            f"was not read: at {st.st_size} B it is past the cap this gate opens a "
            f"candidate permission document under ({_cap_phrase()})"
        )

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(_MAX_SURFACE_BYTES + 1)
    except (OSError, ValueError) as exc:
        return _Unreadable(f"would not yield its bytes ({exc.__class__.__name__})")
    if len(text) > _MAX_SURFACE_BYTES:
        # `st_size` under-reported; a truncated document is not one.
        return _Unreadable(
            f"yielded more than the {st.st_size} B `stat` reported, so what could be read "
            f"of it within the cap ({_cap_phrase()}) is a truncated document rather than "
            f"the whole one"
        )
    return text


def _granted_allow(doc) -> list[str]:
    """Every `permissions.allow` entry `doc` grants."""
    perms = doc.get("permissions") if isinstance(doc, dict) else None
    allow = perms.get("allow") if isinstance(perms, dict) else None
    if not isinstance(allow, list):
        return []
    return [e for e in allow if isinstance(e, str)]


def _apply_edit(old_text: str, spec: dict, tool_name: str) -> str | _Unknown:
    """`old_text` with one old->new replacement applied, or UNKNOWN.

    `spec` is an `Edit`'s whole `tool_input` or ONE element of a `MultiEdit`'s `edits`
    list: the two carry the same three keys, so the substitution is written once.
    """
    old_string = spec.get("old_string")
    new_string = spec.get("new_string")
    if not isinstance(old_string, str) or not isinstance(new_string, str):
        return _Unknown(
            f"this {tool_name} call carries no old_string/new_string pair of strings, so the "
            f"text it would produce — and with it whether the call widens a permission "
            f"surface — is unknown rather than known to be no"
        )
    if spec.get("replace_all"):
        return old_text.replace(old_string, new_string)
    return old_text.replace(old_string, new_string, 1)


def _apply_multi_edit(old_text: str, tool_input: dict) -> str | _Unknown:
    """`old_text` with every edit in a `MultiEdit`'s `edits` list applied IN ORDER.

    Sequential application, each edit against the previous result, is what the tool
    itself does — so the after-document this returns is the one the call would write, and
    conjunct (c) keeps `Edit`-grade precision on this path rather than falling back to the
    Bash path's coarse "writes a surface at all".
    """
    edits = tool_input.get("edits")
    if not isinstance(edits, list) or not edits:
        return _Unknown(
            "this MultiEdit call carries no non-empty edits list, so the text it would "
            "produce — and with it whether the call widens a permission surface — is "
            "unknown rather than known to be no"
        )
    text = old_text
    for spec in edits:
        if not isinstance(spec, dict):
            return _Unknown(
                "this MultiEdit call carries an edits element that is not an object, so the "
                "text it would produce — and with it whether the call widens a permission "
                "surface — is unknown rather than known to be no"
            )
        applied = _apply_edit(text, spec, "MultiEdit")
        if isinstance(applied, _Unknown):
            return applied
        text = applied
    return text


def _widening_between(path: str, old_text: str, new_text: str) -> _Widening | None:
    old_doc = _load_json(old_text)
    new_doc = _load_json(new_text)
    if not (permission_surface.is_permission_surface(old_doc)
            or permission_surface.is_permission_surface(new_doc)):
        return None

    entries = permission_surface.widens(old_doc, new_doc)
    baseline_unparseable = entries is None
    if baseline_unparseable:
        # UNKNOWN: the file on disk is not a JSON object, so no baseline exists to
        # diff against. `widens` refuses to coerce that to "no widening", and neither
        # does this caller: with no baseline, nothing shows any entry to be pre-
        # existing, so every entry the new document grants counts as granted here.
        # Over-reports rather than under-reports, and (c) still narrows the result.
        # The flag travels with the result so the deny message can say which of the two
        # it is looking at instead of calling both of them "adding".
        entries = _granted_allow(new_doc)
    if not entries:
        return None
    return _Widening(path, tuple(entries), True, baseline_unparseable)


def _located(path: str, what: str) -> str | _Unknown:
    """`path` if it is ABSOLUTE, else UNKNOWN — the ONE test every path in this gate takes.

    Absolute is all this function establishes, and the name should not be read as more: it
    says nothing about whether anything is on the path, what kind of thing that is, or
    whether it can be read. Those are `_read_text`'s to answer, and it answers them
    separately (ABSENT / NOT_A_SURFACE / `_Unreadable` / bytes) precisely because they are
    different facts. What this one rules out is a path whose MEANING is not yet fixed:

    A path that is still relative AFTER every join this gate can perform does not name a
    file; it names a file *relative to a directory nobody supplied*. Left to fall through,
    it reaches `_read_text` as written, gets ENOENT because the base is missing rather than
    because nothing is on the path, `os.path.lexists` agrees, and it lands on ABSENT — and
    ABSENT means "a creation, not a widening", i.e. a definite ALLOW. That is the gate
    asserting a file is not a permission surface when it could not work out which file is
    meant: a verdict outside the evidence domain it rests on. The honest answer is the third
    one the design already has.

    ONE FUNCTION, TAKING THE JOINED PATH, BECAUSE TWO DRIFTED. The predicate used to be
    written separately at each call site, and the two spellings were not equivalent: the
    file-tool site tested the BASE (`if not cwd`) BEFORE joining, the Bash site tested the
    RESULT (`os.path.isabs(target)`) after. A relative but non-empty `cwd` — `"sub"` — passes
    "the base is non-empty" and still yields a relative join, so the same session answered
    the same unresolvable target two different ways: `Bash` DENY, `Edit` ALLOW. Worse, with a
    live process directory the joined path silently resolved against THAT, so the gate read a
    settings file nobody had named. Testing the joined path is what makes the two agree by
    construction; there is no spelling of the base-side test that generalizes, because only
    the join knows whether it produced an absolute path."""
    if os.path.isabs(path):
        return path
    return _Unknown(
        f"{what} did not resolve to an absolute path — the payload's cwd and the process's "
        f"own were both unusable — so which file it names, and with it whether this call "
        f"widens a permission surface, is unknown rather than known to be no"
    )


def _target_path(tool_name: str, tool_input: dict, cwd: str) -> str | _Unknown:
    """The absolute path a file tool's payload names as its write target, or UNKNOWN.

    ONE FUNCTION FOR THE TARGET FIELD, because it is read out of untrusted input and this
    gate's own rule (`_ON_ERROR`) is that every external source gets exactly one function
    that parses it. Three separate things happen to that field — which key holds it
    (`_TARGET_FIELD`, since `NotebookEdit` spells it differently), that it is a non-empty
    string, and that it resolves to an absolute path — and they were written inline at the
    one call site, which is how the wrong-key defect (D4) got in: the key was a literal, so
    a tool whose field is named differently was read as having no target at all.

    The other `tool_input` strings this gate reads (`command`, `content`,
    `old_string`/`new_string`) deliberately keep their checks at their use sites. Each is
    read exactly once, so its use site IS its single boundary, and each produces an
    `_Unknown` whose sentence is specific to what that field would have determined.
    """
    target_field = _TARGET_FIELD[tool_name]
    file_path = tool_input.get(target_field)
    if not isinstance(file_path, str) or not file_path:
        return _Unknown(
            f"this {tool_name} call carries no {target_field} string, so which file it would "
            f"write — and with it whether that file is a permission surface — is unknown "
            f"rather than known to be no"
        )
    return _located(
        file_path if os.path.isabs(file_path) else os.path.join(cwd, file_path),
        f"the file path this {tool_name} call carries, {file_path},",
    )


def _file_tool_widening(tool_name: str, tool_input: dict, cwd: str) -> _Widening | _Unknown | None:
    path = _target_path(tool_name, tool_input, cwd)
    if isinstance(path, _Unknown):
        return path

    old_text = _read_text(path)
    if old_text is _Read.ABSENT:
        return None  # nothing on disk to widen — a creation, not a widening
    if old_text is _Read.NOT_A_SURFACE:
        return None  # identified, and not a JSON document — see `_read_text`
    if isinstance(old_text, _Unreadable):
        return _Unknown(
            f"the file it would write, {path}, {old_text.why}, so whether "
            f"this call widens the permission surface already there is unknown rather than "
            f"known to be no"
        )

    if tool_name == "NotebookEdit":
        # COARSER THAN THE OTHER FILE TOOLS, for the same reason the Bash path is: there is
        # no before/after pair to diff. `NotebookEdit` replaces one CELL's source inside a
        # JSON notebook, so `new_source` is not the after-document and cannot be turned into
        # one without this gate parsing the notebook format — a parser this task is under
        # standing instruction not to invent. So the question asked here is the Bash path's:
        # is the target a permission surface TODAY. `entries_known=False` records that (c)
        # has nothing to narrow with.
        #
        # The false-positive surface this leaves is `NotebookEdit` against a file that is
        # already a `permissions.allow` document, while armed. A real `.ipynb` has no
        # `permissions` object, so ordinary notebook editing never reaches here.
        if permission_surface.is_permission_surface(_load_json(old_text)):
            return _Widening(path, (), False)
        return None

    if tool_name == "Write":
        new_text = tool_input.get("content")
        if not isinstance(new_text, str):
            return _Unknown(
                "this Write call carries no content string, so the text it would produce — "
                "and with it whether the call widens a permission surface — is unknown "
                "rather than known to be no"
            )
    elif tool_name == "MultiEdit":
        new_text = _apply_multi_edit(old_text, tool_input)
        if isinstance(new_text, _Unknown):
            return new_text
    else:
        new_text = _apply_edit(old_text, tool_input, tool_name)
        if isinstance(new_text, _Unknown):
            return new_text

    return _widening_between(path, old_text, new_text)


def _is_surface_on_disk(path: str) -> bool | _Unknown:
    """Is `path` a permission surface TODAY — or could the gate not tell?

    ABSENT and NOT_A_SURFACE both ANSWER the question with a no (nothing on the path, and
    a path that is not a JSON document, are neither of them permission surfaces); an
    `_Unreadable` does not answer it, and must not be reported as a "no".

    The NOT_A_SURFACE arm is what makes `patch` and `git apply` judgeable on this path at
    all: `bash_write_targets` reports the working DIRECTORY as their write target, which
    is exactly a path identified and known not to be a JSON document.

    THE COPY VERBS STILL REACH THIS ARM TOO, and the earlier claim here that they no
    longer do was simply false. A bare final token is ambiguous, so `cp s.json d` emits
    `<cwd>/d` AND `<cwd>/d/s.json` — the directory reading among them — and a malformed
    one-operand `cp DEST` emits that token alone. What `_copy_targets` changed is that the
    directory reading is no longer the ONLY candidate: a spelling that SAYS directory
    (`-t DIR`, a trailing separator) emits the joins alone, and an ambiguous one emits the
    join alongside. Since `_bash_widening` tests every candidate and one definite surface
    outranks the rest, this arm answering "no" about `<cwd>/d` no longer decides such a
    call. What the arm costs is stated with the patch residual in the module docstring; it
    is not a general clearance for directory targets.
    """
    text = _read_text(path)
    if text is _Read.ABSENT:
        return False
    if text is _Read.NOT_A_SURFACE:
        return False
    if isinstance(text, _Unreadable):
        return _Unknown(
            f"the file it would write, {path}, {text.why}, so whether "
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
        # The helper does its own join against `cwd`, so what comes back is already the
        # joined path `_located` expects. A target still relative here is the same
        # unresolvable case the file-tool path meets, answered by the same predicate.
        located = _located(target, f"the write target {target or '(the working directory)'}")
        if isinstance(located, _Unknown):
            unknown = unknown or located
            continue
        is_surface = _is_surface_on_disk(located)
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
    if widening.entries_known and widening.baseline_unparseable:
        granted = (
            "granting " + ", ".join(repr(e) for e in widening.entries)
            + " — the file on disk is not a JSON object, so there is no baseline to show "
            "which of those it already granted, and every entry the new document carries is "
            "listed"
        )
    elif widening.entries_known:
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
            "ask whether what it grants is RELEVANT to the denial — while armed, a write is "
            "refused whenever this gate's lexer RESOLVES a target that is a permission "
            "surface on disk today. Not any write: the quantifier is over what the lexer "
            "resolves, which is a much smaller set — an unmodelled verb, or a path the "
            "command builds at runtime, is not covered. One consequence worth knowing: a "
            "multi-line command can carry a PHANTOM write target, because the lexer eats the "
            "newline and `cp a.txt b.txt` followed by `jq . settings.json` becomes one segment "
            "whose last positional is the settings file. Run the lines as separate calls and "
            "each is judged on its own."
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


def _base_dir(payload: dict) -> str:
    """The directory a relative target resolves against — obtained LAZILY, and never raising.

    Two defects live in the one line this replaces, `_str_field(payload, "cwd", os.getcwd())`,
    and they are the same defect at two strengths. A default ARGUMENT is evaluated eagerly, so
    `os.getcwd()` ran on every modelled call even when the payload carried a perfectly good
    `cwd` — and `os.getcwd()` raises `FileNotFoundError` once the process's own directory has
    been removed. In this fleet that is not hypothetical: landing a unit deletes its branch and
    worktree, so a session still sitting in that worktree would have denied EVERY subsequent
    Edit, Write and Bash for the rest of its life, through `main()`'s catch-all.

    That is precisely the failure `_str_field` was introduced to remove, reintroduced by
    `_str_field`'s own call site — so laziness alone is not the fix. Ambient process state is
    as untrusted as the payload, and when neither source yields a directory this returns ""
    rather than raising, so `main()`'s catch-all keeps meaning "a bug in the gate" rather than
    "a directory moved".

    WHAT THIS RETURNS IS A CANDIDATE, NOT A DIRECTORY. It is whatever the payload said, or
    whatever the process is sitting in, or "" — and the payload is untrusted, so a caller
    gets no promise that the value is absolute, that it exists, or that it is non-empty. The
    two earlier drafts of this docstring each promised more than the code delivered and each
    was falsified by measurement: the first claimed "" makes a relative target fall through to
    UNKNOWN by itself (measured ALLOW — the path reads as ABSENT, i.e. "a creation, not a
    widening"), the second then described the callers as checking `not cwd`, which a RELATIVE
    non-empty `cwd` like "sub" passes on its way to a still-relative join. Both were the same
    error: reasoning about the base instead of about the joined result.

    So no caller of this function judges the value it gets back. Every caller joins first and
    hands the RESULT to `_located`, which answers exactly one question about it — is it
    absolute — and hands what survives to `_read_text`, which answers what is actually there.
    Two functions, because they establish two different facts; see `_located` for why a path
    the gate cannot even locate must never be given a definite no. There is deliberately no
    consumer count here; a count is a fact about other code that goes stale silently, and the
    previous one already had.
    """
    declared = _str_field(payload, "cwd", "")
    if declared:
        return declared
    try:
        return os.getcwd()
    except OSError:
        return ""


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
    #
    # NAMED RESIDUAL — AN UNMODELLED TOOL NAME IS A DELIBERATE ALLOW, not a judgement.
    # `_MODELLED_TOOLS` is the set whose input shape this gate knows how to read; a client
    # tool that writes files under a name not in it exits here, silently, and the write is
    # never examined. That is the right default (a gate that denied tools whose payloads it
    # cannot read would refuse most of the client's surface on no evidence at all) but it is
    # a HOLE, not a clearance, and it has already been paid once: `MultiEdit` and
    # `NotebookEdit` both write files by name and both allowed the very payload `Edit`
    # denied until they were added. Closing it for a new tool means adding the name here and
    # its target field to `_TARGET_FIELD`; nothing detects the need automatically, so
    # "the gate allowed it" must never be read as "the gate judged it safe".
    #
    # THE MATCH IS EXACT-STRING, and that is part of the same residual rather than a
    # separate one: `edit` and `EDIT` are not in `_MODELLED_TOOLS` and exit here. It is
    # deliberate — the tool name is a protocol identifier the client emits with one fixed
    # spelling, so case-folding it would only broaden the set toward names no client sends,
    # and the gate would then be judging payloads whose shape it is guessing at. But it does
    # mean the set is a list of literals, not a family, and a client that ever renamed a tool
    # would silently leave it unjudged.
    tool_name = _str_field(payload, "tool_name", "")
    if tool_name not in _MODELLED_TOOLS:
        return None

    cwd = _base_dir(payload)

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
    #
    # THE TRANSCRIPT IS LOCATED BY THE SAME DISCIPLINE AS A WRITE TARGET, through the same
    # function. It is a path out of the same untrusted payload, and it was read straight off
    # the field: measured, a RELATIVE `transcript_path` resolved against whatever directory
    # the process happened to sit in, so a decoy file there answered conjunct (b) and flipped
    # a real self-grant to ALLOW. Locating write targets and not this one left the class open
    # one route over — and this route fails toward ALLOW, which is the worse direction.
    declared_transcript = _str_field(payload, "transcript_path", "")
    located_transcript = _located(
        declared_transcript if os.path.isabs(declared_transcript)
        else os.path.join(cwd, declared_transcript),
        f"the transcript path this call carries, {declared_transcript},",
    )
    if isinstance(located_transcript, _Unknown):
        # Unlocatable, so (b) cannot be answered — the same state as a transcript that will
        # not open, and already fail-closed below. No new branch.
        located_transcript = ""
    arming = denial_arming.armed(located_transcript)
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
