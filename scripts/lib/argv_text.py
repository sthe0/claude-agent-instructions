"""The `@<path>` convention: potentially-large text passed BETWEEN commands
travels as a FILE reference in argv, never as inline argv text.

Difficulty removed: Linux caps a single argv string at MAX_ARG_STRLEN =
32 * PAGE_SIZE = 131072 bytes (and the whole vector at ARG_MAX). A coordinator
handing a specialist a dossier, a replanning task or a long critique inline hits
that ceiling as an E2BIG the kernel raises BEFORE the child starts — or, when the
receiver is path-typed, as an illegible ``OSError: File name too long``. Neither
failure names the actual contract. This module is the one place that decides how
an argv value is read, so every narrative argument across ``agentctl`` and the two
spawn wrappers shares one escaping rule instead of each growing its own.

The rule, applied to the argv VALUE only:

  ``None``            -> ``None``      (an absent argument stays absent, distinct
                                        from an empty one)
  ``""``              -> ``""``
  ``"@@text"``        -> ``"@text"``   (escape hatch for prose that legitimately
                                        begins with a single ``@``)
  ``"@path"``         -> the file's contents, or a clean ``SystemExit`` naming
                        the path, the contract and the ``@@`` escape when the
                        file is missing or unreadable
  anything else       -> returned verbatim

The rule is NOT applied recursively to file CONTENTS: a file whose text begins
with ``@`` is returned untouched.

FAIL-LOUD on a missing referent is the load-bearing choice, not an oversight. A
silent fallback to verbatim would record a typo'd ``@/tmp/dosier.md`` into engine
state as literal nine-character prose while the coordinator believes a dossier was
delivered — a silent false-green, the failure mode this repo's gate design exists
to prevent. The raise is loud and names its own remedy.

CAVEAT a reader must not have to discover from the source: file contents are
``rstrip()``-ed, matching the rstrip both spawn wrappers have always applied to a
constraints value. So a value routed through a file is NOT byte-identical to the same
text passed inline — a trailing newline is lost. This is cosmetic for every
consumer here (prose fields, all stripped before storage) but it is real.

The module's second entry point serves the flags that are ALREADY path-only and
stay that way — ``--plan``, ``--context-dossier``. They need no ``@``: the path is
the whole value. What they need is ``read_required_file`` / ``is_readable_file``,
which state the file-path contract when a caller passes prose instead of a path,
rather than leaking the ``OSError`` a bare ``Path`` operation raises.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Linux single-argv-string ceiling: 32 * PAGE_SIZE. The reason this module
# exists; also encoded with the same derivation in
# scripts/tests/test_spawn_specialist_stdin_prompt.py.
MAX_ARG_STRLEN = 131072

# Long enough that a REAL path is named in full in the error (a pytest tmp_path
# already runs past 80 chars), short enough that a 120 KB inline payload
# mistaken for a reference does not become a 120 KB error message.
_REF_SNIPPET_CHARS = 200


def read_arg_text(value: str | None) -> str | None:
    """Resolve one argv value per the `@<path>` convention documented above.

    Raises ``SystemExit`` with an actionable message when a ``@``-reference names
    a file that cannot be read.
    """
    if value is None:
        return None
    if value.startswith("@@"):
        return value[1:]
    if value.startswith("@"):
        return _read_ref(value[1:])
    return value


def read_arg_text_list(values: list[str] | None) -> list[str] | None:
    """Element-wise ``read_arg_text`` for an ``action="append"`` argument.

    Preserves the None-vs-empty-list distinction: ``None`` (the argument was never
    given) stays ``None``, ``[]`` stays ``[]``.
    """
    if values is None:
        return None
    return [read_arg_text(v) for v in values]


def stage_text_to_tempfile(text: str) -> Path:
    """Write `text` to a new temp file and return its absolute path.

    For a coordinator holding large text in memory that must cross a process
    boundary as ``@<path>``. The CALLER owns cleanup — nothing here deletes the
    file.
    """
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", prefix="argv-text-", delete=False, encoding="utf-8"
    )
    with tmp:
        tmp.write(text)
    return Path(tmp.name)


def is_readable_file(path: str | Path | None) -> bool:
    """Whether `path` names a file this process can read — and NEVER raises.

    The naive ``Path(x).is_file()`` is itself unsafe as a probe: handed prose
    instead of a path it raises OSError (ENAMETOOLONG) rather than returning
    False, which is the illegible traceback this module exists to replace. So the
    PROBE is guarded, not only the read — a guard around ``read_text`` alone
    leaves the original traceback reachable one call earlier.

    Every OSError means "not a readable file" here (ENAMETOOLONG, EACCES, ELOOP,
    ENOTDIR alike): the caller's remedy is the same for all of them, and a
    narrower catch would let the rarer ones escape as tracebacks.
    """
    if not path:
        return False
    try:
        candidate = Path(path)
        return candidate.is_file() and os.access(candidate, os.R_OK)
    except OSError:
        return False


def read_required_file(path: str | Path, flag_name: str) -> str:
    """Read the file `flag_name` was given, or exit stating the contract.

    For path-typed arguments that take no ``@``. On any failure the ``SystemExit``
    names the flag, says a readable file PATH is expected, echoes (truncated) what
    arrived instead, and gives the remedy — so the reader fixes their argument
    rather than investigating the filesystem.
    """
    text = _read_file_or_none(path)
    if text is None:
        raise SystemExit(file_arg_error(flag_name, path))
    return text


def file_arg_error(flag_name: str, value: object) -> str:
    """The one wording for "this flag takes a file path, and that was not one".

    Shared so a wrapper's early pre-check and the read at prompt-assembly time
    cannot drift into saying different things about the same contract.
    """
    return (
        f"error: {flag_name} expects the path of a readable file, but got "
        f"'{abbreviate(value)}'. Write the text to a file and pass its path — "
        f"{flag_name} never takes the text itself."
    )


def abbreviate(value: object) -> str:
    """Render an argv value for a diagnostic, bounded in length.

    Without this a 120 KB payload mistaken for a path becomes a 120 KB error
    message — unreadable in a terminal and in the refusal log alike.
    """
    text = str(value)
    if len(text) <= _REF_SNIPPET_CHARS:
        return text
    return f"{text[:_REF_SNIPPET_CHARS]}... ({len(text)} chars)"


def _read_file_or_none(path: str | Path | None) -> str | None:
    if not is_readable_file(path):
        return None
    try:
        return Path(path).read_text(encoding="utf-8").rstrip()
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError (a ValueError, not an OSError) is how a file that
        # exists but is not utf-8 surfaces — "unreadable" per the contract, so it
        # takes the same clean exit rather than an illegible traceback.
        return None


def _read_ref(ref: str) -> str:
    text = _read_file_or_none(ref)
    if text is None:
        raise SystemExit(_ref_error(ref))
    return text


def _ref_error(ref: str) -> str:
    return (
        f"error: '@{abbreviate(ref)}' does not name a readable file. "
        "A leading '@' means \"read this value from the file at this path\" — "
        "write the text to a file and pass '@<path>'. "
        "To pass text that really starts with '@', double it: '@@'."
    )
