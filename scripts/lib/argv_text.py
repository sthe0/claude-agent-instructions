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
``rstrip()``-ed, matching the existing ``read_text_or_file`` semantics in both
spawn wrappers. So a value routed through a file is NOT byte-identical to the same
text passed inline — a trailing newline is lost. This is cosmetic for every
consumer here (prose fields, all stripped before storage) but it is real.
"""
from __future__ import annotations

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


def _read_ref(ref: str) -> str:
    # The probe is guarded as well as the read: on an over-long value the
    # `is_file()` call itself raises OSError (ENAMETOOLONG), which is exactly the
    # illegible traceback this convention exists to replace.
    try:
        path = Path(ref)
        if ref and path.is_file():
            return path.read_text(encoding="utf-8").rstrip()
    except OSError:
        pass
    raise SystemExit(_ref_error(ref))


def _ref_error(ref: str) -> str:
    shown = ref[:_REF_SNIPPET_CHARS]
    if len(ref) > _REF_SNIPPET_CHARS:
        shown += f"... ({len(ref)} chars)"
    return (
        f"error: '@{shown}' does not name a readable file. "
        "A leading '@' means \"read this value from the file at this path\" — "
        "write the text to a file and pass '@<path>'. "
        "To pass text that really starts with '@', double it: '@@'."
    )
