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
are denied. Always exits 0 — a hook crash must never wedge the workflow.

DENY is signaled with the PreToolUse permissionDecision JSON on stdout (mirrors
hook-guard-destructive-rm.py):
  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
   "permissionDecision": "deny", "permissionDecisionReason": "..."}}
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import config_root, git_cwd  # noqa: E402

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


def _is_git_commit(command: str) -> bool:
    """True iff the command runs `git commit` (tokenized, not substring). Any
    parse doubt => False (allow)."""
    try:
        tokens = shlex.split(command)
    except Exception:
        return False
    for i in range(len(tokens) - 1):
        if os.path.basename(tokens[i]) == "git" and tokens[i + 1] == "commit":
            return True
    return False


# --- best-effort in-place Bash-write detection (extends the git-commit deny) ---
#
# The Bash branch of this guard used to deny ONLY a literal `git commit` in canon,
# so every other in-place write verb (`sed -i`, `>>`, `tee`, `cp`/`mv`, `patch`,
# `git apply`) slipped past it — a hole under the "canon is read-only" promise.
# The helpers below close the DETECTABLE write verbs, fail-open (any parse doubt
# ALLOWS), and allow the identical verbs targeting a worktree / second mount.
#
# NAMED RESIDUAL (not closable by any PreToolUse hook, do not claim otherwise):
# an interpreter one-liner that opens a path for writing internally
# (`python3 -c "open(p,'w')"`, `perl -e '...'`, an `eval`'d string, any program
# that writes a file with no shell-visible write verb) and a redirection glued to
# a preceding word (`foo>bar`, `2>bar`) carry no token this hook can key on. The
# durable guarantee for those is the tool-level Edit/Write deny plus keeping
# feature work out of the canon checkout entirely.

_BASH_SEPS = {";", "&&", "||", "|", "|&", "&"}


def _split_segments(tokens: list[str]):
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


def _canon_target(candidate: str, eff_cwd: str) -> str | None:
    """Realpath of `candidate` (resolved rel to `eff_cwd`) iff it lands in canon,
    else None. A not-yet-existing write target resolves through its nearest
    existing parent so a redirect creating a new file in canon is still caught."""
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
    keeps copying OUT of canon (canon source, outside dest) allowed."""
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


def _segment_write_target(seg: list[str], eff_cwd: str) -> str | None:
    """The canon path a single command segment would write in place, or None.
    Covers output redirection, `sed -i`, `tee`, `cp`/`mv` dest, `patch`, and
    `git apply`; every path is resolved rel to `eff_cwd`."""
    if not seg:
        return None

    # (a) output redirection anywhere in the segment: `> f`, `>> f`, glued `>f`/`>>f`.
    for i, tok in enumerate(seg):
        redirect_tgt: str | None = None
        if tok in (">", ">>"):
            redirect_tgt = seg[i + 1] if i + 1 < len(seg) else None
        elif tok.startswith(">") and tok.strip(">"):
            redirect_tgt = tok.lstrip(">")
        if redirect_tgt:
            hit = _canon_target(redirect_tgt, eff_cwd)
            if hit:
                return hit

    # (b) verb-based writers.
    verb = os.path.basename(seg[0]) if seg[0] else ""
    rest = _operands_until_redirect(seg[1:])

    if verb == "patch":
        return _canon_cwd(eff_cwd)
    if verb == "git" and "apply" in rest:
        return _canon_cwd(eff_cwd)
    if verb == "sed" and _sed_in_place(rest):
        for tok in rest:
            if tok.startswith("-"):
                continue
            hit = _canon_target(tok, eff_cwd)
            if hit:
                return hit
        return None
    if verb == "tee":
        for tok in rest:
            if tok.startswith("-"):
                continue
            hit = _canon_target(tok, eff_cwd)
            if hit:
                return hit
        return None
    if verb in ("cp", "mv"):
        dest = _cp_mv_dest(rest)
        if dest:
            return _canon_target(dest, eff_cwd)
        return None
    return None


def _canon_bash_write(command: str, payload_cwd: str) -> str | None:
    """Best-effort: the canon path a non-`git commit` Bash command writes in
    place, or None. Fail-open on any parse error (allow), reusing the leading-`cd`
    resolution so `cd <wt> && sed -i ... f` keys off the worktree, not the
    session cwd."""
    try:
        tokens = shlex.split(command)
    except Exception:
        return None
    if not tokens:
        return None
    eff_cwd = git_cwd.effective_git_cwd(command, payload_cwd)
    for seg in _split_segments(tokens):
        hit = _segment_write_target(seg, eff_cwd)
        if hit:
            return hit
    return None


def _deny_msg(target: str) -> str:
    return (
        f"Refusing to modify canon ({target}) directly from a live agent session. Canon "
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
        payload_cwd = payload.get("cwd") or os.getcwd()
        if _is_git_commit(command):
            cwd = git_cwd.effective_git_cwd(command, payload_cwd)
            target_dir = _nearest_existing_dir(cwd)
            if target_dir is None:
                return None
            if _is_primary_core(target_dir):
                return _deny_msg(os.path.realpath(str(_core_root())))
            return None
        # Non-commit Bash: best-effort deny of an in-place write into canon.
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
