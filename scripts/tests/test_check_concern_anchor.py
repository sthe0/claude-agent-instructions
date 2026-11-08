"""A blocking concern about a plan check blocks only when it cites an order requirement.

A reviewer who finds a plan check weak writes the concern as `check: ... req:<id>`; the id is
the order requirement the check fails to decide. A `blocking:` check concern that cites no id
of the plan's order requirements is recorded as advisory, so a `revise` made of such concerns
composes as pass. A plan whose order has no requirements, and a concern that is not about a
check, are left as written.

The tests call only `cli.cmd_plan_review` and read the stored record.
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, gates
from agentctl.plan import load_plan, review_ids
from agentctl.state import PlanPairReview

PAIR = "2-1"

ORDER = """
[meta.order]
customer_id = "user"
customer = "the user, who asked for the demo"
functional_place = "The place where the demo module is checked."

[[meta.order.requirements]]
id = "R1"
text = "The module imports cleanly."
derivation = "A broken import blocks every consumer."

[[meta.order.requirements]]
id = "R2"
text = "The tests for the module pass."
derivation = "An untested module regresses silently."
"""

EMPTY_ORDER = """
[meta.order]
customer_id = "user"
customer = "the user, who asked for the demo"
functional_place = "The place where the demo module is checked."
"""


@pytest.fixture(autouse=True)
def gate_on(monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")


@pytest.fixture(autouse=True)
def private_escalation_ledger(monkeypatch, tmp_path):
    monkeypatch.setenv(cli.ESCALATION_LEDGER_ENV, str(tmp_path / "escalations.jsonl"))


def _session(store, fixtures_dir, tmp_path, sid, order) -> Path:
    plan = tmp_path / "plan.toml"
    plan.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text() + order)
    cli.cmd_start(Namespace(session=sid, task="demo", goal="", done_criterion="",
                            criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(Namespace(session=sid, chat=False, changed_lines=200, files=5,
                               wall_clock_min=60, tracker_key=None, architectural=True,
                               external_effect=False, new_dependency=False,
                               public_api_change=False), store=store)
    cli.cmd_plan(Namespace(session=sid), store=store)
    cli.cmd_submit_plan(Namespace(session=sid, plan=str(plan)), store=store)
    return plan


def _pair_revise(store, sid, plan, concern):
    import hashlib

    d = cli.cmd_plan_review(
        Namespace(session=sid, target=None, scope=f"topo:{PAIR}", verdict="revise",
                  reviewer="thinker", concerns=[concern], concern_ids=None, note="",
                  regression_command=None,
                  plan_digest=hashlib.sha256(plan.read_bytes()).hexdigest()),
        store=store, runner=None)
    assert d.ok, d.detail
    return store.load(sid).plan_pair_reviews[PAIR]


def _pass_the_other_pairs(store, sid, plan):
    import hashlib

    for pair in review_ids(load_plan(str(plan))):
        if pair == PAIR:
            continue
        d = cli.cmd_plan_review(
            Namespace(session=sid, target=None, scope=f"topo:{pair}", verdict="pass",
                      reviewer="thinker", concerns=None, concern_ids=None, note="",
                      regression_command=None,
                      plan_digest=hashlib.sha256(plan.read_bytes()).hexdigest()),
            store=store, runner=None)
        assert d.ok, d.detail


def test_unanchored_check_concern_is_advisory_and_the_revise_composes_as_pass(
        store, fixtures_dir, tmp_path):
    plan = _session(store, fixtures_dir, tmp_path, "a1", ORDER)
    _pass_the_other_pairs(store, "a1", plan)
    record = _pair_revise(store, "a1", plan, "blocking: C2: check: stage:1 the check is weak")
    assert isinstance(record, PlanPairReview)
    assert record.effective_severities == ["advisory"]
    assert (record.raw_verdict, record.verdict) == ("revise", "pass")
    assert cli.cmd_plan_review_compose(
        Namespace(session="a1", target=None), store=store).ok


def test_check_concern_citing_an_order_requirement_stays_blocking(store, fixtures_dir, tmp_path):
    plan = _session(store, fixtures_dir, tmp_path, "a2", ORDER)
    record = _pair_revise(store, "a2", plan, "blocking: C2: check: stage:1 req:R1 the check is weak")
    assert record.effective_severities == ["blocking"]
    assert record.verdict == "revise"


def test_check_concern_citing_an_unknown_requirement_is_advisory(store, fixtures_dir, tmp_path):
    plan = _session(store, fixtures_dir, tmp_path, "a3", ORDER)
    record = _pair_revise(store, "a3", plan, "blocking: C2: check: stage:1 req:R9 the check is weak")
    assert record.effective_severities == ["advisory"]


def test_check_token_after_the_part_token_is_read_too(store, fixtures_dir, tmp_path):
    plan = _session(store, fixtures_dir, tmp_path, "a4", ORDER)
    assert _pair_revise(store, "a4", plan, "blocking: C2: stage:1 check: weak").effective_severities == [
        "advisory"]
    plan = _session(store, fixtures_dir, tmp_path, "a5", ORDER)
    assert _pair_revise(store, "a5", plan, "blocking: C2: stage:1 check: req:R2 weak").effective_severities == [
        "blocking"]


def test_plan_without_order_requirements_keeps_the_check_concern_as_written(
        store, fixtures_dir, tmp_path):
    plan = _session(store, fixtures_dir, tmp_path, "b1", EMPTY_ORDER)
    record = _pair_revise(store, "b1", plan, "blocking: C2: check: stage:1 the check is weak")
    assert record.effective_severities == ["blocking"]


def test_blocking_concern_that_is_not_about_a_check_stays_blocking(store, fixtures_dir, tmp_path):
    plan = _session(store, fixtures_dir, tmp_path, "b2", ORDER)
    record = _pair_revise(store, "b2", plan, "blocking: C2: stage:1 the scaffold is wrong")
    assert record.effective_severities == ["blocking"]


def test_check_concern_anchored_is_a_pure_function_of_body_and_ids():
    ids = ("R1", "R2")
    anchored = getattr(gates, "check_concern_anchored", None)
    assert anchored is not None, "gates.check_concern_anchored is missing"
    assert anchored("C2: check: stage:1 weak", ids) is False
    assert anchored("C2: check: req:R2 weak", ids) is True
    assert anchored("C2: stage:3 check: req:R1. weak", ids) is True
    assert anchored("C2: check: req:R9 weak", ids) is False
    assert anchored("C2: stage:1 not a check, mentions req:R9", ids) is True
    assert anchored("C2: check: weak", ()) is True


def test_a_note_about_an_unanchored_check_stays_a_note(store, fixtures_dir, tmp_path):
    plan = _session(store, fixtures_dir, tmp_path, "n1", ORDER)
    record = _pair_revise(store, "n1", plan,
                          "note: C2: check: stage:1 the check is weak")
    assert record.effective_severities == ["note"]
