"""core-difficulty-digest clustering + mass + flag (ADR-0001 S3 stage 9).

Channels are mocked with the in-memory double; no network. Proves: two records with the same
functional_ground from DIFFERENT channels collapse into ONE cluster; mass sums severity weights;
a critical item flags regardless of mass; a sub-threshold cluster is not flagged.
"""
import importlib.util
import sys
from pathlib import Path

import difficulty_channel as dc

_SPEC = importlib.util.spec_from_file_location(
    "core_difficulty_digest",
    Path(__file__).resolve().parents[1] / "core-difficulty-digest.py",
)
digest_mod = importlib.util.module_from_spec(_SPEC)
# register before exec so @dataclass can resolve cls.__module__ via sys.modules
sys.modules["core_difficulty_digest"] = digest_mod
_SPEC.loader.exec_module(digest_mod)


def _rec(ground, sev, reporter, ts="2026-06-26T00:00:00", target="CLAUDE.md"):
    return dc.DifficultyRecord(
        ts=ts, layer="core", target=target, functional_ground=ground,
        severity=sev, reporter=reporter, evidence="e",
    )


def test_same_ground_two_channels_one_cluster():
    recs = [
        _rec("gate denies a legitimate memory write", dc.Severity.MEDIUM, "startrek"),
        _rec("gate denies a legitimate memory write", dc.Severity.MEDIUM, "external"),
    ]
    clusters = digest_mod.cluster_records(recs)
    assert len(clusters) == 1
    assert clusters[0].reporters == {"startrek", "external"}


def test_mass_sums_severity_weights():
    recs = [
        _rec("ground A", dc.Severity.HIGH, "c1"),
        _rec("ground A", dc.Severity.MEDIUM, "c2"),
    ]
    [cluster] = digest_mod.cluster_records(recs)
    # high(4) + medium(2) == 6; geometric ladder means one high == two mediums.
    assert cluster.mass == dc.Severity.HIGH.mass + dc.Severity.MEDIUM.mass == 6


def test_critical_item_flags_regardless_of_mass():
    recs = [_rec("rare critical ground", dc.Severity.CRITICAL, "c1")]
    [cluster] = digest_mod.cluster_records(recs)
    # threshold deliberately huge so only the critical short-circuit can flag it
    assert digest_mod.is_flagged(cluster, threshold=1000)
    flagged = digest_mod.digest(recs, threshold=1000)
    assert len(flagged) == 1


def test_sub_threshold_cluster_not_flagged():
    recs = [_rec("low-key ground", dc.Severity.LOW, "c1")]
    [cluster] = digest_mod.cluster_records(recs)
    assert not digest_mod.is_flagged(cluster, threshold=8)
    assert digest_mod.digest(recs, threshold=8) == []


def test_distinct_grounds_stay_separate():
    recs = [
        _rec("authentication token expiry handling", dc.Severity.HIGH, "c1"),
        _rec("plan approval gate wording ambiguity", dc.Severity.HIGH, "c2"),
    ]
    clusters = digest_mod.cluster_records(recs)
    assert len(clusters) == 2


def test_threshold_reader_override_and_placeholder_fallback(tmp_path):
    # override wins
    assert digest_mod.read_mass_threshold(override=3) == 3
    # placeholder (non-integer) value in config -> default fallback
    cfg = tmp_path / "config.md"
    cfg.write_text(
        "| `core-difficulty-mass-threshold` | `<calibrated in stage 13>` | mass |\n",
        encoding="utf-8",
    )
    assert digest_mod.read_mass_threshold(config_path=cfg) == digest_mod.DEFAULT_MASS_THRESHOLD
    # a real integer value is read
    cfg.write_text("| `core-difficulty-mass-threshold` | `12` | mass |\n", encoding="utf-8")
    assert digest_mod.read_mass_threshold(config_path=cfg) == 12


def test_pull_all_via_in_memory_double():
    # register a null channel pre-seeded with one record, prove pull_all normalizes it
    ch = dc.NullChannel()
    ch.submit(_rec("seeded ground", dc.Severity.HIGH, "null"))
    dc.register_channel("seeded", lambda: ch)
    recs = digest_mod.pull_all(["seeded"])
    assert len(recs) == 1 and recs[0].functional_ground == "seeded ground"


def test_default_channels_include_startrek_and_github(monkeypatch):
    """When no --channel args are given, both startrek and github are attempted."""
    attempted = []

    def fake_pull_all(channel_names, since=None):
        attempted.extend(channel_names)
        return []

    monkeypatch.setattr(digest_mod, "pull_all", fake_pull_all)
    digest_mod.main([])
    assert "startrek" in attempted
    assert "github" in attempted


# ── Non-built-in channels resolve through the plugin seam (B1) ────────────────

_PLUGIN_ADAPTER_SRC = '''
from difficulty_channel.port import DifficultyChannel, DifficultyRecord, Severity, register_channel


class OverlayChannel(DifficultyChannel):
    """Synthetic test-only adapter — proves the consumer resolution path, no network."""

    def submit(self, record):
        return "overlay-1"

    def pull(self, since=None):
        return [DifficultyRecord(
            ts="2026-06-26T00:00:00", layer="core", target="CLAUDE.md",
            functional_ground="ground from the overlay channel",
            severity=Severity.LOW, reporter="overlay", evidence="e",
        )]


register_channel("overlay", OverlayChannel)
'''


def test_pull_all_skips_a_channel_this_machine_cannot_resolve(monkeypatch, tmp_path, capsys):
    """A non-built-in channel with no plugin installed degrades to a skip — it must not kill the
    run, or every channel listed after it is silently lost."""
    monkeypatch.setenv("CLAUDE_DIFFICULTY_PLUGIN_DIR", str(tmp_path))  # no adapters/ subdir at all
    ch = dc.NullChannel()
    ch.submit(_rec("ground from the installed channel", dc.Severity.LOW, "null"))
    dc.register_channel("installed-here", lambda: ch)

    recs = digest_mod.pull_all(["not-installed-here", "installed-here"])

    assert [r.functional_ground for r in recs] == ["ground from the installed channel"]
    assert "not-installed-here" in capsys.readouterr().err


def test_pull_all_resolves_a_plugin_channel_through_the_port_registry(monkeypatch, tmp_path):
    """The path a machine WITH the overlay installed takes: pull_all loads the plugin, the plugin
    registers itself, and get_channel resolves it — with no static import of the adapter."""
    (tmp_path / "adapters").mkdir()
    (tmp_path / "adapters" / "overlay.py").write_text(_PLUGIN_ADAPTER_SRC, encoding="utf-8")
    monkeypatch.setenv("CLAUDE_DIFFICULTY_PLUGIN_DIR", str(tmp_path))

    recs = digest_mod.pull_all(["overlay"])

    assert [r.functional_ground for r in recs] == ["ground from the overlay channel"]
    assert dc.get_channel("overlay").__class__.__name__ == "OverlayChannel"
