"""Remove here-document bodies and here-string operands from a Bash command.

Difficulty removed: `shlex` is a lexer, not a shell parser. It tokenizes a
here-document body as ordinary words, performs no parameter expansion, and --
critically -- does not RAISE on any of it, so a consumer that scans every token
for a `>` redirect reads an ordinary Markdown blockquote line inside a heredoc
body as shell syntax and refuses the command. A fail-open "any parse doubt
allows" contract does not protect such a consumer, because there is no parse
error to fall open on.

What this module exposes is a NEUTRAL transformation: body text out, everything
else verbatim. It carries no allow/deny policy, because its downstream readers
need opposite doubt polarity -- `git_cwd.effective_git_cwd` (which reads this
module's output text, not this module itself: it holds no import of
`shell_tokens`) must never resolve doubt into a more permissive guess, while
the canon guard must ignore stripped data. Each caller keeps its own decision
rule. Hiding a construct's tokens can only make `effective_git_cwd` fall back
to `payload_cwd` -- the more restrictive direction -- so the opposite-polarity
concern this paragraph names is satisfied by construction, not merely asserted.

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

The construction-LOCATING walk lives in exactly one place, `_removal_regions`,
which returns `(start, end, collapse_text)` spans in command order. Two
appliers consume that list without re-deriving location logic: `_strip_bodies`
collapses each span (a `<<`/`<<-` body becomes a single `\n`, a `<<<` operand
becomes a single ` `), and `neutralize_heredoc_constructs` instead BLANKS each
span in place -- every character replaced with a space, except an original
`\n`, which stays a `\n` -- so a downstream `shlex` lexer can walk past a
construct without ever trusting its content as absent. A second, independent
span-finding walk is the failure mode this split exists to prevent: a
hand-written span formula is correct only once it is checked against the walk
that already knows where these constructs are, and by then it was pointless to
write a second one.

`heredoc_bodies` reads the same list a third way, reporting the body BYTES to a
caller that needs them rather than needs them gone. A region deliberately spans
the construct's syntax as well as its body, so this reader trims that syntax
back off (`_region_body_text`) -- narrowing an already-located span, never
searching for one, which is why it does not reopen the failure mode above.

Neutralization answers a narrower question than stripping does -- WHERE a
construct is, not whether its body may be trusted away -- so it relaxes two of
the seven clauses and leaves the rest untouched. Clause (iv)'s allowlist widens
from `CONSUMERS` to `CONSUMERS | NON_SHELL_CONSUMERS`: a `python3`/`perl`/
`ruby`/`node` heredoc body is native code to its interpreter and must stay
UNTRUSTED (removing it would be wrong), but it is provably not bash syntax
either, so hiding it from a shell lexer is safe even though removing it is
not. Clause (v) (the residue holds exactly one statement) is dropped
entirely: locating a construct never depended on what follows it. Clauses
(i)-(iii), (vi) and (vii) stay exactly as they are for stripping -- they
establish WHERE the construct is, which both operations need identically, and
relaxing any of them would misidentify a span, not just its trust level.
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

# Interpreters whose heredoc body is native code -- never safe to REMOVE (that
# would change what runs) -- but provably not bash syntax either, so it is safe
# to HIDE from a shell lexer. Consulted only by `neutralize_heredoc_constructs`
# and `heredoc_construct_spans`, as `CONSUMERS | NON_SHELL_CONSUMERS`; never
# merged into `CONSUMERS` itself, whose members' bodies `_strip_bodies` deletes
# outright. Seeded minimally with the interpreters the three measured false
# positives named; `bash`/`sh`/`zsh`/`env` and unknown names stay excluded on
# purpose -- a shell body really is shell syntax.
NON_SHELL_CONSUMERS = frozenset({"python", "python3", "perl", "ruby", "node"})

# Characters that genuinely end an unquoted word in bash -- the metacharacter set
# from bash(1) GLOSSARY, "a character that, when unquoted, separates words". Keep
# it as exactly that set: turning it into an ad-hoc enumeration reopens clause
# (vii), whose whole point is that this reader must agree with bash's grammar.
_WORD_END = frozenset(" \t\n|&;()<>")

# A bare-delimiter body is expanded by the SHELL before any consumer starts, so a
# body carrying any of these can write on its own regardless of how inert the
# consumer is.
_EXPANSION_TRIGGERS = ("$", "`", "\\")

# Characters after which an unquoted `#` opens a comment. Whitespace is the
# obvious member and was for a while the only one, but bash starts a comment
# wherever a WORD starts, and a metacharacter ends the preceding word just as
# whitespace does -- `git status;#c`, `a&#c`, `b|#c` and `(#c)` all comment in
# real bash. The narrower test fed the comment's own inert text through the
# quote/paren/backtick walk, where an unbalanced construct inside it desynced
# the very counters the walk depends on.
_COMMENT_START_AFTER = frozenset(" \t\n;&|()")

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


def command_line(command: str) -> str:
    """The command line proper: text up to the first newline NOT preceded by a
    line continuation. A continuation moves where a here-document body begins,
    which is why clause (ii) disqualifies one outright.

    Public: a second consumer (the permission-self-grant gate) needs this exact
    computation to decide whether an unparseable here-document body is safely
    inert, without re-implementing "where does a body begin" a second time.
    """
    i = 0
    while i < len(command):
        if command[i] == "\\" and i + 1 < len(command):
            i += 2
            continue
        if command[i] == "\n":
            return command[:i]
        i += 1
    return command


def has_process_substitution(command: str) -> bool:
    """True iff `command` contains a process-substitution opening sequence,
    `<(` or `>(`, anywhere in its text.

    Difficulty removed: `<(cmd)`/`>(cmd)` forks and runs `cmd` as a side effect
    of preparing the OUTER command's argument list -- regardless of whether the
    outer command ever succeeds, or the resulting `/dev/fd/N` path is even
    used. Neither `<` nor `>` is a bash statement/pipe separator, so a nested
    write inside `<(...)`/`>(...)` never starts a new segment for
    `bash_write_targets.split_segments` and is never a segment's own command
    word -- it rides along, invisible, as a trailing token of whatever
    ordinary-looking command precedes it (`git commit -F - <(dd if=x of=
    <surface> bs=1) <<'EOF'`). That is true whether the surrounding text lexes
    cleanly or not, so this check has to run BEFORE either consumer trusts its
    own grammar: `_write_incapable_bash_call`'s conjuncts only ever look at a
    segment's first word(s), and `bash_write_targets.command_write_targets`'s
    per-segment verb dispatch never looks past the first `<`/`>` token either
    (`_operands_until_redirect`) -- both blind to a command hiding past that
    point, and a directory-shaped candidate (the `patch`/`git apply`
    convention) does not help here, because a caller like the self-grant gate
    reads a directory as "not a permission surface", i.e. an ALLOW.

    A raw substring scan, not a parse: strictly conservative, matching the
    same textual philosophy `_write_incapable_bash_call` conjunct (E) already
    uses for writer verbs, and it is what lets ONE primitive serve every
    caller regardless of whether that caller's own tokenizer would have
    accepted the text at all. Callers pass text with heredoc bodies already
    stripped (`strip_heredoc_bodies`) so a heredoc BODY merely mentioning the
    two characters -- ordinary prose about a shell redirection -- does not
    trip this by itself; the command line proper is exactly where a process
    substitution the shell actually runs must appear.
    """
    return "<(" in command or ">(" in command


# The full punctuation-character set `shlex.shlex(punctuation_chars=True)` uses
# by default -- bash's own operator characters. `shlex` groups an entire RUN of
# ADJACENT characters from this set into one token (that is what makes it
# split `;` or `|` off a glued WORD in the first place), but it does not know
# that `;`, `|`, `&`, `&&`, `||` and `|&` are themselves distinct bash
# operators from `(`/`)` -- so a separator glued directly against a
# parenthesis with no whitespace (`;(`, `|(`, `&&(`, `;;`, `;)`) comes back as
# ONE token instead of bash's own two-or-more. `_split_punct_run` re-splits
# such a token by the same maximal-munch rule bash's own lexer applies.
_PUNCT_CHARS = frozenset("();<>|&")
_TWO_CHAR_OPERATORS = ("&&", "||", "|&", "<<", ">>")


def _split_punct_run(token: str) -> list[str]:
    """Split a token made ENTIRELY of `_PUNCT_CHARS` into bash's own maximal-
    munch operators -- greedily preferring a recognized two-character operator,
    falling back to the single character otherwise. Applies regardless of
    which operators are glued together (`;(`, `|(`, `&&(`, `;;`, `;)`, `)|`,
    ...): the rule is positional and character-based, not a pattern matched
    against the two shapes a prior review round happened to report."""
    pieces: list[str] = []
    i = 0
    n = len(token)
    while i < n:
        two = token[i:i + 2]
        if two in _TWO_CHAR_OPERATORS:
            pieces.append(two)
            i += 2
            continue
        pieces.append(token[i])
        i += 1
    return pieces


def separator_exact_split(text: str) -> list[str]:
    """Tokenize `text` so bash's own statement/pipe separators (`;`, `|`, `||`,
    `&`, `&&`, `|&`) and its subshell parentheses (`(`, `)`) always come back
    as their own tokens -- even glued to an adjacent word (`echo hi;dd of=x`)
    or glued directly to EACH OTHER (`echo hi;(dd of=x)`) with no whitespace
    anywhere.

    `shlex.split`'s default mode folds an unquoted `;`/`|`/`&` into whatever
    word touches it, because `whitespace_split` treats every non-whitespace,
    non-quote character alike -- `bash_write_targets.split_segments`'s own
    docstring names that as an accepted residual for its ORIGINAL callers, who
    only need SOME segment to answer. A caller that reads "every segment's
    command word is write-incapable" as a soundness claim over the WHOLE line
    cannot rely on that residual: a glued `;dd` or `|bash` hides the second
    command's real word from such a caller entirely, and it would still run
    under real bash regardless of how it lexed.

    `shlex.shlex(punctuation_chars=True)` recognizes exactly bash's own
    operator character set (`(); <>|&`), which removes the WORD-glued residual
    at its root. It does NOT by itself remove the OPERATOR-glued residual --
    the lexer still folds an entire run of adjacent punctuation characters
    into one token, so `;(` is returned as a single token rather than bash's
    own two. `_split_punct_run` re-splits any such all-punctuation token by
    bash's own maximal-munch rule, closing that residual by construction for
    every operator combination this character set admits, not by pattern-
    matching the specific glued shapes a review round happened to report.

    Raises `ValueError` under the same conditions `shlex.split` would (an
    unbalanced quote); a caller wanting the fail-closed behaviour that gives it
    already catches that from `shlex.split` today. That equivalence is why
    `commenters` is cleared below: `shlex.shlex` defaults it to `'#'` while
    `shlex.split` sets it to `''`, and an unmatched default would make a `#`
    anywhere in the text END the token stream -- every statement after it
    silently absent from a caller reading "every segment's command word is
    write-incapable" as a claim over the WHOLE text. A trailing `# comment` on
    a multi-line command is ordinary, so a `#`-truncated stream is a real
    false-ALLOW route, not an exotic one; with `commenters` cleared, `#` is an
    ordinary word character and a `#`-led segment degrades to an unrecognized
    command word, which every caller here answers fail-closed.
    """
    lexer = shlex.shlex(normalize_newline_separators(text), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    tokens: list[str] = []
    for tok in lexer:
        if tok and all(c in _PUNCT_CHARS for c in tok):
            tokens.extend(_split_punct_run(tok))
        else:
            tokens.append(tok)
    return tokens


def normalize_newline_separators(command: str) -> str:
    """`command` with every bash statement-terminating bare newline rewritten to
    a whitespace-padded `;`, so a caller's tokenizer sees the same segment
    boundary bash itself draws there.

    Difficulty removed: neither `shlex.split` (`bash_write_targets.
    command_write_targets`'s general-path tokenizer) nor `separator_exact_split`
    above (the fallback path's tokenizer) ever emits a token for a bare `\n` --
    both fold it into whitespace exactly like a space, unlike `;`/`|`/`&`/`&&`/
    `||`, which DO become their own token once whitespace-surrounded. An
    ordinary, cleanly-lexing multi-line command therefore collapses into ONE
    segment in `bash_write_targets.split_segments`, so only the first
    statement's leading word is ever checked as a writer/write-incapable verb --
    any write-capable statement on a later physical line is invisible to both
    callers. This is the ONE shared primitive both tokenizers route a bare
    newline through, rather than two independent copies of "is this newline a
    separator".

    A depth-0 newline outside every quote, here-document body, and subshell/
    arithmetic/conditional/command-substitution construct is the separator this
    function targets -- the same positive shape `_holds_multiple_statements`
    already recognizes at `;&\n`, applied here to the RAW command rather than a
    heredoc-stripped residue, because a caller may never reach
    `strip_heredoc_bodies`'s residue at all: `_recognized` is false for most
    write-verb commands by clauses (ii)/(iv) of the module docstring, so a
    command carrying an actual writer verb routinely reaches this function with
    its heredoc bodies still physically present. A newline inside a here-
    document BODY is inert with respect to statement separation -- it is the
    body's own DATA, not bash grammar -- so this function locates and skips
    every heredoc body span with the same delimiter/terminator walk
    `_strip_bodies` uses, deliberately INDEPENDENT of `_pipeline_consumers_ok`:
    whether a body is safe to REMOVE is a different question from where its
    span ends, and this function never removes a body byte, only rewrites a
    statement-separator newline found OUTSIDE any such span.

    TWO NESTING AXES, NOT ONE COUNTER. A backtick substitution does not nest, so
    a backtick toggles between outside and inside its own substitution -- but
    that toggle is a different AXIS from `$()`/`()`/`(())`/`[[]]` depth, and one
    counter carrying both desyncs the moment the two interleave. A backtick
    opened inside `$( )` and closed after the `)` left the shared counter at 1
    with every construct on the line balanced, so every later statement-
    separating newline failed the depth test and was not rewritten -- the
    write-hiding direction this function exists to remove. Separate state -- a
    COUNT for the axis that nests, a BOOLEAN for the one that cannot -- makes
    each answer only for itself, and the gate is their CONJUNCTION: a newline
    separates only where both say top level, which is what the shared counter
    already did for input the two axes never interleave in.

    Fail-closed on doubt, in this function's OWN safe direction: a heredoc
    operator whose delimiter or terminator cannot be located stops being
    tracked as a body at all, so every newline past that point falls back to
    the ordinary depth-0 rule -- MORE segmentation, not less, the safe
    direction for a write-detection primitive (it can only add scrutiny a
    caller applies on top, never remove scrutiny that was already there).
    """
    out: list[str] = []
    i = 0
    n = len(command)
    quote = None
    depth = 0
    in_backtick = False
    while i < n:
        c = command[i]
        if quote:
            if c == "\\" and quote == '"':
                out.append(command[i:i + 2])
                i += 2
                continue
            if c == quote:
                quote = None
            out.append(c)
            i += 1
            continue
        if c == "\\":
            out.append(command[i:i + 2])
            i += 2
            continue
        if c in "'\"":
            quote = c
            out.append(c)
            i += 1
            continue
        at_top = depth == 0 and not in_backtick
        if at_top and c == "#" and (i == 0 or command[i - 1] in _COMMENT_START_AFTER):
            j = command.find("\n", i)
            j = n if j < 0 else j
            out.append(command[i:j])
            i = j
            continue
        if command.startswith("$((", i) or command.startswith("((", i):
            depth += 1
            out.append(command[i:i + 2])
            i += 2
            continue
        if command.startswith("))", i):
            depth = max(0, depth - 1)
            out.append(command[i:i + 2])
            i += 2
            continue
        if command.startswith("$(", i):
            depth += 1
            out.append(command[i:i + 2])
            i += 2
            continue
        if command.startswith("[[", i):
            depth += 1
            out.append(command[i:i + 2])
            i += 2
            continue
        if command.startswith("]]", i):
            depth = max(0, depth - 1)
            out.append(command[i:i + 2])
            i += 2
            continue
        if c == "(":
            depth += 1
            out.append(c)
            i += 1
            continue
        if c == ")":
            depth = max(0, depth - 1)
            out.append(c)
            i += 1
            continue
        if c == "`":
            in_backtick = not in_backtick
            out.append(c)
            i += 1
            continue
        if at_top and command.startswith("<<<", i):
            end = _skip_here_string_operand(command, i)
            out.append(command[i:end])
            i = end
            continue
        if at_top and command.startswith("<<", i):
            end = _skip_heredoc_span(command, i)
            out.append(command[i:end])
            i = end
            continue
        if at_top and c == "\n":
            out.append(" ; ")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _skip_here_string_operand(command: str, i: int) -> int:
    """End index of the `<<<` operator's operand, for
    `normalize_newline_separators` -- the operand is a single shell word, never
    a statement boundary, so it is copied through unchanged regardless of what
    it contains. Fail-closed on an unterminated quoted operand: returns `i + 3`
    so the caller's walk resumes right after the bare operator."""
    n = len(command)
    j = i + 3
    while j < n and command[j] == " ":
        j += 1
    if j < n and command[j] in "'\"":
        operand_quote = command[j]
        k = command.find(operand_quote, j + 1)
        return k + 1 if k != -1 else i + 3
    start = j
    while j < n and command[j] not in _WORD_END:
        j += 1
    return j if j > start else i + 3


def _skip_heredoc_span(command: str, i: int) -> int:
    """End index of a `<<`/`<<-` heredoc's delimiter line, body and terminator
    line, for `normalize_newline_separators` -- every newline in that span is
    heredoc-body data, never a statement separator, so the whole span is
    copied through unchanged. Mirrors `_strip_bodies`'s own delimiter/
    terminator walk, deliberately without its `_pipeline_consumers_ok` gate:
    where the body ends is a syntactic fact, independent of whether removing
    it would be safe. Fail-closed on doubt in this function's own direction --
    see `normalize_newline_separators`'s docstring."""
    n = len(command)
    j = i + 2
    if j < n and command[j] == "-":
        j += 1
    while j < n and command[j] == " ":
        j += 1
    match = _DELIMITER_WORD.match(command[j:])
    if not match:
        return i + 2
    backslash, open_quote, word, close_quote = match.groups()
    if open_quote and open_quote != close_quote:
        return i + 2
    j += match.end()
    if j < n and command[j] not in _WORD_END:
        return i + 2
    lines = command[j:].split("\n")
    terminator = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == word:
            terminator = index
            break
    if terminator is None:
        return i + 2
    return j + len("\n".join(lines[:terminator + 1]))


def _consumer_ok(element: str, consumers: frozenset[str] = CONSUMERS) -> bool:
    """True iff a pipeline element's command word is on `consumers`, after
    skipping leading `VAR=value` assignments and taking the basename."""
    words = element.split()
    i = 0
    while i < len(words) and _ASSIGNMENT_PREFIX.fullmatch(words[i]):
        i += 1
    if i >= len(words):
        return False
    return os.path.basename(words[i]) in consumers


def _pipeline_consumers_ok(command: str, pos: int, consumers: frozenset[str] = CONSUMERS) -> bool:
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
    return all(_consumer_ok(part, consumers) for part in pipeline.split("|") if part.strip())


def _recognized(command: str, consumers: frozenset[str] = CONSUMERS) -> bool:
    """Clauses (ii)-(iv) over the whole command: does it match the positively
    understood shape? Clause (iii) deliberately does NOT work out WHICH name a
    definition rebinds -- any definition at all disqualifies -- because chasing
    the rebound name is the enumeration trap this module exists to avoid."""
    if _DEFINITION.search(command):
        return False
    head = command_line(command)
    if any(token in head for token in _UNRECOGNIZED):
        return False
    return all(_consumer_ok(part, consumers) for part in head.split("|"))


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


def first_heredoc_body_shell_inert(command: str) -> bool | None:
    """Clause (vi) alone, for the FIRST `<<` / `<<-` / `<<<` operator on `command`'s
    command line proper -- None if no such operator is found outside quotes (there
    is no body for the shell to expand).

    Deliberately independent of clauses (iii)/(iv): a caller here is asking "would
    the SHELL itself expand this body before any consumer starts", not "is it safe
    to remove this text" -- `strip_heredoc_bodies` answers the latter and requires
    the whole recognized shape, including a consumer on `CONSUMERS`, before it will
    say anything at all. A write-incapable command word (`git commit`, not on
    `CONSUMERS`) still leaves the body reaching the shell exactly the same way a
    `cat` would, so this question must be answerable without that gate.

    Mirrors `_strip_bodies`'s own quote-tracking walk and delimiter parsing, since
    that is the one piece with no cheaper answer -- unlike the command-line
    boundary (`command_line`, exposed above for the same reason), the walk itself
    is not otherwise exposed. Fail-closed on doubt: a delimiter or terminator this
    walk cannot locate returns False, the same footing `_strip_bodies` stands on.
    """
    line = command_line(command)
    n = len(line)
    i = 0
    quote = None
    while i < n:
        c = line[i]
        if quote is None and c == "\\":
            i += 2
            continue
        if quote is None and c in "'\"":
            quote = c
            i += 1
            continue
        if quote == '"' and c == "\\":
            i += 2
            continue
        if quote and c == quote:
            quote = None
            i += 1
            continue
        if quote is None:
            if line.startswith("<<<", i):
                j = i + 3
                while j < n and line[j] == " ":
                    j += 1
                if j < n and line[j] in "'\"":
                    operand_quote = line[j]
                    k = line.find(operand_quote, j + 1)
                    if k == -1:
                        return False
                    if k + 1 < n and line[k + 1] not in _WORD_END:
                        return False  # quoted operand glued to more word
                    return _body_inert(operand_quote == "'", line[j + 1:k])
                start = j
                while j < n and line[j] not in _WORD_END:
                    j += 1
                return _body_inert(False, line[start:j])
            if line.startswith("<<", i):
                j = i + 2
                if j < n and line[j] == "-":
                    j += 1
                while j < n and line[j] == " ":
                    j += 1
                match = _DELIMITER_WORD.match(line[j:])
                if not match:
                    return False
                backslash, open_quote, word, close_quote = match.groups()
                if open_quote and open_quote != close_quote:
                    return False
                delimiter_quoted = bool(backslash) or bool(open_quote)
                end = j + match.end()
                if end < n and line[end] not in _WORD_END:
                    return False
                lines = command[len(line):].split("\n")
                terminator = None
                for index, ln in enumerate(lines[1:], start=1):
                    if ln.strip() == word:
                        terminator = index
                        break
                if terminator is None:
                    return False
                return _body_inert(delimiter_quoted, "\n".join(lines[1:terminator]))
        i += 1
    return None


def first_heredoc_consumes_entire_tail(command: str) -> bool:
    """True iff the FIRST `<<` / `<<-` / `<<<` operator on `command`'s command line
    proper accounts for the ENTIRE remainder of `command` -- its operand (`<<<`) or its
    body plus terminator line (`<<`/`<<-`) is the last thing in `command`, with no
    further text (a second command, a later heredoc) after it. Vacuously True when
    there is no such operator at all, provided nothing follows the command line proper
    either. Fail-closed on doubt, the same footing `first_heredoc_body_shell_inert`
    stands on: an operator this walk cannot locate a terminator for returns False.

    Exists for a caller reasoning about the command-line-proper region in isolation
    (`_write_incapable_bash_call`'s conjuncts (A)/(B) in hook-guard-permission-self-
    grant.py) that must not silently ignore a SECOND command bash would run after the
    first heredoc's terminator -- that text is invisible to `shlex` and to every
    conjunct that trusts the lexed command-line-proper alone, so nothing upstream of
    this function would otherwise ever look at it.
    """
    line = command_line(command)
    n = len(line)
    i = 0
    quote = None
    while i < n:
        c = line[i]
        if quote is None and c == "\\":
            i += 2
            continue
        if quote is None and c in "'\"":
            quote = c
            i += 1
            continue
        if quote == '"' and c == "\\":
            i += 2
            continue
        if quote and c == quote:
            quote = None
            i += 1
            continue
        if quote is None:
            if line.startswith("<<<", i):
                return command[len(line):].strip() == ""
            if line.startswith("<<", i):
                j = i + 2
                if j < n and line[j] == "-":
                    j += 1
                while j < n and line[j] == " ":
                    j += 1
                match = _DELIMITER_WORD.match(line[j:])
                if not match:
                    return False
                backslash, open_quote, word, close_quote = match.groups()
                if open_quote and open_quote != close_quote:
                    return False
                end = j + match.end()
                if end < n and line[end] not in _WORD_END:
                    return False
                lines = command[len(line):].split("\n")
                terminator = None
                for index, ln in enumerate(lines[1:], start=1):
                    if ln.strip() == word:
                        terminator = index
                        break
                if terminator is None:
                    return False
                return all(ln.strip() == "" for ln in lines[terminator + 1:])
        i += 1
    return command[len(line):].strip() == ""


def _removal_regions(command: str, consumers: frozenset[str]) -> list[tuple[int, int, str]] | None:
    """Locate every here-document / here-string construct removable under
    clauses (i)-(iv), (vi) and (vii), as `(start, end, collapse_text)` triples
    in command order -- `None` on any doubt, discarding whatever was found so
    far, since the walk is all-or-nothing. This is the ONE construction-locating
    walk `_strip_bodies` and `neutralize_heredoc_constructs` both apply; it never
    itself decides what a span becomes, only where it is.

    Mirrors `_strip_bodies`'s original character-by-character scan exactly --
    same quote/backslash/comment handling, same doubt points -- except it
    records spans instead of building output text, and clause (iv) is checked
    against the caller's `consumers` rather than the module-level `CONSUMERS`.
    A `<<<` records one region and the walk continues; a `<<`/`<<-` records two
    regions (the operator+delimiter token, and the body+terminator line) and
    the walk ends there, exactly as the original ends its scan at the first
    `<<`/`<<-` it removes.
    """
    regions: list[tuple[int, int, str]] = []
    i = 0
    n = len(command)
    quote = None
    while i < n:
        c = command[i]
        if quote is None and c == "\\":
            i += 2
            continue
        if quote is None and c in "'\"":
            quote = c
            i += 1
            continue
        if quote == '"' and c == "\\":
            i += 2
            continue
        if quote and c == quote:
            quote = None
            i += 1
            continue
        if quote is None:
            if c == "#" and (i == 0 or command[i - 1] in " \t\n"):
                j = command.find("\n", i)
                j = n if j < 0 else j
                i = j
                continue
            if command.startswith("<<<", i):
                if not _pipeline_consumers_ok(command, i, consumers):
                    return None
                j = i + 3
                while j < n and command[j] == " ":
                    j += 1
                if j < n and command[j] in "'\"":
                    operand_quote = command[j]
                    k = command.find(operand_quote, j + 1)
                    if k == -1:
                        return None
                    if not _body_inert(operand_quote == "'", command[j + 1:k]):
                        return None
                    j = k + 1
                    if j < n and command[j] not in _WORD_END:
                        return None  # quoted operand glued to more word
                else:
                    start = j
                    while j < n and command[j] not in _WORD_END:
                        j += 1
                    if not _body_inert(False, command[start:j]):
                        return None
                regions.append((i, j, " "))
                i = j
                continue
            if command.startswith("<<", i):
                if not _pipeline_consumers_ok(command, i, consumers):
                    return None
                j = i + 2
                if j < n and command[j] == "-":
                    j += 1
                while j < n and command[j] == " ":
                    j += 1
                match = _DELIMITER_WORD.match(command[j:])
                if not match:
                    return None
                backslash, open_quote, word, close_quote = match.groups()
                if open_quote and open_quote != close_quote:
                    return None
                delimiter_quoted = bool(backslash) or bool(open_quote)
                j += match.end()
                # (vii) The delimiter must END here in bash's grammar too. Reading
                # `EOF` out of `<<EOF.X` makes this reader overshoot bash's real
                # terminator and swallow the following genuine statement into the
                # body -- and a fail-closed path guarding only the not-found case
                # does not help, because a terminator IS found, at the wrong line.
                if j < n and command[j] not in _WORD_END:
                    return None
                lines = command[j:].split("\n")
                terminator = None
                for index, line in enumerate(lines[1:], start=1):
                    if line.strip() == word:
                        terminator = index
                        break
                if terminator is None:
                    return None
                if not _body_inert(delimiter_quoted, "\n".join(lines[1:terminator])):
                    return None
                # Region A: the operator+delimiter token itself (`<<'EOF'`).
                # Region B: the body+terminator line, plus its trailing newline
                # when one exists in `command` -- `lines[0]` (redirect targets
                # etc. on the operator's own line) sits UNCOVERED between them
                # and survives verbatim, exactly as the original left it.
                line0_end = j + len(lines[0])
                pre_len = len("\n".join(lines[:terminator + 1]))
                pos_after_terminator = j + pre_len
                body_end = pos_after_terminator + 1 if pos_after_terminator < n else pos_after_terminator
                regions.append((i, j, ""))
                regions.append((line0_end, body_end, "\n"))
                return regions
        i += 1
    return regions if quote is None else None


def _apply_regions(command: str, regions: list[tuple[int, int, str]]) -> str:
    """`command` with every `(start, end, collapse_text)` region replaced by its
    `collapse_text`, and every byte outside a region copied verbatim."""
    out = []
    pos = 0
    for start, end, collapse in regions:
        out.append(command[pos:start])
        out.append(collapse)
        pos = end
    out.append(command[pos:])
    return "".join(out)


def _blank_region(command: str, start: int, end: int) -> str:
    """`command[start:end]` with every character replaced by a space, except an
    original `\\n`, which stays a `\\n` -- length-preserving, unlike the collapse
    text `_removal_regions` computes for removal."""
    return "".join(ch if ch == "\n" else " " for ch in command[start:end])


# The three `collapse_text` values `_removal_regions` emits, read back as a
# region's KIND. It emits no others, so matching on them discriminates its
# output totally rather than heuristically.
_HERE_STRING_REGION = " "
_HEREDOC_OPERATOR_REGION = ""
_HEREDOC_BODY_REGION = "\n"


def _region_body_text(command: str, start: int, end: int, collapse: str) -> str | None:
    """The body text inside one `_removal_regions` region, or `None` for a
    region that carries no body (a here-document's operator+delimiter token).

    A region spans the construct's SYNTAX as well as its body -- `<<<` and the
    operand's quotes, the newline/terminator line/trailing newline bracketing a
    here-document body -- because removal and blanking both need the whole
    construct gone. Recovering the body alone therefore means trimming that
    syntax back off, and each branch below reproduces exactly the slice
    `_removal_regions` already handed to `_body_inert` for that construct,
    reached from the region bounds instead of from the walk's local variables.

    This is trimming, not locating: it never searches `command` for a
    construct, only narrows one the single walk has already found, so it does
    not reintroduce the second span-finding walk the module docstring forbids.
    """
    if collapse == _HEREDOC_OPERATOR_REGION:
        return None
    if collapse == _HERE_STRING_REGION:
        operand = command[start + len("<<<"):end].lstrip(" ")
        if operand[:1] in ("'", '"'):
            return operand[1:-1]
        return operand
    inner = command[start + 1:end]  # drop the newline that opens the region
    if inner.endswith("\n"):
        inner = inner[:-1]  # drop the newline after the terminator line
    body, _, _terminator_line = inner.rpartition("\n")
    return body


def _strip_bodies(command: str) -> str:
    """Remove the first here-document body / here-string operand, or return
    `command` unchanged on any doubt. Fail-closed is the safe direction here: the
    caller then sees MORE text than the shell would, never less."""
    regions = _removal_regions(command, CONSUMERS)
    return command if regions is None else _apply_regions(command, regions)


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


def heredoc_bodies(command: str) -> list[str]:
    """Every here-document body / here-string operand `strip_heredoc_bodies`
    would remove from `command`, in extraction order -- or `[]` when `command`
    falls outside the recognized shape, or when nothing is actually stripped.

    Reads the SAME `_removal_regions` walk under the SAME `CONSUMERS` that
    `strip_heredoc_bodies` removes, and repeats clause (v) against the same
    residue, so this extractor and the stripper cannot disagree about which
    bytes are body text. A caller that needs the BYTES (rather than merely
    needing them gone) uses this instead of re-deriving the recognizer against
    `strip_heredoc_bodies`'s return value.

    A non-empty region list always shortens `command` (every region replaces at
    least its operator with shorter collapse text), so "nothing was stripped"
    and "no regions" are the same condition -- checked here as the latter.
    """
    if not _recognized(command):
        return []
    regions = _removal_regions(command, CONSUMERS)
    if not regions:
        return []
    if _holds_multiple_statements(_apply_regions(command, regions)):
        return []
    bodies = (_region_body_text(command, *region) for region in regions)
    return [body for body in bodies if body is not None]


def neutralize_heredoc_constructs(command: str) -> str:
    """`command` with every recognized here-document body / here-string operand
    BLANKED (each character replaced with a space, an original `\\n` preserved),
    or `command` verbatim when it falls outside the recognized shape. Unlike
    `strip_heredoc_bodies`, length is always preserved, so a byte offset outside
    a blanked span still means what it meant in `command`.

    Answers WHERE a construct is, not whether its body may be trusted, so it
    widens clause (iv) to `CONSUMERS | NON_SHELL_CONSUMERS` and drops clause (v)
    (see the module docstring) -- a heredoc a later statement goes on to execute
    is still hidden from `shlex`, because hiding it does not require trusting
    it, only locating it.
    """
    consumers = CONSUMERS | NON_SHELL_CONSUMERS
    if not _recognized(command, consumers):
        return command
    regions = _removal_regions(command, consumers)
    if regions is None:
        return command
    out = []
    pos = 0
    for start, end, _collapse in regions:
        out.append(command[pos:start])
        out.append(_blank_region(command, start, end))
        pos = end
    out.append(command[pos:])
    return "".join(out)


def heredoc_construct_spans(command: str) -> list[tuple[int, int]]:
    """`[(start, end), ...]` of every here-document / here-string construct
    `neutralize_heredoc_constructs` would blank in `command`, in command order,
    or `[]` when it falls outside the recognized shape. Same widened clause
    (iv) and dropped clause (v) as the neutralizer -- this is its span view,
    not the stricter `strip_heredoc_bodies` shape."""
    consumers = CONSUMERS | NON_SHELL_CONSUMERS
    if not _recognized(command, consumers):
        return []
    regions = _removal_regions(command, consumers)
    if regions is None:
        return []
    return [(start, end) for start, end, _collapse in regions]
