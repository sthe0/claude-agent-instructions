"""The echo check: a stage whose expected_result_image restates its own control.

Defect 5 of the SMD act-modelling rework. A stage declares an image of the RESULT and,
separately, a CONTROL that judges it; the defect is the first collapsing into the second,
so the plan records that a check went well and never records what now exists.

The remedy is deliberately weak, and that weakness is the thing these tests pin:

  * It WARNS. An echo verdict never refuses a submission, at any of the three seams. The
    independent labelling found 11 echoes in 200 images where the estimate the refusal had
    been sized on said 58 — a defect that rare does not earn an author's round trip, and a
    verdict resting on a model's reading of a sentence should not spend one. The two
    submission tests below assert the POSITIVE direction (submitted, PLAN-READY, warning
    present), so a later re-tightening into a refusal turns them red instead of passing in
    a silence nobody reads.
  * It is fail-open. No judge, a judge that errors, a judge that returns nothing usable —
    no warning at all, and the submission is byte-identical to the feature being absent.
  * It keeps the loader pure. The prefilter is structural and runs anywhere; only the judge
    spawns anything, and only from the submission seam. `parse_plan` spawns nothing.

The calibration of the prefilter itself — recall and false positives against the labelled
corpus, and the proof the labels preceded the mechanism — lives in test_prefilter_recall.py.
"""
from __future__ import annotations

import json
import subprocess
import tomllib
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import advisor, cli, plan as plan_mod
from agentctl.dispatch import RunResult
from agentctl.result_image import echo_prefilter, judge_echo
from agentctl.state import Node
from agentctl.submission import submission_advice, validate_submission

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CORPUS_DIR = FIXTURES / "plan_corpus"

# Fixtures taken from the labelled corpus BY KEY rather than pasted, so a test can never
# drift from the label it claims to exercise. Both echoes and the genuine image come from
# one plan file: same author, same week, same subject matter — the difference under test is
# the images, not the plans around them.
ECHO_KEYS = ("effort-divergence-trigger.toml:3", "effort-divergence-trigger.toml:4")
GENUINE_KEY = "agentctl-pop-pointer-v1.toml:2"


def ns(**kw):
    return Namespace(**kw)


def _corpus_image(key: str) -> str:
    name, index = key.rsplit(":", 1)
    doc = tomllib.loads((CORPUS_DIR / name).read_text(encoding="utf-8"))
    for stage in doc["stage"]:
        if stage["index"] == int(index):
            return stage["expected_result_image"]
    raise AssertionError(f"no stage {index} in {name}")


def _labelled(key: str) -> str:
    labels = json.loads(
        (FIXTURES / "result_image_echo_labels.json").read_text(encoding="utf-8")
    )
    for row in labels["labels"]:
        if row["key"] == key:
            return row["label"]
    raise AssertionError(f"{key} is not in the labels file")


_PLAN = """
[meta]
task_id = "ri"
goal = "g"
done_criterion = "dc"
criterion_type = "measurable"
weight_class = "substantive"
external_research = "none applies"

[[stage]]
index = 1
title = "the stage under test"
executor = "in_thread"
expected_result_image = {image}
criterion_type = "measurable"
done_criterion = "d"
verify_command = "pytest -q"
material = "m"
means = "bash"
method = "run"
conditions = "c"
invariants = "n"
capability_required = "cap"
material_refs = ["scripts/agentctl/result_image.py"]
knowledge_refs = ["scripts/agentctl/submission.py"]
knowledge = "how the submission seam differs from the loader"
[stage.principle]
statement = "s"
source = "src"
derivation = "der"
confidence = "high"
refutation = "r"
"""


def _write_plan(path: Path, image: str) -> str:
    # json.dumps is a valid TOML basic string for these values, and it escapes the
    # backticks, quotes and dashes real corpus images carry.
    path.write_text(_PLAN.format(image=json.dumps(image)), encoding="utf-8")
    return str(path)


def _judge(verdict: str):
    """A judge runner that always answers `verdict`."""
    def run(argv, timeout=None):
        return RunResult(0, verdict + "\n", "")
    return run


def _judge_unavailable(argv, timeout=None):
    """The judge cannot be reached — the shape a missing/failing `claude -p` takes."""
    return RunResult(1, "", "no such model")


def _judge_raises(argv, timeout=None):
    raise OSError("judge process could not be started")


@pytest.fixture(autouse=True)
def _advisor_on(monkeypatch):
    """The seam resolves its judge through advisor.resolve_enabled, whose documented
    force-on knob is this env var. Set explicitly so these tests do not depend on the
    machine's config.md advisor-mode."""
    monkeypatch.setenv("AGENTCTL_ADVISOR", "1")


def _submit(store, plan_path, runner):
    cli.cmd_start(ns(session="ri", task="ri", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session="ri", chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session="ri"), store=store)
    return cli.cmd_submit_plan(ns(session="ri", plan=plan_path), store=store, runner=runner)


def _advisories(d) -> list[str]:
    return list(d.data.get("advisories", []))


def _echo_warnings(d) -> list[str]:
    return [a for a in _advisories(d) if "restates the stage's own check" in a]


# --- the remedy warns, and never refuses ------------------------------------


@pytest.mark.parametrize("key", ECHO_KEYS)
def test_echo_image_is_submitted_and_carries_a_warning(store, tmp_path, key):
    """POSITIVE direction on purpose. Both halves are the assertion: the plan goes through
    (ok, PLAN-READY, the session actually at PLAN_READY) AND the author is told which stage
    and what it restates. A remedy re-tightened into a refusal fails the first half; a
    remedy quietly dropped fails the second."""
    assert _labelled(key) == "echo", "fixture drift: this key is not a labelled echo"
    d = _submit(store, _write_plan(tmp_path / "p.toml", _corpus_image(key)), _judge("ECHO"))

    assert d.ok is True
    assert d.marker == "PLAN-READY"
    assert store.load("ri").node == Node.PLAN_READY.value
    assert not d.data.get("problems"), "an echo must never appear as a submission problem"

    warnings = _echo_warnings(d)
    assert len(warnings) == 1
    assert "stage 1" in warnings[0]
    assert "'the stage under test'" in warnings[0]


def test_genuine_image_submits_with_no_warning(store, tmp_path):
    """The judge is stubbed to say ECHO about everything, so silence here can only come
    from the prefilter declining to ask — which is what keeps the model off 189 of the
    corpus's 200 images."""
    assert _labelled(GENUINE_KEY) == "genuine"
    d = _submit(store, _write_plan(tmp_path / "p.toml", _corpus_image(GENUINE_KEY)),
                _judge("ECHO"))

    assert d.ok is True
    assert _echo_warnings(d) == []


@pytest.mark.parametrize("runner", [_judge_unavailable, _judge_raises])
def test_judge_unavailable_warns_about_nothing(store, tmp_path, runner):
    """Fail-open: the prefilter alone is never allowed to produce a verdict. An image that
    trips every structural condition, with no judge to ask, must submit exactly as it would
    have before this check existed."""
    image = _corpus_image(ECHO_KEYS[0])
    assert echo_prefilter(image), "fixture drift: this image no longer trips the prefilter"

    d = _submit(store, _write_plan(tmp_path / "p.toml", image), runner)

    assert d.ok is True
    assert _echo_warnings(d) == []


def test_judge_verdict_decides_between_two_identical_prefilter_hits(store, tmp_path):
    """The prefilter proposes and the judge disposes: the same bytes, warned about or not
    purely on the judge's answer. This is the boundary the whole design rests on — a
    structural test may not classify meaning."""
    image = _corpus_image(ECHO_KEYS[0])
    plan = _write_plan(tmp_path / "p.toml", image)

    assert submission_advice(plan_mod.load_plan(plan), judge_runner=_judge("ECHO"))
    assert submission_advice(plan_mod.load_plan(plan), judge_runner=_judge("GENUINE")) == []


def test_no_runner_reaches_the_judge_via_the_production_fallback(store, tmp_path, monkeypatch):
    """`main()` always calls `cmd_submit_plan` with `runner=None` in production — this is
    what proves the judge is actually reachable there, not merely that a runner passed in
    by a test can trigger it. The three cmd_* entry points (submit_plan, approve, replan)
    now resolve `runner if runner is not None else advisor.subprocess_runner`, matching the
    pre-existing idiom at the ledger/question/acceptance-review call sites, so `runner=None`
    no longer means "no judge" the way `submission_advice`'s own `judge_runner=None` still
    does when called directly (see test_judge_verdict_decides_between_two_identical_
    prefilter_hits above)."""
    calls: list[list[str]] = []

    def fake_subprocess_runner(argv, *, timeout=None):
        calls.append(argv)
        return RunResult(0, "ECHO\n", "")

    monkeypatch.setattr(advisor, "subprocess_runner", fake_subprocess_runner)

    image = _corpus_image(ECHO_KEYS[0])
    d = _submit(store, _write_plan(tmp_path / "p.toml", image), None)

    assert calls, "cmd_submit_plan(runner=None) never reached advisor.subprocess_runner"
    assert _echo_warnings(d)


def test_advisor_off_reaches_no_runner_and_spawns_nothing(store, tmp_path, monkeypatch):
    """The production fallback makes the runner non-None, but `advisor.resolve_enabled`
    stays the real kill switch: with the advisor off, an echo image submits silently and
    `advisor.subprocess_runner` is never even looked at, let alone called."""
    monkeypatch.setenv("AGENTCTL_ADVISOR", "0")

    def forbidden(argv, *, timeout=None):
        raise AssertionError("advisor.subprocess_runner was called with the advisor off")

    monkeypatch.setattr(advisor, "subprocess_runner", forbidden)

    image = _corpus_image(ECHO_KEYS[0])
    d = _submit(store, _write_plan(tmp_path / "p.toml", image), None)

    assert d.ok is True
    assert _echo_warnings(d) == []


def test_validate_submission_returns_advice_without_raising(tmp_path):
    """The seam's raising wrapper gained a way to SAY something. Existing callers, which
    pass no judge and ignore the return, still see the same nothing."""
    plan = plan_mod.load_plan(_write_plan(tmp_path / "p.toml", _corpus_image(ECHO_KEYS[0])))

    assert validate_submission(plan) == []
    assert validate_submission(plan, judge_runner=_judge("ECHO"))


# --- the prefilter's structural conditions ----------------------------------


def test_prefilter_flags_an_image_containing_its_own_verify_command():
    """The degenerate case: an image that transcribes the command judging it has stopped
    describing a result. Structural, and reachable by an image no corpus contains."""
    command = "python3 -m pytest scripts/tests/test_thing.py -q"
    image = f"The migration is complete once {command} reports no failures."

    assert not echo_prefilter(image)
    reasons = echo_prefilter(image, verify_command=command)
    assert any("verify_command" in r for r in reasons)


def test_prefilter_ignores_a_leading_code_span_that_is_not_a_command():
    """An image may legitimately open by naming the symbol it created. Only an opening
    INVOCATION is evidence the author began with the check."""
    assert not echo_prefilter("`_restore_current_stage` exists as a module-level helper.")
    assert echo_prefilter("`pytest scripts/tests -q` exits 0.")


def test_prefilter_reads_clauses_not_the_whole_string():
    """Verdict-saturation is a per-clause property: one clause that names a state of the
    world is enough to rescue an image whose other clause reports a verdict. A whole-string
    test cannot see the difference."""
    saturated = "The suite passes; every case is asserted."
    rescued = "The suite passes; config.md carries one table row per constant."

    assert any("every clause" in r for r in echo_prefilter(saturated))
    assert not any("every clause" in r for r in echo_prefilter(rescued))


def test_judge_echo_is_fail_open_in_every_direction():
    for runner in (None, _judge_unavailable, _judge_raises, _judge("")):
        assert judge_echo("`pytest -q` exits 0.", runner) is False
    assert judge_echo("`pytest -q` exits 0.", _judge("ECHO"), enabled=False) is False
    assert judge_echo("`pytest -q` exits 0.", _judge("ECHO")) is True


# --- the loader stays pure --------------------------------------------------


def test_parse_plan_spawns_no_subprocess(monkeypatch, tmp_path):
    """`parse_plan` is documented pure, and this check is the reason that documentation is
    now load-bearing: the echo remedy introduced a model call into the submission path, and
    the loader is reached with strict=True from seven in-session call sites, several of
    which RE-READ a plan the session already accepted. A subprocess on that path would put
    a model in front of every re-read.

    Guards the whole subprocess surface rather than one function — Popen is what run(),
    check_output() and the rest are built on."""
    def forbidden(*args, **kwargs):
        raise AssertionError(f"parse_plan spawned a subprocess: {args!r}")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)

    path = _write_plan(tmp_path / "p.toml", _corpus_image(ECHO_KEYS[0]))
    doc = plan_mod.load_plan(path)

    assert doc.stages[0].subject.result == _corpus_image(ECHO_KEYS[0])
