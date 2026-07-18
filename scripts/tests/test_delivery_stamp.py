"""Tests for agentctl/delivery.py — the sidecar stamp module — and for the
present-plan advisory nudge cmd_submit_plan emits on both the submit_plan and
revise_plan edges."""
from __future__ import annotations

import json
import os
import tempfile
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, delivery
from agentctl.delivery import DeliveryStamp
from agentctl.state import Node
from agentctl.store import FileStateStore


def ns(**kw) -> Namespace:
    return Namespace(**kw)


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_PRESENTATION", "1")


def _to_plan_ready(store, sid, plan) -> None:
    cli.cmd_start(
        ns(session=sid, task="demo", goal="", done_criterion="",
           criterion_type="measurable", recursion_depth=0),
        store=store,
    )
    cli.cmd_classify(
        ns(session=sid, chat=False, changed_lines=200, files=5, wall_clock_min=60,
           tracker_key=None, architectural=True, external_effect=False,
           new_dependency=False, public_api_change=False),
        store=store,
    )
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)


# --- round trip -------------------------------------------------------------

def test_round_trip_hook_stamp(tmp_path):
    state_file = tmp_path / "sess.json"
    stamp = DeliveryStamp(
        plan_path="/plan.toml", plan_sha256="a" * 64, rendering_sha256="b" * 64,
        verified_ts=42.0, source=delivery.SOURCE_HOOK,
    )
    delivery.write_stamp(state_file, stamp)
    assert delivery.read_stamp(state_file) == stamp


def test_round_trip_override_stamp_with_by_and_note(tmp_path):
    state_file = tmp_path / "sess.json"
    stamp = DeliveryStamp(
        plan_path="/plan.toml", plan_sha256="a" * 64, rendering_sha256="b" * 64,
        verified_ts=42.0, source=delivery.SOURCE_OVERRIDE, by="fedor", note="hook down",
    )
    delivery.write_stamp(state_file, stamp)
    assert delivery.read_stamp(state_file) == stamp


def test_stamp_path_for_sibling_of_legacy_root_shaped_path():
    state_file = Path("/home/x/.claude/agentctl/state/sess1.json")
    target = delivery.stamp_path_for(state_file)
    assert target == Path("/home/x/.claude/agentctl/state/sess1.delivery.json")
    assert target.parent == state_file.parent


# --- atomic publication -------------------------------------------------------

def test_write_stamp_uses_os_replace(tmp_path, monkeypatch):
    state_file = tmp_path / "sess.json"
    calls = []
    real_replace = os.replace

    def spy(src, dst):
        calls.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr(delivery.os, "replace", spy)
    stamp = DeliveryStamp(
        plan_path="/p", plan_sha256="a" * 64, rendering_sha256="b" * 64,
        verified_ts=1.0, source=delivery.SOURCE_HOOK,
    )
    delivery.write_stamp(state_file, stamp)
    assert len(calls) == 1
    assert calls[0][1] == delivery.stamp_path_for(state_file)


def test_write_stamp_leaves_no_tempfile_behind(tmp_path):
    state_file = tmp_path / "sess.json"
    stamp = DeliveryStamp(
        plan_path="/p", plan_sha256="a" * 64, rendering_sha256="b" * 64,
        verified_ts=1.0, source=delivery.SOURCE_HOOK,
    )
    delivery.write_stamp(state_file, stamp)
    leftover = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftover == []
    assert delivery.stamp_path_for(state_file).exists()


def test_write_stamp_tempfile_created_in_target_directory(tmp_path, monkeypatch):
    nested = tmp_path / "nested"
    nested.mkdir()
    state_file = nested / "sess.json"
    seen_dirs = []
    real_mkstemp = tempfile.mkstemp

    def spy(*a, **kw):
        seen_dirs.append(kw.get("dir"))
        return real_mkstemp(*a, **kw)

    monkeypatch.setattr(delivery.tempfile, "mkstemp", spy)
    stamp = DeliveryStamp(
        plan_path="/p", plan_sha256="a" * 64, rendering_sha256="b" * 64,
        verified_ts=1.0, source=delivery.SOURCE_HOOK,
    )
    delivery.write_stamp(state_file, stamp)
    assert seen_dirs == [str(nested)]


# --- fail-open parse ----------------------------------------------------------

def test_read_stamp_none_on_absent_corrupt_scalar_list_and_missing_keys(tmp_path):
    state_file = tmp_path / "sess.json"
    assert delivery.read_stamp(state_file) is None  # absent

    target = delivery.stamp_path_for(state_file)
    target.write_text("{not valid json", encoding="utf-8")
    assert delivery.read_stamp(state_file) is None  # corrupt JSON

    target.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert delivery.read_stamp(state_file) is None  # JSON list, not object

    target.write_text(json.dumps("just a string"), encoding="utf-8")
    assert delivery.read_stamp(state_file) is None  # JSON scalar

    target.write_text(json.dumps({"plan_path": "/p"}), encoding="utf-8")
    assert delivery.read_stamp(state_file) is None  # missing required keys


# --- deletion / reset wiring ---------------------------------------------------

def test_delete_stamp_removes_file(tmp_path):
    state_file = tmp_path / "sess.json"
    stamp = DeliveryStamp(
        plan_path="/p", plan_sha256="a" * 64, rendering_sha256="b" * 64,
        verified_ts=1.0, source=delivery.SOURCE_HOOK,
    )
    delivery.write_stamp(state_file, stamp)
    assert delivery.stamp_path_for(state_file).exists()
    delivery.delete_stamp(state_file)
    assert not delivery.stamp_path_for(state_file).exists()


def test_delete_stamp_missing_file_is_noop(tmp_path):
    state_file = tmp_path / "sess.json"
    delivery.delete_stamp(state_file)  # must not raise


def test_cmd_reset_deletes_delivery_stamp(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    store = FileStateStore(home / "agentctl" / "state")
    sid = "reset1"
    cli.cmd_start(
        ns(session=sid, task="t1", goal="g", done_criterion="dc",
           criterion_type="measurable", recursion_depth=0),
        store=store,
    )
    state_file = store.path(sid)
    stamp = DeliveryStamp(
        plan_path="/p", plan_sha256="a" * 64, rendering_sha256="b" * 64,
        verified_ts=1.0, source=delivery.SOURCE_HOOK,
    )
    delivery.write_stamp(state_file, stamp)
    assert delivery.stamp_path_for(state_file).exists()

    cli.cmd_reset(
        ns(session=sid, task="t2", goal="", done_criterion="", criterion_type="measurable",
           recursion_depth=0, force=True),
        store=store,
    )
    assert not delivery.stamp_path_for(state_file).exists()


# --- cmd_submit_plan advisory on both submit and revise edges -----------------

def test_present_plan_advisory_on_submit_and_revise_branches(store, fixtures_dir, gate_on):
    sid = "sub1"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    cli.cmd_start(
        ns(session=sid, task="demo", goal="", done_criterion="",
           criterion_type="measurable", recursion_depth=0),
        store=store,
    )
    cli.cmd_classify(
        ns(session=sid, chat=False, changed_lines=200, files=5, wall_clock_min=60,
           tracker_key=None, architectural=True, external_effect=False,
           new_dependency=False, public_api_change=False),
        store=store,
    )
    cli.cmd_plan(ns(session=sid), store=store)

    d1 = cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    assert d1.ok is True
    assert d1.node == Node.PLAN_READY.value
    assert d1.marker == "PLAN-READY"
    assert any("present-plan" in a for a in d1.data.get("advisories", []))

    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    d2 = cli.cmd_submit_plan(ns(session=sid, plan=refined), store=store)
    assert d2.ok is True
    assert d2.node == Node.PLAN_READY.value
    assert d2.marker == "PLAN-READY"
    assert any("present-plan" in a for a in d2.data.get("advisories", []))


def test_present_plan_advisory_absent_when_gate_off(store, fixtures_dir, monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_PRESENTATION", "0")
    sid = "sub2"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    cli.cmd_start(
        ns(session=sid, task="demo", goal="", done_criterion="",
           criterion_type="measurable", recursion_depth=0),
        store=store,
    )
    cli.cmd_classify(
        ns(session=sid, chat=False, changed_lines=200, files=5, wall_clock_min=60,
           tracker_key=None, architectural=True, external_effect=False,
           new_dependency=False, public_api_change=False),
        store=store,
    )
    cli.cmd_plan(ns(session=sid), store=store)
    d = cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    assert d.ok is True
    assert not any("present-plan" in a for a in d.data.get("advisories", []))
