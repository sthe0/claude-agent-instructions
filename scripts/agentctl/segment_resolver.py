"""Per-segment command resolution: what each `&&` / `;` / `|` piece of a Bash
command line changes, and what stays undecided.

Difficulty removed: `tool_contracts.resolve_command` is all-or-nothing -- one
`git -C <dir>`, one redirect, one `cd` or one `python3 -c` anywhere makes the
WHOLE command unresolved, so the PreToolUse grant hook could neither cover the
commands agents really run (`git -C <venue> commit`, `cd out && mv a b`,
`cmd > file`) nor deny a resolved write outside the grants. This module splits
the line into segments and gives each its own verdict, so the hook reads the
effects of the decidable parts and falls through only for the rest.

It is a separate module from `resolve_command` on purpose: that function's
behaviour is pinned for every existing caller (digest-neutrality differential),
and segment mode is looser than its closed grammar by design (git writes,
redirects, `cd` re-basing). It reuses the reviewed git grammar
(`_find_git_subcommand`, `_resolve_git`) and the contract table rather than
duplicating them.

A segment's status is one of
  resolved  -- `resources` lists every effect (possibly none);
  opaque    -- the segment runs code this module cannot read (`opaque_kind`
               python-c | stdin-body | script-file); its redirect targets are
               still listed, the code's own effects are for a later judge;
  unresolved -- undecidable (`reason_class` is one of the canonical tags).
A consumer may act on a resolved segment's effects, but must treat any
opaque or unresolved segment as "the line is not fully decided".

The working directory is tracked conservatively: `cd <literal>` re-bases only
`&&`-chained successors; once a `cd` (or a command that may move the cwd) has
occurred, a successor reached through `;`, `||`, `&` or a newline runs in
either of two directories, and a `cd` inside a pipeline moves nothing for sure,
so every later segment that needs the cwd is unresolved.
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
        self.redirect: _Redirect | None = None
        self.heredocs: list[tuple[_Redirect, _RawSegment, str]] = []

    def _reset_word(self) -> None:
        self.buf = []
        self.started = self.quoted = self.nonlit = False

    def _add(self, ch: str, *, quoted: bool = False) -> None:
        self.buf.append(ch)
        self.started = True
        if quoted:
            self.quoted = True

    def _flush(self) -> None:
        if not self.started:
            return
        tok = _Tok("".join(self.buf), self.nonlit)
        if self.redirect is not None:
            self.redirect.target = tok
            if self.redirect.op in _HEREDOC_OPS:
                self.heredocs.append((self.redirect, self.cur, tok.text))
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
                    self.nonlit = True
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
        self.nonlit = True
        if nxt == "(":
            self.cur.nested = "command substitution"
            j = _skip_substitution(t, self.i + 1)
            self.buf.append(t[self.i : j])
            self.i = j
        elif nxt == "'":
            self.i += 1
        else:
            self.buf.append("$")
            self.i += 1

    def _backtick(self) -> None:
        j = self.text.find("`", self.i + 1)
        if j < 0:
            raise _ScanError("unterminated backtick")
        self.cur.nested = "command substitution"
        self.buf.append(self.text[self.i : j + 1])
        self.started = self.nonlit = True
        self.i = j + 1

    def _newline(self) -> None:
        self._flush()
        if self.redirect is not None:
            raise _ScanError("redirection without a target")
        j = self.i + 1
        end = self.i
        for red, owner, delimiter in self.heredocs:
            _, j = _read_heredoc(self.text, j, delimiter, red.op == "<<-")
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


class _Cwd:
    """Where the next segment runs: a path, or `None` when it cannot be known."""

    def __init__(self, venue_real: str) -> None:
        self.venue = venue_real
        self.out: str | None = venue_real
        self.pipe_in: str | None = venue_real
        self.moved = False
        self.pipeline_cd = False

    def enter(self, op: str) -> str | None:
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


_UNCHANGED = object()


@dataclass
class _Verdict:
    status: str
    resources: list = field(default_factory=list)
    opaque_kind: str | None = None
    reason_class: str | None = None
    reason: str | None = None
    new_cwd: object = _UNCHANGED
    mutates_cwd: bool = False


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
    if tok.nonlit or tool_contracts._is_non_literal(tok.text):
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


def _head_branch(root: str) -> str | None:
    gitdir = os.path.join(root, ".git")
    try:
        if os.path.isfile(gitdir):
            with open(gitdir, encoding="utf-8") as fh:
                line = fh.read().strip()
            if not line.startswith("gitdir:"):
                return None
            target = line[len("gitdir:") :].strip()
            gitdir = target if os.path.isabs(target) else os.path.join(root, target)
        with open(os.path.join(gitdir, "HEAD"), encoding="utf-8") as fh:
            head = fh.read().strip()
    except OSError:
        return None
    prefix = "ref: refs/heads/"
    return head[len(prefix) :] if head.startswith(prefix) else None


_GIT_WRITE_SUBCOMMANDS = frozenset({"add", "commit", "stash", "checkout", "restore", "switch", "merge", "rm", "mv", "reset"})
_GIT_COMMIT_REF_SUBCOMMANDS = frozenset({"commit", "merge"})
_GIT_MESSAGE_SUBCOMMANDS = frozenset({"commit", "merge", "stash"})
_GIT_RESET_DISCARDING_FLAGS = frozenset({"--hard", "--merge", "--keep"})
_COUNT_FLAG_RE = re.compile(r"^-\d+$")


def _message_value_indexes(rest: list[str]) -> set[int]:
    """Indexes of tokens that are a commit/merge message or message file: text that cannot change
    which repository or branch the command touches, so a `$` in it is harmless."""
    idx: set[int] = set()
    for i, tok in enumerate(rest):
        if tok in ("--message", "--file") or re.fullmatch(r"-[A-Za-z]*[mF]", tok):
            idx.add(i + 1)
        elif tok.startswith("--message=") or (tok.startswith("-m") and not tok.startswith("--")):
            idx.add(i)
    return idx


def _git(toks: list[_Tok], cwd: str | None, ctx: "_Context") -> _Verdict:
    operands = [t.text for t in toks[1:]]
    sub, rest, global_values, reason = tool_contracts._find_git_subcommand(operands)
    if reason is not None:
        return _unres("contract-unresolved", reason)
    if sub is None:
        return _unres("contract-unresolved", "bare `git` with no subcommand")
    for flag in ("--git-dir", "--work-tree"):
        if flag in global_values:
            return _unres("contract-unresolved", f"git {flag} is not resolved in segment mode")
    c_value = global_values.get("-C")
    if c_value is not None and tool_contracts._is_non_literal(c_value):
        return _unres("residual-syntax", f"git -C {c_value!r} is non-literal")
    repo_dir_arg = c_value if c_value is not None else "."
    if not os.path.isabs(repo_dir_arg) and cwd is None:
        return _unres("contract-unresolved", _CWD_UNCERTAIN)
    repo_dir = os.path.realpath(os.path.join(cwd or "/", repo_dir_arg))

    if sub not in _GIT_WRITE_SUBCOMMANDS:
        if sub == "log":
            rest = [t for t in rest if not _COUNT_FLAG_RE.match(t)]
        return _from_resolution(tool_contracts._resolve_git([sub, *rest], repo_dir))

    rest_toks = toks[len(toks) - len(rest) :]
    exempt = _message_value_indexes(rest) if sub in _GIT_MESSAGE_SUBCOMMANDS else set()
    for i, tok in enumerate(rest_toks):
        if i not in exempt and (tok.nonlit or tool_contracts._is_non_literal(tok.text)):
            return _unres("residual-syntax", f"git {sub} argument {tok.text!r} is non-literal")
    if sub == "reset" and _GIT_RESET_DISCARDING_FLAGS & set(rest):
        return _unres("contract-unresolved", "git reset --hard/--merge/--keep discards work and is not resolved")
    root = _worktree_root(repo_dir)
    if root is None:
        return _unres("contract-unresolved", f"{repo_dir!r} is not inside a git worktree")
    out: list = [_file_write(root)]
    if sub in _GIT_COMMIT_REF_SUBCOMMANDS:
        if root in ctx.branch_switched:
            return _unres("contract-unresolved", "the branch was switched earlier in this command line")
        branch = _head_branch(root)
        if branch is None:
            return _unres("contract-unresolved", "HEAD is detached or unreadable: no branch to commit to")
        out.append(resources.VcsRefResource(".", branch, "commit"))
    if sub in ("checkout", "switch") and not (sub == "checkout" and rest[:1] == ["--"]):
        ctx.branch_switched.add(root)
    return _ok(out)


def _cd(toks: list[_Tok], cwd: str | None) -> _Verdict:
    operands = [t for t in toks[1:] if t.text not in ("-L", "-P", "--")]
    verdict = _ok()
    verdict.new_cwd = None
    if len(operands) != 1 or operands[0].text == "-":
        return verdict
    path, bad = _resolve_path(operands[0], cwd)
    if bad is None and path is not None:
        verdict.new_cwd = os.path.realpath(path)
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
_PYTHON_VALUE_FLAGS = frozenset({"-W", "-X", "-Q"})
_SHELL_C_FLAG_RE = re.compile(r"-[A-Za-z]*c[A-Za-z]*")
_CWD_MUTATING_PROGRAMS = frozenset({"pushd", "popd", "source", ".", "eval", "exec", "builtin", "command"})


def _delegate(toks: list[_Tok], cwd: str | None, ctx: "_Context") -> _Verdict:
    if cwd is None:
        return _unres("contract-unresolved", _CWD_UNCERTAIN)
    text = " ".join(shlex.quote(t.text) for t in toks)
    return _from_resolution(tool_contracts.resolve_command(text, cwd, contract_table=ctx.table))


def _interpreter(prog: str, toks: list[_Tok], cwd: str | None, ctx: "_Context") -> _Verdict:
    operands = [t.text for t in toks[1:]]
    is_python = bool(widening_targets.INTERPRETER_RE.match(prog))
    i = 0
    while i < len(operands) and operands[i].startswith("-") and operands[i] != "-":
        flag = operands[i]
        if is_python:
            if flag.startswith("-c"):
                return _opaque("python-c")
            if flag.startswith("-m"):
                return _delegate(toks, cwd, ctx)
            if flag in _PYTHON_VALUE_FLAGS:
                i += 1
        elif prog in _SHELL_INTERPRETERS:
            if _SHELL_C_FLAG_RE.fullmatch(flag):
                return _unres("adhoc-undeclared", f"{prog} -c runs inline code, never resolved by this table")
        elif flag in _EVAL_FLAGS.get(prog, ()):
            return _unres("adhoc-undeclared", f"{prog} {flag} runs inline code, never resolved by this table")
        i += 1
    if i >= len(operands) or operands[i] == "-":
        return _opaque("stdin-body")
    if is_python and cwd is not None:
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
    if prog in tool_contracts._SCRIPT_INTERPRETERS or widening_targets.INTERPRETER_RE.match(prog):
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
                if tok.text.startswith("-") and tok.text != "-" and tok.text.split("=", 1)[0] not in entry.safe_flags:
                    return _unres("contract-unresolved", f"{prog!r} flag {tok.text!r} is outside the reviewed safe-flag set")
        return _ok()
    return _delegate(toks, cwd, ctx)


@dataclass
class _Context:
    table: dict
    branch_switched: set = field(default_factory=set)


def _resolve_one(raw: _RawSegment, cwd: str | None, ctx: _Context) -> _Verdict:
    if raw.nested:
        verdict = _unres("residual-syntax", f"{raw.nested} hides nested work")
    elif not raw.words:
        verdict = _ok()
    else:
        first = raw.words[0]
        prog = first.text
        if prog in tool_contracts._SHELL_RESERVED_WORDS or first.nonlit:
            verdict = _unres("residual-syntax", f"{prog!r} is a shell keyword or a non-literal program word")
        elif tool_contracts._BARE_ASSIGNMENT_RE.match(prog):
            verdict = _unres("contract-unresolved", f"leading environment assignment {prog!r} is never in the reviewed closed set")
        elif "/" in prog:
            verdict = _unres("contract-unresolved", f"program token {prog!r} is path-qualified, never in the reviewed closed set")
        elif prog in tool_contracts._FORBIDDEN_WRAPPER_TOKENS:
            verdict = _unres("contract-unresolved", f"wrapper token {prog!r} is never in the reviewed closed set")
        else:
            verdict = _program(prog, raw.words, cwd, ctx)
        if verdict.status == "unresolved" and (verdict.reason_class == "residual-syntax" or prog in _CWD_MUTATING_PROGRAMS):
            verdict.mutates_cwd = True
    if verdict.status == "unresolved" and raw.nested:
        verdict.mutates_cwd = True
    if verdict.status == "unresolved":
        return verdict
    red_resources, bad = _redirect_resources(raw.redirects, cwd)
    if bad is not None:
        return bad
    verdict.resources = list(dict.fromkeys([*verdict.resources, *red_resources]))
    return verdict


def resolve_segments(
    text: str, venue: str, *, contract_table: dict | None = None
) -> tool_contracts.SegmentedResolution:
    venue_real = tool_contracts._realpath(venue)
    try:
        raws = _Scanner(text).run()
    except _ScanError as exc:
        return tool_contracts.SegmentedResolution([
            tool_contracts.Segment(text.strip(), venue_real, "unresolved", [], None, "residual-syntax", str(exc))
        ])
    ctx = _Context(contract_table if contract_table is not None else tool_contracts.load_contract_table())
    cwd_state = _Cwd(venue_real)
    segments: list[tool_contracts.Segment] = []
    for index, raw in enumerate(raws):
        next_op = raws[index + 1].op_before if index + 1 < len(raws) else ""
        cwd = cwd_state.enter(raw.op_before)
        verdict = _resolve_one(raw, cwd, ctx)
        if verdict.new_cwd is not _UNCHANGED:
            in_pipeline = raw.op_before in _PIPE_OPS or next_op in _PIPE_OPS
            cwd_state.pipeline_cd = cwd_state.pipeline_cd or in_pipeline
            cwd_state.out = None if in_pipeline else verdict.new_cwd
            cwd_state.moved = True
        elif verdict.mutates_cwd:
            cwd_state.out = None
            cwd_state.moved = True
        else:
            cwd_state.out = None if cwd_state.pipeline_cd else cwd
        seg_text = (text[raw.start : raw.end].strip() + raw.extra).strip()
        segments.append(tool_contracts.Segment(
            seg_text, cwd if cwd is not None else venue_real, verdict.status, verdict.resources,
            verdict.opaque_kind, verdict.reason_class, verdict.reason))
    return tool_contracts.SegmentedResolution(segments)
