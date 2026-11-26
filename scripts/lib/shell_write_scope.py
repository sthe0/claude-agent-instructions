"""Where a shell command line may write, as a gate that refuses what it cannot read needs it.

Difficulty removed: ``lib/bash_write_targets.py`` lists the write-target candidates of one command line against
ONE working directory and leaves a ``$VAR`` as it is; the Claude hooks that consume it accept that, because their
answer on an unreadable target is "allow". A gate that must REFUSE what it cannot show to lie outside a protected
set needs three more things, kept here so the lexer stays the one shared reading of the command grammar:

* every directory the command can be running in: the session's, the one a leading ``cd`` or ``git -C`` selects
  (``git_cwd.effective_git_cwd``, what both Claude hooks use), and the target of every later ``cd`` / ``pushd``;
* the variables the session shell is known to carry unchanged (``ALLOWED_VARIABLES``), expanded, and ``$PWD``
  expanded to the directory the scan is in; every other ``$name``, ``$OLDPWD``, and a name the command assigns
  itself, stays unexpanded;
* a verdict on what is still unreadable: a target that keeps a ``$``, a backtick or a brace expansion, one that
  sits under a directory no ``cd`` target could be resolved to, one under ``/proc`` or ``/dev/fd`` (whose real
  path is this process's, not the shell's), and a command line that does not tokenize.

A target is resolved as the hooks resolve one (``hook-guard-canon-readonly.py``, ``_canon_target``): the real path
of the raw joined path, so a link is followed before a ``..`` is applied, as the shell does.

The recursive and whole-directory forms of ``cp`` and ``mv`` are reported apart (``whole``): a
``cp -r`` onto a directory that is an ancestor of a protected path merges into it, so the caller refuses those
whose target is such an ancestor.
"""
from __future__ import annotations

import glob
import os
import re
import string
from typing import NamedTuple

from . import bash_write_targets, git_cwd, shell_tokens

# A value no shell reads as syntax: letters, digits and path punctuation. A variable whose value holds anything
# else (a space, a quote, a glob character) is not expanded, so its use in a target stays unreadable.
SAFE_VALUE_CHARACTERS = frozenset(string.ascii_letters + string.digits + "_./@%+=:,~-")
NAME_CHARACTERS = frozenset(string.ascii_letters + string.digits + "_")
# What the tool shell inherits unchanged from the process that started OpenCode, and a command does not rebind
# unless it names the variable itself (``assigned_names``). ``PWD`` is not here: it is the directory of the scan.
ALLOWED_VARIABLES = frozenset({
    "HOME", "USER", "LOGNAME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
    "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG", "AGENT_OPENCODE_HOME",
})
GLOB_CHARACTERS = "*?["
UNKNOWN_DIRECTORY = "/\0unknown-directory"
MAX_DIRECTORIES = 64
DIRECTORY_VERBS = ("cd", "pushd")
VOLATILE_ROOTS = ("/proc", "/dev/fd")
_ASSIGNMENT = re.compile(r"(?<![\w$])([A-Za-z_]\w*)\+?=")
_BINDER = re.compile(r"\b(?:export|declare|typeset|local|readonly|read|unset|for|select|mapfile|readarray|getopts|"
                     r"let|printf\s+-v)\b([^;&|\n]*)")
_HOME_FORM = re.compile(r"(^|/)~[-+]")


class UnreadableCommand(ValueError):
    """The command line does not tokenize, or names more directories than the check follows."""


class WriteScope(NamedTuple):
    resolved: tuple[str, ...]
    unreadable: tuple[str, ...]
    whole: tuple[str, ...] = ()


def _is_name(text: str) -> bool:
    return bool(text) and not text[0].isdigit() and all(c in NAME_CHARACTERS for c in text)


def assigned_names(command: str) -> frozenset[str]:
    """The names ``command`` may rebind itself: ``NAME=`` and ``NAME+=`` anywhere, and the words after a
    command that binds (``export``, ``read``, ``for``, ``declare``, ...). A name in this set keeps its ``$`` use
    unexpanded: the process environment is no longer what the command reads. A heuristic over the text, not a
    parse: it can only add names, which makes a target unreadable and so refused."""
    names = set(_ASSIGNMENT.findall(command))
    for rest in _BINDER.findall(command):
        names.update(re.findall(r"[A-Za-z_]\w*", rest))
    return frozenset(names)


def expand_variables(text: str, env, pwd: str | None = None) -> str:
    """``text`` with each ``$NAME`` and ``${NAME}`` replaced by its value when the name is in
    ``ALLOWED_VARIABLES`` and set in ``env``, or is ``PWD`` and ``pwd`` is given, the value is made of
    ``SAFE_VALUE_CHARACTERS`` and ``text`` does not assign the name itself; every other use of ``$`` (``OLDPWD``
    among them) is kept as written. Single quotes are not told apart: a literal ``'$NAME'`` is expanded too,
    which can only add a candidate."""
    assigned = assigned_names(text)
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
            value = None
            if _is_name(name) and name not in assigned:
                value = pwd if name == "PWD" else env.get(name) if name in ALLOWED_VARIABLES else None
            if value is not None and all(c in SAFE_VALUE_CHARACTERS for c in value):
                out.append(value)
                i = end
                continue
        out.append(text[i])
        i += 1
    return "".join(out)


def is_volatile(path: str) -> bool:
    """True for a path under ``/proc`` or ``/dev/fd``, as written or with its ``..`` applied: what such a path
    names depends on the process that opens it (``/proc/self/cwd`` is this bridge's directory, not the shell's)."""
    return any(candidate == root or candidate.startswith(root + os.sep)
               for candidate in (path, os.path.normpath(path)) for root in VOLATILE_ROOTS)


def has_unexpanded_syntax(path: str) -> bool:
    """True when ``path`` still holds a variable, a command substitution, a backtick, a ``~+`` / ``~-`` form
    (the shell's PWD and OLDPWD) or a brace expansion: what it names cannot be told without running the shell."""
    if "$" in path or "`" in path or "\0" in path.replace(UNKNOWN_DIRECTORY, "") or _HOME_FORM.search(path):
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
    earlier directories possible). A target that cannot be read, or lies under ``/proc`` or ``/dev/fd``, puts
    ``UNKNOWN_DIRECTORY`` in."""
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
        if has_unexpanded_syntax(target) or any(c in target for c in GLOB_CHARACTERS) or is_volatile(target):
            add(UNKNOWN_DIRECTORY)
            continue
        for directory in list(found):
            joined = target if os.path.isabs(target) else os.path.join(directory, target)
            add(UNKNOWN_DIRECTORY if is_volatile(joined) else joined)
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


def _short_flags(rest: list[str]) -> str:
    return "".join(token[1:] for token in rest if token.startswith("-") and not token.startswith("--"))


def whole_directory_targets(segment: list[str], directory: str) -> list[str]:
    """The paths a recursive or whole-directory ``cp`` / ``mv`` segment writes into: a ``cp`` with ``-r``,
    ``-R``, ``-a`` or ``-T``, a ``mv`` of a directory (or with ``-T``). The same candidates the shared lexer
    lists, and the target itself under ``-T``; a bare destination that is an existing directory is not one
    (``cp -r x d`` writes ``d/x``). Empty for every other segment, ``install -d`` included: it makes directories
    and never writes into an existing one."""
    if not segment:
        return []
    verb = os.path.basename(segment[0])
    rest = bash_write_targets._operands_until_redirect(segment[1:])
    long_flags = {token for token in rest if token.startswith("--")}
    short = _short_flags(rest)
    positionals, dest_option = bash_write_targets._copy_operands(rest)
    no_target_directory = "T" in short or "--no-target-directory" in long_flags
    if verb == "cp":
        whole = bool(set("rRaT") & set(short)) or bool({"--recursive", "--archive", "--no-target-directory"} & long_flags)
    elif verb == "mv":
        sources = positionals if dest_option is not None else positionals[:-1]
        whole = no_target_directory or any(os.path.isdir(bash_write_targets._abs(s, directory)) for s in sources)
    else:
        return []
    if not whole:
        return []
    found = bash_write_targets._copy_targets(rest, directory)
    destination = dest_option if dest_option is not None else positionals[-1] if positionals else None
    if destination is not None:
        absolute = bash_write_targets._abs(destination, directory)
        if no_target_directory:
            found.append(absolute)
        elif os.path.isdir(absolute) and not destination.endswith(os.sep):
            found = [path for path in found if path != absolute]
    return found


def _tokens(expanded: str) -> list[str]:
    try:
        return shell_tokens.separator_exact_split(shell_tokens.neutralize_heredoc_constructs(expanded))
    except Exception:
        raise UnreadableCommand("the command line does not tokenize") from None


def write_scope(command: str, cwd: str, env) -> WriteScope:
    """The real paths ``command`` can write (``resolved``), the write targets it cannot be read to name
    (``unreadable``) and the real paths its recursive or whole-directory copies write into (``whole``). The
    command is read once for each directory it can run in, with ``$PWD`` that directory. Raises
    ``UnreadableCommand`` for a command line that does not tokenize."""
    views: dict[str, tuple[str, list[str]]] = {}

    def view(directory: str) -> tuple[str, list[str]]:
        if directory not in views:
            expanded = expand_variables(command, env, None if directory.startswith(UNKNOWN_DIRECTORY) else directory)
            views[directory] = (expanded, _tokens(expanded))
        return views[directory]

    directories = [cwd]
    queue = [cwd]
    while queue:
        expanded, tokens = view(queue.pop(0))
        for found in working_directories(expanded, cwd, tokens):
            if found not in directories:
                directories.append(found)
                if not found.startswith(UNKNOWN_DIRECTORY):
                    queue.append(found)
        if len(directories) > MAX_DIRECTORIES:
            raise UnreadableCommand(f"the command line changes directory in more than {MAX_DIRECTORIES} ways")
    candidates: list[str] = []
    whole_candidates: list[str] = []
    for directory in directories:
        expanded, tokens = view(directory)
        for target in bash_write_targets.command_write_targets(expanded, directory):
            if target not in candidates:
                candidates.append(target)
        for segment in bash_write_targets.split_segments(tokens):
            whole_candidates += whole_directory_targets(segment, directory)

    def resolve(paths: list[str]) -> list[str]:
        return [os.path.realpath(path) for candidate in paths if not candidate.startswith(UNKNOWN_DIRECTORY)
                and not has_unexpanded_syntax(candidate) and not is_volatile(candidate)
                for path in _expansions(candidate)]

    unreadable: list[str] = []
    for candidate in candidates:
        if candidate.startswith(UNKNOWN_DIRECTORY) or has_unexpanded_syntax(candidate) or is_volatile(candidate):
            unreadable.append(candidate.replace(UNKNOWN_DIRECTORY, "<unknown directory>"))
    return WriteScope(tuple(dict.fromkeys(resolve(candidates))), tuple(unreadable),
                      tuple(dict.fromkeys(resolve(whole_candidates))))
