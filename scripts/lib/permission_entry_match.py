"""Would a permission-surface entry have COVERED a given tool call?

Difficulty removed: the self-grant gate needs a second structural condition
beyond "a denial happened this session" (`toolDenialKind` cannot separate a
harness-allowlist refusal from one of this repo's own hook denies, and the
field that would -- `decisionReason` -- is discarded and absent from the
transcript record). This module answers a different, answerable question
instead: given a permission-entry specifier and a `(tool_name, tool_input)`
call, had that entry been present at the time, would it have permitted that
exact call? A caller joins this to an armed denial to decide relevance; this
module knows nothing about denials, transcripts, or which file is a settings
file.

FAIL-TOWARD-COVERING IS THE CONTRACT, NOT A DETAIL. This is a narrowing
primitive layered over a deny-by-default gate: on any entry this module
cannot parse, any tool it does not model an operand for, or any call missing
the operand field it expects, `covers()` resolves to `True`. Failing toward
"not relevant" would silently re-open the hole the gate exists to close.

THE RAW-STRING SEPARATOR RULE (Bash only). `covers()` reaches Bash segments
through `split_segments(shlex.split(command))`, and `shlex.split` destroys
separator information before the segmenter ever sees a token: it eats a
newline as ordinary whitespace, and it does not detach a separator glued to
the preceding word. On every affected spelling -- a glued or spaced `;`, a
glued `&&`, a glued `|`, any newline -- the lexer silently returns ONE WRONG
SEGMENT rather than raising, so the fail-toward-covering branch above is
never reached; a real self-grant would slip through as an UNDER-report.
Stated over the CLASS rather than a list of spellings: before any lexing,
`covers()` scans the raw command string for any separator (`_BASH_SEPS` plus
`\\n`/`\\r`) occurring other than as a standalone token, and if one is found,
returns `True` unconditionally -- the lexer is never asked about an input it
mis-segments silently. This is permanent at this layer (the loss happens in
`shlex.split`, upstream of `split_segments`); no lexer fix closes it.
"""
from __future__ import annotations

import fnmatch
import re
import shlex
from typing import Any

from .bash_write_targets import _BASH_SEPS, split_segments

_FILE_TOOLS = {"Edit", "Write", "Read", "Glob", "Grep"}

_ENTRY_RE = re.compile(r"^(?P<tool>[A-Za-z_][A-Za-z0-9_]*)(?:\((?P<spec>.*)\))?$")

_SEP_PATTERN = re.compile(
    "|".join(sorted((re.escape(s) for s in _BASH_SEPS), key=len, reverse=True))
)

# `shlex.shlex(posix=True).whitespace`, minus the two that short-circuit below.
_LEXER_WHITESPACE = " \t"


def _parse_entry(entry: str) -> tuple[str, str | None] | None:
    """`(tool, spec)` for a `Tool(spec)` or bare `Tool` entry, else `None` if
    `entry` does not match either shape at all."""
    if not isinstance(entry, str):
        return None
    m = _ENTRY_RE.match(entry)
    if not m:
        return None
    return m.group("tool"), m.group("spec")


def _has_unresolved_separator(command: str) -> bool:
    """True iff a Bash separator character occurs in the RAW `command` string
    other than as a standalone token -- i.e. not surrounded on both sides by
    whitespace (or string start/end). A standalone-token separator (the
    well-formed spaced `&&` case) segments correctly under `shlex.split` and
    is left to real segment-wise matching; every other spelling is a case
    `shlex.split` is known to mis-segment silently.

    "Whitespace" here means THE LEXER'S whitespace (`_LEXER_WHITESPACE`), not
    `str.isspace()`. The carve-out rests on the separator surviving lexing as
    its own token, so the only surrounding characters that make it standalone
    are the ones `shlex` actually splits on. Python calls 29 characters
    whitespace; `shlex` splits on 4 of them. Testing with `str.isspace()`
    admits the other 25 -- `\\x0b`, `\\x0c`, `\\x1c`-`\\x1f`, `\\x85`, `\\xa0`,
    and the Unicode spaces -- each of which `shlex` glues to the separator,
    so `"cd /repo \\x0b; git push"` lexes as `[... '/repo', '\\x0b;', 'git' ...]`
    and `covers()` returns `False` on a command the lexer got wrong. That is
    the one direction this module must never fail in.

    `\\n`/`\\r` are IN the lexer's whitespace and still cannot use the
    carve-out: `shlex` eats them, so a separator surrounded by newlines is
    standalone while the newline itself is a lost separator. They therefore
    short-circuit ahead of the scan, which leaves space and tab as the only
    admissible surroundings."""
    if "\n" in command or "\r" in command:
        return True
    for m in _SEP_PATTERN.finditer(command):
        before = command[m.start() - 1] if m.start() > 0 else " "
        after = command[m.end()] if m.end() < len(command) else " "
        if before not in _LEXER_WHITESPACE or after not in _LEXER_WHITESPACE:
            return True
    return False


def _covers_bash(spec: str, tool_input: dict) -> bool:
    command = tool_input.get("command")
    if not isinstance(command, str):
        return True  # missing/unknown operand field

    if _has_unresolved_separator(command):
        return True  # class-level raw-string short-circuit -- see module docstring

    try:
        tokens = shlex.split(command)
    except ValueError:
        return True  # unbalanced quote -- shlex raises, split_segments never sees it

    for seg in split_segments(tokens):
        operand = " ".join(seg)
        if spec.endswith(":*"):
            if operand.startswith(spec[:-2]):
                return True
        elif operand == spec:
            return True
    return False


def _file_operand(tool_input: dict) -> str | None:
    for key in ("file_path", "path", "pattern"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value
    return None


def _covers_file_tool(spec: str, tool_input: dict) -> bool:
    operand = _file_operand(tool_input)
    if operand is None:
        return True  # missing/unknown operand field

    if spec.endswith(":*"):
        return operand.startswith(spec[:-2])
    return fnmatch.fnmatch(operand, spec)


def covers(entry: str, tool_name: str, tool_input: dict[str, Any]) -> bool:
    """True iff `entry` (a `permissions.allow`/`deny`-style specifier) would
    have permitted a call of `tool_name` with `tool_input`.

    Three entry forms: `Tool(prefix:*)` (operand begins with `prefix`),
    `Tool(exact)` (operand matches `exact` -- glob/fnmatch for the file
    tools, whose specifiers are path globs), and bare `Tool` (every call of
    that tool, including `Tool(*)`, treated the same way).

    Any ambiguity -- an unparseable entry, a tool this module does not model
    an operand for, or a call missing the operand field it expects --
    resolves to `True`. See the module docstring for why."""
    parsed = _parse_entry(entry)
    if parsed is None:
        return True

    entry_tool, spec = parsed
    if entry_tool != tool_name:
        return False

    if spec is None or spec == "*":
        return True

    if tool_name == "Bash":
        return _covers_bash(spec, tool_input)
    if tool_name in _FILE_TOOLS:
        return _covers_file_tool(spec, tool_input)

    return True  # unmodelled tool with a non-wildcard specifier
