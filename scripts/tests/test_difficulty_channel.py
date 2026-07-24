"""DifficultyChannel port + record schema + registry (ADR-0001 S3 stage 7).

The port is transport-agnostic: these tests exercise the in-memory NullChannel double only,
never external I/O. They pin the record schema (the single join contract), the severity enum,
the submit/pull(since) round-trip, and config-routed registry resolution.

Also covers the machine-local adapter-plugin seam (``difficulty_channel.adapters.load_adapter``,
B1): a non-built-in channel name resolves from a plugin dir (synthetic adapter, not a real
tracker — no network), an unknown name fails with a clear error rather than crashing, and a
built-in name never touches the plugin dir at all, so a machine with none configured still
resolves.
"""
# scripts/ is on sys.path via conftest.py, so the package imports normally.
import pytest

import difficulty_channel as dc
from difficulty_channel import adapters


def _rec(ts="2026-06-26T00:00:00", ground="gate denies a legit write", sev=dc.Severity.HIGH):
    return dc.DifficultyRecord(
        ts=ts,
        layer="core",
        target="CLAUDE.md",
        functional_ground=ground,
        severity=sev,
        reporter="agent",
        evidence="quote from session",
    )


def test_record_round_trips_through_in_memory_channel():
    ch = dc.NullChannel()
    handle = ch.submit(_rec())
    assert isinstance(handle, str) and handle
    pulled = ch.pull()
    assert len(pulled) == 1
    assert pulled[0].functional_ground == "gate denies a legit write"
    assert pulled[0].severity is dc.Severity.HIGH


def test_submit_then_pull_since_filters():
    ch = dc.NullChannel()
    ch.submit(_rec(ts="2026-06-01T00:00:00"))
    ch.submit(_rec(ts="2026-06-20T00:00:00"))
    recent = ch.pull(since="2026-06-10T00:00:00")
    assert len(recent) == 1
    assert recent[0].ts == "2026-06-20T00:00:00"
    assert len(ch.pull()) == 2  # None == all


def test_severity_enum_validates_and_carries_mass():
    assert dc.Severity.parse("critical") is dc.Severity.CRITICAL
    # raw string in the record is normalised to the enum
    r = dc.DifficultyRecord(
        ts="t", layer="team", target="x", functional_ground="g",
        severity="medium", reporter="r",
    )
    assert r.severity is dc.Severity.MEDIUM
    assert dc.Severity.CRITICAL.mass > dc.Severity.LOW.mass
    with pytest.raises(ValueError):
        dc.Severity.parse("catastrophic")


def test_empty_functional_ground_rejected():
    with pytest.raises(ValueError):
        dc.DifficultyRecord(
            ts="t", layer="core", target="x", functional_ground="   ",
            severity=dc.Severity.LOW, reporter="r",
        )


def test_registry_resolves_name_to_channel():
    ch = dc.get_channel("null")
    assert isinstance(ch, dc.DifficultyChannel)
    assert isinstance(ch, dc.NullChannel)
    with pytest.raises(KeyError):
        dc.get_channel("does-not-exist")


def test_register_custom_channel():
    dc.register_channel("null2", dc.NullChannel)
    assert isinstance(dc.get_channel("null2"), dc.NullChannel)


# ── Adapter plugin seam (B1) ──────────────────────────────────────────────────

_SYNTHETIC_ADAPTER_SRC = '''
from difficulty_channel.port import DifficultyChannel, register_channel


class AcmeCorpChannel(DifficultyChannel):
    """Synthetic test-only adapter — proves the plugin-present resolution path, nothing more."""

    def __init__(self, **kwargs):
        self._store = []

    def submit(self, record):
        self._store.append(record)
        return "acmecorp-1"

    def pull(self, since=None):
        return list(self._store)


register_channel("acmecorp", AcmeCorpChannel)
'''


def test_load_adapter_plugin_present_resolves_synthetic_channel(monkeypatch, tmp_path):
    plugin_dir = tmp_path / "difficulty-channel-plugins"
    (plugin_dir / "adapters").mkdir(parents=True)
    (plugin_dir / "adapters" / "acmecorp.py").write_text(_SYNTHETIC_ADAPTER_SRC, encoding="utf-8")
    monkeypatch.setenv("CLAUDE_DIFFICULTY_PLUGIN_DIR", str(plugin_dir))

    adapters.load_adapter("acmecorp")

    ch = dc.get_channel("acmecorp")
    assert ch.__class__.__name__ == "AcmeCorpChannel"
    assert ch.submit(_rec()) == "acmecorp-1"


def test_load_adapter_plugin_absent_raises_clear_error(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_DIFFICULTY_PLUGIN_DIR", str(tmp_path))  # no adapters/ subdir at all

    with pytest.raises(FileNotFoundError, match="no-such-adapter"):
        adapters.load_adapter("no-such-adapter")


def test_load_adapter_builtin_noop_with_no_plugin_dir_configured(monkeypatch, tmp_path):
    """The real DEFAULT path: nothing configured, no plugin dir on this machine at all."""
    monkeypatch.delenv("CLAUDE_DIFFICULTY_PLUGIN_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)  # agent_home() consults it first
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(tmp_path))  # exists, but has no plugins subdir

    adapters.load_adapter("github")  # must not raise, must never look at the plugin dir

    assert isinstance(dc.get_channel("github"), adapters.GitHubChannel)
