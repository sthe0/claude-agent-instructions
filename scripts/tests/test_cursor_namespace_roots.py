"""Project-root discovery in cursor/scripts/migrate-cursor-namespace.sh.

Which mounts hold project checkouts is per-machine data, not Core source: the
script discovers them from agent-identity.local's `cursor_project_roots=` glob
list and Core ships no built-in roots. The script is exercised black-box with a
stub repo (so the global setup-symlinks step is inert) and a stub HOME.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MIGRATE = REPO / "cursor" / "scripts" / "migrate-cursor-namespace.sh"


def _stub_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "stub-repo"
    (repo / "scripts").mkdir(parents=True, exist_ok=True)
    symlinks = repo / "scripts" / "setup-symlinks.sh"
    symlinks.write_text("#!/usr/bin/env bash\necho STUB-GLOBAL\n")
    symlinks.chmod(0o755)
    return repo


def _project_root(tmp_path: Path, relpath: str) -> Path:
    root = tmp_path / relpath
    (root / ".claude" / "scripts").mkdir(parents=True)
    setup_local = root / ".claude" / "scripts" / "setup-local.sh"
    setup_local.write_text(f"#!/usr/bin/env bash\necho STUB-LOCAL {relpath}\n")
    setup_local.chmod(0o755)
    return root


def _run(tmp_path: Path, *args: str, identity: "str | None" = None):
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_AGENT_HOME", "CLAUDE_AGENT_IDENTITY")}
    env["HOME"] = str(tmp_path)
    env["CLAUDE_INSTRUCTIONS_REPO"] = str(_stub_repo(tmp_path))
    if identity is not None:
        idf = tmp_path / "agent-identity.local"
        idf.write_text(identity)
        env["CLAUDE_AGENT_IDENTITY"] = str(idf)
    return subprocess.run([str(MIGRATE), *args], env=env,
                          capture_output=True, text=True)


def test_no_identity_key_discovers_nothing(tmp_path):
    """Core ships no built-in roots: an unconfigured machine finds none."""
    _project_root(tmp_path, "mounts/alpha/checkout")
    r = _run(tmp_path, "--all-configured-roots")
    assert r.returncode == 0, r.stderr
    assert "No project roots were passed" in r.stdout
    assert "STUB-LOCAL" not in r.stdout


def test_configured_glob_discovers_every_matching_root(tmp_path):
    _project_root(tmp_path, "mounts/alpha/checkout")
    _project_root(tmp_path, "mounts/beta/checkout")
    (tmp_path / "mounts" / "gamma").mkdir(parents=True)  # no .claude/ — skipped
    r = _run(tmp_path, "--all-configured-roots",
             identity="cursor_project_roots=~/mounts/*/checkout\n")
    assert r.returncode == 0, r.stderr
    assert "STUB-LOCAL mounts/alpha/checkout" in r.stdout
    assert "STUB-LOCAL mounts/beta/checkout" in r.stdout


def test_configured_list_is_comma_and_space_separated(tmp_path):
    _project_root(tmp_path, "one")
    _project_root(tmp_path, "two")
    r = _run(tmp_path, "--all-configured-roots",
             identity=f"cursor_project_roots={tmp_path}/one, {tmp_path}/two\n")
    assert r.returncode == 0, r.stderr
    assert "STUB-LOCAL one" in r.stdout
    assert "STUB-LOCAL two" in r.stdout


def test_explicit_root_argument_needs_no_identity_key(tmp_path):
    root = _project_root(tmp_path, "explicit")
    r = _run(tmp_path, str(root))
    assert r.returncode == 0, r.stderr
    assert "STUB-LOCAL explicit" in r.stdout


def test_help_names_the_neutral_flag(tmp_path):
    r = _run(tmp_path, "--help")
    assert r.returncode == 0, r.stderr
    assert "--all-configured-roots" in r.stdout
    assert "cursor_project_roots" in r.stdout
