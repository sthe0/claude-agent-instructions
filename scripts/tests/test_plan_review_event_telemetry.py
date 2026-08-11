"""cmd_plan_review's durable history event previously carried only event/reviewer/
target/verdict, dropping plan_sha256/concerns/note (transient on the single-slot
PlanReview, overwritten by the next review) and never recording plan_bytes or
finding counts at all. That made a round's fix size and finding count
unrecoverable after the fact — see the availability census in
plan-convergence-evidence/availability.tsv. This locks the fix: the fields the
PlanReview slot already holds, plus plan_bytes and the two optional finding-count
arguments, now round-trip onto the event."""
from __future__ import annotations

import hashlib
from argparse import Namespace
from pathlib import Path

from agentctl import cli
from agentctl.state import SessionState


def ns(**kw):
    return Namespace(**kw)


def _sha256_file(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _start_session(store, sid: str, plan: Path) -> None:
    store.save(SessionState(session_id=sid, task_id="t", weight_class="SUBSTANTIVE",
                            plan_path=str(plan), plan_verified=True))


def test_plan_review_event_carries_the_full_record(store, tmp_path):
    plan = tmp_path / "plan.toml"
    plan.write_text("[meta]\ntask_id = \"t\"\n")
    _start_session(store, "s", plan)

    cli.cmd_plan_review(ns(
        session="s", verdict="pass", reviewer="thinker",
        concerns=["the fix sizes are unrecoverable"], note="see availability census",
        target=None, plan_digest=_sha256_file(plan),
        findings_blocking=3, findings_nonblocking=1,
    ), store=store)

    event = store.load("s").history[-1]
    assert event["event"] == "plan_review"
    assert event["target"] == str(plan)
    assert event["verdict"] == "pass"
    assert event["reviewer"] == "thinker"
    assert event["plan_sha256"] == _sha256_file(plan)
    assert event["plan_bytes"] == plan.stat().st_size
    assert event["concerns"] == ["the fix sizes are unrecoverable"]
    assert event["note"] == "see availability census"
    assert event["findings_blocking"] == 3
    assert event["findings_nonblocking"] == 1


def test_plan_review_event_findings_default_to_none(store, tmp_path):
    plan = tmp_path / "plan.toml"
    plan.write_text("[meta]\ntask_id = \"t\"\n")
    _start_session(store, "s", plan)

    cli.cmd_plan_review(ns(
        session="s", verdict="revise", reviewer="thinker",
        concerns=None, note="", target=None, plan_digest=None,
        findings_blocking=None, findings_nonblocking=None,
    ), store=store)

    event = store.load("s").history[-1]
    assert event["findings_blocking"] is None
    assert event["findings_nonblocking"] is None
    assert event["plan_bytes"] == plan.stat().st_size
    assert event["plan_sha256"] == ""
    assert event["concerns"] == []
    assert event["note"] == ""
