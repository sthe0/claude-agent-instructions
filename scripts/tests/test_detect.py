"""Tests for difficulty_channel.detect — all probes injected, no real host access.

Two halves, mirroring the module's two responsibilities:

* the NEUTRAL rules Core keeps (github credentials, and the credential-less default),
  exercised with no hook at all — i.e. the real default path, not a stub of it;
* the HOOK SEAM through which an org attaches its own signals, exercised with a
  synthetic hook and with a synthetic plugin dir (never this machine's real one).
"""
import pytest

from difficulty_channel.detect import (
    DetectResult,
    detect_channel,
    load_detect_hook,
)


def _make(
    fqdn: str = "laptop.local",
    commands: set[str] | None = None,
    paths: set[str] | None = None,
    env: dict[str, str] | None = None,
    hook=None,
) -> DetectResult:
    """Build a DetectResult using fake injected probes."""
    cmds = commands or set()
    existing_paths = paths or set()
    envvars = env or {}
    return detect_channel(
        hostname=lambda: fqdn,
        has_command=lambda cmd: cmd in cmds,
        path_exists=lambda p: p in existing_paths,
        getenv=lambda k: envvars.get(k),
        hook=hook,
    )


# ── neutral rules (no hook — the real default path) ───────────────────────────

def test_github_token_file_gives_github():
    result = _make(paths={"~/.github-token"})
    assert result.channel == "github"
    assert "github-token-file" in result.evidence
    assert result.warnings == []


def test_github_token_env_detected():
    result = _make(env={"GITHUB_TOKEN": "ghp_fake"})
    assert result.channel == "github"
    assert "github-token-env" in result.evidence
    assert result.warnings == []


def test_gh_cli_detected():
    result = _make(commands={"gh"})
    assert result.channel == "github"
    assert "gh-cli" in result.evidence
    assert result.warnings == []


def test_nothing_gives_github_with_warning():
    result = _make()
    assert result.channel == "github"
    assert result.evidence == []
    assert len(result.warnings) == 1
    assert "no credentials" in result.warnings[0]


def test_no_signal_is_org_neutral():
    """Core has no rule keyed on a hostname, a toolchain, or an org filesystem path:
    with no hook installed, none of them can move the channel off the default."""
    result = _make(
        fqdn="dev-machine.internal.example.com",
        commands={"some-corp-cli", "another-corp-cli"},
        paths={"/etc/some-corp", "~/.some-corp-token"},
    )
    assert result.channel == "github"
    assert result.evidence == []


# ── hook seam ─────────────────────────────────────────────────────────────────

def test_hook_decision_wins_over_neutral_rules():
    """A hook that decides overrides even a present github credential — the org's own
    precedence is the hook's business, not Core's."""
    hook = lambda **kw: DetectResult(  # noqa: E731
        channel="orgchan", evidence=["org-signal"], warnings=["org-warning"]
    )
    result = _make(commands={"gh"}, hook=hook)
    assert result.channel == "orgchan"
    assert result.evidence == ["org-signal"]
    assert result.warnings == ["org-warning"]


def test_hook_returning_none_defers_to_neutral_rules():
    result = _make(paths={"~/.github-token"}, hook=lambda **kw: None)
    assert result.channel == "github"
    assert "github-token-file" in result.evidence


def test_hook_returning_none_still_reaches_the_default():
    result = _make(hook=lambda **kw: None)
    assert result.channel == "github"
    assert "no credentials" in result.warnings[0]


def test_hook_receives_all_four_probes_by_keyword():
    seen = {}

    def hook(**kwargs):
        seen.update(kwargs)
        return None

    _make(fqdn="host.example.com", commands={"gh"}, hook=hook)
    assert set(seen) == {"hostname", "has_command", "path_exists", "getenv"}
    assert seen["hostname"]() == "host.example.com"
    assert seen["has_command"]("gh") is True


# ── load_detect_hook (the one impure entry point) ─────────────────────────────

def test_plugin_absent_yields_no_hook_and_the_default_channel(tmp_path, monkeypatch):
    """The REAL default path, with no stub anywhere: an empty plugin dir installs no
    hook, and detection then resolves github without raising."""
    monkeypatch.setenv("CLAUDE_DIFFICULTY_PLUGIN_DIR", str(tmp_path / "empty-plugins"))
    hook = load_detect_hook()
    assert hook is None
    result = _make(hook=hook)
    assert result.channel == "github"


def test_plugin_dir_without_detect_hook_is_not_an_error(tmp_path, monkeypatch):
    """A machine that installs adapters but no detect hook must still detect cleanly."""
    (tmp_path / "adapters").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_DIFFICULTY_PLUGIN_DIR", str(tmp_path))
    assert load_detect_hook() is None


def test_plugin_detect_hook_is_loaded_and_used(tmp_path, monkeypatch):
    (tmp_path / "detect.py").write_text(
        "from difficulty_channel.detect import DetectResult\n"
        "def detect(hostname, has_command, path_exists, getenv):\n"
        "    if has_command('orgtool'):\n"
        "        return DetectResult(channel='orgchan', evidence=['orgtool'])\n"
        "    return None\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_DIFFICULTY_PLUGIN_DIR", str(tmp_path))
    hook = load_detect_hook()
    assert hook is not None
    assert _make(commands={"orgtool"}, hook=hook).channel == "orgchan"
    assert _make(commands={"gh"}, hook=hook).channel == "github"


def test_plugin_dirs_are_not_confused_with_each_other(tmp_path, monkeypatch):
    """The plugin dir is part of the loaded module's identity — two dirs, two hooks."""
    for name in ("a", "b"):
        d = tmp_path / name
        d.mkdir()
        (d / "detect.py").write_text(
            "from difficulty_channel.detect import DetectResult\n"
            f"def detect(**kw):\n    return DetectResult(channel='chan-{name}')\n",
            encoding="utf-8",
        )
    monkeypatch.setenv("CLAUDE_DIFFICULTY_PLUGIN_DIR", str(tmp_path / "a"))
    assert _make(hook=load_detect_hook()).channel == "chan-a"
    monkeypatch.setenv("CLAUDE_DIFFICULTY_PLUGIN_DIR", str(tmp_path / "b"))
    assert _make(hook=load_detect_hook()).channel == "chan-b"


def test_broken_plugin_hook_raises_and_is_not_cached(tmp_path, monkeypatch):
    """A half-executed module must not be served to the next caller."""
    (tmp_path / "detect.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_DIFFICULTY_PLUGIN_DIR", str(tmp_path))
    with pytest.raises(RuntimeError):
        load_detect_hook()
    with pytest.raises(RuntimeError):
        load_detect_hook()
