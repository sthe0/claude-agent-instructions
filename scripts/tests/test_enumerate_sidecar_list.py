"""Tests for enumerate_sidecar.list_sidecars and cmd_enumerate_sidecar_list.

Covers:
  - list_sidecars with no files returns empty list;
  - list_sidecars returns (session_dir_name, digest) pairs for existing sidecars;
  - list_sidecars with session_id filters to that session only;
  - in-progress temp files (.tmp-*.json) are skipped;
  - cmd_enumerate_sidecar_list: no session returns all sessions' sidecars;
  - cmd_enumerate_sidecar_list: with --session filters and returns node from state;
  - cmd_enumerate_sidecar_list: empty result message varies by whether --session given.
"""
from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, enumerate_sidecar
from agentctl.state import SessionState


def ns(**kw):
    return Namespace(**kw)


# ---------------------------------------------------------------------------
# list_sidecars unit tests
# ---------------------------------------------------------------------------

class TestListSidecars:
    def test_empty_root_returns_empty(self, tmp_path):
        root = tmp_path / "sidecars"
        assert enumerate_sidecar.list_sidecars(root=root) == []

    def test_missing_root_returns_empty(self, tmp_path):
        root = tmp_path / "nonexistent"
        assert enumerate_sidecar.list_sidecars(root=root) == []

    def test_returns_session_and_digest(self, tmp_path):
        root = tmp_path / "sidecars"
        enumerate_sidecar.write("sess-1", "abc123", {"ok": True}, root=root)
        result = enumerate_sidecar.list_sidecars(root=root)
        assert len(result) == 1
        session_dir, digest = result[0]
        assert digest == "abc123"
        assert "sess" in session_dir

    def test_multiple_sessions(self, tmp_path):
        root = tmp_path / "sidecars"
        enumerate_sidecar.write("sess-a", "digest1", {}, root=root)
        enumerate_sidecar.write("sess-b", "digest2", {}, root=root)
        result = enumerate_sidecar.list_sidecars(root=root)
        digests = {d for _, d in result}
        assert digests == {"digest1", "digest2"}

    def test_session_filter_limits_results(self, tmp_path):
        root = tmp_path / "sidecars"
        enumerate_sidecar.write("sess-a", "digest1", {}, root=root)
        enumerate_sidecar.write("sess-b", "digest2", {}, root=root)
        result = enumerate_sidecar.list_sidecars("sess-a", root=root)
        assert len(result) == 1
        _, digest = result[0]
        assert digest == "digest1"

    def test_session_filter_no_match_returns_empty(self, tmp_path):
        root = tmp_path / "sidecars"
        enumerate_sidecar.write("sess-a", "digest1", {}, root=root)
        result = enumerate_sidecar.list_sidecars("other-session", root=root)
        assert result == []

    def test_tmp_files_are_skipped(self, tmp_path):
        root = tmp_path / "sidecars"
        # Write a real sidecar and a temp file alongside it
        enumerate_sidecar.write("sess-1", "real-digest", {}, root=root)
        # Plant a fake .tmp- file
        from agentctl.store import safe_session_id
        session_dir = root / safe_session_id("sess-1")
        (session_dir / ".tmp-partial.json").write_text("{}", encoding="utf-8")
        result = enumerate_sidecar.list_sidecars(root=root)
        digests = [d for _, d in result]
        assert "real-digest" in digests
        assert ".tmp-partial" not in digests
        assert len(result) == 1


# ---------------------------------------------------------------------------
# cmd_enumerate_sidecar_list integration tests
# ---------------------------------------------------------------------------

class TestCmdEnumerateSidecarList:
    def _inject_root(self, monkeypatch, root: Path):
        monkeypatch.setattr(enumerate_sidecar, "DEFAULT_ROOT", root)

    def test_no_sidecars_no_session(self, store, tmp_path, monkeypatch):
        root = tmp_path / "sidecars"
        self._inject_root(monkeypatch, root)
        d = cli.cmd_enumerate_sidecar_list(ns(session=None), store=store)
        assert d.ok is True
        assert d.data["sidecars"] == []
        assert "no enumerate-sidecar files" in d.detail

    def test_no_sidecars_with_session(self, store, tmp_path, monkeypatch):
        root = tmp_path / "sidecars"
        self._inject_root(monkeypatch, root)
        d = cli.cmd_enumerate_sidecar_list(ns(session="my-session"), store=store)
        assert d.ok is True
        assert d.data["sidecars"] == []
        assert "my-session" in d.detail

    def test_lists_all_when_no_session(self, store, tmp_path, monkeypatch):
        root = tmp_path / "sidecars"
        self._inject_root(monkeypatch, root)
        enumerate_sidecar.write("sess-a", "d1", {}, root=root)
        enumerate_sidecar.write("sess-b", "d2", {}, root=root)
        d = cli.cmd_enumerate_sidecar_list(ns(session=None), store=store)
        assert d.ok is True
        assert len(d.data["sidecars"]) == 2
        digests = {s["digest"] for s in d.data["sidecars"]}
        assert digests == {"d1", "d2"}

    def test_filters_by_session(self, store, tmp_path, monkeypatch):
        root = tmp_path / "sidecars"
        self._inject_root(monkeypatch, root)
        enumerate_sidecar.write("sess-a", "d1", {}, root=root)
        enumerate_sidecar.write("sess-b", "d2", {}, root=root)
        d = cli.cmd_enumerate_sidecar_list(ns(session="sess-a"), store=store)
        assert d.ok is True
        assert len(d.data["sidecars"]) == 1
        assert d.data["sidecars"][0]["digest"] == "d1"

    def test_data_root_is_populated(self, store, tmp_path, monkeypatch):
        root = tmp_path / "sidecars"
        self._inject_root(monkeypatch, root)
        d = cli.cmd_enumerate_sidecar_list(ns(session=None), store=store)
        assert "root" in d.data
        assert str(root) == d.data["root"]

    def test_node_from_session_state(self, store, tmp_path, monkeypatch):
        root = tmp_path / "sidecars"
        self._inject_root(monkeypatch, root)
        state = SessionState(session_id="my-session", task_id="t")
        store.save(state)
        d = cli.cmd_enumerate_sidecar_list(ns(session="my-session"), store=store)
        assert d.node == state.node

    def test_node_is_none_when_no_session(self, store, tmp_path, monkeypatch):
        root = tmp_path / "sidecars"
        self._inject_root(monkeypatch, root)
        d = cli.cmd_enumerate_sidecar_list(ns(session=None), store=store)
        assert d.node == "(none)"
