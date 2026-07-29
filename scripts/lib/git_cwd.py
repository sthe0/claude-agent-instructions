"""Resolve the working directory a VCS commit command actually targets.

Difficulty removed: a hook that keys an enforcement/nudge decision off the
ambient session cwd misfires when the triggering command embeds its own
`cd <dir> &&` / `git -C <dir>` redirect — the command targets a DIFFERENT tree
than the session sits in (the standard isolated-worktree landing pattern). This
shared primitive parses that redirect so every consumer keys off the tree the
command really commits to. Two hooks need it: hook-guard-canon-readonly.py (its
original home, #44) and hook-readme-currency-reminder.py; extracting it here
gives one implementation and one test surface, so the rule cannot drift.

A second difficulty, narrower: a leading `cd <dir> ;` whose `<dir>` does not
exist FAILS at runtime, and `;` unconditionally runs the next segment anyway —
in the session's ORIGINAL directory, not `<dir>`. Treating such a `cd` as a
redirect (the general rule above) reports a tree the command never actually
reaches, silently discarding a real write into the original directory. The
`_leading_cd_noop_on_failure` check below recognizes the single narrow shape
where this is provable from the command text alone — `cd <literal-dir> ;
<segment>`, nothing else — and falls back to `payload_cwd` there instead.
Every other shape (`&&`/`||` gating, more than two segments, a non-literal
target, any grouping construct) keeps the general rule unchanged: it is
guesswork whether the interior command actually reaches `payload_cwd`, and per
the module's own contract, doubt resolves to the LESS permissive existing
behavior only where it is already provable, not everywhere doubt exists.
Accepted race: a directory created between this check and the command's actual
execution makes the check's `cd` failure stale; undetectable from text alone.
"""
from __future__ import annotations

import os
import re
import shlex

_SEPS = {";", "&&", "||", "|", "|&", "&"}
_GROUPING_TOKENS = {"(", ")", "{", "}"}
_COMPOUND_KEYWORDS = {"if", "for", "while", "until", "case", "do", "then", "esac", "done", "fi"}
_NON_LITERAL_CD_TARGET = re.compile(r"[$`~*?\[\]]")


def _split_on_seps(tokens: list[str]):
    """Segments of `tokens` split on the shell list separators, each paired with
    the separator token that preceded it (`None` for the first segment)."""
    seg: list[str] = []
    sep: str | None = None
    for tok in tokens:
        if tok in _SEPS:
            yield sep, seg
            seg = []
            sep = tok
        else:
            seg.append(tok)
    yield sep, seg


def _leading_cd_noop_on_failure(tokens: list[str], payload_cwd: str) -> bool:
    """True iff `tokens` is narrowly `cd <literal-dir> ; <segment>` — exactly two
    segments joined by a single unconditional `;`, no grouping construct
    anywhere (a `( )`/`{ }`/compound keyword could hide a conditional re-entry
    this check cannot see), the leading segment exactly `cd <literal-target>`
    with no OTHER segment itself a `cd`, and `<literal-target>` does not exist
    right now — the one shape where a failed `cd` provably leaves the next
    segment running against `payload_cwd` unchanged."""
    if any(t in _GROUPING_TOKENS or t in _COMPOUND_KEYWORDS for t in tokens):
        return False
    segments = list(_split_on_seps(tokens))
    if len(segments) != 2:
        return False
    (first_sep, first_seg), (second_sep, second_seg) = segments
    if first_sep is not None or second_sep != ";":
        return False
    if len(first_seg) != 2 or first_seg[0] != "cd":
        return False
    target = first_seg[1]
    if target == "-" or _NON_LITERAL_CD_TARGET.search(target):
        return False
    if second_seg and second_seg[0] == "cd":
        return False
    candidate = target if os.path.isabs(target) else os.path.join(payload_cwd, target)
    return not os.path.isdir(candidate)


def effective_git_cwd(command: str, payload_cwd: str) -> str:
    """The directory a `git commit` in `command` actually targets: the redirect
    the command itself selects (`git -C <dir> commit` or a leading `cd <dir> &&`
    / `cd <dir> ;`), or `payload_cwd` when the command has no such redirect —
    including the narrow `cd <literal-absent-dir> ; <segment>` shape, where the
    `cd` demonstrably fails and `payload_cwd` is where `<segment>` actually
    runs (see module docstring). Best-effort: any parse doubt (or the harness's
    tracked shell cwd getting reset out from under a `cd`/`-C` the command
    actually issues) falls back to `payload_cwd`, never to a MORE permissive
    guess."""
    try:
        tokens = shlex.split(command)
    except Exception:
        return payload_cwd

    def _resolve(candidate: str) -> str:
        if not os.path.isabs(candidate):
            candidate = os.path.join(payload_cwd, candidate)
        return candidate

    for i in range(len(tokens) - 3):
        if (os.path.basename(tokens[i]) == "git" and tokens[i + 1] == "-C"
                and tokens[i + 3] == "commit"):
            return _resolve(tokens[i + 2])
    if len(tokens) >= 2 and tokens[0] == "cd":
        if _leading_cd_noop_on_failure(tokens, payload_cwd):
            return payload_cwd
        return _resolve(tokens[1])
    return payload_cwd
