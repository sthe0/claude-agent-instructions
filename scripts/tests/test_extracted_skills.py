"""Coverage for the extracted-skills overlay: skills that live outside this repo
(in <agent-home>/skills-local/) because they are machine- or org-specific.

Three pieces are under test:
  * setup-symlinks.sh links the overlay into the skill catalog,
  * verify-extracted-skills-resolve.sh fails when an extracted skill does not
    resolve in the catalog,
  * verify-layout-contract.sh fails in BOTH directions — an extracted skill that
    reappears in the repo, and one that is missing from the overlay.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
RESOLVE = SCRIPTS / "verify-extracted-skills-resolve.sh"
CONTRACT = SCRIPTS / "verify-layout-contract.sh"
SETUP_SYMLINKS = SCRIPTS / "setup-symlinks.sh"


def _env(agent_home: Path, home: "Path | None" = None) -> "dict[str, str]":
    env = dict(os.environ)
    env["CLAUDE_AGENT_HOME"] = str(agent_home)
    if home is not None:
        env["HOME"] = str(home)
    return env


def _run(script: Path, agent_home: Path, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script)], env=_env(agent_home, **kw),
        capture_output=True, text=True,
    )


def _agent_home(tmp_path: Path) -> Path:
    home = tmp_path / ".claude-agent"
    (home / "skills").mkdir(parents=True)
    (home / "skills-local").mkdir()
    return home


def _extract(agent_home: Path, name: str, *, link: bool = True) -> None:
    """Put a skill in the overlay, optionally linking it into the catalog."""
    overlay = agent_home / "skills-local" / name
    overlay.mkdir()
    (overlay / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
    if link:
        (agent_home / "skills" / name).symlink_to(overlay)


def _manifest(agent_home: Path, body: str) -> None:
    (agent_home / "extracted-skills.local").write_text(body, encoding="utf-8")


# ── verify-extracted-skills-resolve.sh ───────────────────────────────────────

def test_no_manifest_is_a_valid_state(tmp_path):
    result = _run(RESOLVE, _agent_home(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "nothing to resolve" in result.stdout


def test_resolving_skill_passes(tmp_path):
    agent_home = _agent_home(tmp_path)
    _extract(agent_home, "bridge-management")
    _manifest(agent_home, "bridge-management\n")

    result = _run(RESOLVE, agent_home)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 skill(s) checked" in result.stdout


def test_unlinked_skill_fails(tmp_path):
    """The failure this script exists for: the overlay has it, the catalog does not."""
    agent_home = _agent_home(tmp_path)
    _extract(agent_home, "bridge-management", link=False)
    _manifest(agent_home, "bridge-management\n")

    result = _run(RESOLVE, agent_home)
    assert result.returncode == 1
    assert "does not resolve" in result.stdout


def test_dangling_link_fails(tmp_path):
    agent_home = _agent_home(tmp_path)
    _manifest(agent_home, "bridge-management\n")
    (agent_home / "skills" / "bridge-management").symlink_to(tmp_path / "gone")

    result = _run(RESOLVE, agent_home)
    assert result.returncode == 1
    assert "does not resolve" in result.stdout


def test_comments_and_blank_lines_ignored(tmp_path):
    agent_home = _agent_home(tmp_path)
    _manifest(agent_home, "# a comment\n\n   \n")

    result = _run(RESOLVE, agent_home)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 skill(s) checked" in result.stdout


# ── verify-layout-contract.sh: both directions ───────────────────────────────

def test_contract_rejects_extracted_skill_still_in_repo(tmp_path):
    """A name in the manifest that still has a directory in the repo is a failed
    extraction — here provoked with a skill this repo really does ship."""
    agent_home = _agent_home(tmp_path)
    _manifest(agent_home, "tracker-management\n")

    result = _run(CONTRACT, agent_home)
    assert result.returncode == 1
    assert f"must not exist: {REPO}/skills/tracker-management" in result.stdout


def test_contract_rejects_extracted_skill_missing_from_overlay(tmp_path):
    agent_home = _agent_home(tmp_path)
    _manifest(agent_home, "bridge-management\n")  # no overlay copy created

    result = _run(CONTRACT, agent_home)
    assert result.returncode == 1
    assert f"missing file {agent_home}/skills-local/bridge-management/SKILL.md" in result.stdout


# ── setup-symlinks.sh: the overlay reaches the catalog ───────────────────────

def test_setup_symlinks_links_the_agent_home_overlay(tmp_path):
    """Exit status is deliberately not asserted: under a faked HOME the installer
    aborts in a later step (install-reminder-hooks.sh resolves this repo through
    $HOME). That abort happens well after the skill linking, and containing the
    installer to the sandbox is worth more here than a clean exit code."""
    home = tmp_path / "home"
    agent_home = home / ".claude-agent"
    (agent_home / "skills-local" / "bridge-management").mkdir(parents=True)
    (agent_home / "skills-local" / "bridge-management" / "SKILL.md").write_text("x", encoding="utf-8")

    _run(SETUP_SYMLINKS, agent_home, home=home)

    linked = agent_home / "skills" / "bridge-management"
    assert linked.is_symlink()
    assert linked.resolve() == (agent_home / "skills-local" / "bridge-management").resolve()
