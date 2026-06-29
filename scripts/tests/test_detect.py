"""Tests for difficulty_channel.detect — all probes injected, no real host access."""
from difficulty_channel.detect import detect_channel, DetectResult


def _make(
    fqdn: str = "laptop.local",
    commands: set[str] | None = None,
    paths: set[str] | None = None,
    env: dict[str, str] | None = None,
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
    )


def test_strong_internal_only_gives_startrek():
    result = _make(
        commands={"ya", "arc"},
        paths={"~/.tracker-token"},
    )
    assert result.channel == "startrek"
    assert "arcadia-toolchain" in result.evidence
    assert result.warnings == []


def test_both_tokens_strong_internal_wins():
    """When a machine has both internal signals and github creds, startrek wins."""
    result = _make(
        commands={"ya", "arc", "gh"},
        paths={"~/.tracker-token"},
    )
    assert result.channel == "startrek"
    assert "arcadia-toolchain" in result.evidence
    assert result.warnings == []


def test_github_only_gives_github():
    result = _make(paths={"~/.github-token"})
    assert result.channel == "github"
    assert "github-token-file" in result.evidence
    assert result.warnings == []


def test_tracker_token_only_gives_startrek():
    """Weak internal signal with no github cred routes to startrek."""
    result = _make(paths={"~/.tracker-token"})
    assert result.channel == "startrek"
    assert "tracker-token" in result.evidence
    assert result.warnings == []


def test_nothing_gives_github_with_warning():
    result = _make()
    assert result.channel == "github"
    assert result.evidence == []
    assert len(result.warnings) == 1
    assert "no credentials" in result.warnings[0]


def test_strong_internal_without_tracker_token_warns():
    """Internal machine with arcadia toolchain but no tracker-token gets a warning."""
    result = _make(commands={"ya", "arc"})
    assert result.channel == "startrek"
    assert "arcadia-toolchain" in result.evidence
    assert len(result.warnings) == 1
    assert "tracker-token" in result.warnings[0]


def test_corp_hostname_is_strong_signal():
    result = _make(fqdn="dev-machine.yandex-team.ru", paths={"~/.tracker-token"})
    assert result.channel == "startrek"
    assert "corp-hostname" in result.evidence


def test_github_token_env_detected():
    result = _make(env={"GITHUB_TOKEN": "ghp_fake"})
    assert result.channel == "github"
    assert "github-token-env" in result.evidence


def test_etc_yandex_is_strong_signal():
    result = _make(paths={"/etc/yandex", "~/.tracker-token"})
    assert result.channel == "startrek"
    assert "etc-yandex" in result.evidence


def test_skotty_command_is_strong_signal():
    result = _make(commands={"skotty"}, paths={"~/.tracker-token"})
    assert result.channel == "startrek"
    assert "skotty" in result.evidence
