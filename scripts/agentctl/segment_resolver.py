"""Per-segment command resolution: what each `&&` / `;` / `|` piece of a Bash
command line changes, and what stays undecided.

Difficulty removed: `tool_contracts.resolve_command` is all-or-nothing -- one
`git -C <dir>`, one redirect, one `cd` or one `python3 -c` anywhere makes the
WHOLE command unresolved, so the PreToolUse grant hook could neither cover the
commands agents really run (`git -C <venue> commit`, `cd out && mv a b`,
`cmd > file`) nor deny a resolved write outside the grants. This module splits
the line into segments and gives each its own verdict, so the hook reads the
effects of the decidable parts and falls through only for the rest.

The resolver FAILS CLOSED. An under-resolved segment costs one permission
prompt; an over-resolved one silently auto-allows an effect nobody granted. So
a form resolves only when it is on an explicit allow-list whose full effect is
nameable (`GIT_WRITE_FORMS` for git, the writer/awk/interpreter tables below,
the contract table for everything else); every other form is unresolved:
  * git: add / commit / restore / rm / mv / merge / reset / checkout / switch in
    the listed flag shapes, push to a literal ref, read-only subcommands -- and
    only in the venue's own repository (same git common dir: another repository
    has its own refs, hooks and config). Stash, rebase, pull, am, cherry-pick,
    commit-ish reset, branch-creating or ref-forcing checkout/switch flags stay
    unresolved.
  * a commit/merge (or a push of `HEAD`) is attributed to the current branch only
    when no other segment of the line -- before it, or running concurrently with
    it under `&` or `|` -- is a branch switch, unresolved or opaque.
  * the working directory is known only after a resolved or opaque segment (an
    unresolved one may be a shell function or builtin that moved it); a `cd` that
    is conditional (reached via `||` or `&`, inside a list holding `||`,
    backgrounded, in a pipeline, with -L/-P, or through a symlink-sensitive `..`)
    leaves it unknown. An opaque segment whose cwd is unknown is unresolved.
  * syntax the scanner cannot read (`$'..'`, a command substitution inside an
    unquoted heredoc body, an unterminated quote) is residual-syntax.

It is a separate module from `resolve_command` on purpose: that function's
behaviour is pinned for every existing caller (digest-neutrality differential),
and segment mode is looser than its closed grammar by design (git writes,
redirects, `cd` re-basing). It reuses the reviewed git grammar and the contract
table through `tool_contracts`' public names rather than duplicating them.

Why it has its own scanner: `lib/bash_write_targets.py`, `lib/git_cwd.py` and
`grants._raw_top_level_segments` are best-effort lexical guards -- a miss there
degrades to what the harness would do anyway. A segment verdict here can AUTHORISE
a call, so its scanner must refuse anything it cannot read exactly, which those
splitters do not. Converging the four on this scanner is a named follow-up.

A segment's status is one of
  resolved  -- `resources` lists every effect (possibly none);
  opaque    -- the segment runs code this module cannot read (`opaque_kind`
               python-c | stdin-body | script-file) in a KNOWN cwd; its redirect
               targets are still listed, the code's own effects are for a later
               judge;
  unresolved -- undecidable (`reason_class` is one of the canonical tags).
A consumer may act on a resolved segment's effects, but must treat any
opaque or unresolved segment as "the line is not fully decided".
"""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field

from lib import widening_targets

from . import resources, tool_contracts


class _ScanError(Exception):
    pass


@dataclass
class _Tok:
    text: str
    nonlit: bool = False
    #: an expansion or glob outside double quotes: the shell may word-split or glob it
    bare_nonlit: bool = False


@dataclass
class _Redirect:
    op: str
    fd: str | None
    target: _Tok | None = None


@dataclass
class _RawSegment:
    start: int
    op_before: str
    end: int = 0
    words: list[_Tok] = field(default_factory=list)
    redirects: list[_Redirect] = field(default_factory=list)
    nested: str | None = None
    extra: str = ""


_PIPE_OPS = frozenset({"|", "|&"})
_ASYNC_OPS = frozenset({"&", "|", "|&"})
_CONTINUATION_OPS = frozenset({"&&", "||", "|", "|&"})
_WRITE_REDIRECTS = frozenset({">", ">>", ">|", "&>", "&>>", "<>"})
_HEREDOC_OPS = frozenset({"<<", "<<-"})
_NO_EFFECT_DEVICES = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"})


def _skip_substitution(text: str, i: int) -> int:
    """`i` is the index of the `(` of a `$(`/`<(`/`>(`; returns the index after its matching `)`."""
    depth = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            j = text.find("'", i + 1)
            if j < 0:
                raise _ScanError("unterminated quote inside a command substitution")
            i = j + 1
            continue
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            i += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise _ScanError("unterminated command substitution")


def _read_heredoc(text: str, start: int, delimiter: str, strip_tabs: bool) -> tuple[str, int]:
    n = len(text)
    i = start
    body: list[str] = []
    while i < n:
        j = text.find("\n", i)
        line_end = n if j < 0 else j
        line = text[i:line_end]
        following = n if j < 0 else j + 1
        if (line.lstrip("\t") if strip_tabs else line) == delimiter:
            return "\n".join(body), following
        body.append(line)
        i = following
    return "\n".join(body), n


class _Scanner:
    """Quote-, heredoc- and operator-aware split of a command line into
    `_RawSegment`s. Never executes or expands anything: an expansion it cannot
    read marks the token non-literal, a nested execution marks the segment."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.n = len(text)
        self.i = 0
        self.segments: list[_RawSegment] = []
        self.cur = _RawSegment(start=0, op_before="")
        self.buf: list[str] = []
        self.started = False
        self.quoted = False
        self.nonlit = False
        self.bare = False
        self.redirect: _Redirect | None = None
        self.heredocs: list[tuple[_Redirect, _RawSegment, str, bool]] = []

    def _reset_word(self) -> None:
        self.buf = []
        self.started = self.quoted = self.nonlit = self.bare = False

    def _add(self, ch: str, *, quoted: bool = False) -> None:
        self.buf.append(ch)
        self.started = True
        if quoted:
            self.quoted = True

    def _mark_bare_nonlit(self) -> None:
        self.nonlit = self.bare = True

    def _flush(self) -> None:
        if not self.started:
            return
        tok = _Tok("".join(self.buf), self.nonlit, self.bare)
        if self.redirect is not None:
            self.redirect.target = tok
            if self.redirect.op in _HEREDOC_OPS:
                self.heredocs.append((self.redirect, self.cur, tok.text, self.quoted))
            self.redirect = None
        else:
            self.cur.words.append(tok)
        self._reset_word()

    def _end_segment(self, op: str, end: int, resume: int) -> None:
        self.cur.end = end
        self.segments.append(self.cur)
        self.cur = _RawSegment(start=resume, op_before=op)

    def run(self) -> list[_RawSegment]:
        t = self.text
        while self.i < self.n:
            c = t[self.i]
            if c == "\\":
                nxt = t[self.i + 1] if self.i + 1 < self.n else ""
                if nxt != "\n":
                    self._add(nxt or "\\", quoted=True)
                self.i += 2
            elif c == "'":
                j = t.find("'", self.i + 1)
                if j < 0:
                    raise _ScanError("unterminated single quote")
                self.buf.append(t[self.i + 1 : j])
                self.started = self.quoted = True
                self.i = j + 1
            elif c == '"':
                self._double_quoted()
            elif c == "$":
                self._dollar()
            elif c == "`":
                self._backtick()
            elif c in " \t":
                self._flush()
                self.i += 1
            elif c == "\n":
                self._newline()
            elif c == "#" and not self.started:
                j = t.find("\n", self.i)
                self.i = self.n if j < 0 else j
            elif c in ";&|<>()":
                self._operator()
            else:
                if c in "*?[{" or (c == "~" and not self.started):
                    self._mark_bare_nonlit()
                self._add(c)
                self.i += 1
        self._flush()
        if self.redirect is not None:
            raise _ScanError("redirection without a target")
        self.cur.end = self.n
        self.segments.append(self.cur)
        return [s for s in self.segments if s.words or s.redirects or s.nested]

    def _double_quoted(self) -> None:
        t, n = self.text, self.n
        self.started = self.quoted = True
        i = self.i + 1
        while True:
            if i >= n:
                raise _ScanError("unterminated double quote")
            c = t[i]
            if c == '"':
                break
            if c == "\\":
                nxt = t[i + 1] if i + 1 < n else ""
                if nxt in ('$', '"', "\\", "`"):
                    self.buf.append(nxt)
                    i += 2
                elif nxt == "\n":
                    i += 2
                else:
                    self.buf.append("\\")
                    i += 1
                continue
            if c == "$":
                self.nonlit = True
                if t[i + 1 : i + 2] == "(":
                    self.cur.nested = "command substitution"
                    j = _skip_substitution(t, i + 1)
                    self.buf.append(t[i:j])
                    i = j
                    continue
            if c == "`":
                self.cur.nested = "command substitution"
                j = t.find("`", i + 1)
                if j < 0:
                    raise _ScanError("unterminated backtick")
                self.buf.append(t[i : j + 1])
                i = j + 1
                continue
            self.buf.append(c)
            i += 1
        self.i = i + 1

    def _dollar(self) -> None:
        t = self.text
        nxt = t[self.i + 1 : self.i + 2]
        self.started = True
        self._mark_bare_nonlit()
        if nxt == "(":
            self.cur.nested = "command substitution"
            j = _skip_substitution(t, self.i + 1)
            self.buf.append(t[self.i : j])
            self.i = j
        elif nxt == "'":
            raise _ScanError("ANSI-C quoting $'...' is not parsed: its escapes decide where the quote ends")
        else:
            self.buf.append("$")
            self.i += 1

    def _backtick(self) -> None:
        j = self.text.find("`", self.i + 1)
        if j < 0:
            raise _ScanError("unterminated backtick")
        self.cur.nested = "command substitution"
        self.buf.append(self.text[self.i : j + 1])
        self.started = True
        self._mark_bare_nonlit()
        self.i = j + 1

    def _newline(self) -> None:
        self._flush()
        if self.redirect is not None:
            raise _ScanError("redirection without a target")
        j = self.i + 1
        end = self.i
        for red, owner, delimiter, delimiter_quoted in self.heredocs:
            body, j = _read_heredoc(self.text, j, delimiter, red.op == "<<-")
            if not delimiter_quoted and ("$(" in body or "`" in body):
                owner.nested = owner.nested or "command substitution inside an unquoted heredoc body"
            if owner is self.cur:
                end = j
            else:
                owner.extra += "\n" + self.text[self.i + 1 : j].rstrip("\n")
        self.heredocs = []
        self.i = j
        if not (self.cur.words or self.cur.redirects or self.cur.nested) and self.cur.op_before in _CONTINUATION_OPS:
            return
        self._end_segment("\n", end, j)

    def _operator(self) -> None:
        t, i = self.text, self.i
        c = t[i]
        two = t[i : i + 2]
        if c in "<>" or two == "&>":
            self._redirect_operator()
            return
        self._flush()
        if c in "()":
            self.cur.nested = "subshell or grouping"
            self.i += 1
            return
        if self.redirect is not None:
            raise _ScanError("redirection without a target")
        if c == ";":
            op = ";;" if two == ";;" else ";"
        elif c == "&":
            op = "&&" if two == "&&" else "&"
        else:
            op = "||" if two == "||" else ("|&" if two == "|&" else "|")
        self._end_segment(op, i, i + len(op))
        self.i += len(op)

    def _redirect_operator(self) -> None:
        t, i = self.text, self.i
        c = t[i]
        fd = None
        if c in "<>" and self.started and not self.quoted and "".join(self.buf).isdigit():
            fd = "".join(self.buf)
            self._reset_word()
        else:
            self._flush()
        if self.redirect is not None:
            raise _ScanError("redirection without a target")
        two, three = t[i : i + 2], t[i : i + 3]
        if two in ("<(", ">("):
            self.cur.nested = "process substitution"
            self.i = _skip_substitution(t, i + 1)
            return
        for op in ("<<<", "<<-", "&>>", "<<", "<&", "<>", "&>", ">>", ">|", ">&"):
            if (three if len(op) == 3 else two) == op:
                break
        else:
            op = c
        red = _Redirect(op, fd)
        self.cur.redirects.append(red)
        self.redirect = red
        self.i += len(op)


_UNCHANGED = object()


@dataclass
class _Verdict:
    status: str
    resources: list = field(default_factory=list)
    opaque_kind: str | None = None
    reason_class: str | None = None
    reason: str | None = None
    new_cwd: object = _UNCHANGED
    #: a checkout/switch that moves HEAD: every commit/merge of the line becomes unattributable
    moves_branch: bool = False
    #: the resources name the current branch (commit/merge, push of HEAD)
    branch_dependent: bool = False


class _Cwd:
    """Where the next segment runs: a path, or `None` when it cannot be known."""

    def __init__(self, venue_real: str) -> None:
        self.venue = venue_real
        self.out: str | None = venue_real
        self.pipe_in: str | None = venue_real
        self.moved = False
        self.pipeline_cd = False
        self.or_seen = False

    def enter(self, op: str) -> str | None:
        if op == "||":
            self.or_seen = True
        elif op not in ("&&", "|", "|&"):
            self.or_seen = False
        if op in _PIPE_OPS:
            return self.pipe_in
        self.pipeline_cd = False
        if op == "":
            cwd = self.venue
        elif op == "&&":
            cwd = self.out
        else:
            cwd = None if self.moved else self.out
        self.pipe_in = cwd
        return cwd

    def leave(self, op_before: str, next_op: str, verdict: _Verdict, cwd: str | None) -> None:
        if verdict.status == "unresolved":
            self.out = None
            self.moved = True
            return
        if verdict.new_cwd is _UNCHANGED:
            self.out = None if self.pipeline_cd else cwd
            return
        in_pipeline = op_before in _PIPE_OPS or next_op in _PIPE_OPS
        conditional = in_pipeline or self.or_seen or op_before == "&" or next_op == "&"
        self.pipeline_cd = self.pipeline_cd or in_pipeline
        self.out = None if conditional else verdict.new_cwd
        self.moved = True


def _ok(res: list | None = None) -> _Verdict:
    return _Verdict("resolved", list(res or []))


def _unres(reason_class: str, reason: str) -> _Verdict:
    return _Verdict("unresolved", reason_class=reason_class, reason=reason)


def _opaque(kind: str) -> _Verdict:
    return _Verdict("opaque", opaque_kind=kind)


def _file_write(path: str) -> resources.FileResource:
    return resources.FileResource(path, "write")


def _from_resolution(res: tool_contracts.Resolution) -> _Verdict:
    if res.status == "resolved":
        return _ok(res.resources)
    return _unres(res.reason_class or "contract-unresolved", res.reason or "")


_CWD_UNCERTAIN = "the working directory is uncertain here (an earlier cd, pipeline or unreadable command may have moved it)"


def _resolve_path(tok: _Tok, cwd: str | None) -> tuple[str | None, _Verdict | None]:
    """`(absolute path, None)`, `(None, None)` for a no-effect device, or `(None, verdict)` when undecidable."""
    if tok.nonlit or tool_contracts.is_non_literal(tok.text):
        return None, _unres("residual-syntax", f"path {tok.text!r} is non-literal")
    if tok.text in _NO_EFFECT_DEVICES:
        return None, None
    if os.path.isabs(tok.text):
        return tok.text, None
    if cwd is None:
        return None, _unres("contract-unresolved", _CWD_UNCERTAIN)
    return os.path.join(cwd, tok.text), None


def _targets(toks: list[_Tok], cwd: str | None) -> tuple[list[str], _Verdict | None]:
    paths: list[str] = []
    for tok in toks:
        path, bad = _resolve_path(tok, cwd)
        if bad is not None:
            return [], bad
        if path is not None:
            paths.append(path)
    return paths, None


def _redirect_resources(redirects: list[_Redirect], cwd: str | None) -> tuple[list, _Verdict | None]:
    out: list = []
    for red in redirects:
        writes = red.op in _WRITE_REDIRECTS
        if red.op == ">&":
            writes = red.target is not None and not (red.target.text.isdigit() or red.target.text == "-")
        if not writes:
            continue
        if red.target is None:
            return [], _unres("residual-syntax", "redirection without a target")
        target_path, bad = _resolve_path(red.target, cwd)
        if bad is not None:
            return [], bad
        if target_path is not None:
            out.append(_file_write(target_path))
    return out, None


def _worktree_root(start: str) -> str | None:
    cur = start
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _git_dir(root: str) -> str | None:
    gitdir = os.path.join(root, ".git")
    if os.path.isfile(gitdir):
        try:
            with open(gitdir, encoding="utf-8") as fh:
                line = fh.read().strip()
        except OSError:
            return None
        if not line.startswith("gitdir:"):
            return None
        target = line[len("gitdir:") :].strip()
        return target if os.path.isabs(target) else os.path.join(root, target)
    return gitdir


def _head_branch(root: str) -> str | None:
    gitdir = _git_dir(root)
    if gitdir is None:
        return None
    try:
        with open(os.path.join(gitdir, "HEAD"), encoding="utf-8") as fh:
            head = fh.read().strip()
    except OSError:
        return None
    prefix = "ref: refs/heads/"
    return head[len(prefix) :] if head.startswith(prefix) else None


def _common_dir(root: str) -> str | None:
    """The git directory shared by every worktree of one repository (refs, config, hooks, stash)."""
    gitdir = _git_dir(root)
    if gitdir is None:
        return None
    try:
        with open(os.path.join(gitdir, "commondir"), encoding="utf-8") as fh:
            gitdir = os.path.join(gitdir, fh.read().strip())
    except FileNotFoundError:
        pass
    except OSError:
        return None
    return os.path.realpath(gitdir)


@dataclass(frozen=True)
class GitForm:
    """One allow-listed git write shape: the flags it accepts and how many positionals it needs."""

    flags: frozenset
    value_flags: frozenset = frozenset()
    min_positionals: int = 0


#: The git subcommands (besides push and the read-only table) that resolve in segment mode, with
#: the only flags each accepts. Anything else -- stash in any form, rebase, pull, am, cherry-pick,
#: reset to a commit-ish, checkout/switch -b/-B/-c/-C/--orphan/--detach/--force, commit --amend --
#: is unresolved. Widening an entry widens what the hook may auto-allow: it needs a reviewed reason.
GIT_WRITE_FORMS: dict[str, GitForm] = {
    "add": GitForm(frozenset({"-A", "--all", "-u", "--update", "-v", "--verbose", "-n", "--dry-run"})),
    "commit": GitForm(
        frozenset({"-q", "--quiet", "-a", "--all", "-s", "--signoff", "-n", "--no-verify", "--allow-empty", "-v", "--verbose"}),
        frozenset({"-m", "--message", "-F", "--file"}),
    ),
    "restore": GitForm(frozenset({"-q", "--quiet", "--staged", "-S", "--worktree", "-W"}), min_positionals=1),
    "rm": GitForm(frozenset({"-r", "--cached", "-q", "--quiet", "-n", "--dry-run"}), min_positionals=1),
    "mv": GitForm(frozenset({"-n", "--dry-run", "-v", "--verbose", "-k"}), min_positionals=2),
    "merge": GitForm(
        frozenset({"-q", "--quiet", "--no-edit", "--no-ff", "--ff-only", "--squash"}),
        frozenset({"-m", "--message"}),
        min_positionals=1,
    ),
    "checkout": GitForm(frozenset({"-q", "--quiet"})),
    "switch": GitForm(frozenset({"-q", "--quiet"}), min_positionals=1),
    "reset": GitForm(frozenset({"-q", "--quiet", "--mixed"})),
}
_GIT_BRANCH_DEPENDENT = frozenset({"commit", "merge"})
_COUNT_FLAG_RE = re.compile(r"^-\d+$")


def _git_literal(tok: _Tok) -> bool:
    return not (tok.nonlit or tool_contracts.is_non_literal(tok.text))


def _parse_git_args(rest_toks: list[_Tok], form: GitForm) -> tuple[list[str], list[str], bool, str | None]:
    """`(positionals before --, positionals after --, saw --, refusal reason)`. A value taken by a
    value-flag (a commit message, a message file) is exempt from the literal check unless it holds an
    expansion or glob outside double quotes, which could word-split into extra arguments."""
    before: list[str] = []
    after: list[str] = []
    seen_dd = False
    i = 0
    n = len(rest_toks)
    while i < n:
        tok = rest_toks[i]
        text = tok.text
        if seen_dd:
            if not _git_literal(tok):
                return [], [], False, f"path {text!r} is non-literal"
            after.append(text)
            i += 1
            continue
        if text == "--":
            seen_dd = True
            i += 1
            continue
        if text.startswith("--"):
            key, eq, _ = text.partition("=")
            if key in form.value_flags:
                value_tok = tok if eq else (rest_toks[i + 1] if i + 1 < n else None)
                if value_tok is None or value_tok.bare_nonlit:
                    return [], [], False, f"value of {key} is missing or holds an unquoted expansion"
                i += 1 if eq else 2
                continue
            if key in form.flags and not eq and _git_literal(tok):
                i += 1
                continue
            return [], [], False, f"flag {text!r} is outside the allow-listed shape"
        if text.startswith("-") and len(text) > 1:
            letters = text[1:]
            consumed_next = False
            for pos, ch in enumerate(letters):
                flag = "-" + ch
                if flag in form.value_flags:
                    if pos + 1 < len(letters):
                        if tok.bare_nonlit:
                            return [], [], False, f"value glued to {flag} holds an unquoted expansion"
                    else:
                        value_tok = rest_toks[i + 1] if i + 1 < n else None
                        if value_tok is None or value_tok.bare_nonlit:
                            return [], [], False, f"value of {flag} is missing or holds an unquoted expansion"
                        consumed_next = True
                    break
                if flag not in form.flags:
                    return [], [], False, f"flag {text!r} is outside the allow-listed shape"
            i += 2 if consumed_next else 1
            continue
        if not _git_literal(tok):
            return [], [], False, f"argument {text!r} is non-literal"
        before.append(text)
        i += 1
    return before, after, seen_dd, None


def _git_write_shape(sub: str, form: GitForm, before: list[str], after: list[str], seen_dd: bool) -> str | None:
    """A refusal reason, or `None` when the positionals have the shape this subcommand's allow-list names."""
    if len(before) + len(after) < form.min_positionals:
        return f"git {sub} needs at least {form.min_positionals} operand(s)"
    if sub == "reset":
        head_only = before in ([], ["HEAD"])
        if not (head_only if seen_dd else (head_only or before[0] == "HEAD")):
            return "git reset to a commit-ish moves the branch ref; only the path form is resolved"
    elif sub == "checkout":
        if seen_dd:
            if not after or len(before) > 1:
                return "git checkout -- needs paths and at most one source"
        elif len(before) != 1:
            return "git checkout is resolved only as `checkout <branch>` or `checkout [<ref>] -- <paths>`"
    elif sub == "switch" and len(before) != 1:
        return "git switch is resolved only as `switch <branch>`"
    return None


def _normalised_push(res: tool_contracts.Resolution, root: str) -> _Verdict:
    out: list = []
    branch_dependent = False
    for r in res.resources:
        ref = r.ref[len("refs/heads/") :] if r.ref.startswith("refs/heads/") else r.ref
        if ref == "HEAD":
            branch = _head_branch(root)
            if branch is None:
                return _unres("contract-unresolved", "HEAD is detached or unreadable: no branch to push")
            ref, branch_dependent = branch, True
        out.append(resources.VcsRefResource(r.remote, ref, "push"))
    verdict = _ok(out)
    verdict.branch_dependent = branch_dependent
    return verdict


def _git(toks: list[_Tok], cwd: str | None, ctx: "_Context") -> _Verdict:
    operands = [t.text for t in toks[1:]]
    sub, rest, global_values, reason = tool_contracts.find_git_subcommand(operands)
    if reason is not None:
        return _unres("contract-unresolved", reason)
    if sub is None:
        return _unres("contract-unresolved", "bare `git` with no subcommand")
    for flag in ("--git-dir", "--work-tree"):
        if flag in global_values:
            return _unres("contract-unresolved", f"git {flag} is not resolved in segment mode")
    c_value = global_values.get("-C")
    if c_value is not None and tool_contracts.is_non_literal(c_value):
        return _unres("residual-syntax", f"git -C {c_value!r} is non-literal")
    repo_dir_arg = c_value if c_value is not None else "."
    if not os.path.isabs(repo_dir_arg) and cwd is None:
        return _unres("contract-unresolved", _CWD_UNCERTAIN)
    repo_dir = os.path.realpath(os.path.join(cwd or "/", repo_dir_arg))
    root = _worktree_root(repo_dir)
    if root is None:
        return _unres("contract-unresolved", f"{repo_dir!r} is not inside a git worktree")
    if ctx.venue_common is None or _common_dir(root) != ctx.venue_common:
        return _unres("contract-unresolved", f"{root!r} is not the venue's own repository: its refs, hooks and config are not the venue's")

    form = GIT_WRITE_FORMS.get(sub)
    if form is None:
        if sub == "push":
            res = tool_contracts.resolve_git(["push", *rest], repo_dir)
            return _normalised_push(res, root) if res.status == "resolved" else _from_resolution(res)
        if sub == "log":
            rest = [t for t in rest if not _COUNT_FLAG_RE.match(t)]
        return _from_resolution(tool_contracts.resolve_git([sub, *rest], repo_dir))

    before, after, seen_dd, bad = _parse_git_args(toks[len(toks) - len(rest) :], form)
    if bad is None:
        bad = _git_write_shape(sub, form, before, after, seen_dd)
    if bad is not None:
        return _unres("residual-syntax" if "non-literal" in bad or "expansion" in bad else "contract-unresolved", f"git {sub}: {bad}")
    out: list = [_file_write(root)]
    if sub in _GIT_BRANCH_DEPENDENT:
        branch = _head_branch(root)
        if branch is None:
            return _unres("contract-unresolved", "HEAD is detached or unreadable: no branch to commit to")
        out.append(resources.VcsRefResource(".", branch, "commit"))
    verdict = _ok(out)
    verdict.branch_dependent = sub in _GIT_BRANCH_DEPENDENT
    verdict.moves_branch = sub == "switch" or (sub == "checkout" and not seen_dd)
    return verdict


def _logical_cd_target(path: str) -> str | None:
    """The cwd after `cd <path>` (path already absolute), or `None` when bash's logical `..` handling
    could land somewhere other than the physical resolution."""
    real = os.path.realpath(path)
    if ".." not in path.split(os.sep):
        return real
    return real if os.path.realpath(os.path.normpath(path)) == real else None


def _cd(toks: list[_Tok], cwd: str | None) -> _Verdict:
    verdict = _ok()
    verdict.new_cwd = None
    operands = []
    for tok in toks[1:]:
        if tok.text in ("-L", "-P"):
            return verdict
        if tok.text != "--":
            operands.append(tok)
    if len(operands) != 1 or operands[0].text == "-":
        return verdict
    path, bad = _resolve_path(operands[0], cwd)
    if bad is None and path is not None:
        verdict.new_cwd = _logical_cd_target(path)
    return verdict


_MKDIR_FLAGS = frozenset({"-p", "--parents", "-v", "--verbose"})
_TOUCH_FLAGS = frozenset({"-a", "-m", "-c", "--no-create"})
_MV_FLAGS = frozenset({"-f", "-n", "-v", "-i", "--force", "--no-clobber", "--verbose", "--interactive"})
_CP_FLAGS = _MV_FLAGS | frozenset({"-r", "-R", "-a", "-p", "--recursive", "--archive"})
_TEE_FLAGS = frozenset({"-a", "--append", "-i", "--ignore-interrupts", "-p"})


def _split_operands(prog: str, toks: list[_Tok], allowed_flags: frozenset) -> tuple[list[_Tok], _Verdict | None]:
    operands: list[_Tok] = []
    for tok in toks[1:]:
        if tok.text == "--":
            continue
        if tok.text.startswith("-") and tok.text != "-":
            if tok.text not in allowed_flags:
                return [], _unres("contract-unresolved", f"{prog} flag {tok.text!r} is outside the reviewed closed set")
            continue
        operands.append(tok)
    return operands, None


def _writer(prog: str, toks: list[_Tok], cwd: str | None) -> _Verdict:
    flags = {"mkdir": _MKDIR_FLAGS, "touch": _TOUCH_FLAGS, "tee": _TEE_FLAGS, "mv": _MV_FLAGS, "cp": _CP_FLAGS}[prog]
    operands, bad = _split_operands(prog, toks, flags)
    if bad is not None:
        return bad
    if prog in ("mv", "cp") and len(operands) < 2:
        return _unres("contract-unresolved", f"{prog} needs a source and a destination")
    paths, bad = _targets(operands, cwd)
    if bad is not None:
        return bad
    if prog == "cp":
        paths = paths[-1:] if operands else []
    return _ok([_file_write(p) for p in paths])


_AWK_UNSAFE_RE = re.compile(r"[>|]|\b(?:system|getline)\b")


def _awk(toks: list[_Tok]) -> _Verdict:
    operands = [t.text for t in toks[1:]]
    i = 0
    while i < len(operands) and operands[i].startswith("-") and operands[i] != "-":
        flag = operands[i]
        if flag == "--":
            i += 1
            break
        if flag in ("-F", "-v"):
            i += 2
        elif flag.startswith(("-F", "-v")):
            i += 1
        else:
            return _unres("contract-unresolved", f"awk flag {flag!r} is outside the reviewed closed set")
    if i >= len(operands):
        return _unres("contract-unresolved", "awk with no program operand")
    if _AWK_UNSAFE_RE.search(operands[i]):
        return _unres("contract-unresolved", "the awk program redirects, pipes or runs a command (`>`, `|`, system, getline)")
    return _ok()


_SHELL_INTERPRETERS = frozenset({"bash", "sh", "zsh", "dash"})
_EVAL_FLAGS = {"node": frozenset({"-e", "-p", "--eval", "--print"}), "ruby": frozenset({"-e"}), "perl": frozenset({"-e", "-E"})}
_PYTHON_VALUE_LETTERS = "WX"
_PYTHON_VALUE_FLAGS = frozenset({"--check-hash-based-pycs"})
_SHELL_VALUE_FLAGS = frozenset({"-o", "-O", "--rcfile", "--init-file"})
_SHELL_C_FLAG_RE = re.compile(r"-[A-Za-z]*c[A-Za-z]*")


def _delegate(toks: list[_Tok], cwd: str | None, ctx: "_Context") -> _Verdict:
    if cwd is None:
        return _unres("contract-unresolved", _CWD_UNCERTAIN)
    text = " ".join(shlex.quote(t.text) for t in toks)
    return _from_resolution(tool_contracts.resolve_command(text, cwd, contract_table=ctx.table))


def _interpreter(prog: str, toks: list[_Tok], cwd: str | None, ctx: "_Context") -> _Verdict:
    if cwd is None:
        return _unres("contract-unresolved", _CWD_UNCERTAIN)
    operands = [t.text for t in toks[1:]]
    is_python = bool(widening_targets.INTERPRETER_RE.match(prog))
    i = 0
    while i < len(operands) and operands[i].startswith("-") and operands[i] != "-":
        flag = operands[i]
        if flag == "--":
            i += 1
            break
        if is_python:
            if flag.startswith("--"):
                i += 1 if flag in _PYTHON_VALUE_FLAGS else 0
            else:
                letters = flag[1:]
                for pos, ch in enumerate(letters):
                    if ch == "c":
                        return _opaque("python-c")
                    if ch == "m":
                        return _delegate(toks, cwd, ctx)
                    if ch in _PYTHON_VALUE_LETTERS:
                        i += 1 if pos == len(letters) - 1 else 0
                        break
        elif prog in _SHELL_INTERPRETERS:
            if flag in _SHELL_VALUE_FLAGS:
                i += 1
            elif _SHELL_C_FLAG_RE.fullmatch(flag):
                return _unres("adhoc-undeclared", f"{prog} -c runs inline code, never resolved by this table")
        elif flag in _EVAL_FLAGS.get(prog, ()):
            return _unres("adhoc-undeclared", f"{prog} {flag} runs inline code, never resolved by this table")
        i += 1
    if i >= len(operands) or operands[i] == "-":
        return _opaque("stdin-body")
    if is_python:
        delegated = _delegate(toks, cwd, ctx)
        if delegated.status == "resolved":
            return delegated
    return _opaque("script-file")


def _program(prog: str, toks: list[_Tok], cwd: str | None, ctx: "_Context") -> _Verdict:
    if prog == "cd":
        return _cd(toks, cwd)
    if prog == "git":
        return _git(toks, cwd, ctx)
    if prog in ("mkdir", "touch", "tee", "mv", "cp"):
        return _writer(prog, toks, cwd)
    if prog == "awk":
        return _awk(toks)
    if prog in tool_contracts.SCRIPT_INTERPRETERS or widening_targets.INTERPRETER_RE.match(prog):
        return _interpreter(prog, toks, cwd, ctx)
    entry = ctx.table.get(prog)
    if entry is None:
        return _unres("unknown-program", f"{prog!r} has no tool_contracts.toml entry")
    if entry.effect == "unresolved":
        return _unres("contract-unresolved", entry.reason or "")
    if entry.effect == "none":
        assert entry.safe_flags is not None
        if "*" not in entry.safe_flags:
            for tok in toks[1:]:
                if tok.nonlit or tool_contracts.is_non_literal(tok.text):
                    return _unres("residual-syntax", f"{prog!r} operand {tok.text!r} is non-literal: it may expand into a flag outside the safe set")
                if tok.text.startswith("-") and tok.text != "-" and tok.text.split("=", 1)[0] not in entry.safe_flags:
                    return _unres("contract-unresolved", f"{prog!r} flag {tok.text!r} is outside the reviewed safe-flag set")
        return _ok()
    return _delegate(toks, cwd, ctx)


@dataclass
class _Context:
    table: dict
    venue_common: str | None = None


def _resolve_one(raw: _RawSegment, cwd: str | None, ctx: _Context) -> _Verdict:
    if raw.nested:
        verdict = _unres("residual-syntax", f"{raw.nested} hides nested work")
    elif not raw.words:
        verdict = _ok()
    else:
        first = raw.words[0]
        prog = first.text
        if prog in tool_contracts.SHELL_RESERVED_WORDS or first.nonlit:
            verdict = _unres("residual-syntax", f"{prog!r} is a shell keyword or a non-literal program word")
        elif tool_contracts.BARE_ASSIGNMENT_RE.match(prog):
            verdict = _unres("contract-unresolved", f"leading environment assignment {prog!r} is never in the reviewed closed set")
        elif "/" in prog:
            verdict = _unres("contract-unresolved", f"program token {prog!r} is path-qualified, never in the reviewed closed set")
        elif prog in tool_contracts.FORBIDDEN_WRAPPER_TOKENS:
            verdict = _unres("contract-unresolved", f"wrapper token {prog!r} is never in the reviewed closed set")
        else:
            verdict = _program(prog, raw.words, cwd, ctx)
    if verdict.status == "unresolved":
        return verdict
    red_resources, bad = _redirect_resources(raw.redirects, cwd)
    if bad is not None:
        return bad
    verdict.resources = list(dict.fromkeys([*verdict.resources, *red_resources]))
    return verdict


def _demote_branch_dependent(raws: list[_RawSegment], verdicts: list[_Verdict]) -> None:
    """A segment whose resources name the current branch is only trustworthy when nothing else on the
    line can change which branch that is: no earlier segment, and no segment running concurrently with
    it (after it under `&` or inside a pipeline), is a branch switch, unresolved or opaque."""
    hazards = [v.status != "resolved" or v.moves_branch for v in verdicts]
    for k, verdict in enumerate(verdicts):
        if not verdict.branch_dependent or verdict.status != "resolved":
            continue
        concurrent_after = k + 1 < len(raws) and raws[k + 1].op_before in _ASYNC_OPS
        others = hazards[:k] + (hazards[k + 1 :] if concurrent_after else [])
        if any(others):
            verdicts[k] = _unres("contract-unresolved", "the current branch is uncertain here: another segment of the line may switch it")


def resolve_segments(
    text: str, venue: str, *, contract_table: dict | None = None
) -> tool_contracts.SegmentedResolution:
    venue_real = tool_contracts.realpath(venue)
    try:
        raws = _Scanner(text).run()
    except _ScanError as exc:
        return tool_contracts.SegmentedResolution([
            tool_contracts.Segment(text.strip(), None, "unresolved", [], None, "residual-syntax", str(exc))
        ])
    venue_root = _worktree_root(venue_real)
    ctx = _Context(
        contract_table if contract_table is not None else tool_contracts.load_contract_table(),
        _common_dir(venue_root) if venue_root is not None else None,
    )
    cwd_state = _Cwd(venue_real)
    verdicts: list[_Verdict] = []
    cwds: list[str | None] = []
    for index, raw in enumerate(raws):
        next_op = raws[index + 1].op_before if index + 1 < len(raws) else ""
        cwd = cwd_state.enter(raw.op_before)
        verdict = _resolve_one(raw, cwd, ctx)
        cwd_state.leave(raw.op_before, next_op, verdict, cwd)
        verdicts.append(verdict)
        cwds.append(cwd)
    _demote_branch_dependent(raws, verdicts)
    segments = []
    for raw, verdict, cwd in zip(raws, verdicts, cwds):
        seg_text = (text[raw.start : raw.end].strip() + raw.extra).strip()
        segments.append(tool_contracts.Segment(
            seg_text, cwd if verdict.status != "unresolved" else None, verdict.status, verdict.resources,
            verdict.opaque_kind, verdict.reason_class, verdict.reason))
    return tool_contracts.SegmentedResolution(segments)
