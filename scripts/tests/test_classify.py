from agentctl.classify import Signals, classify
from agentctl.config import Thresholds
from agentctl.state import Route, WeightClass

THR = Thresholds({"small-change-max-lines": "20", "substantive-wall-clock-min": "30"})


def test_chat_is_direct():
    c = classify(Signals(is_chat=True), THR)
    assert c.weight_class == WeightClass.CHAT.value
    assert c.route == Route.DIRECT.value


def test_small_change_single_file_under_threshold():
    c = classify(Signals(changed_lines=10, files=1), THR)
    assert c.weight_class == WeightClass.SMALL_CHANGE.value
    assert c.route == Route.IN_THREAD.value


def test_over_line_threshold_is_substantive():
    c = classify(Signals(changed_lines=21, files=1), THR)
    assert c.weight_class == WeightClass.SUBSTANTIVE.value
    assert c.route == Route.SPAWN.value


def test_multi_file_is_substantive():
    c = classify(Signals(changed_lines=3, files=2), THR)
    assert c.weight_class == WeightClass.SUBSTANTIVE.value


def test_architectural_flag_is_substantive():
    c = classify(Signals(changed_lines=1, files=1, architectural=True), THR)
    assert c.weight_class == WeightClass.SUBSTANTIVE.value


def test_wall_clock_threshold_is_substantive():
    c = classify(Signals(changed_lines=1, files=1, wall_clock_min=30), THR)
    assert c.weight_class == WeightClass.SUBSTANTIVE.value


def test_tracker_key_forces_substantive_even_when_tiny():
    c = classify(Signals(changed_lines=1, files=1, tracker_key="ABC-123"), THR)
    assert c.weight_class == WeightClass.SUBSTANTIVE.value
    assert any("tracker" in r for r in c.reasons)


def test_non_tracker_string_does_not_force():
    c = classify(Signals(changed_lines=1, files=1, tracker_key="not-a-key"), THR)
    assert c.weight_class == WeightClass.SMALL_CHANGE.value
