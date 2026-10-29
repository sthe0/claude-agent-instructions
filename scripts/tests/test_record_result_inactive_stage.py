"""record-result on a stage a replan re-armed (PENDING, node VERIFYING) is refused before
it touches state, and the refusal names `agentctl next-stage` as the way out — instead of
logging an attempt and then failing at the EXECUTING -> VERIFYING transition."""
from __future__ import annotations

from agentctl.state import Node, StageStatus
from test_verify_execution import _executing, _record_passed, _stage, runner_returning

NEXT_STAGE_HINT = "agentctl next-stage"


def _rearmed(sid):
    s = _executing(sid, _stage(1, verify_command="true", status=StageStatus.PENDING.value))
    s.node = Node.VERIFYING.value
    return s


def test_rearmed_stage_is_refused_with_next_stage_hint(store):
    store.save(_rearmed("rr-pending"))
    before = store.load("rr-pending").to_dict()
    d = _record_passed(store, "rr-pending", runner=runner_returning(0))
    assert d.ok is False
    assert d.action == "next_stage"
    assert NEXT_STAGE_HINT in d.detail
    assert StageStatus.PENDING.value in d.detail and Node.VERIFYING.value in d.detail
    assert store.load("rr-pending").to_dict() == before


def test_active_stage_at_executing_still_records(store):
    store.save(_executing("rr-active", _stage(1, verify_command="true")))
    d = _record_passed(store, "rr-active", runner=runner_returning(0))
    assert d.ok is True
