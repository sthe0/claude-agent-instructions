"""Tests for the extended Bash branch of hook-guard-canon-readonly.py — deny
common in-place write verbs (`sed -i`, `>>`/`>`, `tee`, `cp`/`mv` dest, `patch`,
`git apply`) whose target resolves under canon, while ALLOWING the same verbs
into a linked worktree and staying fail-open on garbage / plain reads.

Hermetic: every repo is a local `git init` in tmp_path; the hook is invoked as a
subprocess with a JSON payload on stdin. Mirrors test_hook_guard_canon_readonly.py
(the git-commit deny tests live there; this file covers only the new write verbs).
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = SCRIPTS_DIR / "hook-guard-canon-readonly.py"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def git(*args, cwd, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env={**os.environ, **GIT_ENV},
        check=check,
        capture_output=True,
        text=True,
    )


def make_core(tmp_path: Path) -> Path:
    core = tmp_path / "core"
    core.mkdir()
    git("init", "--quiet", "-b", "main", ".", cwd=core)
    (core / "README.md").write_text("seed\n")
    (core / "scripts").mkdir()
    (core / "scripts" / "existing.py").write_text("x = 1\n")
    git("add", "-A", cwd=core)
    git("commit", "--quiet", "-m", "seed", cwd=core)
    return core


def make_worktree(core: Path, tmp_path: Path) -> Path:
    wt = tmp_path / "wt"
    git("worktree", "add", "-b", "wt-branch", str(wt), "main", cwd=core)
    return wt


def run_hook(core, command: str, cwd) -> subprocess.CompletedProcess:
    env = {**os.environ, **GIT_ENV, "CLAUDE_INSTRUCTIONS_REPO": str(core)}
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        env=env,
        capture_output=True,
        text=True,
    )


def _denied(proc) -> bool:
    return proc.returncode == 0 and '"permissionDecision": "deny"' in proc.stdout


def _allowed(proc) -> bool:
    return proc.returncode == 0 and proc.stdout.strip() == ""


# --- canon writes: DENY ---

def test_sed_in_place_into_canon_denies(tmp_path):
    core = make_core(tmp_path)
    proc = run_hook(core, "sed -i 's/x/y/' scripts/existing.py", cwd=core)
    assert _denied(proc), proc.stdout


def test_sed_in_place_with_suffix_into_canon_denies(tmp_path):
    core = make_core(tmp_path)
    proc = run_hook(core, "sed -i.bak 's/x/y/' scripts/existing.py", cwd=core)
    assert _denied(proc), proc.stdout


def test_append_redirect_into_canon_denies(tmp_path):
    core = make_core(tmp_path)
    proc = run_hook(core, "echo hi >> scripts/existing.py", cwd=core)
    assert _denied(proc), proc.stdout


def test_glued_redirect_into_canon_denies(tmp_path):
    core = make_core(tmp_path)
    proc = run_hook(core, "echo hi >scripts/new.py", cwd=core)
    assert _denied(proc), proc.stdout


def test_tee_into_canon_denies(tmp_path):
    core = make_core(tmp_path)
    proc = run_hook(core, "echo hi | tee scripts/existing.py", cwd=core)
    assert _denied(proc), proc.stdout


def test_cp_dest_into_canon_denies(tmp_path):
    core = make_core(tmp_path)
    proc = run_hook(core, "cp /tmp/whatever scripts/copied.py", cwd=core)
    assert _denied(proc), proc.stdout


def test_mv_dest_into_canon_denies(tmp_path):
    core = make_core(tmp_path)
    proc = run_hook(core, "mv /tmp/whatever scripts/moved.py", cwd=core)
    assert _denied(proc), proc.stdout


def test_cp_target_directory_flag_into_canon_denies(tmp_path):
    core = make_core(tmp_path)
    proc = run_hook(core, "cp /tmp/a /tmp/b -t scripts", cwd=core)
    assert _denied(proc), proc.stdout


def test_patch_in_canon_cwd_denies(tmp_path):
    core = make_core(tmp_path)
    proc = run_hook(core, "patch -p1 < /tmp/some.diff", cwd=core)
    assert _denied(proc), proc.stdout


def test_git_apply_in_canon_cwd_denies(tmp_path):
    core = make_core(tmp_path)
    proc = run_hook(core, "git apply /tmp/some.diff", cwd=core)
    assert _denied(proc), proc.stdout


def test_cd_into_canon_then_sed_denies(tmp_path):
    """A leading `cd <canon>` must make the write resolve against canon even when
    the session payload cwd is elsewhere."""
    core = make_core(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    proc = run_hook(core, f"cd {core} && sed -i 's/x/y/' scripts/existing.py", cwd=outside)
    assert _denied(proc), proc.stdout


def test_leading_cd_missing_dir_semicolon_into_canon_denies(tmp_path):
    """The 2026-07-29 incident shape: `cd <missing-dir> ; <write>` fails the `cd`
    at runtime, and `;` unconditionally runs the write anyway — in the session's
    actual (canon) directory, not the failed target. Previously ALLOWED because
    the guard read the failed `cd`'s literal target as a relocation."""
    core = make_core(tmp_path)
    missing = tmp_path / "does-not-exist"
    proc = run_hook(core, f"cd {missing} ; echo b > s2", cwd=core / "scripts")
    assert _denied(proc), proc.stdout


def test_leading_cd_missing_dir_and_short_circuits_stays_allowed(tmp_path):
    """`&&` gates on the leading `cd`'s exit status: a failed `cd` to a missing
    dir short-circuits the list, so the write never runs and the verdict must
    stay ALLOWED — the discriminator a naive isdir-only fix would get wrong."""
    core = make_core(tmp_path)
    missing = tmp_path / "does-not-exist"
    proc = run_hook(core, f"cd {missing} && echo b > s2", cwd=core / "scripts")
    assert _allowed(proc), proc.stdout


def test_non_leading_cd_verdicts_unchanged(tmp_path):
    """A `cd` that is not the FIRST segment never reaches the new safe-shape
    check (it only inspects `tokens[0]`) — the pre-existing (accepted) false
    deny for a since-created directory stays exactly as it was."""
    core = make_core(tmp_path)
    made = tmp_path / "made"
    proc = run_hook(core, f"mkdir -p {made} && cd {made} && echo b > s2", cwd=core / "scripts")
    assert _denied(proc), proc.stdout


def test_leading_cd_outside_safe_shape_stays_allowed(tmp_path):
    """Every shape the safe-shape allowlist declines keeps today's ALLOW verdict
    for an absolute, nonexistent `cd` target. Grouped by the clause that excludes
    it. Each was run under `bash -c` in a scratch cwd and observed: most write
    nothing there, and the four that DO are marked as accepted lost denies —
    ALLOW is the right pin either way, since the rule buys the absence of false
    denies with a bounded class of lost ones."""
    core = make_core(tmp_path)
    missing = tmp_path / "does-not-exist"
    commands = [
        # (d) grouping construct — neither `_split_on_seps` nor the hook's
        # `_split_segments` models group boundaries, so a segment count taken
        # across a group says nothing about what the shell will run: a group can
        # hide a conditional re-entry the check cannot see.
        f"cd {missing} && {{ echo a ; echo b > s2 ; }}",
        f"cd {missing} && ( echo a ; echo b > s2 )",
        f"cd {missing} && if true ; then echo b > s2 ; fi",
        f"cd {missing} && {{ echo a ; git commit -m x ; }}",
        # LOST DENY (measured: writes s2 in the original cwd) — a subshell with
        # no `cd` of its own inherits the failed `cd`'s directory.
        f"cd {missing} ; ( echo b > s2 )",
        # (d) tested by CONTAINMENT: `shlex.split` glues an unspaced paren to its
        # neighbour, so each glued form must give the same verdict as its spaced
        # twin above. An equality-based check accepts these and denies them.
        f"cd {missing} && (echo a ; echo b > s2)",
        f"cd {missing} ; (cd /tmp; echo b > s2)",
        f"cd {missing} ; ( cd /tmp ; echo b > s2 )",
        # (e) a later `cd` can succeed where the leading one failed.
        f"cd {missing} ; cd /tmp && cp /tmp/a f",
        # (b) non-literal target — the text is not the path the shell will use,
        # so the isdir premise the safe shape rests on does not hold. Measured:
        # unset `$NOPE` collapses the target to /tmp, where the `cd` SUCCEEDS
        # and the write lands — nothing reaches the original cwd.
        "cd /tmp/$NOPE ; echo b > s2",
        "cd /home/the0/wt-$TASK ; cp /tmp/a notes.md",
        # LOST DENY (measured: writes s2 in the original cwd) — an unmatched
        # glob expands to itself, so this `cd` fails like a literal absent one.
        f"cd {missing}* ; echo b > s2",
        # (f) `&&`/`||` gate on the exit status of the command IMMEDIATELY before
        # them, which after the first segment is no longer the `cd` — a rule
        # computing reachability from separator KIND denies all three.
        f"cd {missing} ; false && echo b > s2",
        f"cd {missing} ; true || echo b > s2",
        f"cd {missing} && echo b > s2",
        # (f) an intervening segment is an actor the guard does not model: it can
        # end the shell or move it.
        f"cd {missing} ; exit 1 ; echo b > s2",
        f"cd {missing} ; set -e ; false ; echo b > s2",
        f"cd {missing} ; pushd /tmp ; echo b > s2",
        f"cd {missing} ; exit ; echo b > s2",
        # (f) LOST DENIES (measured: both write s2 in the original cwd) — the
        # cost of the two-segment limit. The trailing `;` reaches the same class
        # by accident: it yields an empty third segment, so the count check
        # declines. (A trailing `;;` does NOT — it is not a separator, so it
        # rides inside segment 2 and the command still denies.)
        f"cd {missing} ; echo a ; echo b > s2",
        f"cd {missing} ; echo b > s2 ;",
    ]
    for cmd in commands:
        proc = run_hook(core, cmd, cwd=core / "scripts")
        assert _allowed(proc), f"{cmd!r}: {proc.stdout}"


def test_leading_cd_dash_deny_is_pre_existing_and_unchanged(tmp_path):
    """`cd -` is outside the safe shape (clause b), but unlike the other
    non-literal targets it still DENIES — and did so identically before this
    stage. `-` is not absolute, so the general relocation branch joins it onto
    the payload cwd and the write resolves under canon. Measured pre- and
    post-fix: both `<cwd>/-`. The real target is `$OLDPWD`, unknowable from the
    command text, so this sits with the by-design `$VAR` deny rather than with
    the false denies. Pinned as UNCHANGED, not as correct — closing it is not
    this stage's rule."""
    core = make_core(tmp_path)
    proc = run_hook(core, "cd - ; echo b > s2", cwd=core / "scripts")
    assert _denied(proc), proc.stdout


# --- same verbs into a linked worktree: ALLOW ---

def test_sed_in_place_into_worktree_allows(tmp_path):
    core = make_core(tmp_path)
    wt = make_worktree(core, tmp_path)
    proc = run_hook(core, "sed -i 's/x/y/' scripts/existing.py", cwd=wt)
    assert _allowed(proc), proc.stdout


def test_append_redirect_into_worktree_allows(tmp_path):
    core = make_core(tmp_path)
    wt = make_worktree(core, tmp_path)
    proc = run_hook(core, "echo hi >> scripts/existing.py", cwd=wt)
    assert _allowed(proc), proc.stdout


def test_tee_into_worktree_allows(tmp_path):
    core = make_core(tmp_path)
    wt = make_worktree(core, tmp_path)
    proc = run_hook(core, "echo hi | tee scripts/existing.py", cwd=wt)
    assert _allowed(proc), proc.stdout


def test_git_apply_in_worktree_allows(tmp_path):
    core = make_core(tmp_path)
    wt = make_worktree(core, tmp_path)
    proc = run_hook(core, "git apply /tmp/some.diff", cwd=wt)
    assert _allowed(proc), proc.stdout


def test_cd_into_worktree_then_sed_allows(tmp_path):
    core = make_core(tmp_path)
    wt = make_worktree(core, tmp_path)
    proc = run_hook(core, f"cd {wt} && sed -i 's/x/y/' scripts/existing.py", cwd=core)
    assert _allowed(proc), proc.stdout


# --- copying OUT of canon is a read of the source: ALLOW ---

def test_cp_canon_source_out_allows(tmp_path):
    core = make_core(tmp_path)
    proc = run_hook(core, "cp scripts/existing.py /tmp/exfil.py", cwd=core)
    assert _allowed(proc), proc.stdout


# --- non-write / read-only Bash in canon: ALLOW ---

def test_sed_without_in_place_allows(tmp_path):
    core = make_core(tmp_path)
    proc = run_hook(core, "sed 's/x/y/' scripts/existing.py", cwd=core)
    assert _allowed(proc), proc.stdout


def test_plain_read_allows(tmp_path):
    core = make_core(tmp_path)
    for cmd in ("cat scripts/existing.py", "grep x scripts/existing.py", "ls scripts"):
        proc = run_hook(core, cmd, cwd=core)
        assert _allowed(proc), f"{cmd!r}: {proc.stdout}"


def test_input_redirect_only_allows(tmp_path):
    """`<` is an input read, not a write — a bare read redirect must not deny."""
    core = make_core(tmp_path)
    proc = run_hook(core, "cat < scripts/existing.py", cwd=core)
    assert _allowed(proc), proc.stdout


# --- fail-open on unparseable input ---

def test_unbalanced_quote_fails_open(tmp_path):
    core = make_core(tmp_path)
    proc = run_hook(core, "sed -i 's/x/y/ scripts/existing.py", cwd=core)  # unterminated quote
    assert _allowed(proc), proc.stdout


# --- here-document bodies are DATA, not syntax ---
#
# Every ALLOW case below is DENIED by the guard without the body stripper: the
# body carries a `>` that `shlex` hands over as an ordinary token, and `shlex`
# raises on none of it, so the module's fail-open path cannot cover these.

def test_heredoc_body_blockquote_allows(tmp_path):
    """A Markdown blockquote line inside a body is prose, not a redirect."""
    core = make_core(tmp_path)
    cmd = "cat > /tmp/notes.md <<'EOF'\n> a quoted line\nEOF"
    assert _allowed(run_hook(core, cmd, cwd=core))


def test_heredoc_body_append_marker_allows(tmp_path):
    """The append twin of the blockquote case: `>>` at the start of a body line
    is prose, and the guard's token walk looks for `>>` on the same pass."""
    core = make_core(tmp_path)
    cmd = "cat > /tmp/notes.md <<'EOF'\n>> a quoted line\nEOF"
    assert _allowed(run_hook(core, cmd, cwd=core))


def test_heredoc_body_absolute_canon_path_allows(tmp_path):
    """The cwd-independent anchor of the family, asserted at BOTH cwds.

    Every other false positive here denies only when the payload cwd is canon,
    because its bogus token is relative and `_canon_target` resolves it against
    the effective cwd — so a test for one of those written at an outside cwd
    passes without the fix and pins nothing. This body carries an ABSOLUTE canon
    path instead, which denied at every cwd before the stripper existed. That is
    what isolates "a body is data" from the cwd-resolution behaviour, and it is
    why the outside-cwd half is asserted rather than assumed.
    """
    core = make_core(tmp_path)
    cmd = f"cat > /tmp/notes.md <<'EOF'\n> {core}/f.txt\nEOF"
    for cwd in (core, Path("/tmp")):
        assert _allowed(run_hook(core, cmd, cwd=cwd)), f"cwd={cwd}"


def test_here_string_operand_allows(tmp_path):
    core = make_core(tmp_path)
    assert _allowed(run_hook(core, 'cat <<< "> notes.txt"', cwd=core))


def test_heredoc_bare_delimiter_inert_body_allows(tmp_path):
    """An unquoted delimiter is fine when the body itself triggers no expansion."""
    core = make_core(tmp_path)
    cmd = "cat > /tmp/x.md <<EOF\n> plain line\nEOF"
    assert _allowed(run_hook(core, cmd, cwd=core))


def test_heredoc_tab_indented_form_allows(tmp_path):
    core = make_core(tmp_path)
    cmd = "cat > /tmp/x.md <<-'EOF'\n\t> tabbed line\n\tEOF"
    assert _allowed(run_hook(core, cmd, cwd=core))


# --- the same handling must not widen: these still DENY ---

def test_heredoc_redirect_on_command_line_still_denies(tmp_path):
    """Body text only is removed; a canon path on the command line survives."""
    core = make_core(tmp_path)
    cmd = "cat <<'EOF' > scripts/existing.py\nbody\nEOF"
    assert _denied(run_hook(core, cmd, cwd=core))


def test_heredoc_piped_to_tee_into_canon_still_denies(tmp_path):
    core = make_core(tmp_path)
    cmd = "cat <<'EOF' | tee scripts/existing.py\nbody\nEOF"
    assert _denied(run_hook(core, cmd, cwd=core))


def test_heredoc_into_interpreter_still_denies(tmp_path):
    """`bash` is not on the consumer allowlist: its body really is executed."""
    core = make_core(tmp_path)
    cmd = "bash <<'EOF'\necho x > scripts/existing.py\nEOF"
    assert _denied(run_hook(core, cmd, cwd=core))


def test_heredoc_into_rebound_interpreter_still_denies(tmp_path):
    """A pipeline-WIDE allowlist check — a first-word check passes this one."""
    core = make_core(tmp_path)
    cmd = "cat <<'EOF' | bash\necho x > scripts/existing.py\nEOF"
    assert _denied(run_hook(core, cmd, cwd=core))


def test_heredoc_expanding_body_still_denies(tmp_path):
    """With an unquoted delimiter the SHELL expands the body before any consumer
    starts, so a bare-delimiter body carrying `$` is never treated as inert."""
    core = make_core(tmp_path)
    cmd = "cat <<EOF > /tmp/x.md\n$(echo x > scripts/existing.py)\nEOF"
    assert _denied(run_hook(core, cmd, cwd=core))


def test_heredoc_body_executed_by_later_statement_still_denies(tmp_path):
    """An inert consumer can still PERSIST a body that a later statement runs, so
    a residue holding more than one statement strips nothing."""
    core = make_core(tmp_path)
    cmd = "cat <<'EOF' > /tmp/s.sh\necho x > scripts/existing.py\nEOF\nbash /tmp/s.sh"
    assert _denied(run_hook(core, cmd, cwd=core))


def test_heredoc_delimiter_not_ending_at_word_boundary_still_denies(tmp_path):
    """`<<EOF.X` terminates on `EOF.X` in bash. Reading the delimiter as `EOF`
    overshoots that terminator and swallows the next real statement as body."""
    core = make_core(tmp_path)
    cmd = (
        "cat <<EOF.X > /tmp/junk.txt\nfiller\nEOF.X\n"
        "echo x > scripts/existing.py\nEOF"
    )
    assert _denied(run_hook(core, cmd, cwd=core))


def test_here_string_glued_redirect_denies(tmp_path):
    """The here-string operand ends at `>` — a bash metacharacter — so the redirect
    after it is command-line text and survives body removal.

    This one is DENIED HERE AND ALLOWED BEFORE the stripper existed, so it is a
    pre-existing bypass incidentally closed rather than a non-regression case:
    `shlex` handed the whole of `x>scripts/existing.py` over as one token, which
    matched no redirect pattern. bash genuinely writes there, so tightening is the
    correct direction — but it IS a behaviour change, not a pure false-positive
    removal, and is pinned here so a later reader does not mistake it for one."""
    core = make_core(tmp_path)
    assert _denied(run_hook(core, "cat <<< x>scripts/existing.py", cwd=core))


def test_heredoc_in_function_definition_still_denies(tmp_path):
    """A definition anywhere disqualifies: the consumer name may be rebound."""
    core = make_core(tmp_path)
    cmd = "cat() { bash; }\ncat <<'EOF'\necho x > scripts/existing.py\nEOF"
    assert _denied(run_hook(core, cmd, cwd=core))


# --- 1b: the deny stays, the message explains it ---

def test_unexpanded_variable_deny_names_the_cause(tmp_path):
    """`cat > $S/x.md` resolves to a literal `$S` under the cwd — the deny is
    correct (a fresh shell per tool call leaves `$S` unset, and the hook cannot
    tell that from an `$S` that would have expanded into canon anyway). Only the
    message changes."""
    core = make_core(tmp_path)
    proc = run_hook(core, "cat hi > $S/x.md", cwd=core)
    assert _denied(proc), proc.stdout
    assert "shell state does not persist between tool calls" in proc.stdout
    assert "absolute path" in proc.stdout


def test_ordinary_deny_omits_the_variable_note(tmp_path):
    core = make_core(tmp_path)
    proc = run_hook(core, "echo hi >> scripts/existing.py", cwd=core)
    assert _denied(proc), proc.stdout
    assert "shell state does not persist" not in proc.stdout


def test_body_stripper_is_called_once_on_the_bash_path():
    """Applied once, at the Bash entry point of `decide()`. A second call site
    means a consumer was fixed in isolation and the next one will be missed.

    Asserted over the parsed AST rather than by counting the symbol's occurrences
    in the source text: a comment that names the function is not a call site, and
    the count would report one as a violation with a message pointing at the
    wrong thing.

    Both call shapes are matched. `shell_tokens.strip_heredoc_bodies(...)` parses
    to an `ast.Attribute`, but a second call site introduced through
    `from lib.shell_tokens import strip_heredoc_bodies` parses to an `ast.Name` —
    matching only the first would leave exactly the kind of second consumer this
    test exists to catch invisible to it.
    """
    tree = ast.parse(HOOK_SCRIPT.read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Attribute) and node.func.attr == "strip_heredoc_bodies")
            or (isinstance(node.func, ast.Name) and node.func.id == "strip_heredoc_bodies")
        )
    ]
    assert len(calls) == 1, f"{len(calls)} call sites, expected exactly one"

    decide = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "decide"
    )
    assert calls[0] in set(ast.walk(decide)), "the single call site is not inside decide()"
