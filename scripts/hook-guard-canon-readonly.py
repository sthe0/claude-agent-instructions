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
`git -C <dir>` nor a leading `cd <dir> &&` — from a session whose directory is canon
is denied by design, not by a detection gap: a linked worktree keeps its own index,
so such a commit reaches canon's index and never the worktree's (reproduction and
verdict: docs/decisions/canon-guard-bare-commit-verdict.md, #44). A commit that
reaches a worktree by a route this module does not parse (a `cd` after the first
token, a subshell, `GIT_DIR`) is denied too — conservatively, per `git_cwd`'s
contract, and it was denied before that verdict as well. Here-document bodies and
here-string operands are DATA and are removed before any Bash command is
inspected (`lib/shell_tokens.py`), so a Markdown blockquote line inside a body is
not read as a redirect. Always exits 0 — a hook crash must never wedge the
workflow.

This guard raises the cost of an ACCIDENTAL canon write; it is not an
evasion-proof boundary. See NAMED RESIDUAL below for what reaches canon today.

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
from lib import bash_write_targets, config_root, git_cwd, shell_tokens  # noqa: E402

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
# WHAT THIS GUARD IS. It raises the cost of an ACCIDENTAL canon write. It is not
# an evasion-proof boundary and must not be described as one: anyone who wants to
# write canon from a Bash call can, and the list below says how. The durable
# guarantees are the tool-level Edit/Write deny and keeping feature work out of
# the canon checkout entirely (`scripts/session-isolate.sh`).
#
# NAMED RESIDUAL — shell-invisible writes, not closable by any PreToolUse hook:
# an interpreter one-liner that opens a path for writing internally
# (`python3 -c "open(p,'w')"`, `perl -e '...'`, an `eval`'d string, any program
# that writes a file with no shell-visible write verb) and a redirection glued to
# a preceding word (`foo>bar`, `2>bar`) carry no token this hook can key on.
#
# NAMED RESIDUAL — write verbs measured to reach canon TODAY, out of scope here
# and unchanged by the here-document handling: `exec 3>f`, `exec 3>>f`, `dd of=f`,
# `cp`/`mv` in forms the dest parser misses, `>|f`, `sort -o f`, `sed 'w f'`,
# `awk '{print > "f"}'`, and `python3 <<EOF` (an interpreter consumer, which the
# body stripper refuses to touch for exactly this reason). Closing these is a
# separate change with its own evidence; do not read their absence as coverage.
#
# NAMED RESIDUAL — a path holding an UNEXPANDED `$VAR` is denied BY DESIGN, not by
# accident. Each Bash tool call is a fresh shell, so `$S` assigned in an earlier
# call is unset and `cat > $S/x.md` really does write `<cwd>/$S/x.md`. The hook
# receives byte-identical input whether `$S` is unset or would have expanded into
# canon, so it cannot distinguish them and must deny both; `_deny_msg` names the
# cause instead of pretending the path was literal.
#
# NAMED RESIDUAL — spurious DENYs the body stripper leaves behind, all in the safe
# direction: only the FIRST here-document body is removed, so a second body on the
# same command (`cat <<A <<B`) is still read as syntax; a `$(` opened inside a
# quoted-delimiter body and closed after it leaves an unbalanced `)` in the
# residue; and an unbalanced QUOTE anywhere makes `shlex.split` raise, which is the
# module's one genuinely reachable fail-open path (measured — a here-document body
# never raises, which is why the stripper exists at all).

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


def _canon_bash_write(command: str, payload_cwd: str) -> str | None:
    """Best-effort: the canon path a non-`git commit` Bash command writes in
    place, or None. Fail-open on any parse error (allow), reusing the leading-`cd`
    resolution so `cd <wt> && sed -i ... f` keys off the worktree, not the
    session cwd. The write-target lexing (segments, redirect/`sed -i`/`tee`/
    `cp`/`mv` targets) lives in `lib/bash_write_targets.py`, which knows nothing
    of canon; every candidate it returns — already an absolute path, and in the
    same priority order the lexer used to search internally — is checked here,
    the only place that applies canon policy, stopping at the first hit."""
    eff_cwd = git_cwd.effective_git_cwd(command, payload_cwd)
    for candidate in bash_write_targets.command_write_targets(command, eff_cwd):
        hit = _canon_target(candidate, eff_cwd)
        if hit:
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
        f"names no target this guard can read — neither `git -C <dir>` nor a leading `cd <dir> &&` "
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
        if _is_git_commit(command):
            cwd = git_cwd.effective_git_cwd(command, payload_cwd)
            target_dir = _nearest_existing_dir(cwd)
            if target_dir is None:
                return None
            if _is_primary_core(target_dir):
                return _commit_deny_msg(os.path.realpath(str(_core_root())))
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
