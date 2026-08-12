"""Regression tests for the three measured false positives that motivate
separating WHERE a here-document construct is from WHETHER its body may be
trusted (docs/decisions/heredoc-body-neutralization.md).

Each test is expressed as the REAL observable the user suffered, wherever a
hook can produce one — not as a unit assertion on a layer that happens to be
convenient. T1 and T3 invoke `hook-guard-canon-readonly.py` as a subprocess,
exactly like `test_guard_canon_bash_writes.py`, so a fix that only touches the
library without also fixing the hook's call site cannot make them pass. T2 has
no hook-observable deny to assert against (`decide()`'s Bash branch fails open
on any exception, so `_canon_bash_write`'s `ValueError` never becomes a deny;
the instance's user-visible hard refusal came from a different, unmerged
guard) — it is asserted at the library level instead, against the function
this task adds.

All three are measured RED against the unmodified tree (2026-08-12): T1
because `python3` is deliberately absent from `CONSUMERS`, so nothing is
stripped and the body's `>` is read as a redirect; T2 because the body
persists a later statement, so clause (v) rejects the strip and the body's
apostrophe reaches `shlex.split` unguarded; T3 because `python3` again fails
clause (iv), so the unstripped body's `git commit` mention reaches
`_is_git_commit`'s tokenizer.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = SCRIPTS_DIR / "hook-guard-canon-readonly.py"
sys.path.insert(0, str(SCRIPTS_DIR))

from lib import shell_tokens  # noqa: E402

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


# --- T1 (I1, issue #66): a Python heredoc body's `>` is not a redirect ---

def test_python_heredoc_comparison_body_allows(tmp_path):
    """`python3 - <<'PY'` whose body compares with `>` must not be read as a
    canon write. Measured RED today: the guard denies a phantom candidate
    `<cwd>/14)`, because `python3` is not on `CONSUMERS` so nothing is
    stripped and `shlex` reads the body's `> 14)` as an ordinary redirect."""
    core = make_core(tmp_path)
    cmd = "python3 - <<'PY'\nprint(text.count('word') > 14)\nPY"
    proc = run_hook(core, cmd, cwd=core)
    assert _allowed(proc), proc.stdout
    assert "14)" not in proc.stdout, proc.stdout


# --- T2 (I2, issue #71 comment): an apostrophe in a persisted body ---

def test_apostrophe_in_persisted_heredoc_body_does_not_break_lexing():
    """A body persisted by an inert consumer (`cat > f <<'TXT' ... TXT`) and
    then run by a later statement must stay unstripped by
    `strip_heredoc_bodies` (clause (v): the residue holds two statements) —
    but the NEUTRALIZER this task adds must still hide the construct, because
    hiding does not require trusting the body, only locating it. Measured RED
    today: `shlex.split` on the unstripped text raises `ValueError: No
    closing quotation` on the body's ordinary English apostrophe."""
    cmd = (
        "cat > /tmp/x.md <<'TXT'\n"
        "other people's work\n"
        "TXT\n"
        "python3 /tmp/x.md"
    )
    neutralized = shell_tokens.neutralize_heredoc_constructs(cmd)
    tokens = shlex.split(neutralized)  # must not raise
    assert "python3" in tokens, tokens
    assert "/tmp/x.md" in tokens, tokens


# --- T3 (I3, found while planning): a mentioned `git commit` inside a body ---

def test_git_commit_mentioned_in_heredoc_body_is_not_a_commit(tmp_path):
    """`python3 - <<'PY'` whose body merely mentions `git commit` in a comment
    must not be read as the session issuing a commit. Measured RED today:
    `python3` fails clause (iv) so nothing is stripped, and
    `_is_git_commit`'s `shlex.split` finds the literal `git`/`commit` token
    pair inside the body."""
    core = make_core(tmp_path)
    cmd = "python3 - <<'PY'\n# git commit -m nope\nPY"
    proc = run_hook(core, cmd, cwd=core)
    assert _allowed(proc), proc.stdout
    assert "Refusing to run" not in proc.stdout, proc.stdout
