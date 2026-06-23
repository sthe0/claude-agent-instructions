"""parse_marker + cmd_dispatch marker routing: every specialist return marker on a
spawn's stdout routes to a Directive whose action/node/marker the manager acts on,
with the deterministic continuation text already assembled."""
import importlib.util
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, dispatch
from agentctl.dispatch import RunResult, parse_marker
from agentctl.state import Node

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def ns(**kw):
    return Namespace(**kw)


# --- parse_marker -----------------------------------------------------------

@pytest.mark.parametrize("marker", list(dispatch.RETURN_MARKERS))
def test_parse_marker_each_known_marker(marker):
    m, body = parse_marker(f"{marker}: some detail here\n")
    assert m == marker
    assert body == "some detail here"


def test_parse_marker_malformed():
    m, body = parse_marker("MALFORMED: specialist output did not start with a marker\nrest\n")
    assert m == "MALFORMED"
    assert body == "specialist output did not start with a marker"


def test_parse_marker_skips_leading_blank_lines():
    m, body = parse_marker("\n\n   \nCLARIFY: which key?\n")
    assert m == "CLARIFY"
    assert body == "which key?"


def test_parse_marker_no_marker():
    assert parse_marker("just some free text\nwith no marker\n") == (None, "")
    assert parse_marker("") == (None, "")


def test_parse_marker_only_inspects_first_nonempty_line():
    # a marker buried on a later line is not a return marker
    assert parse_marker("preamble text\nCOMPLETED: done\n") == (None, "")


# --- drift guard ------------------------------------------------------------

def test_return_markers_mirror_spawn_specialist():
    spec = importlib.util.spec_from_file_location(
        "spawn_specialist", REPO_ROOT / "scripts" / "spawn-specialist.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert dispatch.RETURN_MARKERS == mod.RETURN_MARKERS


# --- cmd_dispatch routing ---------------------------------------------------

def _to_executing(store, sid, fixtures_dir):
    """Advance a fresh substantive session to EXECUTING with stage 1 active."""
    plan = str(fixtures_dir / "plan_two_stage.toml")
    cli.cmd_start(ns(session=sid, task="demo-two-stage", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_decompose(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)


def _dispatch_with(store, sid, stdout, returncode=0):
    runner = lambda argv: RunResult(returncode, stdout=stdout)
    return cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
                               dry_run=True), store=store, runner=runner)


def test_completed_routes_to_record_result(store, fixtures_dir):
    _to_executing(store, "m1", fixtures_dir)
    d = _dispatch_with(store, "m1", "COMPLETED: stage done\n")
    assert d.ok is True
    assert d.action == "record_result"
    assert d.marker == "COMPLETED"
    assert d.data["intent_diff_required"] is True
    assert d.node == Node.EXECUTING.value


def test_clarify_routes_with_continuation(store, fixtures_dir):
    _to_executing(store, "m2", fixtures_dir)
    d = _dispatch_with(store, "m2", "CLARIFY: which config key?\n")
    assert d.ok is True
    assert d.action == "answer_clarify"
    assert d.marker == "CLARIFY"
    assert d.data["question"] == "which config key?"
    assert "which config key?" in d.data["continuation"]
    assert d.node == Node.EXECUTING.value


def test_replan_routes_to_replan(store, fixtures_dir):
    _to_executing(store, "m3", fixtures_dir)
    d = _dispatch_with(store, "m3", "REPLAN: step criterion is wrong\n")
    assert d.ok is False
    assert d.action == "replan"
    assert d.marker == "REPLAN"
    assert d.data["reason"] == "step criterion is wrong"


def test_incomplete_routes_to_decide(store, fixtures_dir):
    _to_executing(store, "m4", fixtures_dir)
    d = _dispatch_with(store, "m4", "INCOMPLETE: half done, blocked on X\n")
    assert d.ok is False
    assert d.action == "decide_incomplete"
    assert d.marker == "INCOMPLETE"
    assert d.data["reason"] == "half done, blocked on X"


def test_plan_ready_routes_to_approval_gate(store, fixtures_dir):
    _to_executing(store, "m5", fixtures_dir)
    d = _dispatch_with(store, "m5", "PLAN-READY: plan at /tmp/p.toml\n")
    assert d.ok is True
    assert d.action == "await_plan_approval"
    assert d.marker == "PLAN-READY"
    assert d.node == Node.EXECUTING.value  # node unchanged — re-enters approval gate


def test_escalate_parks_blocked(store, fixtures_dir):
    _to_executing(store, "m6", fixtures_dir)
    d = _dispatch_with(store, "m6", "ESCALATE: spec ambiguity\n")
    assert d.ok is False
    assert d.action == "escalate"
    assert d.marker == "ESCALATE"
    assert d.node == Node.BLOCKED.value
    assert store.load("m6").blocked_from == Node.EXECUTING.value


def test_malformed_parks_blocked(store, fixtures_dir):
    _to_executing(store, "m7", fixtures_dir)
    d = _dispatch_with(store, "m7", "MALFORMED: no marker\n")
    assert d.ok is False
    assert d.node == Node.BLOCKED.value
    assert d.marker == "ESCALATE"


def test_markerless_success_parks_blocked(store, fixtures_dir):
    _to_executing(store, "m8", fixtures_dir)
    d = _dispatch_with(store, "m8", "free text, no marker\n", returncode=0)
    assert d.ok is False
    assert d.node == Node.BLOCKED.value


def test_markerless_failure_handles_spawn_failure(store, fixtures_dir):
    _to_executing(store, "m9", fixtures_dir)
    d = _dispatch_with(store, "m9", "", returncode=1)
    assert d.ok is False
    assert d.action == "handle_spawn_failure"
    assert d.node == Node.EXECUTING.value  # not blocked — a plain spawn failure


def test_marker_wins_over_nonzero_returncode(store, fixtures_dir):
    # a specialist may exit non-zero yet carry a valid CLARIFY marker — marker wins
    _to_executing(store, "m10", fixtures_dir)
    d = _dispatch_with(store, "m10", "CLARIFY: which path?\n", returncode=1)
    assert d.action == "answer_clarify"
    assert d.marker == "CLARIFY"
