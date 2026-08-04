"""Tests for `confirm-delivery`'s typed escape reason.

The escape existed before this field and was taken silently: `--note` recorded a
sentence, and a sentence cannot be counted. These tests pin the two properties
that make the reason a datum rather than another archive entry — it is REQUIRED,
and it is CLOSED — plus the boundary that keeps it a human act (`--by hook`
stays refused).

Refusals are asserted as Directive(ok=False), not SystemExit: the enum is
enforced in the command body so that a caller building the namespace directly —
which every one of this repo's other test modules does — gets the same answer as
one going through argparse.
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, delivery
from agentctl.store import FileStateStore

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def ns(**kw) -> Namespace:
    return Namespace(**kw)


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_PRESENTATION", "1")
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "0")
    monkeypatch.setenv("AGENTCTL_STAGE_REVIEW", "0")
    monkeypatch.setenv("AGENTCTL_ADVISOR", "0")


@pytest.fixture
def home_store(tmp_path, monkeypatch):
    """A store whose root agrees with config_root.resolve_agentctl_state_file,
    so the sidecar this command writes is the one a reader would find."""
    home = tmp_path / "home"
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return FileStateStore(home / "agentctl" / "state")


def _presented(store, sid: str, tmp_path: Path) -> None:
    """Drive a session to the point where a delivery stamp has something to bind
    to: a plan, an approval-ready state, and an essence presentation receipt."""
    plan = str(FIXTURES / "plan_two_stage.toml")
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
    rendering = tmp_path / "rendering.md"
    rendering.write_text("essence text", encoding="utf-8")
    cli.cmd_present_plan(
        ns(session=sid, kind="essence", rendering_file=str(rendering),
           emit_skeleton=False),
        store=store,
    )


def _stamp(store, sid: str) -> delivery.DeliveryStamp | None:
    return delivery.read_stamp(store.path(sid))


def test_missing_escape_reason_is_refused(gate_on, home_store, tmp_path):
    _presented(home_store, "ce1", tmp_path)
    d = cli.cmd_confirm_delivery(
        ns(session="ce1", by="fedor", note="hook is dead", escape_reason=""),
        store=home_store,
    )
    assert d.ok is False
    assert "--escape-reason" in d.detail
    assert _stamp(home_store, "ce1") is None


def test_out_of_set_escape_reason_is_refused(gate_on, home_store, tmp_path):
    """A closed set that accepts anything is a free-text field with extra steps."""
    _presented(home_store, "ce2", tmp_path)
    d = cli.cmd_confirm_delivery(
        ns(session="ce2", by="fedor", note="hook is dead",
           escape_reason="because I said so"),
        store=home_store,
    )
    assert d.ok is False
    assert "not one of" in d.detail
    assert delivery.ESCAPE_HOOK_NOT_INSTALLED in d.detail
    assert _stamp(home_store, "ce2") is None


@pytest.mark.parametrize("reason", list(delivery.DELIVERY_ESCAPE_REASONS))
def test_every_declared_reason_writes_a_stamp_carrying_it(
        gate_on, home_store, tmp_path, reason):
    """Every value in the set is actually usable — including `other`, whose
    absence would push unforeseen cases into the nearest wrong bucket."""
    sid = "ce3-" + reason
    _presented(home_store, sid, tmp_path)
    d = cli.cmd_confirm_delivery(
        ns(session=sid, by="fedor", note="why the hook could not", escape_reason=reason),
        store=home_store,
    )
    assert d.ok is True, d.detail
    stamp = _stamp(home_store, sid)
    assert stamp is not None
    assert stamp.source == delivery.SOURCE_OVERRIDE
    assert stamp.escape_reason == reason
    assert stamp.note == "why the hook could not"


def test_by_hook_is_still_rejected(gate_on, home_store, tmp_path):
    """The boundary the typed reason must not erode: the hook path is barred
    from this command however well-formed the rest of the call is."""
    _presented(home_store, "ce4", tmp_path)
    d = cli.cmd_confirm_delivery(
        ns(session="ce4", by="hook", note="n",
           escape_reason=delivery.ESCAPE_HOOK_NOT_FIRED),
        store=home_store,
    )
    assert d.ok is False
    assert _stamp(home_store, "ce4") is None


def test_refusal_names_every_missing_field_at_once(gate_on, home_store, tmp_path):
    """One refusal listing all three, not three round-trips."""
    _presented(home_store, "ce5", tmp_path)
    d = cli.cmd_confirm_delivery(
        ns(session="ce5", by="", note="", escape_reason=""), store=home_store)
    assert d.ok is False
    for flag in ("--by", "--note", "--escape-reason"):
        assert flag in d.detail


def test_parser_requires_the_flag():
    """The argparse half. Body validation covers direct callers; this covers the
    command line, where a forgotten flag must not default to empty and slip
    through as an untyped escape."""
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["confirm-delivery", "--session", "s", "--by", "f", "--note", "n"])
    args = parser.parse_args(
        ["confirm-delivery", "--session", "s", "--by", "f", "--note", "n",
         "--escape-reason", delivery.ESCAPE_OTHER])
    assert args.escape_reason == delivery.ESCAPE_OTHER
