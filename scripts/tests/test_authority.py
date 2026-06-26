"""Core-authority detection + non-author routing (ADR-0001 S3 stage 10).

No real push: the capability probe is mocked. Proves the config flag wins, the push-capability
fallback works when the flag is absent, and a non-author resolves to 'route-to-channel' (file a
DifficultyRecord), never 'edit-core'.
"""
import difficulty_channel as dc
from difficulty_channel import authority


def _rec(ground="core rule is ambiguous"):
    return dc.DifficultyRecord(
        ts="2026-06-26T00:00:00", layer="core", target="CLAUDE.md",
        functional_ground=ground, severity=dc.Severity.HIGH, reporter="agent", evidence="e",
    )


def test_is_author_honours_config_flag(tmp_path):
    cfg = tmp_path / "config.md"
    cfg.write_text("| `is_author` | `false` | per machine |\n", encoding="utf-8")
    # probe would say True, but the flag must win
    assert authority.is_author(config_path=cfg, probe=lambda: True) is False
    cfg.write_text("| `is_author` | `true` | per machine |\n", encoding="utf-8")
    assert authority.is_author(config_path=cfg, probe=lambda: False) is True


def test_explicit_flag_param_wins():
    assert authority.is_author(flag=False, probe=lambda: True) is False
    assert authority.is_author(flag=True, probe=lambda: False) is True


def test_fallback_to_push_capability_when_flag_absent(tmp_path):
    cfg = tmp_path / "config.md"  # no is_author row
    cfg.write_text("| `something-else` | `1` | x |\n", encoding="utf-8")
    assert authority.is_author(config_path=cfg, probe=lambda: True) is True
    assert authority.is_author(config_path=cfg, probe=lambda: False) is False


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
