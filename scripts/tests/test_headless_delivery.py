"""The headless customer channel: a command in `<config root>/delivery-channel.toml`
that the engine itself hands each delivery to (essence, replan_diff, resolution_ask),
so the delivery and resolution-ask gates have evidence under `claude -p`, where no
hook observes a transcript.

A fake channel command (written into tmp_path) saves the stdin of every delivery to
its own file; the tests compare those bytes with the receipts the engine recorded.
"""
from __future__ import annotations

import hashlib
import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, delivery
from agentctl.render import render_plan_grants
from agentctl.plan import load_plan
from agentctl.state import Node
from agentctl.store import FileStateStore
from conftest import STAGE_OBSERVATIONS

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SID = "headless"

CHANNEL_SCRIPT = """\
import pathlib, sys

here = pathlib.Path(__file__).parent
saved = here / "saved"
saved.mkdir(exist_ok=True)
kind = sys.argv[1]
data = sys.stdin.buffer.read()
(saved / f"{len(list(saved.iterdir())):03d}.{kind}").write_bytes(data)
mode_file = here / "mode"
mode = mode_file.read_text().strip() if mode_file.exists() else "ok"
if mode == "fail":
    sys.exit(3)
if mode == "silent":
    sys.exit(0)
right = "confirm" if kind == "resolution_ask" else "approve"
print(("approve" if right == "confirm" else "confirm") if mode == "wrong" else right)
"""


def ns(**kw) -> Namespace:
    return Namespace(**kw)


@pytest.fixture(autouse=True)
def _gates_on(monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_PRESENTATION", "1")
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "1")
    monkeypatch.setenv("AGENTCTL_RESOLUTION_ASK_GATE", "1")
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "0")
    monkeypatch.setenv("AGENTCTL_STAGE_REVIEW", "0")
    monkeypatch.setenv("AGENTCTL_ADVISOR", "0")


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    # Imported here, not at module level, so the module still collects on a tree
    # without delivery_channel (the stage's negative control runs it there).
    try:
        from agentctl import delivery_channel
    except ImportError:
        delivery_channel = None
    if delivery_channel is not None:
        monkeypatch.setattr(delivery_channel, "CONFIG_NAME", "delivery-channel.toml")
    return home


@pytest.fixture
def store(root) -> FileStateStore:
    return FileStateStore(root / "agentctl" / "state")


@pytest.fixture
def channel(tmp_path, root) -> Path:
    """The fake channel, configured at the config root; returns its directory."""
    cdir = tmp_path / "channel"
    cdir.mkdir()
    script = cdir / "channel.py"
    script.write_text(CHANNEL_SCRIPT, encoding="utf-8")
    (root / "delivery-channel.toml").write_text(
        f"command = [{json.dumps(sys.executable)}, {json.dumps(str(script))}]\n", encoding="utf-8")
    return cdir


def saved(cdir: Path) -> list[tuple[str, bytes]]:
    d = cdir / "saved"
    if not d.exists():
        return []
    return [(p.name.split(".", 1)[1], p.read_bytes()) for p in sorted(d.iterdir())]


def channel_events(store, sid=SID) -> list[dict]:
    return [h for h in store.load(sid).history
            if h.get("event") == delivery.CHANNEL_EVENT and h.get("source") == "channel"]


def venued(tmp_path: Path, name: str, venue: Path) -> str:
    from conftest import venue_plan
    return str(venue_plan(FIXTURES / name, tmp_path / "plans", venue))


def to_plan_ready(store, plan: str, sid=SID) -> None:
    cli.cmd_start(ns(session=sid, task="demo", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(
        session=sid, chat=False, changed_lines=200, files=5, wall_clock_min=60,
        tracker_key=None, architectural=False, external_effect=False,
        new_dependency=False, public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)


def present(store, tmp_path, kind: str, plan: str, text: str, sid=SID, target=None):
    body = text
    if kind in ("essence", "full"):
        fmt = "compact" if kind == "essence" else "full"
        body += "\n\n" + render_plan_grants(load_plan(plan), fmt=fmt).strip()
    rendering = tmp_path / f"{kind}.md"
    rendering.write_text(body, encoding="utf-8")
    return cli.cmd_present_plan(ns(session=sid, kind=kind, plan=target, rendering_file=str(rendering),
                                   emit_skeleton=False), store=store)


def approve(store, sid=SID):
    return cli.cmd_approve(ns(session=sid, by="user"), store=store)


def to_resolution(store, sid=SID) -> None:
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    for observation in STAGE_OBSERVATIONS[:2]:
        cli.cmd_next_stage(ns(session=sid), store=store)
        cli.cmd_record_result(ns(session=sid, status="passed", actual="ok",
                                 control="reviewed: ok", observation=observation), store=store)
    cli.cmd_verify_final(ns(session=sid), store=store)
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="searched", note=""), store=store)
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="skipped", note="fixture"), store=store)
    assert store.load(sid).node == Node.RESOLUTION.value


def resolve(store, sid=SID):
    return cli.cmd_resolve(ns(session=sid, by="user", quality=5, quality_by="user-confirmed",
                              quality_note=None), store=store)


def run_to_approved(store, tmp_path, root):
    plan = venued(tmp_path, "plan_two_stage.toml", tmp_path)
    to_plan_ready(store, plan)
    d = present(store, tmp_path, "essence", plan, "Essence of the plan.")
    assert d.ok is True, d.detail
    return plan


def test_headless_approve_and_replan_delivered(store, channel, tmp_path, root):
    plan = run_to_approved(store, tmp_path, root)
    stamp = delivery.read_stamp(store.path(SID))
    assert stamp is not None and stamp.source == "channel"
    assert [k for k, _ in saved(channel)] == ["essence"]
    d = approve(store)
    assert d.ok is True, d.detail

    cli.cmd_partition(ns(session=SID, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=SID), store=store)
    refined = venued(tmp_path, "plan_two_stage_refined.toml", tmp_path)
    d = present(store, tmp_path, "replan_diff", refined, "Proposed refinement.", target=refined)
    assert d.ok is True, d.detail
    assert [k for k, _ in saved(channel)] == ["essence", "replan_diff"]
    d = cli.cmd_replan(ns(session=SID, plan=refined), store=store)
    assert d.ok is True, d.detail
    assert store.load(SID).stage(1).title == "Scaffold the module skeleton"


def test_headless_resolve_delivered(store, channel, tmp_path, root):
    run_to_approved(store, tmp_path, root)
    assert approve(store).ok is True
    to_resolution(store)
    d = resolve(store)
    assert d.ok is True, d.detail
    sent = dict(saved(channel))
    assert b"[resolution-ask]" in sent["resolution_ask"]
    assert store.load(SID).node == Node.RESOLVED.value


def test_channel_stamp_binds_delivered_bytes(store, channel, tmp_path, root):
    plan = run_to_approved(store, tmp_path, root)
    assert approve(store).ok is True
    cli.cmd_partition(ns(session=SID, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=SID), store=store)
    refined = venued(tmp_path, "plan_two_stage_refined.toml", tmp_path)
    assert present(store, tmp_path, "replan_diff", refined, "Diff.", target=refined).ok is True
    assert cli.cmd_replan(ns(session=SID, plan=refined), store=store).ok is True
    to_resolution(store)
    assert resolve(store).ok is True

    files = saved(channel)
    events = channel_events(store)
    assert [k for k, _ in files] == ["essence", "replan_diff", "resolution_ask"]
    assert [e["kind"] for e in events] == [k for k, _ in files]
    assert [e["sha256"] for e in events] == [hashlib.sha256(b).hexdigest() for _, b in files]
    assert all(e["answered"] is True for e in events)
    stamp = delivery.read_stamp(store.path(SID))
    essence_or_diff = {hashlib.sha256(b).hexdigest() for k, b in files if k != "resolution_ask"}
    assert stamp.source == "channel" and stamp.rendering_sha256 in essence_or_diff


def test_no_channel_behaves_as_main(store, tmp_path, root):
    plan = venued(tmp_path, "plan_two_stage.toml", tmp_path)
    to_plan_ready(store, plan)
    assert present(store, tmp_path, "essence", plan, "Essence.").ok is True
    assert delivery.read_stamp(store.path(SID)) is None
    assert channel_events(store) == []
    d = approve(store)
    assert d.ok is False
    assert any("no delivery proof recorded" in b for b in d.data["blockers"])


def test_failed_channel_stamps_nothing(store, channel, tmp_path, root):
    for mode in ("fail", "silent"):
        (channel / "mode").write_text(mode, encoding="utf-8")
        sid = f"headless-{mode}"
        plan = venued(tmp_path, "plan_two_stage.toml", tmp_path)
        to_plan_ready(store, plan, sid=sid)
        assert present(store, tmp_path, "essence", plan, "Essence.", sid=sid).ok is True
        assert delivery.read_stamp(store.path(sid)) is None
        events = channel_events(store, sid)
        assert len(events) == 1 and events[0]["answered"] is False
        assert approve(store, sid).ok is False


def test_session_written_channel_stamp_refused(store, tmp_path, root):
    plan = venued(tmp_path, "plan_two_stage.toml", tmp_path)
    to_plan_ready(store, plan)
    assert present(store, tmp_path, "essence", plan, "Essence.").ok is True
    receipt = store.load(SID).plan_presentations[-1]
    delivery.write_stamp(store.path(SID), delivery.DeliveryStamp(
        plan_path=receipt.plan_path, plan_sha256=receipt.plan_sha256,
        rendering_sha256=receipt.rendering_sha256, verified_ts=1.0, source="channel"))
    d = approve(store)
    assert d.ok is False
    assert any("no answered channel delivery" in b for b in d.data["blockers"])
    d = cli.cmd_confirm_delivery(ns(session=SID, kind="essence", by="channel", note="n",
                                    escape_reason=delivery.ESCAPE_OTHER), store=store)
    assert d.ok is False


def test_session_reachable_channel_config_ignored(store, channel, tmp_path, root, monkeypatch):
    (root / "delivery-channel.toml").unlink()
    config = f'command = [{json.dumps(sys.executable)}, {json.dumps(str(channel / "channel.py"))}]\n'
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    plans = tmp_path / "plans"
    plans.mkdir(exist_ok=True)
    for where in (cwd, store.root, store.root.parent, plans):
        Path(where).mkdir(parents=True, exist_ok=True)
        (Path(where) / "delivery-channel.toml").write_text(config, encoding="utf-8")
    plan = venued(tmp_path, "plan_two_stage.toml", tmp_path)
    to_plan_ready(store, plan)
    assert present(store, tmp_path, "essence", plan, "Essence.").ok is True
    assert saved(channel) == []
    assert channel_events(store) == []
    assert delivery.read_stamp(store.path(SID)) is None


def test_wrong_first_line_is_not_an_answer(store, channel, tmp_path, root):
    (channel / "mode").write_text("wrong", encoding="utf-8")
    plan = venued(tmp_path, "plan_two_stage.toml", tmp_path)
    to_plan_ready(store, plan)
    assert present(store, tmp_path, "essence", plan, "Essence.").ok is True
    assert delivery.read_stamp(store.path(SID)) is None
    assert [e["answered"] for e in channel_events(store)] == [False]

    (channel / "mode").write_text("ok", encoding="utf-8")
    assert present(store, tmp_path, "essence", plan, "Essence again.").ok is True
    assert approve(store).ok is True
    to_resolution(store)
    (channel / "mode").write_text("wrong", encoding="utf-8")
    d = resolve(store)
    assert d.ok is False
    assert any("gave no `confirm` answer" in b for b in d.data["blockers"])


def test_full_kind_is_logged_without_a_stamp(store, channel, tmp_path, root):
    plan = venued(tmp_path, "plan_two_stage.toml", tmp_path)
    to_plan_ready(store, plan)
    assert present(store, tmp_path, "full", plan, "[stage 1] a\n[stage 2] b\n").ok is True
    assert [e["kind"] for e in channel_events(store)] == ["full"]
    assert channel_events(store)[0]["answered"] is True
    assert delivery.read_stamp(store.path(SID)) is None


def test_resolve_with_invalid_quality_does_not_call_channel(store, channel, tmp_path, root):
    run_to_approved(store, tmp_path, root)
    assert approve(store).ok is True
    to_resolution(store)
    before = len(saved(channel))
    for quality in (None, 9):
        d = cli.cmd_resolve(ns(session=SID, by="user", quality=quality, quality_by="user-confirmed",
                               quality_note=None), store=store)
        assert d.ok is False
    assert len(saved(channel)) == before


def test_unknown_kind_is_rejected(channel):
    from agentctl import delivery_channel

    with pytest.raises(ValueError):
        delivery_channel.deliver("bogus", b"x")
