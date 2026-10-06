#!/usr/bin/env python3
"""PreToolUse hook (Bash): deny a shell write of literal text into a source tree.

Difficulty removed: the production-edit gate (hook-state-gate.py) and the
cross-session scope guard are registered for Edit|Write only, so `sed -i`,
`perl -i`, `cat > f <<EOF`, `echo text > f`, `printf text > f` and `echo text |
tee f` edit a tracked source file with no plan gate, no scope check and no edit
ledger entry. A gate keyed on the tool name is bypassed by any other tool that
reaches the same effect; this hook gates the EFFECT on the second channel.

The verdict is a function of shell SYNTAX and filesystem facts only — never of
the written text, which is why a heredoc body is blanked before tokenizing and a
quoted string that merely mentions `sed -i` changes nothing. A command is denied
iff it has a WRITE SHAPE aimed at a target that lies in a TRACKED TREE and in no
allowlisted root:
  * write shapes: an in-place editor flag (`sed -i`, `perl -i`), or a redirect
    (`>`, `>>`) / `tee` whose producer is a literal-text command — `echo`,
    `printf`, or `cat` with no file operand (which is what a heredoc or here-
    string feeds);
  * tracked tree: the target's resolved path lies inside a git work tree
    (`git rev-parse --is-inside-work-tree`, as hook-guard-canon-readonly.py
    asks git) or under an ancestor holding a `.arc` marker directory (a
    filesystem-only test; no VCS client is called) — the ancestor walk stops at
    the home directory, whose own `.arc` is that client's global state, not a
    working-copy root;
  * allowlist: the engine gate's own exemptions (`exempt_paths.is_engine_exempt`:
    memory roots and `/tmp/`) and `exempt_paths.scratch_roots()`, matched against
    BOTH the nominal path and its symlink-resolved path — a match on either
    allows, so a memory root that resolves into a git or arc tree is never denied.

Everything else is ALLOWED: generator redirects (`python3 gen.py > out.py`),
`cat a > b` (a copy), `cp`/`mv`/`install`, git/arc/patch, a plain directory with
neither a work tree nor a `.arc` marker, and any command this hook cannot read.
Fail-open is total: a garbage envelope, a tokenizer error, an unrecognized
heredoc shape, a process substitution, or a target holding an unexpanded `$VAR`
(each Bash call is a fresh shell, so the hook cannot tell where it points) all
exit 0 with no deny JSON. Always exits 0 — a hook crash must never wedge a call.

NAMED RESIDUAL — shell-invisible writes: `python3 -c "open(p,'w')"`, `awk -i
inplace`, `dd of=f`, `find -exec sed -i`, `xargs sed -i`, `>|f`, `&>f`, and a
redirect glued to a word carry no token this hook reads; the prose norm
("Edit tools only for source edits", developer SKILL.md and CLAUDE.md § Limits)
covers them and post-review fixes alike. A quoted argument that begins with `>`
(`echo '>x'`) is read as a redirect, because the shared lexer drops quote
information. This guard raises the cost of an accidental bypass; it is not an
evasion-proof boundary.

DENY is signaled with the PreToolUse permissionDecision JSON on stdout (the
shape hook-guard-canon-readonly.py prints).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentctl import exempt_paths  # noqa: E402
from lib import bash_write_targets, git_cwd, shell_tokens  # noqa: E402

HOOK_NAME = "hook-guard-bash-source-edit.py"
GIT_TIMEOUT_S = 3

_SEPARATORS = frozenset({";", "&&", "||", "|", "|&", "&"})
_PIPES = frozenset({"|", "|&"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_LITERAL_PRODUCERS = frozenset({"echo", "printf"})


def _drop_fd_duplications(tokens: list[str]) -> list[str]:
    """`2>&1` / `>&2` tokenize as `2 > & 1`, and the lone `&` would split the
    segment in two. Remove the whole duplication so the redirect that follows it
    still belongs to its own command."""
    out: list[str] = []
    i = 0
    while i < len(tokens):
        if (tokens[i] == ">" and i + 2 < len(tokens) and tokens[i + 1] == "&"
                and (tokens[i + 2].isdigit() or tokens[i + 2] == "-")):
            if out and out[-1].isdigit():
                out.pop()
            i += 3
            continue
        out.append(tokens[i])
        i += 1
    return out


def _pipelines(tokens: list[str]) -> list[list[list[str]]]:
    """Segments grouped into pipelines: a `|`/`|&` keeps the next segment in the
    same pipeline, any other separator starts a new one."""
    pipelines: list[list[list[str]]] = [[]]
    seg: list[str] = []
    for tok in tokens:
        if tok in _SEPARATORS:
            if seg:
                pipelines[-1].append(seg)
                seg = []
            if tok not in _PIPES:
                pipelines.append([])
        else:
            seg.append(tok)
    if seg:
        pipelines[-1].append(seg)
    return [p for p in pipelines if p]


def _command_and_rest(seg: list[str]) -> tuple[str, list[str]]:
    i = 0
    while i < len(seg) and _ASSIGNMENT.match(seg[i]):
        i += 1
    if i >= len(seg):
        return "", []
    return os.path.basename(seg[i]), seg[i + 1:]


def _operands(rest: list[str]) -> list[str]:
    """Tokens before the first redirection operator."""
    out: list[str] = []
    for tok in rest:
        if tok and tok[0] in "<>":
            break
        out.append(tok)
    return out


def _sed_in_place_files(rest: list[str]) -> list[str]:
    """File operands of `sed -i ...`, or [] when it is not an in-place sed. The
    first positional is the script unless `-e`/`-f` supplied one."""
    in_place = script_given = skip_next = False
    positionals: list[str] = []
    for tok in _operands(rest):
        if skip_next:
            skip_next = False
        elif tok in ("-e", "-f", "--expression", "--file"):
            script_given = skip_next = True
        elif tok.startswith(("--expression=", "--file=")):
            script_given = True
        elif tok == "--in-place" or tok.startswith("--in-place="):
            in_place = True
        elif tok.startswith("-") and tok != "-" and not tok.startswith("--"):
            if "i" in tok[1:]:
                in_place = True
            elif tok[-1] in "ef":
                script_given = skip_next = True
        elif tok.startswith("-") or not tok:
            continue
        else:
            positionals.append(tok)
    if not in_place:
        return []
    return positionals if script_given else positionals[1:]


def _perl_in_place_files(rest: list[str]) -> list[str]:
    """File operands of `perl -i ...`, or [] when it is not an in-place perl. In
    a flag cluster `i` swallows the rest as the backup suffix and `e`/`E` as the
    script; the first positional is a script file unless `-e` supplied one."""
    in_place = script_given = skip_next = False
    positionals: list[str] = []
    for tok in _operands(rest):
        if skip_next:
            skip_next = False
        elif tok.startswith("-") and tok != "-" and not tok.startswith("--"):
            for pos, ch in enumerate(tok[1:], start=1):
                if ch == "i":
                    in_place = True
                    break
                if ch in "eE":
                    script_given = True
                    skip_next = pos == len(tok) - 1
                    break
        elif tok.startswith("-") or not tok:
            continue
        else:
            positionals.append(tok)
    if not in_place:
        return []
    return positionals if script_given else positionals[1:]


def _file_operands(rest: list[str]) -> list[str]:
    return [t for t in _operands(rest) if t and t != "-" and not t.startswith("-")]


def _cat_reads_only_stdin(rest: list[str]) -> bool:
    """`cat` with no file operand and no `<` input: its stdin is a heredoc /
    here-string body (blanked before tokenizing) or an upstream pipe."""
    return not _file_operands(rest) and not any(t.startswith("<") for t in rest)


def _is_literal_start(seg: list[str]) -> bool:
    """True iff `seg` begins a pipeline with a literal-text producer: echo,
    printf, or a stdin-only `cat`."""
    verb, rest = _command_and_rest(seg)
    return verb in _LITERAL_PRODUCERS or (verb == "cat" and _cat_reads_only_stdin(rest))


def _absolute(token: str, cwd: str) -> str:
    token = os.path.expanduser(token)
    return token if os.path.isabs(token) else os.path.join(cwd, token)


def _segment_targets(seg: list[str], literal_pipeline: bool, eff_cwd: str) -> list[str]:
    verb, rest = _command_and_rest(seg)
    if verb in ("sed", "gsed"):
        files = _sed_in_place_files(rest)
    elif verb == "perl":
        files = _perl_in_place_files(rest)
    elif verb == "tee" and literal_pipeline:
        files = _file_operands(rest)
    elif verb in _LITERAL_PRODUCERS or (
            verb == "cat" and literal_pipeline and _cat_reads_only_stdin(rest)):
        return bash_write_targets.redirect_targets(seg, eff_cwd)
    else:
        return []
    return [_absolute(t, eff_cwd) for t in files]


def _write_targets(command: str, payload_cwd: str) -> list[str]:
    """Absolute write-target candidates of the write shapes in `command`; [] on
    any shape this hook does not read (fail-open)."""
    command = shell_tokens.neutralize_heredoc_constructs(command)
    if shell_tokens.has_process_substitution(command):
        return []
    try:
        tokens = shell_tokens.separator_exact_split(command)
    except Exception:
        return []
    if any(t.startswith("<<") for t in tokens):
        return []
    eff_cwd = git_cwd.effective_git_cwd(command, payload_cwd)
    targets: list[str] = []
    for pipeline in _pipelines(_drop_fd_duplications(tokens)):
        literal_pipeline = _is_literal_start(pipeline[0])
        for seg in pipeline:
            targets.extend(_segment_targets(seg, literal_pipeline, eff_cwd))
    return targets


def _under_roots(path: str, roots) -> bool:
    return any(path == root or path.startswith(root + os.sep) for root in roots)


def _allowlisted(nominal: str, resolved: str) -> bool:
    roots = exempt_paths.scratch_roots()
    return any(
        exempt_paths.is_engine_exempt(p) or _under_roots(p, roots)
        for p in (nominal, resolved)
    )


def _nearest_existing_dir(path: str) -> str | None:
    cur = Path(path)
    while not cur.is_dir():
        if cur.parent == cur:
            return None
        cur = cur.parent
    return str(cur)


def _arc_tree(directory: str) -> bool:
    """True iff an ancestor below the home directory holds a `.arc` marker
    directory. The home directory's own `.arc` is arc's global state (mount
    table, store, token), not a mount root, so the walk stops there —
    otherwise everything under home would be an arc tree."""
    home = os.path.realpath(Path.home())
    cur = Path(directory)
    for d in (cur, *cur.parents):
        if os.path.realpath(d) == home:
            return False
        if (d / ".arc").is_dir():
            return True
    return False


def _git_work_tree(directory: str) -> bool:
    try:
        proc = subprocess.run(
            ["git", "-C", directory, "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_S, check=False,
        )
    except Exception:
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _in_tracked_tree(resolved: str) -> bool:
    directory = _nearest_existing_dir(resolved)
    if directory is None:
        return False
    return _arc_tree(directory) or _git_work_tree(directory)


def _denied_target(candidate: str) -> str | None:
    """The resolved target iff `candidate` is in a tracked tree and not
    allowlisted. A path still holding `$VAR` or a backtick is unreadable here
    (the hook's environment is not the command's), so it is allowed."""
    if "$" in candidate or "`" in candidate:
        return None
    nominal = os.path.normpath(candidate)
    resolved = os.path.realpath(candidate)
    if _allowlisted(nominal, resolved):
        return None
    return resolved if _in_tracked_tree(resolved) else None


def _deny_msg(target: str) -> str:
    return (
        f"{HOOK_NAME}: refusing a shell write of literal text into a tracked source "
        f"tree ({target}). `sed -i`, `perl -i`, heredoc/here-string/`echo`/`printf` "
        f"redirects and `| tee` change source past the plan gate and the "
        f"cross-session scope check, because both govern only the Edit and Write "
        f"tools. Make this change with the Edit/Write tools — post-review fixes included. "
        f"Not blocked: /tmp and scratch roots, memory roots, generator redirects "
        f"(`python3 gen.py > out`), cp/mv, and git."
    )


def decide(payload: dict) -> str | None:
    if payload.get("tool_name") != "Bash":
        return None
    command = ((payload.get("tool_input") or {}).get("command") or "").strip()
    if not command:
        return None
    payload_cwd = payload.get("cwd") or os.getcwd()
    for candidate in _write_targets(command, payload_cwd):
        hit = _denied_target(candidate)
        if hit:
            return _deny_msg(hit)
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
