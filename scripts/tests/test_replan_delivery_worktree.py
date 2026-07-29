"""cmd_replan must carry [meta] delivery_worktree into live state in all three
diff-kind branches (no_change/refinement/substantive) — the twin fix to the
existing state.repo_root carry, since resolve_check_venue reads both fields as
one pair (state.py:996-1011). Also covers the delivery_worktree_changed
visibility event: a replan that moves the venue while a stage is already
PASSED or ACTIVE logs it rather than silently stranding that stage's venue."""
from argparse import Namespace

from agentctl import cli
from agentctl.state import Node, StageStatus


def ns(**kw):
    return Namespace(**kw)


def _to_executing_stage1(store, sid, plan):
    cli.cmd_start(ns(session=sid, task="demo-two-stage", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)


def _delivery_worktree_events(state):
    return [h for h in state.history if h.get("event") == "delivery_worktree_changed"]


def test_refinement_replan_refreshes_delivery_worktree(store, fixtures_dir):
    sid = "rf-dw"
    base = str(fixtures_dir / "plan_two_stage_verifyfix_delivery.toml")
    changed = str(fixtures_dir / "plan_two_stage_verifyfix_delivery_changed.toml")
    _to_executing_stage1(store, sid, base)
    assert store.load(sid).delivery_worktree == "/tmp/test-delivery-a"

    d = cli.cmd_replan(ns(session=sid, plan=changed), store=store)
    assert d.action == "continue"
    state = store.load(sid)
    assert state.node == Node.EXECUTING.value
    assert state.delivery_worktree == "/tmp/test-delivery-b"


def test_substantive_replan_refreshes_delivery_worktree(store, fixtures_dir):
    sid = "sb-dw"
    base = str(fixtures_dir / "plan_two_stage.toml")
    bigger = str(fixtures_dir / "plan_two_stage_substantive_delivery.toml")
    _to_executing_stage1(store, sid, base)
    assert store.load(sid).delivery_worktree is None

    d = cli.cmd_replan(ns(session=sid, plan=bigger), store=store)
    assert d.marker == "PLAN-READY"
    state = store.load(sid)
    assert state.node == Node.PLAN_READY.value
    assert state.delivery_worktree == "/tmp/test-delivery-c"


def test_no_change_replan_refreshes_delivery_worktree(store, fixtures_dir, tmp_path):
    """A legacy session (plan_snapshot_path=None) whose plan file's delivery_worktree
    is edited IN PLACE self-diffs to no_change (old==new==plan_path, for lack of a
    snapshot to diff against). The no_change branch must still refresh
    state.delivery_worktree from the file, mirroring the existing verify_command
    backfill it already performs. Stage 1 is PASSED so this also exercises the
    no_change branch's visibility-event call site — without it that call can be
    deleted with every test still green."""
    sid = "nc-dw"
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_verifyfix_delivery.toml").read_text())
    _to_executing_stage1(store, sid, str(plan_path))

    state = store.load(sid)
    state.plan_snapshot_path = None
    state.plan_snapshot_hash = None
    state.stage(1).outcome.status = StageStatus.PASSED.value
    store.save(state)
    assert state.delivery_worktree == "/tmp/test-delivery-a"

    edited = plan_path.read_text().replace(
        "/tmp/test-delivery-a", "/tmp/test-delivery-a2")
    assert edited != plan_path.read_text()  # the replacement actually matched
    plan_path.write_text(edited)

    d = cli.cmd_replan(ns(session=sid, plan=str(plan_path)), store=store)
    assert d.action == "continue"  # no_change, resumes without re-approval
    state = store.load(sid)
    assert state.delivery_worktree == "/tmp/test-delivery-a2"
    events = _delivery_worktree_events(state)
    assert len(events) == 1
    assert events[0]["old"] == "/tmp/test-delivery-a"
    assert events[0]["new"] == "/tmp/test-delivery-a2"
    assert events[0]["affected_stages"] == [{"index": 1, "status": "PASSED"}]


def test_changed_worktree_with_a_passed_stage_is_logged(store, fixtures_dir):
    """A refinement replan that moves delivery_worktree while stage 1 is already
    PASSED (verified in the OLD venue) must log the change — PASSED stages don't
    lose their Outcome on refinement, but a mover here means a later verify-final
    re-run reads a different tree than the one that actually PASSED."""
    sid = "pw-dw"
    base = str(fixtures_dir / "plan_two_stage_verifyfix_delivery.toml")
    changed = str(fixtures_dir / "plan_two_stage_verifyfix_delivery_changed.toml")
    _to_executing_stage1(store, sid, base)

    state = store.load(sid)
    state.stage(1).outcome.status = StageStatus.PASSED.value
    state.current_stage = 2
    store.save(state)

    d = cli.cmd_replan(ns(session=sid, plan=changed), store=store)
    assert d.action == "continue"
    state = store.load(sid)
    events = _delivery_worktree_events(state)
    assert len(events) == 1
    assert events[0]["old"] == "/tmp/test-delivery-a"
    assert events[0]["new"] == "/tmp/test-delivery-b"
    assert events[0]["affected_stages"] == [{"index": 1, "status": "PASSED"}]


def test_changed_worktree_with_an_active_stage_is_logged(store, fixtures_dir):
    """A substantive replan discards any outcome the carry-forward loop doesn't
    explicitly preserve (it only carries PASSED forward), so an ACTIVE stage —
    already dispatched into the OLD delivery_worktree — would be silently
    invisible to a check made AFTER state.stages is replaced. This pins that the
    visibility check reads the pre-replan stage list."""
    sid = "aw-dw"
    base = str(fixtures_dir / "plan_two_stage.toml")
    bigger = str(fixtures_dir / "plan_two_stage_substantive_delivery.toml")
    _to_executing_stage1(store, sid, base)

    state = store.load(sid)
    state.stage(1).outcome.status = StageStatus.ACTIVE.value
    store.save(state)

    d = cli.cmd_replan(ns(session=sid, plan=bigger), store=store)
    assert d.marker == "PLAN-READY"
    state = store.load(sid)
    events = _delivery_worktree_events(state)
    assert len(events) == 1
    assert events[0]["old"] is None
    assert events[0]["new"] == "/tmp/test-delivery-c"
    assert events[0]["affected_stages"] == [{"index": 1, "status": "ACTIVE"}]


def test_unchanged_worktree_is_not_logged(store, fixtures_dir):
    """A refinement replan that does not touch delivery_worktree (both plans leave
    it unset) must not emit the event, even with a PASSED stage in play — the
    event is not vacuously present on every replan."""
    sid = "un-dw"
    base = str(fixtures_dir / "plan_two_stage_verifyfix.toml")
    changed = str(fixtures_dir / "plan_two_stage_verifyfix_changed.toml")
    _to_executing_stage1(store, sid, base)

    state = store.load(sid)
    state.stage(1).outcome.status = StageStatus.PASSED.value
    store.save(state)

    d = cli.cmd_replan(ns(session=sid, plan=changed), store=store)
    assert d.action == "continue"
    state = store.load(sid)
    assert not _delivery_worktree_events(state)
