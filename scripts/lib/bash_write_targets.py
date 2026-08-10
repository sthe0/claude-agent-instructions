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


def _prepare_command_for_whole_stream(command: str) -> str:
    """Join backslash-newline continuations AND turn every remaining newline
    that lies outside a quoted span into a statement separator (`" ; "`), in
    ONE quote/backslash-aware pass. `;` is already a member of `_BASH_SEPS`,
    so no new separator token is needed downstream — this only supplies the
    token `shlex.split()` never emits for a bare newline (it treats one
    exactly like a space).

    This is `_join_backslash_continuations` with one added branch. The two
    transformations share the exact same quote-tracking state (single vs.
    double, backslash-escape handling): a newline decision needs to know
    whether it is inside a quote, and that is precisely the state the
    continuation-join already computes. A second, separately-written scanner
    would have to re-derive that same state on its own token-by-token walk —
    two independent implementations of one escaping rule, with no way to
    keep them from drifting apart. That drift is exactly the bug class this
    fix closes (a per-line split that stopped tracking quote state across a
    physical-line boundary at all), so one pass is used here rather than two.
    `_join_backslash_continuations` itself is kept unedited below — it is
    unit-tested directly by name, and it is still the exact transform the
    per-line FALLBACK below needs (continuation-joining without newline
    substitution, so real statement newlines remain physical-line
    boundaries for `str.splitlines()`).

    A newline that survives inside a single- or double-quoted span (real
    shell performs no line-splitting inside a quoted string) is left
    untouched, keeping the quoted string one token exactly as `shlex.split()`
    itself would read it.

    A newline inside an UNQUOTED `$(...)` command substitution or backtick
    span is a second such case: the substitution's *result* is used as one
    argument value of the containing command, so turning that newline into a
    `;` statement separator splits the substitution in half at the shlex
    layer — the outer command's real write target is lost and a fabricated
    fragment (the substitution's own broken-off tail) is reported in its
    place. `$(` / `)` are tracked with a depth counter (nesting, e.g.
    `$(echo $(ls))`, closes correctly) and a backtick span with a toggle;
    while either is active the newline becomes a plain space instead — the
    substitution stays one shlex token, its internal words stay separate.
    Depth/toggle tracking is skipped entirely while inside a quoted span
    (mirrors the quote-blindness of `_join_backslash_continuations` — a
    `$(`/backtick that is itself only quoted TEXT, e.g. inside a
    single-quoted argument, never reaches this branch at all; and a
    `$(...)` embedded in a DOUBLE-quoted argument already has its newlines
    preserved by the pre-existing `not in_double` guard, so it needs no
    separate tracking of its own). `$((...))`  arithmetic expansion is
    covered by the same `$(` counter with no special case: the inner `(` of
    the `((` digraph is not itself a tracked opener, so it passes through
    untouched, and depth still reaches back to 0 by the construct's own
    closing `)`s.

    Two related constructs are DELIBERATELY left unhandled, each pinned by
    its own test documenting the (imperfect but non-crashing) behavior:

    - Process substitution `<(...)`/`>(...)`: recognizing it would need a
      second opener class keyed on `<(`/`>(` rather than `$(`, AND that
      token shape already collides with `segment_write_target`'s pre-
      existing (newline-unrelated) `tok.startswith(">")` redirect heuristic
      — `>(cmd)` is misread as a bare `>`-redirect to a path `(cmd)` even on
      a single physical line, today, independent of this fix. Extending
      depth-tracking to `<(`/`>(` here would paper over a symptom of that
      separate bug without fixing it, and risks new interactions for a
      construct this hook family does not otherwise special-case.
    - A bare (non-`$`) subshell/group nested *inside* a `$(...)` — e.g.
      `$(cmd1 && (cmd2)\ncmd3)` — is invisible to a counter that only opens
      on the two-character `$(` sequence: the inner group's own closing `)`
      decrements the SAME counter as the outer substitution's, so depth
      reaches 0 one `)` early and a newline after that point is (wrongly)
      treated as a top-level separator again. Fixing this needs full
      paren-matching (tracking bare `(` too, and distinguishing a grouping
      paren from stray literal parens in ordinary argument text) — a
      materially bigger parser than the depth counter this fix adds."""
    out: list[str] = []
    in_single = False
    in_double = False
    subst_depth = 0
    in_backtick = False
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
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            in_double = not in_double
            out.append(ch)
            i += 1
            continue
        if not in_double:
            if ch == "$" and i + 1 < n and command[i + 1] == "(":
                subst_depth += 1
                out.append("$(")
                i += 2
                continue
            if ch == ")" and subst_depth > 0:
                subst_depth -= 1
                out.append(ch)
                i += 1
                continue
            if ch == "`":
                in_backtick = not in_backtick
                out.append(ch)
                i += 1
                continue
            if ch == "\n":
                if subst_depth == 0 and not in_backtick:
                    out.append(" ; ")
                else:
                    out.append(" ")
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def command_write_targets(command: str, eff_cwd: str) -> list[str]:
    """Every write-target candidate of `command` (all segments, in command
    order), as absolute paths resolved against `eff_cwd`. Strips heredoc/
    here-string bodies first (`lib/shell_tokens.py`) so a line of body text is
    never read as command syntax.

    Primary path: tokenize the WHOLE command as one stream, after
    `_prepare_command_for_whole_stream` has (a) resolved backslash-newline
    continuations and (b) turned every newline that is NOT inside a quoted
    span into a `;` statement separator. This is what a real shell does —
    a bare newline and `;` are both plain statement separators — and it is
    the only way to keep a value that itself spans physical lines (a
    multi-line commit message, `git commit -m 'first line\\n\\nbody'`) as
    ONE token: splitting by physical line first (the earlier fix here) made
    the line that merely CLOSES such a quote untokenizable on its own,
    silently dropping every target from that line onward even though
    nothing on it was actually malformed.

    Fallback path, entered only when the whole-stream `shlex.split()` raises
    (a quote that is genuinely unterminated somewhere in the command, with
    no well-defined lexical reading of what follows it): recover per
    PHYSICAL LINE, exactly as before this fix — `_join_backslash_continuations`
    then `str.splitlines()`, each line tokenized and skipped independently on
    its own `shlex.split()` failure. This still fails open on the line
    actually carrying the bad quote, and on any line whose own quoting
    depended on a span opened before it (an accepted, irreducible residual —
    see `test_unterminated_quote_after_multiline_quoted_argument_still_returns_nothing`);
    it recovers everything else, matching every other consumer's fail-open
    convention in this hook family."""
    command = shell_tokens.strip_heredoc_bodies(command)

    prepared = _prepare_command_for_whole_stream(command)
    try:
        tokens = shlex.split(prepared)
    except Exception:
        tokens = None

    targets: list[str] = []
    if tokens is not None:
        for seg in split_segments(tokens):
            targets.extend(segment_write_target(seg, eff_cwd))
        return targets

    joined = _join_backslash_continuations(command)
    for line in joined.splitlines():
        try:
            line_tokens = shlex.split(line)
        except Exception:
            continue
        if not line_tokens:
            continue
        for seg in split_segments(line_tokens):
            targets.extend(segment_write_target(seg, eff_cwd))
    return targets
