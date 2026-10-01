"""Tests for scripts/instruction-sandbox.sh and its canon-snapshot helper."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SANDBOX = REPO / "scripts" / "instruction-sandbox.sh"
VERIFY = REPO / "scripts" / "instruction-sandbox-verify.sh"
LIVE = REPO / "scripts" / "instruction-sandbox-live.py"
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


def test_dry_run_without_root_creates_no_tmp_dir(fake_home):
    import glob
    before = set(glob.glob("/tmp/instruction-sandbox.*"))
    res = _run([str(SANDBOX), "--source", str(REPO), "--dry-run"], _base_env(fake_home))
    assert res.returncode == 0
    after = set(glob.glob("/tmp/instruction-sandbox.*"))
    assert after == before, f"dry run without --root left behind: {after - before}"


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


PROJECT_CHECK = REPO / "scripts" / "lib" / "instruction-sandbox-project-check.sh"

_COMPOSER = """composer_detect() {{ [[ -f "$1/.marker" ]]; }}
composer_protected_paths() {{ echo "{protected}"; }}
composer_validate() {{ {validate}; }}
composer_compose() {{
  mkdir -p "$ISB_PROJECT_ROOT/.claude"
  echo project > "$ISB_PROJECT_ROOT/CLAUDE.md"
  ln -s "$1/inner" "$ISB_PROJECT_ROOT/.claude/inner"
{extra}
}}
composer_snapshot() {{ {snapshot}; }}
"""


def _composer(plugins: Path, name: str, protected: Path, validate="true", extra="", snapshot="echo state-a") -> Path:
    (plugins / "composers").mkdir(parents=True, exist_ok=True)
    path = plugins / "composers" / f"{name}.sh"
    path.write_text(_COMPOSER.format(protected=protected, validate=validate, extra=extra, snapshot=snapshot))
    return path


def _stub_source(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    (src / "scripts").mkdir(parents=True)
    stub = src / "scripts" / "setup-symlinks.sh"
    stub.write_text("#!/usr/bin/env bash\nexit 0\n")
    stub.chmod(0o755)
    _git(src, "init", "-q")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "stub")
    return src


@pytest.fixture
def project_env(tmp_path, fake_home):
    mount = tmp_path / "mount"
    (mount / "inner").mkdir(parents=True)
    (mount / ".marker").write_text("")
    protected = tmp_path / "protected"
    protected.mkdir()
    plugins = tmp_path / "plugins"
    env = _base_env(fake_home)
    env["CLAUDE_INSTRUCTION_SANDBOX_PLUGIN_DIR"] = str(plugins)
    return mount, protected, plugins, env


def _refused(res, root: Path):
    assert res.returncode != 0
    assert "refus" in res.stderr.lower(), res.stderr
    assert not root.exists()


def _build(args, env):
    return _run([str(SANDBOX), "--source", str(REPO), *args], env)


def test_project_mount_refuses_without_composer(tmp_path, project_env):
    mount, _, plugins, env = project_env
    (plugins / "composers").mkdir(parents=True)
    root = tmp_path / "r"
    res = _build(["--root", str(root), "--project-mount", str(mount)], env)
    _refused(res, root)
    assert str(plugins) in res.stderr


def test_project_mount_refuses_ambiguous_composers(tmp_path, project_env):
    mount, protected, plugins, env = project_env
    _composer(plugins, "one", protected)
    _composer(plugins, "two", protected)
    root = tmp_path / "r"
    _refused(_build(["--root", str(root), "--project-mount", str(mount)], env), root)
    ok = _build(["--root", str(root), "--project-mount", str(mount), "--project-composer", "one", "--dry-run"], env)
    assert ok.returncode == 0, ok.stderr


def test_project_mount_refuses_failed_validation(tmp_path, project_env):
    mount, protected, plugins, env = project_env
    _composer(plugins, "one", protected, validate="echo nope >&2; return 1")
    root = tmp_path / "r"
    _refused(_build(["--root", str(root), "--project-mount", str(mount)], env), root)


def test_project_mount_refuses_protected_path_and_alias(tmp_path, project_env):
    _, protected, plugins, env = project_env
    (protected / ".marker").write_text("")
    (protected / "sub").mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(protected)
    _composer(plugins, "one", protected)
    for mount in (protected, protected / "sub", alias, alias / "sub"):
        root = tmp_path / "r"
        res = _build(["--root", str(root), "--project-mount", str(mount), "--project-composer", "one"], env)
        _refused(res, root)
        assert "protected" in res.stderr


def test_project_mount_refuses_mount_inside_root(tmp_path, project_env):
    _, protected, plugins, env = project_env
    root = tmp_path / "r"
    inner = root / "mount"
    inner.mkdir(parents=True)
    (inner / ".marker").write_text("")
    _composer(plugins, "one", protected)
    res = _build(["--root", str(root), "--project-mount", str(inner)], env)
    assert res.returncode != 0 and "refus" in res.stderr.lower()
    assert _tree(root) == {"mount": None, "mount/.marker": b""}


def test_project_mount_refuses_root_inside_mount_or_protected(tmp_path, project_env):
    mount, protected, plugins, env = project_env
    _composer(plugins, "one", protected)
    root_in_mount = mount / "sub" / "r"
    res = _build(["--root", str(root_in_mount), "--project-mount", str(mount)], env)
    _refused(res, root_in_mount)
    root_in_protected = protected / "r"
    res2 = _build(["--root", str(root_in_protected), "--project-mount", str(mount)], env)
    _refused(res2, root_in_protected)


def test_project_mount_warns_on_incomplete_composer(tmp_path, project_env):
    mount, _, plugins, env = project_env
    (plugins / "composers").mkdir(parents=True, exist_ok=True)
    broken = plugins / "composers" / "broken.sh"
    broken.write_text("composer_detect() { return 0; }\n")
    root = tmp_path / "r"
    res = _build(["--root", str(root), "--project-mount", str(mount)], env)
    _refused(res, root)
    assert "broken.sh" in res.stderr
    assert "five-function" in res.stderr


def test_project_composition_passes_check_and_records_keys(tmp_path, project_env):
    mount, protected, plugins, env = project_env
    composer = _composer(plugins, "one", protected)
    src = _stub_source(tmp_path)
    root = tmp_path / "r"
    res = _run([str(SANDBOX), "--source", str(src), "--root", str(root), "--project-mount", str(mount)], env)
    assert res.returncode == 0, res.stderr
    keys = dict(ln.split("=", 1) for ln in (root / "sandbox.env").read_text().splitlines())
    assert keys["ISB_PROJECT_MOUNT"] == str(mount)
    assert keys["ISB_COMPOSER"] == str(composer)
    assert keys["ISB_PROTECTED"] == str(protected)
    check = _run([str(PROJECT_CHECK), str(root)], env)
    assert check.returncode == 0, check.stdout
    assert check.stdout.startswith("CHECK project:structure PASS")


def test_failed_compose_assertions_fail_build(tmp_path, project_env):
    mount, protected, plugins, env = project_env
    _composer(plugins, "one", protected, extra='rm -rf "$ISB_PROJECT_ROOT/CLAUDE.md"')
    src = _stub_source(tmp_path)
    res = _run([str(SANDBOX), "--source", str(src), "--root", str(tmp_path / "r"), "--project-mount", str(mount)], env)
    assert res.returncode != 0
    assert "CLAUDE.md" in res.stderr


def _composed_root(tmp_path: Path):
    mount = tmp_path / "cmount"
    protected = tmp_path / "cprotected"
    root = tmp_path / "croot"
    (mount / "inner").mkdir(parents=True)
    protected.mkdir()
    (protected / "x").write_text("")
    (root / "project" / ".claude").mkdir(parents=True)
    (root / "project" / "CLAUDE.md").write_text("p")
    (root / "sandbox.env").write_text(f"ISB_PROJECT_MOUNT={mount}\nISB_PROTECTED={protected}\n")
    return root, mount, protected


def _check(root: Path) -> subprocess.CompletedProcess:
    return _run([str(PROJECT_CHECK), str(root)], os.environ.copy())


def test_project_check_fails_on_dangling_and_protected_links(tmp_path):
    root, mount, protected = _composed_root(tmp_path)
    claude = root / "project" / ".claude"
    assert _check(root).returncode == 0

    (claude / "dangling").symlink_to(mount / "missing")
    res = _check(root)
    assert res.returncode == 1 and "FAIL" in res.stdout and "dangling" in res.stdout
    (claude / "dangling").unlink()

    (mount / "hooks").symlink_to(protected / "x")
    (claude / "hooks").symlink_to(mount / "hooks")
    res = _check(root)
    assert res.returncode == 1 and str(protected) in res.stdout
    (claude / "hooks").unlink()

    outside = tmp_path / "outside"
    outside.write_text("")
    (claude / "out").symlink_to(outside)
    assert _check(root).returncode == 1


def test_project_check_inspects_settings_for_protected_paths(tmp_path):
    root, mount, protected = _composed_root(tmp_path)
    claude = root / "project" / ".claude"

    def hook(path):
        return json.dumps({"hooks": {"Stop": [{"command": f"python3 {path}/scripts/h.py"}]}})

    (claude / "settings.json").write_text(hook(root / "project"))
    assert _check(root).returncode == 0

    (claude / "settings.json").write_text(hook(protected))
    res = _check(root)
    assert res.returncode == 1 and "settings.json" in res.stdout and str(protected) in res.stdout

    (claude / "settings.json").write_text(hook(f"{protected}-sibling"))
    assert _check(root).returncode == 0

    (claude / "settings.json").unlink()
    (mount / "local.json").write_text(hook(protected))
    (claude / "settings.local.json").symlink_to(mount / "local.json")
    res = _check(root)
    assert res.returncode == 1 and "settings.local.json" in res.stdout


def test_project_check_fails_on_missing_structure(tmp_path):
    root, _, _ = _composed_root(tmp_path)
    (root / "project" / "CLAUDE.md").unlink()
    assert _check(root).returncode == 1


def test_snapshot_composer_lines_and_failure(canon_env, tmp_path):
    _, _, env = canon_env
    protected = tmp_path / "p"
    plugin = _composer(tmp_path / "pl", "proj", protected, snapshot="echo a; echo b")
    plain = _snap(env)
    res = _run([str(SNAPSHOT), "--composer", str(plugin)], env)
    assert res.returncode == 0, res.stderr
    assert res.stdout == plain + "composer:proj a\ncomposer:proj b\n"
    bad = _composer(tmp_path / "pl", "bad", protected, snapshot="return 3")
    assert _run([str(SNAPSHOT), "--composer", str(bad)], env).returncode != 0


_PROBE = """
env > "$ISB_ROOT/compose.env"
for V in {vars}; do
  dir="$(eval echo "\\${{$V:-$ISB_ROOT/fallback-$V}}")"
  mkdir -p "$dir"
  echo probe > "$dir/probe"
done
d="${{CLAUDE_INSTRUCTION_SANDBOX_PLUGIN_DIR:-$ISB_ROOT/fallback-CLAUDE_INSTRUCTION_SANDBOX_PLUGIN_DIR}}/composers"
mkdir -p "$d"
echo probe > "$d/probe"
"""


def test_compose_scrub_keeps_caller_overrides_away(tmp_path, fake_home):
    mount = tmp_path / "mount"
    (mount / "inner").mkdir(parents=True)
    (mount / ".marker").write_text("")
    protected = tmp_path / "protected"
    protected.mkdir()
    env = _base_env(fake_home)
    real_dirs = {}
    for var in SCRUB_VARS:
        d = tmp_path / "real" / var
        d.mkdir(parents=True)
        env[var] = str(d)
        real_dirs[var] = d
    plugins = real_dirs["CLAUDE_INSTRUCTION_SANDBOX_PLUGIN_DIR"]
    _composer(plugins, "one", protected, extra=_PROBE.format(vars=" ".join(SCRUB_VARS)))
    src = _stub_source(tmp_path)
    before = {v: _tree(d) for v, d in real_dirs.items()}

    root = tmp_path / "r"
    res = _run([str(SANDBOX), "--source", str(src), "--root", str(root), "--project-mount", str(mount)], env)
    assert res.returncode == 0, res.stderr

    after = {v: _tree(d) for v, d in real_dirs.items()}
    assert_scrub_effective(root / "compose.env", SCRUB_VARS, before, after)


def _load_live():
    spec = importlib.util.spec_from_file_location("isb_live", LIVE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AUTH_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
             "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX")


def _credentials(config_dir: Path, token: str) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": token}}))


def test_live_scrub_list_matches_the_other_scrub_lists():
    live = _load_live()
    assert set(live.SCRUB_VARS) | {"AGENTCTL_SAMPLE_VAR"} == set(SCRUB_VARS)
    for source in (VERIFY, SANDBOX):
        text = source.read_text()
        for var in live.SCRUB_VARS:
            assert var in text, f"{var} missing from {source.name}"


def test_child_env_overrides_scrubs_and_lends_without_copying(tmp_path, monkeypatch):
    live = _load_live()
    real_cfg = tmp_path / "realcfg"
    _credentials(real_cfg, "tok-lent")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(real_cfg))
    for var in AUTH_VARS:
        monkeypatch.delenv(var, raising=False)
    caller = {var: "leak" for var in live.SCRUB_VARS}
    caller.update(AGENTCTL_SAMPLE_VAR="leak", AGENTCTL_OTHER="leak", KEEP_ME="1",
                  HOME="/real/home", XDG_STATE_HOME="/real/state")
    root = tmp_path / "root"
    (root / "home").mkdir(parents=True)

    env, status = live.build_child_env(caller, root)

    assert status == "borrowed"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-lent"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in caller
    assert env["KEEP_ME"] == "1"
    home = root / "home"
    assert env["HOME"] == str(home)
    assert env["CLAUDE_AGENT_HOME"] == env["CLAUDE_CONFIG_DIR"] == str(home / ".claude-agent")
    assert env["CLAUDE_INSTRUCTIONS_REPO"] == str(root / "core")
    for xdg in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
        assert Path(env[xdg]).is_relative_to(home), xdg
    for var in live.SCRUB_VARS:
        assert var not in env, var
    assert not [k for k in env if k.startswith("AGENTCTL_")]
    assert not list(root.rglob(".credentials.json"))


def test_child_env_keeps_caller_env_auth(tmp_path, monkeypatch):
    live = _load_live()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty"))
    root = tmp_path / "root"
    (root / "home").mkdir(parents=True)
    env, status = live.build_child_env({"CLAUDE_CODE_OAUTH_TOKEN": "tok-env"}, root)
    assert status == "env_auth"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-env"


STUB_CLAUDE = """#!/usr/bin/env bash
printf '%s\\0' "$@" > "$STUB_LOG.argv"
printf 'HOME=%s\\nSESSION=%s\\nAGENTCTL=%s\\n' "$HOME" "${CLAUDE_CODE_SESSION_ID:-}" "${AGENTCTL_SAMPLE_VAR:-}" > "$STUB_LOG.env"
cat > /dev/null
case "$STUB_MODE" in
  echo) grep -h '^SANDBOX-MARKER: ' "$HOME/.claude-agent/config.md" "$PWD/CLAUDE.md" 2>/dev/null | sed 's/^SANDBOX-MARKER: //'
        printf 'TOKEN=%s\\n' "$CLAUDE_CODE_OAUTH_TOKEN" > "$STUB_LOG.token" ;;
  miss) echo "I cannot see any marker" ;;
  sleep) sleep 30 ;;
  exit7) exit 7 ;;
esac
"""


def _stub_dir(parent: Path) -> Path:
    bin_dir = parent / "stubbin"
    bin_dir.mkdir()
    claude = bin_dir / "claude"
    claude.write_text(STUB_CLAUDE)
    claude.chmod(0o755)
    return bin_dir


def _stub_env(base: dict, bin_dir: Path, log: Path, mode: str) -> dict:
    return {**base, "PATH": f"{bin_dir}:{base.get('PATH', '')}", "STUB_MODE": mode,
            "STUB_LOG": str(log), "CLAUDE_CODE_OAUTH_TOKEN": "tok-stub"}


@pytest.fixture
def stub_claude(tmp_path):
    bin_dir = _stub_dir(tmp_path)
    log = tmp_path / "stublog"

    def make(mode: str, base: dict | None = None) -> dict:
        return _stub_env(dict(os.environ) if base is None else base, bin_dir, log, mode)
    make.log = log
    return make


@pytest.fixture
def live_root(tmp_path):
    root = tmp_path / "liveroot"
    (root / "home" / ".claude-agent").mkdir(parents=True)
    (root / "home" / ".claude-agent" / "config.md").write_text("SANDBOX-MARKER: tok-abc\n")
    return root


def _live(root: Path, env: dict, *extra: str, expect=("tok-abc",)) -> subprocess.CompletedProcess:
    args = ["python3", str(LIVE), "--root", str(root), "--cwd", str(root)]
    for token in expect:
        args += ["--expect", token]
    return _run([*args, *extra], env)


def test_live_exit_0_when_the_reply_carries_every_token(live_root, stub_claude):
    res = _live(live_root, stub_claude("echo"))
    assert res.returncode == 0, res.stdout + res.stderr
    assert res.stdout.splitlines() == ["found tok-abc"]
    assert f"HOME={live_root / 'home'}" in Path(f"{stub_claude.log}.env").read_text()
    assert Path(f"{stub_claude.log}.token").read_text() == "TOKEN=tok-stub\n"
    argv = Path(f"{stub_claude.log}.argv").read_bytes().split(b"\0")
    assert argv[argv.index(b"--tools") + 1] == b""


def test_live_scrubs_caller_session_vars_from_the_launch(live_root, stub_claude):
    env = stub_claude("echo")
    env.update(CLAUDE_CODE_SESSION_ID="leak", AGENTCTL_SAMPLE_VAR="leak")
    assert _live(live_root, env).returncode == 0
    log = Path(f"{stub_claude.log}.env").read_text()
    assert "SESSION=\n" in log and "AGENTCTL=\n" in log


def test_live_exit_1_names_the_missing_token(live_root, stub_claude):
    res = _live(live_root, stub_claude("miss"), expect=("tok-abc", "tok-two"))
    assert res.returncode == 1
    assert res.stdout.splitlines() == ["missing tok-abc", "missing tok-two"]


def test_live_timeout_is_unavailable_not_missing(live_root, stub_claude):
    res = _live(live_root, stub_claude("sleep"), "--timeout", "2")
    assert res.returncode == 3
    assert res.stdout.splitlines() == ["UNAVAILABLE timeout"]


def test_live_launch_exit_is_unavailable_not_missing(live_root, stub_claude):
    res = _live(live_root, stub_claude("exit7"))
    assert res.returncode == 3
    assert res.stdout.splitlines() == ["UNAVAILABLE launch-exit=7"]


def test_live_without_a_lendable_credential_is_unavailable_auth(live_root, stub_claude, tmp_path):
    env = {k: v for k, v in stub_claude("echo").items() if k not in AUTH_VARS}
    env["CLAUDE_CONFIG_DIR"] = str(tmp_path / "no-credentials")
    res = _live(live_root, env)
    assert res.returncode == 3
    assert res.stdout.splitlines() == ["UNAVAILABLE auth"]


def test_live_with_no_expected_token_is_a_usage_error(live_root, stub_claude):
    res = _live(live_root, stub_claude("echo"), expect=())
    assert res.returncode == 2


@pytest.fixture(scope="module")
def verify_world(tmp_path_factory):
    base = tmp_path_factory.mktemp("isb-verify")
    home = base / "callerhome"
    canon = home / "claude-agent-instructions"
    (canon / "scripts").mkdir(parents=True)
    (canon / "scripts" / "a.sh").write_text("x")
    _git(canon, "init", "-q")
    _git(canon, "add", "-A")
    _git(canon, "commit", "-q", "-m", "init")
    (home / ".claude-agent").mkdir()
    env = _base_env(home)
    env["CLAUDE_INSTRUCTIONS_CANON"] = str(canon)
    return base, _stub_env(env, _stub_dir(base), base / "stublog", "echo")


def _build_sandbox(base: Path, env: dict, name: str) -> Path:
    root = base / name
    res = _run([str(SANDBOX), "--source", str(REPO), "--core-ref", "HEAD", "--root", str(root)], env)
    assert res.returncode == 0, res.stderr
    return root


@pytest.fixture(scope="module")
def verify_root(verify_world):
    base, env = verify_world
    return _build_sandbox(base, env, "vroot")


def _verify(root: Path, env: dict, *args: str, mode: str = "echo"):
    res = _run([str(VERIFY), *args, str(root)], {**env, "STUB_MODE": mode})
    checks = {}
    for line in res.stdout.splitlines():
        if line.startswith("CHECK "):
            _, name, status, *detail = line.split(" ", 3)
            checks[name] = (status, detail[0] if detail else "")
    return res, checks


def test_verify_passes_every_check_on_the_unmodified_candidate(verify_root, verify_world):
    _, env = verify_world
    res, checks = _verify(verify_root, env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert res.stdout.splitlines()[-1] == "RESULT: PASS"
    assert {name: status for name, (status, _) in checks.items()} == {
        "static:lint-prose-length": "PASS",
        "static:verify-layout-contract": "PASS",
        "static:verify-instructions-sync": "PASS",
        "static:lint-hooks-executable": "PASS",
        "live:core-marker": "PASS",
        "canon:unchanged": "PASS",
    }


def test_verify_maps_a_completed_launch_without_the_marker_to_fail(verify_root, verify_world):
    _, env = verify_world
    res, checks = _verify(verify_root, env, mode="miss")
    assert res.returncode == 1
    status, detail = checks["live:core-marker"]
    assert status == "FAIL" and detail.startswith("marker-missing isb-core-")


def test_verify_maps_timeout_and_launch_exit_to_unavailable(verify_root, verify_world):
    _, env = verify_world
    for mode, args, reason in (("sleep", ("--timeout", "2"), "timeout"), ("exit7", (), "launch-exit=7")):
        res, checks = _verify(verify_root, env, *args, mode=mode)
        assert checks["live:core-marker"] == ("UNAVAILABLE", reason)
        assert res.returncode in (1, 3)


def test_verify_no_live_skips_the_launch_and_writes_no_marker(verify_world):
    base, env = verify_world
    root = _build_sandbox(base, env, "vnolive")
    _, checks = _verify(root, env, "--no-live")
    assert not [name for name in checks if name.startswith("live:")]
    assert "SANDBOX-MARKER" not in (root / "core" / "config.md").read_text()


def test_verify_names_the_failing_check_for_a_broken_instruction_link(verify_world):
    base, env = verify_world
    root = _build_sandbox(base, env, "vbroken")
    link = root / "home" / ".claude-agent" / "CLAUDE.md"
    link.unlink()
    link.symlink_to("/nonexistent-isb-target")
    res, checks = _verify(root, env, "--no-live")
    assert res.returncode == 1
    assert checks["static:verify-instructions-sync"][0] == "FAIL"
    assert checks["static:lint-hooks-executable"][0] == "PASS"
    assert res.stdout.splitlines()[-1] == "RESULT: FAIL"


def test_verify_replaces_an_earlier_marker_instead_of_stacking(verify_root, verify_world):
    _, env = verify_world
    _verify(verify_root, env)
    _verify(verify_root, env)
    assert (verify_root / "core" / "config.md").read_text().count("SANDBOX-MARKER: ") == 1
