"""Delta re-review: after a pass on a topo row whose parts then changed, only a concern
that cites a changed part (or re-raises an open blocker) stays blocking, and the re-review
bundle carries the prior verdict and a textual diff of the row's parts.

A first review, a revise after a revise and a revise on a current row keep their
existing severity rules. The module's fixtures come from the pair-currency tests; every
agentctl entry point is reached inside the test bodies, so a test fails on an assertion
on a tree without the behaviour rather than at collection."""
from __future__ import annotations

import hashlib
from argparse import Namespace

import pytest

from agentctl import cli, gates
from agentctl.dispatch import RunResult
from agentctl.plan import change_tier, load_plan
from test_norm_staleness_pair_currency import (  # noqa: F401
    SID, _set, _sha, env, make_env,
)

PAIR = "3-1"
UNIT = "unit:3"
STAGE3_EDIT = _set(2, method="a rewritten method for stage three")


def _record(env, scope, verdict, concerns=None, *, regression_command=None, runner=None):
    return cli.cmd_plan_review(
        Namespace(session=SID, target=None, scope=f"topo:{scope}", verdict=verdict,
                  reviewer="thinker", concerns=concerns, concern_ids=None, note="",
                  plan_digest=_sha(env.plan), regression_command=regression_command),
        store=env.store, runner=runner)


def _snapshot_current(env) -> str:
    data = env.plan.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    cli._write_plan_version_snapshot(env.store, env.state(), data, digest)
    return digest


def _history(env, scope):
    d = cli.cmd_plan_review_pair_history(
        Namespace(session=SID, target=str(env.plan), pair=scope), store=env.store)
    assert d.ok, d.detail
    return d.data


def _bundle(env, scope):
    from agentctl.render import render_pair_review_bundle, render_unit_review_bundle
    doc, history = env.doc(), _history(env, scope)
    if scope.startswith("unit:"):
        return render_unit_review_bundle(doc, scope, plan_sha256=_sha(env.plan), history=history)
    return render_pair_review_bundle(
        doc, scope, plan_sha256=_sha(env.plan), view_dir="/view", history=history)


def _downgrade_events(env, scope):
    return [e for e in env.state().history
            if e.get("event") == "plan_review_concerns_downgraded"
            and e.get("scope", "").endswith(scope)]


def _rounds(env) -> int:
    return env.state().review_rounds


def _assert_composes_as_pass(env, scope, own_part):
    assert _record(env, scope, "pass").ok
    before = _rounds(env)
    env.edit(STAGE3_EDIT)
    d = _record(env, scope, "revise", ["blocking: C1: the wording is vague in places"])
    assert d.ok, d.detail
    record = env.state().plan_pair_reviews[scope]
    assert record.concerns == ["C1: the wording is vague in places"]
    assert record.effective_severities == [gates.EFFECTIVE_ADVISORY]
    assert env.status(scope) in gates.PAIR_SATISFIED
    events = _downgrade_events(env, scope)
    assert events and own_part in str(events[-1]), events
    assert _rounds(env) - before == 1, "a downgraded revise counts one round, like a pass"


def test_topological_revise_citing_no_changed_part_composes_as_pass(env):
    _assert_composes_as_pass(env, PAIR, "stage:3")


def test_unit_revise_citing_no_changed_part_composes_as_pass(env):
    _assert_composes_as_pass(env, UNIT, "stage:3")


def test_rereview_bundle_carries_the_diff_and_prior_verdict(env):
    assert _record(env, PAIR, "pass").ok
    _snapshot_current(env)
    env.edit(STAGE3_EDIT)

    bundle = _bundle(env, PAIR)

    assert "## Prior review of this pair" in bundle and "verdict `pass`" in bundle
    diff = bundle.split("## What changed in this pair since the prior verdict", 1)[1]
    assert any(line.startswith("-") and "method-3" in line for line in diff.splitlines())
    assert any(line.startswith("+") and "a rewritten method for stage three" in line
               for line in diff.splitlines())
    assert "right after its marker" in bundle


def test_part_token_after_the_marker_names_a_changed_part_and_blocks(env):
    assert _record(env, PAIR, "pass").ok
    env.edit(STAGE3_EDIT)
    d = _record(env, PAIR, "revise", ["blocking: C1: stage:3 the method contradicts its means"])
    assert d.ok, d.detail
    record = env.state().plan_pair_reviews[PAIR]
    assert record.verdict == "revise"
    assert record.effective_severities == [gates.EFFECTIVE_BLOCKING]
    assert gates._concern_part_token("C1: stage:3 text") == "stage:3"
    assert gates._concern_part_token("stage:3 C1: text") == "stage:3"
    assert gates._concern_names_part("C2: stage:3 text", "stage:3")


def test_carried_pass_counts_as_the_rows_latest_pass(env):
    from agentctl import carry_forward

    assert _record(env, PAIR, "pass").ok
    judged = _snapshot_current(env)
    for tier in ("medium", "small"):
        old = env.doc()
        env.edit(_set(2, cost_tier=tier))
        new = env.doc()
        state = env.state()
        carry_forward.apply(state, old, new, new_path=str(env.plan), new_sha=_sha(env.plan),
                            change=change_tier(old, new))
        env.store.save(state)
        _snapshot_current(env)
    carried = env.state().plan_pair_reviews[PAIR]
    assert carried.carried_tier == 1 and carried.carried_from_sha == judged
    assert env.status(PAIR) in gates.PAIR_SATISFIED

    env.edit(lambda d: (d["stage"][2].update(cost_tier="small"), STAGE3_EDIT(d)))
    diff = _history(env, PAIR)["prior_diff"]
    assert diff["available"] and diff["from_sha"] == judged
    lines = diff["text"].splitlines()
    assert any(line.startswith("+") and "Cost tier" in line and "small" in line for line in lines)

    d = _record(env, PAIR, "revise", ["blocking: C1: an unrelated worry"])
    assert d.ok, d.detail
    assert env.state().plan_pair_reviews[PAIR].effective_severities == [gates.EFFECTIVE_ADVISORY]


def test_first_review_revise_needs_no_citation(env):
    d = _record(env, PAIR, "revise", ["blocking: C1: nothing cites a part"])
    assert d.ok, d.detail
    record = env.state().plan_pair_reviews[PAIR]
    assert record.verdict == "revise"
    assert record.effective_severities == [gates.EFFECTIVE_BLOCKING]


def test_revise_after_a_prior_revise_may_cite_its_open_blocker(env):
    assert _record(env, PAIR, "revise", ["blocking: C1: stage:3 is under-specified"]).ok
    first = env.state().plan_pair_reviews[PAIR]
    blocker = first.concern_ids[0]
    env.edit(STAGE3_EDIT)
    d = _record(env, PAIR, "revise", [f"blocking: re:{blocker} C1: stage:3 still under-specified"])
    assert d.ok, d.detail
    record = env.state().plan_pair_reviews[PAIR]
    assert record.verdict == "revise"
    assert record.effective_severities == [gates.EFFECTIVE_BLOCKING]
    d = _record(env, PAIR, "revise", ["blocking: C1: a different complaint without a part"])
    assert d.ok, d.detail
    assert env.state().plan_pair_reviews[PAIR].effective_severities == [gates.EFFECTIVE_BLOCKING]


def test_revise_on_a_current_row_keeps_the_regression_evidence_rule(env):
    assert _record(env, PAIR, "pass").ok
    d = _record(env, PAIR, "revise", ["blocking: C1: regressed"])
    assert not d.ok and "--regression-command" in d.detail
    assert env.status(PAIR) == "current"
    d = _record(env, PAIR, "revise", ["blocking: C1: regressed"], regression_command="check",
                runner=lambda argv: RunResult(returncode=1))
    assert d.ok, d.detail
    assert env.status(PAIR) == "revise"


def test_missing_snapshot_bundle_says_so(env):
    assert _record(env, PAIR, "pass").ok
    env.edit(STAGE3_EDIT)

    diff = _history(env, PAIR)["prior_diff"]
    bundle = _bundle(env, PAIR)

    assert diff["available"] is False and diff["text"] == ""
    assert "prior version is unavailable" in bundle
    assert "judge the pair whole" in bundle
