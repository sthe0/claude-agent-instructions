"""hook-ask-answer-record.py: every answered AskUserQuestion becomes one row in the answer ledger
beside the engine session's state file, and nothing is written for a session with no state file.

The ledger is what `order-agree` reads in place of a transcript, so the hook must keep the
offered option labels (the consent rule judges a choice against them), never raise into the
tool call, and leave rows only ever appended.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentctl import answer_ledger

HOOK = Path(__file__).resolve().parent.parent / "hook-ask-answer-record.py"
QUESTION = "Is this the order you gave?"
YES = "Yes, agreed [order-agree:abcdef012345]"
NO = "No"


@pytest.fixture
def home(tmp_path):
    state_dir = tmp_path / "agentctl" / "state"
    state_dir.mkdir(parents=True)
    return tmp_path


def _state_file(home: Path, session: str = "s1", *, create: bool = True) -> Path:
    path = home / "agentctl" / "state" / f"{session}.json"
    if create:
        path.write_text("{}", encoding="utf-8")
    return path


def _payload(session: str = "s1", answer: str = YES) -> dict:
    return {
        "tool_name": "AskUserQuestion", "session_id": session,
        "tool_input": {"questions": [{"question": QUESTION, "options": [
            {"label": YES, "description": "agree"}, {"label": NO}]}]},
        "tool_response": {"answers": {QUESTION: answer}},
    }


def _run(payload, home: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(home)}
    env.pop("CLAUDE_AGENT_HOME", None)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        capture_output=True, text=True, timeout=30, env=env,
    )


def test_an_answered_ask_is_recorded_with_its_offered_options(home):
    state_file = _state_file(home)
    before = time.time()

    result = _run(_payload(), home)

    assert result.returncode == 0 and result.stderr == ""
    [record] = answer_ledger.answers(state_file)
    assert record.at >= before
    assert record.answers == {QUESTION: YES}
    [question] = record.questions
    assert question["question"] == QUESTION
    assert [o["label"] for o in question["options"]] == [YES, NO]


def test_the_recorded_row_is_judged_by_the_consent_rule(home):
    from lib import ask_evidence
    state_file = _state_file(home)
    _run(_payload(answer=YES), home)
    _run(_payload(answer=NO), home)

    chose_yes, chose_no = answer_ledger.answers(state_file)

    marker = "[order-agree:abcdef012345]"
    assert ask_evidence.chose_marked_option(chose_yes.tool_input(), chose_yes.answers, marker)
    assert not ask_evidence.chose_marked_option(chose_no.tool_input(), chose_no.answers, marker)


def test_rows_are_appended_never_rewritten(home):
    state_file = _state_file(home)

    for _ in range(3):
        _run(_payload(), home)

    assert len(answer_ledger.answers(state_file)) == 3
    lines = answer_ledger.ledger_path_for(state_file).read_text(encoding="utf-8").splitlines()
    assert all(json.loads(line)["answers"] == {QUESTION: YES} for line in lines)


def test_the_harness_string_form_of_the_response_is_recorded(home):
    state_file = _state_file(home)
    payload = _payload()
    payload["tool_response"] = f'Your questions have been answered: "{QUESTION}"="{YES}".'

    _run(payload, home)

    [record] = answer_ledger.answers(state_file)
    assert record.answers == {QUESTION: YES}


def test_a_session_without_a_state_file_leaves_no_trace(home):
    _state_file(home, "other")

    result = _run(_payload(session="s1"), home)

    assert result.returncode == 0
    assert not list((home / "agentctl" / "state").glob("*.answers.jsonl"))


def test_another_tool_is_ignored(home):
    state_file = _state_file(home)
    payload = _payload()
    payload["tool_name"] = "Bash"

    _run(payload, home)

    assert not answer_ledger.ledger_path_for(state_file).exists()


def test_an_ask_with_no_answers_is_ignored(home):
    state_file = _state_file(home)
    payload = _payload()
    payload["tool_response"] = {"answers": {}}

    _run(payload, home)

    assert not answer_ledger.ledger_path_for(state_file).exists()


@pytest.mark.parametrize("raw", ["not json", "[]", "{}", '{"tool_name": "AskUserQuestion"}', ""])
def test_malformed_input_exits_zero_quietly(home, raw):
    result = _run(raw, home)

    assert result.returncode == 0 and result.stderr == ""


def test_the_ledger_reader_skips_corrupt_lines_and_counts_them(home):
    state_file = _state_file(home)
    _run(_payload(), home)
    with open(answer_ledger.ledger_path_for(state_file), "a", encoding="utf-8") as fh:
        fh.write("garbage\n" + json.dumps({"at": "x", "questions": [], "answers": {}}) + "\n")

    read = answer_ledger.read(state_file)

    assert read.exists and len(read.records) == 1 and read.skipped == 2


def test_a_missing_ledger_reads_as_absent(home):
    read = answer_ledger.read(_state_file(home))

    assert read.exists is False and read.records == []
