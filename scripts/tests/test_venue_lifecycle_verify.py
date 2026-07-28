"""Stage 2 of the venue-lifecycle plan: verify-final resolves a measurable stage
check's cwd via `SessionState.resolve_final_check_venue` (the FINAL venue —
`verify_venue_at_final` when declared, else `verify_venue`) instead of the
execution-time `_resolve_or_refuse(state, crit.verify_venue)` it used to share
with `cmd_dispatch`/`cmd_record_result`. Those two execution sites are
deliberately UNCHANGED — during EXECUTING the un-landed change exists only in
the delivery venue. `[[final_check]]` entries keep their single `venue` field
(they run at the one verify-final site, so that field already IS their final
venue).

The second half of this stage closes G1: every verify-final refusal (a
declared venue that resolves to a directory missing on disk) now routes into
DIAGNOSING — `declare`/`investigate`/`critique` become reachable — instead of
stranding the session at VERIFYING, where those commands refuse ("difficulty
commands run only in the DIAGNOSING cycle") and only `reset --force` escaped.
A refusal is still never a stage FAILURE; only the destination node changes.

Proves the plan's four cases verbatim:
  (a) an opted-in stage (verify_venue="delivery", verify_venue_at_final=
      "repo_root") records PASSED against the delivery tree, then — after that
      tree is removed, simulating a landed/cleaned-up worktree — passes
      verify-final at repo_root: the end-to-end G2 proof;
  (b) a stage declaring only verify_venue="delivery" (no opt-in) still refuses
      at verify-final once the worktree is gone — pre-existing behaviour is
      preserved for plans that do not declare the new field;
  (c) after a refusal the session node is DIAGNOSING, a Difficulty exists,
      `declare` is ACCEPTED, and no stage is marked FAILED — asserted across
      all three refusal flavours: a stage shell check, a [[final_check]] shell
      check, and a landed check;
  (d) a plan/state with no venue fields at all produces the identical
      directive and command sequence it produced before this change.

Re-entry idempotency (the stage-2 review's blocking finding): verify-final's
body transitions the node (`final` -> RESOLUTION on success, `diagnose` ->
DIAGNOSING on a refusal/failure), both legal only from VERIFYING, while its
resolution gate is node-agnostic. A second verify-final call after a prior
refusal routed the session to DIAGNOSING must therefore NOT re-run the
transitioning body (that raised an uncaught TransitionError); it returns a
legible directive back into the difficulty cycle. Same guard makes a re-call
from RESOLUTION idempotent. `test_reinvoke_*` prove both, venue-restored and
venue-still-missing.
"""
import shlex
import shutil
from argparse import Namespace

from agentctl import cli
from agentctl.dispatch import RunResult
from agentctl.state import (
    Actor,
    CheckKind,
    Criterion,
    FinalCheck,
    GateRecord,
    LandedSpec,
    Means,
    Node,
    Outcome,
    Partition,
    Route,
    SessionState,
    Stage,
    StageStatus,
    Subject,
    WeightClass,
)


def ns(**kw):
    return Namespace(**kw)


def _stage(verify_command="pytest -q", expected_exit=0, verify_venue="delivery",
           verify_venue_at_final=None, status=StageStatus.ACTIVE.value, index=1,
           verify_kind="shell", landed=None):
    return Stage(
        index=index, title="s1",
        subject=Subject(material="m", result="img"),
        means=Means(means="bash", method="run"),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(
            criterion_type="measurable", done_criterion="c",
            verify_command=verify_command, expected_exit=expected_exit,
            verify_venue=verify_venue, verify_venue_at_final=verify_venue_at_final,
            verify_kind=verify_kind, landed=landed,
        ),
        outcome=Outcome(status=status),
    )


def _executing(sid, stage, repo_root=None, delivery_worktree=None):
    s = SessionState(
        session_id=sid, task_id="t",
        weight_class=WeightClass.SUBSTANTIVE.value, route=Route.SPAWN.value,
        node=Node.EXECUTING.value, repo_root=repo_root,
        delivery_worktree=delivery_worktree,
        approval=GateRecord("plan_approval", armed=True, passed=True),
        partition=Partition(m1=True, verdict="recommended"),
        stages=[stage],
    )
    s.current_stage = 1
    return s


def _verifying(sid, stages, repo_root=None, delivery_worktree=None, final_check=None):
    return SessionState(
        session_id=sid, task_id="t",
        weight_class=WeightClass.SUBSTANTIVE.value, route=Route.SPAWN.value,
        node=Node.VERIFYING.value, repo_root=repo_root,
        delivery_worktree=delivery_worktree,
        approval=GateRecord("plan_approval", armed=True, passed=True),
        partition=Partition(m1=True, verdict="recommended"),
        stages=stages,
        final_check=final_check or [],
    )


class _Capture:
    """Captures the single argv of the one call expected in a cmd_record_result test."""

    def __init__(self, code=0):
        self.code, self.argv = code, None

    def __call__(self, argv):
        self.argv = argv
        return RunResult(self.code, stdout="", stderr="")


class _CaptureAll:
    """Captures every argv across cmd_verify_final's stage + final_check loops."""

    def __init__(self, code=0):
        self.code, self.calls = code, []

    def __call__(self, argv):
        self.calls.append(argv)
        return RunResult(self.code, stdout="", stderr="")


# --- (a): opted-in stage survives delivery-venue removal, end-to-end (G2) ----

def test_opted_in_stage_survives_delivery_venue_removal(store, tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree = tmp_path / "worktrees" / "w1"
    worktree.mkdir(parents=True)

    stage = _stage(verify_venue="delivery", verify_venue_at_final="repo_root")
    s = _executing("a1", stage, repo_root=str(repo_root), delivery_worktree=str(worktree))
    store.save(s)

    cap = _Capture()
    d = cli.cmd_record_result(
        ns(session="a1", status="passed", actual="ok", control=None),
        store=store, runner=cap,
    )
    assert d.ok is True
    assert cap.argv == ["bash", "-c", f"cd {shlex.quote(str(worktree))} && pytest -q"]

    # Simulate landing: the delivery worktree is gone by the time verify-final runs.
    shutil.rmtree(worktree)

    cap2 = _CaptureAll()
    d2 = cli.cmd_verify_final(ns(session="a1"), store=store, runner=cap2)
    assert d2.ok is True, d2.detail
    assert d2.action == "await_user_confirmation"
    assert cap2.calls == [["bash", "-c", f"cd {shlex.quote(str(repo_root))} && pytest -q"]]


# --- (b): a bare (non-opted-in) stage still refuses once the worktree is gone --

def test_bare_delivery_venue_still_refuses_when_worktree_gone(store, tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree = tmp_path / "worktrees" / "w2"
    worktree.mkdir(parents=True)

    stage = _stage(verify_venue="delivery", status=StageStatus.PASSED.value)
    s = _verifying("b1", [stage], repo_root=str(repo_root), delivery_worktree=str(worktree))
    store.save(s)

    shutil.rmtree(worktree)

    cap = _CaptureAll()
    d = cli.cmd_verify_final(ns(session="b1"), store=store, runner=cap)
    assert d.ok is False
    assert d.action == "declare"
    assert cap.calls == []  # refusal short-circuits before the check ever runs
    assert store.load("b1").stage(1).outcome.status != StageStatus.FAILED.value


# --- (c): every refusal flavour lands in DIAGNOSING, declare succeeds, no FAILED --

def test_stage_shell_refusal_reaches_diagnosing_and_declare_is_accepted(store, tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree = tmp_path / "worktrees" / "w3"
    worktree.mkdir(parents=True)

    stage = _stage(verify_venue="delivery", status=StageStatus.PASSED.value)
    s = _verifying("c1", [stage], repo_root=str(repo_root), delivery_worktree=str(worktree))
    store.save(s)
    shutil.rmtree(worktree)

    d = cli.cmd_verify_final(ns(session="c1"), store=store, runner=_CaptureAll())
    assert d.ok is False
    assert d.node == Node.DIAGNOSING.value
    assert d.action == "declare"
    assert d.marker == "OVERCOME-DIFFICULTY"

    reloaded = store.load("c1")
    assert reloaded.difficulty is not None
    assert reloaded.stage(1).outcome.status != StageStatus.FAILED.value

    d2 = cli.cmd_declare(
        ns(session="c1", expected="repo_root venue usable", actual="worktree removed",
           mismatch="delivery worktree was cleaned up before verify-final ran"),
        store=store,
    )
    assert d2.ok is True
    assert d2.action == "investigate"


def test_final_check_shell_refusal_reaches_diagnosing_and_declare_is_accepted(store, tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree = tmp_path / "worktrees" / "w4"
    worktree.mkdir(parents=True)

    stage = _stage(verify_command=None, status=StageStatus.PASSED.value)
    fc = FinalCheck(command="pytest -q", venue="delivery")
    s = _verifying("c2", [stage], repo_root=str(repo_root),
                    delivery_worktree=str(worktree), final_check=[fc])
    store.save(s)
    shutil.rmtree(worktree)

    d = cli.cmd_verify_final(ns(session="c2"), store=store, runner=_CaptureAll())
    assert d.ok is False
    assert d.node == Node.DIAGNOSING.value
    assert d.action == "declare"
    assert d.marker == "OVERCOME-DIFFICULTY"

    reloaded = store.load("c2")
    assert reloaded.difficulty is not None
    assert reloaded.stage(1).outcome.status != StageStatus.FAILED.value

    d2 = cli.cmd_declare(
        ns(session="c2", expected="delivery venue usable", actual="worktree removed",
           mismatch="delivery worktree was cleaned up before verify-final ran"),
        store=store,
    )
    assert d2.ok is True
    assert d2.action == "investigate"


def test_landed_check_refusal_reaches_diagnosing_and_declare_is_accepted(store):
    # render_landed_command's simplest refusal trigger: no repo_root at all —
    # nothing to resolve the target/remote refs against. Pure-Python, no git.
    stage = _stage(
        verify_command=None, verify_kind=CheckKind.LANDED.value,
        landed=LandedSpec(target="main", remote="origin", delivered_stage=1),
        status=StageStatus.PASSED.value,
    )
    s = _verifying("c3", [stage], repo_root=None)
    store.save(s)

    d = cli.cmd_verify_final(ns(session="c3"), store=store, runner=_CaptureAll())
    assert d.ok is False
    assert d.node == Node.DIAGNOSING.value
    assert d.action == "declare"
    assert d.marker == "OVERCOME-DIFFICULTY"

    reloaded = store.load("c3")
    assert reloaded.difficulty is not None
    assert reloaded.stage(1).outcome.status != StageStatus.FAILED.value

    d2 = cli.cmd_declare(
        ns(session="c3", expected="repo_root set", actual="no repo_root declared",
           mismatch="landed check cannot resolve target/remote refs without repo_root"),
        store=store,
    )
    assert d2.ok is True
    assert d2.action == "investigate"


# --- (d): a plan with no venue fields produces the identical directive sequence --

def test_absent_verify_venue_at_final_is_byte_identical_to_pre_change(store, tmp_path):
    repo_root = tmp_path / "repo"
    worktree = tmp_path / "worktrees" / "w5"
    worktree.mkdir(parents=True)

    stage = _stage(status=StageStatus.PASSED.value)  # verify_venue_at_final absent (V4)
    assert stage.criterion.verify_venue_at_final is None
    s = _verifying("d1", [stage], repo_root=str(repo_root), delivery_worktree=str(worktree))
    store.save(s)

    cap = _CaptureAll()
    d = cli.cmd_verify_final(ns(session="d1"), store=store, runner=cap)
    assert d.ok is True
    assert d.action == "await_user_confirmation"
    # Same tree cmd_dispatch/cmd_record_result would have used — the venue never
    # moves to repo_root just because this field now exists on the Criterion.
    assert cap.calls == [["bash", "-c", f"cd {shlex.quote(str(worktree))} && pytest -q"]]


# --- re-entry idempotency: verify-final called again from DIAGNOSING/RESOLUTION --

def test_reinvoke_verify_final_from_diagnosing_venue_restored_does_not_crash(store, tmp_path):
    # A refusal routed the session to DIAGNOSING; the coordinator (mis)reads the
    # directive as "recreate the venue and re-run verify-final". Before the guard
    # this re-entered the transitioning body from DIAGNOSING and raised
    # TransitionError on the success-path `final` edge. Now it returns a legible
    # directive pointing back into the difficulty cycle.
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree = tmp_path / "worktrees" / "w6"
    worktree.mkdir(parents=True)

    stage = _stage(verify_venue="delivery", status=StageStatus.PASSED.value)
    s = _verifying("e1", [stage], repo_root=str(repo_root), delivery_worktree=str(worktree))
    store.save(s)
    shutil.rmtree(worktree)

    d1 = cli.cmd_verify_final(ns(session="e1"), store=store, runner=_CaptureAll())
    assert d1.node == Node.DIAGNOSING.value

    worktree.mkdir(parents=True)  # venue "fixed", re-run
    d2 = cli.cmd_verify_final(ns(session="e1"), store=store, runner=_CaptureAll())
    assert d2.ok is False
    assert d2.node == Node.DIAGNOSING.value
    assert d2.action == "declare"
    assert d2.marker == "OVERCOME-DIFFICULTY"


def test_reinvoke_verify_final_from_diagnosing_venue_still_missing_does_not_crash(store, tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree = tmp_path / "worktrees" / "w7"
    worktree.mkdir(parents=True)

    stage = _stage(verify_venue="delivery", status=StageStatus.PASSED.value)
    s = _verifying("e2", [stage], repo_root=str(repo_root), delivery_worktree=str(worktree))
    store.save(s)
    shutil.rmtree(worktree)

    d1 = cli.cmd_verify_final(ns(session="e2"), store=store, runner=_CaptureAll())
    assert d1.node == Node.DIAGNOSING.value

    d2 = cli.cmd_verify_final(ns(session="e2"), store=store, runner=_CaptureAll())  # still gone
    assert d2.ok is False
    assert d2.node == Node.DIAGNOSING.value
    assert d2.action == "declare"
    assert d2.marker == "OVERCOME-DIFFICULTY"


def test_reinvoke_verify_final_from_resolution_is_idempotent(store, tmp_path):
    # The success side of the same guard: once verify-final has passed and armed
    # the resolution gate (node RESOLUTION), a second call must not re-run the
    # `final` transition (illegal from RESOLUTION) — it returns the resolve hint.
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    stage = _stage(verify_venue="repo_root", status=StageStatus.PASSED.value)
    s = _verifying("e3", [stage], repo_root=str(repo_root))
    store.save(s)

    d1 = cli.cmd_verify_final(ns(session="e3"), store=store, runner=_CaptureAll())
    assert d1.ok is True
    assert d1.node == Node.RESOLUTION.value

    d2 = cli.cmd_verify_final(ns(session="e3"), store=store, runner=_CaptureAll())
    assert d2.ok is True
    assert d2.node == Node.RESOLUTION.value
    assert d2.action == "resolve"
