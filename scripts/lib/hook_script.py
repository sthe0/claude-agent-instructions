"""Which script a hook command runs: the one parser every hook consumer shares.

``self-diagnose`` (broken-hook scan), ``install-reminder-hooks.sh`` (Core rows,
dangling-row prune) and ``hook_fragments`` (fragment merge) must agree on a hook's
script identity, or one script gets wired twice under two spellings.
"""
import os
import re
import shlex
from pathlib import Path

HOOK_INTERPRETER_BASENAMES = {
    "python", "python3", "python2", "bash", "sh", "zsh", "dash", "ksh",
    "env", "node", "nodejs", "ruby", "perl",
}

_VAR_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def hook_script_path(command):
    """The absolute script path a hook command invokes, or None when there is
    nothing on disk to resolve.

    We expand ~ and env vars ($CLAUDE_PROJECT_DIR etc.) and shlex-split the
    command. If the first token (after skipping any leading `VAR=val`
    assignments) is itself an absolute path, that IS the script — return it
    directly without walking further, so an absolute interpreter invoked with
    module args (`/abs/python -m pkg.main`) resolves at the interpreter, not
    past it. Otherwise, if that token's basename names a known interpreter
    (python/bash/sh/env/...), the real script is a later token: scan the
    remaining tokens, skipping flags and further `VAR=val` assignments, for
    the first absolute path. A candidate token CONTAINING WHITESPACE (a
    quoted inline payload, e.g. `bash -c '/abs/script.sh arg1'`) is never
    treated as a path — that whole payload is shell text, not a script to
    walk, and treating it as one would false-flag a working hook. Bare
    non-interpreter commands (`jq`, unresolved `$VAR`s left literal) stay
    unflagged either way — fail-safe: we would rather miss a broken hook than
    false-flag a legitimate bare command.
    """
    parsed = _split(command)
    if parsed is None:
        return None
    tokens, idx = parsed
    candidate = Path(tokens[idx])
    if candidate.is_absolute():
        return candidate
    if candidate.name not in HOOK_INTERPRETER_BASENAMES:
        return None
    return _operand_script(tokens, idx + 1)


def _split(command):
    """``(tokens, index of the first non-assignment token)``, or None if there is none."""
    if not command:
        return None
    expanded = os.path.expanduser(os.path.expandvars(command))
    try:
        tokens = shlex.split(expanded)
    except ValueError:
        return None
    idx = 0
    while idx < len(tokens) and _VAR_ASSIGNMENT_RE.match(tokens[idx]):
        idx += 1
    if idx >= len(tokens):
        return None
    return tokens, idx


def _operand_script(tokens, start):
    """The first absolute-path operand from ``start`` on, skipping flags and assignments."""
    for tok in tokens[start:]:
        if tok.startswith("-"):
            continue
        if _VAR_ASSIGNMENT_RE.match(tok):
            continue
        if re.search(r"\s", tok):
            continue
        if tok.startswith("/"):
            return Path(tok)
    return None


def script_name(command):
    """Identity of the script a hook command runs.

    The script's file name when one resolves: ``python3 /a/x.py``, ``env python3
    /a/x.py``, ``python3 -X utf8 /a/x.py``, ``/venv/bin/python /a/x.py`` and a bare
    ``/a/x.py`` are all ``x.py``. A launcher with no script operand (``python3 -m pkg``,
    ``bash -c '…'``, ``uv run x.py``, an unexpanded ``$VAR/x.py``) has no file identity,
    so the whole whitespace-normalized command stands for it and two such entries
    collide only when identical.
    """
    parsed = _split(command)
    if parsed is not None:
        tokens, idx = parsed
        first = Path(tokens[idx])
        if first.is_absolute() and first.name not in HOOK_INTERPRETER_BASENAMES:
            return first.name
        if first.is_absolute() or first.name in HOOK_INTERPRETER_BASENAMES:
            script = _operand_script(tokens, idx + 1)
            if script is not None:
                return script.name
    return " ".join((command or "").split())
