"""usage-digest.py `emit`: opt-in-gated, anonymized, counts-only cross-installation telemetry.

NO live network: the adapter verbs and emit() are exercised through injected fakes. The
load-bearing invariants under test are the opt-in default OFF, the counts-only + anonymized
envelope, the disjoint ISO-week period, and fail-open.

A plugin channel's own add_comment/list_comments behavior is not tested here — an org adapter
lives in the machine-local plugin dir (ADR-0001 B1) and is not statically importable.
``emit()``'s plugin dispatch is covered below via an injected ``plugin_add_comment`` (the same
test seam ``emit()`` exposes for exactly this reason), never the real plugin loader, so these
assertions hold on any machine.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS_DIR / "usage-digest.py"
_spec = importlib.util.spec_from_file_location("usage_digest", SCRIPT)
usage_digest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(usage_digest)

from difficulty_channel.adapters import BUILTIN_NAMES, github  # noqa: E402


# ── adapter verbs: add_comment / list_comments (github) ───────────────────────

def test_github_add_comment_posts_to_issues_comments_path():
    calls = []

    def fake_http(method, url, headers, body):
        calls.append((method, url, headers, body))
        return {}

    github.add_comment("org/repo#7", "hello", http=fake_http, token="t")
    assert len(calls) == 1
    method, url, headers, body = calls[0]
    assert method == "POST"
    assert url == "https://api.github.com/repos/org/repo/issues/7/comments"
    assert headers["Authorization"].startswith("token ")
    assert json.loads(body) == {"body": "hello"}


def test_github_add_comment_rejects_bare_number():
    with pytest.raises(ValueError):
        github.add_comment("7", "hi", http=lambda *a: {}, token="t")


def test_github_list_comments_gets_issues_comments_path():
    calls = []

    def fake_http(method, url, headers, body):
        calls.append((method, url))
        return [{"body": "a"}, {"body": "b"}]

    out = github.list_comments("org/repo#7", http=fake_http, token="t")
    assert out == [{"body": "a"}, {"body": "b"}]
    method, url = calls[0]
    assert method == "GET"
    assert "/repos/org/repo/issues/7/comments" in url


# ── ISO-week disjoint buckets + anonymization ─────────────────────────────────

def test_period_is_an_iso_week_string_not_a_date_range():
    import datetime as dt
    period = usage_digest.just_closed_week(dt.datetime(2026, 7, 13, tzinfo=dt.timezone.utc))
    assert period.count("-W") == 1
    year, week = period.split("-W")
    assert year.isdigit() and week.isdigit() and len(week) == 2
    start, end = usage_digest.week_bounds(period)
    assert (end - start).days == 7  # a disjoint 7-day bucket, not a rolling window


def test_installation_id_is_anonymized_no_raw_hostname():
    iid = usage_digest.installation_id("myhost.example.com", "saltsalt")
    assert "myhost" not in iid
    assert len(iid) == 16
    # stable across calls with the same (machine, salt) — the dedup key holds
    assert iid == usage_digest.installation_id("myhost.example.com", "saltsalt")
    # salt-sensitive: a different salt yields a different id
    assert iid != usage_digest.installation_id("myhost.example.com", "other")


# ── payload envelope: counts-only whitelist ───────────────────────────────────

def _rows():
    task_rows = [
        {"ts": "2026-07-06T00:00:00", "session": "s1", "quality": 4, "tracker_key": "PROJ-1"},
        {"ts": "2026-07-07T00:00:00", "session": "s2", "quality": 2, "tracker_key": None},
    ]
    policy_rows = [{"ts": "2026-07-06T00:00:00", "project": "p"}]
    spawn_rows = [{"ts": "2026-07-06T00:00:00", "cost_usd": 0.5}]
    return task_rows, policy_rows, spawn_rows


def test_build_payload_is_counts_only_with_quality_weight():
    task_rows, policy_rows, spawn_rows = _rows()
    payload = usage_digest.build_payload(
        task_rows, policy_rows, spawn_rows,
        period="2026-W28", channel="github", installation_id="abc123",
    )
    # only whitelisted fields, and n_quality_rated is carried for weighting
    assert set(payload) <= usage_digest.WHITELIST
    assert payload["n_quality_rated"] == 2
    assert payload["n_marked_precedents"] == 1
    assert payload["period"] == "2026-W28"
    # nothing identifying leaked
    for forbidden in ("task_id", "tracker_key", "session", "path", "cwd"):
        assert forbidden not in payload


def test_assert_counts_only_rejects_a_task_id_field():
    with pytest.raises(ValueError):
        usage_digest.assert_counts_only(
            {"schema": "usage/v1", "period": "2026-W28", "task_id": "PROJ-1"}
        )


# ── emit: opt-in gate ─────────────────────────────────────────────────────────

def _capture_add_comment():
    calls = []

    def fake(sink, body, *, http=None):
        calls.append((sink, body))

    return calls, fake


def test_emit_optin_off_calls_no_adapter():
    task_rows, policy_rows, spawn_rows = _rows()
    calls, fake = _capture_add_comment()
    result = usage_digest.emit(
        identity={},  # usage_telemetry unset == OFF
        channel="github", period="2026-W28", installation_id="abc",
        task_rows=task_rows, policy_rows=policy_rows, spawn_rows=spawn_rows,
        github_add_comment=fake, log=lambda m: None,
    )
    assert result["emitted"] is False
    assert result["reason"] == "opt-in-off"
    assert calls == []  # mutation: defaulting opt-in ON turns this RED


def test_emit_optin_on_posts_one_counts_only_anonymized_comment():
    task_rows, policy_rows, spawn_rows = _rows()
    calls, fake = _capture_add_comment()
    result = usage_digest.emit(
        identity={"usage_telemetry": "on"},
        channel="github", period="2026-W28", installation_id="deadbeefcafe0000",
        task_rows=task_rows, policy_rows=policy_rows, spawn_rows=spawn_rows,
        sink="org/repo#1", github_add_comment=fake, log=lambda m: None,
    )
    assert result["emitted"] is True
    assert len(calls) == 1
    sink, body = calls[0]
    assert sink == "org/repo#1"
    # the comment body carries a fenced JSON aggregate — extract and inspect it
    assert usage_digest.AGGREGATE_MARKER in body
    payload = json.loads(body.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert set(payload) <= usage_digest.WHITELIST
    assert payload["n_quality_rated"] == 2
    assert payload["installation_id"] == "deadbeefcafe0000"
    assert payload["period"] == "2026-W28"  # ISO-week, not a rolling window
    # no raw machine identity anywhere in the emitted bytes
    import socket
    assert socket.gethostname() not in body


def test_emit_routes_a_non_builtin_channel_to_the_plugin_adapter():
    task_rows, policy_rows, spawn_rows = _rows()
    pl_calls, pl_fake = _capture_add_comment()
    gh_calls, gh_fake = _capture_add_comment()
    usage_digest.emit(
        identity={"usage_telemetry": "on"},
        channel="orgchan", period="2026-W28", installation_id="abc",
        task_rows=task_rows, policy_rows=policy_rows, spawn_rows=spawn_rows,
        sink="QUEUE-9", plugin_add_comment=pl_fake, github_add_comment=gh_fake,
        log=lambda m: None,
    )
    assert len(pl_calls) == 1 and pl_calls[0][0] == "QUEUE-9"
    assert gh_calls == []


# ── sink resolution: override > machine identity > built-in default ───────────

def test_resolve_sink_prefers_an_explicit_override():
    identity = {"usage_sink_orgchan": "QUEUE-9"}
    assert usage_digest.resolve_sink("orgchan", identity, "OTHER-1") == "OTHER-1"


def test_resolve_sink_reads_the_machine_identity_key():
    identity = {"usage_sink_orgchan": "QUEUE-9"}
    assert usage_digest.resolve_sink("orgchan", identity) == "QUEUE-9"


def test_resolve_sink_of_the_builtin_channel_is_unprovisioned_by_default():
    """Core ships NO sink ref at all — not even for github. A non-empty in-code default
    would post an installation's telemetry to a repo nobody on that machine chose."""
    assert usage_digest.USAGE_SINK_GITHUB == ""
    assert usage_digest.resolve_sink("github", {}) == ""


def test_resolve_sink_of_an_unconfigured_channel_is_empty():
    """Core ships no org sink: a channel nobody configured resolves to '' (a skip), never
    to some other channel's ticket."""
    assert usage_digest.resolve_sink("orgchan", {}) == ""


def test_emit_resolves_the_sink_from_the_machine_identity():
    task_rows, policy_rows, spawn_rows = _rows()
    pl_calls, pl_fake = _capture_add_comment()
    result = usage_digest.emit(
        identity={"usage_telemetry": "on", "usage_sink_orgchan": "QUEUE-9"},
        channel="orgchan", period="2026-W28", installation_id="abc",
        task_rows=task_rows, policy_rows=policy_rows, spawn_rows=spawn_rows,
        plugin_add_comment=pl_fake, log=lambda m: None,
    )
    assert result["emitted"] is True
    assert result["sink"] == "QUEUE-9"
    assert [c[0] for c in pl_calls] == ["QUEUE-9"]


# ── the seam is BUILTIN_NAMES membership, never one built-in's literal name ───
#
# Comparing against "github" alone drops the other built-in into the plugin branch, where
# load_adapter() returns None for a built-in and every resulting AttributeError is swallowed
# by the fail-open handlers — a silent wrong-path, not a visible failure. Each test below
# turns RED if its site is mutated back to a literal comparison.

@pytest.mark.parametrize("channel", sorted(BUILTIN_NAMES))
def test_resolve_sink_of_every_builtin_falls_back_to_the_builtin_default(channel, monkeypatch):
    monkeypatch.setattr(usage_digest, "USAGE_SINK_GITHUB", "org/repo#1")
    assert usage_digest.resolve_sink(channel, {}) == "org/repo#1"
    # built-ins only: an unconfigured org channel still resolves to a skip
    assert usage_digest.resolve_sink("orgchan", {}) == ""


@pytest.mark.parametrize("channel", sorted(BUILTIN_NAMES))
def test_emit_routes_every_builtin_channel_to_the_builtin_adapter(channel):
    task_rows, policy_rows, spawn_rows = _rows()
    gh_calls, gh_fake = _capture_add_comment()
    result = usage_digest.emit(
        identity={"usage_telemetry": "on"},
        channel=channel, period="2026-W28", installation_id="abc",
        task_rows=task_rows, policy_rows=policy_rows, spawn_rows=spawn_rows,
        sink="org/repo#1", github_add_comment=gh_fake,
        plugin_add_comment=lambda *a, **kw: pytest.fail("must not take the plugin path"),
        log=lambda m: None,
    )
    assert result["emitted"] is True
    assert [c[0] for c in gh_calls] == ["org/repo#1"]


# ── rollup segmentation: public built-ins vs. per-org segments ────────────────

@pytest.mark.parametrize("channel", sorted(BUILTIN_NAMES))
def test_channel_segment_folds_every_builtin_into_the_public_segment(channel):
    assert usage_digest.channel_segment(channel) == usage_digest.PUBLIC_SEGMENT


def test_channel_segment_keeps_public_and_org_installations_apart():
    assert usage_digest.channel_segment("github") == usage_digest.PUBLIC_SEGMENT
    assert usage_digest.channel_segment("orgchan") == "orgchan"
    assert usage_digest.channel_segment(None) == "unknown"


# ── emit: fail-open ───────────────────────────────────────────────────────────

def test_emit_failopen_on_raising_adapter():
    task_rows, policy_rows, spawn_rows = _rows()

    def raising(sink, body, *, http=None):
        raise RuntimeError("http exploded")

    result = usage_digest.emit(
        identity={"usage_telemetry": "on"},
        channel="github", period="2026-W28", installation_id="abc",
        task_rows=task_rows, policy_rows=policy_rows, spawn_rows=spawn_rows,
        sink="org/repo#1", github_add_comment=raising, log=lambda m: None,
    )
    assert result["emitted"] is False
    assert result["reason"].startswith("error:")


def test_emit_with_no_sink_configured_skips():
    """An identity with no `usage_sink_<channel>` key and no override: nothing is posted."""
    task_rows, policy_rows, spawn_rows = _rows()
    pl_calls, pl_fake = _capture_add_comment()
    result = usage_digest.emit(
        identity={"usage_telemetry": "on"},
        channel="orgchan", period="2026-W28", installation_id="abc",
        task_rows=task_rows, policy_rows=policy_rows, spawn_rows=spawn_rows,
        plugin_add_comment=pl_fake, log=lambda m: None,
    )
    assert result["emitted"] is False
    assert result["reason"] == "no-sink"
    assert pl_calls == []


# ── CLI: opt-in OFF exits 0 and posts nothing (offline) ───────────────────────

def test_cli_emit_optin_off_exits_zero(tmp_path, capsys):
    identity = tmp_path / "agent-identity.local"
    identity.write_text("difficulty_channel=github\n", encoding="utf-8")  # no usage_telemetry
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    rc = usage_digest.main([
        "emit", "--identity", str(identity), "--channel", "github", "--period", "2026-W28",
        "--task-log", str(empty), "--policy-log", str(empty), "--spawn-log", str(empty),
    ])
    assert rc == 0
    assert "opt-in OFF" in capsys.readouterr().out


# ── window: ISO-week filtering is two-sided and fail-soft ─────────────────────

def test_window_rows_is_two_sided_and_skips_bad_ts():
    import datetime as dt
    start = dt.datetime(2026, 7, 6)   # Mon of ISO-week 2026-W28
    end = dt.datetime(2026, 7, 13)
    rows = [
        {"ts": "2026-07-06T00:00:00"},        # in
        {"ts": "2026-07-12T23:59:59"},        # in
        {"ts": "2026-07-13T00:00:00"},        # out (half-open upper bound)
        {"ts": "2026-07-05T00:00:00"},        # out (before start)
        {"ts": "not-a-date"},                 # skipped, not fatal
        {"nots": 1},                          # skipped
    ]
    kept = usage_digest.window_rows(rows, start, end)
    assert [r["ts"] for r in kept] == ["2026-07-06T00:00:00", "2026-07-12T23:59:59"]
