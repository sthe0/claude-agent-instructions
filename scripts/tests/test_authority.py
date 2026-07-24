"""Core-authority detection + non-author routing (ADR-0001 S3 stage 10).

No real push: the capability probe is mocked. Proves the push-capability probe drives is_author()
(no config-flag branch), the flag param is a test seam, and a non-author resolves to
'route-to-channel'. Also tests channel selection from the machine-local identity file.
"""
import difficulty_channel as dc
from difficulty_channel import authority


def _rec(ground="core rule is ambiguous"):
    return dc.DifficultyRecord(
        ts="2026-06-26T00:00:00", layer="core", target="CLAUDE.md",
        functional_ground=ground, severity=dc.Severity.HIGH, reporter="agent", evidence="e",
    )


def test_explicit_flag_param_wins():
    assert authority.is_author(flag=False, probe=lambda: True) is False
    assert authority.is_author(flag=True, probe=lambda: False) is True


def test_push_capability_is_authoritative():
    """Config-flag branch is gone; push probe always decides when no flag is passed."""
    assert authority.is_author(probe=lambda: True) is True
    assert authority.is_author(probe=lambda: False) is False


def test_fallback_to_push_capability_when_flag_absent():
    assert authority.is_author(probe=lambda: True) is True
    assert authority.is_author(probe=lambda: False) is False


def test_probe_uses_dry_run_no_real_push():
    seen = {}

    def fake_runner(cmd):
        seen["cmd"] = cmd
        return 0

    assert authority.probe_push_capability(runner=fake_runner) is True
    assert "--dry-run" in seen["cmd"] and "push" in seen["cmd"]


def test_non_author_routes_to_channel_not_core():
    assert authority.route_for_core_difficulty(author=False) == authority.ROUTE_TO_CHANNEL
    assert authority.route_for_core_difficulty(author=True) == authority.ROUTE_EDIT_CORE


def test_non_author_files_difficulty_to_channel():
    ch = dc.NullChannel()
    dc.register_channel("auth-test", lambda: ch)
    handle = authority.file_core_difficulty(_rec(), channel="auth-test")
    assert handle and ch.pull()[0].functional_ground == "core rule is ambiguous"


def test_channel_from_local_identity_file(tmp_path):
    identity = tmp_path / "agent-identity.local"
    identity.write_text("difficulty_channel=github\n", encoding="utf-8")
    assert authority.read_configured_channel(path=identity) == "github"


def test_channel_defaults_to_github_when_file_absent(tmp_path):
    missing = tmp_path / "no-such-file.local"
    assert authority.read_configured_channel(path=missing) == "github"


def test_local_identity_ignores_comments(tmp_path):
    identity = tmp_path / "agent-identity.local"
    identity.write_text(
        "# this is a comment\n"
        "difficulty_channel=github\n"
        "# another comment\n",
        encoding="utf-8",
    )
    result = authority.read_local_identity(path=identity)
    assert result == {"difficulty_channel": "github"}


def test_file_core_difficulty_uses_configured_channel(tmp_path):
    """file_core_difficulty() reads the machine-local channel when none is passed explicitly."""
    identity = tmp_path / "agent-identity.local"
    identity.write_text("difficulty_channel=orgchan\n", encoding="utf-8")

    ch = dc.NullChannel()
    dc.register_channel("orgchan-identity-test", lambda: ch)

    # Patch the configured channel to return our test channel name.
    original = authority.read_configured_channel
    authority.read_configured_channel = lambda path=None: "orgchan-identity-test"
    try:
        authority.file_core_difficulty(_rec("identity channel test"))
    finally:
        authority.read_configured_channel = original

    assert ch.pull()[0].functional_ground == "identity channel test"
