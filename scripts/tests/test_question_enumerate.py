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
