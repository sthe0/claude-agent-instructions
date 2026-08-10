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
    the state the join decision depends on.

    Retained as a standalone function solely because two tests bind to it
    directly by name (`test_backslash_newline_inside_single_quotes_is_not_a_continuation`,
    `test_backslash_newline_outside_quotes_is_still_joined`); `command_write_targets`
    no longer calls it — `_split_logical_units` below tracks the same
    backslash-escape state itself, alongside quote/`$(`-depth/backtick
    state, in one pass, so this function's own drift (if any, from a future
    edit) cannot silently change gate behavior — it sits outside the
    scanning pipeline entirely."""
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


def _split_logical_units(command: str) -> list[str]:
    """Split `command` into logical units at every newline that is NOT
    inside a quoted span, a `$(...)` substitution, or a backtick span — the
    ONE quote/`$(`-depth/backtick/backslash-aware scanner `command_write_targets`
    tokenizes against, on both its primary and its fallback reading.
    `" ; ".join(_split_logical_units(command))` is what the primary path
    tokenizes as one stream (`;` is already a member of `_BASH_SEPS`, so no
    new separator token is needed downstream — this only supplies the token
    `shlex.split()` never emits for a bare newline, which it treats exactly
    like a space); each element of the returned list is what the fallback
    tokenizes UNIT BY UNIT when the whole-stream reading raises.

    Backslash-newline continuations are resolved first (elided), except
    inside a single-quoted span, where shell performs no escape processing
    at all and the pair stays two literal characters — the same rule
    `_join_backslash_continuations` implements, tracked here directly rather
    than by calling it, because this scanner needs that same escape state
    simultaneously with quote/depth/backtick state on one pass: a newline
    decision needs to know whether it is inside a quote, a substitution, or
    neither, and re-deriving that state with a second, separately-written
    scanner is exactly the kind of drift a past fix in this file (F4) had to
    close (a per-line split that stopped tracking quote state across a
    physical-line boundary at all).

    A newline that survives inside a single- or double-quoted span (real
    shell performs no line-splitting inside a quoted string) is kept as a
    literal character in the current unit, preserving the quoted string as
    one token exactly as `shlex.split()` itself would read it.

    A newline inside an UNQUOTED `$(...)` command substitution or backtick
    span is a second such case: the substitution's *result* is used as one
    argument value of the containing command, so ending the unit there
    splits the substitution in half at the shlex layer — the outer
    command's real write target is lost and a fabricated fragment (the
    substitution's own broken-off tail) is reported in its place. `$(` / `)`
    are tracked with a depth counter (nesting, e.g. `$(echo $(ls))`, closes
    correctly) and a backtick span with a toggle; while either is active the
    newline becomes a plain space instead — the substitution stays one
    shlex token, its internal words stay separate. Depth/toggle tracking is
    skipped entirely while inside a quoted span (a `$(`/backtick that is
    itself only quoted TEXT, e.g. inside a single-quoted argument, never
    reaches this branch at all; and a `$(...)` embedded in a DOUBLE-quoted
    argument already has its newlines preserved by the pre-existing
    `not in_double` guard, so it needs no separate tracking of its own).
    `$((...))` arithmetic expansion is covered by the same `$(` counter with
    no special case: the inner `(` of the `((` digraph is not itself a
    tracked opener, so it passes through untouched, and depth still reaches
    back to 0 by the construct's own closing `)`s.

    Two related constructs are DELIBERATELY left unhandled, each pinned by
    its own test documenting the (imperfect but non-crashing) behavior:

    - Process substitution `<(...)`/`>(...)`: recognizing it would need a
      second opener class keyed on `<(`/`>(` rather than `$(`, AND that
      token shape already collides with `segment_write_target`'s pre-
      existing (newline-unrelated) `tok.startswith(">")` redirect heuristic
      — `>(cmd)` is misread as a bare `>`-redirect to a path `(cmd)` even on
      a single physical line, today, independent of this scanner. Extending
      depth-tracking to `<(`/`>(` here would paper over a symptom of that
      separate bug without fixing it, and risks new interactions for a
      construct this hook family does not otherwise special-case.
    - A bare (non-`$`) subshell/group nested *inside* a `$(...)` — e.g.
      `$(cmd1 && (cmd2)\ncmd3)` — is invisible to a counter that only opens
      on the two-character `$(` sequence: the inner group's own closing `)`
      decrements the SAME counter as the outer substitution's, so depth
      reaches 0 one `)` early and a newline after that point is (wrongly)
      treated as a top-level unit break again. Fixing this needs full
      paren-matching (tracking bare `(` too, and distinguishing a grouping
      paren from stray literal parens in ordinary argument text) — a
      materially bigger parser than the depth counter this scanner adds."""
    units: list[str] = []
    current: list[str] = []
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
            current.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            nxt = command[i + 1]
            if nxt == "\n":
                i += 2
                continue
            current.append(ch)
            current.append(nxt)
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = True
            current.append(ch)
            i += 1
            continue
        if ch == '"':
            in_double = not in_double
            current.append(ch)
            i += 1
            continue
        if not in_double:
            if ch == "$" and i + 1 < n and command[i + 1] == "(":
                subst_depth += 1
                current.append("$(")
                i += 2
                continue
            if ch == ")" and subst_depth > 0:
                subst_depth -= 1
                current.append(ch)
                i += 1
                continue
            if ch == "`":
                in_backtick = not in_backtick
                current.append(ch)
                i += 1
                continue
            if ch == "\n":
                if subst_depth == 0 and not in_backtick:
                    units.append("".join(current))
                    current = []
                else:
                    current.append(" ")
                i += 1
                continue
        current.append(ch)
        i += 1
    units.append("".join(current))
    return units


def _prepare_command_for_whole_stream(command: str) -> str:
    """The primary path's tokenizer input: every `_split_logical_units` unit
    joined back with `" ; "`, reproducing exactly what a real shell does
    with a bare top-level newline (a plain statement separator, same as
    `;`)."""
    return " ; ".join(_split_logical_units(command))


def command_write_targets(command: str, eff_cwd: str) -> list[str]:
    """Every write-target candidate of `command` (all segments, in command
    order), as absolute paths resolved against `eff_cwd`. Strips heredoc/
    here-string bodies first (`lib/shell_tokens.py`) so a line of body text is
    never read as command syntax.

    Primary path: tokenize the WHOLE command as one stream, after
    `_prepare_command_for_whole_stream` has (a) resolved backslash-newline
    continuations and (b) turned every newline that is NOT inside a quoted
    span into a `;` statement separator. This is the only way to keep a
    value that itself spans physical lines (a multi-line commit message,
    `git commit -m 'first line\\n\\nbody'`) as ONE token: splitting by
    physical line first makes the line that merely CLOSES such a quote
    untokenizable on its own, silently dropping every target from that line
    onward even though nothing on it was actually malformed.

    Fallback path, entered only when the whole-stream `shlex.split()` raises
    (a quote that is genuinely unterminated somewhere in the command, with
    no well-defined lexical reading of what follows it): a genuinely
    unterminated quote never closes, so `_split_logical_units` folds
    everything from the point it opens onward into ONE unit that carries the
    same unterminated quote and fails to tokenize on its own — a quote-aware
    reading alone would then recover NOTHING past that point, including a
    well-formed write on a later, otherwise-unrelated physical line (this
    was tried and measured to regress relative to the prior physical-line
    fallback, which recovered exactly that later line). Neither reading
    subsumes the other — the logical-unit reading recovers a write that
    lies before/across a well-formed multi-line construct (a quoted
    argument, a `$(...)` substitution) even when some OTHER unit in the
    command is unrecoverable; the raw physical-line reading recovers a
    write that lies strictly AFTER the genuinely bad quote, which the
    quote-aware unit split swallows whole — so both run and their results
    are unioned, order-preserving and de-duplicated: this is a deny-gate
    lexer where over-reporting a candidate is the safe direction (a
    fabricated fragment costs nothing; a missed real target does not), so
    unioning two partial, complementary readings is strictly safer than
    picking either alone. This still fails open on a write that shares its
    OWN unit/physical line with the bad quote itself (an accepted,
    irreducible residual — see
    `test_unterminated_quote_on_the_same_unit_as_the_write_is_an_accepted_residual`);
    it recovers everything else."""
    command = shell_tokens.strip_heredoc_bodies(command)

    units = _split_logical_units(command)
    try:
        tokens = shlex.split(" ; ".join(units))
    except Exception:
        tokens = None

    if tokens is not None:
        targets: list[str] = []
        for seg in split_segments(tokens):
            targets.extend(segment_write_target(seg, eff_cwd))
        return targets

    targets = []
    seen: set[str] = set()

    def _harvest(lines) -> None:
        for line in lines:
            try:
                line_tokens = shlex.split(line)
            except Exception:
                continue
            if not line_tokens:
                continue
            for seg in split_segments(line_tokens):
                for target in segment_write_target(seg, eff_cwd):
                    if target not in seen:
                        seen.add(target)
                        targets.append(target)

    _harvest(units)
    _harvest(command.splitlines())
    return targets
