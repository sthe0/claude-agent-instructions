"""Is a directory inside a repository checkout, inside scratch, or neither?

One answer shared by `hook-guard-permission-surface.py` (whether a settings
write forges a project's settings) and `spawn-specialist.py` (whether a
spawned child may load its start directory's project settings).

Difficulty removed: both callers must tell a real checkout from a scratch
directory dressed up as one. Two copies of the marker rules drift apart, and a
name-only marker — an empty `.git` directory, a `.git` file whose `gitdir:`
points nowhere — is one command to forge.
"""
from __future__ import annotations

import os
from typing import Callable, Iterable

_GITDIR_PREFIX = "gitdir:"


def git_root_marker(directory: str) -> bool | None:
    """True when `directory` holds a real `.git`: a directory with `HEAD`, or
    a file whose `gitdir:` target (taken relative to `directory` unless
    absolute) exists and holds `HEAD` — a linked worktree. An empty `.git`
    directory (git itself walks past it), a stray `.git` file and a dangling
    `gitdir:` are no marker. None when a `.git` file cannot be read."""
    git = os.path.join(directory, ".git")
    if os.path.isdir(git) and os.path.isfile(os.path.join(git, "HEAD")):
        return True
    if os.path.isfile(git):
        try:
            with open(git, encoding="utf-8", errors="replace") as fh:
                first_line = fh.readline(4096)
        except OSError:
            return None
        if first_line.startswith(_GITDIR_PREFIX):
            gitdir = os.path.join(directory, first_line[len(_GITDIR_PREFIX):].strip())
            if os.path.isfile(os.path.join(gitdir, "HEAD")):
                return True
    return False


def vcs_root_marker(directory: str) -> bool | None:
    """True when `directory` is a repository root: a real `.git` (see
    `git_root_marker`) or a `.arc` directory (a mounted arc repository's
    root). None when a `.git` file cannot be read."""
    git = git_root_marker(directory)
    if git is None or git:
        return git
    return os.path.isdir(os.path.join(directory, ".arc"))


def make_probe(scratch_roots: Iterable[str], home: str | None = None) -> Callable[[str], "bool | None"]:
    """A probe that, called with a directory, walks from its realpath up the
    ancestors and answers:

    - True at the first repository root (`vcs_root_marker`), so a monorepo
      subproject counts, and so does a real checkout inside a scratch root;
    - False on reaching one of `scratch_roots` first: a scratch directory with
      no repository inside it;
    - None on reaching `home` (default `~`) or `/` first — an unmarked
      directory, which may still be a project kept outside version control —
      or when a directory on the way cannot be inspected.

    The walk stops at `home` and the scratch roots before testing them, so
    `~/.arc` or a home-level dotfiles `.git` marks nothing below it."""
    scratch = {os.path.realpath(root) for root in scratch_roots if root}
    home_real = os.path.realpath(home if home is not None else os.path.expanduser("~"))

    def probe(directory: str) -> bool | None:
        current = os.path.realpath(directory)
        while True:
            if current in scratch:
                return False
            if current == home_real:
                return None
            if os.path.lexists(current):
                if not os.path.isdir(current) or not os.access(current, os.R_OK | os.X_OK):
                    return None
                marker = vcs_root_marker(current)
                if marker is not False:
                    return marker
            parent = os.path.dirname(current)
            if parent == current:
                return None
            current = parent

    return probe
