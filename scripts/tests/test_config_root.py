"""Tests for scripts/lib/config-root.sh — the CLAUDE_AGENT_HOME resolver."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
RESOLVER = SCRIPTS / "lib" / "config-root.sh"


def _source_and_echo(env_extra=None):
    """Source the resolver and echo CLAUDE_AGENT_HOME; returns CompletedProcess."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_AGENT_HOME"}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", "-c", f'source "{RESOLVER}" && printf "%s\\n" "$CLAUDE_AGENT_HOME"'],
        env=env,
        capture_output=True,
        text=True,
    )


def test_default_is_dot_claude_agent(tmp_path):
    r = _source_and_echo({"HOME": str(tmp_path)})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(tmp_path / ".claude-agent")


def test_env_override_respected(tmp_path):
    custom = str(tmp_path / "custom-root")
    r = _source_and_echo({"HOME": str(tmp_path), "CLAUDE_AGENT_HOME": custom})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == custom


def test_resolver_is_idempotent(tmp_path):
    """Sourcing twice keeps the first value (no re-assignment if already set)."""
    custom = str(tmp_path / "my-root")
    r = subprocess.run(
        [
            "bash", "-c",
            f'export CLAUDE_AGENT_HOME="{custom}" && '
            f'source "{RESOLVER}" && source "{RESOLVER}" && '
            f'printf "%s\\n" "$CLAUDE_AGENT_HOME"',
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == custom


# ── agent_home_read: read-time root resolution (mirrors config_root.py) ───────

def _run_agent_home_read(home: Path, config_dir=None, agent_home=None) -> str:
    """Source the resolver in a fresh bash and call agent_home_read."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_AGENT_HOME", "CLAUDE_CONFIG_DIR")}
    env["HOME"] = str(home)
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    if agent_home is not None:
        env["CLAUDE_AGENT_HOME"] = str(agent_home)
    r = subprocess.run(
        ["bash", "-c", f'source "{RESOLVER}" && agent_home_read'],
        env=env, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_read_config_dir_wins(tmp_path):
    got = _run_agent_home_read(
        tmp_path, config_dir=tmp_path / "cfg", agent_home=tmp_path / "ah")
    assert got == str(tmp_path / "cfg")


def test_read_explicit_agent_home_wins_over_dirs(tmp_path):
    """An explicit caller CLAUDE_AGENT_HOME is honored even when ~/.claude-agent exists."""
    (tmp_path / ".claude-agent").mkdir()
    got = _run_agent_home_read(tmp_path, agent_home=tmp_path / "custom")
    assert got == str(tmp_path / "custom")


def test_read_isolated_root_when_present(tmp_path):
    """No overrides: an existing ~/.claude-agent is the read root."""
    (tmp_path / ".claude-agent").mkdir()
    assert _run_agent_home_read(tmp_path) == str(tmp_path / ".claude-agent")


def test_read_legacy_root_when_no_isolated(tmp_path):
    """No overrides, no ~/.claude-agent: fall back to the legacy ~/.claude."""
    assert _run_agent_home_read(tmp_path) == str(tmp_path / ".claude")


def test_read_ignores_installtime_default(tmp_path):
    """The resolver's own install-time export (CLAUDE_AGENT_HOME default) must NOT
    leak into read-time resolution — only a PRE-source caller value counts."""
    r = subprocess.run(
        ["bash", "-c",
         f'source "{RESOLVER}" && source "{RESOLVER}" && agent_home_read'],
        env={**{k: v for k, v in os.environ.items()
                if k not in ("CLAUDE_AGENT_HOME", "CLAUDE_CONFIG_DIR")},
             "HOME": str(tmp_path)},
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    # No ~/.claude-agent dir exists, so read-time resolution says legacy —
    # even though sourcing exported CLAUDE_AGENT_HOME=$HOME/.claude-agent.
    assert r.stdout.strip() == str(tmp_path / ".claude")


# ── agent_legacy_inplace_layout: shared legacy-layout detector ────────────────

def _run_legacy_detect(home: Path, repo: Path, agent_home=None):
    """Source the resolver and call agent_legacy_inplace_layout; return rc."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_AGENT_HOME"}
    env["HOME"] = str(home)
    if agent_home is not None:
        env["CLAUDE_AGENT_HOME"] = str(agent_home)
    return subprocess.run(
        ["bash", "-c",
         f'source "{RESOLVER}" && agent_legacy_inplace_layout "{repo}"'],
        env=env, capture_output=True, text=True,
    ).returncode


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "claude-agent-instructions"
    repo.mkdir()
    (repo / "CLAUDE.md").write_text("# constitution\n")
    return repo


def test_legacy_detect_true_when_inplace_symlink(tmp_path):
    """~/.claude/CLAUDE.md symlinked into the repo → legacy layout present (rc 0)."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    repo = _make_repo(tmp_path)
    (home / ".claude" / "CLAUDE.md").symlink_to(repo / "CLAUDE.md")
    assert _run_legacy_detect(home, repo, agent_home=home / ".claude-agent") == 0


def test_legacy_detect_false_when_clean_isolated(tmp_path):
    """~/.claude exists but holds no repo-pointing symlink → not legacy (rc 1)."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text("{}\n")  # personal, not a repo symlink
    repo = _make_repo(tmp_path)
    assert _run_legacy_detect(home, repo, agent_home=home / ".claude-agent") == 1


def test_legacy_detect_false_when_no_dot_claude(tmp_path):
    """Fresh machine with no ~/.claude at all → not legacy (rc 1)."""
    home = tmp_path / "home"
    home.mkdir()
    repo = _make_repo(tmp_path)
    assert _run_legacy_detect(home, repo, agent_home=home / ".claude-agent") == 1


def test_legacy_detect_false_when_claude_is_the_isolated_root(tmp_path):
    """If ~/.claude IS the configured root, its symlinks are not 'legacy' (rc 1)."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    repo = _make_repo(tmp_path)
    (home / ".claude" / "CLAUDE.md").symlink_to(repo / "CLAUDE.md")
    assert _run_legacy_detect(home, repo, agent_home=home / ".claude") == 1


# ── Structural audit: no hardcoded config-root path outside the resolvers ─────
#
# Mechanical enumerator over every *.sh / *.py under scripts/ and cursor/scripts/
# (tests excluded): a config-root artifact spelled with the legacy $HOME/.claude
# (shell) or a home-joined ".claude" (python) must either go through the
# resolvers (lib/config-root.sh, lib/config_root.py) or be an explicitly
# allowlisted INTENTIONAL legacy reference (migration source, read-time legacy
# fallback, protective denylist). New scripts are enumerated automatically —
# the audit can no longer silently miss a file the way the old fixed five-file
# list missed set-context-cap.sh.

_SH_ROOT_TARGET_RE = re.compile(
    r'\$HOME/\.claude/'
    r'(?:CLAUDE\.md|config\.md|memory-global|skills|agents|settings\.json|'
    r'agent-identity\.local|projects\.d|projects|agentctl|plans)'
)
_PY_ROOT_TARGET_RE = re.compile(
    r'(?:Path\.home\(\)\s*/\s*["\']\.claude["\']'
    r'|os\.path\.join\([^)]*["\']\.claude["\'])'
)

# Repo-relative paths whose legacy-root references are intentional.
_LEGACY_REF_ALLOWLIST = {
    "scripts/session-start-digest.sh":       "read-time legacy auto-memory mirror fallback",
    "scripts/setup-project-memory.sh":       "legacy auto-memory dir is its migration source",
    "scripts/project_entry/projects.sh":     "legacy projects.d read fallback",
    "scripts/project_entry/projects.py":     "legacy projects.d read fallback (python side)",
    "scripts/lib/config_root.py":            "the canonical legacy_home() accessor",
    "scripts/hook-guard-destructive-rm.py":  "protective denylist covers BOTH roots",
}


def _iter_root_scripts():
    repo = SCRIPTS.parent
    for base in (SCRIPTS, repo / "cursor" / "scripts"):
        for p in sorted(base.rglob("*")):
            if p.suffix not in (".sh", ".py") or not p.is_file():
                continue
            if "tests" in p.parts or "__pycache__" in p.parts:
                continue
            yield p


def _root_offenders() -> "dict[str, list[str]]":
    repo = SCRIPTS.parent
    offenders: "dict[str, list[str]]" = {}
    for p in _iter_root_scripts():
        pat = _SH_ROOT_TARGET_RE if p.suffix == ".sh" else _PY_ROOT_TARGET_RE
        found = pat.findall(p.read_text(encoding="utf-8", errors="replace"))
        if found:
            offenders[p.relative_to(repo).as_posix()] = found
    return offenders


def test_no_unlisted_config_root_hardcodes():
    offenders = _root_offenders()
    unlisted = {f: m for f, m in offenders.items() if f not in _LEGACY_REF_ALLOWLIST}
    assert not unlisted, (
        "Hardcoded legacy config-root references outside the allowlist — route "
        f"them through lib/config-root.sh / lib/config_root.py: {unlisted}"
    )


def test_legacy_ref_allowlist_is_current():
    """Every allowlist entry must still contain a legacy reference — a stale
    entry would silently re-open the audit hole for that file."""
    offenders = set(_root_offenders())
    stale = set(_LEGACY_REF_ALLOWLIST) - offenders
    assert not stale, f"Allowlist entries with no legacy reference left (remove them): {stale}"


# ── Python resolver: scripts/lib/config_root.py (read-time analog) ────────────

import importlib  # noqa: E402
import sys  # noqa: E402

sys.path.insert(0, str(SCRIPTS))
from lib import config_root  # noqa: E402


def _reload_env(monkeypatch, tmp_home, **env):
    """Point HOME at a tmp dir, clear the root env vars, then apply overrides."""
    monkeypatch.setenv("HOME", str(tmp_home))
    for var in ("CLAUDE_CONFIG_DIR", "CLAUDE_AGENT_HOME", "CLAUDE_AGENT_IDENTITY"):
        monkeypatch.delenv(var, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    importlib.reload(config_root)


def test_py_config_config_dir_wins(monkeypatch, tmp_path):
    custom = tmp_path / "cfg-dir"
    _reload_env(monkeypatch, tmp_path, CLAUDE_CONFIG_DIR=str(custom))
    assert config_root.agent_home() == custom


def test_py_agent_home_env_override(monkeypatch, tmp_path):
    custom = tmp_path / "agent-root"
    _reload_env(monkeypatch, tmp_path, CLAUDE_AGENT_HOME=str(custom))
    assert config_root.agent_home() == custom


def test_py_config_dir_precedes_agent_home(monkeypatch, tmp_path):
    _reload_env(
        monkeypatch,
        tmp_path,
        CLAUDE_CONFIG_DIR=str(tmp_path / "a"),
        CLAUDE_AGENT_HOME=str(tmp_path / "b"),
    )
    assert config_root.agent_home() == tmp_path / "a"


def test_py_isolated_default_when_present(monkeypatch, tmp_path):
    (tmp_path / ".claude-agent").mkdir()
    _reload_env(monkeypatch, tmp_path)
    assert config_root.agent_home() == tmp_path / ".claude-agent"


def test_py_legacy_fallback_when_not_isolated(monkeypatch, tmp_path):
    # No ~/.claude-agent, no env → legacy ~/.claude (pre-migration machine).
    _reload_env(monkeypatch, tmp_path)
    assert config_root.agent_home() == tmp_path / ".claude"


def test_py_skills_dir(monkeypatch, tmp_path):
    custom = tmp_path / "agent-root"
    _reload_env(monkeypatch, tmp_path, CLAUDE_AGENT_HOME=str(custom))
    assert config_root.skills_dir() == custom / "skills"


def test_py_identity_file_default(monkeypatch, tmp_path):
    custom = tmp_path / "agent-root"
    _reload_env(monkeypatch, tmp_path, CLAUDE_AGENT_HOME=str(custom))
    assert config_root.identity_file() == custom / "agent-identity.local"


def test_py_identity_file_override(monkeypatch, tmp_path):
    ident = tmp_path / "elsewhere" / "id.local"
    _reload_env(
        monkeypatch,
        tmp_path,
        CLAUDE_AGENT_HOME=str(tmp_path / "agent-root"),
        CLAUDE_AGENT_IDENTITY=str(ident),
    )
    assert config_root.identity_file() == ident


# ── resolve_agentctl_state_file: current root first, legacy fallback ─────────

def _touch_state(root: Path, session_id: str) -> Path:
    d = root / "agentctl" / "state"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{session_id}.json"
    f.write_text("{}", encoding="utf-8")
    return f


def test_py_state_file_current_root_wins(monkeypatch, tmp_path):
    _reload_env(monkeypatch, tmp_path, CLAUDE_AGENT_HOME=str(tmp_path / "root"))
    current = _touch_state(tmp_path / "root", "s-both")
    _touch_state(tmp_path / ".claude", "s-both")
    assert config_root.resolve_agentctl_state_file("s-both") == current


def test_py_state_file_legacy_fallback(monkeypatch, tmp_path):
    # A pre-migration session lives only under ~/.claude — must still resolve
    # (fail closed: gates read this; 'missing on the new root' must not mean 'allow').
    _reload_env(monkeypatch, tmp_path, CLAUDE_AGENT_HOME=str(tmp_path / "root"))
    legacy = _touch_state(tmp_path / ".claude", "s-old")
    assert config_root.resolve_agentctl_state_file("s-old") == legacy


def test_py_state_file_none_when_absent_everywhere(monkeypatch, tmp_path):
    _reload_env(monkeypatch, tmp_path, CLAUDE_AGENT_HOME=str(tmp_path / "root"))
    assert config_root.resolve_agentctl_state_file("s-none") is None


def test_py_state_file_sanitizes_session_id(monkeypatch, tmp_path):
    # Matches agentctl/store.py's FileStateStore sanitization: path-hostile
    # characters are stripped, an empty id maps to "nosession".
    _reload_env(monkeypatch, tmp_path, CLAUDE_AGENT_HOME=str(tmp_path / "root"))
    hostile = _touch_state(tmp_path / "root", "ab")
    assert config_root.resolve_agentctl_state_file("a/../b") == hostile
    empty = _touch_state(tmp_path / "root", "nosession")
    assert config_root.resolve_agentctl_state_file(None) == empty
