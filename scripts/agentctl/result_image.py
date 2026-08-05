"""Does a stage's expected_result_image restate the stage's own check?

The defect: a plan declares two different things per stage -- an image of the
RESULT (what exists, or holds, once the stage is done) and a CONTROL (the check
that decides whether it does) -- and the first collapses into the second. The
image then answers "did the check come out well?" instead of naming the state of
the world the check was evidence for, and a reader who has not run the check
learns nothing from it.

Deciding that collapse is a judgement about MEANING, not about shape: an image
may name its check and still be genuine (sometimes the thing the stage produces
IS a check), and a long, specific restatement of a verdict is still a verdict.
So this module deliberately does NOT decide. It supplies the two halves of the
"separate rule from perception" split (CLAUDE.md; memory leaf
regex-not-for-semantic-classification):

* ``echo_prefilter`` -- a pure, structural, high-recall test that keeps the model
  off the images that plainly cannot be echoes. It is calibrated against an
  independently labelled corpus (scripts/tests/fixtures/result_image_echo_labels
  .json, labelled and committed before this file existed) at a recall floor of
  0.70 and a false-positive ceiling of 0.25, both fixed in advance. Its
  conditions are properties an image OUTSIDE that corpus could equally have --
  never literal strings lifted from the labelled echoes.
* ``judge_echo`` -- the model judgment that actually decides, fail-open in the
  ``advisor.judge_binary_ask`` mould: disabled, no runner, a non-zero exit, an
  unparseable answer or any exception all yield False. False here means "say
  nothing", and saying nothing is safe because the verdict this feeds is a
  WARNING attached to a submission that proceeds -- an echo never refuses a plan.

Nothing here runs a subprocess except ``judge_echo``, and ``judge_echo`` is
reached only from the submission seam. The plan loader stays pure.
"""

from __future__ import annotations

import re

from .text_shape import normalize_string

# The judge model and its budget. Same tier as advisor.judge_binary_ask: this is
# a one-bit classification over a couple of sentences, and it sits in front of a
# warning, so it may never be worth a slow turn.
_JUDGE_MODEL = "haiku"
_JUDGE_TIMEOUT_S = 8

# Clause boundaries. The prefilter reads an image as a sequence of clauses rather
# than as one string, because the defect is per-clause: an image is an echo when
# NO clause survives deleting the verdict, and a whole-string test cannot see
# that a second clause said something about the world. The set is punctuation the
# repo's authors actually use to join a verdict to a result -- sentence end,
# semicolon, and a comma-joined or capitalised conjunction.
_CLAUSE_BOUNDARY = re.compile(r"(?:\.\s+|;\s*|\n+|,\s+and\s+|,\s+then\s+|\s+AND\s+)")

# Check-outcome vocabulary: the ways a clause can report how a check CAME OUT, as
# opposed to what is true because of it. Word-bounded so "exists" is not "exit"
# and "compass" is not "pass". A clause carrying one of these says something
# about a verdict; a clause carrying none says something else, and that is what
# rescues an image from being verdict-saturated.
_VERDICT_MARKER = re.compile(
    r"\b(?:"
    r"exit(?:s|ed|ing)?"
    r"|pass(?:es|ed|ing)?"
    r"|fail(?:s|ed|ing|ure)?"
    r"|succeed(?:s|ed)?"
    r"|green|red"
    r"|assert(?:s|ed|ing|ion|ions)?"
    r"|cover(?:s|ed)?"
    r"|returncode"
    r")\b",
    re.IGNORECASE,
)

# What makes the content of a code span an INVOCATION rather than an identifier
# or a snippet: a program token followed by at least one argument, where the
# argument is a flag, a path, or a subcommand-shaped word. Deliberately no
# allowlist of interpreter names -- an allowlist would only recognise the tools
# this repository happens to use today, and the property being tested ("the image
# opens by naming a command to run") is not about which command.
_PROGRAM_TOKEN = re.compile(r"^[\w.\-/]+$")
_ARGUMENT_TOKEN = re.compile(r"^(?:-{1,2}[\w-]+|[\w.\-/]+)$")

# Below this, a run of the verify_command is too short to be evidence the image
# quoted the check rather than merely sharing a word with it.
_MIN_QUOTED_COMMAND_CHARS = 12


def _clauses(image: str) -> list[str]:
    """Split an image into the clauses the prefilter scores independently."""
    return [c.strip() for c in _CLAUSE_BOUNDARY.split(image) if c and c.strip()]


def _is_invocation(span: str) -> bool:
    tokens = span.split()
    if len(tokens) < 2:
        return False
    if not _PROGRAM_TOKEN.match(tokens[0]):
        return False
    return any(_ARGUMENT_TOKEN.match(t) for t in tokens[1:])


def _leading_command(image: str) -> str:
    """The command the image opens with, or "" if it does not open with one.

    The image's very first thing is a code span holding a command line: the
    author began by naming the check rather than by naming the result. A leading
    code span that holds an identifier, a filename or a value is not this -- an
    image may legitimately open by naming the symbol it created.
    """
    head = (image or "").lstrip()
    if not head.startswith("`"):
        return ""
    end = head.find("`", 1)
    if end <= 1:
        return ""
    span = head[1:end].strip()
    return span if _is_invocation(span) else ""


def _quoted_command(image: str, verify_command: str) -> str:
    """The stage's own verify_command as it appears inside the image, or "".

    The degenerate case: an image that contains the command it is judged by has
    stopped describing a result and started transcribing a control. Compared on
    normalized text so backticks, casing and line wrapping do not hide it, and
    only for commands long enough that co-occurrence is not a coincidence.
    """
    command = normalize_string(verify_command).strip("`")
    if len(command) < _MIN_QUOTED_COMMAND_CHARS:
        return ""
    if command in normalize_string(image):
        return verify_command.strip()
    return ""


def echo_prefilter(image: str, *, verify_command: str = "") -> tuple[str, ...]:
    """Structural reasons to suspect ``image`` restates its own check; () if none.

    High recall by construction and by calibration -- a returned reason means
    "worth a model's attention", never "this is an echo". Each string is phrased
    so it can be shown to the plan's author as WHAT the image restates.

    Pure: no I/O, no subprocess. The submission seam calls it on every stage of
    every incoming plan.
    """
    if not isinstance(image, str) or not image.strip():
        return ()

    reasons: list[str] = []

    command = _leading_command(image)
    if command:
        reasons.append(f"it opens with the command `{command}` rather than with the result")

    quoted = _quoted_command(image, verify_command or "")
    if quoted:
        reasons.append(f"it contains the stage's own verify_command `{quoted}`")

    clauses = _clauses(image)
    if clauses and all(_VERDICT_MARKER.search(c) for c in clauses):
        reasons.append("every clause reports how a check came out, none names a state of the world")

    return tuple(reasons)


_ECHO_JUDGE_PROMPT = (
    "A plan stage declares two separate things: an IMAGE OF THE RESULT (what "
    "exists, or is true, or has changed once the stage is done) and a CONTROL "
    "(the check that decides whether it did).\n\n"
    "Decide whether the IMAGE below has collapsed into the CONTROL -- whether it "
    "reports the verdict of the stage's own check in place of describing the "
    "result that check was evidence for.\n\n"
    "Answer ECHO when a reader who has not run the check learns only that "
    "something succeeded, not what now exists.\n"
    "Answer GENUINE when the image names a state of the world. An image is NOT "
    "an echo merely because it mentions a check, a test or a command -- "
    "sometimes the thing the stage produces IS a check. Where the image does "
    "both, delete the verdict clause: if what remains still says what the result "
    "is, answer GENUINE.\n\n"
    "Answer on the FIRST line with exactly ECHO or GENUINE, nothing else.\n\n"
    "CONTROL: {control}\n\nIMAGE:\n{image}"
)

_ECHO = "ECHO"


def judge_echo(
    image: str,
    runner,
    *,
    verify_command: str = "",
    done_criterion: str = "",
    enabled: bool = True,
    timeout: int = _JUDGE_TIMEOUT_S,
) -> bool:
    """Does the model read ``image`` as restating its own check?

    Fail-open in every direction: disabled, no runner, a non-zero exit, an empty
    or unparseable answer, or any exception returns False -- no warning at all,
    submission unaffected. A fabricated True would put words in an author's plan
    that no judge said.
    """
    if not enabled or not isinstance(image, str) or not image.strip():
        return False
    if runner is None:
        return False
    try:
        control = (verify_command or done_criterion or "(none declared)").strip()
        prompt = _ECHO_JUDGE_PROMPT.format(control=control, image=image)
        result = runner(["claude", "-p", "--model", _JUDGE_MODEL, prompt], timeout=timeout)
        if result.returncode != 0:
            return False
        lines = [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
        if not lines:
            return False
        return lines[0].upper().startswith(_ECHO)
    except Exception:
        return False
