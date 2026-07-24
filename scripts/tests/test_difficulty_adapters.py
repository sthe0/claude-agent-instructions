"""GitHub difficulty-channel adapter (ADR-0001 S3 stage 8).

NO live network: the GitHub adapter's pure record->fields mapping is asserted directly, and its
submit()/pull() are exercised through an injected fake HTTP client.

The org-specific adapter (formerly ``startrek``) is no longer resident in Core — it attaches
through the machine-local plugin seam instead (see ``test_difficulty_channel.py``'s
plugin-loading tests: plugin-present / plugin-absent / no-plugin-dir default).
"""
# scripts/ is on sys.path via conftest.py, so the package imports normally.
import json

import pytest

import difficulty_channel as dc
from difficulty_channel.adapters import external, github


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


# ── GitHub adapter ────────────────────────────────────────────────────────────

def test_github_pure_mapping():
    fields = github.record_to_fields(_rec())
    assert fields["title"] == "[core] gate denies a legitimate memory write"
    assert "severity:high" in fields["labels"]
    assert "layer:core" in fields["labels"]
    assert github.DIFFICULTY_LABEL in fields["labels"]
    body = fields["body"]
    assert "**Target:** `CLAUDE.md`" in body
    assert "**Functional ground:** gate denies a legitimate memory write" in body
    assert "**Reporter:** agent" in body
    assert "**Observed:** 2026-06-26T00:00:00" in body
    assert "session quote" in body


def test_github_submit_uses_injected_http_no_network():
    calls = []

    def fake_http(method, url, headers, body):
        calls.append((method, url, headers, body))
        assert headers["Authorization"].startswith("token ")
        return {"html_url": "https://github.com/sthe0/claude-agent-instructions/issues/7"}

    ch = github.GitHubChannel(http=fake_http, token="fake-ghp-token")
    url = ch.submit(_rec())
    assert url == "https://github.com/sthe0/claude-agent-instructions/issues/7"
    assert len(calls) == 1
    method, call_url, _, _ = calls[0]
    assert method == "POST"
    assert f"/repos/{github.REPO}/issues" in call_url


def test_github_pull_round_trips_through_fake_http():
    def fake_http(method, url, headers, body):
        assert method == "GET"
        assert "labels=difficulty" in url
        return [{
            "body": (
                "**Target:** `CLAUDE.md`\n"
                "**Layer:** core\n"
                "**Functional ground:** gate denies a legitimate memory write\n"
                "**Severity:** high\n"
                "**Reporter:** agent\n"
                "**Observed:** 2026-06-26T00:00:00\n\n"
                "**Evidence:**\nsession quote"
            ),
            "labels": [
                {"name": "severity:high"},
                {"name": "layer:core"},
                {"name": "difficulty"},
            ],
            "title": "[core] gate denies a legitimate memory write",
            "created_at": "2026-06-26T00:00:00Z",
            "user": {"login": "agent"},
        }]

    ch = github.GitHubChannel(http=fake_http, token="t")
    recs = ch.pull(since="2026-06-01T00:00:00")
    assert len(recs) == 1
    r = recs[0]
    assert r.functional_ground == "gate denies a legitimate memory write"
    assert r.severity is dc.Severity.HIGH
    assert r.layer == "core"
    assert r.target == "CLAUDE.md"
    assert r.reporter == "agent"
    assert r.evidence == "session quote"


def test_github_pull_filters_old_records():
    """Records whose observation ts predates `since` are dropped (even if GitHub returned them)."""
    def fake_http(method, url, headers, body):
        return [{
            "body": (
                "**Target:** `x`\n"
                "**Layer:** core\n"
                "**Functional ground:** old ground\n"
                "**Severity:** low\n"
                "**Reporter:** bot\n"
                "**Observed:** 2025-01-01T00:00:00\n\n"
                "**Evidence:**\n"
            ),
            "labels": [{"name": "severity:low"}, {"name": "layer:core"}, {"name": "difficulty"}],
            "title": "[core] old ground",
            "created_at": "2025-01-01T00:00:00Z",
            "user": {"login": "bot"},
        }]

    ch = github.GitHubChannel(http=fake_http, token="t")
    recs = ch.pull(since="2026-06-01T00:00:00")
    assert recs == []


def test_github_registered_in_port_registry():
    assert isinstance(dc.get_channel("github"), github.GitHubChannel)


def test_external_is_back_compat_alias_for_github():
    """'external' channel key still resolves; ExternalChannel is GitHubChannel."""
    assert isinstance(dc.get_channel("external"), github.GitHubChannel)
    assert external.ExternalChannel is github.GitHubChannel


# ── GitHub backlog stream ─────────────────────────────────────────────────────

def test_github_backlog_stream_includes_backlog_label():
    fields = github.record_to_fields(_rec(), stream="backlog")
    assert github.BACKLOG_LABEL in fields["labels"]
    assert github.DIFFICULTY_LABEL not in fields["labels"]


def test_github_report_stream_includes_difficulty_label():
    fields = github.record_to_fields(_rec(), stream="report")
    assert github.DIFFICULTY_LABEL in fields["labels"]
    assert github.BACKLOG_LABEL not in fields["labels"]


def test_github_record_to_fields_default_stream_is_report():
    fields = github.record_to_fields(_rec())
    assert github.DIFFICULTY_LABEL in fields["labels"]
    assert github.BACKLOG_LABEL not in fields["labels"]


def test_github_pull_always_filters_by_difficulty_label():
    """pull() always queries labels=difficulty regardless of channel stream (digest read path)."""
    urls = []

    def fake_http(method, url, headers, body):
        urls.append(url)
        return []

    ch_report = github.GitHubChannel(http=fake_http, token="t", stream="report")
    ch_report.pull()
    assert f"labels={github.DIFFICULTY_LABEL}" in urls[-1]

    ch_backlog = github.GitHubChannel(http=fake_http, token="t", stream="backlog")
    ch_backlog.pull()
    assert f"labels={github.DIFFICULTY_LABEL}" in urls[-1]


def test_github_channel_stream_param_forwarded_in_submit():
    calls = []

    def fake_http(method, url, headers, body):
        calls.append(json.loads(body))
        return {"html_url": "https://github.com/sthe0/claude-agent-instructions/issues/99"}

    ch = github.GitHubChannel(http=fake_http, token="t", stream="backlog")
    ch.submit(_rec())
    assert github.BACKLOG_LABEL in calls[0]["labels"]
    assert github.DIFFICULTY_LABEL not in calls[0]["labels"]
