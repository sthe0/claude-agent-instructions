"""Each `conditions` line is a world state marked by how it is secured (fix D2).

A premise about the environment adopted at planning without a probe fails the stage that rests
on it; meanwhile `conditions` filled with conduct norms. Under the `conditions-securing` flag the
submission seam refuses a substantive stage whose condition lacks a securing mark
(`agentctl/conditions_securing.py`); approve logs a `fix-firing/v1` row per new `probe-revised`
mark and one operating row.

These tests drive the real `submit-plan` / `approve` seams with the flag forced on through
AGENTCTL_CONDITIONS_SECURING, so on a tree without the fix they fail by assertion: the plan is
accepted, or no row is written.
"""
from __future__ import annotations

import importlib.util
import json
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli
from agentctl.config import parse_config_md
from agentctl.dispatch import RunResult
from agentctl.state import Node
from conftest import SUBSTANTIVE_FINAL_CHECK, SUBSTANTIVE_ORDER, declare_venue
from plan_site_fixture import GROUNDED_SOURCE

FLAG_ENV = "AGENTCTL_CONDITIONS_SECURING"

PROBE = "- The fixture directory is present [probe: ls scripts/tests/fixtures => 1 dir listed]"
REVISED = ("- The base branch is main at 5d52233a [probe-revised: git rev-parse main => 5d52233a; "
           "was: the base branch is trunk]")
MEANS = "- The parser reads the new key [means: stage 1]"
RISK = "- The judge endpoint answers within 15 s [risk: unprobed; a timeout only fails open]"
UNMARKED = "Edits only in the worktree; no push."

_STAGE = """
[[stage]]
index = {index}
title = "{title}"
executor = "in_thread"
expected_result_image = "The seam refuses an unsecured condition."
criterion_type = "measurable"
done_criterion = "d{index}"
verify_command = "pytest -q"
negative_control = "false"
material = "m{index} (traces to R1)"
means = "bash"
method = "run"
procedure = "1. read the fixture. 2. apply the edit. 3. re-check the seam"
conditions = {conditions}
preconditions = "The branch is checked out."
invariants = "the canon checkout stays read-only"
capability_required = "cap"
material_refs = ["scripts/agentctl/submission.py"]
knowledge_refs = ["scripts/agentctl/conditions_securing.py"]
knowledge = "where a submission requirement may bind"
{supplies}[stage.principle]
statement = "s"
source = '{source}'
derivation = "der"
confidence = "high"
refutation = "r"
"""

_META = """
[meta]
task_id = "cs"
goal = "g"
done_criterion = "dc"
criterion_type = "measurable"
weight_class = "substantive"
external_research = "none applies"
"""


def _write_plan(path: Path, c1: str, c2: str) -> str:
    stages = "".join(
        _STAGE.format(index=i, title=title, conditions=json.dumps(c), source=GROUNDED_SOURCE,
                      supplies=supplies)
        for i, title, c, supplies in (
            (1, "the stage that goes first", c1, ""),
            (2, "the stage under test", c2, '[[stage.supplies]]\non = 1\nelement = "result"\n'),
        )
    )
    path.write_text(declare_venue(_META + SUBSTANTIVE_ORDER + stages + SUBSTANTIVE_FINAL_CHECK,
                                  path.parent), encoding="utf-8")
    return str(path)


def _no_judge(argv, timeout=None):
    """Every model judge unreachable: the restatement check fails open, so only the structural
    securing check can refuse."""
    return RunResult(1, "", "no such model")


def ns(**kw):
    return Namespace(**kw)


def _start(store, session):
    cli.cmd_start(ns(session=session, task="cs", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=session, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=session), store=store)


def _submit(store, plan, session="cs"):
    return cli.cmd_submit_plan(ns(session=session, plan=plan), store=store, runner=_no_judge)


def _securing(d) -> list[str]:
    return [p for p in d.data.get("problems", []) if "`conditions` not secured" in p]


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv(FLAG_ENV, "1")


def test_unmarked_condition_is_refused(store, tmp_path, flag_on):
    _start(store, "cs")
    plan = _write_plan(tmp_path / "p.toml", PROBE, UNMARKED)

    d = _submit(store, plan)

    assert d.ok is False
    assert store.load("cs").node == Node.PLANNING.value
    refusals = _securing(d)
    assert len(refusals) == 1 and "stage 2" in refusals[0], d.data.get("problems")
    assert "invariants" in refusals[0]


def test_each_mark_kind_is_accepted(store, tmp_path, flag_on):
    _start(store, "cs")
    plan = _write_plan(tmp_path / "p.toml", PROBE, "\n".join([REVISED, MEANS, RISK]))

    d = _submit(store, plan)

    assert d.ok is True, d.data.get("problems")
    assert getattr(store.load("cs"), "conditions_securing_required", None) is True


def test_means_naming_a_non_ancestor_is_refused(store, tmp_path, flag_on):
    _start(store, "cs")
    # Stage 1 depends on nothing, so stage 2 cannot secure its condition.
    plan = _write_plan(tmp_path / "p.toml", "- The tree is clean [means: stage 2]", PROBE)

    d = _submit(store, plan)

    assert d.ok is False
    refusals = _securing(d)
    assert len(refusals) == 1 and "stage 1" in refusals[0], d.data.get("problems")
    assert "not a transitive depends_on ancestor" in refusals[0]


@pytest.mark.parametrize("bad", [
    "- The fixture is present [probe: ls fixtures => ]",
    "- The base is main [probe-revised: git rev-parse main => 5d52233a]",
    "- The base is main [probe-revised: git rev-parse main => 5d52233a; was: ]",
])
def test_probe_without_result_or_revision_without_was_is_refused(store, tmp_path, flag_on, bad):
    _start(store, "cs")
    plan = _write_plan(tmp_path / "p.toml", PROBE, bad)

    d = _submit(store, plan)

    assert d.ok is False
    assert len(_securing(d)) == 1, d.data.get("problems")


def test_in_flight_session_is_not_refused_after_the_flag_lands(store, tmp_path, monkeypatch):
    """The requirement is decided once, at the session's first accepted submission. A plan
    accepted while the flag was off stays exempt when the flag turns on mid-session: approve
    re-validates the (possibly edited) plan bytes with the STORED decision."""
    monkeypatch.setenv(FLAG_ENV, "0")
    _start(store, "cs")
    plan = _write_plan(tmp_path / "p.toml", UNMARKED, UNMARKED)
    assert _submit(store, plan).ok is True
    assert getattr(store.load("cs"), "conditions_securing_required", False) is False

    monkeypatch.setenv(FLAG_ENV, "1")
    d = cli.cmd_approve(ns(session="cs", by="user"), store=store)

    assert d.ok is True, d.data.get("problems")
    assert store.load("cs").node == Node.APPROVED.value


@pytest.mark.parametrize("bad, phrase", [
    ("Secured as below.\n" + PROBE, "text before the first `- ` condition line"),
    ("- The tree is clean [probe: git status => clean] [risk: unprobed]",
     "carries 2 marks; exactly one is allowed"),
    ("- The tree is clean [probe: git status => clean]\n  and the branch is main",
     "a line follows its mark; the mark must end the condition"),
    ("- The tree is clean [probe: git status => clean] and the branch is main",
     "text follows its mark; the mark must end the line"),
])
def test_condition_grammar_violations_are_refused(store, tmp_path, flag_on, bad, phrase):
    _start(store, "cs")
    plan = _write_plan(tmp_path / "p.toml", PROBE, bad)

    d = _submit(store, plan)

    assert d.ok is False
    refusals = _securing(d)
    assert len(refusals) == 1 and "stage 2" in refusals[0], d.data.get("problems")
    assert phrase in refusals[0]


def test_kill_switch_overrides_a_stored_decision(store, tmp_path, monkeypatch, flag_on):
    """AGENTCTL_CONDITIONS_SECURING=0 suspends a session's stored True; the stored decision
    itself is left as it was."""
    _start(store, "cs")
    plan = _write_plan(tmp_path / "p.toml", PROBE, PROBE)
    assert _submit(store, plan).ok is True
    assert store.load("cs").conditions_securing_required is True

    _write_plan(tmp_path / "p.toml", PROBE, UNMARKED)
    monkeypatch.setenv(FLAG_ENV, "0")
    d = cli.cmd_approve(ns(session="cs", by="user"), store=store)

    assert d.ok is True, d.data.get("problems")
    assert store.load("cs").conditions_securing_required is True


def test_telemetry_failure_never_blocks_submit_or_approve(store, tmp_path, monkeypatch, flag_on):
    from agentctl import cli_common

    def boom(*a, **k):
        raise OSError("telemetry sink unavailable")

    monkeypatch.setattr(cli_common, "_securing_spend", boom)
    monkeypatch.setattr(cli_common.fix_firings, "append", boom)
    _start(store, "cs")
    plan = _write_plan(tmp_path / "p.toml", PROBE, "\n".join([REVISED, RISK]))

    assert _submit(store, plan).ok is True
    d = cli.cmd_approve(ns(session="cs", by="user"), store=store)

    assert d.ok is True, d.data.get("problems")
    assert store.load("cs").node == Node.APPROVED.value


def test_in_flight_session_is_not_refused_at_replan(store, tmp_path, monkeypatch):
    """A session accepted with the flag off replans an unmarked plan after the flag turns on:
    the replan seam reads the stored decision, not the live flag."""
    from agentctl import cli_replan

    monkeypatch.setenv(FLAG_ENV, "0")
    _start(store, "cs")
    plan = _write_plan(tmp_path / "p.toml", UNMARKED, UNMARKED)
    assert _submit(store, plan).ok is True
    assert cli.cmd_approve(ns(session="cs", by="user"), store=store).ok is True
    cli.cmd_partition(ns(session="cs", m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session="cs"), store=store)
    cli.cmd_record_result(ns(session="cs", status="failed", actual="boom"), store=store)
    cli.cmd_declare(ns(session="cs", expected="e", actual="a", mismatch="m"), store=store)
    cli.cmd_investigate(ns(session="cs", localized_expectation="le", localized_actual="la",
                           hypotheses=["h1", "h2"]), store=store)
    cli.cmd_critique(ns(session="cs", functional_ground="fg", replanning_task="rt",
                        failure_address="normative", norm_targets=["stage:1"],
                        cause_category="implementation_defect", machinery_induced="false",
                        mechanism="none", functional_place="verification"), store=store)
    cli.cmd_normalize(ns(session="cs", factor="reproducible cause", level="note"), store=store)

    seen: list[bool] = []
    real = cli_replan._submission_problems

    def spy(*a, **k):
        seen.append(k.get("conditions_securing_required"))
        return real(*a, **k)

    monkeypatch.setattr(cli_replan, "_submission_problems", spy)
    monkeypatch.setenv(FLAG_ENV, "1")
    refined = _write_plan(tmp_path / "refined.toml", UNMARKED, UNMARKED + " Stage 2 re-run.")
    d = cli.cmd_replan(ns(session="cs", plan=refined), store=store, runner=_no_judge)

    assert seen == [False], (seen, d.detail)
    assert not _securing(d), d.data.get("problems")


def test_flag_off_accepts_an_unmarked_plan(store, tmp_path, monkeypatch):
    monkeypatch.setenv(FLAG_ENV, "0")
    _start(store, "cs")
    plan = _write_plan(tmp_path / "p.toml", UNMARKED, UNMARKED)

    d = _submit(store, plan)

    assert d.ok is True
    assert not _securing(d)


def test_approve_writes_firing_and_operating_rows(store, tmp_path, monkeypatch, flag_on):
    log = tmp_path / "fix-firings.jsonl"
    monkeypatch.setenv("AGENTCTL_FIX_FIRINGS_LOG", str(log))
    _start(store, "cs")
    plan = _write_plan(tmp_path / "p.toml", PROBE, UNMARKED)
    assert _submit(store, plan).ok is False

    _write_plan(tmp_path / "p.toml", PROBE, "\n".join([REVISED, RISK]))
    assert _submit(store, plan).ok is True
    assert cli.cmd_approve(ns(session="cs", by="user"), store=store).ok is True

    rows = [r for r in _rows(log) if r.get("fix") == "D2"]
    firings = [r for r in rows if r["row"] == "firing"]
    operating = [r for r in rows if r["row"] == "operating"]
    assert len(firings) == 1, rows
    assert firings[0]["schema"] == "fix-firing/v1"
    assert firings[0]["stage"] == 2
    assert firings[0]["observables"]["was"] == "the base branch is trunk"
    assert firings[0]["observables"]["observed"] == "5d52233a"
    assert len(operating) == 1, rows
    assert operating[0]["observables"]["securing_refusals"] == 1
    assert operating[0]["observables"]["probe_revised_new"] == 1


def test_config_ships_securing_on():
    assert parse_config_md().get("conditions-securing") == "on"


def test_module_is_present():
    assert importlib.util.find_spec("agentctl.conditions_securing") is not None
