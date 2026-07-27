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


def _commit(repo: Path, env: dict, message: str) -> "subprocess.CompletedProcess[str]":
    target = repo / "file.txt"
    target.write_text((target.read_text(encoding="utf-8") if target.exists() else "") + "x", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True, env=env)
    return subprocess.run(
        ["git", "commit", "-m", message], cwd=repo, capture_output=True, text=True, env=env, timeout=15,
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
