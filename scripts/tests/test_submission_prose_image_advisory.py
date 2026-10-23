"""Advisory for a stage whose expected_result_image is a long, unenumerated prose block.

The advisory fires purely from the plan's bytes, with no model call:

  * It is WARN-only: the plan is submitted and the session reaches PLAN-READY.
  * It is STRUCTURAL: unlike the echo check, it fires with judge_runner=None and
    spawns no subprocess.
  * It is GATED on the plan's own weight_class: a non-substantive plan's prose
    image is not advised, since the acceptance-round convergence problem does not
    arise outside a substantive plan's review cycle.

The two tunables — _PROSE_FLOOR and _ENUMERATION_PRESENT — are tested here: a short
image stays silent regardless of the absence of enumeration markers, and any of the
accepted enumeration shapes silences the advisory regardless of image length.
"""
from __future__ import annotations

import json
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, plan as plan_mod
from agentctl.submission import submission_advice

from conftest import SUBSTANTIVE_FINAL_CHECK, SUBSTANTIVE_ORDER, declare_venue


def ns(**kw):
    return Namespace(**kw)


# A long prose image that exceeds _PROSE_FLOOR and carries no enumeration marker.
_PROSE_IMAGE = (
    "The migration completes without error and the database reflects the new schema. "
    "Every previously-passing query returns the expected result set. The rollback path "
    "was not triggered and the audit log carries a single completed-migration entry."
)

# An image short enough to stay silent regardless of the absence of markers.
_SHORT_IMAGE = "The package imports and `status` runs on empty state."

assert len(_PROSE_IMAGE) >= 150, "fixture drift: prose image is below _PROSE_FLOOR"
assert len(_SHORT_IMAGE) < 150, "fixture drift: short image is at or above _PROSE_FLOOR"


_PLAN = """
[meta]
task_id = "pa"
goal = "g"
done_criterion = "dc"
criterion_type = "measurable"
weight_class = "substantive"
external_research = "none applies"
""" + SUBSTANTIVE_ORDER + """
[[stage]]
index = 1
title = "the stage under test"
executor = "in_thread"
expected_result_image = {image}
criterion_type = "measurable"
done_criterion = "d"
verify_command = "true"
negative_control = "false"
material = "m (traces to R1)"
means = "bash"
method = "run"
procedure = "1. read the fixture. 2. apply the edit. 3. re-check the seam"
conditions = "c"
preconditions = "p"
invariants = "n"
capability_required = "cap"
material_refs = ["scripts/agentctl/submission.py"]
knowledge_refs = ["scripts/agentctl/result_image.py"]
knowledge = "how the submission seam differs from the loader"
[stage.principle]
statement = "s"
source = "src"
derivation = "der"
confidence = "high"
refutation = "r"
""" + SUBSTANTIVE_FINAL_CHECK


def _write_plan(path: Path, image: str, *, weight_class: str = "substantive") -> str:
    text = _PLAN.format(image=json.dumps(image))
    if weight_class != "substantive":
        text = text.replace('weight_class = "substantive"', f'weight_class = "{weight_class}"')
    path.write_text(declare_venue(text, path.parent), encoding="utf-8")
    return str(path)


def _prose_advisories(d) -> list[str]:
    return [a for a in d.data.get("advisories", []) if "prose block" in a]


def _submit(store, plan_path):
    cli.cmd_start(ns(session="pa", task="pa", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session="pa", chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session="pa"), store=store)
    return cli.cmd_submit_plan(ns(session="pa", plan=plan_path), store=store, runner=None)


# --- advisory fires on a substantive plan's long prose image ------------------


def test_prose_image_advisory_fires_on_substantive_plan(store, tmp_path):
    """A long prose image on a substantive plan yields the advisory; the submission
    succeeds (never refuses). POSITIVE direction: both halves are the assertion."""
    d = _submit(store, _write_plan(tmp_path / "p.toml", _PROSE_IMAGE))

    assert d.ok is True, "prose-image advisory must never refuse a plan"
    assert not d.data.get("problems"), "advisory must not appear as a submission problem"
    warnings = _prose_advisories(d)
    assert len(warnings) == 1
    assert "stage 1" in warnings[0]
    assert "'the stage under test'" in warnings[0]


def test_prose_image_advisory_silent_on_non_substantive_plan(tmp_path):
    """The advisory is gated on the plan declaring itself substantive; a non-substantive
    plan with the same long prose image produces nothing."""
    doc = plan_mod.load_plan(_write_plan(tmp_path / "p.toml", _PROSE_IMAGE, weight_class="small"))
    assert not any("prose block" in a for a in submission_advice(doc))


def test_prose_image_advisory_silent_below_floor(tmp_path):
    """A short image stays silent even when it carries no enumeration markers."""
    doc = plan_mod.load_plan(_write_plan(tmp_path / "p.toml", _SHORT_IMAGE))
    assert not any("prose block" in a for a in submission_advice(doc))


# --- enumeration shapes suppress the advisory ---------------------------------


@pytest.mark.parametrize("marker,label", [
    ("(1) the first item.\n(2) the second item.\n(3) the third item.", "parenthesized"),
    ("1. the first item.\n2. the second item.\n3. the third item.", "numbered"),
    ("- the first item.\n- the second item.\n- the third item.", "dash-bullet"),
    ("* the first item.\n* the second item.\n* the third item.", "asterisk-bullet"),
    ("• the first item.\n• the second item.\n• the third item.", "unicode-bullet"),
])
def test_prose_image_advisory_silent_on_enumerated_image(tmp_path, marker, label):
    """An image carrying any accepted enumeration shape yields no advisory.

    Each image is padded to well above _PROSE_FLOOR so only the marker presence
    decides, not the length."""
    image = marker + "  " + "x" * 200
    doc = plan_mod.load_plan(_write_plan(tmp_path / "p.toml", image))
    assert not any("prose block" in a for a in submission_advice(doc)), (
        f"advisory fired for {label!r} enumeration shape"
    )


# --- structural: no judge_runner needed, no subprocess spawned ----------------


def test_prose_image_advisory_fires_without_judge_runner(tmp_path):
    """The prose-image advisory is purely structural — it fires with judge_runner=None,
    unlike the echo check, which requires a runner to say anything."""
    doc = plan_mod.load_plan(_write_plan(tmp_path / "p.toml", _PROSE_IMAGE))
    advisories = submission_advice(doc, judge_runner=None)
    assert any("prose block" in a for a in advisories)


def test_prose_image_advisory_does_not_spawn_subprocess(monkeypatch, tmp_path):
    """No `claude -p` call — the advisory is structural and must never spawn a process.

    Guards the whole subprocess surface (Popen is what run() and check_output() are
    built on) so a future accidental subprocess call fails loudly here."""
    def forbidden(*args, **kwargs):
        raise AssertionError(f"unexpected subprocess call: {args!r}")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)

    doc = plan_mod.load_plan(_write_plan(tmp_path / "p.toml", _PROSE_IMAGE))
    advisories = submission_advice(doc, judge_runner=None)
    assert any("prose block" in a for a in advisories)
