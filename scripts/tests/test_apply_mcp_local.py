"""Tests for scripts/apply-mcp-local.sh / scripts/lib/mcp_registration.py.

The script registers MCP server definitions into <root>/.claude.json — the file
Claude Code reads user-scope servers from. Every test runs against a throwaway
root; none touches the real config root. test_real_cli is the consumer-side
control: it asks the actual `claude` CLI whether it sees the registered servers.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
APPLY = SCRIPTS / "apply-mcp-local.sh"

_SECRET = "s3cr3t-token-value-4242"
_LOGGED_IN = {"oauthAccount": {"emailAddress": "dummy@example.invalid"}, "numStartups": 3}


class Env:
    """A throwaway config root plus the two definition directories."""

    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "root"
        self.local = tmp_path / "mcp-local"
        self.plugins = tmp_path / "mcp-plugins"
        self.home = tmp_path / "home"
        for d in (self.root, self.local, self.plugins, self.home):
            d.mkdir()
        self.claude_json = self.root / ".claude.json"

    def login(self, extra: dict | None = None, mode: int | None = None) -> None:
        self.claude_json.write_text(json.dumps({**_LOGGED_IN, **(extra or {})}, indent=2))
        if mode is not None:
            self.claude_json.chmod(mode)

    def define(self, kind: str, name: str, cfg: dict | str) -> None:
        directory = self.local if kind == "local" else self.plugins
        text = cfg if isinstance(cfg, str) else json.dumps(cfg)
        (directory / f"{name}.json").write_text(text)

    def env(self, *, plugin_seam: bool = True) -> dict:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("CLAUDE_", "ANTHROPIC_"))}
        env.update(HOME=str(self.home), CLAUDE_AGENT_HOME=str(self.root),
                   CLAUDE_MCP_LOCAL_DIR=str(self.local))
        if plugin_seam:
            env["CLAUDE_MCP_PLUGIN_DIR"] = str(self.plugins)
        return env

    def run(self, *args: str, plugin_seam: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(APPLY), *args], env=self.env(plugin_seam=plugin_seam),
                              capture_output=True, text=True, timeout=60)

    def servers(self) -> dict:
        return json.loads(self.claude_json.read_text()).get("mcpServers", {})


@pytest.fixture
def env(tmp_path) -> Env:
    return Env(tmp_path)


def _stdio(cmd: str = "example-server", **extra) -> dict:
    return {"type": "stdio", "command": cmd, "args": [], **extra}


def test_registers_local_and_plugin_servers(env):
    env.login({"projects": {"/x": {"allowedTools": []}}}, mode=0o640)
    env.define("local", "alpha", _stdio("alpha-cmd"))
    env.define("plugin", "beta", _stdio("beta-cmd"))
    before = json.loads(env.claude_json.read_text())
    proc = env.run()
    assert proc.returncode == 0, proc.stderr
    after = json.loads(env.claude_json.read_text())
    assert after["mcpServers"] == {"alpha": _stdio("alpha-cmd"), "beta": _stdio("beta-cmd")}
    assert {k: v for k, v in after.items() if k != "mcpServers"} == before
    assert stat.S_IMODE(env.claude_json.stat().st_mode) == 0o640
    assert "registered: alpha (mcp-local)" in proc.stdout
    assert "registered: beta (mcp-plugins)" in proc.stdout


def test_rerun_is_idempotent(env):
    env.login()
    env.define("local", "alpha", _stdio())
    assert env.run().returncode == 0
    first = env.claude_json.read_bytes()
    proc = env.run()
    assert proc.returncode == 0
    assert env.claude_json.read_bytes() == first
    assert "registered:" not in proc.stdout


def test_changed_definition_is_updated(env):
    env.login()
    env.define("local", "alpha", _stdio("old-cmd"))
    env.run()
    env.define("local", "alpha", _stdio("new-cmd"))
    proc = env.run()
    assert proc.returncode == 0
    assert env.servers()["alpha"]["command"] == "new-cmd"
    assert "registered: alpha" in proc.stdout


def test_not_logged_in_without_file_creates_nothing(env):
    env.define("local", "alpha", _stdio())
    proc = env.run()
    assert proc.returncode == 0
    assert "log in" in proc.stdout
    assert not env.claude_json.exists()


def test_not_logged_in_without_oauth_account_leaves_file_untouched(env):
    env.claude_json.write_text(json.dumps({"numStartups": 1}))
    before = env.claude_json.read_bytes()
    env.define("local", "alpha", _stdio())
    proc = env.run()
    assert proc.returncode == 0
    assert "log in" in proc.stdout
    assert env.claude_json.read_bytes() == before


def test_no_definitions_is_nothing_to_do(env):
    env.login()
    before = env.claude_json.read_bytes()
    proc = env.run()
    assert proc.returncode == 0
    assert "nothing to do" in proc.stdout
    assert env.claude_json.read_bytes() == before


def test_secret_values_never_reach_output(env):
    env.login()
    env.define("local", "alpha", _stdio("alpha-cmd", env={"API_TOKEN": _SECRET}))
    env.define("plugin", "beta", {"type": "http", "url": "https://example.invalid/mcp",
                                  "headers": {"Authorization": f"Bearer {_SECRET}"}})
    for args in ((), ("--check",)):
        proc = env.run(*args)
        assert _SECRET not in proc.stdout + proc.stderr
        assert "alpha-cmd" not in proc.stdout + proc.stderr
        assert "example.invalid" not in proc.stdout + proc.stderr
    env.claude_json.unlink()
    env.login()
    env.define("plugin", "gamma", "{ not json " + _SECRET)
    proc = env.run()
    assert proc.returncode == 2
    assert _SECRET not in proc.stdout + proc.stderr


def test_local_definition_wins_over_plugin_definition(env):
    env.login()
    env.define("local", "alpha", _stdio("from-local"))
    env.define("plugin", "alpha", _stdio("from-plugin"))
    proc = env.run()
    assert proc.returncode == 0
    assert env.servers()["alpha"]["command"] == "from-local"
    assert "alpha is defined in both" in proc.stdout


def test_malformed_definition_aborts_without_writing(env):
    env.login()
    before = env.claude_json.read_bytes()
    env.define("local", "good", _stdio())
    env.define("plugin", "broken", "{ not json")
    proc = env.run()
    assert proc.returncode == 2
    assert "broken.json" in proc.stderr
    assert env.claude_json.read_bytes() == before


def test_non_object_definition_aborts_without_writing(env):
    env.login()
    before = env.claude_json.read_bytes()
    env.define("local", "listy", "[1, 2]")
    proc = env.run()
    assert proc.returncode == 2
    assert "listy.json" in proc.stderr
    assert env.claude_json.read_bytes() == before


def test_unparseable_target_is_an_error_and_untouched(env):
    env.claude_json.write_text("{ truncated")
    before = env.claude_json.read_bytes()
    env.define("local", "alpha", _stdio())
    proc = env.run()
    assert proc.returncode == 2
    assert ".claude.json" in proc.stderr
    assert env.claude_json.read_bytes() == before


def test_unrelated_registration_is_preserved(env):
    env.login({"mcpServers": {"hand-added": {"type": "stdio", "command": "keep-me"}}})
    env.define("local", "alpha", _stdio())
    assert env.run().returncode == 0
    servers = env.servers()
    assert servers["hand-added"] == {"type": "stdio", "command": "keep-me"}
    assert "alpha" in servers


def test_settings_local_json_is_neither_created_nor_modified(env):
    env.login()
    env.define("local", "alpha", _stdio())
    env.run()
    assert not (env.root / "settings.local.json").exists()
    existing = env.root / "settings.local.json"
    existing.write_text('{"keep": true}\n')
    env.define("local", "beta", _stdio())
    env.run()
    assert existing.read_text() == '{"keep": true}\n'


def test_check_names_missing_servers_without_values(env):
    env.login({"mcpServers": {"alpha": _stdio()}})
    env.define("local", "alpha", _stdio())
    env.define("local", "beta", _stdio("beta-cmd", env={"API_TOKEN": _SECRET}))
    env.define("plugin", "gamma", _stdio())
    before = env.claude_json.read_bytes()
    proc = env.run("--check")
    assert proc.returncode == 1
    assert proc.stdout.strip() == "missing: beta, gamma"
    assert env.claude_json.read_bytes() == before


def test_check_passes_when_everything_is_registered(env):
    env.login()
    env.define("local", "alpha", _stdio())
    env.run()
    proc = env.run("--check")
    assert proc.returncode == 0
    assert "missing" not in proc.stdout


def test_check_skips_when_not_logged_in_or_nothing_defined(env):
    assert env.run("--check").returncode == 0
    env.define("local", "alpha", _stdio())
    proc = env.run("--check")
    assert proc.returncode == 0
    assert "log in" in proc.stdout
    assert not env.claude_json.exists()


def test_check_reports_malformed_definition_as_checker_error(env):
    env.login()
    env.define("local", "broken", "{ nope")
    assert env.run("--check").returncode == 2


def test_plugin_seam_defaults_to_mcp_plugins_under_the_root(env):
    env.login()
    (env.root / "mcp-plugins").mkdir()
    (env.root / "mcp-plugins" / "orgsrv.json").write_text(json.dumps(_stdio("org-cmd")))
    proc = env.run(plugin_seam=False)
    assert proc.returncode == 0, proc.stderr
    assert env.servers()["orgsrv"]["command"] == "org-cmd"


def test_unknown_argument_is_a_usage_error(env):
    assert env.run("--bogus").returncode == 2


def test_judge_isolation_guard(env, monkeypatch, tmp_path):
    """Registering servers into the system root must not leak into judge runs:
    a judge child gets a fresh sandbox root with no mcpServers and is started
    with --strict-mcp-config."""
    sys.path.insert(0, str(SCRIPTS))
    from lib import host_llm

    env.login({"mcpServers": {"ambient-server": _stdio()}})
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(env.root))
    monkeypatch.setattr(host_llm, "_SANDBOX_ROOT", tmp_path / "sandbox")
    kwargs = host_llm.isolated_run_kwargs()
    child_root = Path(kwargs["env"]["CLAUDE_CONFIG_DIR"])
    assert child_root.resolve() != env.root.resolve()
    child_json = child_root / ".claude.json"
    if child_json.exists():
        assert "mcpServers" not in json.loads(child_json.read_text())
    assert "--strict-mcp-config" in host_llm.LEAN_ISOLATION_FLAGS


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not on PATH")
def test_real_cli(env, tmp_path):
    """The consumer sees what the script registered: `claude mcp list` under the
    prepared root lists both a personal and an org-layer server."""
    suffix = uuid.uuid4().hex[:10]
    names = [f"corelocal-{suffix}", f"coreplugin-{suffix}"]
    exiting = _stdio(sys.executable, args=["-c", "import sys; sys.exit(0)"])
    env.login()
    env.define("local", names[0], exiting)
    env.define("plugin", names[1], exiting)
    assert env.run().returncode == 0

    cwd = tmp_path / "empty-cwd"
    cwd.mkdir()
    cli_env = env.env()
    cli_env["CLAUDE_CONFIG_DIR"] = str(env.root)
    proc = subprocess.run(["claude", "mcp", "list"], env=cli_env, cwd=cwd,
                          capture_output=True, text=True, timeout=120)
    out = proc.stdout + proc.stderr
    for name in names:
        assert name in out, out
