"""Concern severity: `blocking:` / `note:` tags, `re:<id>` restatements, and the
advisory downgrade of a blocking concern whose part is frozen.

A reviewer writes each concern `<severity>: [re:<concern-id>] <body>`; an untagged
one is refused. A `blocking:` concern is recorded ADVISORY when its part is unchanged
since the scope's previous record and carries no undischarged blocker, or when it
restates a dispositioned concern without a new regression command; a `revise` whose
blocking concerns are all downgraded is recorded with effective verdict `pass`. The
raw verdict stays on the record and a `plan_review_concerns_downgraded` event is logged.

New symbols are referenced inside test bodies only, so the module collects on a tree
that predates them.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, gates
from agentctl.dispatch import RunResult
from agentctl.plan import load_plan, review_pairs
from agentctl.state import Node, PlanPairReview, PlanReview, SessionState
from lib import marker_extract

SCRIPTS = Path(__file__).resolve().parent.parent
DIGEST = "a" * 64
PAIR = "2-1"
RED = lambda argv: RunResult(1, stdout="", stderr="reproduced")  # noqa: E731


def ns(**kw):
    return Namespace(**kw)


@pytest.fixture(autouse=True)
def gate_on(monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")


@pytest.fixture
def drv():
    spec = importlib.util.spec_from_file_location(
        "plan_review_topological", SCRIPTS / "plan-review-topological.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["plan_review_topological"] = mod
    spec.loader.exec_module(mod)
    return mod


def _sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _session(store, fixtures_dir, tmp_path, sid) -> Path:
    plan = tmp_path / "plan.toml"
    plan.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    cli.cmd_start(ns(session=sid, task="demo", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=str(plan)), store=store)
    return plan


def _retitle(plan: Path, stage_title: str) -> None:
    plan.write_text(plan.read_text().replace(f'title = "{stage_title}', f'title = "{stage_title}+'))


def _review(store, sid, plan, verdict, concerns=None, *, scope=None, ids=None, reviewer="thinker",
            note="", attest=True, regression=None, runner=None):
    return cli.cmd_plan_review(
        ns(session=sid, target=None, scope=scope, verdict=verdict, reviewer=reviewer,
           concerns=concerns, concern_ids=ids, note=note, regression_command=regression,
           plan_digest=_sha(plan) if attest and verdict != "override" else None),
        store=store, runner=runner)


def _pair(store, sid, plan, verdict, concerns=None, *, pair=PAIR, **kw):
    return _review(store, sid, plan, verdict, concerns, scope=f"topo:{pair}", **kw)


def _override(store, sid, plan, pair=PAIR):
    d = _pair(store, sid, plan, "override", pair=pair, reviewer="user", note="accepted")
    assert d.ok, d.detail
    return d


def _pass_every_pair(store, sid, plan):
    for pair in review_pairs(load_plan(str(plan))):
        assert _pair(store, sid, plan, "pass", pair=pair).ok, pair


def _status(store, sid, plan, pair=PAIR) -> str:
    return gates.pair_status(store.load(sid), load_plan(str(plan)), str(plan), pair)


def _record(store, sid, pair=PAIR) -> PlanPairReview:
    return store.load(sid).plan_pair_reviews[pair]


def _compose(store, sid):
    return cli.cmd_plan_review_compose(ns(session=sid, target=None), store=store)


def _downgrade_events(store, sid) -> list[dict]:
    return [e for e in store.load(sid).history if e["event"] == "plan_review_concerns_downgraded"]


# --- 1. the input grammar -------------------------------------------------------

def test_untagged_concern_is_refused_by_the_cli_at_every_scope(store, fixtures_dir, tmp_path):
    plan = _session(store, fixtures_dir, tmp_path, "g1")
    for scope in (None, "stage:1", f"topo:{PAIR}"):
        d = _review(store, "g1", plan, "revise", ["C1: no severity written"], scope=scope)
        assert d.ok is False and "untagged" in d.detail, scope
    state = store.load("g1")
    assert state.plan_review is None and state.plan_pair_reviews == {} and state.concern_ledger == {}


def test_untagged_concern_is_refused_by_the_driver(drv):
    text = "\n".join(["REVIEW:", "Verdict: revise", f"Plan digest: {DIGEST}", "C1: no severity written"])
    with pytest.raises(drv.TopoRefused, match="untagged"):
        drv.parse_review_output(text)


def test_driver_returns_the_tagged_concern_line(drv):
    text = "\n".join(["REVIEW:", "Verdict: revise", f"Plan digest: {DIGEST}",
                      "blocking: C1: gap", "note: C4: aside"])
    assert drv.parse_review_output(text).concerns == ["blocking: C1: gap", "note: C4: aside"]


def test_blocking_concern_on_a_pass_is_refused_by_the_cli(store, fixtures_dir, tmp_path):
    plan = _session(store, fixtures_dir, tmp_path, "g2")
    for scope in (None, f"topo:{PAIR}"):
        d = _review(store, "g2", plan, "pass", ["blocking: C1: should not be on a pass"], scope=scope)
        assert d.ok is False and "blocking" in d.detail, scope
    assert _review(store, "g2", plan, "pass", ["note: C4: fine"], scope=f"topo:{PAIR}").ok


def test_blocking_concern_on_a_pass_is_refused_by_the_driver(drv):
    refused = "\n".join(["REVIEW:", "Verdict: pass", f"Plan digest: {DIGEST}", "blocking: C1: no"])
    with pytest.raises(drv.TopoRefused, match="blocking"):
        drv.parse_review_output(refused)
    allowed = "\n".join(["REVIEW:", "Verdict: pass", f"Plan digest: {DIGEST}", "note: C1: fyi"])
    assert drv.parse_review_output(allowed).concerns == ["note: C1: fyi"]


def test_restating_an_unknown_concern_id_is_refused(store, fixtures_dir, tmp_path):
    plan = _session(store, fixtures_dir, tmp_path, "g3")
    d = _pair(store, "g3", plan, "revise", ["blocking: re:nope#1.c0 C1: again"])
    assert d.ok is False and "names no recorded concern" in d.detail
    assert store.load("g3").plan_pair_reviews == {}


def test_extractor_labels_a_terminal_review_block_with_tagged_concerns_review():
    from lib import review_block

    text = "\n".join(["REVIEW:", "Verdict: revise", f"Plan digest: {DIGEST}",
                      "blocking: C1: gap", "note: re:x#1.c0 C4: aside"])

    def never(argv, **kwargs):
        raise AssertionError("the model must not be consulted for a structural REVIEW block")

    result = marker_extract.extract(text, runner=never)
    assert result.marker == "REVIEW"
    block = review_block.find_terminal_review_block(text)
    assert [value for (_, value), _ in block.region] == ["blocking: C1: gap", "note: re:x#1.c0 C4: aside"]


# --- 2. effective severity ------------------------------------------------------

@pytest.mark.parametrize("kwargs,expected", [
    (dict(severity="note"), "note"),
    (dict(has_prev=False), "blocking"),
    (dict(parts=()), "blocking"),
    (dict(new_evidence=True), "blocking"),
    (dict(changed={"stage:1"}), "blocking"),
    (dict(open_parts={"stage:1"}), "blocking"),
    (dict(), "advisory"),
    (dict(restates_dispositioned=True, changed={"stage:1"}), "advisory"),
    (dict(restates_dispositioned=True, new_evidence=True), "blocking"),
])
def test_effective_concern_severity_branches(kwargs, expected):
    args = dict(severity="blocking", parts=("stage:1",), has_prev=True, changed=set(),
                open_parts=set(), restates_dispositioned=False, new_evidence=False)
    args.update(kwargs)
    assert gates.effective_concern_severity(**args) == expected


def test_first_review_downgrades_nothing(store, fixtures_dir, tmp_path):
    plan = _session(store, fixtures_dir, tmp_path, "s1")
    assert _pair(store, "s1", plan, "revise", ["blocking: C1: first sight"]).ok
    record = _record(store, "s1")
    assert (record.verdict, record.effective_severities) == ("revise", ["blocking"])
    assert _status(store, "s1", plan) == "revise"
    assert _downgrade_events(store, "s1") == []

    _review(store, "s1", plan, "revise", ["blocking: stage:1: first sight"])
    assert store.load("s1").plan_review.effective_severities == ["blocking"]


def test_blocking_on_a_changed_part_revises_and_stales_the_pair(store, fixtures_dir, tmp_path):
    plan = _session(store, fixtures_dir, tmp_path, "s2")
    _pass_every_pair(store, "s2", plan)
    assert _compose(store, "s2").ok
    baseline = store.load("s2").plan_review

    _retitle(plan, "Scaffold module")
    assert _pair(store, "s2", plan, "revise", ["blocking: C1: stage 2 misreads stage 1"]).ok
    record = _record(store, "s2")
    assert (record.verdict, record.raw_verdict, record.effective_severities) == (
        "revise", "revise", ["blocking"])
    assert _status(store, "s2", plan) == "revise"
    composed = _compose(store, "s2")
    assert composed.ok is False and composed.data["failing"][PAIR] == "revise"
    assert PAIR in gates.walk_stale_pairs(store.load("s2"), load_plan(str(plan)), str(plan), baseline)


def test_blocking_on_an_unchanged_part_without_an_open_blocker_is_advisory(store, fixtures_dir, tmp_path):
    plan = _session(store, fixtures_dir, tmp_path, "s3")
    assert _pair(store, "s3", plan, "revise", ["blocking: C1: raised once"]).ok
    _override(store, "s3", plan)

    assert _pair(store, "s3", plan, "revise", ["blocking: C1: raised again"]).ok
    record = _record(store, "s3")
    assert record.effective_severities == ["advisory"]
    assert (record.severities, record.raw_verdict) == (["blocking"], "revise")
    assert len(_downgrade_events(store, "s3")) == 1


def test_reraised_blocker_on_an_unchanged_part_with_an_open_blocker_stays_blocking(
        store, fixtures_dir, tmp_path):
    plan = _session(store, fixtures_dir, tmp_path, "s4")
    first = _pair(store, "s4", plan, "revise", ["blocking: C1: raised once"])
    again = _pair(store, "s4", plan, "revise", ["blocking: C1: raised twice"])
    assert _record(store, "s4").effective_severities == ["blocking"]
    assert _status(store, "s4", plan) == "revise"

    restated = _pair(store, "s4", plan, "revise",
                     [f"blocking: re:{first.data['concern_ids'][0]} C1: raised thrice"])
    assert restated.ok and again.ok
    assert _record(store, "s4").effective_severities == ["blocking"]
    assert _status(store, "s4", plan) == "revise"
    assert _downgrade_events(store, "s4") == []


# --- 3. discharge exits ---------------------------------------------------------

def test_fixed_blocker_no_longer_holds_its_part(store, fixtures_dir, tmp_path):
    plan = _session(store, fixtures_dir, tmp_path, "d1")
    first = _review(store, "d1", plan, "revise", ["blocking: stage:1: scaffold is wrong"], ids=["c-a"])
    (first_id,) = first.data["concern_ids"]
    assert store.load("d1").concern_ledger[first_id].status == "open"

    plan.write_text((fixtures_dir / "plan_two_stage_substantive_stage1and2_retitled.toml").read_text())
    second = _review(store, "d1", plan, "revise", ["blocking: stage:2: tests are wrong"], ids=["c-b"])
    (second_id,) = second.data["concern_ids"]
    ledger = store.load("d1").concern_ledger
    assert (ledger[first_id].status, ledger[second_id].status) == ("fixed", "open")

    _review(store, "d1", plan, "revise", ["blocking: stage:1: scaffold again"], ids=["c-c"])
    assert store.load("d1").plan_review.effective_severities == ["advisory"]


@pytest.mark.parametrize("accepted,expected", [(True, "advisory"), (False, "blocking")])
def test_risk_accepted_blocker_no_longer_holds_its_part(store, fixtures_dir, tmp_path, accepted, expected):
    sid = f"d2-{accepted}"
    plan = _session(store, fixtures_dir, tmp_path, sid)
    _review(store, sid, plan, "revise", ["blocking: stage:1: scaffold is wrong"], ids=["c-a"])
    if accepted:
        d = cli.cmd_risk_accept(ns(session=sid, scope=None, concern_id="c-a", basis="the team accepts the gap",
                                   risk="a regression ships", author="fedor"), store=store)
        assert d.ok, d.detail

    _review(store, sid, plan, "revise", ["blocking: stage:1: scaffold is still wrong"], ids=["c-b"])
    assert store.load(sid).plan_review.effective_severities == [expected]


def test_passed_review_supersedes_the_open_blockers(store, fixtures_dir, tmp_path):
    plan = _session(store, fixtures_dir, tmp_path, "d3")
    first = _review(store, "d3", plan, "revise", ["blocking: stage:1: scaffold is wrong"])
    (first_id,) = first.data["concern_ids"]
    assert _review(store, "d3", plan, "pass").ok
    assert store.load("d3").concern_ledger[first_id].status == "superseded"


# --- 4. what a downgraded revise composes as ------------------------------------

def test_revise_of_only_note_concerns_composes_as_pass(store, fixtures_dir, tmp_path):
    plan = _session(store, fixtures_dir, tmp_path, "c1")
    for pair in review_pairs(load_plan(str(plan))):
        if pair == PAIR:
            assert _pair(store, "c1", plan, "revise", ["note: C4: worth a look", "note: C1: minor"]).ok
        else:
            assert _pair(store, "c1", plan, "pass", pair=pair).ok
    record = _record(store, "c1")
    assert (record.verdict, record.raw_verdict) == ("pass", "revise")
    assert _status(store, "c1", plan) == "current"
    assert _compose(store, "c1").ok


@pytest.mark.parametrize("cause", ["unchanged-part", "restated-disposition"])
def test_revise_of_only_downgraded_concerns_composes_as_pass_and_stays_current(
        store, fixtures_dir, tmp_path, cause):
    sid = f"c2-{cause}"
    plan = _session(store, fixtures_dir, tmp_path, sid)
    first = _pair(store, sid, plan, "revise", ["blocking: C1: raised once"])
    (first_id,) = first.data["concern_ids"]
    _override(store, sid, plan)

    if cause == "unchanged-part":
        concern = "blocking: C1: raised again"
    else:
        _retitle(plan, "Scaffold module")
        assert _pair(store, sid, plan, "revise", ["blocking: C1: changed part, no restatement"]).ok
        assert _record(store, sid).effective_severities == ["blocking"]
        _override(store, sid, plan)
        _retitle(plan, "Scaffold module")
        concern = f"blocking: re:{first_id} C1: raised again"
    for pair in review_pairs(load_plan(str(plan))):
        if pair != PAIR:
            assert _pair(store, sid, plan, "pass", pair=pair).ok
    assert _pair(store, sid, plan, "revise", [concern]).ok

    record = _record(store, sid)
    assert (record.verdict, record.raw_verdict, record.effective_severities) == ("pass", "revise", ["advisory"])
    assert _status(store, sid, plan) == "current"
    assert _downgrade_events(store, sid)
    assert _compose(store, sid).ok


@pytest.mark.parametrize("new_command,expected", [(False, "advisory"), (True, "blocking")])
def test_restating_a_dispositioned_concern_blocks_only_with_a_new_regression_command(
        store, fixtures_dir, tmp_path, new_command, expected):
    sid = f"r1-{new_command}"
    plan = _session(store, fixtures_dir, tmp_path, sid)
    first = _pair(store, sid, plan, "revise", ["blocking: C1: raised once"])
    (first_id,) = first.data["concern_ids"]
    _override(store, sid, plan)
    _retitle(plan, "Scaffold module")

    d = _pair(store, sid, plan, "revise", [f"blocking: re:{first_id} C1: raised again"],
              regression="repro-new" if new_command else None, runner=RED)
    assert d.ok, d.detail
    assert _record(store, sid).effective_severities == [expected]
    assert _record(store, sid).verdict == ("pass" if expected == "advisory" else "revise")


# --- 5. stored records of the earlier format ------------------------------------

def test_legacy_untagged_records_load_as_blocking(tmp_path):
    plan = tmp_path / "plan.toml"
    plan.write_text("x")
    state = SessionState(
        session_id="s", task_id="t", weight_class="SUBSTANTIVE", plan_path=str(plan),
        plan_verified=True, node=Node.PLAN_READY.value,
        plan_review=PlanReview(str(plan), "revise", "thinker", concerns=["old untagged concern"],
                               plan_sha256=_sha(plan)),
        plan_pair_reviews={"2-1": PlanPairReview(
            pair="2-1", base=2, service=1, verdict="revise", reviewer="thinker",
            concerns=["C1: old untagged concern"], plan_path=str(plan))},
    )
    raw = json.loads(json.dumps(state.to_dict()))
    for key in ("severities", "effective_severities", "raw_verdict", "part_digests", "stable_ids"):
        raw["plan_review"].pop(key, None)
    for key in ("concern_ids", "severities", "effective_severities", "raw_verdict", "part_digests"):
        raw["plan_pair_reviews"]["2-1"].pop(key, None)
    raw.pop("concern_ledger", None)

    loaded = SessionState.from_dict(raw)
    assert loaded.concern_ledger == {}
    for record in (loaded.plan_review, loaded.plan_pair_reviews["2-1"]):
        assert record.effective_severities == []
        assert gates.concern_is_blocking(record, 0) is True
    assert gates.plan_review_blockers(loaded, str(plan))
