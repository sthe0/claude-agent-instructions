"""Generic Bash write-target lexer: which file path(s), if any, a command writes.

Difficulty removed: hook-guard-canon-readonly.py originally fused this parsing
(split a command into pipeline/list segments, find the write-target operand of
a redirect / `sed -i` / `cp`/`mv` / `tee` segment) with canon-specific policy
(is that operand inside the canon checkout). A second consumer that needs the
same parsing but a DIFFERENT policy over the result could not reuse it without
either duplicating the lexer or dragging canon policy along. This module holds
only the parsing; a caller supplies its own policy over the candidate paths it
returns.

Every returned candidate is an ABSOLUTE path, resolved relative to the
`eff_cwd` the caller supplies — join-only, no existence check and no
filesystem policy. Heredoc/here-string bodies are stripped via
`lib/shell_tokens.py` before tokenizing, so a Markdown blockquote line inside a
body is never read as syntax.
"""
from __future__ import annotations

import os
import shlex

from . import shell_tokens

_BASH_SEPS = {";", "&&", "||", "|", "|&", "&"}


def split_segments(tokens: list[str]):
    """Yield the pipeline/list segments of a tokenized command, split on the
    shell separators `; && || | |& &`. Best-effort: a separator glued inside a
    single shlex token (`a;b`) is left intact — an accepted residual."""
    seg: list[str] = []
    for tok in tokens:
        if tok in _BASH_SEPS:
            if seg:
                yield seg
            seg = []
        else:
            seg.append(tok)
    if seg:
        yield seg


def _operands_until_redirect(rest: list[str]) -> list[str]:
    """Tokens of a segment (after the command word) up to the first redirection
    operator — `<`/`>` starts an I/O target, not a positional of the verb."""
    out: list[str] = []
    for tok in rest:
        if tok and tok[0] in "<>":
            break
        out.append(tok)
    return out


def _sed_in_place(rest: list[str]) -> bool:
    """True iff any token is a sed in-place flag: `-i`, `-i.bak`, `--in-place`,
    `--in-place=.bak`, or a clustered short flag containing `i` (`-ni`)."""
    for tok in rest:
        if tok == "--in-place" or tok.startswith("--in-place="):
            return True
        if tok.startswith("-") and not tok.startswith("--") and "i" in tok[1:]:
            return True
    return False


def _cp_mv_dest(rest: list[str]) -> str | None:
    """The write destination of a `cp`/`mv`: the `-t DIR` / `--target-directory`
    value if present, else the last positional. Returning only the destination
    keeps copying a path OUT of a tree of interest (source there, dest
    elsewhere) unflagged."""
    positionals: list[str] = []
    take_next = False
    dest_opt: str | None = None
    for tok in rest:
        if take_next:
            dest_opt = tok
            take_next = False
        elif tok in ("-t", "--target-directory"):
            take_next = True
        elif tok.startswith("--target-directory="):
            dest_opt = tok.split("=", 1)[1]
        elif tok.startswith("-"):
            continue
        else:
            positionals.append(tok)
    if dest_opt is not None:
        return dest_opt
    return positionals[-1] if positionals else None


def _abs(candidate: str, eff_cwd: str) -> str:
    return candidate if os.path.isabs(candidate) else os.path.join(eff_cwd, candidate)


def segment_write_target(seg: list[str], eff_cwd: str) -> list[str]:
    """Every write-target candidate of a single command segment, as absolute
    paths resolved against `eff_cwd`, in the order a caller should prefer them:
    output-redirect targets in left-to-right order, then a verb-based writer's
    target(s) (`sed -i`, `tee`, `cp`/`mv` dest, `patch`/`git apply` — the last
    two write relative to the cwd itself, so `eff_cwd` is the sole candidate).
    Empty when the segment has no detectable write.

    A caller that wants a single verdict per segment should test candidates in
    the returned order and stop at its own first match — that reproduces the
    single-verdict search this module was extracted from (hook-guard-canon-
    readonly.py's `_segment_write_target`, before it carried canon policy)."""
    if not seg:
        return []

    candidates: list[str] = []

    # (a) output redirection anywhere in the segment: `> f`, `>> f`, glued `>f`/`>>f`.
    for i, tok in enumerate(seg):
        redirect_tgt: str | None = None
        if tok in (">", ">>"):
            redirect_tgt = seg[i + 1] if i + 1 < len(seg) else None
        elif tok.startswith(">") and tok.strip(">"):
            redirect_tgt = tok.lstrip(">")
        if redirect_tgt:
            candidates.append(_abs(redirect_tgt, eff_cwd))

    # (b) verb-based writers.
    verb = os.path.basename(seg[0]) if seg[0] else ""
    rest = _operands_until_redirect(seg[1:])

    if verb == "patch":
        candidates.append(eff_cwd)
    elif verb == "git" and "apply" in rest:
        candidates.append(eff_cwd)
    elif verb == "sed" and _sed_in_place(rest):
        for tok in rest:
            if tok.startswith("-"):
                continue
            candidates.append(_abs(tok, eff_cwd))
    elif verb == "tee":
        for tok in rest:
            if tok.startswith("-"):
                continue
            candidates.append(_abs(tok, eff_cwd))
    elif verb in ("cp", "mv"):
        dest = _cp_mv_dest(rest)
        if dest:
            candidates.append(_abs(dest, eff_cwd))

    return candidates


def _join_backslash_continuations(command: str) -> str:
    """Resolve a shell line-continuation (`\\` immediately followed by a
    newline) into one logical line, EXCEPT inside a single-quoted span,
    where shell performs no escape processing at all and the pair stays two
    literal characters. Elsewhere a backslash escapes whatever follows it —
    including a quote character, so `\\'`/`\\"` outside single quotes does
    not toggle quote tracking.

    This is a best-effort re-implementation of shell quoting, not a full
    parser: it tracks single- and double-quote spans only, which is exactly
    the state the join decision depends on. Getting it wrong inside a
    single-quoted span leaves that physical line's backslash+newline intact,
    so the line fails its own `shlex.split()` and is dropped by the per-line
    recovery below — a lost line, never a fabricated target."""
    out: list[str] = []
    in_single = False
    in_double = False
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if in_single:
            if ch == "'":
                in_single = False
            out.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            nxt = command[i + 1]
            if nxt == "\n":
                i += 2
                continue
            out.append(ch)
            out.append(nxt)
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = True
        elif ch == '"':
            in_double = not in_double
        out.append(ch)
        i += 1
    return "".join(out)


def command_write_targets(command: str, eff_cwd: str) -> list[str]:
    """Every write-target candidate of `command` (all segments, in command
    order), as absolute paths resolved against `eff_cwd`. Strips heredoc/
    here-string bodies first (`lib/shell_tokens.py`) so a line of body text is
    never read as command syntax, then resolves backslash-newline line
    continuations (`_join_backslash_continuations`) BEFORE splitting into
    physical lines, so a logical command wrapped across lines — the routine
    shape of agent-authored multi-line Bash — is tokenized whole rather than
    truncated at its own continuation.

    Tokenized ONE PHYSICAL LINE AT A TIME, never as one combined token stream:
    `shlex.split()` treats a newline exactly like a space and emits no token
    for it, so `_BASH_SEPS` — a set of literal separator TOKENS — has nothing
    to match a newline against. Tokenizing the whole command at once therefore
    merged two lines into one segment, letting a verb-based writer's operand
    scan on line 1 sweep up line 2's command word as a spurious argument.

    Fail-open PER LINE, not per command: a line that does not tokenize (an
    unterminated quote, a lone trailing backslash) is skipped on its own —
    matching every other consumer's fail-open convention in this hook family
    without letting one malformed line discard every other line's real
    targets."""
    command = shell_tokens.strip_heredoc_bodies(command)
    command = _join_backslash_continuations(command)
    targets: list[str] = []
    for line in command.splitlines():
        try:
            tokens = shlex.split(line)
        except Exception:
            continue
        if not tokens:
            continue
        for seg in split_segments(tokens):
            targets.extend(segment_write_target(seg, eff_cwd))
    return targets
