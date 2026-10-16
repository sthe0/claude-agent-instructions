"""R15: live conversion of a plan that would need added dependencies refuses, writes nothing and
names the ordinary replan as the way forward.

Difficulty removed: converter rule (c) adds `depends_on` to stages that rely on array order, so
converting such a plan in place would change what the plan says while the session's approval,
review and presentation records are bound to the old text. The live verb refuses that case;
`plan-convert` into a separate directory plus an ordinary replan is the route.

Every plan here is synthetic; sessions live in a temporary state directory.
"""
from __future__ import annotations

import hashlib
import sys
from argparse import Namespace
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

SESSION = "live-a"
ROUTE_WORDS = ("plan-convert", "separate directory", "replan", "plan review")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _plan(second_depends_on: str, first_depends_on: str | None) -> bytes:
    first = f"depends_on = {first_depends_on}\n" if first_depends_on is not None else ""
    return (
        "[meta]\n"
        'weight_class = "small_change"\n'
        'task_id = "live-additions"\n'
        'goal = "exercise the live refusal"\n'
        'done_criterion = "all stages PASSED"\n'
        'criterion_type = "measurable"\n'
        "\n[[stage]]\nindex = 1\n"
        f"{first}"
        'title = "First"\nexecutor = "spawn:developer"\n'
        'expected_result_image = "first image"\ncriterion_type = "measurable"\n'
        'done_criterion = "first done"\noutput_artifacts = ["a.py"]\n'
        "\n[[stage]]\nindex = 2\n"
        f"depends_on = {second_depends_on}\n"
        'title = "Second"\nexecutor = "spawn:developer"\n'
        'expected_result_image = "second image"\ncriterion_type = "measurable"\n'
        'done_criterion = "second done"\noutput_artifacts = ["b.py"]\n'
    ).encode("utf-8")


NEEDS_ADDITIONS = _plan("[1]", None)
NEEDS_NONE = _plan("[1]", "[]")


def _live_session(store, tmp_path: Path, source: bytes) -> Path:
    from agentctl.state import GateRecord, Node, PlanPresentation, PlanReview, SessionState

    plan_dir = tmp_path / "plans-live"
    plan_dir.mkdir()
    plan_file = plan_dir / "demo.toml"
    plan_file.write_bytes(source)
    root = _sha(source)
    state = SessionState(session_id=SESSION, task_id="live")
    state.node = Node.EXECUTING.value
    state.approval = GateRecord("plan_approval", armed=True, passed=True, by="user")
    state.plan_path = str(plan_file)
    state.accepted_plan_digest = root
    state.plan_snapshot_path = str(plan_dir / "snapshot.toml")
    state.plan_snapshot_hash = root
    state.plan_review = PlanReview(
        plan_path=str(plan_file), verdict="pass", reviewer="thinker", plan_sha256=root)
    state.plan_presentations = [PlanPresentation(
        plan_path=str(plan_file), kind="essence", plan_sha256=root,
        rendering_sha256="0" * 64, rendering_text="the essence", presented_ts=1.0)]
    store.save(state)
    return plan_file


def _live(store):
    from agentctl import plan_convert

    return plan_convert.cmd_plan_convert_live(
        Namespace(session=SESSION, by="agent", revert=False), store=store)


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _world(store, tmp_path: Path) -> dict:
    state = store.load(SESSION)
    return {
        "state": store.load(SESSION).to_dict() if hasattr(state, "to_dict") else repr(state),
        "files": _tree_bytes(tmp_path),
    }


def test_live_additions_refusal_names_every_addition_and_the_route(store, tmp_path, monkeypatch):
    from agentctl import plan_store

    monkeypatch.setenv(plan_store.OBJECTS_DIR_ENV, str(tmp_path / "objects"))
    _live_session(store, tmp_path, NEEDS_ADDITIONS)

    result = _live(store)

    assert not result.ok
    assert result.data["would_add"] == [{"stage": 1, "depends_on": []}]
    assert "stage 1 depends_on []" in result.detail
    for words in ROUTE_WORDS:
        assert words in result.detail


def test_live_additions_refusal_lists_each_added_dependency(store, tmp_path, monkeypatch):
    from agentctl import plan_store

    monkeypatch.setenv(plan_store.OBJECTS_DIR_ENV, str(tmp_path / "objects"))
    _live_session(store, tmp_path, _plan("None", None).replace(b"depends_on = None\n", b""))

    result = _live(store)

    assert not result.ok
    assert result.data["would_add"] == [
        {"stage": 1, "depends_on": []}, {"stage": 2, "depends_on": [1]}]
    assert "stage 2 depends_on [1]" in result.detail


def test_live_additions_refusal_changes_no_state_plan_or_history_byte(
        store, tmp_path, monkeypatch):
    from agentctl import plan_store

    monkeypatch.setenv(plan_store.OBJECTS_DIR_ENV, str(tmp_path / "objects"))
    _live_session(store, tmp_path, NEEDS_ADDITIONS)
    before = _world(store, tmp_path)

    result = _live(store)

    assert not result.ok
    assert _world(store, tmp_path) == before
    state = store.load(SESSION)
    assert state.plan_conversions == []
    assert not (tmp_path / "plans-live" / "demo.parts").exists()


def test_live_additions_a_plan_that_needs_none_still_converts(store, tmp_path, monkeypatch):
    from agentctl import plan_store

    monkeypatch.setenv(plan_store.OBJECTS_DIR_ENV, str(tmp_path / "objects"))
    plan_file = _live_session(store, tmp_path, NEEDS_NONE)

    result = _live(store)

    assert result.ok, result.detail
    assert (plan_file.parent / "demo.parts" / "stage-1.toml").is_file()
    assert len(store.load(SESSION).plan_conversions) == 1


def test_live_additions_the_named_route_moves_the_session_to_the_converted_plan(
        store, tmp_path, venued_fixtures_dir, monkeypatch):
    from agentctl import cli, plan_convert

    # The replan's diff classification runs for real. Two gates are off: the presentation
    # receipt, switched off here as the suite's other replan tests do (it needs a delivered
    # turn), and the plan-review gate, which conftest.py switches off for every test.
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")

    source = venued_fixtures_dir / "route.toml"
    source.write_text(
        (venued_fixtures_dir / "plan_two_stage.toml").read_text(encoding="utf-8").replace(
            "depends_on = []\n", "", 1), encoding="utf-8")
    sid = "route"
    for step in (
        lambda: cli.cmd_start(Namespace(session=sid, task="route", goal="", done_criterion="",
                                        criterion_type="measurable", recursion_depth=0),
                              store=store),
        lambda: cli.cmd_classify(Namespace(
            session=sid, chat=False, changed_lines=200, files=5, wall_clock_min=60,
            tracker_key=None, architectural=True, external_effect=False, new_dependency=False,
            public_api_change=False), store=store),
        lambda: cli.cmd_plan(Namespace(session=sid), store=store),
        lambda: cli.cmd_submit_plan(Namespace(session=sid, plan=str(source)), store=store),
        lambda: cli.cmd_approve(Namespace(session=sid, by="user"), store=store),
    ):
        step()
    out = tmp_path / "converted"

    converted = plan_convert.convert_into_dir(source, out)
    assert converted["added_depends_on"] == [{"stage": 1, "depends_on": []}]
    assert (out / "route.parts" / "stage-1.toml").is_file()

    refused = plan_convert.cmd_plan_convert_live(
        Namespace(session=sid, by="agent", revert=False), store=store)
    assert not refused.ok
    assert refused.data["would_add"] == [{"stage": 1, "depends_on": []}]

    replanned = cli.cmd_replan(Namespace(session=sid, plan=converted["index"]), store=store)

    assert replanned.ok, replanned.detail
    assert store.load(sid).plan_path == converted["index"]
