"""Stage 3 of norm-staleness: the AcceptanceReview is bound per requirement to the
deliverables it was recorded against, the staleness is reported at every replan, and a
replan that moves no grant keeps the grant set the user approved (#338).

Two difficulties, one file because both are "a record outlived the bytes it was written
against, and the engine compared the wrong thing":

  * ACCEPTANCE. The resolution gate compared `review.plan_sha256` to the digest of the
    whole accepted plan, so a control-only replan -- a verify command, a method -- made
    an accepted order read as unaccepted and forced a re-run of `accept` over deliverables
    nobody had touched. The binding is now per requirement: its text, and for each
    coverage entry the deliverable it names (a stage's interface token, a final check's
    identity). Stale iff a requirement the review accepted moved, or the plan declares one
    the review lacks. A review written before the binding existed (`requirement_bindings`
    is None) keeps the raw digest comparison.
  * GRANTS. `_stage_grant_entries` re-derived the derived grants at every dispatch, from
    engine code and the venue's filesystem as they were THEN, so a `__init__.py` landing in
    the venue after approval silently withdrew an approved grant (or, after a refinement
    replan re-hashed against the new filesystem, silently re-approved a narrower set). The
    set bound at approve is now materialized and read back; the re-derive-and-compare
    remains only for a session bound before entries were stored.

Every test drives an entry point that exists on the base tree (`cmd_replan`, `cmd_accept`,
`gates._acceptance_review_resolution_blockers`, `_stage_grant_entries`) and names new
behaviour only by what it observes, so on the base each one fails on its assertion rather
than on a missing symbol."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentctl import cli, gates
from agentctl.dispatch import RunResult

import test_renormalization as rn
from conftest import SUBSTANTIVE_FINAL_CHECK, SUBSTANTIVE_ORDER
from test_stage_grants import _to_executing, ns


_ORDER = """
[meta.order]
customer_id = "user"
customer = "the position that posed this fixture's task"
functional_place = "the norm governing an act of activity, in a test"

[[meta.order.requirements]]
id = "R1"
text = "stage one delivers the field"
derivation = "fixture-derivation"

[[meta.order.requirements]]
id = "R2"
text = "stage two delivers the refusal"
derivation = "fixture-derivation"

[[meta.order.requirements]]
id = "R3"
text = "the end-to-end check passes"
derivation = "fixture-derivation"

[meta.order.coverage]
R1 = ["stage 1 verify_command"]
R2 = ["stage 2 verify_command"]
R3 = ["final_check 1"]
"""

_FINAL_CHECK = """
[[final_check]]
command = "true"
expected_exit = 0
"""


def _render(path: Path, *, order: str = _ORDER, final_check: str = _FINAL_CHECK,
            stage2_verify: str = "pytest -q", **over) -> str:
    """The two-stage template of test_renormalization, with a three-requirement order
    (one requirement per deliverable kind: stage 1, stage 2, the final check)."""
    fields = dict(rn._DEFAULTS)
    fields.update(over)
    for key in ("method", "done_criterion", "result", "title"):
        fields[key] = json.dumps(fields[key])
    text = rn._PLAN.replace(SUBSTANTIVE_ORDER, order).replace(
        SUBSTANTIVE_FINAL_CHECK, final_check,
    ).replace('material = "m2 (traces to R1)"', 'material = "m2 (traces to R2)"')
    text = text.replace('verify_command = "pytest -q"\nnegative_control = "false"\n'
                        'material = "m2',
                        f'verify_command = {json.dumps(stage2_verify)}\n'
                        'negative_control = "false"\nmaterial = "m2')
    path.write_text(text.format(**fields), encoding="utf-8")
    return str(path)


def _judge_yes(argv, *, timeout=None, stdin=""):
    return RunResult(0, stdout="YES\nconcrete and adequate", stderr="")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")
    monkeypatch.delenv("AGENTCTL_ACCEPTANCE", raising=False)


def _accepted(store, tmp_path, sid="ns", **plan_over):
    """An approved session whose order has been accepted in full against the plan."""
    plan = _render(tmp_path / "v1.toml", **plan_over)
    rn._approved(store, plan, session=sid)
    d = cli.cmd_accept(
        ns(session=sid, author="user", verdict=["R1|pass", "R2|pass", "R3|pass"],
           note="compared the delivered engine against each requirement",
           bypass=False, bypass_reason=""),
        store=store, runner=_judge_yes,
    )
    assert d.ok is True, d.detail
    return plan


def _replan(store, plan_path, sid="ns", **flags):
    return cli.cmd_replan(
        ns(session=sid, plan=plan_path, renormalize=flags.get("renormalize", False),
           coverage_waiver=None, normalization_waiver=None, cost_log=None),
        store=store, runner=rn._judge_unavailable,
    )


def _acceptance_blockers(store, sid="ns") -> list[str]:
    return gates._acceptance_review_resolution_blockers(store.load(sid))


# --- ACCEPTANCE: a control-only edit moves no binding ------------------------


def test_a_control_only_replan_keeps_the_acceptance_current(store, tmp_path):
    """The defect, whole: stage 2's verify command is a control, not a deliverable. The
    plan's digest moves, no requirement's binding does, and the accepted order must read
    as accepted -- not as 'no AcceptanceReview recorded'."""
    _accepted(store, tmp_path)
    assert _acceptance_blockers(store) == []

    d = _replan(store, _render(tmp_path / "v2.toml", stage2_verify="pytest -q -x"))

    assert d.ok is True, d.detail
    assert d.data.get("acceptance_stale") == []
    assert _acceptance_blockers(store) == []


def test_a_moved_result_image_stales_only_the_requirement_covered_by_that_stage(
    store, tmp_path,
):
    """Stage 2's result image is part of its interface; R2 covers stage 2. R1 (stage 1)
    and R3 (the final check) are accepted against deliverables nobody touched."""
    _accepted(store, tmp_path)

    d = _replan(store, _render(tmp_path / "v2.toml",
                               result="The seam refuses a plan restating its requirement, with a message."))

    assert d.ok is True, d.detail
    replans = [h for h in store.load("ns").history if h.get("event") == "replan"]
    assert replans and replans[-1]["kind"] == "refinement", replans
    assert d.data.get("acceptance_stale") == ["R2"]
    blockers = _acceptance_blockers(store)
    assert blockers and "R2" in blockers[0] and "R1" not in blockers[0]


def test_a_moved_requirement_text_stales_that_requirement(store, tmp_path):
    _accepted(store, tmp_path)
    reworded = _ORDER.replace("stage one delivers the field",
                              "stage one delivers the field and its default")

    d = _replan(store, _render(tmp_path / "v2.toml", order=reworded))

    assert d.data.get("acceptance_stale") == ["R1"]


def test_a_requirement_the_review_lacks_is_stale_not_vacuously_accepted(store, tmp_path):
    _accepted(store, tmp_path)
    grown = _ORDER.replace(
        "[meta.order.coverage]",
        '[[meta.order.requirements]]\nid = "R4"\ntext = "a late requirement"\n'
        'derivation = "fixture-derivation"\n\n[meta.order.coverage]',
    ).replace('R3 = ["final_check 1"]', 'R3 = ["final_check 1"]\nR4 = ["stage 2 verify_command"]')

    d = _replan(store, _render(tmp_path / "v2.toml", order=grown))

    assert d.data.get("acceptance_stale") == ["R4"]


def test_a_final_check_edited_in_its_command_only_keeps_its_requirement_accepted(
    store, tmp_path,
):
    """A final check is bound on its identity -- label, kind, venue, expected exit,
    landed -- not on the command that does the checking: R3 stays accepted when only the
    command is rewritten, and goes stale when the expected exit (what the check asserts)
    moves."""
    _accepted(store, tmp_path)

    same_identity = _replan(store, _render(
        tmp_path / "v2.toml",
        final_check='\n[[final_check]]\ncommand = "test -d ."\nexpected_exit = 0\n'))
    assert same_identity.data.get("acceptance_stale") == []

    moved = _replan(store, _render(
        tmp_path / "v3.toml",
        final_check='\n[[final_check]]\ncommand = "test -d ."\nexpected_exit = 1\n'))
    assert moved.data.get("acceptance_stale") == ["R3"]


def test_a_failed_requirement_still_blocks_after_a_control_only_replan(store, tmp_path):
    """Keeping the acceptance current must not launder its verdicts: a requirement the
    customer verdicted `fail` is blocked by name both before and after a replan that
    moves no binding -- not by the review having gone stale (which re-accepting would
    clear), and not by nothing."""
    plan = _render(tmp_path / "v1.toml")
    rn._approved(store, plan, session="ns")
    d = cli.cmd_accept(
        ns(session="ns", author="user", verdict=["R1|pass", "R2|fail", "R3|pass"],
           note="stage two does not refuse a restated requirement", bypass=False,
           bypass_reason=""),
        store=store, runner=_judge_yes,
    )
    assert d.ok is True, d.detail
    before = _acceptance_blockers(store)
    assert before and "non-pass" in before[0] and "R2" in before[0], before

    d = _replan(store, _render(tmp_path / "v2.toml", stage2_verify="pytest -q -x"))

    assert d.ok is True, d.detail
    assert d.data.get("acceptance_stale") == []
    after = _acceptance_blockers(store)
    assert after and "non-pass" in after[0] and "R2" in after[0], after


def test_accept_refuses_a_plan_file_edited_in_place_after_it_was_accepted(store, tmp_path):
    """`accept` binds requirement digests from the plan it reads. The engine accepted the
    bytes it digested at approve; a file rewritten in place since is not that plan, and a
    review stamped with the accepted digest over bindings computed from other bytes
    would read as current for a deliverable nobody saw."""
    plan = _render(tmp_path / "v1.toml")
    rn._approved(store, plan, session="ns")
    _render(tmp_path / "v1.toml",
            result="The seam refuses a plan restating its requirement, with a message.")

    d = cli.cmd_accept(
        ns(session="ns", author="user", verdict=["R1|pass", "R2|pass", "R3|pass"],
           note="compared the delivered engine against each requirement",
           bypass=False, bypass_reason=""),
        store=store, runner=_judge_yes,
    )

    assert d.ok is False
    assert "edited since it was accepted" in d.detail
    assert store.load("ns").acceptance_review is None


def test_an_acceptance_never_reads_current_over_a_plan_file_edited_in_place(
    store, tmp_path,
):
    """The gate half: the review was recorded honestly, the file was then rewritten under
    the same accepted digest (no replan ran), moving the result image R2 is covered by.
    The digest the engine holds still equals the review's, so only comparing the bytes
    the gate reads to that digest tells the two plans apart."""
    plan = _accepted(store, tmp_path)
    assert _acceptance_blockers(store) == []

    _render(Path(plan), result="The seam refuses a plan restating its requirement, with a message.")

    blockers = _acceptance_blockers(store)
    assert blockers and "stale" in blockers[0], blockers


def test_a_review_without_bindings_keeps_the_raw_digest_comparison(store, tmp_path):
    """A review recorded before the binding existed carries no per-requirement digests;
    it must neither crash nor be waved through: any plan move still stales it, as it did."""
    _accepted(store, tmp_path)
    state = store.load("ns")
    state.acceptance_review.requirement_bindings = None
    store.save(state)
    assert _acceptance_blockers(store) == []

    _replan(store, _render(tmp_path / "v2.toml", stage2_verify="pytest -q -x"))

    blockers = _acceptance_blockers(store)
    assert blockers and "stale" in blockers[0]


# --- ACCEPTANCE: every replan kind reports ------------------------------------


def test_a_no_change_replan_reports_the_acceptance_it_leaves_standing(store, tmp_path):
    plan = _accepted(store, tmp_path)

    d = _replan(store, plan)

    assert d.ok is True
    assert d.data.get("acceptance_stale") == []


def test_a_renormalize_replan_reports_the_acceptance_it_leaves_standing(store, tmp_path):
    """A renormalization touches only the procedure, so it can never move a binding: it
    must say so (an empty list) rather than stay silent about an acceptance that exists."""
    _accepted(store, tmp_path)
    renormed = _render(
        tmp_path / "v2.toml",
        procedure='procedure = "1. Read the engine. 2. Extend the seam. 3. Run the suite."\n')

    d = _replan(store, renormed, renormalize=True)

    assert d.ok is True, d.detail
    assert d.data.get("acceptance_stale") == []
    assert _acceptance_blockers(store) == []


def test_a_replan_without_an_acceptance_review_says_nothing_about_one(store, tmp_path):
    rn._approved(store, _render(tmp_path / "v1.toml"), session="ns")

    d = _replan(store, _render(tmp_path / "v2.toml", stage2_verify="pytest -q -x"))

    assert d.ok is True
    assert "acceptance_stale" not in d.data


# --- GRANTS (#338): the approved set is bound, not re-derived -----------------


def _derived(state, stage=1):
    declared, derived, dropped, error, note = cli._stage_grant_entries(state, stage)
    return derived, error


def test_dispatch_reads_the_approved_derived_grants_not_the_venue_as_it_is_now(
    store, fixtures_dir, tmp_path, monkeypatch,
):
    """DR-O proposes `python3 mod.py` for an output artifact unless the venue has an
    `__init__.py` next to it -- a fact about the filesystem at derivation time. Approval
    bound the grant; a package marker appearing afterwards must not withdraw it."""
    monkeypatch.chdir(tmp_path)
    sid = "grants-venue-drift"
    _to_executing(store, sid, fixtures_dir)
    approved, error = _derived(store.load(sid))
    assert error is None
    assert any("mod.py" in e["rule"] for e in approved), approved

    (tmp_path / "__init__.py").write_text("")

    after, error = _derived(store.load(sid))
    assert error is None, error
    assert after == approved


def test_a_refinement_replan_that_moves_no_grant_keeps_the_approved_set(
    store, fixtures_dir, tmp_path, monkeypatch,
):
    """The refinement branch re-binds the snapshot; against a venue that has since
    changed, re-hashing used to approve the NARROWER re-derived set under the old user
    approval. A replan whose old and new grant hashes are equal keeps what was approved."""
    monkeypatch.chdir(tmp_path)
    sid = "grants-refine-drift"
    _to_executing(store, sid, fixtures_dir)
    approved, _ = _derived(store.load(sid))
    assert approved

    (tmp_path / "__init__.py").write_text("")
    d = cli.cmd_replan(ns(session=sid, plan=str(fixtures_dir / "plan_two_stage_refined.toml")),
                       store=store)
    assert d.action == "continue", d.detail

    after, error = _derived(store.load(sid))
    assert error is None, error
    assert after == approved


_ARTIFACT_LINE = {1: 'output_artifacts = ["mod.py"]\n',
                  2: 'output_artifacts = ["tests/test_mod.py"]\n'}


_WIDE_VERIFY = "pytest -q && git status"
_NARROW_VERIFY = "pytest -q"
_GIT_STATUS = "Bash(git status:*)"
_RUN_MOD = "Bash(python3 mod.py:*)"


def _two_stage_verifying(fixtures_dir, path: Path, stage: int, command: str) -> str:
    """`plan_two_stage.toml` with a verify_command on one stage. Rewriting only a
    verify_command to a subset of the segments it ran is the refinement that moves a
    stage's derived grants (a declared `[stage.grants]` edit is substantive)."""
    text = (fixtures_dir / "plan_two_stage.toml").read_text(encoding="utf-8")
    text = text.replace(_ARTIFACT_LINE[stage],
                        f'verify_command = {json.dumps(command)}\nexpected_exit = 0\n'
                        + _ARTIFACT_LINE[stage], 1)
    path.write_text(text, encoding="utf-8")
    return str(path)


def _marker_then_drift(store, fixtures_dir, tmp_path, monkeypatch, *, narrowed_stage: int):
    """Approve with a package marker in the venue (so DR-O derives no
    `python3 mod.py` for stage 1), remove it, then replan a refinement that only
    narrows `narrowed_stage`'s verify_command. Returns (sid, state before)."""
    monkeypatch.chdir(tmp_path)
    marker = tmp_path / "__init__.py"
    marker.write_text("")
    sid = f"grants-drift-{narrowed_stage}"
    v1 = _two_stage_verifying(fixtures_dir, tmp_path / "v1.toml", narrowed_stage, _WIDE_VERIFY)
    _to_executing(store, sid, fixtures_dir, plan_path=v1)
    before = store.load(sid)
    approved, error = _derived(before, narrowed_stage)
    assert error is None, error
    assert _GIT_STATUS in {e["rule"] for e in approved}, approved
    assert _RUN_MOD not in {e["rule"] for e in _derived(before, 1)[0]}

    marker.unlink()
    v2 = _two_stage_verifying(fixtures_dir, tmp_path / "v2.toml", narrowed_stage, _NARROW_VERIFY)
    d = cli.cmd_replan(ns(session=sid, plan=v2), store=store)
    assert d.action == "continue", d.detail
    return sid, before


def test_a_refinement_narrowing_another_stage_does_not_widen_this_one_with_venue_drift(
    store, fixtures_dir, tmp_path, monkeypatch,
):
    """The refinement branch re-derived EVERY stage against today's venue. Stage 1's
    grant inputs did not move, but the marker that kept `python3 mod.py` out of its
    approved set was gone by then, so a refinement aimed at stage 2 handed stage 1 a
    Bash grant the user never approved."""
    sid, before = _marker_then_drift(store, fixtures_dir, tmp_path, monkeypatch,
                                     narrowed_stage=2)

    state = store.load(sid)
    stage1, error = _derived(state, 1)
    stage2, _ = _derived(state, 2)

    assert error is None, error
    assert _RUN_MOD not in {e["rule"] for e in stage1}, stage1
    assert state.approved_grant_entries["1"] == before.approved_grant_entries["1"]
    assert _GIT_STATUS not in {e["rule"] for e in stage2}, "the narrowing itself still lands"


def test_a_refinement_narrowing_a_stage_withholds_venue_drift_from_that_stage_too(
    store, fixtures_dir, tmp_path, monkeypatch,
):
    """When the narrowed stage is the drifting one, what the plan removed is removed but
    the derived grant the approval never stored stays out, and the withholding is on the
    record rather than silent."""
    sid, _ = _marker_then_drift(store, fixtures_dir, tmp_path, monkeypatch, narrowed_stage=1)

    state = store.load(sid)
    stage1, error = _derived(state, 1)

    assert error is None, error
    rules = {e["rule"] for e in stage1}
    assert _RUN_MOD not in rules, stage1
    assert _GIT_STATUS not in rules, stage1
    withheld = [h for h in state.history if h.get("event") == "grant_drift_withheld"]
    assert withheld and _RUN_MOD in withheld[-1]["stages"]["1"], withheld


def test_the_stored_entries_reproduce_the_hash_the_plan_derives(fixtures_dir):
    """`approved_grants_sha256` is guarded at dispatch by hashing the stored entries, so
    the entries hash and the plan hash must be one digest."""
    from agentctl import plan as plan_mod

    doc = plan_mod.load_plan(str(fixtures_dir / "plan_two_stage.toml"))

    assert (plan_mod.entries_grants_sha256(plan_mod.materialized_grant_entries(doc))
            == plan_mod.grants_sha256(doc))


def test_stored_entries_that_no_longer_reproduce_the_approved_hash_withhold_the_derived(
    store, fixtures_dir,
):
    """The hash stays the guard once entries are stored: a stored set that does not hash
    to what was approved is reported, not trusted."""
    sid = "grants-stored-mismatch"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    assert state.approved_grant_entries
    state.approved_grants_sha256 = "0" * 64
    store.save(state)

    derived, error = _derived(store.load(sid))

    assert derived == []
    assert error and "approved_grants_sha256" in error


def test_a_session_bound_before_entries_were_stored_still_refuses_on_a_hash_mismatch(
    store, fixtures_dir,
):
    """The legacy path is kept, not loosened: no stored entries means the approved set
    can only be re-derived, and a re-derivation that no longer reproduces the hash is
    reported rather than trusted."""
    sid = "grants-legacy"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    state.approved_grant_entries = None
    state.approved_grants_sha256 = "0" * 64
    store.save(state)

    derived, error = _derived(store.load(sid))

    assert derived == []
    assert error and "derived-grants hash mismatch" in error


def test_a_refinement_materializes_a_legacy_sessions_entries_only_when_its_hash_holds(
    store, fixtures_dir, tmp_path, monkeypatch,
):
    """The refresh path adopts a session bound before entries were stored under the same
    rule `--renormalize` applies: what is written down is what the user approved. A
    re-derivation that reproduces the approved hash is that set; one that does not is
    today's venue and engine, and writing it down as the approval would launder drift."""
    monkeypatch.chdir(tmp_path)
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")

    holds = "legacy-hash-holds"
    _to_executing(store, holds, fixtures_dir)
    state = store.load(holds)
    state.approved_grant_entries = None
    store.save(state)
    cli.cmd_replan(ns(session=holds, plan=refined), store=store)
    assert store.load(holds).approved_grant_entries, "the approved hash reproduces: stored"

    drifted = "legacy-hash-drifted"
    _to_executing(store, drifted, fixtures_dir)
    state = store.load(drifted)
    state.approved_grant_entries = None
    state.approved_grants_sha256 = "0" * 64
    store.save(state)
    cli.cmd_replan(ns(session=drifted, plan=refined), store=store)
    assert store.load(drifted).approved_grant_entries is None


def test_a_grant_growing_replan_still_goes_back_for_approval(store, fixtures_dir):
    sid = "grants-grow"
    _to_executing(store, sid, fixtures_dir,
                  plan_path=str(fixtures_dir / "plan_two_stage_verifyfix_grantgrowth.toml"))

    d = cli.cmd_replan(
        ns(session=sid,
           plan=str(fixtures_dir / "plan_two_stage_verifyfix_grantgrowth_changed.toml")),
        store=store)

    assert d.marker == "PLAN-READY"
    assert not store.load(sid).approval.passed


def test_a_renormalize_leaves_the_snapshot_on_the_approved_bytes_and_the_entries_intact(
    store, tmp_path,
):
    """Successive renormalizations are each measured against the bytes the user approved;
    the stored entries are what makes dispatch independent of which bytes the snapshot
    is."""
    before = rn._approved(store, _render(tmp_path / "v1.toml"), session="ns")
    entries = getattr(before, "approved_grant_entries", None)
    assert entries, "approve must have materialized the approved grant entries"

    d = _replan(store, _render(
        tmp_path / "v2.toml",
        procedure='procedure = "1. Read the engine. 2. Extend the seam. 3. Run the suite."\n'),
        renormalize=True)
    assert d.ok is True, d.detail

    after = store.load("ns")
    assert after.plan_snapshot_hash == before.plan_snapshot_hash
    assert after.approved_grant_entries == entries


_PROCEDURE_EDIT = (
    'procedure = "1. Read the engine. 2. Extend the seam. 3. Run the suite."\n'
)


def _legacy_bound(store, tmp_path, *, approved_hash):
    """An approved session as base code left it: the approved hash, no stored entries."""
    rn._approved(store, _render(tmp_path / "v1.toml"), session="ns")
    state = store.load("ns")
    state.approved_grant_entries = None
    if approved_hash is not None:
        state.approved_grants_sha256 = approved_hash
    store.save(state)
    return store.load("ns").approved_grants_sha256


def test_a_renormalize_materializes_the_entries_of_a_legacy_session_whose_hash_holds(
    store, tmp_path,
):
    approved = _legacy_bound(store, tmp_path, approved_hash=None)
    assert approved, "fixture premise: the approved plan derives a grant set"

    d = _replan(store, _render(tmp_path / "v2.toml", procedure=_PROCEDURE_EDIT),
                renormalize=True)

    assert d.ok is True, d.detail
    after = store.load("ns")
    assert after.approved_grants_sha256 == approved
    assert after.approved_grant_entries, "the entries the approved hash covers are stored"


def test_a_renormalize_never_rebinds_a_legacy_hash_the_new_plan_does_not_reproduce(
    store, tmp_path,
):
    """The hash the user approved is not the hash of what this plan derives now (engine
    or venue drift). Renormalizing must neither adopt the new set under the old approval
    nor refuse: it leaves the legacy binding exactly as it was."""
    stale = "0" * 64
    _legacy_bound(store, tmp_path, approved_hash=stale)

    d = _replan(store, _render(tmp_path / "v2.toml", procedure=_PROCEDURE_EDIT),
                renormalize=True)

    assert d.ok is True, d.detail
    after = store.load("ns")
    assert after.approved_grants_sha256 == stale
    assert after.approved_grant_entries is None


def test_a_state_file_written_before_the_new_fields_loads_with_them_unset(
    store, fixtures_dir,
):
    sid = "old-state"
    _to_executing(store, sid, fixtures_dir)
    path = store.path(sid)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.pop("approved_grant_entries", None)
    for review in (raw.get("acceptance_review"),):
        if review:
            review.pop("requirement_bindings", None)
    path.write_text(json.dumps(raw), encoding="utf-8")

    state = store.load(sid)

    assert state is not None
    assert getattr(state, "approved_grant_entries", None) is None
