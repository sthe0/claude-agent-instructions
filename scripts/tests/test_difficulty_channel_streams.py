"""DifficultyChannel.pull_stream — the optional, additive stream-selection capability.

An adapter's default pull_stream() delegates to pull() for "report" (every adapter already
supports it) and raises StreamUnsupported for anything else, never returning an empty list —
an empty result would read to a multi-channel caller as "this source has no items," silently
under-covering a board built from pull_stream(). GitHubChannel overrides pull_stream() to
select the backlog label via the same URL builder pull() uses, with no live network (injected
fake HTTP client). This module also pins that pull()'s signature and behaviour are byte-identical
to before this change.
"""
# scripts/ is on sys.path via conftest.py, so the package imports normally.
import inspect

import pytest

import difficulty_channel as dc
from difficulty_channel.adapters import github
from difficulty_channel.port import DifficultyChannel, StreamUnsupported


def _rec(ts="2026-06-26T00:00:00"):
    return dc.DifficultyRecord(
        ts=ts,
        layer="core",
        target="CLAUDE.md",
        functional_ground="backlog item not visible on the report stream",
        severity=dc.Severity.MEDIUM,
        reporter="agent",
        evidence="quote",
    )


# ── Default implementation on the ABC ──────────────────────────────────────────

def test_default_pull_stream_delegates_to_pull_for_report():
    ch = dc.NullChannel()
    ch.submit(_rec())
    assert ch.pull_stream("report") == ch.pull()


def test_default_pull_stream_raises_for_unsupported_stream():
    class BareChannel(DifficultyChannel):
        """An adapter that has not opted into pull_stream at all — only pull()/submit()."""

        def __init__(self):
            self._store = []

        def submit(self, record):
            self._store.append(record)
            return "bare-1"

        def pull(self, since=None):
            return list(self._store)

    ch = BareChannel()
    ch.submit(_rec())
    assert ch.pull_stream("report") == ch.pull()
    with pytest.raises(StreamUnsupported):
        ch.pull_stream("backlog")


# ── NullChannel stream-aware double ─────────────────────────────────────────────

def test_null_channel_pull_stream_round_trips_backlog():
    ch = dc.NullChannel()
    ch.submit_to_stream(_rec(ts="2026-06-01T00:00:00"), stream="backlog")
    ch.submit_to_stream(_rec(ts="2026-06-20T00:00:00"), stream="backlog")
    assert ch.pull_stream("report") == []  # backlog items don't leak onto report
    backlog = ch.pull_stream("backlog")
    assert len(backlog) == 2
    recent = ch.pull_stream("backlog", since="2026-06-10T00:00:00")
    assert len(recent) == 1 and recent[0].ts == "2026-06-20T00:00:00"


def test_null_channel_report_stream_unaffected_by_backlog_seed():
    ch = dc.NullChannel()
    ch.submit(_rec())  # goes to "report" via the existing submit() path
    ch.submit_to_stream(_rec(), stream="backlog")
    assert len(ch.pull()) == 1
    assert len(ch.pull_stream("report")) == 1
    assert len(ch.pull_stream("backlog")) == 1


# ── GitHubChannel.pull_stream (fake HTTP, no network) ───────────────────────────

def test_github_pull_stream_report_uses_difficulty_label():
    urls = []

    def fake_http(method, url, headers, body):
        urls.append(url)
        return []

    ch = github.GitHubChannel(http=fake_http, token="t")
    ch.pull_stream("report")
    assert f"labels={github.DIFFICULTY_LABEL}" in urls[-1]


def test_github_pull_stream_backlog_uses_backlog_label():
    urls = []

    def fake_http(method, url, headers, body):
        urls.append(url)
        return []

    ch = github.GitHubChannel(http=fake_http, token="t")
    ch.pull_stream("backlog")
    assert f"labels={github.BACKLOG_LABEL}" in urls[-1]


def test_github_pull_stream_unknown_stream_raises():
    ch = github.GitHubChannel(http=lambda *a: [], token="t")
    with pytest.raises(StreamUnsupported):
        ch.pull_stream("no-such-stream")


def test_github_pull_stream_backlog_round_trips_record():
    def fake_http(method, url, headers, body):
        assert f"labels={github.BACKLOG_LABEL}" in url
        return [{
            "body": (
                "**Target:** `CLAUDE.md`\n"
                "**Layer:** core\n"
                "**Functional ground:** backlog item not visible on the report stream\n"
                "**Severity:** medium\n"
                "**Reporter:** agent\n"
                "**Observed:** 2026-06-26T00:00:00\n\n"
                "**Evidence:**\nquote"
            ),
            "labels": [
                {"name": "severity:medium"},
                {"name": "layer:core"},
                {"name": github.BACKLOG_LABEL},
            ],
            "title": "[core] backlog item not visible on the report stream",
            "created_at": "2026-06-26T00:00:00Z",
            "user": {"login": "agent"},
        }]

    ch = github.GitHubChannel(http=fake_http, token="t")
    [r] = ch.pull_stream("backlog")
    assert r.functional_ground == "backlog item not visible on the report stream"


# ── Regression pin: pull()'s signature and behaviour are unchanged ─────────────

def test_pull_signature_unchanged():
    assert list(inspect.signature(DifficultyChannel.pull).parameters) == ["self", "since"]


def test_pull_stream_signature_has_stream_param():
    params = inspect.signature(DifficultyChannel.pull_stream).parameters
    assert "stream" in params
    assert "since" in params


def test_github_pull_produces_exact_pre_change_url_and_records():
    """pull() alone (never pull_stream) must be byte-identical to the pre-change adapter:
    same URL (labels=difficulty, no stream leakage) and the same record mapping."""
    urls = []

    def fake_http(method, url, headers, body):
        urls.append(url)
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
    assert urls == [
        f"https://api.github.com/repos/{github.REPO}/issues"
        f"?labels={github.DIFFICULTY_LABEL}&state=open&per_page=100"
        f"&since=2026-06-01T00:00:00"
    ]
    assert len(recs) == 1
    assert recs[0].functional_ground == "gate denies a legitimate memory write"
