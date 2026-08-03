"""Remove here-document bodies and here-string operands from a Bash command.

Difficulty removed: `shlex` is a lexer, not a shell parser. It tokenizes a
here-document body as ordinary words, performs no parameter expansion, and --
critically -- does not RAISE on any of it, so a consumer that scans every token
for a `>` redirect reads an ordinary Markdown blockquote line inside a heredoc
body as shell syntax and refuses the command. A fail-open "any parse doubt
allows" contract does not protect such a consumer, because there is no parse
error to fall open on.

What this module exposes is a NEUTRAL transformation: body text out, everything
else verbatim. It carries no allow/deny policy, because its two consumers need
opposite doubt polarity -- `git_cwd.effective_git_cwd` must never resolve doubt
into a more permissive guess, while the canon guard must ignore stripped data.
Each caller keeps its own decision rule.

RECOGNITION IS A POSITIVE SHAPE, NOT A LIST OF DISQUALIFIERS.

A here-document body is data with respect to the SHELL and code with respect to
whatever program consumes it: `bash <<'EOF'` really executes its body. So the
body is inert exactly when the consuming pipeline is known to treat stdin as
data. Twelve DENY-to-ALLOW regressions were measured against successive rules
that enumerated the contexts in which `<<` must NOT be stripped -- quotes,
arithmetic left shift, a subshell paren desyncing a nesting counter, an
interpreter consumer, a rebound consumer name, a body executed by a later
statement, process substitution, a line continuation. That set is open, and the
enumeration never converged.

Stated positively instead, a `<<` / `<<<` is a strippable operator only when ALL
of the following hold, and an unrecognized construct disqualifies by DEFAULT
rather than by appearing on a list:

  (i)   it is outside every quote and outside a `#` comment;
  (ii)  the command line is built only from plain words, `|`, and `>`/`>>`
        redirects to literal paths -- no process substitution, command
        substitution, arithmetic, conditional, brace group, `&`, `;`, or line
        continuation;
  (iii) no function definition and no alias assignment appears anywhere;
  (iv)  every `|`-separated element of the command line is on `CONSUMERS`;
  (v)   the residue after body removal holds exactly one statement;
  (vi)  the SHELL itself will not expand the body -- a quoted delimiter, or a
        body free of `$`, backtick and backslash;
  (vii) the delimiter word is identifier characters ending at a real bash word
        boundary, so this reader and bash agree which line terminates the body.

Anything outside that shape -- anticipated or not -- strips nothing. That is what
makes the non-widening claim reviewable at all: the question stops being "is any
dangerous construct missing from my list" (unbounded, and it succeeded ten times
across seven review rounds) and becomes "is any construct ON the short recognized
list dangerous" -- a closed set of four syntactic forms plus `CONSUMERS`, which a
reviewer can discharge exhaustively.

There is deliberately NO nesting-depth counter. Every construct that could open
nesting is itself disqualified by clause (ii), so depth at a recognized operator
is zero by construction; an earlier counter caused two of the twelve regressions
on its own. Adding one back is the signal that this rule has drifted.

Clauses (vi) and (vii) were both found by attacking the closed set, and both were
closed by NARROWING a character class rather than by naming another exception. A
fix that names an exception instead is the signal the inversion is being eroded.

Consequences accepted, every one a spurious no-op in the safe direction: a
heredoc nested in `( )` or `$( )`, one in a multi-statement command
(`cd /tmp && cat > x.md <<'EOF'`), one on a continued line, one in a command that
also defines a function, and one with a bare delimiter whose body merely mentions
a `$`, a backtick or a backslash, are all left untouched.

A SECOND neutral transformation lives here for the same reason: `tokenize` and
the vocabulary around it. `shlex.split` reads a shell command in POSIX mode with
no operator grammar at all, so it glues an operator to its neighbour -- `exec
3>f` arrives as `['exec', '3>f']` and `echo hi;cp a f` as `['echo', 'hi;cp', 'a',
'f']`, hiding a redirect and a statement boundary from every scanner downstream.
`shlex.shlex(posix=False, punctuation_chars=True, whitespace_split=True)` splits
both correctly. Its one behavioural difference from the POSIX reading is that it
RETAINS quotes on a quoted word, and that difference is load-bearing in both
directions at once:

  * operator-hood must be decided BEFORE quote removal, because `grep ">" f`
    redirects nothing while `grep > f` does. A quoted operator arrives as the
    word `'">"'`, which is not a member of `REDIRECT_OPS`, so the distinction
    costs no extra code -- but any caller that unquotes before classifying
    throws it away and starts reading a search pattern as a redirect;
  * every consumer doing PATH ARITHMETIC must see the value AFTER quote removal,
    because `cd "/repo/b"` really does move to `/repo/b`. Hence `unquote_word`,
    and hence `was_quoted` for the one case where the answer is neither: a
    leading `~` expands unquoted and stays literal quoted.

Like `strip_heredoc_bodies`, none of it carries allow/deny policy -- `tokenize`
raises on an unbalanced quote exactly where `shlex.split` does, and each caller
decides for itself which way that doubt falls.
"""
from __future__ import annotations

import os
import re
import shlex

# Commands known to treat standard input as inert DATA. An ALLOWLIST, never a
# denylist of interpreters: a denylist naming `bash` and `sh` was measured to
# leave `zsh`, `/bin/bash`, `env bash`, `bash -s` and `cat ... | bash` open, each
# of which genuinely writes. Adding a name here changes the security argument and
# needs the same real-bash oracle evidence as any other change to this module.
CONSUMERS = frozenset({
    "cat", "tee", "head", "tail", "wc", "sort", "uniq", "nl", "rev",
    "base64", "md5sum", "sha256sum",
})

# Characters that genuinely end an unquoted word in bash -- the metacharacter set
# from bash(1) GLOSSARY, "a character that, when unquoted, separates words". Keep
# it as exactly that set: turning it into an ad-hoc enumeration reopens clause
# (vii), whose whole point is that this reader must agree with bash's grammar.
_WORD_END = frozenset(" \t\n|&;()<>")

# A bare-delimiter body is expanded by the SHELL before any consumer starts, so a
# body carrying any of these can write on its own regardless of how inert the
# consumer is.
_EXPANSION_TRIGGERS = ("$", "`", "\\")

# Constructs outside the recognized shape. Each either runs a program the body
# would reach (process / command substitution), changes what `<<` means
# (arithmetic left shift), or moves a statement boundary that clause (v) depends
# on being able to see.
_UNRECOGNIZED = (
    ">(", "<(",
    "$(", "`",
    "$((", "((", "[[",
    "\\\n",
    "{", "}",
    "&",
    ";",
)

_ASSIGNMENT_PREFIX = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
_DELIMITER_WORD = re.compile(r"""(\\)?(['"])?([A-Za-z0-9_]+)(['"])?""")
_DEFINITION = re.compile(
    r"(^|[;&|\n]|\bthen\b|\bdo\b)\s*(function\s+\w+|\w+\s*\(\s*\))"
    r"|(^|[;&|\n])\s*alias\s"
)


def _command_line(command: str) -> str:
    """The command line proper: text up to the first newline NOT preceded by a
    line continuation. A continuation moves where a here-document body begins,
    which is why clause (ii) disqualifies one outright."""
    i = 0
    while i < len(command):
        if command[i] == "\\" and i + 1 < len(command):
            i += 2
            continue
        if command[i] == "\n":
            return command[:i]
        i += 1
    return command


def _consumer_ok(element: str) -> bool:
    """True iff a pipeline element's command word is on `CONSUMERS`, after
    skipping leading `VAR=value` assignments and taking the basename."""
    words = element.split()
    i = 0
    while i < len(words) and _ASSIGNMENT_PREFIX.fullmatch(words[i]):
        i += 1
    if i >= len(words):
        return False
    return os.path.basename(words[i]) in CONSUMERS


def _pipeline_consumers_ok(command: str, pos: int) -> bool:
    """Clause (iv) for the pipeline owning the operator at `pos`. Pipeline-WIDE,
    not first-word-only: `cat <<'EOF' | bash` satisfies a first-word check while
    still executing the body, and was measured doing exactly that."""
    start = max((command.rfind(ch, 0, pos) for ch in (";", "\n", "&")), default=-1)
    end = len(command)
    for ch in (";", "\n"):
        j = command.find(ch, pos)
        if j != -1:
            end = min(end, j)
    pipeline = command[start + 1:end]
    return all(_consumer_ok(part) for part in pipeline.split("|") if part.strip())


def _recognized(command: str) -> bool:
    """Clauses (ii)-(iv) over the whole command: does it match the positively
    understood shape? Clause (iii) deliberately does NOT work out WHICH name a
    definition rebinds -- any definition at all disqualifies -- because chasing
    the rebound name is the enumeration trap this module exists to avoid."""
    if _DEFINITION.search(command):
        return False
    head = _command_line(command)
    if any(token in head for token in _UNRECOGNIZED):
        return False
    return all(_consumer_ok(part) for part in head.split("|"))


def _holds_multiple_statements(residue: str) -> bool:
    """Clause (v): does the residue hold more than one statement, counting only
    unquoted depth-0 separators?

    A body persisted to a file by a genuinely inert consumer can be executed by a
    LATER statement (`cat <<'EOF' > /tmp/s.sh` ... `bash /tmp/s.sh`, measured to
    write). That construction is byte-identical to the primary false positive up
    to the later statement, so the only cheap sound discriminator is whether a
    later statement exists at all -- a body cannot be executed later when there is
    no later.
    """
    depth = 0
    i = 0
    quote = None
    statements = 0  # separated segments that held something
    filled = False  # does the segment being scanned hold something?
    while i < len(residue):
        c = residue[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 2
                continue
            if c == quote:
                quote = None
            filled = True
            i += 1
            continue
        if c in "'\"":
            quote = c
            filled = True
            i += 1
            continue
        if residue.startswith("$((", i) or residue.startswith("((", i):
            depth += 1
            i += 2
            continue
        if residue.startswith("))", i):
            depth = max(0, depth - 1)
            i += 2
            continue
        if residue.startswith("$(", i):
            depth += 1
            i += 2
            continue
        if residue.startswith("[[", i):
            depth += 1
            i += 2
            continue
        if residue.startswith("]]", i):
            depth = max(0, depth - 1)
            i += 2
            continue
        if c == "(":
            depth += 1
            filled = True
            i += 1
            continue
        if c == ")":
            depth = max(0, depth - 1)
            filled = True
            i += 1
            continue
        if c == "`":
            # Backticks do not nest, so one toggles between outside and inside
            # its own substitution -- but only from depth 0 or 1. Deeper, we are
            # already inside some other construct and there is no reliable
            # partner to pair this backtick with, so leave the depth alone.
            depth = 1 - depth if depth in (0, 1) else depth
            filled = True
            i += 1
            continue
        if depth == 0 and c in ";&\n":
            if filled:
                statements += 1
            filled = False
            i += 1
            continue
        filled = filled or not c.isspace()
        i += 1
    if filled:
        statements += 1
    return statements > 1


def _body_inert(delimiter_quoted: bool, text: str) -> bool:
    """Clause (vi). With an UNQUOTED delimiter bash performs parameter expansion,
    command substitution and arithmetic expansion in the body itself, before the
    consumer is even started -- so `cat <<EOF` with `$(echo hi > /elsewhere)`
    inside really writes, however inert the consumer is."""
    return delimiter_quoted or not any(ch in text for ch in _EXPANSION_TRIGGERS)


def _strip_bodies(command: str) -> str:
    """Remove the first here-document body / here-string operand, or return
    `command` unchanged on any doubt. Fail-closed is the safe direction here: the
    caller then sees MORE text than the shell would, never less."""
    out = []
    i = 0
    n = len(command)
    quote = None
    while i < n:
        c = command[i]
        if quote is None and c == "\\":
            out.append(command[i:i + 2])
            i += 2
            continue
        if quote is None and c in "'\"":
            quote = c
            out.append(c)
            i += 1
            continue
        if quote == '"' and c == "\\":
            out.append(command[i:i + 2])
            i += 2
            continue
        if quote and c == quote:
            quote = None
            out.append(c)
            i += 1
            continue
        if quote is None:
            if c == "#" and (i == 0 or command[i - 1] in " \t\n"):
                j = command.find("\n", i)
                j = n if j < 0 else j
                out.append(command[i:j])
                i = j
                continue
            if command.startswith("<<<", i):
                if not _pipeline_consumers_ok(command, i):
                    return command
                j = i + 3
                while j < n and command[j] == " ":
                    j += 1
                if j < n and command[j] in "'\"":
                    operand_quote = command[j]
                    k = command.find(operand_quote, j + 1)
                    if k == -1:
                        return command
                    if not _body_inert(operand_quote == "'", command[j + 1:k]):
                        return command
                    j = k + 1
                    if j < n and command[j] not in _WORD_END:
                        return command  # quoted operand glued to more word
                else:
                    start = j
                    while j < n and command[j] not in _WORD_END:
                        j += 1
                    if not _body_inert(False, command[start:j]):
                        return command
                out.append(" ")
                i = j
                continue
            if command.startswith("<<", i):
                if not _pipeline_consumers_ok(command, i):
                    return command
                j = i + 2
                if j < n and command[j] == "-":
                    j += 1
                while j < n and command[j] == " ":
                    j += 1
                match = _DELIMITER_WORD.match(command[j:])
                if not match:
                    return command
                backslash, open_quote, word, close_quote = match.groups()
                if open_quote and open_quote != close_quote:
                    return command
                delimiter_quoted = bool(backslash) or bool(open_quote)
                j += match.end()
                # (vii) The delimiter must END here in bash's grammar too. Reading
                # `EOF` out of `<<EOF.X` makes this reader overshoot bash's real
                # terminator and swallow the following genuine statement into the
                # body -- and a fail-closed path guarding only the not-found case
                # does not help, because a terminator IS found, at the wrong line.
                if j < n and command[j] not in _WORD_END:
                    return command
                lines = command[j:].split("\n")
                terminator = None
                for index, line in enumerate(lines[1:], start=1):
                    if line.strip() == word:
                        terminator = index
                        break
                if terminator is None:
                    return command
                if not _body_inert(delimiter_quoted, "\n".join(lines[1:terminator])):
                    return command
                out.append(lines[0])
                out.append("\n" + "\n".join(lines[terminator + 1:]))
                return "".join(out)
        out.append(c)
        i += 1
    return command if quote is not None else "".join(out)


def heredoc_body_runs_as_shell(command: str) -> bool:
    """Whether any here-document body in `command` is executed as SHELL.

    `strip_heredoc_bodies` refuses a command for two different reasons -- the
    body is fed to a shell (`bash <<'EOF'`, `cat <<'EOF' | bash`), or the body is
    interpreter data carrying an expansion trigger -- and a caller that scans the
    unstripped residue needs them apart. Only the first makes body text shell
    syntax; reading an interpreter's data as syntax is what turned prose and
    python into 70 measured false denies."""
    for index, _ in enumerate(command):
        if command.startswith("<<", index) and not _pipeline_consumers_ok(command, index):
            return True
    return False


def strip_heredoc_bodies(command: str) -> str:
    """`command` with here-document bodies and here-string operands removed, or
    `command` verbatim when it falls outside the recognized shape.

    Body text only is ever removed; command-line text is returned byte-for-byte.
    That is why clause (iv) need not inspect consumer FLAGS: a path riding the
    command line (`tee -a <p>`, `tee <p>`, `sort -o <p>`, `nl -s <p>`) survives
    body removal untouched and still reaches the caller's scanner.
    """
    if not _recognized(command):
        return command
    residue = _strip_bodies(command)
    if residue != command and _holds_multiple_statements(residue):
        return command
    return residue


# --------------------------------------------------------------------------
# Shell-punctuation tokenizer and the vocabulary built on it
# --------------------------------------------------------------------------

# Tokens that end one element of a shell list. `punctuation_chars=True` emits a
# run of punctuation as a SINGLE token, so `&&`, `||` and `|&` never arrive
# split and this can stay a membership test rather than a prefix scan.
SEPARATORS = frozenset({";", ";;", "&&", "||", "|", "|&", "&"})

# Redirect operators that open their operand for WRITING. `>&` is listed here
# even though it is usually a file-descriptor duplication, because `>&file` is a
# real write; the two readings are separated by the OPERAND, in
# `redirect_write_target`, not by the operator alone.
WRITE_REDIRECT_OPS = frozenset({">", ">>", ">|", ">&", "&>", "&>>", "<>"})
READ_REDIRECT_OPS = frozenset({"<", "<<", "<<<", "<&"})
REDIRECT_OPS = WRITE_REDIRECT_OPS | READ_REDIRECT_OPS

# An operand of `>&` that names a file descriptor rather than a file: a number
# (`2>&1`), a number with a trailing `-` for a move (`2>&1-`), or a bare `-` for
# a close (`2>&-`, `>&-`). Applied to the UNQUOTED operand, so `>&"1"` is read as
# the fd it is and not as a file literally named `1`.
_FD_OPERAND = re.compile(r"\A(?:\d+-?|-)\Z")


def tokenize(command: str) -> list[str]:
    """`command` split on whitespace AND shell punctuation, quotes retained.

    Raises `ValueError` on an unbalanced quote, exactly as `shlex.split` does --
    every existing caller's fail-open path keeps working unchanged.
    """
    lex = shlex.shlex(command, posix=False, punctuation_chars=True)
    lex.whitespace_split = True
    return list(lex)


def was_quoted(token: str) -> bool:
    """Whether `token` carries a quote character.

    Deliberately a test on the RAW token: both questions it answers -- is this an
    operator, and would the shell expand a leading `~` -- are settled before
    quote removal, so a caller that unquotes first can no longer ask them.
    """
    return "'" in token or '"' in token


def unquote_word(token: str) -> str:
    """`token` as the shell would pass it after quote removal, with no expansion.

    Returns `token` verbatim when it does not reduce to exactly one word, which
    is the only safe answer for a caller doing path arithmetic on the result.
    """
    try:
        parts = shlex.split(token)
    except ValueError:
        return token
    return parts[0] if len(parts) == 1 else token


def operand_word(token: str) -> str:
    """`token` as a caller doing PATH ARITHMETIC must read it: quotes removed and
    a leading `~` expanded exactly where the shell would expand it.

    Both halves are decided on the RAW token, which is why this cannot be folded
    into `unquote_word`: quoting suppresses tilde expansion, so `"~/x"` really is
    a directory named `~` while `~/x` is `$HOME/x` (measured, both quote kinds).
    A `~` that is not the word's first character is not expanded either (`a~b`
    stays `a~b`), which is why this is a prefix test rather than a substitution.

    Every consumer that turns a token into a path goes through here. Reading the
    raw `~` as a relative path instead is a false deny with a determinate right
    answer -- unlike an unexpanded `$VAR`, whose value this process genuinely
    does not know -- and it produced 12 of the corpus's false denies.
    """
    word = unquote_word(token)
    if word.startswith("~") and not was_quoted(token):
        return os.path.expanduser(word)
    return word


def redirect_write_target(op: str, operand: str | None) -> str | None:
    """The raw operand token `op` opens for writing, or `None`.

    `None` covers three distinct cases on purpose -- `op` is not a write
    redirect, `op` has no operand at all, and `op` is a `>&` duplicating or
    closing a file descriptor rather than naming a file. All three mean the same
    thing to a caller scanning for write targets: nothing is written here.
    """
    if op not in WRITE_REDIRECT_OPS or operand is None:
        return None
    if op == ">&" and _FD_OPERAND.match(unquote_word(operand)):
        return None
    return operand


def split_segments(tokens: list[str]):
    """Segments of `tokens` split on `SEPARATORS`, each paired with the separator
    token that preceded it (`None` for the first).

    EMPTY segments are kept: a caller that counts segments to recognize a command
    shape needs a trailing `;` to register. A caller that instead iterates
    segments looking for something must therefore tolerate an empty one.
    """
    seg: list[str] = []
    sep: str | None = None
    for tok in tokens:
        if tok in SEPARATORS:
            yield sep, seg
            seg = []
            sep = tok
        else:
            seg.append(tok)
    yield sep, seg


def drop_substitutions(tokens: list[str]) -> list[str]:
    """`tokens` with command- and process-substitution contents removed.

    Under `punctuation_chars=True` a substitution does not survive as one token:
    `$(mktemp)` arrives as `['$', '(', 'mktemp', ')']` and `<(sort a)` as
    `['<(', 'sort', 'a', ')']` (measured 2026-08-03), so its INTERIOR words would
    otherwise be read as operands of the outer command -- `cp /tmp/a $(mktemp)`
    would appear to copy to a file named `mktemp`. A backtick substitution does
    survive inside its word, so any token containing one is dropped whole.

    A bare `(` is a subshell GROUP, not a substitution, and is left alone: it is
    a statement boundary its callers must still be able to see.

    An unbalanced substitution drops every remaining token. That is the
    fail-open direction for both consumers -- fewer operands found, never more.
    """
    out: list[str] = []
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        if tok in ("<(", ">(") or (tok == "$" and i + 1 < n and tokens[i + 1] == "("):
            if tok == "$":
                # a command substitution arrives as `$` then `(`; the depth walk
                # has to start on the token that actually carries the paren
                i += 1
            depth = 0
            while i < n:
                depth += tokens[i].count("(") - tokens[i].count(")")
                i += 1
                if depth <= 0:
                    break
            continue
        if "`" in tok:
            i += 1
            continue
        out.append(tok)
        i += 1
    return out


# --------------------------------------------------------------------------
# Command-prefix stripping
# --------------------------------------------------------------------------

# A leading `VAR=value` assignment, which precedes the command word rather than
# being one. Anchored on an identifier so a bare `--opt=value` never matches.
_ASSIGNMENT = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*=")

# Shell keywords that can head a list element inside a compound command; the
# real command word follows.
_KEYWORD_HEADS = frozenset({"do", "then", "else", "elif", "if", "while", "until"})


class _Wrapper:
    """One wrapper's option grammar: which options are flags, which consume the
    FOLLOWING token, and how many leading positional operands precede the
    wrapped command.

    Both option sets are ALLOWLISTS. A shared "short option that takes a value"
    regex was measured to swallow the command word after `sudo -i`, so an option
    outside both sets stops the strip with `recognized=False` (doubt) rather than
    being guessed at in either direction.
    """

    __slots__ = ("flags", "values", "positionals")

    def __init__(self, flags=(), values=(), positionals=0):
        self.flags = frozenset(flags)
        self.values = frozenset(values)
        self.positionals = positionals


# Read off each tool's own `--help` on 2026-08-03 (util-linux 2.39.3, GNU
# coreutils 9.4, findutils 4.9.0, sudo 1.9.15p5), not from recall.
#
# Three deliberate omissions, each of which therefore yields DOUBT:
#   * `env -C` / `env --chdir` change the working directory, which is the very
#     thing the caller is trying to resolve -- consuming the value would discard
#     it silently. Same for `env -S` / `--split-string`, which re-splits the rest
#     of the line into different arguments than the ones seen here.
#   * `sudo -h` is AMBIGUOUS in sudo's own help: it is listed both as `--help`
#     (a flag) and as `--host=host` (a value option). No reading of it is safe.
#   * `flock -c`, `chrt -p`, `xargs -e`/`-l`, `sudo -e`/`-l`, and every `-V` /
#     `--version` either run no wrapped command at all or take the command as an
#     option value, so the token after them is not the command word.
#
# `doas`, `time` and `command` carry EMPTY option tables: none of the three was
# available to measure here, so only their bare form strips and any option at
# all falls to doubt.
_WRAPPERS = {
    "sudo": _Wrapper(
        flags=("-A", "--askpass", "-b", "--background", "-B", "--bell",
               "-E", "--preserve-env", "-H", "--set-home", "-i", "--login",
               "-K", "--remove-timestamp", "-k", "--reset-timestamp",
               "-n", "--non-interactive", "-P", "--preserve-groups",
               "-S", "--stdin", "-s", "--shell", "-v", "--validate"),
        values=("-C", "--close-from", "-D", "--chdir", "-g", "--group",
                "--host", "-p", "--prompt", "-R", "--chroot", "-r", "--role",
                "-t", "--type", "-T", "--command-timeout", "-U", "--other-user",
                "-u", "--user"),
    ),
    "doas": _Wrapper(),
    "env": _Wrapper(
        flags=("-i", "--ignore-environment", "-v", "--debug",
               "--block-signal", "--default-signal", "--ignore-signal",
               "--list-signal-handling"),
        values=("-u", "--unset"),
    ),
    "time": _Wrapper(),
    "nohup": _Wrapper(),
    "command": _Wrapper(),
    "xargs": _Wrapper(
        flags=("-o", "--open-tty", "-p", "--interactive", "-r",
               "--no-run-if-empty", "-t", "--verbose", "-x", "--exit",
               "--show-limits", "-i", "--replace"),
        values=("-a", "--arg-file", "-d", "--delimiter", "-E", "-I",
                "-L", "--max-lines", "-n", "--max-args", "-P", "--max-procs",
                "-s", "--max-chars", "--process-slot-var"),
    ),
    "nice": _Wrapper(values=("-n", "--adjustment")),
    "stdbuf": _Wrapper(
        values=("-i", "--input", "-o", "--output", "-e", "--error"),
    ),
    "ionice": _Wrapper(
        flags=("-t", "--ignore"),
        values=("-c", "--class", "-n", "--classdata", "-p", "--pid",
                "-P", "--pgid", "-u", "--uid"),
    ),
    # `timeout DURATION COMMAND` and `chrt <prio> <command>` and
    # `flock <file> <command>` each put an operand BEFORE the command word.
    "timeout": _Wrapper(
        flags=("--preserve-status", "--foreground", "-v", "--verbose"),
        values=("-k", "--kill-after", "-s", "--signal"),
        positionals=1,
    ),
    "setsid": _Wrapper(flags=("-c", "--ctty", "-f", "--fork", "-w", "--wait")),
    "chrt": _Wrapper(
        flags=("-b", "--batch", "-d", "--deadline", "-f", "--fifo",
               "-i", "--idle", "-o", "--other", "-r", "--rr",
               "-R", "--reset-on-fork", "-a", "--all-tasks", "-m", "--max",
               "-v", "--verbose"),
        values=("-T", "--sched-runtime", "-P", "--sched-period",
                "-D", "--sched-deadline"),
        positionals=1,
    ),
    "flock": _Wrapper(
        flags=("-s", "--shared", "-x", "--exclusive", "-u", "--unlock",
               "-n", "--nonblock", "-o", "--close", "-F", "--no-fork",
               "--verbose"),
        values=("-w", "--timeout", "-E", "--conflict-exit-code"),
        positionals=1,
    ),
    "watch": _Wrapper(
        flags=("-b", "--beep", "-c", "--color", "-C", "--no-color",
               "-d", "--differences", "-e", "--errexit", "-g", "--chgexit",
               "-p", "--precise", "-r", "--no-rerun", "-t", "--no-title"),
        values=("-q", "--equexit", "-n", "--interval"),
    ),
}

WRAPPERS = frozenset(_WRAPPERS)


def _consume_wrapper(segment: list[str], i: int, spec: _Wrapper):
    """Index of the first token after `spec`'s options and leading operands, or
    `None` when an option outside both allowlists is reached."""
    remaining = spec.positionals
    opts_done = False
    while i < len(segment):
        tok = unquote_word(segment[i])
        if _ASSIGNMENT.match(tok):
            i += 1
            continue
        if not opts_done and tok.startswith("-") and tok != "-":
            if tok == "--":
                opts_done = True
                i += 1
                continue
            head = tok.split("=", 1)[0]
            if "=" in tok and (head in spec.values or head in spec.flags):
                i += 1
                continue
            if tok in spec.flags:
                i += 1
                continue
            if tok in spec.values:
                i += 2
                continue
            return None
        if remaining > 0:
            remaining -= 1
            i += 1
            continue
        return i
    return i


def strip_command_prefix(segment: list[str]) -> tuple[list[str], bool]:
    """`segment` with everything before its command word removed, plus whether
    the strip was fully RECOGNIZED.

    Removed: leading `VAR=value` assignments, shell keywords that can head a list
    element, and wrapper commands together with their options and any operand
    the wrapper takes before the command it runs.

    `recognized=False` means an option shape outside the wrapper's allowlists was
    reached, so which token is the command word is no longer known; the segment
    is returned truncated at that point and the caller must resolve the doubt in
    whichever direction is safe for IT. This module states no preference.
    """
    i = 0
    while i < len(segment):
        tok = unquote_word(segment[i])
        if _ASSIGNMENT.match(tok) or tok in _KEYWORD_HEADS:
            i += 1
            continue
        if tok in _WRAPPERS:
            nxt = _consume_wrapper(segment, i + 1, _WRAPPERS[tok])
            if nxt is None:
                return segment[i + 1:], False
            i = nxt
            continue
        break
    return segment[i:], True
