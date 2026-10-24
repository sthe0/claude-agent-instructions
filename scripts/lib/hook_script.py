"""Which script a hook command runs: the parser self-diagnose (broken-hook scan) and
install-reminder-hooks.sh (dangling-row prune, fragment script check) share."""
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
    if not command:
        return None
    expanded = os.path.expanduser(os.path.expandvars(command))
    try:
        tokens = shlex.split(expanded)
    except ValueError:
        return None
    if not tokens:
        return None

    idx = 0
    while idx < len(tokens) and _VAR_ASSIGNMENT_RE.match(tokens[idx]):
        idx += 1
    if idx >= len(tokens):
        return None

    candidate = Path(tokens[idx])
    if candidate.is_absolute():
        return candidate
    if candidate.name not in HOOK_INTERPRETER_BASENAMES:
        return None

    for tok in tokens[idx + 1:]:
        if tok.startswith("-"):
            continue
        if _VAR_ASSIGNMENT_RE.match(tok):
            continue
        if re.search(r"\s", tok):
            continue
        if tok.startswith("/"):
            return Path(tok)
    return None
