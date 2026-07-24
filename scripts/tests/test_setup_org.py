"""setup-org.sh — one-command org-portable onboarding wizard.

Machine-independent contract test: the wizard creates the per-machine identity
file, is idempotent, and prints the onboarding checklist. `_run` below points the
detect plugin dir at the temp HOME, so no machine-local detect hook can fire and
the neutral rules decide — which makes `difficulty_channel=github` deterministic on
any host, and the headline org-neutrality invariant worth asserting here rather
than describing. The hook branch itself is covered by the difficulty_channel.detect
unit tests, which inject a synthetic hook.
"""
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "setup-org.sh"


def _run(home, *args):
    env = dict(os.environ)
    env["HOME"] = str(home)
    # Hermetic: let config-root.sh resolve the default $HOME/.claude-agent, and let both
    # plugin dirs resolve under it too, so neither an inherited override from the runner's
    # own agent session nor a real machine-local hook reaches this run.
    for var in ("CLAUDE_AGENT_HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_AGENT_IDENTITY",
                "CLAUDE_DIFFICULTY_PLUGIN_DIR", "CLAUDE_PROJECT_PLUGIN_DIR"):
        env.pop(var, None)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
    )


def test_creates_identity_and_prints_checklist(tmp_path):
    r = _run(tmp_path, "--non-interactive")
    assert r.returncode == 0, r.stderr
    idf = tmp_path / ".claude-agent" / "agent-identity.local"
    assert idf.exists()
    # With no hook reachable, the neutral rules decide — Core's only channel is the public one.
    assert "difficulty_channel=github" in idf.read_text()
    assert "Onboarding checklist" in r.stdout


def test_idempotent_never_overwrites(tmp_path):
    r1 = _run(tmp_path, "--non-interactive")
    assert r1.returncode == 0, r1.stderr
    content1 = (tmp_path / ".claude-agent" / "agent-identity.local").read_text()
    r2 = _run(tmp_path, "--non-interactive")
    assert r2.returncode == 0, r2.stderr
    content2 = (tmp_path / ".claude-agent" / "agent-identity.local").read_text()
    assert content1 == content2  # identity file is never rewritten


def test_unknown_arg_rejected(tmp_path):
    r = _run(tmp_path, "--bogus")
    assert r.returncode == 2


def test_help_exits_zero(tmp_path):
    r = _run(tmp_path, "--help")
    assert r.returncode == 0
    assert "Usage" in r.stdout or "Onboarding" in r.stdout
