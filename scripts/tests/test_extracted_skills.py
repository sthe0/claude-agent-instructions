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
import re
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
RESOLVE = SCRIPTS / "verify-extracted-skills-resolve.sh"
CONTRACT = SCRIPTS / "verify-layout-contract.sh"
SETUP_SYMLINKS = SCRIPTS / "setup-symlinks.sh"
SYNC = SCRIPTS / "verify-instructions-sync.sh"


def _env(agent_home: Path) -> "dict[str, str]":
    env = dict(os.environ)
    env["CLAUDE_AGENT_HOME"] = str(agent_home)
    return env


def _run(script: Path, agent_home: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script)], env=_env(agent_home),
        capture_output=True, text=True,
    )


def _extract_bash_function(source: str, name: str) -> str:
    """Pull one top-level `name() { ... }` definition verbatim out of a shell
    script, so a test can exercise the real function body without running the
    rest of the script around it."""
    match = re.search(
        rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", source, re.MULTILINE | re.DOTALL,
    )
    assert match, f"function {name}() not found"
    return match.group(0)


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
    # Output honesty: a block that failed must not also claim to have passed.
    assert "OK: extracted skills absent from the repo, present in the overlay" not in result.stdout


def test_contract_rejects_extracted_skill_missing_from_overlay(tmp_path):
    agent_home = _agent_home(tmp_path)
    _manifest(agent_home, "bridge-management\n")  # no overlay copy created

    result = _run(CONTRACT, agent_home)
    assert result.returncode == 1
    assert f"missing file {agent_home}/skills-local/bridge-management/SKILL.md" in result.stdout


# ── verify-instructions-sync.sh: the control is actually run ─────────────────

def test_sync_verifier_runs_the_resolve_check(tmp_path):
    """A control nothing invokes decays unnoticed, so the resolve check rides along
    with the layout contract in the machine-state verifier. Only reachability is
    asserted — the verifier's other checks fail against a non-canonical checkout."""
    env = _env(_agent_home(tmp_path))
    env["CLAUDE_INSTRUCTIONS_REPO"] = str(REPO)
    result = subprocess.run(
        ["bash", str(SYNC)], env=env, capture_output=True, text=True,
    )

    assert "=== Extracted skills ===" in result.stdout
    assert "nothing to resolve" in result.stdout


# ── setup-symlinks.sh: the overlay reaches the catalog ───────────────────────

def test_setup_symlinks_links_the_agent_home_overlay(tmp_path):
    """Running the whole installer end to end is not hermetic: setup-symlinks.sh
    self-locates $REPO from $0, so it chains into sub-installers (install-git-hooks.sh
    runs `git config core.hooksPath` in $REPO, install-reminder-hooks.sh hardcodes
    $HOME/claude-agent-instructions, several scripts chmod real repo files) that
    reach outside the sandbox this test controls. The behaviour under test —
    the overlay reaching the skill catalog — lives entirely in link()/
    link_local_skills(); pull those two functions verbatim out of the real
    installer and run only them, so nothing else in the installer is reachable
    regardless of how its later steps change."""
    agent_home = _agent_home(tmp_path)
    (agent_home / "skills-local" / "bridge-management").mkdir()
    (agent_home / "skills-local" / "bridge-management" / "SKILL.md").write_text("x", encoding="utf-8")

    source = SETUP_SYMLINKS.read_text(encoding="utf-8")
    functions = _extract_bash_function(source, "link") + _extract_bash_function(source, "link_local_skills")
    script = (
        "set -euo pipefail\n"
        f'CLAUDE_AGENT_HOME="{agent_home}"\n'
        f"{functions}"
        f'link_local_skills "{agent_home}/skills-local"\n'
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr

    linked = agent_home / "skills" / "bridge-management"
    assert linked.is_symlink()
    assert linked.resolve() == (agent_home / "skills-local" / "bridge-management").resolve()


def test_setup_symlinks_extraction_touches_only_claude_agent_home():
    """The containment argument above holds only as long as link()/
    link_local_skills() stay free of $HOME, $REPO, chmod, and git references —
    pin that so a future edit to either function that reaches outside
    $CLAUDE_AGENT_HOME fails this test instead of silently widening what the
    extraction-based test above can touch."""
    source = SETUP_SYMLINKS.read_text(encoding="utf-8")
    functions = _extract_bash_function(source, "link") + _extract_bash_function(source, "link_local_skills")
    assert "$HOME" not in functions
    assert "$REPO" not in functions
    assert "chmod" not in functions
    assert "git " not in functions
