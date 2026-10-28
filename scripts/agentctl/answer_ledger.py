"""Append-only ledger of the user's AskUserQuestion answers, kept beside a session's state file.

Difficulty removed: an act that needs the user's consent (agreeing the order's understanding,
dismissing a challenge) must rest on evidence the engine can read without parsing a transcript
and the acting agent cannot write through a CLI verb. The PostToolUse hook
`hook-ask-answer-record.py` appends one row per answered ask; `order-agree` and
`order-challenge-dismiss` read the rows back and judge them with
`lib.ask_evidence.chose_marked_option`.

The ledger is a sidecar for the same reason the delivery stamp is (see `delivery.py`): the hook
is an out-of-process writer and `state.json` has a single unlocked writer, the engine. Rows are
only ever appended, one JSON line under an exclusive `flock`, never rewritten. A reader skips
lines it cannot parse and reports how many it skipped.
"""
from __future__ import annotations

import fcntl
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AnswerRecord:
    at: float
    # One entry per asked question: {"question": str, "options": [offered labels]}. The offered
    # option dicts keep `label` and `description` so a marker in either is judged as the
    # content-waiver gate judges it.
    questions: list
    answers: dict = field(default_factory=dict)

    def tool_input(self) -> dict:
        return {"questions": [
            {"question": q.get("question"), "options": q.get("options") or []}
            for q in self.questions if isinstance(q, dict)
        ]}


@dataclass
class LedgerRead:
    records: list
    skipped: int = 0
    exists: bool = False


def ledger_path_for(state_file: Path) -> Path:
    return state_file.parent / (state_file.name + ".answers.jsonl")


def append(state_file: Path, record: dict) -> None:
    path = ledger_path_for(state_file)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.write(line)
            fh.flush()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def read(state_file: Path) -> LedgerRead:
    path = ledger_path_for(state_file)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return LedgerRead([], 0, False)
    records: list[AnswerRecord] = []
    skipped = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            at = raw["at"]
            questions = raw["questions"]
            answers = raw["answers"]
            if (isinstance(at, bool) or not isinstance(at, (int, float))
                    or not isinstance(questions, list) or not isinstance(answers, dict)):
                raise ValueError("shape")
        except Exception:
            skipped += 1
            continue
        records.append(AnswerRecord(float(at), questions, answers))
    return LedgerRead(records, skipped, True)


def answers(state_file: Path) -> list[AnswerRecord]:
    return read(state_file).records
