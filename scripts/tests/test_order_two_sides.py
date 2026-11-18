"""The order in two sides: what the user agrees, what is shown, what is kept beside.

The customer side (the activity, the difficulty, the functional place, the customer, and the
`customer_side` table with its acceptance checks) is agreed by the user and covered by the agreed
digest. The executor side (the type and specifics entries, the requirements, the how-built
proposals with their verdicts, the constraints on the product) is shown at agreement and not
agreed: it has its own digest and changes without a new agreement. The records beside the order
(binding conditions, method ideas, conditions on means, the planning budget) are in neither digest.

Calls into code a mutant replaces go through their module (`cli_order.`, `plan.`), never a
by-name import, so `order_two_sides_mutants` can plant a defect that these tests must catch.
"""
from __future__ import annotations

import json
from argparse import Namespace

import pytest

from agentctl import cli_order, plan
from agentctl.dispatch import RunResult
from agentctl.plan import load_order_draft
from order_agreement_mutants import mutant  # noqa: F401  (autouse fixture)
from test_order_agreement import (  # noqa: F401  (home and ostore are fixtures)
    SESSION, _marker_of, _presented, _start, _user_answers, home, ostore,
)
from test_order_phase import _LINES, BUDGET, SIDES, _draft, _submit

OTHER_REGULARITY = "a-change-outside-the-state-its-consumers-read-is-unseen-by-them"

BESIDE = (
    '\n[[meta.order.binding_conditions]]\nid = "B1"\ntext = "no change to the release branch"\n'
    'set_by = "user"\n'
    '\n[[meta.order.method_ideas]]\nid = "M1"\ntext = "tag from a cron job"\nverdict = "reject"\n'
    'critique = "tags a commit nobody asked for"\n'
    '\n[[meta.order.means_conditions]]\nid = "E1"\ntext = "the command runs on the build host"\n'
)


def ns(**kw) -> Namespace:
    return Namespace(**kw)


def _doc(path):
    return load_order_draft(path)[0]


def _digests(path):
    doc = _doc(path)
    return plan.understanding_digest(doc), plan.executor_side_digest(doc)


def _sides(tmp_path, name, *, old=None, new=None, extra="", **draft_kw):
    """A new-form draft whose sides text has `old` replaced by `new` (or `extra` appended)."""
    sides = SIDES.replace(old, new) if old else SIDES
    assert not old or sides != SIDES, old
    return _draft(tmp_path, name=name, sides=sides + extra, **draft_kw)


def _replacing(field, old, new):
    text = _LINES[field].replace(old, new)
    assert text != _LINES[field], (field, old)
    return {field: text}


def _agree_two_sided(store, tmp_path, *, correction="", decisions=(), **draft_kw):
    path = _presented(store, tmp_path, **draft_kw)
    assert _user_answers(_marker_of(path)) == 1
    d = cli_order.cmd_order_agree(
        ns(session=SESSION, note="", correction=correction, binding_decision=list(decisions)), store=store)
    assert d.ok, d.detail
    return path


def test_executor_side_outside_agreed_digest(tmp_path):
    doc = _doc(_draft(tmp_path))

    agreed = plan.understanding_parts(doc)
    shown = plan.executor_side_parts(doc)

    assert shown and set(shown) >= {"type_and_specifics", "R:R1", "P:P1", "K:K1"}
    assert not set(shown) & set(agreed), sorted(set(shown) & set(agreed))
    assert {"S:step", "S:missing", "S:product", "S:criterion_type", "C:A1"} <= set(agreed)
    assert plan.executor_side_digest(doc) != plan.understanding_digest(doc)
    assert not any(k.startswith(("R:", "P:", "K:")) or k == "planning_estimate" for k in agreed)


@pytest.mark.parametrize("change", [
    pytest.param({"replace": _replacing("requirements", "tagged by a command", "tagged by one command")},
                 id="requirement-text"),
    pytest.param({"replace": _replacing("requirements", "removes the hand-work", "saves a day")},
                 id="requirement-derivation"),
    pytest.param({"replace": _replacing("proposals", "tag releases automatically", "tag on every merge")},
                 id="proposal-text"),
    pytest.param({"replace": _replacing("proposals", 'verdict = "adopt"', 'verdict = "adapt"')},
                 id="proposal-verdict"),
    pytest.param({"replace": _replacing("constraints", "no new dependency", "no new service")},
                 id="constraint-text"),
    pytest.param({"old": "the main line only", "new": "release branches too"}, id="specifics"),
    pytest.param({"old": "a-child-process-reads-its-parents-ambient-root", "new": OTHER_REGULARITY},
                 id="type-entry-ref"),
])
def test_executor_side_change_keeps_agreed_digest(tmp_path, change):
    base = _digests(_draft(tmp_path))

    changed = _digests(_sides(tmp_path, "changed.toml", **change))

    assert changed[0] == base[0], "an executor-side change moved the agreed digest"
    assert changed[1] != base[1], "an executor-side change did not move the executor side's digest"


@pytest.mark.parametrize("change", [
    pytest.param({"old": 'step = "cutting a release"', "new": 'step = "publishing a release"'}, id="step"),
    pytest.param({"old": "a command that tags the release", "new": "a button that tags the release"},
                 id="missing"),
    pytest.param({"old": 'criterion_type = "measurable"', "new": 'criterion_type = "acceptance-review"'},
                 id="criterion-type"),
    pytest.param({"old": "the command tags a release", "new": "the command tags and signs a release"},
                 id="check-text"),
    pytest.param({"old": 'kind = "direct"',
                  "new": 'kind = "mediated"\nreason = "the tag is observed through the log"'},
                 id="check-kind-and-reason"),
])
def test_customer_side_change_moves_the_agreed_digest(tmp_path, change):
    base = _digests(_draft(tmp_path))

    changed = _digests(_sides(tmp_path, "changed.toml", **change))

    assert changed[0] != base[0]


def test_agreement_record_names_customer_side_and_checks(ostore, tmp_path):
    path = _agree_two_sided(
        ostore, tmp_path, correction="the release is cut by the build host, not by hand",
        decisions=["B1=keep"], sides=SIDES + BESIDE)

    agreement = ostore.load(SESSION).order_agreement
    doc = _doc(path)

    assert agreement.customer_side_agreed is True
    assert agreement.understanding_sha256 == plan.understanding_digest(doc)
    assert {"S:step", "S:missing", "S:product", "S:criterion_type", "C:A1"} <= set(agreement.parts)
    assert agreement.executor_side_sha256 == plan.executor_side_digest(doc)
    assert agreement.executor_side_parts == plan.executor_side_parts(doc)
    assert agreement.correction == "the release is cut by the build host, not by hand"
    assert agreement.binding_decisions == [{"id": "B1", "decision": "keep", "decided_by": "user"}]
    assert [b["id"] for b in agreement.binding_conditions] == ["B1"]
    assert agreement.binding_conditions[0]["text"] == "no change to the release branch"
    assert agreement.planning_budget == {
        "active_minutes": 12, "usd": 4.5, "derivation": "one reviewed plan of a few stages"}


def test_agreement_needs_a_decision_on_every_binding_condition(ostore, tmp_path):
    path = _presented(ostore, tmp_path, sides=SIDES + BESIDE)
    assert _user_answers(_marker_of(path)) == 1

    d = cli_order.cmd_order_agree(ns(session=SESSION, note="", correction="", binding_decision=[]),
                                  store=ostore)

    assert d.ok is False and "B1" in d.detail
    assert ostore.load(SESSION).order_agreement is None


def test_agreement_renews_the_planning_arming_from_the_budget(ostore, tmp_path):
    _agree_two_sided(ostore, tmp_path)

    segments = ostore.load(SESSION).effort_segments

    assert segments["agreed_estimate"]["active_minutes"] == 12 and segments["agreed_estimate"]["usd"] == 4.5


def test_an_order_without_a_customer_side_is_refused_at_submit(ostore, tmp_path):
    _start(ostore)

    d = _submit(ostore, _draft(tmp_path, omit=("customer_side",)))

    assert d.ok is False and "customer_side" in d.detail
    assert not ostore.load(SESSION).order_draft_path


@pytest.mark.parametrize("old, new, expected", [
    ('kind = "direct"', 'kind = "mediated"', "mediated acceptance check A1 has no 'reason'"),
    ('kind = "direct"', 'kind = "sampled"', "kind 'sampled'"),
    ('criterion_type = "measurable"', 'criterion_type = "vibes"', "criterion_type must be one of"),
    ('specifics = "releases here are tagged from the main line only"\n', "", "missing 'specifics'"),
])
def test_order_submit_refuses_a_malformed_side(ostore, tmp_path, old, new, expected):
    _start(ostore)

    d = _submit(ostore, _sides(tmp_path, "bad.toml", old=old, new=new))

    assert d.ok is False and any(expected in p for p in d.data["problems"]), d.data["problems"]


def test_beside_records_outside_both_digests(ostore, tmp_path):
    plain = _digests(_draft(tmp_path))
    beside = _sides(tmp_path, "beside.toml", extra=BESIDE)

    assert _digests(beside) == plain, "a record beside the order moved a digest"

    _start(ostore)
    assert _submit(ostore, beside).ok
    kept = ostore.load(SESSION).order_beside
    assert [b["id"] for b in kept["binding_conditions"]] == ["B1"]
    assert [m["id"] for m in kept["method_ideas"]] == ["M1"]
    assert kept["method_ideas"][0]["verdict"] == "reject"
    assert [c["id"] for c in kept["means_conditions"]] == ["E1"]
    assert kept["planning_budget"]["active_minutes"] == 12


def test_a_beside_record_may_change_after_agreement_without_moving_the_digest(tmp_path):
    first = _digests(_sides(tmp_path, "one.toml", extra=BESIDE))

    second = _digests(_sides(
        tmp_path, "two.toml", extra=BESIDE.replace("tag from a cron job", "tag from a webhook")))

    assert first == second


def _stub(stdout="  [ 91] a-child-process-reads-its-parents-ambient-root.md\n", returncode=0):
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        return RunResult(returncode, stdout, "")

    run.calls = calls
    return run


def _search(store, ident="S1", query="release tagging", runner=None, tier="experience"):
    return cli_order.cmd_order_search(
        ns(session=SESSION, id=ident, tier=tier, query=query), store=store, runner=runner or _stub())


NONE_ENTRY = ('type_entry = { kind = "regularity", ref = "a-child-process-reads-its-parents-ambient-root" }',
              'type_entry = { kind = "none", search_id = "S1" }')


def test_order_search_records_the_query_and_results_in_the_session(ostore):
    _start(ostore)
    runner = _stub()

    d = _search(ostore, runner=runner)

    assert d.ok, d.detail
    [record] = ostore.load(SESSION).order_searches
    assert record["id"] == "S1" and record["tier"] == "experience" and record["query"] == "release tagging"
    assert record["results"]["leaves"] == ["a-child-process-reads-its-parents-ambient-root.md"]
    [argv] = runner.calls
    assert argv[1].endswith("record-experience.py") and argv[2:4] == ["search", "--tier"]
    assert argv[-1] == "release tagging"


def test_order_search_refuses_a_repeated_id_and_a_failed_search(ostore):
    _start(ostore)
    assert _search(ostore).ok

    again = _search(ostore)
    failed = _search(ostore, ident="S2", runner=_stub(returncode=2))

    assert again.ok is False and "already recorded" in again.detail
    assert failed.ok is False and "failed" in failed.detail
    assert [s["id"] for s in ostore.load(SESSION).order_searches] == ["S1"]


def test_a_none_type_entry_needs_a_search_recorded_in_this_session(ostore, tmp_path):
    _start(ostore)
    draft = _sides(tmp_path, "none.toml", old=NONE_ENTRY[0], new=NONE_ENTRY[1])

    refused = _submit(ostore, draft)
    assert refused.ok is False and any("order-search" in p for p in refused.data["problems"])

    assert _search(ostore).ok
    assert _submit(ostore, draft).ok


def test_a_type_entry_naming_no_record_is_refused(ostore, tmp_path):
    _start(ostore)

    d = _submit(ostore, _sides(tmp_path, "ghost.toml", old="a-child-process-reads-its-parents-ambient-root",
                               new="no-such-record-anywhere"))

    assert d.ok is False and any("no-such-record-anywhere" in p for p in d.data["problems"])


@pytest.mark.parametrize("pattern", ["*", "[a-z]*", "a-child-process-reads-its-parents-ambient-roo?"])
def test_a_type_entry_ref_is_a_name_not_a_pattern(ostore, tmp_path, pattern):
    _start(ostore)

    d = _submit(ostore, _sides(tmp_path, "glob.toml", old="a-child-process-reads-its-parents-ambient-root",
                               new=pattern))

    assert d.ok is False and any(pattern in p for p in d.data["problems"])
    assert not ostore.load(SESSION).order_draft_path


def test_a_two_sided_order_with_an_old_form_planning_estimate_is_refused(ostore, tmp_path):
    _start(ostore)
    both = _sides(tmp_path, "both.toml")
    both.write_text(both.read_text(encoding="utf-8").replace(
        "[meta.order]\n", "[meta.order]\n" + _LINES["planning_estimate"] + "\n", 1), encoding="utf-8")

    d = _submit(ostore, both)

    assert d.ok is False and any("planning_estimate" in p for p in d.data["problems"])


@pytest.mark.parametrize("decisions, expected", [
    pytest.param(["B9=keep"], "B9", id="unknown-id"),
    pytest.param(["B1=maybe"], "keep/drop", id="unknown-verdict"),
    pytest.param(["B1=keep", "B1=drop"], "twice", id="same-id-twice"),
])
def test_agreement_refuses_an_ill_formed_binding_decision(ostore, tmp_path, decisions, expected):
    path = _presented(ostore, tmp_path, sides=SIDES + BESIDE)
    assert _user_answers(_marker_of(path)) == 1

    d = cli_order.cmd_order_agree(
        ns(session=SESSION, note="", correction="", binding_decision=decisions), store=ostore)

    assert d.ok is False and expected in d.detail
    assert ostore.load(SESSION).order_agreement is None


def _revise(store, path, why="the way it is built changed"):
    return cli_order.cmd_order_executor_revise(
        ns(session=SESSION, plan=str(path), why=why, by="agent"), store=store)


def test_executor_revision_without_reagreement(ostore, tmp_path):
    _agree_two_sided(ostore, tmp_path)
    agreed = ostore.load(SESSION).order_agreement
    revised = _sides(tmp_path, "revised.toml",
                     replace=_replacing("proposals", "tag releases automatically", "tag on every merge"))

    d = _revise(ostore, revised)

    assert d.ok, d.detail
    state = ostore.load(SESSION)
    assert state.order_agreement.understanding_sha256 == agreed.understanding_sha256
    assert state.order_agreement.executor_side_sha256 == agreed.executor_side_sha256
    [record] = state.executor_side_revisions
    assert record["changed"] == ["P:P1"]
    assert record["earlier_executor_side_sha256"] == agreed.executor_side_sha256
    assert record["executor_side_sha256"] == plan.executor_side_digest(_doc(revised))
    assert record["agreed_understanding_sha256"] == agreed.understanding_sha256
    assert record["why"] == "the way it is built changed" and record["by"] == "agent"


def test_an_accepted_revision_leaves_the_session_describing_the_draft_in_force(ostore, tmp_path):
    _agree_two_sided(ostore, tmp_path, decisions=["B1=keep"], sides=SIDES + BESIDE)
    revised = _sides(
        tmp_path, "revised.toml", extra=BESIDE.replace("tag from a cron job", "tag from a webhook"),
        replace=_replacing("proposals", "tag releases automatically", "tag on every merge"))

    d = _revise(ostore, revised)

    assert d.ok, d.detail
    state = ostore.load(SESSION)
    assert state.order_draft_path == str(revised)
    assert state.order_beside == cli_order._beside_records(_doc(revised))
    assert state.order_beside["method_ideas"][0]["text"] == "tag from a webhook"


def test_executor_revision_of_the_customer_side_is_refused(ostore, tmp_path):
    _agree_two_sided(ostore, tmp_path)
    touching = _sides(tmp_path, "touching.toml", old="the command tags a release",
                      new="the command tags and signs a release",
                      replace=_replacing("proposals", "tag releases automatically", "tag on every merge"))

    d = _revise(ostore, touching)

    assert d.ok is False and "C:A1" in d.detail and "new agreement" in d.detail
    assert not ostore.load(SESSION).executor_side_revisions


@pytest.mark.parametrize("beside", [
    pytest.param(BESIDE.replace("no change to the release branch", "no change to any branch"),
                 id="condition-text"),
    pytest.param("", id="condition-dropped"),
])
def test_executor_revision_of_a_binding_condition_is_refused(ostore, tmp_path, beside):
    _agree_two_sided(ostore, tmp_path, decisions=["B1=keep"], sides=SIDES + BESIDE)
    state = ostore.load(SESSION)
    before = (state.order_draft_path, state.order_beside)
    revised = _sides(tmp_path, "revised.toml", extra=beside,
                     replace=_replacing("proposals", "tag releases automatically", "tag on every merge"))

    d = _revise(ostore, revised)

    assert d.ok is False and "binding condition" in d.detail
    state = ostore.load(SESSION)
    assert not state.executor_side_revisions
    assert (state.order_draft_path, state.order_beside) == before


def test_a_binding_condition_cannot_be_reworded_by_submitting_then_revising(ostore, tmp_path):
    _agree_two_sided(ostore, tmp_path, decisions=["B1=keep"], sides=SIDES + BESIDE)
    revised = _sides(tmp_path, "revised.toml", extra=BESIDE.replace(
        "no change to the release branch", "no change to any branch"),
        replace=_replacing("proposals", "tag releases automatically", "tag on every merge"))
    assert _submit(ostore, revised).ok

    d = _revise(ostore, revised)

    assert d.ok is False and "binding condition" in d.detail
    assert not ostore.load(SESSION).executor_side_revisions


def test_executor_revision_of_the_planning_budget_is_refused(ostore, tmp_path):
    _agree_two_sided(ostore, tmp_path)
    revised = _sides(tmp_path, "revised.toml", budget=BUDGET.replace("usd = 4.5", "usd = 9.0"),
                     replace=_replacing("proposals", "tag releases automatically", "tag on every merge"))

    d = _revise(ostore, revised)

    assert d.ok is False and "planning budget" in d.detail
    assert not ostore.load(SESSION).executor_side_revisions


def test_executor_revision_failing_the_executor_side_checks_is_refused(ostore, tmp_path):
    _agree_two_sided(ostore, tmp_path)
    unfit = _sides(tmp_path, "unfit.toml", old=SIDES.split("specifics = ")[1].split("\n")[0],
                   new='""')

    d = _revise(ostore, unfit)

    assert d.ok is False and "not fit" in d.detail
    assert not ostore.load(SESSION).executor_side_revisions


def test_executor_revision_that_changes_nothing_is_refused(ostore, tmp_path):
    path = _agree_two_sided(ostore, tmp_path)

    d = _revise(ostore, path)

    assert d.ok is False and "nothing to record" in d.detail


def test_executor_revision_needs_a_why_and_an_agreement(ostore, tmp_path):
    _start(ostore)
    path = _draft(tmp_path)

    assert _revise(ostore, path).ok is False
    assert "--why" in _revise(ostore, path, why=" ").detail


def test_the_revision_record_round_trips_through_the_state_store(ostore, tmp_path):
    _agree_two_sided(ostore, tmp_path)
    assert _revise(ostore, _sides(
        tmp_path, "revised.toml", replace=_replacing("constraints", "no new dependency", "no new service"))).ok

    again = ostore.load(SESSION)

    assert json.loads(json.dumps(again.executor_side_revisions))[0]["changed"] == ["K:K1"]
