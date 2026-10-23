"""verify-final skips re-running a stage's verify_command when the venue tree
is unchanged since the stage last passed (same checked_tree_identity).

The cache is the same one record-result maintains (Outcome.checked_tree_identity /
checked_tree_ok); verify-final is now a consumer. Three controls:

  - skip (unchanged tree + green cache): bash verify command NOT called
  - no-skip (tree moved — different HEAD): bash verify command IS called
  - no-skip (result was red — checked_tree_ok=False): bash verify command IS called

MUTATION ANCHOR: removing the `if _cached_check_hit(stage, identity): continue`
block from cmd_verify_final flips the `assert runner.bash_calls == []` assertion
in test_verify_final_skips_stage_when_tree_unchanged — the verify command would
run and appear in bash_calls, turning the test RED.
"""
from __future__ import annotations

import hashlib
from argparse import Namespace

from agentctl import cli
from agentctl.dispatch import RunResult
from agentctl.state import (
    Actor,
    Criterion,
    CriterionType,
    GateRecord,
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


def _identity_for(head: str, verify_command: str, expected_exit: int = 0) -> str:
    """Reproduce the identity that cmd_record_result / _venue_tree_identity +
    _check_definition_digest would produce for a runner returning `head`.

    Mirrors the two private functions in cli.py exactly — HEAD sha + "\x00" +
    tree sha (tree is "tree-of-<head>" as _Runner below returns it), then
    concatenated with the check-definition digest separated by ":".  If either
    half changes in cli.py this helper will diverge and tests will fail fast
    (the stored identity won't match, forcing a re-run) rather than silently
    serving stale results — the same fail-safe the cache itself provides."""
    tree_sha = f"tree-of-{head}"
    tree_id = hashlib.sha256(
        head.encode() + b"\x00" + tree_sha.encode()
    ).hexdigest()

    check_digest = hashlib.sha256()
    for part in (verify_command, str(expected_exit), "", ""):  # nc and nc_waiver are None
        check_digest.update(part.encode())
        check_digest.update(b"\x00")

    return f"{tree_id}:{check_digest.hexdigest()}"


class _Runner:
    """Minimal runner covering git identity probes (for _venue_tree_identity)
    and bash verify-command calls.  Returns a fixed HEAD sha so identity
    computations are deterministic; records every bash call for assertions."""

    def __init__(self, head: str = "deadbeef12345678"):
        self.head = head
        self.bash_calls: list[str] = []

    def __call__(self, argv, *, timeout=None, stdin=""):
        if argv[:1] == ["env"] and "git" in argv:
            git_argv = argv[argv.index("git"):]
        else:
            git_argv = argv
        if git_argv[:1] == ["git"]:
            if "rev-parse" in git_argv:
                if "--git-path" in git_argv:
                    return RunResult(0, stdout=".git/index", stderr="")
                return RunResult(0, stdout=self.head, stderr="")
            if "write-tree" in git_argv:
                return RunResult(0, stdout=f"tree-of-{self.head}", stderr="")
            return RunResult(0, stdout="", stderr="")  # add -A, ls-files -s
        if argv[:2] == ["bash", "-c"]:
            self.bash_calls.append(argv[2])
            return RunResult(0, stdout="", stderr="")
        raise AssertionError(f"unexpected call: {argv}")


def _verifying_session(store, sid: str, *,
                       verify_command: str = "pytest -q",
                       checked_tree_identity: str | None = None,
                       checked_tree_ok: bool | None = None) -> None:
    """A VERIFYING session with one PASSED measurable stage, optionally with a
    pre-seeded green-check cache."""
    stage = Stage(
        index=1, title="s1",
        subject=Subject(material="m", result="tests pass"),
        means=Means(means="pytest", method="run"),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(
            criterion_type=CriterionType.MEASURABLE.value,
            done_criterion="the suite passes",
            verify_command=verify_command,
            expected_exit=0,
        ),
        outcome=Outcome(
            status=StageStatus.PASSED.value,
            checked_tree_identity=checked_tree_identity,
            checked_tree_ok=checked_tree_ok,
        ),
    )
    state = SessionState(
        session_id=sid, task_id="t",
        weight_class=WeightClass.SUBSTANTIVE.value, route=Route.SPAWN.value,
        node=Node.VERIFYING.value,
        approval=GateRecord("plan_approval", armed=True, passed=True),
        partition=Partition(m1=True, verdict="recommended"),
        stages=[stage],
    )
    store.save(state)


# --- skip: unchanged tree, green cache -----------------------------------------

def test_verify_final_skips_stage_when_tree_unchanged(store):
    """When checked_tree_identity matches the current venue tree and checked_tree_ok
    is True, verify-final must not re-run the verify_command.

    MUTATION ANCHOR: removing the _cached_check_hit guard from cmd_verify_final
    causes bash_calls to contain the verify command, flipping this test RED."""
    HEAD = "deadbeef12345678"
    CMD = "pytest -q"
    identity = _identity_for(HEAD, CMD)

    _verifying_session(store, "skip-1",
                       verify_command=CMD,
                       checked_tree_identity=identity,
                       checked_tree_ok=True)

    runner = _Runner(head=HEAD)
    d = cli.cmd_verify_final(ns(session="skip-1"), store=store, runner=runner)

    assert d.ok is True, d.detail
    assert runner.bash_calls == [], (
        f"verify_command was re-run despite a valid cache hit: {runner.bash_calls}"
    )


def test_verify_final_still_passes_when_cache_skips(store):
    """A cache-skipped stage must not appear in failures; the overall result is ok."""
    HEAD = "aabbccdd11223344"
    CMD = "true"
    identity = _identity_for(HEAD, CMD)

    _verifying_session(store, "skip-2",
                       verify_command=CMD,
                       checked_tree_identity=identity,
                       checked_tree_ok=True)

    runner = _Runner(head=HEAD)
    d = cli.cmd_verify_final(ns(session="skip-2"), store=store, runner=runner)

    assert d.ok is True
    assert "failures" not in d.data or not d.data.get("failures")


# --- no-skip: tree moved (different HEAD) --------------------------------------

def test_verify_final_reruns_when_head_changed(store):
    """When the HEAD has moved since the stage's last green run, the stored
    identity no longer matches and verify-final must re-run the command."""
    OLD_HEAD = "0000000000000000"
    NEW_HEAD = "ffffffffffffffff"
    CMD = "pytest -q"
    old_identity = _identity_for(OLD_HEAD, CMD)

    _verifying_session(store, "rerun-1",
                       verify_command=CMD,
                       checked_tree_identity=old_identity,
                       checked_tree_ok=True)

    # Runner returns a DIFFERENT head than the one recorded in the cache.
    runner = _Runner(head=NEW_HEAD)
    d = cli.cmd_verify_final(ns(session="rerun-1"), store=store, runner=runner)

    assert d.ok is True  # command still exits 0, so verify-final passes
    assert CMD in " ".join(runner.bash_calls), (
        f"expected verify_command to be re-run when HEAD changed, bash_calls: {runner.bash_calls}"
    )


# --- no-skip: cached result was red (checked_tree_ok=False) --------------------

def test_verify_final_reruns_when_cached_result_was_red(store):
    """A cached RED result (checked_tree_ok=False) must never be trusted as a
    skip; verify-final re-runs the command and uses its actual exit code."""
    HEAD = "cafecafe12341234"
    CMD = "pytest -q"
    identity = _identity_for(HEAD, CMD)

    _verifying_session(store, "rerun-2",
                       verify_command=CMD,
                       checked_tree_identity=identity,
                       checked_tree_ok=False)  # red cache — must not skip

    runner = _Runner(head=HEAD)
    d = cli.cmd_verify_final(ns(session="rerun-2"), store=store, runner=runner)

    assert d.ok is True  # the runner returns exit 0, so the re-run passes
    assert CMD in " ".join(runner.bash_calls), (
        f"expected verify_command to be re-run when cached result was red, bash_calls: {runner.bash_calls}"
    )


# --- no-skip: no cache at all (first call) ------------------------------------

def test_verify_final_runs_when_no_cache(store):
    """When checked_tree_identity is None (no prior cache), verify-final must
    always run the command."""
    CMD = "pytest -q"

    _verifying_session(store, "rerun-3",
                       verify_command=CMD,
                       checked_tree_identity=None,
                       checked_tree_ok=None)

    runner = _Runner(head="1234567890abcdef")
    d = cli.cmd_verify_final(ns(session="rerun-3"), store=store, runner=runner)

    assert d.ok is True
    assert CMD in " ".join(runner.bash_calls), (
        f"expected verify_command to run when cache is absent, bash_calls: {runner.bash_calls}"
    )
