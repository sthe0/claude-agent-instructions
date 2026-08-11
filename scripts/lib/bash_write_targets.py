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
filesystem policy. That is why a `cp`/`mv`/`install` destination yields both
readings of an ambiguous final token rather than one resolved by `isdir` (see
`_copy_targets`): CANDIDATES, plural and possibly over-inclusive, are the
contract. Heredoc/here-string bodies are stripped via
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


#: Verbs of the form `VERB [options] SRC... DEST` — the destination is a write
#: target, and when it is a directory the files written are inside it.
_COPY_VERBS = ("cp", "mv", "install")

#: Options of those verbs that consume the FOLLOWING token as their value, so
#: that value is not a source. Without `-m`'s `600` here, `install -m 600 s d`
#: reads `600` as a source and manufactures a `d/600` candidate. An option a
#: given verb does not have (`-m` on `mv`) makes the command itself invalid, so
#: one table for the three verbs costs nothing.
_COPY_VALUE_OPTS = frozenset({
    "-t", "--target-directory",   # all three
    "-S", "--suffix",             # all three
    "-m", "--mode", "-o", "--owner", "-g", "--group",  # install
})


def _copy_operands(rest: list[str]) -> tuple[list[str], str | None]:
    """`(positionals, explicit_target_directory)` of a `_COPY_VERBS` segment."""
    positionals: list[str] = []
    dest_opt: str | None = None
    take_next: str | None = None
    for tok in rest:
        if take_next is not None:
            if take_next in ("-t", "--target-directory"):
                dest_opt = tok
            take_next = None
        elif tok in _COPY_VALUE_OPTS:
            take_next = tok
        elif tok.startswith("--target-directory="):
            dest_opt = tok.split("=", 1)[1]
        elif tok.startswith("-"):
            continue
        else:
            positionals.append(tok)
    return positionals, dest_opt


def _copy_targets(rest: list[str], eff_cwd: str) -> list[str]:
    """Every path a `cp`/`mv`/`install` segment writes.

    A DESTINATION DIRECTORY IS RESOLVED TO THE FILES IT MEANS. `cp s.json d/`
    does not write `d`; it writes `d/s.json`. Reporting the directory made a
    consumer that asks "is this target a JSON document" answer no and allow the
    write — one command, no new grammar, and the whole point of the verb missed.
    So each source contributes `DEST/basename(SRC)`, one candidate per source,
    which is a JOIN over tokens already in hand rather than a second parse.

    WHICH SPELLINGS MEAN A DIRECTORY IS DECIDED WITHOUT TOUCHING THE FILESYSTEM,
    and that is deliberate: this module's contract is join-only, no existence
    check, and a shared lexer that stats paths would hand every consumer a
    verdict that depends on when it was asked. Two spellings SAY directory —
    `-t DIR`, and a trailing separator — and those emit the joins alone. A bare
    final token is AMBIGUOUS (`cp s.json d` writes `d` if `d` is a file and
    `d/s.json` if it is a directory), so both readings are emitted as what this
    function returns anyway: candidates, which each caller tests under its own
    policy. Over-emitting is safe for both consumers here — a candidate that
    does not exist answers nothing, and containment is monotone under the join,
    so a joined path inside a tree of interest implies its destination was too.
    """
    positionals, dest_opt = _copy_operands(rest)
    if dest_opt is not None:
        dest, sources, dest_is_dir = dest_opt, positionals, True
    elif len(positionals) >= 2:
        dest, sources = positionals[-1], positionals[:-1]
        dest_is_dir = dest.endswith(os.sep)
    elif positionals:
        # One operand and no `-t`: not a well-formed copy, and there is no
        # source to join. Report it as the destination, as this verb's handling
        # always has.
        return [_abs(positionals[-1], eff_cwd)]
    else:
        return []

    abs_dest = _abs(dest, eff_cwd)
    targets: list[str] = [] if dest_is_dir else [abs_dest]
    for src in sources:
        base = os.path.basename(src.rstrip(os.sep))
        if base:
            targets.append(os.path.join(abs_dest, base))
    return targets


def _abs(candidate: str, eff_cwd: str) -> str:
    return candidate if os.path.isabs(candidate) else os.path.join(eff_cwd, candidate)


def segment_write_target(seg: list[str], eff_cwd: str) -> list[str]:
    """Every write-target candidate of a single command segment, as absolute
    paths resolved against `eff_cwd`, in the order a caller should prefer them:
    output-redirect targets in left-to-right order, then a verb-based writer's
    target(s) (`sed -i`, `tee`, the `cp`/`mv`/`install` destination AND the
    files it means when it is a directory, `patch`/`git apply` — the last two
    write relative to the cwd itself, so `eff_cwd` is the sole candidate).
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

    # THE PATCH VERBS ARE THE ONE PLACE A DIRECTORY IS STILL THE ANSWER, and it
    # is not an oversight: which files a patch writes is stated inside the patch
    # BODY, so the only honest target derivable from the command line is the
    # directory it is applied in. A consumer whose policy is containment (the
    # canon guard) is served exactly right by that. A consumer that asks "is
    # this target a permission document" gets a directory, which is no document,
    # and allows -- a named residual of the self-grant gate rather than a thing
    # to fix here, because fixing it means reading the patch body, i.e. a second
    # grammar. `_copy_targets` below is the opposite case: there the file names
    # ARE on the command line, so answering with the directory was a real defect.
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
    elif verb in _COPY_VERBS:
        candidates.extend(_copy_targets(rest, eff_cwd))

    return candidates


def command_write_targets(command: str, eff_cwd: str) -> list[str]:
    """Every write-target candidate of `command` (all segments, in command
    order), as absolute paths resolved against `eff_cwd`. Strips heredoc/
    here-string bodies first (`lib/shell_tokens.py`) so a line of body text is
    never read as command syntax. Fail-open (empty list) on any parse error —
    matching every other consumer's convention in this hook family."""
    command = shell_tokens.strip_heredoc_bodies(command)
    try:
        tokens = shlex.split(command)
    except Exception:
        return []
    if not tokens:
        return []
    targets: list[str] = []
    for seg in split_segments(tokens):
        targets.extend(segment_write_target(seg, eff_cwd))
    return targets
