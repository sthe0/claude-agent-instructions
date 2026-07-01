"""setup-org.sh — one-command org-portable onboarding wizard.

Machine-independent contract test: the wizard creates the per-machine identity
file (with whatever channel detect.py resolves on this host), is idempotent, and
prints the onboarding checklist. The `github`-under-no-signals branch of channel
detection is proven separately by the difficulty_channel.detect unit tests, since
detect.py inspects the real machine (not $HOME) and would yield `startrek` on a
Yandex host regardless of the temp HOME used here.
"""
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "setup-org.sh"


def _run(home, *args):
    env = dict(os.environ)
    env["HOME"] = str(home)
    # Hermetic: let config-root.sh resolve the default $HOME/.claude-agent,
    # not an inherited override from the runner's own agent session.
    for var in ("CLAUDE_AGENT_HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_AGENT_IDENTITY"):
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
    assert "difficulty_channel=" in idf.read_text()
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
