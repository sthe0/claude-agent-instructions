"""Stage 10 of smd-act-defects-8: a derived, guarded, parametrized enumerator over
`submission.py`'s two per-object substantive-field tables —
`_SUBSTANTIVE_SUBMISSION_FIELDS` (per-stage) and `_SUBSTANTIVE_META_FIELDS`
(per-plan).

Several of these fields already have a hand-written refusal test, added one at a
time as each field was introduced by an earlier stage (`test_preconditions.py`,
`test_renormalization.py`'s `procedure`/`method` cases, `test_knowledge_place.py`
for the two ref lists). None of those individual tests is redundant with this
file — this file adds the piece none of them provides on its own: a GUARD tying
the case list to the code's own tables, so a field added to either table without
a matching omission case fails loudly here rather than shipping unexercised.

`_ORDER_PARTS`/`state.Order` is deliberately OUT OF SCOPE. `test_meta_order.py`
already builds exactly this pattern — derived field set, declared exemptions,
a totality guard, one parametrized refusal case per part — for that third table,
716 lines deep. Re-parametrizing `_ORDER_PARTS` here would exercise the same
code from two files that could silently drift apart; the one authority for that
domain stays `test_meta_order.py`.
"""
from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli
from agentctl.dispatch import RunResult
from agentctl.submission import _SUBSTANTIVE_META_FIELDS, _SUBSTANTIVE_SUBMISSION_FIELDS

from conftest import SUBSTANTIVE_FINAL_CHECK, SUBSTANTIVE_ORDER

_STAGE_LABELS = tuple(label for _attr, label, _supply in _SUBSTANTIVE_SUBMISSION_FIELDS)
_META_LABELS = tuple(label for _attr, label in _SUBSTANTIVE_META_FIELDS)

_PLAN = """
[meta]
task_id = "rs"
{goal}{done_criterion}criterion_type = "measurable"
{weight_class}external_research = "none applies"
""" + SUBSTANTIVE_ORDER + """
[[stage]]
index = 1
title = "the stage that goes first"
executor = "in_thread"
expected_result_image = "The parser reads the new key."
criterion_type = "measurable"
done_criterion = "d1"
verify_command = "pytest -q"
material = "m1"
means = "bash"
method = "run"
procedure = "1. read the fixture. 2. apply the edit. 3. re-check the seam"
conditions = "The tree is clean."
preconditions = "The branch is checked out."
invariants = "n1"
capability_required = "cap"
material_refs = ["scripts/agentctl/plan.py"]
knowledge_refs = ["scripts/agentctl/state.py"]
knowledge = "how the loader stays lenient"
[stage.principle]
statement = "s"
source = "src"
derivation = "der"
confidence = "high"
refutation = "r"

[[stage]]
index = 2
title = "the stage under test"
executor = "in_thread"
expected_result_image = "The seam refuses a plan that omits the place."
criterion_type = "measurable"
done_criterion = "d2"
verify_command = "pytest -q"
material = "m2"
means = "bash"
method = {method}
{procedure}conditions = "The advisor is reachable."
{preconditions}invariants = "n2"
capability_required = "cap"
{material_refs}{knowledge_refs}{knowledge}[[stage.supplies]]
on = 1
element = "result"
[stage.principle]
statement = "s"
source = "src"
derivation = "der"
confidence = "high"
refutation = "r"
""" + "{final_check}"

_DEFAULTS = {
    "goal": 'goal = "g"\n',
    "done_criterion": 'done_criterion = "dc"\n',
    "weight_class": 'weight_class = "substantive"\n',
    "method": "run",
    "procedure": 'procedure = "1. read the fixture. 2. apply the edit. 3. re-check the seam"\n',
    "preconditions": 'preconditions = "Stage 1\'s field exists."\n',
    "material_refs": 'material_refs = ["scripts/agentctl/submission.py"]\n',
    "knowledge_refs": 'knowledge_refs = ["scripts/agentctl/procedure.py"]\n',
    "knowledge": 'knowledge = "where a submission requirement may bind"\n',
    "final_check": SUBSTANTIVE_FINAL_CHECK,
}


def ns(**kw):
    return Namespace(**kw)


def _write_plan(path: Path, **over) -> str:
    """Render the compliant two-stage template, blanking one field's line by
    keyword (empty string omits the key entirely); `method` alone is an inline
    value rather than a whole line, so it is JSON-quoted separately."""
    fields = dict(_DEFAULTS)
    fields.update(over)
    fields["method"] = json.dumps(fields["method"])
    path.write_text(_PLAN.format(**fields), encoding="utf-8")
    return str(path)


def _omit(label: str, path: Path) -> str:
    """A compliant plan with exactly ONE submission-table field blanked.

    `method` is the one case that also needs `weight_class` undeclared: it is
    already a LOADER requirement once a stage declares itself substantive
    (`plan._SUBSTANTIVE_STAGE_FIELDS`), so testing its SUBMISSION-seam row
    needs the one plan shape where the loader stays lenient and the seam is
    the only thing left to refuse it — the same shape
    `test_renormalization.py`'s own `method` case uses."""
    if label == "method":
        return _write_plan(path, weight_class="", method="")
    if label in _STAGE_LABELS:
        return _write_plan(path, **{label: ""})
    if label in _META_LABELS:
        return _write_plan(path, **{label: ""})
    raise AssertionError(f"no omission recipe for {label!r}")


def _judge(verdict: str):
    def run(argv, timeout=None):
        return RunResult(0, verdict + "\n", "")
    return run


def _judge_unavailable(argv, timeout=None):
    """The judge cannot be reached — the shape a missing/failing `claude -p` takes."""
    return RunResult(1, "", "no such model")


@pytest.fixture(autouse=True)
def _advisor_on(monkeypatch):
    """The seam resolves its judge through advisor.resolve_enabled, whose documented
    force-on knob is this env var. Set explicitly so these tests do not depend on the
    machine's config.md advisor-mode — and every command below is handed an explicit
    stub runner, so no test here can reach a live `claude -p`."""
    monkeypatch.setenv("AGENTCTL_ADVISOR", "1")


def _submit(store, plan_path, runner, session="rs"):
    cli.cmd_start(ns(session=session, task="rs", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=session, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=session), store=store)
    return cli.cmd_submit_plan(ns(session=session, plan=plan_path), store=store,
                               runner=runner)


def _problems(d) -> list[str]:
    return list(d.data.get("problems", []))


# --- the guard: the case list is the code's own two tables, nothing more, --
# --- nothing less, and non-empty -------------------------------------------


def test_omission_cases_are_exactly_the_two_submission_tables_field_set(tmp_path):
    assert _STAGE_LABELS and _META_LABELS
    assert not (set(_STAGE_LABELS) & set(_META_LABELS)), (
        "a label in both tables would be ambiguous below — _omit dispatches on "
        "table membership"
    )
    for label in _STAGE_LABELS + _META_LABELS:
        # Every label must have an omission recipe that produces a loadable plan —
        # proof the recipe exists and does not itself raise, without a full
        # submission round trip.
        _omit(label, tmp_path / f"{label}.toml")


# --- per-stage table: _SUBSTANTIVE_SUBMISSION_FIELDS ------------------------


@pytest.mark.parametrize("label", _STAGE_LABELS)
def test_stage_field_omission_is_refused_at_submission(label, store, tmp_path):
    plan = _omit(label, tmp_path / "p.toml")

    d = _submit(store, plan, _judge("DISTINCT"))

    assert d.ok is False
    assert d.action == "fix_plan"
    assert any(f"missing {label!r}" in p and "stage 2" in p for p in _problems(d)), (
        label, _problems(d)
    )


# --- per-plan table: _SUBSTANTIVE_META_FIELDS -------------------------------


@pytest.mark.parametrize("label", _META_LABELS)
def test_meta_field_omission_is_refused_at_submission(label, store, tmp_path):
    plan = _omit(label, tmp_path / "p.toml")

    d = _submit(store, plan, _judge("DISTINCT"))

    assert d.ok is False
    assert d.action == "fix_plan"
    assert any(f"missing {label!r}" in p and p.startswith("[meta]") for p in _problems(d)), (
        label, _problems(d)
    )


# --- knowledge's supply-element alternative ---------------------------------


def test_knowledge_is_not_refused_when_supplied_via_a_result_edge(store, tmp_path):
    """`knowledge`'s row in `_SUBSTANTIVE_SUBMISSION_FIELDS` names `"knowledge"` as
    a supply-element alternative — a stage may satisfy the requirement either by
    filling the field or by carrying a supply edge whose `element` is
    `"knowledge"`. Omitting the field while adding such an edge must NOT be
    refused; without this case the alternative path is asserted only by
    submission.py's own code, never exercised."""
    fields = dict(_DEFAULTS)
    fields.update(knowledge="")
    fields["method"] = json.dumps(fields["method"])
    text = _PLAN.format(**fields).replace(
        '[[stage.supplies]]\non = 1\nelement = "result"',
        '[[stage.supplies]]\non = 1\nelement = "result"\n'
        '[[stage.supplies]]\non = 1\nelement = "knowledge"',
    )
    plan_path = tmp_path / "p.toml"
    plan_path.write_text(text, encoding="utf-8")

    d = _submit(store, str(plan_path), _judge("DISTINCT"))

    assert not any("missing 'knowledge'" in p for p in _problems(d)), _problems(d)
