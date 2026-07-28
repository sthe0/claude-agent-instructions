"""Stage 3 of the landed-check plan: close the correction path.

Stage 1 (schema) and stage 2 (synthesizer + verify-site wiring) made
`kind = "landed"` a typed, engine-executed check. This stage proves the
correction machinery actually SEES it: a landed-field-only edit, or a
venue-only edit, must classify as `diff_plans` "refinement" (not silently
dropped as "no_change") and the same fields must move `stage_carry_key`,
`stage_question_key` and `gates._operative_surface` — the three places that
decide PASSED carry-forward, premise-question invalidation, and replan
coverage. Also regression-locks two latent omissions found while landing
this plan: `_prose`/`_fc` never compared `verify_venue`/`fc.venue` at all, so
a venue-only correction on ANY check (shell or landed) was silently dropped.
"""
from agentctl.gates import _operative_surface
from agentctl.plan import PlanDoc, PlanMeta, diff_plans, parse_plan, stage_carry_key, stage_question_key
from agentctl.state import Actor, Criterion, Means, Outcome, Stage, StageStatus, Subject


def _stage_dict(index=1, **overrides):
    base = {
        "index": index, "title": "s", "executor": "in_thread",
        "expected_result_image": "img", "done_criterion": "dc",
        "means": "Edit", "method": "do",
    }
    base.update(overrides)
    return base


def _doc(stages, final_check=None) -> PlanDoc:
    data = {"meta": {"task_id": "t"}, "stage": stages}
    if final_check is not None:
        data["final_check"] = final_check
    return parse_plan(data)


def _landed_table(**overrides):
    base = {"target": "main", "remote": "origin", "delivered_stage": 1}
    base.update(overrides)
    return base


def _direct_stage(*, verify_venue="delivery", verify_kind="shell", landed=None):
    """Bypasses parse_plan, whose exact-match vocabulary check makes a
    whitespace/case variant of venue or kind unreachable via valid TOML."""
    return Stage(
        index=1, title="s",
        subject=Subject(material="m", result="img"),
        means=Means(means="Edit", method="do"),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(
            criterion_type="measurable", done_criterion="dc",
            verify_venue=verify_venue, verify_kind=verify_kind, landed=landed,
        ),
        outcome=Outcome(status=StageStatus.PENDING.value),
    )


def _direct_doc(stages) -> PlanDoc:
    return PlanDoc(meta=PlanMeta(task_id="t"), stages=stages)


# --- a landed-field-only edit is a refinement --------------------------------

def test_diff_stage_landed_target_only_edit_is_refinement():
    old = _doc([_stage_dict(verify_kind="landed", landed=_landed_table())])
    new = _doc([_stage_dict(verify_kind="landed", landed=_landed_table(target="release"))])
    assert diff_plans(old, new) == "refinement"


def test_diff_final_check_landed_target_only_edit_is_refinement():
    old = _doc([_stage_dict()], final_check=[{"kind": "landed", "landed": _landed_table()}])
    new = _doc([_stage_dict()],
                final_check=[{"kind": "landed", "landed": _landed_table(target="release")}])
    assert diff_plans(old, new) == "refinement"


# --- a venue-only edit is a refinement (regression: was silently dropped) ---

def test_diff_stage_verify_venue_only_edit_is_refinement():
    old = _doc([_stage_dict(verify_venue="delivery")])
    new = _doc([_stage_dict(verify_venue="repo_root")])
    assert diff_plans(old, new) == "refinement"


def test_diff_final_check_venue_only_edit_is_refinement():
    old = _doc([_stage_dict()], final_check=[{"command": "true", "venue": "delivery"}])
    new = _doc([_stage_dict()], final_check=[{"command": "true", "venue": "repo_root"}])
    assert diff_plans(old, new) == "refinement"


# --- the structural signature stays untouched by any of the above -----------

def test_venue_and_landed_edits_never_go_substantive():
    old = _doc([_stage_dict(verify_kind="landed", landed=_landed_table())])
    new = _doc([_stage_dict(verify_kind="landed", landed=_landed_table(target="release"))])
    assert diff_plans(old, new) != "substantive"


# --- the same fields move carry-key / question-key / operative-surface ------

def test_landed_field_change_alters_stage_carry_key():
    old = _doc([_stage_dict(verify_kind="landed", landed=_landed_table())]).stages[0]
    new = _doc([_stage_dict(verify_kind="landed", landed=_landed_table(target="release"))]).stages[0]
    assert stage_carry_key(old) != stage_carry_key(new)


def test_landed_field_change_alters_stage_question_key():
    old = _doc([_stage_dict(verify_kind="landed", landed=_landed_table())]).stages[0]
    new = _doc([_stage_dict(verify_kind="landed", landed=_landed_table(target="release"))]).stages[0]
    assert stage_question_key(old) != stage_question_key(new)


def test_landed_field_change_alters_operative_surface():
    old = _doc([_stage_dict(verify_kind="landed", landed=_landed_table())])
    new = _doc([_stage_dict(verify_kind="landed", landed=_landed_table(target="release"))])
    assert _operative_surface(old) != _operative_surface(new)


def test_stage_venue_change_alters_carry_key_and_question_key_and_surface():
    old = _doc([_stage_dict(verify_venue="delivery")])
    new = _doc([_stage_dict(verify_venue="repo_root")])
    assert stage_carry_key(old.stages[0]) != stage_carry_key(new.stages[0])
    assert stage_question_key(old.stages[0]) != stage_question_key(new.stages[0])
    assert _operative_surface(old) != _operative_surface(new)


# --- a whitespace/case-only edit alters none of them -------------------------

def test_whitespace_only_venue_edit_alters_neither_key():
    a, b = _direct_stage(verify_venue="delivery"), _direct_stage(verify_venue="  Delivery  ")
    assert stage_carry_key(a) == stage_carry_key(b)
    assert stage_question_key(a) == stage_question_key(b)


def test_whitespace_only_venue_edit_does_not_alter_operative_surface():
    a, b = _direct_doc([_direct_stage(verify_venue="delivery")]), _direct_doc([_direct_stage(verify_venue="  Delivery  ")])
    assert _operative_surface(a) == _operative_surface(b)


def test_whitespace_only_venue_edit_is_no_change():
    a, b = _direct_doc([_direct_stage(verify_venue="delivery")]), _direct_doc([_direct_stage(verify_venue="  Delivery  ")])
    assert diff_plans(a, b) == "no_change"


def test_whitespace_only_kind_edit_alters_neither_key():
    a, b = _direct_stage(verify_kind="shell"), _direct_stage(verify_kind="  Shell  ")
    assert stage_carry_key(a) == stage_carry_key(b)
    assert stage_question_key(a) == stage_question_key(b)


# --- writing a default out explicitly diffs as no_change (parsed value, ------
# --- not a presence bit) ------------------------------------------------------

def test_stage_venue_default_written_out_explicitly_is_no_change():
    old = _doc([_stage_dict()])  # verify_venue omitted -> defaults to "delivery"
    new = _doc([_stage_dict(verify_venue="delivery")])  # same default, spelled out
    assert diff_plans(old, new) == "no_change"
    assert stage_carry_key(old.stages[0]) == stage_carry_key(new.stages[0])
    assert stage_question_key(old.stages[0]) == stage_question_key(new.stages[0])
    assert _operative_surface(old) == _operative_surface(new)


def test_final_check_kind_default_written_out_explicitly_is_no_change():
    old = _doc([_stage_dict()], final_check=[{"command": "true"}])  # kind omitted -> "shell"
    new = _doc([_stage_dict()], final_check=[{"command": "true", "kind": "shell"}])
    assert diff_plans(old, new) == "no_change"
    assert _operative_surface(old) == _operative_surface(new)
