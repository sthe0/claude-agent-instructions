"""Reusable HEAD-baseline vs working-tree violation-set diff.

Difficulty removed: a whole-repo verifier (e.g. verify-config-root-refs.py)
that ignores `--staged` blocks an unrelated commit on a PRE-EXISTING red —
a violation that was already on HEAD, or that lives in a file the staged
change never touches. Filtering by FILE LIST (the verify-cross-refs.py
model) does not generalize to a cross-file finder such as a stale-allowlist-
entry check, whose violations are not naturally keyed to one file.

This module diffs a verifier's own VIOLATION SET (not its stdout, not the
staged file list) between a clean HEAD baseline and the current working
tree, so a caller can report only what the staged change actually
introduces while still tolerating everything already true at HEAD. It has
no knowledge of any specific verifier: the caller supplies the finder and
the violation-identity key.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Hashable, Iterable, Iterator

# Location/index env vars that `git` exports into a hook subprocess (githooks(5)).
# During a real `git commit`, GIT_INDEX_FILE points at the very index being
# committed; a nested `git worktree add` inherits it and resets THAT index to
# HEAD, silently discarding the caller's staged changes (empty commit). `git -C`
# does NOT override an inherited GIT_INDEX_FILE. Scrub these for the duration of
# any git operation this module runs so nested git resolves context from cwd.
_LEAKING_GIT_ENV = (
    "GIT_INDEX_FILE",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_PREFIX",
    "GIT_NAMESPACE",
    "GIT_INDEX_VERSION",
)


@contextmanager
def _clean_git_env() -> Iterator[None]:
    """Remove the leaking git location/index env vars for the duration of the
    block, restoring the saved values on exit (finally). Nested entry is safe:
    an inner call finds nothing present to pop and restores nothing. A no-op
    when the vars are absent (the common non-hook path)."""
    saved = {k: os.environ.pop(k) for k in _LEAKING_GIT_ENV if k in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


class BaselineUnavailable(Exception):
    """Raised when a clean HEAD baseline cannot be constructed.

    Covers an unborn HEAD (no commits yet), `repo_root` not being inside a
    git work tree, and a `git worktree add` failure for any other reason.
    """


@contextmanager
def baseline_worktree(repo_root: Path) -> Iterator[Path]:
    """A throwaway detached worktree checked out at HEAD of `repo_root`.

    Never mutates the caller's working tree or index — the baseline lives
    entirely in a temporary directory that is removed on exit (best-effort:
    `git worktree remove` first, then an unconditional `shutil.rmtree` so a
    partial/failed add never leaks a directory).
    """
    repo_root = Path(repo_root)
    with _clean_git_env():
        head_check = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--verify", "-q", "HEAD"],
            capture_output=True,
            text=True,
        )
        if head_check.returncode != 0:
            raise BaselineUnavailable(
                f"no HEAD commit in {repo_root} (unborn HEAD or not a git work tree)"
            )

        tmpdir = tempfile.mkdtemp(prefix="baseline-diff-")
        added = False
        try:
            add = subprocess.run(
                ["git", "-C", str(repo_root), "worktree", "add", "--detach", "--quiet", tmpdir, "HEAD"],
                capture_output=True,
                text=True,
            )
            if add.returncode != 0:
                raise BaselineUnavailable(
                    f"git worktree add failed for {repo_root}: {add.stderr.strip()}"
                )
            added = True
            yield Path(tmpdir)
        finally:
            if added:
                subprocess.run(
                    ["git", "-C", str(repo_root), "worktree", "remove", "--force", tmpdir],
                    capture_output=True,
                    text=True,
                )
            shutil.rmtree(tmpdir, ignore_errors=True)


def new_violations(
    finder: Callable[[Path], Iterable[Any]],
    repo_root: Path,
    *,
    key: Callable[[Any], Hashable],
    on_degraded: Callable[[str], None] | None = None,
) -> list:
    """Violations `finder(repo_root)` reports that are NOT already present at HEAD.

    `finder` is any callable `repo_root -> iterable of violations`; the
    violations may be tuples, dicts, or bare strings — whatever shape the
    caller's finder returns — since identity is derived entirely through
    `key`, which should be based on content (e.g. file + stripped line text)
    rather than position, so an unrelated line-number shift of an unchanged
    violation is never reported as new.

    When a clean baseline cannot be constructed (`BaselineUnavailable`),
    degrades to the current whole-repo result — every current violation is
    reported — rather than silently passing a real new violation; `on_degraded`,
    if given, is called with a one-line note describing why.
    """
    with _clean_git_env():
        current = list(finder(repo_root))
        try:
            with baseline_worktree(repo_root) as baseline_root:
                baseline = list(finder(baseline_root))
        except BaselineUnavailable as exc:
            if on_degraded is not None:
                on_degraded(f"baseline unavailable ({exc}); reporting all current violations")
            return current

        baseline_keys = {key(item) for item in baseline}
        return [item for item in current if key(item) not in baseline_keys]
