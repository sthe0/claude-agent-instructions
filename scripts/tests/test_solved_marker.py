"""solved_by_007 marker-stamp primitives: adapter add_tag/add_label + the channel dispatcher.

NO live network: every case exercises an injected fake HTTP client (adapters) or an
injected fake add-verb (dispatcher).

The startrek adapter's own add_tag behavior is no longer tested here — it moved out of
Core to the machine-local plugin dir (ADR-0001 B1) and is no longer statically importable.
``stamp()``'s startrek dispatch is still covered below via an injected ``startrek_add`` (the
same test seam ``stamp()`` exposes for exactly this reason), never the real plugin loader.
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


# ── looks_like_key classifier ─────────────────────────────────────────────────

def test_looks_like_key_classifies_startrek_key():
    assert solved_marker.looks_like_key("DEEPAGENT-445") == "startrek"


def test_looks_like_key_classifies_qualified_github_ref():
    assert solved_marker.looks_like_key("org/repo#7") == "github"
    assert solved_marker.looks_like_key("org/repo/issues/7") == "github"
    assert solved_marker.looks_like_key("https://github.com/org/repo/issues/7") == "github"


@pytest.mark.parametrize("bad_ref", ["7", "#7", None, ""])
def test_looks_like_key_bare_number_or_none_is_unclassifiable(bad_ref):
    assert solved_marker.looks_like_key(bad_ref) is None


# ── stamp() dispatcher ────────────────────────────────────────────────────────

def test_stamp_routes_startrek_key_to_startrek_add():
    calls = []
    result = solved_marker.stamp(
        "DEEPAGENT-445",
        startrek_add=lambda key, tag, **kw: calls.append(("startrek", key, tag)),
        github_add=lambda *a, **kw: pytest.fail("must not call github_add"),
    )
    assert calls == [("startrek", "DEEPAGENT-445", solved_marker.SOLVED_MARKER)]
    assert result == {"channel": "startrek", "key": "DEEPAGENT-445", "stamped": True}


def test_stamp_routes_github_ref_to_github_add():
    calls = []
    result = solved_marker.stamp(
        "org/repo#7",
        startrek_add=lambda *a, **kw: pytest.fail("must not call startrek_add"),
        github_add=lambda ref, tag, **kw: calls.append((ref, tag, kw.get("repo"))),
    )
    assert calls == [("org/repo#7", solved_marker.SOLVED_MARKER, None)]
    assert result == {"channel": "github", "key": "org/repo#7", "stamped": True}


def test_stamp_none_key_is_a_fail_open_skip():
    result = solved_marker.stamp(None)
    assert result["stamped"] is False
    assert result["channel"] is None


def test_stamp_bare_github_number_is_a_fail_open_skip_not_raise():
    result = solved_marker.stamp("7")
    assert result["stamped"] is False
    assert result["channel"] is None


def test_stamp_swallows_read_token_runtime_error():
    def raising_startrek_add(key, tag, **kw):
        raise RuntimeError("no tracker write token")

    result = solved_marker.stamp("DEEPAGENT-1", startrek_add=raising_startrek_add)
    assert result == {
        "channel": "startrek", "key": "DEEPAGENT-1", "stamped": False,
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
