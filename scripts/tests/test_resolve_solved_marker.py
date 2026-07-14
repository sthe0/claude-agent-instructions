"""Wiring of the solved_by_007 marker into cmd_resolve, plus the tracker_key join
field on the quality ledger row.

Covers: classify storing a fully-qualified github ref into state.tracker_key
(the Startrek-shaped-key branch is already covered by test_tracker_plugin.py);
resolve threading that key into both the quality row and solved_marker.stamp,
fail-open when stamp raises, and the no-key case stamping nothing.
"""
from __future__ import annotations

import json
from argparse import Namespace

from agentctl import cli, solved_marker
from agentctl.state import Node


def ns(**kw):
    return Namespace(**kw)


def _read_quality_rows(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _classify(store, sid, *, tracker_key=None):
    cli.cmd_start(ns(session=sid, task="demo", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    return cli.cmd_classify(ns(
        session=sid, chat=False, changed_lines=200, files=5, wall_clock_min=60,
        tracker_key=tracker_key, architectural=False, external_effect=False,
        new_dependency=False, public_api_change=False,
    ), store=store)


def _drive_to_resolved(store, sid, plan, *, tracker_key=None):
    """Start -> classify -> ... -> resolve, returning the resolve Directive."""
    _classify(store, sid, tracker_key=tracker_key)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    for _ in range(2):
        cli.cmd_next_stage(ns(session=sid), store=store)
        cli.cmd_record_result(ns(session=sid, status="passed", actual="ok",
                                 control="reviewed: ok", observation=""), store=store)
    cli.cmd_verify_final(ns(session=sid), store=store)
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="searched",
                             note=""), store=store)
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="skipped",
                             note="test fixture, nothing to record"), store=store)
    if tracker_key is not None:
        # classify auto-activates the tracker plugin whenever tracker_key is set,
        # which gates resolve until plan/result/status are published — discharge it
        # with the honest skip form (no real tracker to post to in this fixture).
        for phase in ("plan", "result", "status"):
            cli.cmd_plugin_record(ns(session=sid, plugin="tracker", phase=phase,
                                     skipped=True, note="test fixture, nothing to publish"),
                                   store=store)
    return cli.cmd_resolve(ns(session=sid, by="user", quality=5, quality_by="user-confirmed",
                              quality_note=None), store=store)


# --- classify: a github ref is stored into state.tracker_key ------------------

def test_classify_stores_github_ref_into_tracker_key(store):
    _classify(store, "gh-1", tracker_key="org/repo#7")
    state = store.load("gh-1")
    assert state.tracker_key == "org/repo#7"


def test_classify_ignores_bare_number(store):
    _classify(store, "gh-2", tracker_key="7")
    state = store.load("gh-2")
    assert state.tracker_key is None


# --- resolve: tracker_key threads into the quality row + the stamp ------------

def test_resolve_with_startrek_key_writes_row_and_invokes_stamp(store, monkeypatch, fixtures_dir, tmp_path):
    calls = []
    monkeypatch.setattr(solved_marker, "stamp", lambda key, **kw: calls.append(key) or
                         {"channel": "startrek", "key": key, "stamped": True})
    plan = str(fixtures_dir / "plan_two_stage.toml")

    d = _drive_to_resolved(store, "res-startrek", plan, tracker_key="DEEPAGENT-1")

    assert d.ok is True
    assert d.marker == "COMPLETED"
    assert calls == ["DEEPAGENT-1"]
    assert d.data["solved_marker"]["stamped"] is True
    assert d.data["quality"]["tracker_key"] == "DEEPAGENT-1"
    rows = _read_quality_rows(cli.TASK_QUALITY_LOG)
    assert rows[-1]["tracker_key"] == "DEEPAGENT-1"


def test_resolve_with_github_ref_invokes_stamp_with_that_ref(store, monkeypatch, fixtures_dir):
    calls = []
    monkeypatch.setattr(solved_marker, "stamp", lambda key, **kw: calls.append(key) or
                         {"channel": "github", "key": key, "stamped": True})
    plan = str(fixtures_dir / "plan_two_stage.toml")

    d = _drive_to_resolved(store, "res-github", plan, tracker_key="org/repo#7")

    assert d.ok is True
    assert calls == ["org/repo#7"]
    assert d.data["solved_marker"]["channel"] == "github"
    assert d.data["quality"]["tracker_key"] == "org/repo#7"


def test_resolve_stamp_raising_still_completes_and_still_wrote_row(store, monkeypatch, fixtures_dir):
    def _raise(key, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(solved_marker, "stamp", _raise)
    plan = str(fixtures_dir / "plan_two_stage.toml")

    d = _drive_to_resolved(store, "res-raise", plan, tracker_key="DEEPAGENT-2")

    assert d.ok is True
    assert d.node == Node.RESOLVED.value
    assert d.marker == "COMPLETED"
    assert d.data["solved_marker"]["stamped"] is False
    rows = _read_quality_rows(cli.TASK_QUALITY_LOG)
    assert rows[-1]["tracker_key"] == "DEEPAGENT-2"
    assert rows[-1]["quality"] == 5


def test_resolve_without_tracker_key_writes_null_and_stamps_nothing(store, monkeypatch, fixtures_dir):
    calls = []
    monkeypatch.setattr(solved_marker, "stamp", lambda key, **kw: calls.append(key) or
                         {"channel": None, "key": key, "stamped": False,
                          "skipped_reason": "no key or unclassifiable key"})
    plan = str(fixtures_dir / "plan_two_stage.toml")

    d = _drive_to_resolved(store, "res-none", plan, tracker_key=None)

    assert d.ok is True
    assert calls == [None]
    assert d.data["solved_marker"]["stamped"] is False
    assert d.data["quality"]["tracker_key"] is None
    rows = _read_quality_rows(cli.TASK_QUALITY_LOG)
    assert rows[-1]["tracker_key"] is None
