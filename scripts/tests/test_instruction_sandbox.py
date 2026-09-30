"""Tests for scripts/instruction-sandbox.sh and its canon-snapshot helper."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SANDBOX = REPO / "scripts" / "instruction-sandbox.sh"
SNAPSHOT = REPO / "scripts" / "lib" / "instruction-sandbox-canon-snapshot.sh"

SCRUB_VARS = [
    "CLAUDE_CODE_SESSION_ID",
    "AGENT_LINEAGE_IDS",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "AGENTCTL_SAMPLE_VAR",
    "CLAUDE_POLICY_LEDGER",
    "CLAUDE_PROJECT_PLUGIN_DIR",
    "CLAUDE_DIFFICULTY_PLUGIN_DIR",
    "CLAUDE_AUTH_PROFILE_DIR",
    "CLAUDE_INSTRUCTION_SANDBOX_PLUGIN_DIR",
    "CLAUDE_INSTRUCTIONS_CANON",
]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(cwd), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _run(cmd: list, env: dict, cwd: Path = REPO) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, env=env, cwd=cwd, capture_output=True, text=True)


def _tree(path: Path) -> dict:
    """Relative path -> bytes (files), None (dirs), link target (symlinks)."""
    out: dict = {}
    for p in sorted(path.rglob("*")):
        rel = str(p.relative_to(path))
        if p.is_symlink():
            out[rel] = os.readlink(p)
        elif p.is_dir():
            out[rel] = None
        else:
            out[rel] = p.read_bytes()
    return out


def _hook_script_paths(node):
    """Yield every whitespace-separated token under a JSON value that names a hook script."""
    if isinstance(node, str):
        for token in node.split():
            if "/scripts/hook-" in token and token.endswith(".py"):
                yield token
    elif isinstance(node, dict):
        for value in node.values():
            yield from _hook_script_paths(value)
    elif isinstance(node, list):
        for value in node:
            yield from _hook_script_paths(value)


def assert_scrub_effective(dump_path, scrub_vars, real_dirs_before, real_dirs_after):
    """Assert a sandboxed run could not write through any scrubbed variable.

    (1) the run's environment dump has no `VAR=` line for a scrubbed var,
    (2) every real directory is byte-identical to its earlier snapshot,
    (3) a fallback probe exists beside the dump for every var — the positive
        control proving the run attempted a write through each var, so (1)/(2)
        cannot pass vacuously.
    """
    dump_path = Path(dump_path)
    lines = dump_path.read_text().splitlines()
    for var in scrub_vars:
        leaked = [ln for ln in lines if ln.startswith(var + "=")]
        assert not leaked, f"{var} reached the install chain: {leaked}"
    for name, before in real_dirs_before.items():
        assert real_dirs_after[name] == before, f"real dir {name} was written to"
    for var in scrub_vars:
        probe = dump_path.parent / f"fallback-{var}" / "probe"
        assert probe.is_file(), f"no fallback probe for {var}: write never attempted"


def _base_env(home: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in SCRUB_VARS}
    env = {k: v for k, v in env.items() if not k.startswith("AGENTCTL_")}
    env["HOME"] = str(home)
    return env


@pytest.fixture
def fake_home(tmp_path):
    home = tmp_path / "fakehome"
    (home / ".claude-agent").mkdir(parents=True)
    (home / ".claude-agent" / "keep").write_text("x")
    return home


def test_refuses_protected_and_nonempty_roots_without_writing(tmp_path, fake_home):
    env = _base_env(fake_home)
    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "f").write_text("x")
    cases = [
        fake_home / ".claude-agent",
        fake_home / ".claude-agent" / "sub",
        fake_home,
        REPO / "sandbox-inside-source",
        nonempty,
    ]
    before = _tree(tmp_path)
    for root in cases:
        res = _run([str(SANDBOX), "--source", str(REPO), "--root", str(root)], env)
        assert res.returncode != 0, root
        assert "instruction-sandbox:" in res.stderr, root
    assert _tree(tmp_path) == before
    assert not (REPO / "sandbox-inside-source").exists()


def test_refuses_unresolvable_ref_without_writing(tmp_path, fake_home):
    root = tmp_path / "r"
    res = _run([str(SANDBOX), "--source", str(REPO), "--core-ref", "no-such-ref", "--root", str(root)],
               _base_env(fake_home))
    assert res.returncode != 0
    assert not root.exists()


def test_dry_run_writes_nothing(tmp_path, fake_home):
    root = tmp_path / "r"
    res = _run([str(SANDBOX), "--source", str(REPO), "--root", str(root), "--dry-run"],
               _base_env(fake_home))
    assert res.returncode == 0
    assert res.stdout.strip().splitlines()[-1] == str(root)
    assert not root.exists()


@pytest.fixture(scope="module")
def real_build(tmp_path_factory):
    base = tmp_path_factory.mktemp("isb-real")
    home = base / "callerhome"
    home.mkdir()
    root = base / "r"
    res = _run([str(SANDBOX), "--source", str(REPO), "--core-ref", "HEAD", "--root", str(root)],
               _base_env(home))
    assert res.returncode == 0, res.stderr
    return root, res


def test_real_build_resolves_under_core(real_build):
    root, res = real_build
    assert res.stdout.strip().splitlines()[-1] == str(root)
    core = root / "core"
    for name in ("CLAUDE.md", "config.md", "memory-global"):
        link = root / "home" / ".claude-agent" / name
        assert link.is_symlink() and link.resolve().is_relative_to(core)
    cursor = root / "home" / ".cursor" / "rules" / "claude-code-sync.mdc"
    assert cursor.is_symlink() and cursor.resolve().is_relative_to(core)

    settings = json.loads((root / "home" / ".claude-agent" / "settings.json").read_text())
    hooks = set(_hook_script_paths(settings))
    assert hooks
    for hook in hooks:
        assert Path(hook).resolve().is_relative_to(core / "scripts"), hook

    assert _git(core, "config", "--get", "core.hooksPath") == "githooks"
    assert _git(core, "remote") == ""
    env = (root / "sandbox.env").read_text()
    assert f"ISB_CORE_SHA={_git(REPO, 'rev-parse', 'HEAD')}" in env


def test_build_leaves_source_registry_and_hookspath_unchanged(tmp_path, fake_home):
    before = (_git(REPO, "worktree", "list"), _git(REPO, "config", "--local", "--get", "core.hooksPath"))
    res = _run([str(SANDBOX), "--source", str(REPO), "--root", str(tmp_path / "r")], _base_env(fake_home))
    assert res.returncode == 0, res.stderr
    after = (_git(REPO, "worktree", "list"), _git(REPO, "config", "--local", "--get", "core.hooksPath"))
    assert after == before


@pytest.fixture
def canon_env(tmp_path):
    home = tmp_path / "home"
    canon = home / "claude-agent-instructions"
    (canon / "scripts").mkdir(parents=True)
    (canon / "scripts" / "a.sh").write_text("x")
    _git(canon, "init", "-q")
    _git(canon, "add", "-A")
    _git(canon, "commit", "-q", "-m", "init")
    (home / ".claude-agent").mkdir()
    (home / ".cursor" / "rules").mkdir(parents=True)
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text("{}")
    env = _base_env(home)
    env["CLAUDE_INSTRUCTIONS_CANON"] = str(canon)
    return home, canon, env


def _snap(env) -> str:
    res = _run([str(SNAPSHOT)], env)
    assert res.returncode == 0, res.stderr
    return res.stdout


def test_snapshot_changes_on_canon_mutations(canon_env):
    home, canon, env = canon_env
    base = _snap(env)
    assert "sha256 " in base and "git-hooks-path " in base and "git-worktree " in base

    (home / ".claude" / "settings.json").write_text('{"a": 1}')
    changed = _snap(env)
    assert changed != base
    (home / ".claude" / "settings.json").write_text("{}")
    assert _snap(env) == base

    (home / ".cursor" / "rules" / "x.mdc").symlink_to(canon / "scripts" / "a.sh")
    assert _snap(env) != base
    (home / ".cursor" / "rules" / "x.mdc").unlink()
    assert _snap(env) == base

    _git(canon, "config", "--local", "core.hooksPath", "githooks")
    assert _snap(env) != base
    _git(canon, "config", "--local", "--unset", "core.hooksPath")
    assert _snap(env) == base

    (canon / "scripts" / "a.sh").write_text("edited")
    assert _snap(env) != base


def test_snapshot_ignores_session_worktrees_memory_and_pycache(canon_env):
    home, canon, env = canon_env
    base = _snap(env)

    _git(canon, "worktree", "add", "-q", "--detach", str(canon / ".claude" / "worktrees" / "s1"), "HEAD")
    (canon / ".claude" / "worktrees" / "s1" / "scripts" / "a.sh").write_text("session edit")
    (canon / "memory-global" / "leaves").mkdir(parents=True)
    (canon / "memory-global" / "leaves" / "leaf.md").write_text("note")
    (canon / "scripts" / "__pycache__").mkdir()
    (canon / "scripts" / "__pycache__" / "a.cpython-311.pyc").write_bytes(b"\0")
    assert _snap(env) == base


def test_snapshot_fails_loudly_without_canon(canon_env):
    _, canon, env = canon_env
    env["CLAUDE_INSTRUCTIONS_CANON"] = str(canon.parent / "missing")
    res = _run([str(SNAPSHOT)], env)
    assert res.returncode != 0


_STUB = """#!/usr/bin/env bash
env > "$HOME/setup.env"
for V in {vars}; do
  dir="$(eval echo "\\${{$V:-$HOME/fallback-$V}}")"
  mkdir -p "$dir"
  echo probe > "$dir/probe"
done
"""


def test_scrub_keeps_caller_overrides_away_from_install_chain(tmp_path):
    src = tmp_path / "src"
    (src / "scripts").mkdir(parents=True)
    stub = src / "scripts" / "setup-symlinks.sh"
    stub.write_text(_STUB.format(vars=" ".join(SCRUB_VARS)))
    stub.chmod(0o755)
    _git(src, "init", "-q")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "stub")

    caller_home = tmp_path / "callerhome"
    caller_home.mkdir()
    env = _base_env(caller_home)
    real_dirs = {}
    for var in SCRUB_VARS:
        d = tmp_path / "real" / var
        d.mkdir(parents=True)
        env[var] = str(d)
        real_dirs[var] = d
    # CLAUDE_INSTRUCTIONS_CANON also feeds the root guard; keep it a real dir outside tmp root.
    before = {v: _tree(d) for v, d in real_dirs.items()}

    root = tmp_path / "r"
    res = _run([str(SANDBOX), "--source", str(src), "--root", str(root)], env)
    assert res.returncode == 0, res.stderr

    after = {v: _tree(d) for v, d in real_dirs.items()}
    assert_scrub_effective(root / "home" / "setup.env", SCRUB_VARS, before, after)


def test_assert_scrub_effective_goes_red_on_leak(tmp_path):
    dump = tmp_path / "setup.env"
    dump.write_text("CLAUDE_POLICY_LEDGER=/real\nOTHER=x CLAUDE_POLICY_LEDGER\n")
    with pytest.raises(AssertionError):
        assert_scrub_effective(dump, ["CLAUDE_POLICY_LEDGER"], {}, {})
    dump.write_text("OTHER=/x/CLAUDE_POLICY_LEDGER=y\n")
    (tmp_path / "fallback-CLAUDE_POLICY_LEDGER").mkdir()
    (tmp_path / "fallback-CLAUDE_POLICY_LEDGER" / "probe").write_text("p")
    assert_scrub_effective(dump, ["CLAUDE_POLICY_LEDGER"], {}, {})
    with pytest.raises(AssertionError):
        assert_scrub_effective(dump, ["CLAUDE_POLICY_LEDGER"], {"d": {"f": b"1"}}, {"d": {"f": b"2"}})
