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

A third difficulty, and the reason cwd resolution is SPLIT into three functions
below: "the cwd of this command" is not one quantity. `cd` moves the SHELL, and
therefore everything after it including a redirect; `git -C` moves ONE git
invocation and nothing else. Measured 2026-08-03:

    cd here && git -C ../other status > notes.md

creates `here/notes.md`, never `other/notes.md` — the shell opens the redirect
in ITS cwd before git ever runs. A single command-wide cwd cannot express that,
so `command_default_cwd` answers for the shell, `segment_git_cwd` answers for
one git invocation's own repo operations, and a caller asking about a redirect
must use the former. `-C` is per-segment and non-transitive for the same
reason: `git -C a status ; git commit` commits in the ambient directory.

WHY THE DETECTOR IS TRI-STATE AND WHY IT SHRANK.

`runs_commit` answers True / False / None, and `effective_git_cwd` acts only on
True. The temptation on meeting an invocation this module cannot place is to
grow the RESOLVER — to teach it about `--git-dir`, `--work-tree`, `--namespace`
until it can answer for those too. That is the wrong direction, because those
options do not merely move the answer, they split the QUESTION. Measured against
git 2.43.0 on 2026-08-03:

    git --git-dir=A/.git --work-tree=B commit -a -m relocated

appends the commit to A's history (A gained a commit; its message is the one
given) while the content committed is B's (`git --git-dir=A/.git show
HEAD:tracked.txt` reads back B's bytes), and A's own working file is left
untouched on disk. And with `--git-dir` alone, from an unrelated directory,
`rev-parse --show-toplevel` reports THE CWD, not A. So "where does this commit
land" and "which tree does this command mutate" have different answers, and a
function with one return value must silently pick one. It returns None instead,
and containment is bought by SHRINKING the detector rather than growing the
resolver.

`--namespace` is grouped with those two by the plan this implements. Measured
here, it does NOT relocate a plain commit: `git --namespace=spaced commit` wrote
`refs/heads/master` exactly as an unnamespaced commit does, leaving no ref under
`refs/namespaces/`. It stays in the set as a conservative doubt — it rewrites
the ref space the invocation reads and writes, and doubt costs a lost deny, not
a false one — but the measured relocation evidence is for `--git-dir` and
`--work-tree` only.
"""
from __future__ import annotations

import os
import re
import shlex

from lib.shell_tokens import (
    drop_substitutions,
    split_segments,
    strip_command_prefix,
    tokenize,
    unquote_word,
)

_GROUPING_CHARS = "(){}"
_COMPOUND_KEYWORDS = {"if", "for", "while", "until", "case", "do", "then", "esac", "done", "fi"}
_NON_LITERAL_CD_TARGET = re.compile(r"[$`~*?\[\]]")

# Git's global-option grammar, and the ONLY place it is encoded. Every entry was
# settled on 2026-08-03 by running the option against the installed git 2.43.0
# in both forms — `git <opt> <value> version` (a value global prints the
# version) and `git <opt> version` (a value global eats `version` and prints
# usage instead) — never from recall or from the manual page.
#
# Two shorts and four longs take a value. The `--opt=value` form is legal for
# the LONGS ONLY: `git -C=/tmp version` and `git -c=a.b=c version` both fail
# with `unknown option`, so the plan's "each also legal as --opt=value" holds
# for the long globals and not for `-C` / `-c`.
_GIT_VALUE_GLOBALS_SHORT = frozenset({"-C", "-c"})
_GIT_VALUE_GLOBALS_LONG = frozenset({
    "--git-dir", "--work-tree", "--namespace", "--config-env", "--attr-source",
})
_GIT_VALUE_GLOBALS = _GIT_VALUE_GLOBALS_SHORT | _GIT_VALUE_GLOBALS_LONG

_GIT_FLAG_GLOBALS = frozenset({
    "--bare", "--no-pager", "--paginate", "-p", "-P", "--no-replace-objects",
    "--literal-pathspecs", "--glob-pathspecs", "--noglob-pathspecs",
    "--icase-pathspecs", "--no-optional-locks",
})

# These print and exit; the subcommand after them never runs (`git --exec-path
# version` prints only the exec path). `--exec-path` is in BOTH readings: bare
# it prints and exits, but `--exec-path=/tmp version` prints the git version, so
# its `=` form is handled with the value globals. `--list-cmds` exists ONLY in
# the `=` form — bare it is `unknown option`, i.e. doubt.
_GIT_PRINT_AND_EXIT = frozenset({
    "-h", "--help", "-v", "--version", "--exec-path", "--html-path",
    "--man-path", "--info-path",
})
_GIT_PRINT_AND_EXIT_EQ = frozenset({"--list-cmds"})

# Globals that relocate the repository the invocation operates on — see the
# module docstring for why an invocation carrying one is answered with doubt
# rather than with a resolved directory.
_GIT_RELOCATING_GLOBALS = frozenset({"--git-dir", "--work-tree", "--namespace"})

# Subcommands that take another COMMAND as an operand and run it (`git bisect
# run <cmd>`, `git submodule foreach <command>`, `git rebase -x <exec>`, `git
# filter-branch --tree-filter <command>`, each read from its own `-h` output on
# 2026-08-03). Whether a commit runs is then a property of a command this
# scanner never sees.
_GIT_COMMAND_TAKING = frozenset({"submodule", "rebase", "bisect", "filter-branch"})


class GitInvocation:
    """What one git invocation's global options say, before its subcommand runs.

    `doubt` means the scanner met an option shape it does not positively
    recognize and therefore no longer knows which token is the subcommand.
    `subcommand` is None when the invocation runs none — either because a
    print-and-exit global consumed it, or because the segment ended.
    """

    __slots__ = ("subcommand", "chdirs", "relocating", "doubt")

    def __init__(self, subcommand=None, chdirs=(), relocating=False, doubt=False):
        self.subcommand = subcommand
        self.chdirs = list(chdirs)
        self.relocating = relocating
        self.doubt = doubt


def scan_git_invocation(segment: list[str]) -> GitInvocation | None:
    """Read `segment`'s git global options up to its subcommand, or None when
    `segment` does not invoke git at all.

    An option shape outside the four positive sets above yields
    `doubt=True` — never a silently skipped flag, which would let the scanner
    read that option's VALUE as the subcommand.
    """
    if not segment or os.path.basename(unquote_word(segment[0])) != "git":
        return None
    chdirs: list[str] = []
    relocating = False
    i = 1
    while i < len(segment):
        tok = unquote_word(segment[i])
        if not tok.startswith("-") or tok == "-":
            return GitInvocation(tok, chdirs, relocating)
        if "=" in tok:
            head = tok.split("=", 1)[0]
            if head in _GIT_VALUE_GLOBALS_LONG or head == "--exec-path":
                relocating = relocating or head in _GIT_RELOCATING_GLOBALS
                i += 1
                continue
            if head in _GIT_PRINT_AND_EXIT_EQ:
                return GitInvocation(None, chdirs, relocating)
            return GitInvocation(None, chdirs, relocating, doubt=True)
        if tok in _GIT_VALUE_GLOBALS:
            if i + 1 >= len(segment):
                return GitInvocation(None, chdirs, relocating, doubt=True)
            if tok == "-C":
                chdirs.append(segment[i + 1])
            relocating = relocating or tok in _GIT_RELOCATING_GLOBALS
            i += 2
            continue
        if tok in _GIT_FLAG_GLOBALS:
            i += 1
            continue
        if tok in _GIT_PRINT_AND_EXIT:
            return GitInvocation(None, chdirs, relocating)
        return GitInvocation(None, chdirs, relocating, doubt=True)
    return GitInvocation(None, chdirs, relocating)


def _resolve(candidate: str, base: str) -> str:
    if not os.path.isabs(candidate):
        candidate = os.path.join(base, candidate)
    return candidate


def _leading_cd_noop_on_failure(tokens: list[str], payload_cwd: str) -> bool:
    """True iff `tokens` is narrowly `cd <literal-dir> ; <segment>` — exactly two
    segments joined by a single unconditional `;`, no grouping construct
    anywhere (a `( )`/`{ }`/compound keyword could hide a conditional re-entry
    this check cannot see), the leading segment exactly `cd <literal-target>`
    with no OTHER segment itself a `cd`, and `<literal-target>` does not exist
    right now — the one shape where a failed `cd` provably leaves the next
    segment running against `payload_cwd` unchanged.

    Grouping is tested by CONTAINMENT, not token equality: a paren can arrive
    glued to its neighbour inside a quoted word (`-m "fix(x)"`), so an equality
    test would accept the glued form and decline its spaced twin — the same
    command, two verdicts, one of them a false deny on a fail-open guard.
    Containment costs two denies it cannot separate from that twin
    (`cd <absent> ; (echo b > s2)`, which does write here, and any commit whose
    MESSAGE carries parentheses); both fall in the fail-open direction and are
    accepted. Compound keywords stay an EQUALITY test — containment would match
    `fi` inside `confirm`.

    The literal-target test runs on the UNQUOTED target, because `cd "/repo/b"`
    really does move to `/repo/b`; `~` and `$` survive quote removal and still
    disqualify, so a quoted target is read exactly as its bare twin.
    """
    if any(any(c in t for c in _GROUPING_CHARS) or t in _COMPOUND_KEYWORDS
           for t in tokens):
        return False
    segments = list(split_segments(tokens))
    if len(segments) != 2:
        return False
    (first_sep, first_seg), (second_sep, second_seg) = segments
    if first_sep is not None or second_sep != ";":
        return False
    if len(first_seg) != 2 or unquote_word(first_seg[0]) != "cd":
        return False
    target = unquote_word(first_seg[1])
    if target == "-" or _NON_LITERAL_CD_TARGET.search(target):
        return False
    if second_seg and unquote_word(second_seg[0]) == "cd":
        return False
    return not os.path.isdir(_resolve(target, payload_cwd))


def _tokenize_for_cwd(command: str) -> list[str] | None:
    """`command`'s tokens for CWD RESOLUTION, or `None` when no lexer parses it.

    The punctuation-aware `tokenize` is tried first and is what the resolution
    rules are written against. Its refusal set does NOT contain `shlex.split`'s,
    though — measured over 3197 harvested commands, 21 parse under `shlex.split`
    and raise here, and 20 do the reverse. The minimal divergence is 6
    characters, a `"` glued to a punctuation character:

        '""h")"'    shlex.split -> ['h)']    tokenize -> ValueError

    which is the closing `)"` of the `git commit -m "$(... "...")"` idiom.

    That asymmetry is not this module's to absorb quietly, because a caller that
    keys a DENY off this answer reads a bail-out to `payload_cwd` as "the commit
    lands in the session's own directory" — the deny-producing answer, reached
    on a command whose `cd` the legacy lexer resolves perfectly well. Before the
    punctuation lexer arrived, one lexer served both the caller's detector and
    this resolver, so a command either was not recognized as a commit at all or
    was resolved correctly; splitting the lexers broke that coupling in the
    direction a fail-open consumer cannot afford.

    So the fallback is the LEGACY lexer, restoring the coupling, rather than a
    posix fallback inside `tokenize` itself: `tokenize`'s contract is that it
    retains quotes, and callers doing path arithmetic distinguish a quoted
    redirect operator from a real one by exactly that. Handing them a
    quote-stripped stream under any condition would silently reintroduce the
    class of false denies the retention exists to prevent.

    `None` (neither lexer parses) leaves the caller its own doubt policy.
    """
    try:
        return tokenize(command)
    except ValueError:
        pass
    try:
        return shlex.split(command)
    except ValueError:
        return None


def command_default_cwd(command: str, payload_cwd: str) -> str:
    """The directory the SHELL runs `command`'s segments in: the leading
    `cd <dir> &&` / `cd <dir> ;` redirect the command itself performs, or
    `payload_cwd`.

    This is the cwd a REDIRECT is opened in, and the starting point every git
    invocation's own `-C` is then resolved against. It deliberately reads the
    RAW tokens rather than the substitution-stripped ones: `cd $(mktemp -d)` must
    still fail the literal-target test on its `$`, which stripping the
    substitution would remove from view.
    """
    tokens = _tokenize_for_cwd(command)
    if tokens is None:
        return payload_cwd
    if len(tokens) >= 2 and unquote_word(tokens[0]) == "cd":
        if _leading_cd_noop_on_failure(tokens, payload_cwd):
            return payload_cwd
        return _resolve(unquote_word(tokens[1]), payload_cwd)
    return payload_cwd


def segment_git_cwd(segment: list[str], default_cwd: str) -> str:
    """The directory ONE git invocation performs its own repo operations in:
    `default_cwd` moved by this segment's own `-C` options, which are cumulative
    and each resolved against the previous (measured 2026-08-03: `git -C cum/x
    -C y rev-parse --absolute-git-dir` reports `cum/x/y/.git`, and a later
    ABSOLUTE `-C` replaces what came before).

    Reads `-C` and nothing else. `--git-dir`, `--work-tree` and `--namespace` are
    deliberately NOT honored here: they do not answer this function's question
    (see the module docstring), and an invocation carrying one is refused
    upstream by `runs_commit` rather than resolved wrongly here.

    Scope: this governs git's own repo operations and its subcommands' path
    operands. It does NOT govern a shell redirect in the same segment — the
    shell opens that in ITS cwd, i.e. in `default_cwd`.
    """
    stripped, recognized = strip_command_prefix(segment)
    if not recognized:
        return default_cwd
    invocation = scan_git_invocation(stripped)
    if invocation is None or invocation.doubt:
        return default_cwd
    cwd = default_cwd
    for raw in invocation.chdirs:
        cwd = _resolve(unquote_word(raw), cwd)
    return cwd


def _segment_runs_commit(segment: list[str], verbs) -> bool | None:
    stripped, recognized = strip_command_prefix(segment)
    if not recognized:
        return None
    if not stripped:
        return False
    verb = os.path.basename(unquote_word(stripped[0]))
    if verb not in verbs:
        return False
    if verb != "git":
        # No global-option grammar is known for another VCS, so only the plain
        # `<vcs> commit` shape is decidable; an option could take a value and
        # hide the subcommand behind it.
        if len(stripped) < 2:
            return False
        nxt = unquote_word(stripped[1])
        if nxt.startswith("-"):
            return None
        return nxt == "commit"
    invocation = scan_git_invocation(stripped)
    if invocation is None or invocation.doubt:
        return None
    if invocation.subcommand in _GIT_COMMAND_TAKING:
        return None
    if invocation.subcommand != "commit":
        return False
    return None if invocation.relocating else True


def runs_commit(command: str, verbs=("git",)) -> bool | None:
    """Whether `command` runs a commit: True, False, or None for doubt.

    True as soon as any one segment provably commits; None when no segment does
    but some segment's answer is unknown; False only when every segment is
    provably not a commit.

    There are exactly FIVE doubt producers, and every one of them is a place
    where an answer could only be guessed:

      1. `tokenize` raises — the command has an unbalanced quote, so its token
         stream is not this command's token stream.
      2. `strip_command_prefix` returns `recognized=False` — a wrapper carried an
         option outside its measured allowlist, so which token is the command
         word is unknown.
      3. `scan_git_invocation` returns `doubt=True` — git carried a global option
         outside the measured grammar, so which token is the subcommand is
         unknown.
      4. The subcommand takes a COMMAND operand (`submodule`, `rebase`,
         `bisect`, `filter-branch`) — whether a commit runs is a property of a
         command string this scanner never parses.
      5. The invocation carries a repo-relocating global (`--git-dir`,
         `--work-tree`, `--namespace`) and would otherwise be a commit — the
         module docstring records the measurement: with `--git-dir=A/.git
         --work-tree=B`, the commit lands in A while the content comes from B,
         so "which tree does this touch" has two answers and no caller of a
         single-valued resolver can be given the right one. `git --git-dir=X
         status` stays a plain False; the doubt is scoped to the commit.

    Producers 1 and 2 also make the whole command undecidable at once, since
    they destroy the segmentation itself; 3, 4 and 5 are per-segment.
    """
    try:
        tokens = tokenize(command)
    except ValueError:
        return None
    doubted = False
    for _sep, segment in split_segments(drop_substitutions(tokens)):
        if not segment:
            continue
        verdict = _segment_runs_commit(segment, verbs)
        if verdict is True:
            return True
        if verdict is None:
            doubted = True
    return None if doubted else False


def effective_git_cwd(command: str, payload_cwd: str) -> str:
    """The directory a `git commit` in `command` actually targets: the redirect
    the command itself selects (`git -C <dir> commit` or a leading `cd <dir> &&`
    / `cd <dir> ;`), or `payload_cwd` when the command has no such redirect —
    including the narrow `cd <literal-absent-dir> ; <segment>` shape, where the
    `cd` demonstrably fails and `payload_cwd` is where `<segment>` actually
    runs (see module docstring). Best-effort: a command NO lexer parses (see
    `_tokenize_for_cwd`), or the harness's tracked shell cwd getting reset out
    from under a `cd`/`-C` the command actually issues, falls back to
    `payload_cwd`.

    That fallback is not a safe default in general, and it is deliberately not
    described as one: a consumer that denies on canon reads `payload_cwd` as the
    DENY-producing answer, so bailing out here costs a false deny rather than a
    lost one. What keeps it acceptable is that `_tokenize_for_cwd` reaches it
    only for a command neither lexer parses — which is also a command the
    caller's own detector cannot have parsed into a commit.

    COMMIT-SCOPED: the `-C` of a segment that does not provably commit is not
    honored, because it moves only that segment. `git -C a status && git commit`
    commits in the shell's directory, not in `a`.
    """
    default_cwd = command_default_cwd(command, payload_cwd)
    tokens = _tokenize_for_cwd(command)
    if tokens is None:
        return payload_cwd
    for _sep, segment in split_segments(drop_substitutions(tokens)):
        if segment and _segment_runs_commit(segment, ("git",)) is True:
            return segment_git_cwd(segment, default_cwd)
    return default_cwd
