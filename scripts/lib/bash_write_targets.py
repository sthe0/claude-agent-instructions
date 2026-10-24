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
otherwise join-only, no existence check and no filesystem policy. A leading `$VAR`
is expanded only from a literal assignment earlier in the same command or from the
two inherited names `HOME`/`TMPDIR`, and only when `_variable_tracking_is_safe`
holds; any other `$VAR` joins under the cwd like a relative token, and `_abs` says
why that is the fail-closed direction. The expansion is fail-closed by intent, not
proven: it premises that the hook's `HOME`/`TMPDIR` equal the command shell's, and it
walks the text in order (residuals named in `_VarTracker`). That is why a
`cp`/`mv`/`install` destination yields both readings of an ambiguous final token rather than one resolved by
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

#: A leading `$NAME` or `${NAME}` (group 1 or 2) followed by the rest of the token.
#: `${NAME:-x}` and the other parameter-expansion operators deliberately do not match.
_LEADING_VAR_RE = re.compile(r"^\$(?:([A-Za-z_][A-Za-z0-9_]*)|\{([A-Za-z_][A-Za-z0-9_]*)\})(.*)$", re.DOTALL)

#: An assignment word `NAME=value`, `NAME+=value` or `NAME[i]=value`.
_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(\[[^\]]*\])?(\+?)=(.*)$", re.DOTALL)

_BASH_SEPS = {";", "&&", "||", "|", "|&", "&"}

#: Variables the hook inherits from the same harness that spawned the command's shell.
_INHERITED_VARS = ("HOME", "TMPDIR")

#: A word that opens a compound command or a block: assignments inside one may run
#: zero, one or many times, in an order the textual walk does not see.
_COMPOUND_WORDS = frozenset({
    "if", "then", "elif", "else", "fi", "for", "select", "while", "until", "do", "done",
    "case", "esac", "function", "coproc", "{", "}", "(", ")",
})

#: Verbs that write to a path named on their command line but have no operand grammar in
#: this module; found inside a `<(...)` body they keep the cwd as a candidate.
_UNMODELED_WRITERS = frozenset({
    "dd", "truncate", "touch", "ln", "rsync", "rm", "mkdir",
    "python", "python3", "perl", "ruby", "node", "bash", "sh",
})

#: Builtins that assign variables through an argument that is not an assignment word.
_OPAQUE_VERBS = frozenset({
    "eval", "source", ".", "read", "unset", "getopts", "mapfile", "readarray", "let",
})

#: Builtins whose `NAME=value` operands are assignments in the current shell.
_ASSIGNING_BUILTINS = frozenset({"export", "declare", "typeset", "local", "readonly"})


def _segments_with_separators(tokens: list[str]):
    """Yield `(segment, separator_before, separator_after)` for each pipeline/list
    segment of a tokenized command; a separator at the command's edge is `None`."""
    seg: list[str] = []
    before: str | None = None
    for tok in tokens:
        if tok in _BASH_SEPS:
            if seg:
                yield seg, before, tok
            seg = []
            before = tok
        else:
            seg.append(tok)
    if seg:
        yield seg, before, None


def split_segments(tokens: list[str]):
    """Yield the pipeline/list segments of a tokenized command, split on the
    shell separators `; && || | |& &`. Best-effort: a separator glued inside a
    single shlex token (`a;b`) is left intact — an accepted residual."""
    for seg, _, _ in _segments_with_separators(tokens):
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


def _sed_script_flag(tok: str) -> str | None:
    """How option `tok` supplies a sed script: `"next"` when its value is the FOLLOWING
    token (`-e`, `--expression`, `-f`, `--file`, a cluster ending in `e`/`f` such as `-ne`),
    `"glued"` when the value is inside `tok` itself (`-es/a/b/`, `-fscript.sed`,
    `--expression=...`), `None` when `tok` is no script option. A short cluster is read
    left to right: `i` swallows the rest of the token as its backup suffix, so `-ie` is
    in-place with suffix `e`, not a script flag. GNU long options may be abbreviated
    (`--expr`), so a prefix of `--expression` / `--file` counts."""
    if not tok.startswith("-"):
        return None
    if tok.startswith("--"):
        name, eq, _ = tok.partition("=")
        for full, shortest in (("--expression", 3), ("--file", 4)):
            if len(name) >= shortest and full.startswith(name):
                return "glued" if eq else "next"
        return None
    for idx, ch in enumerate(tok[1:], start=1):
        if ch == "i":
            return None
        if ch in "ef":
            return "glued" if tok[idx + 1:] else "next"
    return None


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
    """`token` with its leading `$NAME` / `${NAME}` replaced by `env[NAME]`, or `token`
    unchanged when it has no such prefix or `NAME` is not in `env`."""
    m = _LEADING_VAR_RE.match(token)
    if m:
        name = m.group(1) or m.group(2)
        if name in env:
            return env[name] + m.group(3)
    return token


def _abs(candidate: str, eff_cwd: str, var_env: dict[str, str] | None = None) -> str:
    """`candidate` as an absolute path: a leading `$VAR` expanded from `var_env` when it
    is there, then a leading `~` expanded, then the cwd join.

    `~` MEANS THE INVOKING USER'S HOME, this process runs as that user in that
    session, so expanding it names the path the command will really write; and it does
    so by two independent routes rather than one, since with `HOME` unset `expanduser`
    does NOT return the token unchanged (MEASURED, and the opposite is the natural
    assumption) but falls back to the passwd entry for `os.getuid()` — still that same
    user's home.

    A `$VAR` is expanded only from `var_env`, which a caller builds conservatively (see
    `_VarTracker`): the hook's environment is not the command's, so a name is trusted
    only when it is one of the two the hook and the command's shell both inherit from
    the harness, or the command itself assigns it a literal value earlier in the same
    text, and in both cases only when nothing in the command can have changed it behind
    the tracker's back. Everything else stays unexpanded and joins under `eff_cwd` — the
    over-inclusive direction: it can only turn an allow into a deny for a cwd inside a
    protected tree, never the reverse — and remains a named residual in each consumer.

    A leading `~` follows the tracked `HOME` when `var_env` holds one, so `HOME=/x; cp s ~/f`
    names `/x/f` as the shell does. A `HOME` the tracker had to drop (assigned conditionally,
    in a pipeline, ...) is a named residual: `~` then resolves to the invoking user's home.
    """
    if var_env is not None and candidate.startswith("$"):
        candidate = _try_expand_var(candidate, var_env)
    if var_env is not None and "HOME" in var_env and (candidate == "~" or candidate.startswith("~/")):
        candidate = var_env["HOME"] + candidate[1:]
    candidate = os.path.expanduser(candidate)
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
        has_ef = any(_sed_script_flag(tok) for tok in rest)
        skip_next = False
        first_operand = True
        for tok in rest:
            if skip_next:
                skip_next = False
                continue
            if tok.startswith("-"):
                skip_next = _sed_script_flag(tok) == "next"
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


def _dollar_may_be_literal(command: str) -> bool:
    """True iff some `$` in `command` is single-quoted, backslash-escaped or opens an
    ANSI-C / locale string (`$'..'`, `$".."`). The tokenizer drops quoting, so such a
    `$NAME` reaches `_abs` indistinguishable from one the shell expands — and expanding
    a token the shell leaves literal names a path the command never writes."""
    quote: str | None = None
    i = 0
    while i < len(command):
        ch = command[i]
        if ch == "\\" and quote != "'":
            if command[i + 1:i + 2] == "$":
                return True
            i += 2
            continue
        if quote is None and ch in "'\"":
            quote = ch
        elif quote == ch:
            quote = None
        elif ch == "$" and (quote == "'" or command[i + 1:i + 2] in ("'", '"')):
            return True
        i += 1
    return False


def _variable_tracking_is_safe(command: str, tokens: list[str]) -> bool:
    """Whether a textual walk can know a variable's value at each segment: it cannot
    inside a compound command or block (an assignment there runs zero, one or many
    times in an order the walk does not see) nor when a `$` may be literal."""
    return not (_COMPOUND_WORDS.intersection(tokens) or _dollar_may_be_literal(command))


#: Words that run the next word as a command without changing which shell it runs in.
_VERB_WRAPPERS = frozenset({"command", "builtin", "exec", "time"})


def _verb_index(seg: list[str]) -> int | None:
    """Index of the command word of `seg`, past leading `NAME=value` words and the
    `_VERB_WRAPPERS`; `None` when the segment is assignments only."""
    for i, tok in enumerate(seg):
        if not _ASSIGN_RE.match(tok) and tok not in _VERB_WRAPPERS:
            return i
    return None


class _VarTracker:
    """The variables `_abs` may expand while walking one command's segments in order.

    FAIL-CLOSED BY INTENT, NOT BY CONSTRUCTION — a name is in `var_env` only when the
    tracker believes its value is known, because a wrong entry turns a write into a
    protected tree into an allowed one, while a missing entry only keeps the old
    over-inclusive cwd join. Two named residuals remain where a belief can be wrong:

    * A `$X` that the shell resolves LATER than the textual walk does is not seen: text
      the shell runs out of order without a compound word (a `trap` handler string, an
      `alias` body, an `xargs`/`bash -c` string, which are not parsed at all) can read
      a reassigned value where the walk used the earlier one.
    * The seeded `HOME`/`TMPDIR` are premised on the hook and the command's shell having
      been spawned by the same harness; a shell whose profile rewrites either differs
      from the hook's value, and the expansion then names a path the command does not
      write.

    The rules:

    * Seeded with `_INHERITED_VARS` from `os.environ` and nothing else: an arbitrary
      hook variable may be absent or different in a shell a profile initialized.
    * A standalone, unconditional assignment (`o=/tmp/x`, `export o=/tmp/x`) with a
      literal value replaces the entry; the value's own leading `$VAR` is expanded first.
    * Every other mention of an assignment word — a command prefix, after `&&`/`||`, in a
      pipeline or background segment, `+=`, `name[i]=` , a value still holding `$` or a
      backquote — POISONS the name for the rest of the command: it is dropped and never
      trusted again, even by a later literal assignment.
    * A builtin that assigns through an argument (`eval`, `read`, `unset`, `printf -v`,
      `declare -n` ...) discards everything.
    * When `_variable_tracking_is_safe` fails, `var_env` is `None` and nothing is expanded.
    """

    def __init__(self, command: str, tokens: list[str]):
        self.enabled = _variable_tracking_is_safe(command, tokens)
        self.env: dict[str, str] = (
            {name: os.environ[name] for name in _INHERITED_VARS if name in os.environ}
            if self.enabled else {}
        )
        self.poisoned: set[str] = set()

    @property
    def var_env(self) -> dict[str, str] | None:
        return self.env if self.enabled else None

    def _poison(self, name: str) -> None:
        self.poisoned.add(name)
        self.env.pop(name, None)

    def _assign(self, name: str, value: str) -> None:
        value = _try_expand_var(value, self.env)
        if name in self.poisoned or "$" in value or "`" in value:
            self._poison(name)
        else:
            self.env[name] = value

    def observe(self, seg: list[str], before: str | None, after: str | None) -> None:
        """Account for `seg`, which has just been walked, and its separators."""
        if not self.enabled:
            return
        verb_at = _verb_index(seg)
        verb = os.path.basename(seg[verb_at]) if verb_at is not None else ""
        rest = seg[verb_at + 1:] if verb_at is not None else []
        if (
            verb in _OPAQUE_VERBS
            or (verb == "printf" and "-v" in rest)
            or (
                verb in _ASSIGNING_BUILTINS
                and any(t.startswith("-") and not t.startswith("--") and "n" in t for t in rest)
            )
        ):
            self.env.clear()
            return
        builtin = verb in _ASSIGNING_BUILTINS
        prefix = seg if verb_at is None else seg[:verb_at]
        words = prefix + (rest if builtin else [])
        standalone = verb_at is None or (builtin and verb_at == 0)
        unconditional = before in (None, ";") and after not in ("|", "|&", "&")
        for tok in words:
            m = _ASSIGN_RE.match(tok)
            if not m:
                continue
            name, subscript, append, value = m.groups()
            if standalone and unconditional and not subscript and not append:
                self._assign(name, value)
            else:
                self._poison(name)


def _process_substitution_bodies(tokens: list[str]):
    """Yield the tokens between the parentheses of each `<(...)` / `>(...)`, outermost
    first. The operator-exact tokenizer hands them over as `<`, `(`, ..., `)`."""
    for i, tok in enumerate(tokens[:-1]):
        if tok in ("<", ">") and tokens[i + 1] == "(":
            depth = 0
            for j in range(i + 1, len(tokens)):
                depth += (tokens[j] == "(") - (tokens[j] == ")")
                if depth == 0:
                    yield tokens[i + 2:j]
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

    A READ PROCESS SUBSTITUTION `<(cmd)` ADDS NOTHING OF ITS OWN: `<(cmd)` forks
    `cmd` and feeds its stdout into the outer command's argument list, so the outer
    command writes nothing extra due to `<(`. But the nested `cmd` is a command like
    any other, so the segments inside every `<(...)` and `>(...)` are walked as well
    (`diff <(tee canon/f < x) y` still names `canon/f`), and a body whose verb is in
    `_UNMODELED_WRITERS` — a writer this module has no operand grammar for, `dd of=` —
    adds `eff_cwd` as before; the table lists the common interpreters too. A body whose
    verb is outside the table (`<(some-tool --out f)`) is a named residual: the command
    line does not say what it writes, and denying every verb the walker has no grammar
    for would deny every `diff <(git show ...) ...`, the case this module exists to allow.

    `$VAR` expansion in the candidates is `_VarTracker`'s: only `HOME`/`TMPDIR` from
    the hook's environment and literal assignments earlier in the same command
    (`o=/tmp/x; echo > $o/f`), dropped whenever anything could have changed them. That
    tracking is a belief about the shell, not a proof — its residuals (the inherited
    `HOME`/`TMPDIR` premise, text the shell runs out of order) are named in `_VarTracker`.
    Process-substitution bodies are walked without it."""
    command = shell_tokens.neutralize_heredoc_constructs(command)
    extra = [eff_cwd] if ">(" in command else []
    try:
        tokens = shell_tokens.separator_exact_split(command)
    except Exception:
        return extra
    if not tokens:
        return extra
    tracker = _VarTracker(command, tokens)
    targets: list[str] = list(extra)
    for seg, before, after in _segments_with_separators(tokens):
        targets.extend(segment_write_target(seg, eff_cwd, tracker.var_env))
        tracker.observe(seg, before, after)
    for body in _process_substitution_bodies(tokens):
        for seg in split_segments(body):
            targets.extend(segment_write_target(seg, eff_cwd))
            verb_at = _verb_index(seg)
            if verb_at is not None and os.path.basename(seg[verb_at]) in _UNMODELED_WRITERS:
                targets.append(eff_cwd)
    return targets
