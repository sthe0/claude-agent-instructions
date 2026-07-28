"""Coverage for the difficulty-channel plugin overlay: an adapter that lives
outside this repo because it is org-specific.

Three pieces are under test:
  * verify-difficulty-channel-resolves.sh passes for an unconfigured or built-in
    channel and fails loudly when the configured channel has no adapter, or has
    one that loads without registering under that name,
  * verify-plugin-tests.sh is fail-open by default but fails on demand when the
    tests it was told to expect are missing,
  * setup-symlinks.sh creates the plugin directory the loader reads from.

Everything runs against a temp config root, so the machine's own configuration
never decides the outcome. The channel name used throughout is fabricated: this
repo carries no real channel name but the two built-in ones.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from difficulty_channel.adapters import BUILTIN_NAMES, PLUGIN_DIR_NAME

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
RESOLVE = SCRIPTS / "verify-difficulty-channel-resolves.sh"
PLUGIN_TESTS = SCRIPTS / "verify-plugin-tests.sh"
SETUP_SYMLINKS = SCRIPTS / "setup-symlinks.sh"
SYNC = SCRIPTS / "verify-instructions-sync.sh"

# Not a channel anyone runs — the point is that the control reports whatever
# name it is given without knowing any of them.
FAKE_CHANNEL = "acmecorp"
# A second fabricated name, for the adapter that registers under the wrong one.
OTHER_CHANNEL = "acmecorp_eu"


def _env(agent_home: Path) -> "dict[str, str]":
    env = dict(os.environ)
    # Both would outrank CLAUDE_AGENT_HOME (config_root.py) and let the real
    # machine's config leak into a test that is supposed to control it.
    env.pop("CLAUDE_CONFIG_DIR", None)
    env.pop("CLAUDE_AGENT_IDENTITY", None)
    env.pop("CLAUDE_DIFFICULTY_PLUGIN_DIR", None)
    env["CLAUDE_AGENT_HOME"] = str(agent_home)
    return env


def _run(script: Path, agent_home: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script), *args], env=_env(agent_home),
        capture_output=True, text=True,
    )


def _agent_home(tmp_path: Path, channel: str | None = None) -> Path:
    home = tmp_path / ".claude-agent"
    home.mkdir(parents=True)
    if channel is not None:
        (home / "agent-identity.local").write_text(
            f"difficulty_channel={channel}\n", encoding="utf-8",
        )
    return home


def _install_adapter(agent_home: Path, channel: str) -> Path:
    """Put a minimal, honest adapter in the overlay — enough to satisfy the
    plugin contract (register at import time, absolute imports only)."""
    adapters = agent_home / PLUGIN_DIR_NAME / "adapters"
    adapters.mkdir(parents=True, exist_ok=True)
    adapter = adapters / f"{channel}.py"
    adapter.write_text(
        "from difficulty_channel.port import NullChannel, register_channel\n"
        f"register_channel({channel!r}, NullChannel)\n",
        encoding="utf-8",
    )
    return adapter


def _extract_bash_function(source: str, name: str) -> str:
    """Pull one top-level `name() { ... }` definition verbatim out of a shell
    script, so a test can exercise the real function body without running the
    rest of the script around it."""
    match = re.search(
        rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", source, re.MULTILINE | re.DOTALL,
    )
    assert match, f"function {name}() not found"
    return match.group(0)


# ── verify-difficulty-channel-resolves.sh ────────────────────────────────────

def test_unconfigured_machine_has_nothing_to_resolve(tmp_path):
    """No identity file at all — the default channel is built in, so a foreign
    clone that never configured anything is unaffected by this control."""
    result = _run(RESOLVE, _agent_home(tmp_path))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "built in" in result.stdout


def test_builtin_channel_needs_no_plugin(tmp_path):
    for channel in sorted(BUILTIN_NAMES):
        result = _run(RESOLVE, _agent_home(tmp_path / channel, channel=channel))

        assert result.returncode == 0, result.stdout + result.stderr
        assert channel in result.stdout


def test_configured_channel_without_an_adapter_fails_loudly(tmp_path):
    """The RED direction: this is the whole reason the control exists — such a
    machine otherwise fails only at the moment someone files a difficulty."""
    result = _run(RESOLVE, _agent_home(tmp_path, channel=FAKE_CHANNEL))

    assert result.returncode != 0
    assert "FAIL" in result.stdout
    assert FAKE_CHANNEL in result.stdout


def test_configured_channel_with_its_adapter_installed_passes(tmp_path):
    """The GREEN direction, reached the way a real machine reaches it: by
    putting the adapter where the loader looks."""
    home = _agent_home(tmp_path, channel=FAKE_CHANNEL)
    _install_adapter(home, FAKE_CHANNEL)

    result = _run(RESOLVE, home)

    assert result.returncode == 0, result.stdout + result.stderr
    assert FAKE_CHANNEL in result.stdout


def test_a_broken_adapter_is_reported_as_not_resolving(tmp_path):
    """An adapter that raises on import resolves no better than an absent one,
    so the control must not mistake 'file exists' for 'channel works'."""
    home = _agent_home(tmp_path, channel=FAKE_CHANNEL)
    _install_adapter(home, FAKE_CHANNEL).write_text(
        "raise RuntimeError('adapter is broken')\n", encoding="utf-8",
    )

    result = _run(RESOLVE, home)

    assert result.returncode != 0
    assert "FAIL" in result.stdout


def test_an_adapter_that_registers_under_another_name_fails(tmp_path):
    """Importing is only half the plugin contract: the submit path then calls
    get_channel(name), which raises for a name nobody registered. An adapter
    that imports cleanly but registers something else passes any load-only
    check and blows up at the first real filing."""
    home = _agent_home(tmp_path, channel=FAKE_CHANNEL)
    _install_adapter(home, FAKE_CHANNEL).write_text(
        "from difficulty_channel.port import NullChannel, register_channel\n"
        f"register_channel({OTHER_CHANNEL!r}, NullChannel)\n",
        encoding="utf-8",
    )

    result = _run(RESOLVE, home)

    assert result.returncode != 0
    assert "FAIL" in result.stdout
    assert "did not register" in result.stdout
    assert FAKE_CHANNEL in result.stdout


def test_an_adapter_that_registers_nothing_fails(tmp_path):
    """The same gap reached the other way — a module with no register_channel
    call at all."""
    home = _agent_home(tmp_path, channel=FAKE_CHANNEL)
    _install_adapter(home, FAKE_CHANNEL).write_text(
        "SOME_CONSTANT = 1\n", encoding="utf-8",
    )

    result = _run(RESOLVE, home)

    assert result.returncode != 0
    assert "did not register" in result.stdout


# ── verify-plugin-tests.sh ───────────────────────────────────────────────────

def test_no_plugin_tests_is_not_a_failure(tmp_path):
    """Fail-OPEN default: a clone with no overlay at all must stay unaffected."""
    result = _run(PLUGIN_TESTS, _agent_home(tmp_path))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "nothing to run" in result.stdout


def test_assert_tests_min_rejects_an_empty_suite(tmp_path):
    """The anti-vacuity guard: without it, a suite that collected nothing passes
    as 'no tests' and the extracted adapter is silently untested forever."""
    home = _agent_home(tmp_path)
    (home / PLUGIN_DIR_NAME / "tests").mkdir(parents=True)

    empty = _run(PLUGIN_TESTS, home)
    guarded = _run(PLUGIN_TESTS, home, "--assert-tests-min", "1")

    assert empty.returncode == 0, empty.stdout + empty.stderr
    assert guarded.returncode == 1
    assert "at least 1 plugin test file" in guarded.stdout


def test_assert_tests_min_is_satisfied_by_a_real_suite(tmp_path):
    home = _agent_home(tmp_path)
    tests = home / PLUGIN_DIR_NAME / "tests"
    tests.mkdir(parents=True)
    (tests / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    result = _run(PLUGIN_TESTS, home, "--assert-tests-min", "1")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 plugin test file" in result.stdout


def test_a_failing_plugin_test_propagates(tmp_path):
    """The runner is worthless if it swallows the suite's exit code."""
    home = _agent_home(tmp_path)
    tests = home / PLUGIN_DIR_NAME / "tests"
    tests.mkdir(parents=True)
    (tests / "test_sample.py").write_text("def test_no():\n    assert False\n", encoding="utf-8")

    result = _run(PLUGIN_TESTS, home)

    assert result.returncode != 0


def test_require_if_plugin_installed_is_a_noop_without_a_plugin(tmp_path):
    """The flag the machine-state verifier passes: it must not turn a foreign
    clone's clean run into a failure."""
    result = _run(PLUGIN_TESTS, _agent_home(tmp_path), "--require-if-plugin-installed")

    assert result.returncode == 0, result.stdout + result.stderr


def test_require_if_plugin_installed_demands_tests_once_an_adapter_exists(tmp_path):
    home = _agent_home(tmp_path)
    _install_adapter(home, FAKE_CHANNEL)

    result = _run(PLUGIN_TESTS, home, "--require-if-plugin-installed")

    assert result.returncode == 1
    assert "at least 1 plugin test file" in result.stdout


def test_require_if_plugin_installed_covers_every_plugin_file(tmp_path):
    """The adapter is not the only thing extracted into this dir — a detect hook
    lost its tests the same way, so 'installed' cannot mean 'has an adapter'."""
    home = _agent_home(tmp_path)
    plugin_dir = home / PLUGIN_DIR_NAME
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "detect.py").write_text("def detect(*a, **kw):\n    return None\n", encoding="utf-8")

    result = _run(PLUGIN_TESTS, home, "--require-if-plugin-installed")

    assert result.returncode == 1


def test_a_bad_minimum_is_a_usage_error(tmp_path):
    result = _run(PLUGIN_TESTS, _agent_home(tmp_path), "--assert-tests-min", "one")

    assert result.returncode == 2


# ── setup-symlinks.sh creates the seam ───────────────────────────────────────

def test_installer_creates_the_plugin_dir_the_loader_reads(tmp_path):
    """Running the whole installer is not hermetic (it chains into sub-installers
    that reach outside this sandbox), so pull the one function under test verbatim
    out of the real script and run only it."""
    home = _agent_home(tmp_path)
    body = _extract_bash_function(SETUP_SYMLINKS.read_text(encoding="utf-8"), "ensure_plugin_dir")
    script = f'set -euo pipefail\nCLAUDE_AGENT_HOME="{home}"\n{body}\nensure_plugin_dir\n'

    subprocess.run(["bash", "-c", script], check=True, capture_output=True, text=True)

    plugin_dir = home / PLUGIN_DIR_NAME
    assert (plugin_dir / "adapters").is_dir()
    assert (plugin_dir / "README.md").is_file()


def test_installer_does_not_clobber_an_existing_readme(tmp_path):
    home = _agent_home(tmp_path)
    readme = home / PLUGIN_DIR_NAME / "README.md"
    readme.parent.mkdir(parents=True)
    readme.write_text("machine-local notes\n", encoding="utf-8")
    body = _extract_bash_function(SETUP_SYMLINKS.read_text(encoding="utf-8"), "ensure_plugin_dir")
    script = f'set -euo pipefail\nCLAUDE_AGENT_HOME="{home}"\n{body}\nensure_plugin_dir\n'

    subprocess.run(["bash", "-c", script], check=True, capture_output=True, text=True)

    assert readme.read_text(encoding="utf-8") == "machine-local notes\n"


def test_installer_and_loader_agree_on_the_directory_name():
    """Two independent spellings of one path: the installer's literal and the
    loader's constant. They drift silently — the installer would create a seam
    nothing reads."""
    body = _extract_bash_function(SETUP_SYMLINKS.read_text(encoding="utf-8"), "ensure_plugin_dir")

    assert PLUGIN_DIR_NAME in body


# ── both controls are reachable from the machine-state verifier ──────────────

def test_sync_verifier_runs_both_controls(tmp_path):
    """A control nothing invokes decays unnoticed. Only reachability is asserted —
    the verifier's other checks fail against a non-canonical checkout."""
    env = _env(_agent_home(tmp_path))
    env["CLAUDE_INSTRUCTIONS_REPO"] = str(REPO)

    result = subprocess.run(["bash", str(SYNC)], env=env, capture_output=True, text=True)

    assert "=== Difficulty channel ===" in result.stdout
    assert "=== Plugin tests ===" in result.stdout
