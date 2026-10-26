import contextlib
import io
import json

import pytest

from agentctl import cli, inline_class
from agentctl.classify import Signals, classify
from agentctl.config import Thresholds
from agentctl.state import Route, WeightClass

THR = Thresholds({"small-change-max-lines": "20", "substantive-wall-clock-min": "30"})


def test_chat_is_direct():
    c = classify(Signals(is_chat=True), THR)
    assert c.weight_class == WeightClass.CHAT.value
    assert c.route == Route.DIRECT.value


def test_small_change_cohesive_under_threshold():
    c = classify(Signals(changed_lines=10), THR)
    assert c.weight_class == WeightClass.SMALL_CHANGE.value
    assert c.route == Route.IN_THREAD.value


def test_changed_lines_at_the_bound_is_a_small_change():
    c = classify(Signals(changed_lines=20), THR)
    assert c.weight_class == WeightClass.SMALL_CHANGE.value


def test_over_line_threshold_is_substantive():
    c = classify(Signals(changed_lines=21), THR)
    assert c.weight_class == WeightClass.SUBSTANTIVE.value
    assert c.route == Route.SPAWN.value


def test_a_non_cohesive_change_is_substantive():
    c = classify(Signals(changed_lines=3, cohesive=False), THR)
    assert c.weight_class == WeightClass.SUBSTANTIVE.value
    assert any("cohesive" in r for r in c.reasons)


def test_the_small_change_reason_carries_the_definition_and_the_bound():
    c = classify(Signals(changed_lines=1), THR)
    (reason,) = c.reasons
    assert inline_class.missing_criteria(reason) == ()
    assert "small-change-max-lines=20" in reason


def test_architectural_flag_is_substantive():
    c = classify(Signals(changed_lines=1, architectural=True), THR)
    assert c.weight_class == WeightClass.SUBSTANTIVE.value


def test_wall_clock_threshold_is_substantive():
    c = classify(Signals(changed_lines=1, wall_clock_min=30), THR)
    assert c.weight_class == WeightClass.SUBSTANTIVE.value


def test_tracker_key_forces_substantive_even_when_tiny():
    c = classify(Signals(changed_lines=1, tracker_key="ABC-123"), THR)
    assert c.weight_class == WeightClass.SUBSTANTIVE.value
    assert any("tracker" in r for r in c.reasons)


def test_non_tracker_string_does_not_force():
    c = classify(Signals(changed_lines=1, tracker_key="not-a-key"), THR)
    assert c.weight_class == WeightClass.SMALL_CHANGE.value


def _main(root, *argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = cli.main(["--state-root", str(root), *argv])
        except SystemExit as exit_:
            return exit_.code, {}
    return rc, json.loads(out.getvalue())


def _started(tmp_path, sid):
    _main(tmp_path, "start", "--session", sid, "--task", "t", "--goal", "g", "--done-criterion", "d")


def test_the_cli_reads_not_cohesive_as_a_substantive_signal(tmp_path):
    _started(tmp_path, "nc")
    rc, result = _main(tmp_path, "classify", "--session", "nc", "--not-cohesive", "--changed-lines", "1")
    assert rc == 0, result
    assert "substantive" in json.dumps(result)


def test_the_cli_classifies_a_cohesive_small_change_inline(tmp_path):
    _started(tmp_path, "ok")
    rc, result = _main(tmp_path, "classify", "--session", "ok", "--changed-lines", "1")
    assert rc == 0, result
    assert "small" in json.dumps(result)


def test_the_files_flag_is_gone(tmp_path):
    _started(tmp_path, "gone")
    rc, _ = _main(tmp_path, "classify", "--session", "gone", "--files", "2")
    assert rc == 2
