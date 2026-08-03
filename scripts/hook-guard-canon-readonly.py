#!/usr/bin/env python3
"""PreToolUse hook: keep canon checkouts read-only from a live agent session.

Difficulty removed: the PRIMARY Core-instructions worktree (~/claude-agent-
instructions — the tree settings.json hook commands point at) is the live hook
code every session on this machine runs, on ANY branch — even 'main', since a
direct edit there is still an uncommitted, unreviewed mutation of the shared
serving checkout while a session is mid-task. The same problem generalizes to
any other canon source a machine designates (e.g. an org-internal read-only
mirror) via a machine-local, org-neutral path list. Feature work belongs in a
linked worktree or a second mount (see `scripts/session-isolate.sh`); nothing
about canon read-only-ness is decidable from branch name, so this guard drops
the old off-main check entirely and denies unconditionally inside canon.

Decidable from git state + a machine-local path list: DENY an Edit/Write (or a
`git commit`) whose target lies (a) in the Core repo's PRIMARY worktree
(regardless of branch), or (b) under any path registered in the canon-roots
file (scripts/lib/config_root.py's canon_roots_file(), read fail-open). Every
path comparison is realpath-normalized on both sides, so a symlink resolving
INTO canon is denied and one resolving OUTSIDE canon is allowed. Memory writes
are NOT exempt here — the durable memory a session should write lives in
personal auto-memory (~/.claude-agent/projects/<hash>/memory) or a linked
worktree/second mount's project memory, never in canon directly.

Everything else is ALLOWED (fail-open): a linked worktree, a second mount, a
path outside canon entirely, `/tmp`, and any git error or missing canon-roots
file. Non-`git commit` git commands (pull, fetch, merge --ff-only, update-ref,
...) are never inspected — only Bash commands that literally run `git commit`
are denied. A `git commit` naming no target this module can read — neither
`git -C <dir>` nor a leading `cd <dir> &&` / `cd <dir> ;` — from a session
whose directory is canon is denied by design, not by a detection gap: a
linked worktree keeps its own index, so such a commit reaches canon's index
and never the worktree's (reproduction and verdict:
docs/decisions/canon-guard-bare-commit-verdict.md, #44). A commit that
reaches a worktree by a route this module does not parse (a `cd` after the first
token, a subshell, `GIT_DIR`) is denied too — conservatively, per `git_cwd`'s
contract, and it was denied before that verdict as well. Here-document bodies and
here-string operands are DATA and are removed before any Bash command is
inspected (`lib/shell_tokens.py`), so a Markdown blockquote line inside a body is
not read as a redirect. Always exits 0 — a hook crash must never wedge the
workflow.

This guard is a FAIL-OPEN barrier that raises the cost of an ACCIDENTAL canon
write, not a security boundary: known bypasses exist, and any form named in this
file is illustrative rather than a claim that the rest are covered. The measured
forms and their disposition are tracked in
https://github.com/sthe0/claude-agent-instructions/issues/54.

DENY is signaled with the PreToolUse permissionDecision JSON on stdout (mirrors
hook-guard-destructive-rm.py):
  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
   "permissionDecision": "deny", "permissionDecisionReason": "..."}}
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import config_root, git_cwd, shell_tokens  # noqa: E402

GIT_TIMEOUT_S = 3


def _core_root() -> Path:
    return Path(os.environ.get("CLAUDE_INSTRUCTIONS_REPO", str(Path.home() / "claude-agent-instructions")))


def _nearest_existing_dir(path: str) -> str | None:
    """The nearest existing ancestor directory of `path` (which may not exist yet
    for a Write creating a new file), or None if none resolves."""
    p = Path(path)
    if not p.is_absolute():
        return None
    cur = p if p.is_dir() else p.parent
    while True:
        if cur.is_dir():
            return str(cur)
        if cur.parent == cur:
            return None
        cur = cur.parent


def _git_info(cwd: str):
    """(toplevel, git_dir_abs, git_common_dir_abs, branch) for `cwd`, or None on any
    failure. Relative git-dir / git-common-dir are resolved against `cwd`."""
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "rev-parse",
             "--show-toplevel", "--git-dir", "--git-common-dir", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_S, check=False,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    lines = proc.stdout.splitlines()
    if len(lines) < 4:
        return None
    toplevel, git_dir, git_common_dir, branch = lines[0], lines[1], lines[2], lines[3]
    git_dir_abs = os.path.realpath(os.path.join(cwd, git_dir))
    git_common_abs = os.path.realpath(os.path.join(cwd, git_common_dir))
    return os.path.realpath(toplevel), git_dir_abs, git_common_abs, branch


def _is_primary_core(target_dir: str) -> bool:
    """True only when target_dir resolves to the PRIMARY (non-linked) worktree of
    the Core repo, on any branch. Fail-open (False) on any ambiguity: git error,
    linked worktree, or a toplevel other than the Core repo root."""
    info = _git_info(target_dir)
    if info is None:
        return False
    toplevel, git_dir_abs, git_common_abs, _branch = info
    if toplevel != os.path.realpath(str(_core_root())):
        return False  # not the Core repo (or a linked worktree, whose toplevel differs)
    if git_dir_abs != git_common_abs:
        return False  # linked worktree of the Core repo — that's the point of isolation
    return True


def _read_canon_roots() -> list[str]:
    """Non-empty, non-comment lines of the canon-roots file, or [] on any error
    (missing file, unreadable, etc.) — fail-open."""
    try:
        path = config_root.canon_roots_file()
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def _under_registered_canon(file_path: str) -> bool:
    """True iff the realpath of file_path is, or is a descendant of, the realpath
    of any registered canon-roots entry. Both sides are realpath-normalized so a
    symlink resolving into a canon root is caught, and a sibling path that merely
    shares a string prefix (not a path-part prefix) is not."""
    target = os.path.realpath(file_path)
    for root in _read_canon_roots():
        try:
            root_real = os.path.realpath(root)
        except Exception:
            continue
        if target == root_real or target.startswith(root_real + os.sep):
            return True
    return False


def _is_in_canon(target_dir: str, file_path: str) -> bool:
    return _is_primary_core(target_dir) or _under_registered_canon(file_path)


def _adjacent_git_commit(command: str) -> bool:
    """The pre-scanner reading: a `git` token immediately followed by `commit`.

    Kept as the DOUBT branch of `_is_git_commit`, not as dead code. It is wrong
    in both directions (it reads a mention as a commit and misses a commit behind
    a global option), but it denies seven live wrapper forms — `nice git commit`,
    `sudo -i git commit`, `git submodule foreach git commit` and the rest — that
    the scanner answers with doubt, and resolving that doubt to ALLOW instead was
    measured to convert every one of them into an allow."""
    try:
        tokens = shlex.split(command)
    except Exception:
        return False
    for i in range(len(tokens) - 1):
        if os.path.basename(tokens[i]) == "git" and tokens[i + 1] == "commit":
            return True
    return False


_INERT_COMMIT_OPTS = {"--help", "-h", "--dry-run"}


def _every_commit_is_inert(command: str) -> bool:
    """True iff the command has at least one `git commit` segment and EVERY one
    of them only prints (`--help`, `-h`, `--dry-run`) — 13 such commands denied in
    the measured corpus. False on any doubt, which keeps the deny."""
    try:
        tokens = shell_tokens.tokenize(command)
    except Exception:
        return False
    found = False
    for _sep, seg in shell_tokens.split_segments(shell_tokens.drop_substitutions(tokens)):
        if not seg:
            continue
        stripped, recognized = shell_tokens.strip_command_prefix(seg)
        if not recognized or not stripped:
            continue
        invocation = git_cwd.scan_git_invocation(stripped)
        if invocation is None or invocation.doubt or invocation.subcommand != "commit":
            continue
        found = True
        if not any(shell_tokens.unquote_word(t) in _INERT_COMMIT_OPTS for t in stripped[1:]):
            return False
    return found


def _statement_newlines(command: str) -> str:
    """`command` with each statement-ending newline rewritten to `;`.

    A newline separates statements in bash but is plain whitespace to every lexer
    here, so `cat > f\\ngit commit -m x` reads as ONE segment headed by `cat` —
    which the scanner then answers "not a commit", provably rather than doubtfully,
    so no fallback fires. Rewritten only when each line tokenizes on its own,
    which proves no quoted string spans the boundary, and never after a `\\`
    continuation, which is not a statement end."""
    lines = command.split("\n")
    if len(lines) == 1:
        return command
    for line in lines:
        try:
            shell_tokens.tokenize(line)
        except Exception:
            return command
    out = lines[0]
    for line in lines[1:]:
        out += ("\n" if out.rstrip(" \t").endswith("\\") else " ; ") + line
    return out


def _is_git_commit(command: str) -> bool:
    """True iff the command runs a `git commit` that would touch a working tree.

    Doubt is resolved to the adjacency scan rather than to allow, and a commit
    that only prints is demoted back to non-commit."""
    verdict = git_cwd.runs_commit(command)
    if verdict is None:
        verdict = _adjacent_git_commit(command)
    if not verdict:
        return False
    return not _every_commit_is_inert(command)


# --- best-effort in-place Bash-write detection (extends the git-commit deny) ---
#
# The Bash branch of this guard used to deny ONLY a literal `git commit` in canon,
# so every other in-place write verb (`sed -i`, `>>`, `tee`, `cp`/`mv`, `patch`,
# `git apply`) slipped past it — a hole under the "canon is read-only" promise.
# The helpers below close the DETECTABLE write verbs, fail-open (any parse doubt
# ALLOWS), and allow the identical verbs targeting a worktree / second mount.
#
# WHAT THIS GUARD IS. It raises the cost of an ACCIDENTAL canon write. It is not
# an evasion-proof boundary and must not be described as one. The durable
# guarantees are the tool-level Edit/Write deny and keeping feature work out of
# the canon checkout entirely (`scripts/session-isolate.sh`).
#
# FAIL-OPEN BARRIER, NOT A SECURITY BOUNDARY. Every parse doubt allows, so the set
# of write forms that reach canon is open-ended: a write with no shell-visible
# write verb, a write verb this scanner does not model, a path whose text is not
# the path the shell will use, a statement boundary the segmenter does not see.
# Forms named anywhere in this file are ILLUSTRATIVE — the absence of a form is
# not coverage of it, and a reader looking for a guarantee will not find one here.
# The measured forms and their disposition are tracked in
# https://github.com/sthe0/claude-agent-instructions/issues/54.
#
# NAMED RESIDUAL — a path holding an UNEXPANDED `$VAR` is denied BY DESIGN, not by
# accident. Each Bash tool call is a fresh shell, so `$S` assigned in an earlier
# call is unset and `cat > $S/x.md` really does write `<cwd>/$S/x.md`. The hook
# receives byte-identical input whether `$S` is unset or would have expanded into
# canon, so it cannot distinguish them and must deny both; `_deny_msg` names the
# cause instead of pretending the path was literal.
#
# NAMED RESIDUAL — the body stripper leaves spurious DENYs behind (a second
# here-document on one command, a `$(` opened inside a quoted-delimiter body and
# closed after it). They fall in the safe direction and are tracked with the rest
# in the issue above rather than enumerated here.

def _operand_word(raw: str) -> str:
    """A raw token as the path arithmetic below must read it: quotes removed, and
    a leading `~` expanded only when the shell itself would expand it.

    Both halves are decided on the RAW token, which is why this cannot be folded
    into `unquote_word`: `"~/x"` is a literal directory named `~` (measured), so
    expanding it would turn a real canon-relative write into an allow."""
    word = shell_tokens.unquote_word(raw)
    if word.startswith("~") and not shell_tokens.was_quoted(raw):
        return os.path.expanduser(word)
    return word


def _canon_target(raw: str, eff_cwd: str) -> str | None:
    """Realpath of the raw token `raw` (resolved rel to `eff_cwd`) iff it lands in
    canon, else None. A not-yet-existing write target resolves through its nearest
    existing parent so a redirect creating a new file in canon is still caught."""
    candidate = _operand_word(raw)
    if not candidate:
        return None
    path = candidate if os.path.isabs(candidate) else os.path.join(eff_cwd, candidate)
    parent = _nearest_existing_dir(path)
    if parent is None:
        return None
    if _is_in_canon(parent, path):
        return os.path.realpath(path)
    return None


def _canon_cwd(eff_cwd: str) -> str | None:
    """Realpath of `eff_cwd` iff the cwd itself is canon — for cwd-relative
    writers (`patch`, `git apply`) whose write target is derived from the diff,
    not a shell-visible positional, so the cwd is the only decidable signal."""
    parent = _nearest_existing_dir(eff_cwd)
    if parent is None:
        return None
    if _is_in_canon(parent, eff_cwd):
        return os.path.realpath(eff_cwd)
    return None


def _operands(rest: list[str]) -> list[str]:
    """Tokens of a segment (after the command word) up to the first redirection
    operator — a redirect starts an I/O target, not a positional of the verb.

    The token immediately before that operator is dropped when it is bare digits:
    in `cp A B 2>/dev/null` the `2` is the redirect's FILE DESCRIPTOR, and reading
    it as `cp`'s last operand is what made that command deny (measured)."""
    out: list[str] = []
    for tok in rest:
        if tok in shell_tokens.REDIRECT_OPS:
            if out and out[-1].isdigit():
                out.pop()
            return out
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


# `cp`/`mv` options whose VALUE is the destination directory...
_CP_DEST_OPTS = {"-t", "--target-directory"}
# ...and those whose value is not a path at all, but still consumes the token
# after them, so it must not be counted as the last operand.
_CP_VALUE_OPTS = {"-S", "--suffix"}


def _cp_mv_dest(rest: list[str]) -> str | None:
    """The write destination of a `cp`/`mv`: the `-t DIR` / `--target-directory`
    value if present, else the last OPERAND. Returning only the destination keeps
    copying OUT of canon (canon source, outside dest) allowed.

    "Last operand" and "last token" differ, and the difference is a false deny:
    in `cp /tmp/a /tmp/b --suffix .bak` the trailing `.bak` is `--suffix`'s VALUE,
    so the value-taking options have to be consumed rather than merely skipped."""
    positionals: list[str] = []
    take_next: str | None = None
    dest_opt: str | None = None
    for raw in rest:
        tok = shell_tokens.unquote_word(raw)
        if take_next is not None:
            if take_next == "dest":
                dest_opt = raw
            take_next = None
        elif tok in _CP_DEST_OPTS:
            take_next = "dest"
        elif tok in _CP_VALUE_OPTS:
            take_next = "other"
        elif tok.startswith("--target-directory="):
            dest_opt = raw.split("=", 1)[1]
        elif tok.startswith("-") and tok != "-":
            continue
        else:
            positionals.append(raw)
    if dest_opt is not None:
        return dest_opt
    return positionals[-1] if positionals else None


_SED_SCRIPT_OPTS = {"-e", "-f", "--expression", "--file"}


def _sed_files(rest: list[str]) -> list[str]:
    """The FILE operands of a `sed`, with the script expression excluded.

    `sed -i 's/a/b/' f` has two positionals and only one is a path; resolving the
    script as a path made every such command deny (12 in the measured corpus).
    When `-e`/`-f` supplies the script, every positional is a file instead."""
    words = [shell_tokens.unquote_word(t) for t in rest]
    script_seen = any(
        w in _SED_SCRIPT_OPTS or w.startswith("--expression=") or w.startswith("--file=")
        for w in words
    )
    files: list[str] = []
    skip_value = False
    for raw, word in zip(rest, words):
        if skip_value:
            skip_value = False
            continue
        if word in _SED_SCRIPT_OPTS:
            skip_value = True
            continue
        if word.startswith("-") and word != "-":
            continue
        if not script_seen:
            script_seen = True
            continue
        files.append(raw)
    return files


# `patch` forms that read the diff and write nothing. `-C`/`--check` is NOT here:
# measured on GNU patch 2.7.6, `patch -C` exits 2 on the forms this guard sees,
# so treating it as inert would allow a command that never runs anyway.
_PATCH_INERT = {"--dry-run", "-h", "--help", "--version", "-v"}
# `git apply` forms that report on the diff instead of applying it.
_GIT_APPLY_INERT = {"--check", "--stat", "--numstat", "--summary", "-h", "--help"}


def _patch_write_target(rest: list[str], eff_cwd: str) -> str | None:
    """Where a `patch` invocation writes: the `-o FILE`, the `-d DIR` it moves to
    first, or — since the paths come from the diff, not the command line — the
    cwd itself."""
    words = [shell_tokens.unquote_word(t) for t in rest]
    if set(words) & _PATCH_INERT:
        return None
    pending: str | None = None
    for raw, word in zip(rest, words):
        if pending == "-o":
            return _canon_target(raw, eff_cwd)
        if pending == "-d":
            return _canon_cwd(_resolve_dir(raw, eff_cwd))
        if word in ("-o", "--output"):
            pending = "-o"
            continue
        if word.startswith("--output="):
            return _canon_target(raw.split("=", 1)[1], eff_cwd)
        if word in ("-d", "--directory"):
            pending = "-d"
            continue
        if word.startswith("--directory="):
            return _canon_cwd(_resolve_dir(raw.split("=", 1)[1], eff_cwd))
        pending = None
    return _canon_cwd(eff_cwd)


def _resolve_dir(raw: str, base: str) -> str:
    word = _operand_word(raw)
    return word if os.path.isabs(word) else os.path.join(base, word)


def _git_write_target(seg: list[str], rest: list[str], default_cwd: str) -> str | None:
    """Where a `git` segment writes, for the two subcommands that write paths the
    command line does not name: `apply` and `stash apply` take them from the diff,
    so the repo directory is the only decidable signal.

    Keyed off the SUBCOMMAND rather than off `"apply"` appearing anywhere in the
    segment, which denied `git help apply` and `git log --grep apply`. Where the
    scanner cannot say which token is the subcommand, the old membership reading
    is kept: dropping it there would widen the guard, and `git stash apply` is the
    reason the membership test cannot simply be deleted."""
    words = [shell_tokens.unquote_word(t) for t in rest]
    invocation = git_cwd.scan_git_invocation(seg)
    repo_cwd = git_cwd.segment_git_cwd(seg, default_cwd)
    if invocation is None or invocation.doubt:
        return _canon_cwd(repo_cwd) if "apply" in words else None
    if invocation.subcommand == "apply" and not (set(words) & _GIT_APPLY_INERT):
        return _canon_cwd(repo_cwd)
    if invocation.subcommand == "stash" and "apply" in words:
        return _canon_cwd(repo_cwd)
    return None


def _verb_write_target(seg: list[str], default_cwd: str) -> str | None:
    """The canon path the write VERB heading `seg` would write, or None.

    Scope, which is the whole reason the two cwds are separate arguments: a
    subcommand's path operands resolve in the directory that command runs in
    (`git -C <dir>`), while a redirect in the same segment is opened by the SHELL
    in `default_cwd`. The redirect is therefore not scanned here at all."""
    if not seg:
        return None
    verb = os.path.basename(shell_tokens.unquote_word(seg[0])) if seg[0] else ""
    rest = _operands(seg[1:])

    if verb == "git":
        return _git_write_target(seg, rest, default_cwd)
    if verb == "patch":
        return _patch_write_target(rest, default_cwd)
    if verb == "sed" and _sed_in_place(rest):
        for raw in _sed_files(rest):
            hit = _canon_target(raw, default_cwd)
            if hit:
                return hit
        return None
    if verb == "tee":
        for raw in rest:
            if shell_tokens.unquote_word(raw).startswith("-"):
                continue
            hit = _canon_target(raw, default_cwd)
            if hit:
                return hit
        return None
    if verb in ("cp", "mv"):
        dest = _cp_mv_dest(rest)
        return _canon_target(dest, default_cwd) if dest else None
    if verb == "dd":
        for raw in rest:
            if shell_tokens.unquote_word(raw).startswith("of="):
                return _canon_target(raw.split("=", 1)[1], default_cwd)
        return None
    if verb == "sort":
        return _sort_write_target(rest, default_cwd)
    return None


def _sort_write_target(rest: list[str], eff_cwd: str) -> str | None:
    """`sort -o f` writes `f` in place — its three spellings, `-o f`, `-of` and
    `--output=f`."""
    pending = False
    for raw in rest:
        word = shell_tokens.unquote_word(raw)
        if pending:
            return _canon_target(raw, eff_cwd)
        if word == "-o":
            pending = True
        elif word.startswith("--output="):
            return _canon_target(raw.split("=", 1)[1], eff_cwd)
        elif word.startswith("-o") and not word.startswith("--") and len(word) > 2:
            return _canon_target(raw[raw.index("o") + 1:], eff_cwd)
    return None


def _segment_write_target(seg: list[str], default_cwd: str) -> str | None:
    """The canon path one command segment writes in place, or None.

    The segment is read twice: once as it stands, then once with its command
    PREFIX removed (`sudo`, `env -u A`, `xargs -P4`, a `VAR=v` assignment), so a
    wrapped write verb is seen. `recognized=False` means the prefix stripper met
    an option outside its measured allowlist and no longer knows which token is
    the command word — the retry is skipped and the segment reads exactly as it
    did before this fallback existed."""
    if not seg:
        return None
    hit = _verb_write_target(seg, default_cwd)
    if hit:
        return hit
    stripped, recognized = shell_tokens.strip_command_prefix(seg)
    if recognized and stripped and stripped != seg:
        return _verb_write_target(stripped, default_cwd)
    return None


_MASK = "\x01"


def _mask_quoted(command: str) -> tuple[str, list[str]] | None:
    """`command` with each quoted region replaced by a punctuation-free
    placeholder, plus the regions in order, or None on an unterminated quote.

    Operator-hood is settled before quote removal, and the punctuation lexer
    settles it WRONG for a quote that opens mid-word: it splits `<` and `>` out
    of `--format="%H%n%an <%ae>%n%s"` and reports two redirects the shell never
    performs. Measured over the harvested corpus, that one reading moved 456
    commands from allow to deny. Masking answers the quoting question first, so
    only operators the shell would honour reach the lexer at all.
    """
    regions: list[str] = []
    out: list[str] = []
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        if c == "\\" and i + 1 < n:
            out.append(command[i:i + 2])
            i += 2
            continue
        if c not in "'\"":
            out.append(c)
            i += 1
            continue
        j = i + 1
        while j < n:
            if c == '"' and command[j] == "\\" and j + 1 < n:
                j += 2
                continue
            if command[j] == c:
                break
            j += 1
        if j >= n:
            return None
        out.append(f"{_MASK}{len(regions)}{_MASK}")
        regions.append(command[i:j + 1])
        i = j + 1
    return "".join(out), regions


def _unmask(token: str, regions: list[str]) -> str:
    """`token` with its placeholders replaced by the quoted text they stand for."""
    for index, region in enumerate(regions):
        token = token.replace(f"{_MASK}{index}{_MASK}", region)
    return token


def _redirect_write(command: str, shell_cwd: str) -> str | None:
    """The canon path any output redirection in `command` opens, or None.

    Scanned over the WHOLE command, not per segment, for two reasons: every
    redirect is opened by the shell in the shell's own cwd, so segmentation
    carries no information here; and the text must stay raw, because a redirect
    INSIDE a command substitution (`$(echo x > f)`) is a real write that
    dropping substitutions would hide."""
    masked = _mask_quoted(command)
    if masked is None:
        return None
    text, regions = masked
    try:
        tokens = shell_tokens.tokenize(text)
    except Exception:
        return None
    for i, tok in enumerate(tokens):
        if tok not in shell_tokens.REDIRECT_OPS:
            continue
        operand = tokens[i + 1] if i + 1 < len(tokens) else None
        if operand in shell_tokens.SEPARATORS or operand in shell_tokens.REDIRECT_OPS:
            continue
        if operand == "$" and i + 2 < len(tokens) and tokens[i + 2] == "(":
            continue  # the target is whatever a substitution prints — unknowable
        if operand is not None:
            operand = _unmask(operand, regions)
        target = shell_tokens.redirect_write_target(tok, operand)
        if target is None:
            continue
        hit = _canon_target(target, shell_cwd)
        if hit:
            return hit
    return None


_ASSIGNMENT = re.compile(r"\A([A-Za-z_][A-Za-z0-9_]*)=(.*)\Z", re.S)
_EXPANSION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")

# Variables the shell always has, whatever happened in an earlier tool call, and
# whose value this process shares because the hook runs as the same user.
_AMBIENT = ("HOME", "PWD", "TMPDIR", "USER", "LOGNAME", "SHELL", "HOSTNAME")


def _variable_values(tokens: list[str]) -> tuple[dict[str, str], set[str]]:
    """The variable values this command settles by itself, and the names it
    assigns at all.

    The two differ, and the difference is the whole point: `D=/some/dir` gives a
    value the scan can substitute, while `D=$(mktemp -d)` gives only the
    knowledge that `$D` is NOT the unset variable the deny below assumes."""
    values = {name: os.environ[name] for name in _AMBIENT if name in os.environ}
    assigned: set[str] = set()
    for token in tokens:
        match = _ASSIGNMENT.match(token)
        if not match:
            continue
        name, raw = match.groups()
        assigned.add(name)
        word = shell_tokens.unquote_word(raw)
        if "$" not in word and "`" not in word:
            values.setdefault(name, word)
    return values, assigned


def _expand_variables(command: str, values: dict[str, str]) -> str:
    """`command` with every variable of known value substituted.

    Without this the scan reads `$D/x.md` as a literal directory named `$D`
    under the cwd, which is the right answer only while `$D` is unset. When the
    command assigns `D` itself the shell opens a completely different path --
    and resolving it is what keeps a `D=<canon>` assignment DENIED instead of
    trading one wrong verdict for the opposite one."""
    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        return values.get(name, match.group(0))
    return _EXPANSION.sub(replace, command)


def _commit_cwd_settled(command: str, cwd: str) -> bool:
    """Whether the directory a detected `git commit` runs in is actually known.

    The commit deny is only sound when the cwd it fires on is the cwd the
    command really has, and two ways of not knowing it both end at the SAME
    innocent-looking answer -- `payload_cwd`, which in canon is a deny. The cwd
    resolver bails to `payload_cwd` on a command its lexer refuses (52 corpus
    commands: a `git commit -F -` whose here-document body carries an
    apostrophe, every one of them `cd`-ing into a worktree first), and an
    unresolved variable leaves a `$` in the answer -- where reading it as a
    canon-relative directory is wrong in both directions, since bash sends
    `cd $UNSET` to HOME rather than into the current directory.

    Neither case says the commit is safe; both say this hook cannot tell, which
    in a fail-open guard is an allow. The refusal case still has to be narrowed
    to commands whose cwd the answer actually MISSES, though -- reading every
    refusal as doubt would hand the same escape to `cd <canon> && git commit`,
    the very command this deny exists for."""
    if "$" in cwd:
        return False
    return not _unfollowed_cd(command, cwd)


def _unfollowed_cd(command: str, cwd: str) -> bool:
    """Whether `command` changes directory somewhere the resolved `cwd` is not.

    The resolver answers only the LEADING `cd`, so `WT=<worktree> ; cd "$WT" ;
    git commit` gets `payload_cwd` back -- and in canon that is a deny on a
    commit that lands in a worktree. A `cd` the resolver did not take is exactly
    the evidence that its answer is not this command's cwd.

    It cannot become a blanket escape, because a `cd` the resolver DID take
    compares equal and settles nothing loose: `cd <canon> && git commit` still
    denies."""
    try:
        words = shlex.split(command)
    except ValueError:
        return True
    for index, word in enumerate(words[:-1]):
        if word != "cd":
            continue
        target = words[index + 1]
        if not os.path.isabs(target):
            target = os.path.join(cwd, target)
        if os.path.realpath(target) != os.path.realpath(cwd):
            return True
    return False


_SUBSTITUTION = re.compile(r"\$\((?:[^()]|\([^()]*\))*\)", re.S)
_DELIMITER = re.compile(r"<<-?\s*(?P<q>['\"]?)(?P<word>[^\s'\"]+)(?P=q)")

# A shell named ANYWHERE in the command, not just as the here-document's
# consumer: the body may be executed by a later statement, or by a function
# whose definition rebinds an inert-looking name (`cat() { bash; }`), and both
# are shapes `heredoc_body_runs_as_shell` reads as consumer-inert.
_SHELL_WORD = re.compile(r"(?<![\w./-])(?:ba|z|k|da)?sh\b|(?<![\w./-])(?:eval|source|exec)\b")


def _outside_heredoc_body(command: str) -> str:
    """`command` reduced to the text the SHELL reads as syntax when a here-document
    body survived `strip_heredoc_bodies`.

    A body it refuses to strip is data for an interpreter, and the write scan was
    reading that data as shell: prose, python and log text supplied 70 of the
    corpus's false denies, with targets like `<canon>/scripts/=0` and
    `<canon>/scripts/Qwen`. Only two parts of such a command are syntax -- the
    line the here-document opens on, and any command substitution, which an
    EXPANDING body really does run. Text after the body is dropped with it,
    which can hide a real later write; that is the fail-open direction, and the
    body is the only place this hook cannot tell data from syntax at all."""
    index = command.find("<<")
    if index < 0 or shell_tokens.heredoc_body_runs_as_shell(command):
        return command
    if _SHELL_WORD.search(command):
        return command  # a shell named anywhere may yet run the body
    line_end = command.find("\n", index)
    delimiter = _DELIMITER.match(command[index:])
    if line_end < 0 or not delimiter:
        return command
    # Text after the body is command line again, so it is kept -- a real write
    # on a later statement (`EOF` then `echo x > scripts/existing.py`) must not
    # ride out on the body's exemption.
    body = command[line_end:]
    lines = body.split("\n")
    word = delimiter.group("word")
    ends = [n for n, line in enumerate(lines) if line.strip() == word]
    tail = "\n".join(lines[ends[-1] + 1:]) if ends else ""
    return " ; ".join([command[:line_end], *_SUBSTITUTION.findall(body), tail])


def _normalized(command: str) -> tuple[str, set[str]]:
    """`command` with newlines read as the statement separators bash reads them
    as and every known-value variable substituted, plus the names it assigns.

    Both readings belong to the same normalization because both are about what
    the SHELL sees rather than what the tokenizer sees, and both the commit
    detector and the write scan need them: without the first, a `cd` on line 2
    never moves the shell; without the second, `cd "$WT"` reads as a directory
    literally named `$WT` under the cwd -- and in canon that one misreading
    denied 52 corpus commits that target a worktree."""
    command = _statement_newlines(command)
    try:
        tokens = shell_tokens.tokenize(command)
    except Exception:
        return command, set()
    values, assigned = _variable_values(tokens)
    return _expand_variables(command, values), assigned


def _live_expansion(target: str, assigned: set[str]) -> bool:
    """Whether `target` still carries a `$VAR` that this command assigns.

    The deny on an unexpanded variable is CORRECT for the case it was written
    for (`_unexpanded_variable_note`): a variable assigned in an EARLIER tool
    call is unset in this fresh shell, so `$S/x.md` really does land under the
    cwd. It is wrong the moment the command assigns the variable itself from
    something unresolvable (`D=$(mktemp -d)`), because then the shell opens a
    path this hook never saw. Same bytes, opposite verdicts, so the two are
    separated here rather than merged -- and the unknowable side is doubt, which
    in a fail-open guard means allow."""
    names = {a or b for a, b in _EXPANSION.findall(target)}
    return bool(names & assigned)


def _canon_bash_write(command: str, payload_cwd: str) -> str | None:
    """Best-effort: the canon path a Bash command writes in place, or None.
    Fail-open on any parse error (allow).

    The command's default cwd is computed ONCE (a leading `cd <dir>` moves the
    SHELL, hence every later segment), and each segment's own write targets are
    then resolved against the cwd that segment's own command runs in.

    Newlines are normalized to statement separators first, for the same reason
    the commit detector does it: a newline ends a statement in bash but not in
    the tokenizer, so without this a `cd` on line 2 never moves the shell and
    every later line's relative write is resolved against the wrong directory --
    46 of the corpus's false denies were exactly that."""
    command, assigned = _normalized(_outside_heredoc_body(command))
    try:
        tokens = shell_tokens.tokenize(command)
    except Exception:
        return None
    if not tokens:
        return None
    shell_cwd = git_cwd.command_default_cwd(command, payload_cwd)
    hit = _redirect_write(command, shell_cwd)
    if hit and not _live_expansion(hit, assigned):
        return hit
    for _sep, seg in shell_tokens.split_segments(shell_tokens.drop_substitutions(tokens)):
        if not seg:
            continue
        hit = _segment_write_target(seg, shell_cwd)
        if hit and not _live_expansion(hit, assigned):
            return hit
    return None


def _unexpanded_variable_note(target: str) -> str:
    """The extra sentence a target still carrying a `$` needs, or "".

    This deny is CORRECT and stays: each Bash tool call is a fresh shell, so a
    variable assigned in an earlier call is unset here and `$S/x.md` really does
    resolve under the cwd — which, in canon, is a canon write. What the bare
    message lacked was the CAUSE: `<canon>/$S/x.md` reads as a bizarre literal
    path rather than as the predictable consequence of a shell that does not
    persist. The hook cannot tell an unset `$S` from an `$S` that would have
    expanded to canon anyway (identical bytes reach it either way), so naming the
    cause is the whole of the fix — nothing about what is permitted changes.
    """
    if "$" not in target:
        return ""
    return (
        " Note the literal `$` in that path: shell state does not persist between tool "
        "calls, so a shell variable used in a path is always unset here and the path "
        "lands under the current directory instead of where you meant. Write the "
        "absolute path out literally."
    )


_WORKTREE_HINT_LIMIT = 5


def _linked_worktrees(core_root: str) -> list[str]:
    """Absolute paths of the repo's LINKED worktrees, primary omitted (git lists it
    first), or [] on any failure — the deny message then degrades to generic advice
    rather than the hook failing."""
    try:
        proc = subprocess.run(
            ["git", "-C", core_root, "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_S, check=False,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    paths = [ln[len("worktree "):].strip() for ln in proc.stdout.splitlines()
             if ln.startswith("worktree ")]
    return [p for p in paths[1:] if p]


def _commit_deny_msg(target: str) -> str:
    """The deny for a `git commit` whose effective cwd is canon.

    Separate from `_deny_msg` because the cause and the cure are specific, and the
    generic text actively misleads here. This deny is CORRECT and is not a
    misdetected worktree commit: a linked worktree keeps its index at
    `<git-common-dir>/worktrees/<name>/index`, so a commit running in canon reads
    canon's index and can never reach the worktree's staged content — it commits
    canon or fails with "nothing to commit". Reproduction, evidence and the falsified
    alternative: docs/decisions/canon-guard-bare-commit-verdict.md (#44).

    So what the caller needs is the ADDRESSED form of their own command, not the
    isolation advice `_deny_msg` gives — a session hitting this already has a
    worktree; it just did not name it.
    """
    worktrees = _linked_worktrees(target)
    shown = worktrees[:_WORKTREE_HINT_LIMIT]
    if len(shown) == 1:
        hint = f" This repo's linked worktree: `git -C {shown[0]} commit ...`."
    elif shown:
        more = "" if len(shown) == len(worktrees) else f", +{len(worktrees) - len(shown)} more"
        hint = f" Linked worktrees of this repo: {', '.join(shown)}{more}."
    else:
        hint = (" This repo has no linked worktree yet — make one with "
                "`scripts/session-isolate.sh <task-name>`.")
    return (
        f"Refusing to run `git commit` in canon ({target}) from a live agent session. This command "
        f"names no target this guard can read — neither `git -C <dir>` nor a leading "
        f"`cd <dir> &&` / `cd <dir> ;` "
        f"— and a `cd` does not persist between tool calls, so it is taken to run in the session's "
        f"own directory: canon. A commit there commits canon's index, and a linked worktree keeps a "
        f"SEPARATE index whose staged changes are invisible from here, so this would either fail "
        f"with \"nothing to commit\" or land a real commit in canon. Name the target tree "
        f"explicitly: `git -C <worktree> commit -m ...` — that is also the only form this guard "
        f"reads, so a mid-command `cd`, a subshell or `GIT_DIR` is refused here even when it really "
        f"would have reached the worktree.{hint}"
    )


def _deny_msg(target: str) -> str:
    return (
        f"Refusing to modify canon ({target}) directly from a live agent session.{_unexpanded_variable_note(target)} Canon "
        f"checkouts (the PRIMARY Core-instructions worktree, on any branch, and any path "
        f"registered as a canon root) are read-only from here — this is the live hook code "
        f"and reference source every session on the machine runs. Do the work in an isolated "
        f"copy instead: `scripts/session-isolate.sh <task-name>` (a linked git worktree, or a "
        f"second mount for other VCS backends). Writable without isolation: linked worktrees, "
        f"second mounts, personal auto-memory under ~/.claude-agent, and /tmp."
    )


def decide(payload: dict) -> str | None:
    """Return a deny reason, or None to allow. Fail-open on any unexpected shape."""
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}

    if tool_name == "Bash":
        command = (tool_input.get("command") or "").strip()
        if not command:
            return None
        # A here-document body is DATA, not syntax: a Markdown blockquote line
        # inside one is not a `>` redirect, and a `git commit` MENTIONED in one is
        # not a commit. Strip once, here, so that BOTH consumers below read the
        # same text — `_is_git_commit` has the identical defect, and neither
        # consumer's fail-open path can cover it, because `shlex` is a lexer and
        # never raises on a body. Body text only is removed, so a canon path
        # riding the command line survives this byte-for-byte.
        command = shell_tokens.strip_heredoc_bodies(command)
        payload_cwd = payload.get("cwd") or os.getcwd()
        # The commit detector reads statement boundaries; the write scan below
        # deliberately does not, so the rewrite is scoped to this pair.
        commit_command, _assigned = _normalized(command)
        if _is_git_commit(commit_command):
            cwd = git_cwd.effective_git_cwd(commit_command, payload_cwd)
            settled = _commit_cwd_settled(commit_command, cwd)
            target_dir = _nearest_existing_dir(cwd) if settled else None
            if target_dir is not None and _is_primary_core(target_dir):
                return _commit_deny_msg(os.path.realpath(str(_core_root())))
            # A commit targeting a worktree does not end the scan: the SAME
            # command can still write canon (`git -C <wt> commit && cp x <canon>`).
        # Best-effort deny of an in-place write into canon.
        hit = _canon_bash_write(command, payload_cwd)
        if hit:
            return _deny_msg(hit)
        return None

    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return None
    if not os.path.isabs(file_path):
        return None  # relative path — not resolvable to a specific checkout, fail-open
    target_dir = _nearest_existing_dir(file_path)
    if target_dir is None:
        return None
    if _is_in_canon(target_dir, file_path):
        return _deny_msg(os.path.realpath(file_path))
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0

    try:
        reason = decide(payload)
    except Exception:
        return 0

    if reason:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
