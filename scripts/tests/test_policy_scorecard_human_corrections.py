"""policy-scorecard.py attention counters: who spoke is structure, what it meant is a model judge.

A user entry is a *prompt* only when the transcript stamps it `origin.kind == "human"`;
machine-authored entries (meta, compaction summaries, task notifications, spawn briefs)
are not. A human prompt the regex prefilter nominates is a *correction* only when the
model judge says YES; a nomination the judge did not answer is counted separately as
`corrections_unjudged`, never as a correction.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from agentctl.dispatch import RunResult

SCRIPT = Path(__file__).resolve().parent.parent / "policy-scorecard.py"

CORRECTION_TEXT = "Actually, that's wrong, you need to redo it."


@pytest.fixture
def ps(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("policy_scorecard_human_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("POLICY_CORRECTION_VERDICTS", str(tmp_path / "verdicts.json"))
    monkeypatch.setattr(mod, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path / "no-instrepo")
    monkeypatch.setattr(mod, "_CORRECTION_JUDGE_RUNNER", None, raising=False)
    return mod


class FakeJudge:
    def __init__(self, answer="YES", returncode=0):
        self.answer = answer
        self.returncode = returncode
        self.prompts: list[str] = []

    def __call__(self, argv, **kwargs):
        self.prompts.append(kwargs.get("stdin", ""))
        return RunResult(self.returncode, stdout=self.answer, stderr="")


def _write(path: Path, entries: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


def _human(text: str, ts: str = "2026-06-05T10:00:00Z") -> dict:
    return {"type": "user", "timestamp": ts, "origin": {"kind": "human"},
            "message": {"content": text}}


def _session(tmp_path: Path, entries: list[dict], name: str = "s1") -> Path:
    return _write(tmp_path / "projects" / "proj" / f"{name}.jsonl", entries)


def test_machine_entries_are_not_prompts(ps, tmp_path):
    words = "that's wrong, you should have asked first, self-improvement"
    f = _session(tmp_path, [
        {"type": "user", "timestamp": "2026-06-05T10:00:00Z", "isMeta": True,
         "message": {"content": words}},
        {"type": "user", "timestamp": "2026-06-05T10:01:00Z", "isCompactSummary": True,
         "message": {"content": words}},
        {"type": "user", "timestamp": "2026-06-05T10:02:00Z",
         "origin": {"kind": "task-notification"}, "message": {"content": words}},
        {"type": "user", "timestamp": "2026-06-05T10:03:00Z",
         "message": {"content": "AGENT_RECURSION_DEPTH=1 " + words}},
    ])

    attention = ps._scan_session(f)["attention"]

    assert attention["prompts"] == 0
    assert attention["corrections"] == 0


def test_interrupt_sentinel_is_counted_without_origin(ps, tmp_path):
    f = _session(tmp_path, [
        {"type": "user", "timestamp": "2026-06-05T10:00:00Z",
         "message": {"content": "[Request interrupted by user]"}},
    ])

    attention = ps._scan_session(f)["attention"]

    assert attention["interrupts"] == 1
    assert attention["prompts"] == 0


def test_machine_entries_do_not_count_as_questions_or_resolution(ps, tmp_path):
    f = _session(tmp_path, [
        {"type": "user", "timestamp": "2026-06-05T10:00:00Z", "isMeta": True,
         "message": {"content": "is it resolved? looks good"}},
    ])

    row = ps._scan_session(f)

    assert row["user_signals"]["n_user_questions"] == 0
    assert row["effectiveness"]["resolution_confirmed"] == 0


@pytest.mark.parametrize("answer,corrections", [("YES", 1), ("NO", 0)])
def test_judge_decides_correction(ps, tmp_path, answer, corrections):
    judge = FakeJudge(answer)
    ps._CORRECTION_JUDGE_RUNNER = judge
    f = _session(tmp_path, [_human(CORRECTION_TEXT)])

    row = ps._scan_session(f)

    assert row["attention"]["prompts"] == 1
    assert row["attention"]["corrections"] == corrections
    assert row["attention"]["corrections_unjudged"] == 0
    assert row["user_signals"]["n_user_corrections"] == corrections
    assert len(judge.prompts) == 1


def test_unjudged_is_not_a_correction(ps, tmp_path):
    f = _session(tmp_path, [_human(CORRECTION_TEXT)])

    attention = ps._scan_session(f)["attention"]

    assert attention["corrections"] == 0
    assert attention["corrections_unjudged"] == 1


def test_judge_failure_is_unjudged_and_not_cached(ps, tmp_path):
    f = _session(tmp_path, [_human(CORRECTION_TEXT)])
    ps._CORRECTION_JUDGE_RUNNER = FakeJudge("", returncode=1)
    first = ps._scan_session(f)["attention"]
    ps._CORRECTION_JUDGE_RUNNER = FakeJudge("YES")

    second = ps._scan_session(f)["attention"]

    assert (first["corrections"], first["corrections_unjudged"]) == (0, 1)
    assert (second["corrections"], second["corrections_unjudged"]) == (1, 0)


def test_killswitch_leaves_hits_unjudged(ps, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_SI_FEEDBACK_SEMANTIC", "0")
    judge = FakeJudge("YES")
    ps._CORRECTION_JUDGE_RUNNER = judge
    f = _session(tmp_path, [_human(CORRECTION_TEXT)])

    attention = ps._scan_session(f)["attention"]

    assert (attention["corrections"], attention["corrections_unjudged"]) == (0, 1)
    assert judge.prompts == []


def test_verdict_cache_avoids_second_call(ps, tmp_path):
    judge = FakeJudge("YES")
    ps._CORRECTION_JUDGE_RUNNER = judge
    f = _session(tmp_path, [_human(CORRECTION_TEXT)])

    first = ps._scan_session(f)["attention"]
    second = ps._scan_session(f)["attention"]

    assert first["corrections"] == second["corrections"] == 1
    assert len(judge.prompts) == 1


def test_reminder_only_correction_is_not_judged(ps, tmp_path):
    judge = FakeJudge("YES")
    ps._CORRECTION_JUDGE_RUNNER = judge
    f = _session(tmp_path, [_human(
        "Please continue. <system-reminder>you shouldn't have done that, "
        "that's wrong</system-reminder>")])

    attention = ps._scan_session(f)["attention"]

    assert attention["prompts"] == 1
    assert (attention["corrections"], attention["corrections_unjudged"]) == (0, 0)
    assert judge.prompts == []


def test_judge_sees_stripped_text_and_cache_key_ignores_reminder(ps, tmp_path):
    judge = FakeJudge("YES")
    ps._CORRECTION_JUDGE_RUNNER = judge
    f = _session(tmp_path, [
        _human(CORRECTION_TEXT + " <system-reminder>noise one</system-reminder>"),
        _human(CORRECTION_TEXT + " <system-reminder>noise two</system-reminder>",
               ts="2026-06-05T10:01:00Z"),
    ])

    attention = ps._scan_session(f)["attention"]

    assert attention["corrections"] == 2
    assert len(judge.prompts) == 1
    assert "noise one" not in judge.prompts[0]


# ------------------------------------------------------------------- upsert

def _upsert_with(ps, monkeypatch, f: Path, existing: dict | None):
    monkeypatch.setattr(ps, "in_window_files", lambda days, project: [f])
    if existing is not None:
        ps.write_ledger({f.stem: existing})
    return ps.upsert(7, None)


def _stored_row(ps, f: Path, **overrides) -> dict:
    row = ps._scan_session(f)
    row.update(overrides)
    return row


def test_old_scan_version_is_rescanned(ps, tmp_path, monkeypatch):
    f = _session(tmp_path, [_human("Please add a test.")])
    existing = _stored_row(ps, f)
    del existing["scan_version"]

    _, scanned, skipped = _upsert_with(ps, monkeypatch, f, existing)

    assert (scanned, skipped) == (1, 0)


def test_current_row_is_skipped(ps, tmp_path, monkeypatch):
    f = _session(tmp_path, [_human("Please add a test.")])

    _, scanned, skipped = _upsert_with(ps, monkeypatch, f, _stored_row(ps, f))

    assert (scanned, skipped) == (0, 1)


def test_unjudged_row_is_rescanned_only_when_a_judge_is_active(ps, tmp_path, monkeypatch):
    f = _session(tmp_path, [_human(CORRECTION_TEXT)])
    existing = _stored_row(ps, f)
    assert existing["attention"]["corrections_unjudged"] == 1

    _, scanned, _ = _upsert_with(ps, monkeypatch, f, existing)
    assert scanned == 0

    ps._CORRECTION_JUDGE_RUNNER = FakeJudge("YES")
    rows, scanned, _ = _upsert_with(ps, monkeypatch, f, existing)
    assert scanned == 1
    assert rows[f.stem]["attention"]["corrections"] == 1


def test_judge_budget_exhausted_leaves_hits_unjudged(ps, tmp_path, monkeypatch):
    monkeypatch.setenv("POLICY_CORRECTION_JUDGE_BUDGET_S", "0")
    judge = FakeJudge("YES")
    ps._CORRECTION_JUDGE_RUNNER = judge
    f = _session(tmp_path, [_human(CORRECTION_TEXT)])

    rows, scanned, _ = _upsert_with(ps, monkeypatch, f, None)

    assert scanned == 1
    assert rows[f.stem]["attention"]["corrections_unjudged"] == 1
    assert judge.prompts == []


def test_cli_installs_the_real_runner_unless_no_judge(ps, tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "in_window_files", lambda days, project: [])
    sentinel = object()
    monkeypatch.setattr(ps.advisor, "subprocess_runner", sentinel)
    ledger = str(tmp_path / "l.jsonl")

    ps.main(["--ledger-only", "--no-judge", "--ledger", ledger])
    assert ps._CORRECTION_JUDGE_RUNNER is None

    ps.main(["--ledger-only", "--ledger", ledger])
    assert ps._CORRECTION_JUDGE_RUNNER is sentinel
