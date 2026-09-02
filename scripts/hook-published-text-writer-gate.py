#!/usr/bin/env python3
"""PreToolUse hook (matcher: Bash): deny a publication call whose text body
has no preceding tech-writer witness bound to it in the session transcript.

Difficulty removed: reader-facing text (a ticket/PR/issue comment or body)
has repeatedly reached a tracker unpolished, because nothing gated the
PUBLISH step on the FACT of a tech-writer pass -- only on a coordinator's own
attestation, or on nothing at all. This hook gates on a structural,
non-attestable observable instead: `lib.writer_pass.bind` computes whether
the exact bytes about to be published are BOUND to a harness-recorded
tech-writer invocation (a `Skill`/`Agent`/`Bash` witness entry, per
`writer_pass._witness_shape`) earlier in the same transcript -- either as
that witness's own tool_result (WRITER_OUTPUT) or as a Write/Edit composed
after it (POST_WITNESS). Neither strength can be forged by anything the
gated process puts in its own tool call.

TWO PATHS, one classifier, one structural check:

  TEXT (a comment/PR/issue body resolved to literal bytes by
  `lib.published_body.resolve`): gated STRUCTURALLY via
  `writer_pass.bind` -- no model judgment anywhere on this path. A binding of
  NONE_STRENGTH or NO_WITNESS_IN_WINDOW denies; WRITER_OUTPUT or POST_WITNESS
  allows. Both denying strengths read identically to a caller (see
  `writer_pass`'s own docstring) -- the split exists only so the deny message
  can name which is true: NO_WITNESS_IN_WINDOW is an honestly-uncertain deny
  (the scanned tail was truncated, so an earlier witness cannot be ruled
  out), never a claim the transcript is witnessless.

  ATTACHMENT (a file about to be uploaded rather than posted as the body):
  the ONE place this gate uses a model judgment, because "is this genuinely
  a raw artifact, or reader-facing prose smuggled past the TEXT gate as a
  file" is a content-shape question no structural rule answers. The content
  is first sniffed and PARSED as its own declared kind (JSON via
  `json.loads`, a unified diff via a three-line structural anchor, or a
  TOML/plan-render shape via `published_body.artifact_syntax_hint`) --
  never a superficial keyword match -- and only an unrecognized shape reaches
  `agentctl.advisor.judge_published_attachment`. That judge ships UNMEASURED
  (`lib/judge_latency.py`, n=0) and fail-open by design, matching every other
  semantic judge in this repo: killswitch, no-text, no-runner, budget
  exhaustion, timeout, and a malformed/non-zero/exception call all ALLOW,
  never deny, and each is recorded to the advisory sink below so a high
  fail-open rate stays visible.

DENY on the inline-literal TEXT shape (shape 2 -- the body typed directly as
a Bash argument, never composed via a Write/Edit event) carries a message
naming BOTH usable remedies -- spawn/invoke tech-writer and rewrite the
result to a file, publishing via --body-file/-F -- and is explicit that
running the tech-writer Skill inline does NOT by itself clear this gate:
only a body that is byte-identical to that Skill call's own returned text
(WRITER_OUTPUT) or to a file composed after it (POST_WITNESS) binds: an
inline pass whose output never lands as either is functionally unwitnessed
to this gate.

FAIL-OPEN DISCIPLINE. Every genuinely missing observable allows and, where a
sink exists to say so, records to it: no transcript_path in the payload, an
unreadable transcript (writer_pass.UNREADABLE), an unresolvable body
(`published_body.resolve` returns UNRESOLVED and records this itself), an
unreadable attachment, and every fail-open branch of the attachment judge. A
DENIED TEXT body also has one deliberate, human-operated escape:
`CLAUDE_PUBLISHED_TEXT_GATE=0` (see `_TEXT_GATE_OVERRIDE_ENV`) force-allows
it, is named in the deny reason itself so a legitimately-blocked caller can
find it without reading this file, and is recorded to the advisory sink
(TEXT_GATE_OVERRIDE_USED) every time it fires -- an unusually high rate of
that kind is the signal the escape is being routed around routinely rather
than used for a genuine emergency.

NOT_A_PUBLICATION records nothing and is not itself a fail-open case -- it is
the ordinary "this call is not gated at all" outcome, the overwhelming
majority of Bash calls this hook ever sees, and it returns in well under
2 seconds: `published_body.is_publication`/`resolve` are pure Python (shlex
tokenizing plus a handful of dict lookups, no subprocess, no model call), and
this hook imports `writer_pass`/`agentctl.advisor`/`judge_budget`/
`judge_latency` LAZILY -- only once resolution.kind is actually TEXT or
ATTACHMENT -- so a NOT_A_PUBLICATION command (the common case on every other
Bash call in a session) never pays for any of those imports.

ALLOW-PATH ARTIFACT HINT. When a TEXT body binds (WRITER_OUTPUT/POST_WITNESS)
but still carries raw artifact syntax (`published_body.artifact_syntax_hint`
-- a TOML `[section]` header, plan-render `**Field:**` labels, or several
`key = value` lines), that is recorded to the advisory sink as a soft
observation -- it never changes the ALLOW decision. There is no precedent
anywhere in this repo's hooks for an informational, non-blocking
"permissionDecision" JSON on an allow path (every hook here prints nothing
at all on allow), so "emits" here means "produces an advisory record", not
"prints to stdout".

NAMED RESIDUAL (the sixth): `published_body.resolve` recognizes an `mcp_tool`
seam entry for a non-Bash `tool_name`, but that call only ever resolves to
NOT_A_PUBLICATION or UNRESOLVED -- never TEXT or ATTACHMENT -- so an MCP-tool
publication can never be structurally bound or denied by this hook, only
advised. This hook is also registered on a Bash-only PreToolUse matcher (see
install-reminder-hooks.sh), so an MCP-tool publication is doubly unreachable
in production today; the mcp_tool branch exists for a future matcher
widening, not for anything this registration currently exercises.

DENY is signaled with the PreToolUse permissionDecision JSON on stdout:
  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
   "permissionDecision": "deny", "permissionDecisionReason": "..."}}

Always exits 0 -- a hook crash must never wedge the workflow.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import judge_ledger  # noqa: E402

# judge_ledger must import cleanly above for this to record anything -- see
# hook-plan-delivery-gate.py's identical comment on the same pattern.
try:
    from lib import config_root  # noqa: E402
    from lib import published_body  # noqa: E402
except BaseException as exc:
    judge_ledger.import_failed("published_text_writer", f"{type(exc).__name__}: {exc}")
    raise

# This hook's own whole-invocation judge budget, owned here rather than read
# off advisor's own default -- mirrors every sibling judge-calling hook
# (_APPROVAL_ASK_JUDGE_BUDGET_S etc.). 45 >= judge_latency.LAST_RESORT_
# CEILING_S (41) + judge_latency.SIZE_HEADROOM_S (1): the published_attachment
# judge is UNMEASURED (n=0, lib/judge_latency.py), so there is no per-judge
# p90 floor to size a tighter budget against -- the last-resort ceiling
# (the worst latency observed on ANY judge on this model) is what this
# budget must clear instead. hook_wiring.TIMEOUT_REQUIREMENTS records this
# same 45 under this hook's own constant name.
_PUBLISHED_TEXT_JUDGE_BUDGET_S = 45

# Safe-by-default kill-switch for the attachment judge only, matching every
# other semantic judge's env convention (CLAUDE_<JUDGE>_SEMANTIC).
_PUBLISHED_ATTACHMENT_KILLSWITCH_ENV = "CLAUDE_PUBLISHED_ATTACHMENT_SEMANTIC"

# Structural (non-judge) escape for the TEXT path, mirroring
# agentctl.gates.plan_presentation_active's AGENTCTL_PLAN_PRESENTATION=0 —
# another MANDATORY structural gate with an env-only, human-operated force-off.
# "0" force-disables the TEXT-path deny; there is deliberately no "1 forces
# on" half, since this gate is mandatory by default and has nothing to force
# on. Named without "_SEMANTIC" (unlike _PUBLISHED_ATTACHMENT_KILLSWITCH_ENV
# above) because that suffix is reserved for judge-calling paths in this
# repo's convention, and the TEXT path calls no model at all. Named in the
# deny reason itself so a legitimately-blocked caller can see the escape
# hatch without reading this file; every use is recorded to the advisory
# sink (TEXT_GATE_OVERRIDE_USED), the same way every other fail-open path is.
_TEXT_GATE_OVERRIDE_ENV = "CLAUDE_PUBLISHED_TEXT_GATE"

# Leading slice of an attachment's content handed to the judge prompt -- large
# enough to judge tone/register, small enough to keep the prompt cheap.
_JUDGE_EXCERPT_CHARS = 4000

# Bound on how much of an attachment this hook reads at all, TEXT and
# ATTACHMENT alike being untrusted-size file reads off the working tree.
_ATTACHMENT_READ_BYTES = 200_000

# A unified diff's three-line structural anchor (---/+++ file headers
# immediately followed by an @@ hunk header) -- a real structural check, not
# a single-keyword match, so a prose file that merely mentions "@@" once
# does not get misclassified as a genuine artifact.
_UNIFIED_DIFF_RE = re.compile(r"^--- .+\n\+\+\+ .+\n@@ .+@@", re.MULTILINE)

_INLINE_LITERAL_REASON = (
    "the published body is an inline literal in the Bash command, with no "
    "tech-writer witness bound to it{window_note} -- running the tech-writer "
    "Skill inline does NOT by itself clear this gate unless these exact "
    "bytes are that pass's own returned text; spawn (or invoke) tech-writer, "
    "then Write its output to a file and publish via --body-file/-F instead "
    "of an inline literal (structural override, human use only: "
    "{override_env}=0)"
)
_GENERIC_DENY_REASON = (
    "no tech-writer witness is bound to this published body{window_note} -- "
    "spawn (or invoke) tech-writer, then either use its own returned text "
    "verbatim or Write its output to a file before publishing "
    "(structural override, human use only: {override_env}=0)"
)
_ATTACHMENT_DENY_REASON = (
    "this attachment reads as reader-facing prose rather than a genuine "
    "artifact (log/diff/structured data) -- either compose it via a "
    "tech-writer pass and publish it as the comment/PR body TEXT instead of "
    "an attachment, or, if it genuinely is a raw artifact, reshape it so its "
    "content actually parses as the kind its name declares"
)


def _window_note(strength: str) -> str:
    from lib import writer_pass  # lazy: only reached once resolution.kind == TEXT

    if strength == writer_pass.NO_WITNESS_IN_WINDOW:
        return (
            " (the scanned transcript tail was truncated, so an earlier "
            "witness cannot be ruled out -- this is an honestly-uncertain "
            "deny, not a claim the transcript is witnessless)"
        )
    return ""


def _text_deny_reason(shape: int | None, strength: str) -> str:
    template = _INLINE_LITERAL_REASON if shape == 2 else _GENERIC_DENY_REASON
    return template.format(window_note=_window_note(strength), override_env=_TEXT_GATE_OVERRIDE_ENV)


def _load_seam() -> list | None:
    try:
        raw = config_root.publication_tools_file().read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, list) else None


def _read_attachment(path: str | None) -> str | None:
    if not path:
        return None
    try:
        with open(path, "rb") as fh:
            data = fh.read(_ATTACHMENT_READ_BYTES)
    except OSError:
        return None
    if not data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def _recognized_artifact_kind(content: str) -> str | None:
    """Sniff `content`'s own syntactic shape and confirm it actually PARSES
    as that shape -- never a superficial keyword match. Returns the kind name
    when recognized (excluded from the judge), or None (stays ambiguous, the
    judge decides)."""
    stripped = content.strip()
    if stripped[:1] in "{[":
        try:
            json.loads(stripped)
            return "json"
        except ValueError:
            pass
    if _UNIFIED_DIFF_RE.search(content):
        return "unified diff"
    if published_body.artifact_syntax_hint(content):
        return "toml/plan-render"
    return None


def _decide_text(resolution: "published_body.Resolution", command: str, payload: dict) -> tuple[str, str]:
    from lib import writer_pass  # lazy: only needed once resolution.kind == TEXT

    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        published_body.record_advisory("NO_TRANSCRIPT", resolution.shape, command)
        return "allow", ""

    binding = writer_pass.bind(resolution.body or "", transcript_path)

    if binding.strength == writer_pass.UNREADABLE:
        published_body.record_advisory("TRANSCRIPT_UNREADABLE", resolution.shape, command)
        return "allow", ""

    if binding.strength in (writer_pass.WRITER_OUTPUT, writer_pass.POST_WITNESS):
        if published_body.artifact_syntax_hint(resolution.body or ""):
            published_body.record_advisory("ALLOWED_WITH_ARTIFACT_HINT", resolution.shape, command)
        return "allow", ""

    if os.environ.get(_TEXT_GATE_OVERRIDE_ENV) == "0":
        published_body.record_advisory("TEXT_GATE_OVERRIDE_USED", resolution.shape, command)
        return "allow", ""

    return "deny", _text_deny_reason(resolution.shape, binding.strength)


def _decide_attachment(resolution: "published_body.Resolution", command: str) -> tuple[str, str]:
    content = _read_attachment(resolution.path)
    if not content:
        published_body.record_advisory("ATTACHMENT_UNREADABLE", resolution.shape, command)
        return "allow", ""

    kind = _recognized_artifact_kind(content)
    prefilter_fired = kind is None
    judge_ledger.entered("published_attachment", prefilter_fired=prefilter_fired)
    if not prefilter_fired:
        return "allow", ""  # a genuine artifact, parsed as its own declared kind -- no judge needed

    from agentctl import advisor as _advisor  # lazy: only reached for an ambiguous attachment
    from lib import judge_budget  # noqa: E402
    from lib import judge_latency  # noqa: E402

    budget = judge_budget.JudgeBudget(
        _PUBLISHED_TEXT_JUDGE_BUDGET_S, judge_latency.LAST_RESORT_CEILING_S, clock=time.monotonic
    )
    remaining_before_call, call_timeout = budget.remaining_and_timeout(_PUBLISHED_TEXT_JUDGE_BUDGET_S)
    if call_timeout is None:
        judge_ledger.decided(
            "published_attachment", stage="budget", verdict=False,
            reason="budget exhausted before call (fail-open)",
            remaining=remaining_before_call, threshold=None,
            ceiling=_PUBLISHED_TEXT_JUDGE_BUDGET_S,
        )
        published_body.record_advisory("ATTACHMENT_JUDGE_BUDGET_EXHAUSTED", resolution.shape, command)
        return "allow", ""

    name = os.path.basename(resolution.path or "")
    excerpt = content[:_JUDGE_EXCERPT_CHARS]
    is_prose, reason = _advisor.judge_published_attachment(
        name, excerpt, _advisor.subprocess_runner,
        enabled=os.environ.get(_PUBLISHED_ATTACHMENT_KILLSWITCH_ENV) != "0",
        timeout=call_timeout, remaining=remaining_before_call,
        ceiling=_PUBLISHED_TEXT_JUDGE_BUDGET_S,
    )
    if is_prose:
        return "deny", _ATTACHMENT_DENY_REASON
    if reason:
        # Non-empty reason on a False verdict is the fail-open signature
        # (see agentctl.advisor's three-valued judge contract) -- an honest
        # NO carries reason == "".
        published_body.record_advisory("ATTACHMENT_JUDGE_FAIL_OPEN", resolution.shape, command)
    return "allow", ""


def decide(payload: dict) -> tuple[str, str]:
    """Returns (decision, reason). NOT_A_PUBLICATION and UNRESOLVED short-
    circuit before either lazy-import group ever runs -- see the module
    docstring's cost-bound and fail-open paragraphs."""
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    cwd = payload.get("cwd") or os.getcwd()
    command = tool_input.get("command") if tool_name == "Bash" else ""
    command = command if isinstance(command, str) else ""

    resolution = published_body.resolve(tool_name, tool_input, cwd, seam=_load_seam())

    if resolution.kind == published_body.NOT_A_PUBLICATION:
        return "allow", ""
    if resolution.kind == published_body.UNRESOLVED:
        # published_body.resolve() has already recorded this outcome to the
        # advisory sink -- a missing observable, fail open, nothing further.
        return "allow", ""
    if resolution.kind == published_body.TEXT:
        return _decide_text(resolution, command, payload)
    if resolution.kind == published_body.ATTACHMENT:
        return _decide_attachment(resolution, command)
    return "allow", ""  # unreachable: every Resolution.kind is one of the four above


def deny_with(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def main() -> int:
    judge_ledger.hook_start("published_text_writer")
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0
    judge_ledger.source_from_payload(payload)

    try:
        decision, reason = decide(payload)
        has_directive = decision == "deny"
        judge_ledger.final(has_directive=has_directive)
        emit_ok = True
        try:
            if has_directive:
                deny_with(reason)
        except Exception:
            emit_ok = False
        judge_ledger.emitted(ok=emit_ok, had_directive=has_directive)
    except Exception as exc:
        judge_ledger.discarded(reason=repr(exc))
        return 0  # fail-open -- a hook must never wedge a Bash call
    return 0


if __name__ == "__main__":
    sys.exit(main())
