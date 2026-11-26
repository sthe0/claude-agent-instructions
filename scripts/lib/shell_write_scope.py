"""Where a shell command line may write, as a gate that refuses what it cannot read needs it.

Difficulty removed: ``lib/bash_write_targets.py`` lists the write-target candidates of one command line against
ONE working directory and leaves a ``$VAR`` as it is; the Claude hooks that consume it accept that, because their
answer on an unreadable target is "allow". A gate that must REFUSE what it cannot show to lie outside a protected
set needs three more things, kept here so the lexer stays the one shared reading of the command grammar:

* every directory the command can be running in: the session's, the one a leading ``cd`` or ``git -C`` selects
  (``git_cwd.effective_git_cwd``, what both Claude hooks use), and the target of every later ``cd`` / ``pushd``;
* the variables the session shell is known to carry, expanded: a name the process environment holds and whose
  value has no character a shell reads as syntax;
* a verdict on what is still unreadable: a target that keeps a ``$``, a backtick or a brace expansion, one that
  sits under a directory no ``cd`` target could be resolved to, and a command line that does not tokenize.

A target is resolved as the hooks resolve one (``hook-guard-canon-readonly.py``, ``_canon_target``): the real path
of the raw joined path, so a link is followed before a ``..`` is applied, as the shell does.
"""
from __future__ import annotations

import glob
import os
import string
from typing import NamedTuple

from . import bash_write_targets, git_cwd, shell_tokens

# A value no shell reads as syntax: letters, digits and path punctuation. A variable whose value holds anything
# else (a space, a quote, a glob character) is not expanded, so its use in a target stays unreadable.
SAFE_VALUE_CHARACTERS = frozenset(string.ascii_letters + string.digits + "_./@%+=:,~-")
NAME_CHARACTERS = frozenset(string.ascii_letters + string.digits + "_")
GLOB_CHARACTERS = "*?["
UNKNOWN_DIRECTORY = "/\0unknown-directory"
MAX_DIRECTORIES = 64
DIRECTORY_VERBS = ("cd", "pushd")


class UnreadableCommand(ValueError):
    """The command line does not tokenize, or names more directories than the check follows."""


class WriteScope(NamedTuple):
    resolved: tuple[str, ...]
    unreadable: tuple[str, ...]


def _is_name(text: str) -> bool:
    return bool(text) and not text[0].isdigit() and all(c in NAME_CHARACTERS for c in text)


def expand_variables(text: str, env) -> str:
    """``text`` with each ``$NAME`` and ``${NAME}`` replaced by its value in ``env`` when the name is set there
    and the value is made of ``SAFE_VALUE_CHARACTERS``; every other use of ``$`` is kept as written. Single
    quotes are not told apart: a literal ``'$NAME'`` is expanded too, which can only add a candidate."""
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "$" and i + 1 < len(text):
            if text[i + 1] == "{":
                close = text.find("}", i + 2)
                name, end = (text[i + 2:close], close + 1) if close > 0 else ("", i + 1)
            else:
                end = i + 1
                while end < len(text) and text[end] in NAME_CHARACTERS:
                    end += 1
                name = text[i + 1:end]
            value = env.get(name) if _is_name(name) else None
            if value is not None and all(c in SAFE_VALUE_CHARACTERS for c in value):
                out.append(value)
                i = end
                continue
        out.append(text[i])
        i += 1
    return "".join(out)


def has_unexpanded_syntax(path: str) -> bool:
    """True when ``path`` still holds a variable, a command substitution, a backtick or a brace expansion: what
    it names cannot be told without running the shell."""
    if "$" in path or "`" in path or "\0" in path.replace(UNKNOWN_DIRECTORY, ""):
        return True
    start = path.find("{")
    while start >= 0:
        end = path.find("}", start + 1)
        if end < 0:
            return False
        inner = path[start + 1:end]
        if "," in inner or ".." in inner:
            return True
        start = path.find("{", start + 1)
    return False


def _directory_target(tokens: list[str], i: int) -> str | None:
    """The operand of the ``cd`` / ``pushd`` at ``tokens[i]``: the first token that is no option, ``~`` for none;
    ``None`` for ``cd -`` (the previous directory, one this scan already holds)."""
    for token in tokens[i + 1:]:
        if token in bash_write_targets._BASH_SEPS:
            return "~"
        if token == "-":
            return None
        if not token.startswith("-"):
            return token
    return "~"


def working_directories(command: str, cwd: str, tokens: list[str]) -> list[str]:
    """Every directory ``command`` can run a later word in: ``cwd``, the directory ``effective_git_cwd`` names,
    and for each ``cd`` / ``pushd`` target its place under each directory so far (a ``cd`` that fails leaves the
    earlier directories possible). A target that cannot be read puts ``UNKNOWN_DIRECTORY`` in."""
    found = [cwd]

    def add(path: str) -> None:
        if path not in found:
            found.append(path)
        if len(found) > MAX_DIRECTORIES:
            raise UnreadableCommand(f"the command line changes directory in more than {MAX_DIRECTORIES} ways")

    add(git_cwd.effective_git_cwd(command, cwd))
    for i, token in enumerate(tokens):
        if os.path.basename(token) not in DIRECTORY_VERBS:
            continue
        target = _directory_target(tokens, i)
        if target is None:
            continue
        target = os.path.expanduser(target)
        if has_unexpanded_syntax(target) or any(c in target for c in GLOB_CHARACTERS):
            add(UNKNOWN_DIRECTORY)
            continue
        for directory in list(found):
            add(target if os.path.isabs(target) else os.path.join(directory, target))
    return found


def _expansions(candidate: str) -> list[str]:
    """``candidate`` as written, and, for a glob, every path the glob names now and every name its last
    component could be given inside each directory its leading components name."""
    if not any(c in candidate for c in GLOB_CHARACTERS):
        return [candidate]
    found = [candidate, *glob.glob(candidate)]
    parent, leaf = os.path.split(candidate)
    if leaf and not any(c in leaf for c in GLOB_CHARACTERS):
        found += [os.path.join(directory, leaf) for directory in glob.glob(parent)]
    return found


def write_scope(command: str, cwd: str, env) -> WriteScope:
    """The real paths ``command`` can write (``resolved``) and the write targets it cannot be read to name
    (``unreadable``). Raises ``UnreadableCommand`` for a command line that does not tokenize."""
    expanded = expand_variables(command, env)
    try:
        tokens = shell_tokens.separator_exact_split(shell_tokens.neutralize_heredoc_constructs(expanded))
    except Exception:
        raise UnreadableCommand("the command line does not tokenize") from None
    candidates: list[str] = []
    for directory in working_directories(expanded, cwd, tokens):
        for target in bash_write_targets.command_write_targets(expanded, directory):
            if target not in candidates:
                candidates.append(target)
    resolved: list[str] = []
    unreadable: list[str] = []
    for candidate in candidates:
        if candidate.startswith(UNKNOWN_DIRECTORY) or has_unexpanded_syntax(candidate):
            unreadable.append(candidate.replace(UNKNOWN_DIRECTORY, "<unknown directory>"))
            continue
        resolved.extend(os.path.realpath(path) for path in _expansions(candidate))
    return WriteScope(tuple(dict.fromkeys(resolved)), tuple(unreadable))
