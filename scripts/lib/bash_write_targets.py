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


def _split_logical_units(command: str) -> list[str]:
    """Split `command` into logical units at every newline that is NOT
    inside a quoted span, a `$(...)` substitution, a `${...}` expansion, or a
    backtick span — the ONE quote/depth/backtick/backslash-aware scanner
    `command_write_targets` tokenizes against, on both its primary and its
    fallback reading. `" ; ".join(_split_logical_units(command))` is what the
    primary path tokenizes as one stream (`;` is already a member of
    `_BASH_SEPS`, so no new separator token is needed downstream — this only
    supplies the token `shlex.split()` never emits for a bare newline, which
    it treats exactly like a space); each element of the returned list is
    what the fallback tokenizes UNIT BY UNIT when the whole-stream reading
    raises.

    Backslash-newline continuations are resolved first (elided), except
    inside a single-quoted span, where shell performs no escape processing
    at all and the pair stays two literal characters — tracked here directly
    alongside quote/depth/backtick state in the same one-pass scan, because a
    newline decision needs to know whether it is inside a quote, a
    substitution, or neither, and deriving that state with a second,
    separately-written scanner risks exactly the kind of drift this module
    exists to avoid (a per-line split that stops tracking quote state across
    a physical-line boundary at all).

    A newline that survives inside a single- or double-quoted span (real
    shell performs no line-splitting inside a quoted string) is kept as a
    literal character in the current unit, preserving the quoted string as
    one token exactly as `shlex.split()` itself would read it. `$'...'`
    (ANSI-C quoting) needs no separate handling here: it is keyed on the
    same `'` delimiter as an ordinary single-quoted span, so an embedded
    newline inside it is already covered by this same branch.

    A newline inside an UNQUOTED `$(...)` command substitution, `${...}`
    parameter expansion, or backtick span is a second such case: the
    construct's *result* is used as one argument value of the containing
    command, so ending the unit there splits it in half at the shlex layer —
    the outer command's real write target is lost and a fabricated fragment
    (the construct's own broken-off tail) is reported in its place. `$(`/`)`
    and `${`/`}` are each tracked with their own depth counter (nesting on
    either — e.g. `$(echo $(ls))`, `${x:-${y}}` — closes correctly, and the
    two counters cannot cross-trigger each other since they key on disjoint
    character pairs), and a backtick span with a toggle; while any of the
    three is active the newline becomes a plain space instead — the
    construct stays one shlex token, its internal words stay separate.
    Depth/toggle tracking is skipped entirely while inside a quoted span (a
    `$(`/`${`/backtick that is itself only quoted TEXT, e.g. inside a
    single-quoted argument, never reaches this branch at all; and either
    construct embedded in a DOUBLE-quoted argument already has its newlines
    preserved by the pre-existing `not in_double` guard, so it needs no
    separate tracking of its own). `$((...))` arithmetic expansion is
    covered by the same `$(` counter with no special case: the inner `(` of
    the `((` digraph is not itself a tracked opener, so it passes through
    untouched, and depth still reaches back to 0 by the construct's own
    closing `)`s. A bare, unmatched `}` (a brace GROUP's closer, e.g.
    `{ cmd; }`, or a stray literal `}` in ordinary text) never decrements
    `brace_depth` below zero — the guard is `brace_depth > 0`, so it falls
    through as an ordinary character exactly as before this fix, and a bare
    (non-`$`) brace group is never itself tracked as an opener (only the
    two-character `${` sequence is), so it cannot corrupt a genuinely open
    `${...}` span either.

    Left unhandled, each pinned by its own test documenting the (imperfect
    but non-crashing) behavior, together with the reason a fix does not
    belong in this scanner:

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
      materially bigger parser than the depth counter this scanner adds.
    - A standalone `((...))` arithmetic command (not the `$((...))`
      expansion form): its opening is a bare `((` with no `$`, so it is
      never tracked as an opener at all and a newline inside one is read as
      a top-level unit break. Distinguishing this construct's `((` from two
      ordinary nested parens needs the same full paren-matching the bare-
      subshell case above needs, for the same reason — out of scope for a
      depth counter keyed on fixed two-character openers.
    - Shell keyword-based compound commands that span physical lines by
      grammar alone — `[[ ... ]]`, `if`/`fi`, `case`/`esac`, `for`/`done`,
      `while`/`done` — are not openers this scanner tracks at all; a
      newline inside one of these is read as a top-level unit break exactly
      as an ordinary newline would be. Recognizing them needs keyword
      parsing (reserved words, not a fixed character pair), a materially
      different scanner than the depth-counter model this module uses. In
      practice this does not cost a real write target: this module's
      write-detecting verbs (redirects, `sed -i`, `tee`, `cp`/`mv`, `patch`,
      `git apply`) are evaluated per-segment independently of any
      surrounding keyword construct, so a write on its own segment is still
      found even when the keyword construct around it is mis-split.
    - Here-strings (`<<<`) are not an opener at all in this scanner's sense:
      the operand is a single following word (or an already-quoted string,
      whose own newline handling is covered by the ordinary quote-span
      branch above), not a span that must stay open across a newline — so
      no special-casing applies here."""
    units: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    subst_depth = 0
    brace_depth = 0
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
            if ch == "$" and i + 1 < n and command[i + 1] == "{":
                brace_depth += 1
                current.append("${")
                i += 2
                continue
            if ch == "}" and brace_depth > 0:
                brace_depth -= 1
                current.append(ch)
                i += 1
                continue
            if ch == "`":
                in_backtick = not in_backtick
                current.append(ch)
                i += 1
                continue
            if ch == "\n":
                if subst_depth == 0 and brace_depth == 0 and not in_backtick:
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
    well-formed write on a later, otherwise-unrelated physical line that a
    plain per-physical-line split recovers on its own. Neither reading
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
