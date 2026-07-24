"""solved_by_007 marker-stamp primitives: adapter add_tag/add_label + the channel dispatcher.

NO live network: every case exercises an injected fake HTTP client (adapters) or an
injected fake add-verb (dispatcher).

A plugin channel's own add_tag behavior is not tested here — an org adapter lives in the
machine-local plugin dir (ADR-0001 B1) and is not statically importable. ``stamp()``'s
issue-key dispatch is covered below via an injected ``plugin_add`` (the same test seam
``stamp()`` exposes for exactly this reason), never the real plugin loader; the configured
channel is likewise stubbed, so these assertions hold on any machine.
"""
# scripts/ is on sys.path via conftest.py, so both packages import normally.
import json

import pytest

from agentctl import solved_marker
from difficulty_channel.adapters import github


# ── GitHub add_label ──────────────────────────────────────────────────────────

def test_github_add_label_emits_post_to_issues_labels_owner_repo_hash_n():
    calls = []

    def fake_http(method, url, headers, body):
        calls.append((method, url, headers, body))
        return {}

    github.add_label("org/repo#7", "solved_by_007", http=fake_http, token="t")
    assert len(calls) == 1
    method, url, headers, body = calls[0]
    assert method == "POST"
    assert url == f"{github.API_BASE}/repos/org/repo/issues/7/labels"
    assert json.loads(body) == ["solved_by_007"]


def test_github_add_label_parses_issues_path_ref():
    calls = []

    def fake_http(method, url, headers, body):
        calls.append(url)
        return {}

    github.add_label("org/repo/issues/9", "solved_by_007", http=fake_http, token="t")
    assert calls[0] == f"{github.API_BASE}/repos/org/repo/issues/9/labels"


def test_github_add_label_parses_full_url_ref():
    calls = []

    def fake_http(method, url, headers, body):
        calls.append(url)
        return {}

    github.add_label(
        "https://github.com/org/repo/issues/9", "solved_by_007", http=fake_http, token="t"
    )
    assert calls[0] == f"{github.API_BASE}/repos/org/repo/issues/9/labels"


def test_github_add_label_explicit_repo_overrides_parsed_repo():
    calls = []

    def fake_http(method, url, headers, body):
        calls.append(url)
        return {}

    github.add_label(
        "org/repo#7", "solved_by_007", repo="other/repo2", http=fake_http, token="t"
    )
    assert calls[0] == f"{github.API_BASE}/repos/other/repo2/issues/7/labels"


@pytest.mark.parametrize("bad_ref", ["7", "#7"])
def test_github_add_label_bare_number_raises_no_default_repo_post(bad_ref):
    calls = []

    def fake_http(method, url, headers, body):
        calls.append(url)
        return {}

    with pytest.raises(ValueError):
        github.add_label(bad_ref, "solved_by_007", http=fake_http, token="t")
    assert calls == []  # never posts to a default/Core repo


# ── key_shape classifier ──────────────────────────────────────────────────────

def test_key_shape_classifies_a_tracker_issue_key():
    assert solved_marker.key_shape("PROJ-445") == "issue-key"


def test_key_shape_classifies_qualified_github_ref():
    assert solved_marker.key_shape("org/repo#7") == "github"
    assert solved_marker.key_shape("org/repo/issues/7") == "github"
    assert solved_marker.key_shape("https://github.com/org/repo/issues/7") == "github"


@pytest.mark.parametrize("bad_ref", ["7", "#7", None, ""])
def test_key_shape_bare_number_or_none_is_unclassifiable(bad_ref):
    assert solved_marker.key_shape(bad_ref) is None


# ── stamp() dispatcher ────────────────────────────────────────────────────────

@pytest.fixture
def configured_channel(monkeypatch):
    """Pin the configured channel so dispatch assertions hold on any machine."""
    def _set(name: str) -> None:
        monkeypatch.setattr(solved_marker, "read_configured_channel", lambda: name)
    return _set


def test_stamp_routes_issue_key_to_the_configured_channel(configured_channel):
    configured_channel("orgchan")
    calls = []
    result = solved_marker.stamp(
        "PROJ-445",
        plugin_add=lambda key, tag, **kw: calls.append((key, tag)),
        github_add=lambda *a, **kw: pytest.fail("must not call github_add"),
    )
    assert calls == [("PROJ-445", solved_marker.SOLVED_MARKER)]
    assert result == {"channel": "orgchan", "key": "PROJ-445", "stamped": True}


def test_stamp_routes_github_ref_to_github_add():
    calls = []
    result = solved_marker.stamp(
        "org/repo#7",
        plugin_add=lambda *a, **kw: pytest.fail("must not call plugin_add"),
        github_add=lambda ref, tag, **kw: calls.append((ref, tag, kw.get("repo"))),
    )
    assert calls == [("org/repo#7", solved_marker.SOLVED_MARKER, None)]
    assert result == {"channel": "github", "key": "org/repo#7", "stamped": True}


def test_stamp_issue_key_on_a_builtin_channel_is_a_fail_open_skip(configured_channel):
    """A machine with no plugin channel cannot stamp a bare PROJ-1: github labels need
    a fully-qualified ref. Reported as a skip, never raised, and never mis-sent to github."""
    configured_channel("github")
    result = solved_marker.stamp(
        "PROJ-1", github_add=lambda *a, **kw: pytest.fail("must not call github_add")
    )
    assert result["stamped"] is False
    assert result["channel"] == "github"
    assert "issue keys" in result["skipped_reason"]


def test_stamp_issue_key_with_no_plugin_installed_is_a_fail_open_skip(
    configured_channel, monkeypatch, tmp_path
):
    """The other fail-open path `stamp` names: a configured NON-built-in channel whose adapter
    plugin is not installed here. `load_adapter` raises FileNotFoundError, which must surface as
    a skip reason — a raise would propagate into an already-completed resolution."""
    configured_channel("orgchan")
    monkeypatch.setenv("CLAUDE_DIFFICULTY_PLUGIN_DIR", str(tmp_path / "no-plugins"))
    result = solved_marker.stamp(
        "PROJ-1", github_add=lambda *a, **kw: pytest.fail("must not call github_add")
    )
    assert result["stamped"] is False
    assert result["channel"] == "orgchan"
    assert "orgchan" in result["skipped_reason"]


def test_stamp_none_key_is_a_fail_open_skip():
    result = solved_marker.stamp(None)
    assert result["stamped"] is False
    assert result["channel"] is None


def test_stamp_bare_github_number_is_a_fail_open_skip_not_raise():
    result = solved_marker.stamp("7")
    assert result["stamped"] is False
    assert result["channel"] is None


def test_stamp_swallows_read_token_runtime_error(configured_channel):
    configured_channel("orgchan")

    def raising_plugin_add(key, tag, **kw):
        raise RuntimeError("no tracker write token")

    result = solved_marker.stamp("PROJ-1", plugin_add=raising_plugin_add)
    assert result == {
        "channel": "orgchan", "key": "PROJ-1", "stamped": False,
        "skipped_reason": "no tracker write token",
    }


def test_stamp_swallows_http_error():
    def raising_github_add(ref, tag, **kw):
        raise OSError("connection refused")

    result = solved_marker.stamp("org/repo#7", github_add=raising_github_add)
    assert result["stamped"] is False
    assert result["channel"] == "github"


def test_stamp_swallows_simulated_422_missing_label():
    class HTTPError(Exception):
        pass

    def raising_github_add(ref, tag, **kw):
        raise HTTPError("422 Unprocessable Entity: label solved_by_007 does not exist")

    result = solved_marker.stamp("org/repo#7", github_add=raising_github_add)
    assert result["stamped"] is False
    assert "422" in result["skipped_reason"]
