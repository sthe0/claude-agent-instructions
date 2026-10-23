"""Generic Bash write-target lexer: which file path(s), if any, a command writes.

Difficulty removed: hook-guard-canon-readonly.py originally fused this parsing
(split a command into pipeline/list segments, find the write-target operand of
a redirect / `sed -i` / `cp`/`mv` / `tee` segment) with canon-specific policy
(is that operand inside the canon checkout). A second consumer that needs the
same parsing but a DIFFERENT policy over the result could not reuse it without
either duplicating the lexer or dragging canon policy along. This module holds
only the parsing; a caller supplies its own policy over the candidate paths it
returns.

Every returned candidate is an ABSOLUTE path: a leading `~` is expanded, and
what is still relative is joined onto the `eff_cwd` the caller supplies —
otherwise join-only, no existence check and no filesystem policy. A `$VAR` is
NOT expanded and joins under the cwd like any other relative token; `_abs` says
why the two are not one feature. That is why a `cp`/`mv`/`install` destination
yields both readings of an ambiguous final token rather than one resolved by
`isdir` (see `_copy_targets`): CANDIDATES, plural and possibly over-inclusive,
are the contract. Heredoc/here-string bodies are blanked (not removed) via
`lib/shell_tokens.py`'s `neutralize_heredoc_constructs` before tokenizing, so a
Markdown blockquote line or an unbalanced quote inside a body is never read as
syntax, without trusting that a body a later statement goes on to execute is
truly inert.
"""
from __future__ import annotations

import os
import re

from . import shell_tokens

# Matches a leading `$VAR` or `${VAR}` and captures the variable name plus the tail.
_LEADING_VAR_RE = re.compile(r'^\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?(.*)', re.DOTALL)

# Matches a shell assignment `VAR=value` (entire token).
_ASSIGN_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)', re.DOTALL)

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


def _copy_targets(rest: list[str], eff_cwd: str, var_env: dict[str, str] | None = None) -> list[str]:
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
        return [_abs(positionals[-1], eff_cwd, var_env)]
    else:
        return []

    abs_dest = _abs(dest, eff_cwd, var_env)
    targets: list[str] = [] if dest_is_dir else [abs_dest]
    for src in sources:
        base = os.path.basename(src.rstrip(os.sep))
        if base:
            targets.append(os.path.join(abs_dest, base))
    return targets


def _try_expand_var(token: str, env: dict[str, str]) -> str:
    """Expand a leading `$VAR` or `${VAR}` in `token` from `env`, or return `token` unchanged."""
    m = _LEADING_VAR_RE.match(token)
    if m:
        name, tail = m.group(1), m.group(2)
        if name in env:
            return env[name] + tail
    return token


def _abs(candidate: str, eff_cwd: str, var_env: dict[str, str] | None = None) -> str:
    """`candidate` as an absolute path: a leading `~` expanded, then optionally a
    leading `$VAR` expanded from `var_env`, then the cwd join.

    EXPANDUSER, AND SELECTIVELY EXPANDVARS — `~` means the invoking user's home, this
    process runs as that user in that session, so expanding it names the path the
    command will really write; and it does so by two independent routes rather than one,
    since with `HOME` unset `expanduser` does NOT return the token unchanged (MEASURED,
    and the opposite is the natural assumption) but falls back to the passwd entry for
    `os.getuid()` — still that same user's home.

    A `$VAR` is expanded only when `var_env` is supplied and the variable IS present in
    it. `var_env` is built from two sources: assignments in preceding segments of the
    same command (`o=/tmp/x; cat > $o/f`), and `os.environ` for OS-reliably-set
    variables (`$TMPDIR`, `$HOME`, `$PATH`) that the command inherits. A variable NOT in
    `var_env` stays unexpanded, joins under `eff_cwd`, and remains a named residual in
    each consumer — the safe over-inclusive fallback for genuinely unknown variables.
    """
    candidate = os.path.expanduser(candidate)
    if var_env is not None and candidate.startswith("$"):
        candidate = _try_expand_var(candidate, var_env)
    return candidate if os.path.isabs(candidate) else os.path.join(eff_cwd, candidate)


def redirect_targets(seg: list[str], eff_cwd: str, var_env: dict[str, str] | None = None) -> list[str]:
    """Output-redirect candidates anywhere in `seg` — `> f`, `>> f`, glued
    `>f`/`>>f` — as absolute paths resolved against `eff_cwd`. This is the
    shell's OWN effect, orthogonal to which verb the segment invokes, so a
    caller that supplies its own verb-specific resolution (rather than this
    module's `_COPY_VERBS`/`patch`/`sed`/`tee` dispatch below) can still pick
    up a layered redirect via this function alone, without re-deriving the
    redirect grammar. Extracted from `segment_write_target` (part (a) below,
    unchanged) rather than duplicated.

    A `>` or `>>` followed by `(` is the start of a write process substitution
    `>(cmd)`, not an ordinary redirect — `(` is not a file path. The operator-exact
    tokenizer splits `>(tee y)` into `['>', '(', 'tee', ...]`, so the `(` would
    otherwise be read as the redirect's file operand. Skipping it avoids the
    artifact; `command_write_targets` adds `eff_cwd` for `>(` via a separate check."""
    candidates: list[str] = []
    for i, tok in enumerate(seg):
        redirect_tgt: str | None = None
        if tok in (">", ">>"):
            nxt = seg[i + 1] if i + 1 < len(seg) else None
            if nxt is not None and nxt != "(":
                redirect_tgt = nxt
        elif tok.startswith(">") and tok.strip(">"):
            redirect_tgt = tok.lstrip(">")
        if redirect_tgt:
            candidates.append(_abs(redirect_tgt, eff_cwd, var_env))
    return candidates


def segment_write_target(seg: list[str], eff_cwd: str, var_env: dict[str, str] | None = None) -> list[str]:
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

    # (a) output redirection anywhere in the segment: `> f`, `>> f`, glued `>f`/`>>f`.
    candidates: list[str] = redirect_targets(seg, eff_cwd, var_env)

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
        # sed's operand list is SCRIPT [FILE ...] when no -e/-f is present, and
        # FILE ... when -e or -f supplies the script. Without this distinction
        # every inline sed script (e.g. `s/a/b/`) is an over-emitted candidate
        # that joins under eff_cwd and looks like a real path to every consumer.
        has_ef = any(
            tok in ("-e", "-f", "--expression", "--file")
            or tok.startswith(("--expression=", "--file="))
            for tok in rest
        )
        skip_next = False
        first_operand = True
        for tok in rest:
            if skip_next:
                skip_next = False
                continue
            if tok.startswith("-"):
                if tok in ("-e", "-f", "--expression", "--file"):
                    skip_next = True
                continue
            if not has_ef and first_operand:
                first_operand = False
                continue  # inline script operand, not a file path
            candidates.append(_abs(tok, eff_cwd, var_env))
    elif verb == "tee":
        for tok in rest:
            if tok.startswith("-"):
                continue
            candidates.append(_abs(tok, eff_cwd, var_env))
    elif verb in _COPY_VERBS:
        candidates.extend(_copy_targets(rest, eff_cwd, var_env))

    return candidates


def _update_var_env_from_seg(seg: list[str], var_env: dict[str, str]) -> None:
    """Record any leading VAR=value assignment tokens from `seg` into `var_env`.

    Stops at the first non-assignment token, matching bash's own rule that
    assignments before a command word are temporary env for that command."""
    for tok in seg:
        m = _ASSIGN_RE.match(tok)
        if m:
            var_env[m.group(1)] = m.group(2)
        else:
            break


def segments(command: str) -> list[list[str]]:
    """`command`'s pipeline/list segments: heredoc/here-string bodies stripped
    (`shell_tokens.strip_heredoc_bodies`), then
    `shell_tokens.separator_exact_split` (which normalizes newline separators
    itself) and `split_segments`. Empty (not an exception) on any parse error,
    the same fail-open convention as `command_write_targets`.

    A NAMED RESIDUAL, not a bug: this uses `strip_heredoc_bodies` (removal)
    where `command_write_targets` uses `neutralize_heredoc_constructs`
    (blanking, offset-preserving) -- the latter is required to keep a
    body-persisted unbalanced quote from reaching the tokenizer unneutralized
    (see `hook-guard-canon-readonly.py`'s `decide()`, the "I2" bug this
    guards against). Both now tokenize via `separator_exact_split`, so a
    caller of `segments()` that also needs write targets can still call
    `command_write_targets` directly without a token-shape mismatch, but the
    two do not necessarily agree on offsets or on which body text survives.

    PULLED OUT so this module's own callers -- the ones that need to know
    which VERB produced a segment, or that a segment produced none -- get a
    single tokenization to depend on rather than each hand-rolling their own,
    which would be a second place for the heredoc-stripping / newline-
    normalization steps to go stale relative to this module's own.

    Uses `separator_exact_split` rather than plain `shlex.split` so a bash
    separator glued to an adjacent word (`echo hi;dd of=x`) still becomes its
    own token -- otherwise it stays fused into one word, `split_segments`
    never sees a boundary there, and a write-capable command hiding behind a
    write-incapable one on the same glued segment is invisible to every
    caller of this function."""
    command = shell_tokens.strip_heredoc_bodies(command)
    try:
        tokens = shell_tokens.separator_exact_split(command)
    except Exception:
        return []
    return list(split_segments(tokens))


def command_write_targets(command: str, eff_cwd: str) -> list[str]:
    """Every write-target candidate of `command` (all segments, in command
    order), as absolute paths resolved against `eff_cwd`. Blanks heredoc/
    here-string bodies first (`lib/shell_tokens.py`) so a line of body text is
    never read as command syntax. Fail-open (empty list) on any parse error —
    matching every other consumer's convention in this hook family.

    A WRITE PROCESS SUBSTITUTION `>(cmd)` ADDS `eff_cwd` AS AN EXTRA CANDIDATE,
    deliberately ahead of the segment loop: `>(cmd)` forks and runs `cmd` as a
    side effect of preparing the outer command's argument list, piping the outer
    command's output somewhere the command line does not name. Neither `>` nor
    `(` is a bash separator, so `split_segments` never starts a new segment for
    it and `_operands_until_redirect` stops scanning before it — the nested
    command is invisible to every verb dispatch in this module, exactly the same
    blind spot `patch`/`git apply` already answer with the directory they are
    applied in. Over-inclusive by the same contract every candidate here carries.

    A READ PROCESS SUBSTITUTION `<(cmd)` IS NOT ADDED: `<(cmd)` forks `cmd` and
    feeds its stdout into the outer command's argument list — the outer command
    writes nothing extra due to `<(`. Only `>(` warrants the cwd candidate.

    Variable expansion is seeded from `os.environ` (reliably-set OS variables
    like `$TMPDIR`/`$HOME`) and augmented by any `VAR=value` assignments from
    preceding segments in the same command (e.g. `o=/tmp/x; echo > $o/f`)."""
    command = shell_tokens.neutralize_heredoc_constructs(command)
    extra = [eff_cwd] if ">(" in command else []
    try:
        tokens = shell_tokens.separator_exact_split(command)
    except Exception:
        return extra
    if not tokens:
        return extra
    var_env: dict[str, str] = dict(os.environ)
    targets: list[str] = list(extra)
    for seg in split_segments(tokens):
        targets.extend(segment_write_target(seg, eff_cwd, var_env))
        _update_var_env_from_seg(seg, var_env)
    return targets
