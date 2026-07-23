"""replan_coverage_blockers' CHANGE half must be satisfied by any change to the
plan's operative surface (what the engine executes or dispatches on) — not only
by a means/method edit — and must NOT be satisfied by a whitespace-or-case-only
rephrasing of means/method. Regression lock for the landed 06b43bc correction,
whose whole semantic content lived in verify_command/[[final_check]]/[meta] and
which the pre-widening gate reported as "no stage means/method changed"."""
from agentctl import gates
from agentctl.plan import parse_plan
from agentctl.state import Critique


def _stage(index, *, means="Edit", method="do", verify_command=None, expected_exit=0,
           verify_venue=None, executor="in_thread", conditions=None, invariants=None):
    s = {
        "index": index, "title": "s", "executor": executor,
        "expected_result_image": "img", "done_criterion": "dc",
        "means": means, "method": method,
    }
    if verify_command is not None:
        s["verify_command"] = verify_command
    if expected_exit != 0:
        s["expected_exit"] = expected_exit
    if verify_venue is not None:
        s["verify_venue"] = verify_venue
    if conditions is not None:
        s["conditions"] = conditions
    if invariants is not None:
        s["invariants"] = invariants
    return s


def _doc(stages, *, final_check=None, repo_root=None, delivery_worktree=None):
    meta = {"task_id": "t"}
    if repo_root is not None:
        meta["repo_root"] = repo_root
    if delivery_worktree is not None:
        meta["delivery_worktree"] = delivery_worktree
    data = {"meta": meta, "stage": stages}
    if final_check is not None:
        data["final_check"] = final_check
    return parse_plan(data)


def _critique(**kw):
    base = dict(functional_ground="fg", replanning_task="rt",
                invariants_to_preserve=[], differences_to_remove=["some difference"])
    base.update(kw)
    return Critique(**base)


# --- false-block direction: a non-means/method correction still satisfies CHANGE ---

def test_verify_command_only_change_satisfies_change_half():
    old = _doc([_stage(1, verify_command="pytest -q")])
    new = _doc([_stage(1, verify_command="pytest scripts/tests/test_check_venue.py -q")])
    assert gates.replan_coverage_blockers(old, new, _critique()) == []


def test_final_check_only_change_satisfies_change_half():
    old = _doc([_stage(1)], final_check=[{"command": "pytest -q", "expected_exit": 0}])
    new = _doc([_stage(1)], final_check=[{"command": "pytest scripts/tests -q", "expected_exit": 0}])
    assert gates.replan_coverage_blockers(old, new, _critique()) == []


def test_meta_venue_change_satisfies_change_half():
    old = _doc([_stage(1)], repo_root="/repo", delivery_worktree=None)
    new = _doc([_stage(1)], repo_root="/repo", delivery_worktree="/repo/.claude/worktrees/x")
    assert gates.replan_coverage_blockers(old, new, _critique()) == []


# --- false-pass direction: cosmetic means/method edits no longer satisfy CHANGE ---

def test_whitespace_only_means_edit_no_longer_satisfies_change_half():
    old = _doc([_stage(1, means="Edit", method="do")])
    new = _doc([_stage(1, means="  Edit  ", method="do")])
    blockers = gates.replan_coverage_blockers(old, new, _critique())
    assert blockers and "operative surface" in blockers[0]


def test_case_only_means_edit_no_longer_satisfies_change_half():
    old = _doc([_stage(1, means="Edit", method="do")])
    new = _doc([_stage(1, means="EDIT", method="DO")])
    blockers = gates.replan_coverage_blockers(old, new, _critique())
    assert blockers and "operative surface" in blockers[0]


# --- non-vacuity: rewriting prose alone never satisfies CHANGE -------------------

def test_prose_only_edit_does_not_satisfy_change_half():
    old = _doc([_stage(1)])
    new = _doc([_stage(1, conditions="a wholly rewritten narrative that changes nothing executable")])
    blockers = gates.replan_coverage_blockers(old, new, _critique())
    assert blockers and "operative surface" in blockers[0]


# --- PRESERVE half is untouched by this widening ----------------------------------

def test_preserve_half_unchanged_for_identical_inputs():
    old = _doc([_stage(1)])
    new = _doc([_stage(1, conditions="keep idempotency")])
    crit = _critique(invariants_to_preserve=["keep idempotency"], differences_to_remove=[])
    assert gates.replan_coverage_blockers(old, new, crit) == []
