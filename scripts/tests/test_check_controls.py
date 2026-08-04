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

import json
import sys
from argparse import Namespace

from agentctl import cli


def ns(**kw):
    return Namespace(**kw)


def _plan(tmp_path, *commands, name="plan.toml", meta_extra="", stage_extra=""):
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
            stage_extra,
        ]
    path = tmp_path / name
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return str(path)


def _raw_plan(tmp_path, body, name="plan.toml"):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return str(path)


def _ambient_session(store, sid, *, repo_root=None, delivery_worktree=None):
    """A live session that DECLARES venues of its own — the case that separates
    plan-first from session-first-with-plan-fallback.

    Written straight onto the state rather than driven through submit-plan: what
    the precedence rule reads is the two venue FIELDS, so routing the setup
    through the plan-submission machinery would couple these tests to that
    machinery's validation without changing the shape under test."""
    cli.cmd_start(ns(session=sid, task="ambient", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    state = store.load(sid)
    state.repo_root = repo_root
    state.delivery_worktree = delivery_worktree
    store.save(state)
    return state


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

def test_plan_venue_beats_a_session_that_declares_its_own_repo_root(store, tmp_path):
    """`main` injects `--session $CLAUDE_CODE_SESSION_ID` into every subcommand
    that accepts the flag, so this verb always arrives carrying whatever session
    is live — even though its whole point is to run BEFORE a session drives this
    plan. Reading venues from that session ran every check in a tree the operator
    never named, which is the silently-wrong-venue defect this plan exists to
    close.

    The ambient session here DECLARES A REPO_ROOT OF ITS OWN, and that is the
    whole point: the previous form of this test used a session whose repo_root was
    None, which is exactly where plan-first and session-first-with-plan-fallback
    AGREE — mutant M13 (session-first, plan as fallback) passed it. Both trees
    exist and both carry a sentinel, so a wrong resolution is a wrong ANSWER
    rather than an error."""
    plan_venue = tmp_path / "plan-venue"
    plan_venue.mkdir()
    (plan_venue / "plan-sentinel.txt").write_text("plan\n", encoding="utf-8")
    session_venue = tmp_path / "session-venue"
    session_venue.mkdir()
    (session_venue / "session-sentinel.txt").write_text("session\n", encoding="utf-8")
    sid = "cc-ambient-with-venue"
    _ambient_session(store, sid, repo_root=str(session_venue))

    plan = _plan(tmp_path, "test -f plan-sentinel.txt",
                 meta_extra=f'repo_root = "{plan_venue}"')
    d = _run(store, plan, session=sid)
    record = _only(d)
    assert record["cwd"] == str(plan_venue)
    assert record["verdict"] == cli.CONTROL_RAN
    assert record["exit"] == 0
    # The degradation notice's NEGATIVE direction (step 13c, r11): a session that
    # loads cleanly must NOT be reported as unreadable. Pinning the notice's
    # presence alone would be passed by an implementation that emits it on every
    # call, which is a report that carries no information.
    assert d.data["session_error"] is None


def test_plan_delivery_venue_beats_a_session_that_declares_its_own_delivery_worktree(
    store, tmp_path,
):
    """The other half of the same precedence, on the OTHER field. The venue
    carrier is built from two independent expressions, so a mutant that flips
    precedence on the delivery line alone survives the repo_root test above —
    while `verify_venue = "delivery"` is what every code stage in the plan that
    specifies this verb actually declares. The property was pinned on the field
    this plan's controls do not use and unpinned on the one they do."""
    plan_root = tmp_path / "plan-root"
    plan_root.mkdir()
    plan_delivery = tmp_path / "plan-delivery"
    plan_delivery.mkdir()
    (plan_delivery / "delivery-sentinel.txt").write_text("plan\n", encoding="utf-8")
    session_root = tmp_path / "session-root"
    session_root.mkdir()
    session_delivery = tmp_path / "session-delivery"
    session_delivery.mkdir()
    (session_delivery / "delivery-sentinel.txt").write_text("session\n", encoding="utf-8")
    sid = "cc-ambient-with-delivery"
    _ambient_session(store, sid, repo_root=str(session_root),
                     delivery_worktree=str(session_delivery))

    plan = _plan(
        tmp_path, "test -f delivery-sentinel.txt",
        meta_extra=(f'repo_root = "{plan_root}"\n'
                    f'delivery_worktree = "{plan_delivery}"'),
        stage_extra='verify_venue = "delivery"',
    )
    record = _only(_run(store, plan, session=sid))
    assert record["cwd"] == str(plan_delivery)
    assert record["verdict"] == cli.CONTROL_RAN


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


# --- the rest of the closed evidence set, each measured on this machine ---------

def test_exit_126_is_could_not_run(store, tmp_path):
    """Found but not executable. Deleting this member left the whole suite
    byte-identical, which is what "the control does not discriminate" means."""
    plan = _plan(tmp_path, "/etc/hostname")
    record = _only(_run(store, plan))
    assert record["verdict"] == cli.CONTROL_COULD_NOT_RUN
    assert record["exit"] == 126


def test_exit_5_is_could_not_run(store, tmp_path):
    """pytest collected nothing at all — a file that EXISTS and holds no tests,
    which is the shape a renamed test module leaves behind. Distinct from the
    exit-4 shape above, where the file itself is gone."""
    empty = tmp_path / "test_no_tests_here.py"
    empty.write_text("x = 1\n", encoding="utf-8")
    plan = _plan(tmp_path, f"python3 -m pytest {empty} -q -p no:cacheprovider")
    record = _only(_run(store, plan))
    assert record["verdict"] == cli.CONTROL_COULD_NOT_RUN
    assert record["exit"] == 5


def test_a_timeout_is_could_not_run(store, tmp_path, monkeypatch):
    """A hanging check has wedged this engine before, which is why the probe owns
    a ceiling the engine's shared runner does not have."""
    monkeypatch.setattr(cli, "_CONTROL_TIMEOUT_S", 1)
    plan = _plan(tmp_path, "sleep 30")
    record = _only(_run(store, plan))
    assert record["verdict"] == cli.CONTROL_COULD_NOT_RUN
    assert record["exit"] is None
    assert "timeout" in record["evidence"]


# --- venue failures, which are _check_one_control's rather than the classifier's -

def test_a_venue_that_is_not_a_directory_is_could_not_run(store, tmp_path):
    """The residual the refusal path leaves alone: with no delivery_worktree
    declared, a missing repo_root is not refusable, and `cd` would fail and `&&`
    short-circuit before the check ever ran."""
    plan = _plan(tmp_path, "true", meta_extra=f'repo_root = "{tmp_path / "absent"}"')
    record = _only(_run(store, plan))
    assert record["verdict"] == cli.CONTROL_COULD_NOT_RUN
    assert record["exit"] is None
    assert "not a directory" in record["evidence"]


def test_a_missing_declared_venue_is_refused(store, tmp_path):
    """REFUSED, not COULD-NOT-RUN: once a delivery_worktree IS declared, a venue
    that does not exist is an authoring error about the plan rather than evidence
    about the check, and the two must not read alike."""
    plan = _plan(
        tmp_path, "true",
        meta_extra=(f'repo_root = "{tmp_path}"\n'
                    f'delivery_worktree = "{tmp_path / "gone-worktree"}"'),
        stage_extra='verify_venue = "delivery"',
    )
    record = _only(_run(store, plan))
    assert record["verdict"] == cli.CONTROL_REFUSED
    assert record["exit"] is None


# --- the omissions: a check the report never mentioned at all --------------------

def test_a_measurable_stage_without_a_verify_command_is_reported_skipped(store, tmp_path):
    """A measurable stage that declares no command used to fall through a bare
    `continue` and vanish from the report — the reading "every declared check
    runs" then covered a stage whose control cannot fail."""
    plan = _raw_plan(tmp_path, """[meta]
task_id = "check-controls-probe"
goal = "probe"
done_criterion = "every declared check runs"
criterion_type = "measurable"

[[stage]]
index = 1
title = "measurable but commandless"
executor = "spawn:developer"
expected_result_image = "nothing executable"
criterion_type = "measurable"
done_criterion = "a flag is set"
depends_on = []
""")
    record = _only(_run(store, plan))
    assert record["verdict"] == cli.CONTROL_SKIPPED
    assert "cannot fail" in record["evidence"]


def test_an_acceptance_review_stage_is_reported_skipped(store, tmp_path):
    """The other silent omission. An acceptance-review stage carries no executable
    check by design, but the report must SAY so rather than leave the reader to
    infer it from a count that does not add up."""
    plan = _raw_plan(tmp_path, """[meta]
task_id = "check-controls-probe"
goal = "probe"
done_criterion = "the user accepts"
criterion_type = "acceptance-review"

[[stage]]
index = 1
title = "the user reads it"
executor = "spawn:developer"
expected_result_image = "the user accepts the prose"
criterion_type = "acceptance-review"
done_criterion = "the user accepts"
depends_on = []
""")
    record = _only(_run(store, plan))
    assert record["verdict"] == cli.CONTROL_SKIPPED
    assert "acceptance-review" in record["evidence"]


# --- dead on arrival vs not yet built --------------------------------------------

def test_a_missing_declared_artifact_is_labelled_not_yet_built(store, tmp_path):
    """At submit-plan time an honestly-authored plan is nearly all COULD-NOT-RUN,
    so a dead control and an unwritten one produce the same headline bit and
    habituation retires the verb. The label is keyed on the DECLARATION plus the
    measured absence, never on the exit code: a not-yet-written pytest node id
    exits 4 or 5 while a not-yet-written SCRIPT exits 2 with `can't open file`,
    and an exit-code rule would label the first and miss the second — which is
    the very shape this plan's own final checks contain."""
    plan = _raw_plan(tmp_path, f"""[meta]
task_id = "check-controls-probe"
goal = "probe"
done_criterion = "every declared check runs"
criterion_type = "measurable"
repo_root = "{tmp_path}"

[[stage]]
index = 1
title = "writes the linter a later check runs"
executor = "spawn:developer"
expected_result_image = "the linter exists"
criterion_type = "measurable"
done_criterion = "the linter answers"
verify_command = "python3 scripts/not-written-yet.py"
expected_exit = 0
output_artifacts = ["scripts/not-written-yet.py"]
depends_on = []

[[stage]]
index = 2
title = "names a script nobody promised"
executor = "spawn:developer"
expected_result_image = "the command reaches its predicate"
criterion_type = "measurable"
done_criterion = "it answers"
verify_command = "python3 scripts/nobody-declared-this.py"
expected_exit = 0
depends_on = [1]
""")
    d = _run(store, plan)
    declared, undeclared = d.data["checks"]
    assert declared["verdict"] == cli.CONTROL_COULD_NOT_RUN
    assert declared["not_yet_built"] == "scripts/not-written-yet.py"
    # The discrimination, not merely the label: a check whose missing script no
    # stage promised stays an ordinary COULD-NOT-RUN.
    assert undeclared["verdict"] == cli.CONTROL_COULD_NOT_RUN
    assert undeclared["not_yet_built"] is None


# --- the enumerator: every data member and every reachable line ------------------

def test_every_dead_exit_member_is_exercised(store, tmp_path):
    """Reflection over the constant itself, so a member added later with no probe
    fails here automatically instead of shipping unexercised — which is how 126
    and 5 shipped in the first place."""
    assert cli._CONTROL_DEAD_EXITS, "the evidence set must not be empty"
    for code, evidence in cli._CONTROL_DEAD_EXITS.items():
        verdict, observed = cli.classify_control_run(code, "", "")
        assert verdict == cli.CONTROL_COULD_NOT_RUN, code
        assert observed == evidence, code


def test_every_interpreter_diagnostic_member_is_exercised(store, tmp_path):
    """The same shape over the diagnostics tuple, asserted on BOTH streams: a
    stderr-only rule misses every check that folds stderr into stdout with `2>&1`,
    which is exactly what the fail-open pipeline idiom does."""
    assert cli._CONTROL_INTERPRETER_DIAGNOSTICS
    for needle in cli._CONTROL_INTERPRETER_DIAGNOSTICS:
        line = f"bash: line 1: {needle} something\n"
        on_stderr = cli.classify_control_run(1, "", line)
        on_stdout = cli.classify_control_run(1, line, "")
        assert on_stderr[0] == cli.CONTROL_COULD_NOT_RUN, needle
        assert on_stdout[0] == cli.CONTROL_COULD_NOT_RUN, needle
        # The evidence must quote the matched string AND its stream: the member
        # fires at any exit code on either stream, so a false alarm has to be
        # self-diagnosing where it fires rather than sending the reader here.
        assert repr(needle) in on_stderr[1] and "stderr" in on_stderr[1]
        assert repr(needle) in on_stdout[1] and "stdout" in on_stdout[1]


def _executable_lines(fn):
    code = fn.__code__
    return {
        lineno for _, _, lineno in code.co_lines()
        if lineno is not None and lineno != code.co_firstlineno
    }


def test_every_line_of_the_classification_path_is_reached(store, tmp_path, monkeypatch):
    """The branch half the data-driven tests cannot supply.

    BOTH functions, because the domain straddles them: the not-a-directory branch
    and the REFUSED verdict are `_check_one_control`'s, so a coverage instrument
    over `classify_control_run` alone would report the classifier fully covered
    while those two sat unexercised. Named for the classification PATH rather than
    for the classifier because a name that states a narrower property than the
    test pins is how the previous draft read as adequate."""
    targets = {cli.classify_control_run.__code__, cli._check_one_control.__code__}
    seen: set[tuple[str, int]] = set()

    def local(frame, event, arg):
        if event == "line":
            seen.add((frame.f_code.co_name, frame.f_lineno))
        return local

    def tracer(frame, event, arg):
        return local if event == "call" and frame.f_code in targets else None

    boom = tmp_path / "boom.py"
    boom.write_text('raise RuntimeError("boom")\n', encoding="utf-8")
    corpora = [
        # _check_one_control's three exits, one plan each.
        _plan(tmp_path, "true", "false", "definitely-not-a-command-xyz",
              f"python3 {boom}", "python3 -m nosuchmodule_xyz", "/etc/hostname",
              name="reached_ok.toml"),
        _plan(tmp_path, "true", name="reached_nodir.toml",
              meta_extra=f'repo_root = "{tmp_path / "absent"}"'),
        _plan(tmp_path, "true", name="reached_refused.toml",
              meta_extra=(f'repo_root = "{tmp_path}"\n'
                          f'delivery_worktree = "{tmp_path / "gone"}"'),
              stage_extra='verify_venue = "delivery"'),
    ]
    monkeypatch.setattr(cli, "_CONTROL_TIMEOUT_S", 1)
    corpora.append(_plan(tmp_path, "sleep 30", name="reached_timeout.toml"))

    sys.settrace(tracer)
    try:
        for plan in corpora:
            _run(store, plan)
        # The process-death arm, which no plan-driven corpus reaches quickly.
        cli.classify_control_run(-9, "", "")
    finally:
        sys.settrace(None)

    for fn in (cli.classify_control_run, cli._check_one_control):
        reached = {ln for name, ln in seen if name == fn.__name__}
        missing = sorted(_executable_lines(fn) - reached)
        assert not missing, f"{fn.__name__} lines never reached: {missing}"


def test_process_death_shapes_are_could_not_run(store, tmp_path):
    """EXECUTED, not simulated: the whole finding is that the returncode a real
    `bash -c` produces is not the one an exit-code table predicts. bash EXECS AWAY
    for a simple command, so a killed shell arrives as a NEGATIVE returncode and
    never as 137; only a compound, where bash survives to reap its child, produces
    the 128+signal shape."""
    shapes = {
        "kill -9 $$": lambda rc: rc < 0,
        "timeout 1 sleep 5": lambda rc: rc == 124,
        "bash -c 'kill -9 $$'; exit $?": lambda rc: 129 <= rc <= 192,
    }
    for command, arm in shapes.items():
        rc, out, err, timed_out = cli._probe_control(command, None, None)
        assert arm(rc), f"{command!r} measured rc {rc}"
        verdict, evidence = cli.classify_control_run(rc, out, err, timed_out=timed_out)
        assert verdict == cli.CONTROL_COULD_NOT_RUN, command
        assert evidence, command


def test_undecodable_output_is_classified_not_raised(store, tmp_path):
    """A check that prints a byte no UTF-8 decoder accepts must yield a verdict,
    not a UnicodeDecodeError out of the pre-flight — the failure is the check's
    material, and a crash reports nothing about any of the other checks."""
    rc, out, err, timed_out = cli._probe_control(r"printf 'A\xffB'; exit 1", None, None)
    assert rc == 1
    verdict, evidence = cli.classify_control_run(rc, out, err, timed_out=timed_out)
    assert verdict in cli._CONTROL_VERDICTS
    assert evidence


def test_every_stage_class_yields_a_record(store, tmp_path):
    """One record per declared check, for every stage class the dispatch loop can
    meet. This is the row the line-coverage test cannot supply: that test traces
    `classify_control_run` and `_check_one_control`, while a silently-omitted
    stage lives one function further out in `cmd_check_controls` — and it is not
    an unreached line but a record never emitted, which no line-coverage
    instrument can see at any width."""
    plan = _raw_plan(tmp_path, f"""[meta]
task_id = "check-controls-probe"
goal = "probe"
done_criterion = "every declared check runs"
criterion_type = "measurable"
repo_root = "{tmp_path}"

[[stage]]
index = 1
title = "measurable with a command"
executor = "spawn:developer"
expected_result_image = "it answers"
criterion_type = "measurable"
done_criterion = "it answers"
verify_command = "true"
expected_exit = 0
depends_on = []

[[stage]]
index = 2
title = "measurable without a command"
executor = "spawn:developer"
expected_result_image = "nothing executable"
criterion_type = "measurable"
done_criterion = "a flag is set"
depends_on = [1]

[[stage]]
index = 3
title = "acceptance review"
executor = "spawn:developer"
expected_result_image = "the user accepts"
criterion_type = "acceptance-review"
done_criterion = "the user accepts"
depends_on = [2]

[[stage]]
index = 4
title = "the engine-synthesized landed check"
executor = "spawn:developer"
expected_result_image = "the commit is contained in the target"
criterion_type = "measurable"
done_criterion = "the commit is contained"
verify_kind = "landed"
depends_on = [1]

[stage.landed]
target = "ticket/probe-branch"
remote = "origin"
delivered_stage = 1
""")
    d = _run(store, plan)
    assert _verdicts(d) == [
        ("stage 1", cli.CONTROL_RAN),
        ("stage 2", cli.CONTROL_SKIPPED),
        ("stage 3", cli.CONTROL_SKIPPED),
        ("stage 4", cli.CONTROL_SKIPPED),
    ]
    assert len(d.data["checks"]) == 4
    # Three of the four are SKIPPED for three DIFFERENT reasons, so the verdict
    # column alone cannot tell them apart: asserting on it only would be passed by
    # an implementation that collapsed all three classes into one arm.
    by_where = {r["where"]: r for r in d.data["checks"]}
    assert "cannot fail" in by_where["stage 2"]["evidence"]
    assert "acceptance-review" in by_where["stage 3"]["evidence"]
    assert by_where["stage 4"]["evidence"] == "engine-synthesized"


# --- an ambient session this verb cannot read ------------------------------------

def test_an_unparseable_session_state_does_not_abort_the_preflight(store, tmp_path):
    """`main` injects the live session into every invocation, so a state file this
    build cannot construct — the measured trigger was a field written by another
    checkout's in-flight work — aborted the entire pre-flight with a TypeError.
    Everything the load supplies is CONVENIENCE: two fallback venue fields the
    plan overrides wherever it declares them.

    Three assertions plus the notice, because (i) alone would pass against a
    repair that swallowed the PLAN's venues along with the session's, and (ii) is
    what stops (iii) passing vacuously — "every record carries the plan's venue"
    is trivially true of an empty report, and reverting the guard makes the call
    raise, so a vacuous assertion would go red for the wrong reason and read as
    evidence."""
    venue = tmp_path / "plan-venue"
    venue.mkdir()
    sid = "cc-unreadable"
    _ambient_session(store, sid, repo_root=str(tmp_path / "session-venue"))
    raw = json.loads(store.path(sid).read_text(encoding="utf-8"))
    raw["runtime_host"] = "a field this build's SessionState does not know"
    store.path(sid).write_text(json.dumps(raw), encoding="utf-8")

    plan = _plan(tmp_path, "true", "false", meta_extra=f'repo_root = "{venue}"')
    d = _run(store, plan, session=sid)                                   # (i)
    assert len(d.data["checks"]) == 2                                    # (ii)
    assert [r["cwd"] for r in d.data["checks"]] == [str(venue)] * 2      # (iii)
    # (iv) the degradation is REPORTED, not hidden: an operator must never be
    # handed a narrower answer than they asked for in silence. Asserted on the
    # signal's presence, not its wording.
    assert d.data["session_error"]
    assert d.data["session_error"] in d.detail
