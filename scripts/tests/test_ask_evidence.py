import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import ask_evidence

MARKER = "[test-ask]"


def _entry(ts, blocks, tool_result=None):
    entry = {"timestamp": ts, "message": {"content": blocks}}
    if tool_result is not None:
        entry["toolUseResult"] = tool_result
    return json.dumps(entry)


def _ask(call_id, marked=True):
    label = f"yes {MARKER}" if marked else "yes"
    return {"type": "tool_use", "id": call_id, "name": "AskUserQuestion",
            "input": {"questions": [{"question": "q", "options": [{"label": label}]}]}}


def _result(call_id):
    return {"type": "tool_result", "tool_use_id": call_id, "content": "ok"}


def _write(tmp_path, lines):
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_unreadable_transcript_is_none(tmp_path):
    assert ask_evidence.ask_after_event(None, MARKER, 0.0) is None
    assert ask_evidence.ask_after_event(tmp_path / "missing.jsonl", MARKER, 0.0) is None


def test_scan_splits_marked_before_and_after_anchor(tmp_path):
    path = _write(tmp_path, [
        _entry("2026-01-01T00:00:00Z", [_ask("a")]),
        _entry("2026-01-01T00:00:01Z", [_result("a")], {"answers": {"q": "yes"}}),
        _entry("2026-01-01T02:00:00Z", [_ask("b")]),
        _entry("2026-01-01T02:00:01Z", [_result("b")], {"answers": {"q": "yes"}}),
        _entry("2026-01-01T03:00:00Z", [_ask("c", marked=False)]),
        _entry("2026-01-01T04:00:00Z", [_ask("d")]),
    ])
    anchor = ask_evidence.transcript_turns.iso_to_epoch("2026-01-01T01:00:00Z")
    scan = ask_evidence.ask_after_event(path, MARKER, anchor)
    assert [a.answered for a in scan.marked] == [True, True, False]
    assert [a.answered for a in scan.after] == [True, False]
    assert scan.after[0].sole_answers == ({"q": "yes"},)


def test_entry_with_several_results_contributes_no_answers(tmp_path):
    path = _write(tmp_path, [
        _entry("2026-01-01T00:00:00Z", [_ask("a")]),
        _entry("2026-01-01T00:00:01Z", [_result("a"), _result("other")], {"answers": {"q": "yes"}}),
    ])
    scan = ask_evidence.ask_after_event(path, MARKER, 0.0)
    assert scan.marked[0].answered and scan.marked[0].sole_answers == ()


def _tool_input(*labels, question="q"):
    return {"questions": [{"question": question, "options": [{"label": lab} for lab in labels]}]}


def test_chose_marked_option_accepts_the_marked_label():
    tool_input = _tool_input(f"Yes {MARKER}", "No")
    assert ask_evidence.chose_marked_option(tool_input, {"q": f"Yes {MARKER}"}, MARKER)


def test_chose_marked_option_refuses_the_unmarked_label():
    tool_input = _tool_input(f"Yes {MARKER}", "No")
    assert not ask_evidence.chose_marked_option(tool_input, {"q": "No"}, MARKER)


def test_chose_marked_option_refuses_when_every_option_is_marked():
    tool_input = _tool_input(f"Yes {MARKER}", f"Also yes {MARKER}")
    assert not ask_evidence.chose_marked_option(tool_input, {"q": f"Yes {MARKER}"}, MARKER)


def test_chose_marked_option_refuses_another_marker_and_malformed_input():
    tool_input = _tool_input("Yes [other]", "No")
    assert not ask_evidence.chose_marked_option(tool_input, {"q": "Yes [other]"}, MARKER)
    assert not ask_evidence.chose_marked_option(None, {"q": "x"}, MARKER)
    assert not ask_evidence.chose_marked_option(tool_input, ["q"], MARKER)


def test_parse_ask_answers_reads_the_dict_forms():
    assert ask_evidence.parse_ask_answers({"answers": {"q": "a"}}) == {"q": "a"}
    assert ask_evidence.parse_ask_answers({"q": "a"}) == {"q": "a"}


def test_parse_ask_answers_reads_the_harness_string_form():
    text = 'User has answered your questions: "Which?"="Yes, agreed", "Again?"="No".'
    assert ask_evidence.parse_ask_answers(text) == {"Which?": "Yes, agreed", "Again?": "No"}


def test_parse_ask_answers_of_nothing_is_empty():
    assert ask_evidence.parse_ask_answers(None) == {}
    assert ask_evidence.parse_ask_answers({"answers": []}) == {}
