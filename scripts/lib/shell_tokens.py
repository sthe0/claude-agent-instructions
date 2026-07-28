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
"""
from __future__ import annotations

import os
import re

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
    current = ""
    parts = []
    while i < len(residue):
        c = residue[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 2
                current += "  "
                continue
            if c == quote:
                quote = None
            current += c
            i += 1
            continue
        if c in "'\"":
            quote = c
            current += c
            i += 1
            continue
        if residue.startswith("$((", i) or residue.startswith("((", i):
            depth += 1
            i += 2
            current += "  "
            continue
        if residue.startswith("))", i):
            depth = max(0, depth - 1)
            i += 2
            current += "  "
            continue
        if residue.startswith("$(", i):
            depth += 1
            i += 2
            current += "  "
            continue
        if residue.startswith("[[", i):
            depth += 1
            i += 2
            current += "  "
            continue
        if residue.startswith("]]", i):
            depth = max(0, depth - 1)
            i += 2
            current += "  "
            continue
        if c == "(":
            depth += 1
            i += 1
            current += c
            continue
        if c == ")":
            depth = max(0, depth - 1)
            i += 1
            current += c
            continue
        if c == "`":
            depth = 1 - depth if depth in (0, 1) else depth
            i += 1
            current += c
            continue
        if depth == 0 and c in ";&\n":
            parts.append(current)
            current = ""
            i += 1
            continue
        current += c
        i += 1
    parts.append(current)
    return sum(1 for part in parts if part.strip()) > 1


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
