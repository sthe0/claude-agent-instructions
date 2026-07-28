"""check_venue_warnings' schema-24 survivability warning (plan.py): a stage
that declares venue = "delivery" with no `verify_venue_at_final`, in a plan
that ALSO asserts landing somewhere (a `kind = "landed"` stage or
final_check), will REFUSE the moment verify-final re-runs it against a
now-removed delivery worktree. This lint surfaces that as an advisory,
never a blocker — a plan may legitimately keep its worktree past landing
(`land-branch.py --keep-branch`), so the condition is a strong signal, not
a proof.
"""
from argparse import Namespace

from agentctl import cli
from agentctl.plan import check_venue_warnings
from agentctl.state import (
    Actor,
    Criterion,
    FinalCheck,
    LandedSpec,
    Means,
    Stage,
    Subject,
)
from agentctl.store import FileStateStore


def ns(**kw):
    return Namespace(**kw)


def _stage(index=1, verify_command="pytest -q", verify_venue="delivery",
           verify_venue_at_final=None, verify_kind="shell", landed=None):
    return Stage(
        index=index, title="s%d" % index,
        subject=Subject(material="m", result="img"),
        means=Means(means="bash", method="run"),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(
            criterion_type="measurable", done_criterion="c",
            verify_command=verify_command, verify_venue=verify_venue,
            verify_venue_at_final=verify_venue_at_final,
            verify_kind=verify_kind, landed=landed,
        ),
    )


def _landed_final_check(delivered_stage=1):
    return FinalCheck(
        command="", kind="landed",
        landed=LandedSpec(target="main", delivered_stage=delivered_stage),
    )


def test_bare_delivery_stage_warns_when_plan_asserts_landing(tmp_path):
    repo_root = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    stage = _stage()
    warnings = check_venue_warnings(
        [stage], [_landed_final_check()], str(repo_root), str(worktree)
    )
    assert len(warnings) == 1
    assert "stage 1" in warnings[0]
    assert "verify_venue_at_final" in warnings[0]


def test_declared_verify_venue_at_final_silences_warning(tmp_path):
    repo_root = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    stage = _stage(verify_venue_at_final="repo_root")
    warnings = check_venue_warnings(
        [stage], [_landed_final_check()], str(repo_root), str(worktree)
    )
    assert warnings == []


def test_no_landed_assertion_anywhere_silences_warning(tmp_path):
    """The same bare delivery-venue stage that warns above is silent when
    nothing in the plan asserts landing — nothing declares the delivery
    venue is going away, so there is no survivability concern to flag."""
    repo_root = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    stage = _stage()
    assert check_venue_warnings([stage], [], str(repo_root), str(worktree)) == []


def test_repo_root_venue_stage_never_warns(tmp_path):
    repo_root = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    stage = _stage(verify_venue="repo_root")
    warnings = check_venue_warnings(
        [stage], [_landed_final_check()], str(repo_root), str(worktree)
    )
    assert warnings == []


def test_landed_stage_criterion_itself_never_warns(tmp_path):
    """A landed criterion's venue is always repo_root (V2 forbids
    verify_venue_at_final on it) — it is not the "bare delivery" shape this
    warning targets, even though it IS the thing that makes the plan
    'assert landing'."""
    repo_root = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    stage = _stage(
        verify_command=None, verify_venue="repo_root", verify_kind="landed",
        landed=LandedSpec(target="main", delivered_stage=1),
    )
    assert check_venue_warnings([stage], [], str(repo_root), str(worktree)) == []


def test_no_delivery_worktree_declared_silences_warning(tmp_path):
    """No second venue exists to survive or not — the early return shared
    with the pre-existing contradiction lint applies here too."""
    repo_root = tmp_path / "repo"
    stage = _stage()
    warnings = check_venue_warnings(
        [stage], [_landed_final_check()], str(repo_root), None
    )
    assert warnings == []


def test_submit_plan_attaches_survivability_advisory_without_blocking(tmp_path):
    """Integration: a plan that asserts landing via a `kind = "landed"`
    final_check and also carries a bare delivery-venue stage submits clean —
    the warning lands in advisories, never in ok/node/marker."""
    store = FileStateStore(tmp_path / "state")
    sid = "venue-lifecycle-lint"
    cli.cmd_start(ns(session=sid, task="demo", goal="g", done_criterion="dc",
                      criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                         wall_clock_min=60, tracker_key=None, architectural=True,
                         external_effect=False, new_dependency=False,
                         public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)

    repo_root = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    plan = tmp_path / "plan.toml"
    plan.write_text(
        f"""
[meta]
task_id = "demo"
goal = "g"
done_criterion = "dc"
criterion_type = "measurable"
weight_class = "substantive"
external_research = "checked wiki; none applies"
repo_root = "{repo_root}"
delivery_worktree = "{worktree}"

[[stage]]
index = 1
title = "Run the suite"
executor = "in_thread"
expected_result_image = "i"
criterion_type = "measurable"
done_criterion = "d"
verify_command = "pytest -q"
material = "m"
means = "bash"
method = "run"
conditions = "c"
invariants = "n"
capability_required = "cap"

[stage.principle]
statement = "s"
source = "s"
derivation = "d"
confidence = "high"
refutation = "r"

[[final_check]]
kind = "landed"
label = "trunk contains the delivered commit"

[final_check.landed]
target = "main"
remote = "origin"
delivered_stage = 1
""",
        encoding="utf-8",
    )
    d = cli.cmd_submit_plan(ns(session=sid, plan=str(plan)), store=store)

    assert d.ok is True
    from agentctl.state import Node
    assert d.node == Node.PLAN_READY.value
    assert d.marker == "PLAN-READY"
    advisories = d.data.get("advisories", [])
    assert any("stage 1" in a and "verify_venue_at_final" in a for a in advisories)
