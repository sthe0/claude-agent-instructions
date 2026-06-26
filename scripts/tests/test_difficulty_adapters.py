"""Startrek + External difficulty-channel adapters (ADR-0001 S3 stage 8).

NO live network: the Startrek adapter's pure record->fields mapping is asserted directly, and
its submit()/pull() are exercised through an injected fake HTTP client. The external adapter is
a stub advertising the same record contract.
"""
# scripts/ is on sys.path via conftest.py, so the package imports normally.
import pytest

import difficulty_channel as dc
from difficulty_channel.adapters import external, startrek


def _rec():
    return dc.DifficultyRecord(
        ts="2026-06-26T00:00:00",
        layer="core",
        target="CLAUDE.md",
        functional_ground="gate denies a legitimate memory write",
        severity=dc.Severity.HIGH,
        reporter="agent",
        evidence="session quote",
    )


def test_startrek_pure_mapping_targets_core_instr():
    fields = startrek.record_to_fields(_rec())
    assert fields["queue"] == "CORE-INSTR"
    assert fields["priority"] == {"key": "major"}  # HIGH -> major
    assert "gate denies a legitimate memory write" in fields["tags"]
    assert "gate denies a legitimate memory write" in fields["summary"]
    assert "CLAUDE.md" in fields["description"]


def test_startrek_submit_uses_injected_http_no_network():
    calls = []

    def fake_http(method, url, headers, body):
        calls.append((method, url, headers, body))
        assert "Authorization" in headers and headers["Authorization"].startswith("OAuth ")
        return {"key": "CORE-INSTR-42"}

    ch = startrek.StartrekChannel(http=fake_http, token="fake-token")
    key = ch.submit(_rec())
    assert key == "CORE-INSTR-42"
    assert len(calls) == 1
    method, url, _, _ = calls[0]
    assert method == "POST" and url.endswith("/issues")


def test_startrek_pull_round_trips_through_fake_http():
    def fake_http(method, url, headers, body):
        return [{
            "createdAt": "2026-06-26T00:00:00",
            "summary": "[core] some ground",
            "tags": ["some ground"],
            "priority": {"key": "critical"},
            "createdBy": {"id": "user1"},
            "description": "evidence body",
        }]

    ch = startrek.StartrekChannel(http=fake_http, token="t")
    recs = ch.pull(since="2026-06-01T00:00:00")
    assert len(recs) == 1
    assert recs[0].functional_ground == "some ground"
    assert recs[0].severity is dc.Severity.CRITICAL


def test_startrek_registered_in_port_registry():
    assert isinstance(dc.get_channel("startrek"), startrek.StartrekChannel)


def test_external_stub_advertises_same_contract_and_raises():
    ch = external.ExternalChannel()
    assert ch.record_contract is dc.DifficultyRecord
    assert "functional_ground" in external.RECORD_FIELD_MAPPING
    with pytest.raises(NotImplementedError):
        ch.submit(_rec())
    with pytest.raises(NotImplementedError):
        ch.pull()
    assert isinstance(dc.get_channel("external"), external.ExternalChannel)
