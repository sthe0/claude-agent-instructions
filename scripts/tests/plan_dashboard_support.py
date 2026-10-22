"""State builders and the console fixture shared by the plan-dashboard test modules.

``plan_dashboard_fixtures`` builds the on-disk roots; this module builds the in-memory
``SessionState`` values they hold and the three-task root the console view tests read.
``isolated_home`` is an autouse fixture: each test module that wants it imports it by name.
``textual_importers`` is the confinement scan: textual may be imported only by the app layer.
"""
from __future__ import annotations

import ast
import datetime as dt
from pathlib import Path

import pytest

from agentctl.state import (
    Actor, Criterion, CriterionType, GateRecord, Means, Outcome, SessionState, Stage, Subject,
)
from lib import transcript_cost
from plan_dashboard import model
from plan_dashboard_fixtures import build_fixture_root, set_mtime

NOW = dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.timezone.utc)
MILLION = 1_000_000
RATE = transcript_cost.token_cost({"input_tokens": MILLION}, "claude-sonnet-5-5")

ALPHA = "alpha-0000-aaaa"
BETA = "beta-1111-bbbb"
STALE = "stale-2222-cccc"
HARNESS = "harness-1"


def at(**delta) -> dt.datetime:
    return NOW - dt.timedelta(**delta)


def iso(moment: dt.datetime) -> str:
    return moment.isoformat(timespec="seconds")


def event(name: str, moment: dt.datetime, **fields) -> dict:
    return {"event": name, "ts": iso(moment), **fields}


def make_stage(index: int, status: str) -> Stage:
    return Stage(
        index=index, title=f"Stage {index}", subject=Subject(material="m", result="r"),
        means=Means(means="m", method="x"), actor=Actor(executor="in_thread"),
        criterion=Criterion(criterion_type=CriterionType.MEASURABLE.value, done_criterion="d"),
        outcome=Outcome(status=status),
    )


def make_state(sid: str, *, history=(), stages=(), node="EXECUTING", **fields) -> SessionState:
    fields.setdefault("weight_class", "SUBSTANTIVE")
    fields.setdefault("approval", GateRecord("plan_approval", passed=node not in ("CLASSIFIED", "ROUTED", "PLAN_READY")))
    fields.setdefault("resolution", GateRecord("resolution", passed=node == "RESOLVED"))
    return SessionState(
        session_id=sid, task_id=f"task-{sid}", goal=fields.pop("goal", f"goal of {sid}"), node=node,
        history=list(history),
        stages=[make_stage(i, status) for i, status in enumerate(stages, 1)], **fields,
    )


def usage_row(message_id: str, moment, million_input_tokens: int) -> dict:
    return {"message_id": message_id, "ts": moment, "model": "claude-sonnet-5-5",
            "usage": {"input_tokens": million_input_tokens * MILLION, "output_tokens": 0}}


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_AGENT_HOME", raising=False)
    model.clear_cache()
    yield
    model.clear_cache()


def textual_importers(paths) -> list[Path]:
    """The files among ``paths`` whose source imports textual."""
    found = []
    for path in map(Path, paths):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) and not node.level else [])
            if any(name == "textual" or name.startswith("textual.") for name in names):
                found.append(path)
                break
    return found


def console_root(tmp_path):
    alpha = make_state(
        ALPHA, goal="Build the thing [with brackets]", stages=("PASSED", "ACTIVE", "PENDING"), history=[
            event("classify", at(hours=2)), event("submit_plan", at(hours=1, minutes=50)),
            event("approve", at(hours=1, minutes=40)), event("next_stage", at(minutes=30), stage=1),
            event("record_result", at(minutes=20), stage=1), event("next_stage", at(minutes=10), stage=2)])
    beta = make_state(BETA, node="PLAN_READY", stages=("PENDING",), history=[event("classify", at(minutes=5))])
    stale = make_state(STALE, history=[event("classify", at(hours=48))])
    root = build_fixture_root(
        tmp_path, [alpha, beta, stale], now=NOW, bindings={HARNESS: [ALPHA]},
        transcripts={HARNESS: [usage_row("m1", at(minutes=25), 1)]},
        ledger_rows=[{"spawner_session": ALPHA, "stage_index": 1, "ts": iso(at(minutes=25)), "cost_usd": 1.5}])
    broken = root.state_root / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    set_mtime(broken, at(minutes=1))
    return root
