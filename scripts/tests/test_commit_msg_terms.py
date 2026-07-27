"""githooks/commit-msg: hard-blocking term-ruleset gate on the commit message
itself (C1 mechanism, sub-item 8).

Unlike the advisory pre-write hook (hook-term-neutrality.py), a bad commit
message can't be fixed post-landing without a forbidden history rewrite, so
this gate must actually REJECT the commit — proven here via real `git
commit` attempts against a throwaway repo carrying its own copy of the real
hook and the scripts it calls, mirroring test_agent_commit_trailer.py's
`_install_throwaway_repo` pattern. All terms used are synthetic (zorblex).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent

DENY_ZORBLEX = """
[[deny]]
pattern = 'zorblex'
label = "codename"
"""


def _install_throwaway_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)

    (repo / "githooks").mkdir()
    shutil.copy2(REPO_ROOT / "githooks" / "commit-msg", repo / "githooks" / "commit-msg")
    (repo / "githooks" / "commit-msg").chmod(0o755)

    (repo / "scripts").mkdir()
    for name in (
        "agent_commit_trailer.py",
        "verify-tests-accompany-code.py",
        "verify-self-improvement-edit.py",
        "check-org-neutral.py",
    ):
        shutil.copy2(SCRIPTS_DIR / name, repo / "scripts" / name)
    shutil.copytree(SCRIPTS_DIR / "lib", repo / "scripts" / "lib", ignore=shutil.ignore_patterns("__pycache__"))

    subprocess.run(["git", "config", "core.hooksPath", "githooks"], cwd=repo, check=True)
    return repo


def _ruleset_dir(tmp_path: Path) -> Path:
    d = tmp_path / "rulesets"
    d.mkdir(exist_ok=True)
    (d / "synthetic.toml").write_text(DENY_ZORBLEX, encoding="utf-8")
    return d


def _env(tmp_path: Path, ruleset_dir: Path | None) -> dict:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    env["HOME"] = str(home)
    env["CLAUDE_TERM_RULESET_DIR"] = str(ruleset_dir) if ruleset_dir is not None else str(tmp_path / "empty")
    return env


def _commit(repo: Path, env: dict, message: str, content: str = "x") -> "subprocess.CompletedProcess[str]":
    target = repo / "file.txt"
    target.write_text((target.read_text(encoding="utf-8") if target.exists() else "") + content, encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True, env=env)
    return subprocess.run(
        ["git", "commit", "-m", message], cwd=repo, capture_output=True, text=True, env=env, timeout=15,
    )


def _commit_verbose(repo: Path, env: dict, message: str, content: str) -> "subprocess.CompletedProcess[str]":
    """`git commit -v`: git appends the staged diff below the scissors line, so
    the message file the hook receives carries the diff too."""
    target = repo / "file.txt"
    target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True, env=env)
    editor_env = dict(env)
    # A non-interactive editor that keeps whatever git prepared and prepends
    # the message — the diff below the scissors survives into the hook's $1.
    editor = repo / "fake-editor.sh"
    editor.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$MSG_LINE" | cat - "$1" > "$1.tmp" && mv "$1.tmp" "$1"\n',
        encoding="utf-8",
    )
    editor.chmod(0o755)
    editor_env["GIT_EDITOR"] = str(editor)
    editor_env["MSG_LINE"] = message
    return subprocess.run(
        ["git", "commit", "-v"], cwd=repo, capture_output=True, text=True, env=editor_env, timeout=15,
    )


def test_denied_term_in_message_is_rejected(tmp_path):
    repo = _install_throwaway_repo(tmp_path)
    env = _env(tmp_path, _ruleset_dir(tmp_path))

    result = _commit(repo, env, "mentions zorblex in the message")

    assert result.returncode != 0
    assert "zorblex" in (result.stdout + result.stderr)
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True, env=env,
    ).stdout
    assert log == ""  # no commit landed


def test_clean_message_passes(tmp_path):
    repo = _install_throwaway_repo(tmp_path)
    env = _env(tmp_path, _ruleset_dir(tmp_path))

    result = _commit(repo, env, "add a transport-neutral pending-gate seam")

    assert result.returncode == 0, result.stderr


def test_zero_rulesets_lets_the_same_denied_term_through(tmp_path):
    repo = _install_throwaway_repo(tmp_path)
    env = _env(tmp_path, None)  # no ruleset dir -> zero rulesets discovered

    result = _commit(repo, env, "mentions zorblex in the message")

    assert result.returncode == 0, result.stderr


def test_denied_term_below_the_scissors_does_not_block(tmp_path):
    """`git commit -v` appends the diff below the scissors line. A commit whose
    diff REMOVES a denied term must not be blocked by its own removal."""
    repo = _install_throwaway_repo(tmp_path)
    env = _env(tmp_path, _ruleset_dir(tmp_path))
    _commit(repo, env, "seed the file", content="zorblex lives here\n")

    result = _commit_verbose(repo, env, "drop the codename from the seeded file", content="neutral text\n")

    assert result.returncode == 0, result.stdout + result.stderr
    subject = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"], cwd=repo, capture_output=True, text=True, env=env,
    ).stdout.strip()
    assert subject == "drop the codename from the seeded file"


def test_denied_term_in_the_message_still_blocks_under_commit_v(tmp_path):
    """The counterpart of the test above: stripping the diff must not strip the
    message, or the gate would pass everything under `commit -v`."""
    repo = _install_throwaway_repo(tmp_path)
    env = _env(tmp_path, _ruleset_dir(tmp_path))
    _commit(repo, env, "seed the file", content="neutral seed\n")

    result = _commit_verbose(repo, env, "mentions zorblex in the message", content="still neutral\n")

    assert result.returncode != 0
    assert "zorblex" in (result.stdout + result.stderr)


def test_checker_error_is_surfaced_but_does_not_block(tmp_path):
    """A checker that cannot run (exit != 1) says nothing about the message —
    blocking on it would turn any unrelated breakage into a commit-wide outage."""
    repo = _install_throwaway_repo(tmp_path)
    env = _env(tmp_path, _ruleset_dir(tmp_path))
    (repo / "scripts" / "check-org-neutral.py").unlink()

    result = _commit(repo, env, "a perfectly clean message")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "term check could not run" in result.stderr
