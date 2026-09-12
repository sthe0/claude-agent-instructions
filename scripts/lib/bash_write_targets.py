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
filesystem policy. Heredoc/here-string bodies are blanked (not removed) via
`lib/shell_tokens.py`'s `neutralize_heredoc_constructs` before tokenizing, so a
Markdown blockquote line or an unbalanced quote inside a body is never read as
syntax, without trusting that a body a later statement goes on to execute is
truly inert.
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


def command_write_targets(command: str, eff_cwd: str) -> list[str]:
    """Every write-target candidate of `command` (all segments, in command
    order), as absolute paths resolved against `eff_cwd`. Blanks heredoc/
    here-string bodies first (`lib/shell_tokens.py`) so a line of body text is
    never read as command syntax. Fail-open (empty list) on any parse error —
    matching every other consumer's convention in this hook family."""
    command = shell_tokens.neutralize_heredoc_constructs(command)
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
