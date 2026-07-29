"""`agentctl check-controls` — the pre-flight that answers whether a declared
check RUNS, not whether it is green.

The contract's hardest half is the FIRST test here: a control authored before its
work exists is legitimately RED, so a false predicate must be reported as RAN. A
verb that graded greenness would reject every honestly-authored control.

Every COULD-NOT-RUN member is exercised against the SHAPE measured on this
machine (2026-07-29), not against a hand-written exit code alone — the two
round-2 members are the reason: `python3 -m nosuchmodule` exits 1 with a
one-line stderr diagnostic and NO traceback, and a git fatal exits 128, and both
were indistinguishable from an honest red under an exit-code-only rule.
"""
from __future__ import annotations

from argparse import Namespace

from agentctl import cli


def ns(**kw):
    return Namespace(**kw)


def _plan(tmp_path, *commands, name="plan.toml", meta_extra=""):
    """A minimal measurable plan whose stage N runs commands[N-1]."""
    body = [
        "[meta]",
        'task_id = "check-controls-probe"',
        'goal = "probe"',
        'done_criterion = "every declared check runs"',
        'criterion_type = "measurable"',
        meta_extra,
    ]
    for i, command in enumerate(commands, start=1):
        body += [
            "",
            "[[stage]]",
            f"index = {i}",
            f'title = "probe {i}"',
            'executor = "spawn:developer"',
            'expected_result_image = "the command reaches its own predicate"',
            'criterion_type = "measurable"',
            'done_criterion = "the probe answers"',
            f"verify_command = {command!r}".replace("'", '"'),
            "expected_exit = 0",
            f"depends_on = {[] if i == 1 else [i - 1]}",
        ]
    path = tmp_path / name
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return str(path)


def _run(store, plan, session=None):
    return cli.cmd_check_controls(ns(plan=plan, session=session), store=store)


def _verdicts(directive):
    return [(r["where"], r["verdict"]) for r in directive.data["checks"]]


def _only(directive):
    assert len(directive.data["checks"]) == 1, directive.data["checks"]
    return directive.data["checks"][0]


# --- the contract's hardest half ----------------------------------------------

def test_a_false_predicate_is_reported_as_ran(store, tmp_path):
    """RED IS A PASS OF THIS VERB. `false` and a grep that finds nothing are what
    a correct control looks like before its work exists; reporting either as
    COULD-NOT-RUN would make the verb reject exactly the controls it exists to
    protect."""
    plan = _plan(tmp_path, "false", "/bin/grep -q zzz-absent-pattern /etc/hostname")
    d = _run(store, plan)
    assert _verdicts(d) == [("stage 1", cli.CONTROL_RAN), ("stage 2", cli.CONTROL_RAN)]
    assert d.data["counts"][cli.CONTROL_COULD_NOT_RUN] == 0
    assert d.ok is True


# --- the closed COULD-NOT-RUN evidence set ------------------------------------

def test_command_not_found_is_could_not_run(store, tmp_path):
    plan = _plan(tmp_path, "definitely-not-a-command-xyz")
    record = _only(_run(store, plan))
    assert record["verdict"] == cli.CONTROL_COULD_NOT_RUN
    assert record["exit"] == 127


def test_pytest_collecting_nothing_is_could_not_run(store, tmp_path):
    """The dead-on-arrival shape from instance 21: a node id that no longer
    matches anything. pytest reports it as a usage error (exit 4), which an
    exit-code-agnostic reading would mistake for an ordinary red."""
    empty = tmp_path / "test_absent.py"
    plan = _plan(tmp_path, f"python3 -m pytest {empty} -q -p no:cacheprovider")
    record = _only(_run(store, plan))
    assert record["verdict"] == cli.CONTROL_COULD_NOT_RUN
    assert record["exit"] in (4, 5)


def test_argparse_usage_error_is_could_not_run(store, tmp_path):
    """A control that names an agentctl subcommand which no longer exists: the
    parser exits 2 before any engine code runs."""
    plan = _plan(tmp_path, "python3 -m agentctl no-such-subcommand", meta_extra=(
        f'repo_root = "{cli.REPO_ROOT / "scripts"}"'
    ))
    record = _only(_run(store, plan))
    assert record["verdict"] == cli.CONTROL_COULD_NOT_RUN
    assert record["exit"] == 2


def test_a_traceback_on_either_stream_is_could_not_run(store, tmp_path):
    """BOTH streams, not stderr alone. A stderr-only implementation passes a
    stderr-only test while missing every check that folds stderr into stdout with
    `2>&1` — which is precisely what the fail-open pipeline idiom does."""
    boom = tmp_path / "boom.py"
    boom.write_text('raise RuntimeError("boom")\n', encoding="utf-8")
    plan = _plan(tmp_path, f"python3 {boom}", f"python3 {boom} 2>&1")
    d = _run(store, plan)
    assert _verdicts(d) == [
        ("stage 1", cli.CONTROL_COULD_NOT_RUN),
        ("stage 2", cli.CONTROL_COULD_NOT_RUN),
    ]
    assert "stderr" in d.data["checks"][0]["evidence"]
    assert "stdout" in d.data["checks"][1]["evidence"]


def test_an_interpreter_diagnostic_with_exit_one_is_could_not_run(store, tmp_path):
    """Measured: a dead interpreter invocation does NOT raise — it prints one
    line and exits 1, with no traceback, which by exit code alone is
    indistinguishable from an honest false predicate. Every stage verify_command
    in the plan that specifies this verb is `python3 -m pytest`, so a venue whose
    interpreter lacks pytest produces exactly this shape."""
    plan = _plan(tmp_path, "python3 -m nosuchmodule_xyz")
    record = _only(_run(store, plan))
    assert record["verdict"] == cli.CONTROL_COULD_NOT_RUN
    assert record["exit"] == 1
    assert "No module named" in record["evidence"]


def test_git_fatal_exit_128_is_could_not_run(store, tmp_path):
    """The post-land ancestry clause's own failure mode: git cannot answer at
    all. 128 is git's fatal-error code and bash's invalid-exit-argument code; no
    control in this repository uses it as a predicate VALUE."""
    plan = _plan(
        tmp_path,
        "git merge-base --is-ancestor deadbeefdeadbeefdeadbeefdeadbeefdeadbeef HEAD",
        meta_extra=f'repo_root = "{cli.REPO_ROOT}"',
    )
    record = _only(_run(store, plan))
    assert record["verdict"] == cli.CONTROL_COULD_NOT_RUN
    assert record["exit"] == 128


# --- venue resolution is plan-first ---------------------------------------------

def test_venues_resolve_from_the_plan_not_the_ambient_session(store, tmp_path):
    """`main` injects `--session $CLAUDE_CODE_SESSION_ID` into every subcommand
    that accepts the flag, so this verb always arrives carrying whatever session
    is live — even though its whole point is to run BEFORE a session drives this
    plan. When venues came from that session, a session declaring no repo_root
    resolved both venues to None, the `cd` prefix vanished, and every check ran in
    the CALLER's cwd: the verdicts depended on which directory the operator
    happened to be in, which is the same silently-wrong-venue defect this plan
    exists to close. Pinned by placing the plan's venue somewhere the caller's cwd
    is not — the probe reports the directory it actually ran in."""
    venue = tmp_path / "declared-venue"
    venue.mkdir()
    (venue / "sentinel.txt").write_text("here\n", encoding="utf-8")
    sid = "cc-ambient"
    cli.cmd_start(ns(session=sid, task="ambient", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    assert store.load(sid).repo_root is None, "the ambient session declares no venue"

    plan = _plan(tmp_path, "test -f sentinel.txt",
                 meta_extra=f'repo_root = "{venue}"')
    record = _only(_run(store, plan, session=sid))
    assert record["cwd"] == str(venue)
    assert record["verdict"] == cli.CONTROL_RAN
    assert record["exit"] == 0


# --- the read-only promise ------------------------------------------------------

def test_check_controls_never_saves_or_transitions_state(store, tmp_path, fixtures_dir):
    """cmd_check_coverage's guarantee, carried over verbatim: a pre-flight that
    mutated what it inspects would corrupt the state a later replan diffs
    against. Asserted on the persisted BYTES, not on a field-by-field compare."""
    sid = "cc-readonly"
    cli.cmd_start(ns(session=sid, task="demo-two-stage", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(
        ns(session=sid, plan=str(fixtures_dir / "plan_two_stage_finalcheck.toml")),
        store=store,
    )
    state_file = store.path(sid)
    before = state_file.read_bytes()
    node_before = store.load(sid).node

    plan = _plan(tmp_path, "false", "definitely-not-a-command-xyz")
    d = _run(store, plan, session=sid)

    assert state_file.read_bytes() == before
    assert d.node == node_before
    assert d.marker is None
