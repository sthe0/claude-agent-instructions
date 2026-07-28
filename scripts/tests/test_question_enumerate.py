"""question-enumerate: the independent advisor pass that RAISES the questions a
plan's construction should have provoked, so a second reading is structurally
required rather than merely available.

Two surfaces, tested with a STUB runner (never a live `claude -p`):

  * advisor.enumerate_questions(goal, done_criterion, plan_text, runner) — the
    fail-open pure pass, mirroring enumerate_claims: parses `<target>\\t<question>`
    lines, DROPS malformed ones, and returns [] on a None runner / non-zero exit /
    any exception (advisor-absent stays byte-identical to advisor-present-silent).
  * cli.cmd_question_enumerate — ONE call over the whole plan that writes each pair
    as a 'raised' QuestionCandidate, flips bag['enumerated']/['enumerated_at']
    REGARDLESS of the count (a count-gate would let a timeout wedge approve shut with
    no route out), records runner health, and attaches a NON-BLOCKING advisory (F3b)
    whenever the pass produced nothing or the runner did not report healthy.
"""
from __future__ import annotations

from argparse import Namespace
from types import SimpleNamespace

from agentctl import advisor, cli, plugins, plugins_premise
from agentctl.state import SessionState


# --- stub runner ---------------------------------------------------------------

def _runner(stdout, *, returncode=0):
    """A stub advisor runner: returns a fixed RunResult-shaped object, records the
    argv it was handed so a test can assert exactly one call was made."""
    calls: list[list[str]] = []

    def run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    run.calls = calls
    return run


# --- plan helper (mirrors test_question_cli._write_plan) -----------------------

_STAGE_TMPL = """\
[[stage]]
index = {i}
title = "Stage {i}"
executor = "spawn:developer"
expected_result_image = "{img}"
criterion_type = "measurable"
done_criterion = "stage {i} done"
depends_on = {deps}
output_artifacts = ["s{i}.py"]
"""


def _write_plan(path, stages):
    body = [
        "[meta]",
        'task_id = "demo-enumerate"',
        'goal = "exercise question-enumerate"',
        'done_criterion = "all stages PASSED"',
        'criterion_type = "measurable"',
        "",
    ]
    prev = None
    for i, img in stages:
        deps = "[]" if prev is None else f"[{prev}]"
        body.append(_STAGE_TMPL.format(i=i, img=img, deps=deps))
        prev = i
    path.write_text("\n".join(body), encoding="utf-8")
    return path


def _state(store, sid="s", *, plan_path=None):
    state = SessionState(session_id=sid, task_id="t")
    plugins.activate(state, "premise")
    if plan_path is not None:
        state.plan_path = str(plan_path)
    store.save(state)
    return state


def _enumerate(store, sid, run):
    return cli.cmd_question_enumerate(Namespace(session=sid), store=store, runner=run)


# --- advisor.enumerate_questions: the pure fail-open pass -----------------------

def test_enumerate_parses_target_question_pairs():
    out = "plan.goal\tis the goal actually agreed?\nstage:1.means\twhy this tool?"
    pairs = advisor.enumerate_questions("g", "d", "p", _runner(out))
    assert pairs == [
        ("plan.goal", "is the goal actually agreed?"),
        ("stage:1.means", "why this tool?"),
    ]


def test_enumerate_drops_malformed_lines():
    # a well-formed pair, then three malformed lines (no tab, empty question,
    # empty target) — the malformed ones are DROPPED, never raised.
    out = "plan.goal\tgood question?\nno-tab-here\nstage:2.result\t\n\twhat about this?"
    pairs = advisor.enumerate_questions("g", "d", "p", _runner(out))
    assert pairs == [("plan.goal", "good question?")]


def test_enumerate_fails_open_on_nonzero_exit():
    run = _runner("plan.goal\tshould never be read", returncode=1)
    assert advisor.enumerate_questions("g", "d", "p", run) == []
    # None runner and a throwing runner also fail open (mirrors the stage probe).
    assert advisor.enumerate_questions("g", "d", "p", None) == []

    def boom(argv, **kw):
        raise OSError("no binary")

    assert advisor.enumerate_questions("g", "d", "p", boom) == []


# --- cmd_question_enumerate: exactly one call ----------------------------------

def test_makes_exactly_one_call(store, tmp_path):
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one"), (2, "img-two")])
    _state(store, plan_path=plan_path)
    run = _runner("plan.goal\tq1?\nstage:1.means\tq2?")
    d = _enumerate(store, "s", run)
    assert d.ok is True
    assert len(run.calls) == 1  # ONE bounded call over the whole plan
    argv = run.calls[0]
    assert argv[:4] == ["claude", "-p", "--model", "sonnet"]  # inherited model, not redeclared

    bag = store.load("s").plugins["premise"]
    assert [c["disposition"] for c in bag["candidates"]] == ["raised", "raised"]
    assert bag["candidates"][0]["statement"] == "[plan.goal] q1?"
    assert bag["enumerated"] is True
    assert bag["enumerated_count"] == 2
    assert bag["enumerated_runner_ok"] is True


# --- the flag flips REGARDLESS of the count ------------------------------------

def test_flag_flips_even_with_zero_candidates(store, tmp_path):
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one")])
    _state(store, plan_path=plan_path)
    d = _enumerate(store, "s", _runner(""))  # runner healthy, no questions raised
    assert d.ok is True

    live = store.load("s")
    bag = live.plugins["premise"]
    assert bag["enumerated"] is True
    assert bag["enumerated_count"] == 0
    assert bag["candidates"] == []
    # the mandatory-cross-check blocker is discharged by the flag, so the gate no
    # longer reports "not run" — the pass HAVING RUN is what clears it.
    blockers = plugins_premise.premise_blockers(live, bag)
    assert not any("not run" in b for b in blockers)


# --- F3b: the non-blocking advisory fires on the silent-rot paths --------------

def test_zero_candidates_attaches_advisory(store, tmp_path):
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one")])
    _state(store, plan_path=plan_path)
    d = _enumerate(store, "s", _runner(""))  # exit 0 but nothing raised
    advisories = d.data.get("advisories", [])
    assert advisories and any("by hand" in a for a in advisories)
    # the advisory is non-blocking: the directive still passes.
    assert d.ok is True


def test_runner_failure_attaches_advisory(store, tmp_path):
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one")])
    _state(store, plan_path=plan_path)
    run = _runner("plan.goal\tshould never be read", returncode=1)
    d = _enumerate(store, "s", run)
    assert d.ok is True  # fail-open: a broken advisor never wedges the verb

    bag = store.load("s").plugins["premise"]
    assert bag["enumerated"] is True          # flag STILL flips on runner failure
    assert bag["enumerated_runner_ok"] is False
    assert bag["enumerated_count"] == 0
    assert bag["candidates"] == []
    advisories = d.data.get("advisories", [])
    assert advisories and any("unavailable or failed" in a for a in advisories)


# --- a stale enumerated_at re-blocks approve after a content change -------------

def test_stale_enumerated_at_reblocks(store, tmp_path):
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one"), (2, "img-two")])
    _state(store, plan_path=plan_path)
    _enumerate(store, "s", _runner(""))  # enumerated_at stamped against current content

    live = store.load("s")
    bag = live.plugins["premise"]
    assert not any("re-run" in b for b in plugins_premise.premise_blockers(live, bag))

    # change stage 2's definition -> the plan's content digest rotates
    _write_plan(plan_path, [(1, "img-one"), (2, "img-two-EDITED")])
    live = store.load("s")
    blockers = plugins_premise.premise_blockers(live, live.plugins["premise"])
    assert any("different plan content" in b for b in blockers)


# --- #48: --plan names the plan to enumerate -----------------------------------

def test_plan_flag_binds_enumerated_at_to_the_named_plan(store, tmp_path):
    """The corrected plan of a replan is not yet state.plan_path, so without a way
    to name it the enumeration could only ever stamp the OLD plan's digest — and the
    premise gate, which cmd_replan evaluates against the corrected plan, then blocked
    the very replan that would clear it."""
    from agentctl.plan import load_plan

    current = _write_plan(tmp_path / "plan.toml", [(1, "img-one")])
    corrected = _write_plan(tmp_path / "corrected.toml", [(1, "img-one"), (2, "img-two")])
    _state(store, plan_path=current)

    d = cli.cmd_question_enumerate(
        Namespace(session="s", plan=str(corrected)), store=store, runner=_runner(""))
    assert d.ok is True

    bag = store.load("s").plugins["premise"]
    assert bag["enumerated_at"] == plugins_premise._plan_content_digest(load_plan(corrected))
    assert bag["enumerated_at"] != plugins_premise._plan_content_digest(load_plan(current))


def test_omitting_plan_flag_reproduces_previous_behaviour(store, tmp_path):
    """The default must be byte-identical to the pre-flag command, for both call
    shapes: an args object carrying plan=None, and one with no `plan` attribute at
    all (every pre-existing caller, including the parser-free test helper)."""
    from agentctl.plan import load_plan

    current = _write_plan(tmp_path / "plan.toml", [(1, "img-one")])
    expected = plugins_premise._plan_content_digest(load_plan(current))

    _state(store, sid="explicit-none", plan_path=current)
    cli.cmd_question_enumerate(
        Namespace(session="explicit-none", plan=None), store=store, runner=_runner(""))
    assert store.load("explicit-none").plugins["premise"]["enumerated_at"] == expected

    _state(store, sid="absent-attr", plan_path=current)
    cli.cmd_question_enumerate(
        Namespace(session="absent-attr"), store=store, runner=_runner(""))
    assert store.load("absent-attr").plugins["premise"]["enumerated_at"] == expected


def test_plan_flag_rejects_an_unreadable_path(store, tmp_path):
    """--plan takes a caller-supplied path, so a bad one is ordinary bad input and
    must return a Directive rather than raising out of the command."""
    current = _write_plan(tmp_path / "plan.toml", [(1, "img-one")])
    _state(store, plan_path=current)

    d = cli.cmd_question_enumerate(
        Namespace(session="s", plan=str(tmp_path / "nope.toml")),
        store=store, runner=_runner(""))
    assert d.ok is False
    assert "cannot read plan" in d.detail
    assert store.load("s").plugins["premise"]["enumerated"] is False


def test_plan_flag_rejects_a_malformed_plan(store, tmp_path):
    """An existing but unparseable --plan is a SECOND kind of bad caller input and
    takes a different branch from the unreadable-path case: the file reads fine and
    `load_plan` raises. Without the handler it comes out of the command as a
    traceback rather than a Directive, so the branch is pinned separately rather
    than assumed covered by the OSError one."""
    current = _write_plan(tmp_path / "plan.toml", [(1, "img-one")])
    _state(store, plan_path=current)
    broken = tmp_path / "broken.toml"
    broken.write_text("not = [toml\n", encoding="utf-8")

    d = cli.cmd_question_enumerate(
        Namespace(session="s", plan=str(broken)), store=store, runner=_runner(""))
    assert d.ok is False
    assert "cannot parse plan" in d.detail
    assert store.load("s").plugins["premise"]["enumerated"] is False


def test_plan_flag_rejects_an_empty_path(store, tmp_path):
    """`--plan ''` is nonsense input, and falling back to the session's own plan
    would enumerate a DIFFERENT plan than the caller named while reporting success —
    the silent-wrong-object shape this flag exists to make impossible."""
    current = _write_plan(tmp_path / "plan.toml", [(1, "img-one")])
    _state(store, plan_path=current)

    d = cli.cmd_question_enumerate(
        Namespace(session="s", plan="   "), store=store, runner=_runner(""))
    assert d.ok is False
    assert "empty path" in d.detail
    assert store.load("s").plugins["premise"]["enumerated"] is False


def test_enumerated_plan_records_which_plan_was_read(store, tmp_path):
    """Provenance for the orphan-candidate case `--plan` introduces: an abandoned
    pass can leave candidates raised from a plan no longer under evaluation, and
    without this the operator meets an unexplained approve blocker with nothing to
    trace it to."""
    current = _write_plan(tmp_path / "plan.toml", [(1, "img-one")])
    other = _write_plan(tmp_path / "corrected.toml", [(1, "img-one"), (2, "img-two")])
    _state(store, plan_path=current)

    cli.cmd_question_enumerate(
        Namespace(session="s", plan=str(other)), store=store, runner=_runner(""))
    assert store.load("s").plugins["premise"]["enumerated_plan"] == str(other)

    cli.cmd_question_enumerate(
        Namespace(session="s", plan=None), store=store, runner=_runner(""))
    assert store.load("s").plugins["premise"]["enumerated_plan"] == str(current)
