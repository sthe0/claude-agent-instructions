"""Tests for lib/judge_ledger.py and the 14-outcome taxonomy it must
distinguish for every judge-calling hook (hook-escalation-diagnosis-gate.py,
hook-deferring-disposition-gate.py, hook-turn-end-gate.py), all funneled
through agentctl.advisor.subprocess_runner.

The 14 outcomes (only 4 and 5 count into the healthy-call denominator; the
rest are the fail-open breakdown):
  1  hook did not enter
  2  entered, filtered by prefilter
  3  budget exhausted before call
  4  judge called and completed
  5  judge called and timed out by its own ceiling
  6  hook killed by harness at registration timeout
  7  judge called and failed fast without judgment (nonzero rc)
  7a   ... (empty/unparseable stdout)
  7b   ... (exception bypassing RunResult)
  7c   ... (judge disabled)
  8  verdict rendered and discarded by post-judge except
  9  verdict rendered but not emitted
  10 verdict rendered but hook killed before emission
  11 hook killed or exited before verdict, outside any judge call

Outcomes 6/10/11 leave no distinguishing ledger line by construction (the
hook is dead before it can write one) — those are exercised at the ledger
API level directly, simulating the exact partial-write signature a kill at
that point would leave behind, per judge_ledger.hook_start's own docstring
admission that a kill and a normal early return are indistinguishable from
the ledger alone.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from lib import judge_ledger  # noqa: E402
from agentctl.dispatch import RunResult  # noqa: E402

ESCALATION_HOOK = SCRIPTS_DIR / "hook-escalation-diagnosis-gate.py"
DEFERRING_HOOK = SCRIPTS_DIR / "hook-deferring-disposition-gate.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_esc = _load(ESCALATION_HOOK, "hook_escalation_diagnosis_gate_for_usage_test")
_defer = _load(DEFERRING_HOOK, "hook_deferring_disposition_gate_for_usage_test")

# Text that fires outage_escalation_detect's prefilter (present-tense external
# failure + user-facing escalation frame) — mirrors the hook's own docstring.
ESCALATION_PAYLOAD = {
    "tool_name": "AskUserQuestion",
    "tool_input": {
        "questions": [{
            "question": "Сервис лежит, 504 на каждый запрос — к кому за доступом?",
            "options": [{"label": "Эскалировать дежурному"}, {"label": "Подождать"}],
        }]
    },
}

# Benign question: no outage vocabulary at all, so the prefilter never fires.
BENIGN_PAYLOAD = {
    "tool_name": "AskUserQuestion",
    "tool_input": {"questions": [{"question": "Какой язык ответов?", "options": [{"label": "Русский"}]}]},
}

DEFECTIVE_PAYLOAD = {
    "tool_name": "AskUserQuestion",
    "tool_input": {
        "questions": [{
            "question": "Что делать с найденным дефектом?",
            "options": [
                {"label": "Завести отдельной задачей (Рекомендую)", "description": "в бэклог"},
                {"label": "Не трогать"},
            ],
        }]
    },
}


def _use_ledger(monkeypatch, tmp_path, name="ledger.jsonl"):
    path = tmp_path / name
    monkeypatch.setenv("AGENTCTL_JUDGE_LEDGER", str(path))
    return path


def _kinds(records):
    return [r.get("kind") for r in records]


def _fake_runner(*, returncode=0, stdout="", stderr="", timed_out=False, raises=None):
    def run(argv, **kwargs):
        if raises is not None:
            raise raises
        return RunResult(returncode, stdout, stderr, timed_out=timed_out)

    return run


# --- ledger mechanics ---------------------------------------------------------

def test_reset_invocation_always_mints_a_distinct_id(monkeypatch, tmp_path):
    _use_ledger(monkeypatch, tmp_path)
    first = judge_ledger.reset_invocation()
    second = judge_ledger.reset_invocation()
    assert first != second


def test_current_invocation_id_autogenerates_when_unset(monkeypatch, tmp_path):
    _use_ledger(monkeypatch, tmp_path)
    judge_ledger._state["invocation_id"] = None
    generated = judge_ledger.current_invocation_id()
    assert generated
    assert judge_ledger.current_invocation_id() == generated  # memoized


def test_hook_start_resets_judge_and_source_and_mints_a_fresh_id(monkeypatch, tmp_path):
    path = _use_ledger(monkeypatch, tmp_path)
    judge_ledger.set_current_judge("stale_judge")
    judge_ledger.set_source("stale_source")
    first_id = judge_ledger.hook_start("escalation_diagnosis")
    assert judge_ledger.current_judge() is None
    assert judge_ledger.current_source() == "unknown"
    records = judge_ledger.read_records(path)
    assert _kinds(records) == ["hook_start"]
    assert records[0]["hook"] == "escalation_diagnosis"
    assert records[0]["invocation_id"] == first_id


def test_set_source_current_source_defaults_to_unknown(monkeypatch, tmp_path):
    _use_ledger(monkeypatch, tmp_path)
    judge_ledger.set_source(None)
    assert judge_ledger.current_source() == "unknown"
    judge_ledger.set_source("sess-42")
    assert judge_ledger.current_source() == "sess-42"


def test_source_from_payload_prefers_session_id_else_manual(monkeypatch, tmp_path):
    _use_ledger(monkeypatch, tmp_path)
    judge_ledger.source_from_payload({"session_id": "sess-7"})
    assert judge_ledger.current_source() == "sess-7"
    judge_ledger.source_from_payload({"session_id": ""})
    assert judge_ledger.current_source() == "manual"
    judge_ledger.source_from_payload({})
    assert judge_ledger.current_source() == "manual"
    judge_ledger.source_from_payload({"session_id": 12345})
    assert judge_ledger.current_source() == "manual"


def test_source_signature_present_on_every_line_and_updates_after_payload(monkeypatch, tmp_path):
    path = _use_ledger(monkeypatch, tmp_path)
    judge_ledger.hook_start("escalation_diagnosis")
    judge_ledger.source_from_payload({"session_id": "sess-99"})
    judge_ledger.entered("outage_escalation", prefilter_fired=False)
    records = judge_ledger.read_records(path)
    assert records[0]["source"] == "unknown"  # written before source_from_payload
    assert records[1]["source"] == "sess-99"
    assert all("invocation_id" in r for r in records)


def test_write_drops_reason_before_hard_truncating_an_oversized_line(monkeypatch, tmp_path):
    path = _use_ledger(monkeypatch, tmp_path)
    judge_ledger.hook_start("escalation_diagnosis")
    huge_reason = "x" * 4000
    judge_ledger.decided(
        "outage_escalation", stage="call", verdict=False, reason=huge_reason,
        timed_out=False, malformed=True, remaining=10.0, threshold=20.0, ceiling=30.0,
        duration=1.5,
    )
    records = judge_ledger.read_records(path)
    decided = [r for r in records if r["kind"] == "decided"][0]
    assert "reason" not in decided or decided["reason"] != huge_reason
    with open(path, "rb") as fh:
        lines = fh.read().splitlines()
    assert all(len(line) <= judge_ledger._MAX_LINE_BYTES for line in lines)


def test_write_is_fail_silent_on_unwritable_path(monkeypatch, tmp_path):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("i am a file, not a directory")
    monkeypatch.setenv("AGENTCTL_JUDGE_LEDGER", str(blocker / "ledger.jsonl"))
    judge_ledger.hook_start("escalation_diagnosis")  # must not raise


def test_read_records_skips_malformed_lines(tmp_path):
    path = tmp_path / "mixed.jsonl"
    path.write_text(
        json.dumps({"kind": "hook_start"}) + "\n"
        + "not json at all\n"
        + json.dumps(["a", "list", "not", "a", "dict"]) + "\n"
        + json.dumps({"kind": "final"}) + "\n"
    )
    records = judge_ledger.read_records(path)
    assert _kinds(records) == ["hook_start", "final"]


# --- outcome 1: hook did not enter -------------------------------------------

def test_outcome_1_hook_did_not_enter(monkeypatch, tmp_path):
    path = _use_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(_esc.advisor, "subprocess_runner", _fake_runner(stdout="YES"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {}})))
    assert _esc.main() == 0
    records = judge_ledger.read_records(path)
    assert _kinds(records) == ["hook_start", "final", "emitted"]
    assert not any(r["kind"] == "entered" for r in records)


# --- outcome 2: entered, filtered by prefilter -------------------------------

def test_outcome_2_entered_filtered_by_prefilter(monkeypatch, tmp_path):
    path = _use_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(_esc.advisor, "subprocess_runner", _fake_runner(stdout="YES"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(BENIGN_PAYLOAD)))
    assert _esc.main() == 0
    records = judge_ledger.read_records(path)
    entered = [r for r in records if r["kind"] == "entered"]
    assert len(entered) == 1
    assert entered[0]["prefilter_fired"] is False
    assert not any(r["kind"] == "decided" for r in records)


# --- outcome 3: budget exhausted before call ---------------------------------

def test_outcome_3_budget_exhausted_before_call(monkeypatch, tmp_path):
    path = _use_ledger(monkeypatch, tmp_path)

    class _ExhaustedBudget:
        def __init__(self, *a, **k):
            pass

        def remaining_and_timeout(self, cap_s, **kwargs):
            return -5.0, None

    monkeypatch.setattr(_esc.judge_budget, "JudgeBudget", _ExhaustedBudget)
    judge_ledger.hook_start("escalation_diagnosis")
    result = _esc.decide(ESCALATION_PAYLOAD, runner=_fake_runner(stdout="YES"))
    assert result is None
    records = judge_ledger.read_records(path)
    decided = [r for r in records if r["kind"] == "decided"][0]
    assert decided["stage"] == "budget"
    assert decided["verdict"] is False
    assert decided["remaining"] == -5.0
    assert decided["ceiling"] == _esc._JUDGE_BUDGET_S
    assert decided["threshold"] is None


# --- outcome 4: judge called and completed -----------------------------------

def test_outcome_4_judge_called_and_completed(monkeypatch, tmp_path):
    path = _use_ledger(monkeypatch, tmp_path)
    judge_ledger.hook_start("escalation_diagnosis")
    result = _esc.decide(ESCALATION_PAYLOAD, runner=_fake_runner(returncode=0, stdout="YES"))
    assert result is not None
    records = judge_ledger.read_records(path)
    decided = [r for r in records if r["kind"] == "decided"][0]
    assert decided["stage"] == "call"
    assert decided["timed_out"] is False
    assert decided["malformed"] is False
    assert decided.get("reason", "") == ""
    assert isinstance(decided["duration"], (int, float))
    assert decided["duration"] >= 0


# --- outcome 5: judge called and timed out -----------------------------------

def test_outcome_5_judge_called_and_timed_out(monkeypatch, tmp_path):
    path = _use_ledger(monkeypatch, tmp_path)
    judge_ledger.hook_start("escalation_diagnosis")
    runner = _fake_runner(returncode=1, stdout="", stderr="advisor timed out after 30s", timed_out=True)
    result = _esc.decide(ESCALATION_PAYLOAD, runner=runner)
    assert result is None  # a timed-out judge fails open, never denies
    records = judge_ledger.read_records(path)
    decided = [r for r in records if r["kind"] == "decided"][0]
    assert decided["stage"] == "call"
    # The structural discriminator is the boolean field alone, never a stderr
    # substring match — this is what distinguishes outcome 5 from outcome 7.
    assert decided["timed_out"] is True


# --- outcome 6: hook killed by harness at registration timeout --------------

def test_outcome_6_registration_timeout_kill_leaves_unpaired_started(monkeypatch, tmp_path):
    path = _use_ledger(monkeypatch, tmp_path)
    judge_ledger.hook_start("escalation_diagnosis")
    judge_ledger.started("outage_escalation")
    # simulates the harness killing the process here: no `call` line ever follows
    records = judge_ledger.read_records(path)
    assert _kinds(records) == ["hook_start", "started"]


# --- outcomes 7 / 7a / 7b / 7c: judge called, failed fast, no judgment ------

def test_outcome_7_nonzero_returncode(monkeypatch, tmp_path):
    path = _use_ledger(monkeypatch, tmp_path)
    judge_ledger.hook_start("escalation_diagnosis")
    result = _esc.decide(ESCALATION_PAYLOAD, runner=_fake_runner(returncode=2, stdout=""))
    assert result is None
    decided = [r for r in judge_ledger.read_records(path) if r["kind"] == "decided"][0]
    assert decided["stage"] == "call"
    assert decided["malformed"] is True
    assert decided["timed_out"] is False
    assert "non-zero" in decided["reason"]


def test_outcome_7a_empty_output(monkeypatch, tmp_path):
    path = _use_ledger(monkeypatch, tmp_path)
    judge_ledger.hook_start("escalation_diagnosis")
    result = _esc.decide(ESCALATION_PAYLOAD, runner=_fake_runner(returncode=0, stdout=""))
    assert result is None
    decided = [r for r in judge_ledger.read_records(path) if r["kind"] == "decided"][0]
    assert decided["malformed"] is True
    assert "no output" in decided["reason"]


def test_outcome_7a_unparseable_output(monkeypatch, tmp_path):
    path = _use_ledger(monkeypatch, tmp_path)
    judge_ledger.hook_start("escalation_diagnosis")
    result = _esc.decide(ESCALATION_PAYLOAD, runner=_fake_runner(returncode=0, stdout="MAYBE"))
    assert result is None
    decided = [r for r in judge_ledger.read_records(path) if r["kind"] == "decided"][0]
    assert decided["malformed"] is True
    assert "unparseable" in decided["reason"]


def test_outcome_7b_judge_raises(monkeypatch, tmp_path):
    path = _use_ledger(monkeypatch, tmp_path)
    judge_ledger.hook_start("escalation_diagnosis")
    result = _esc.decide(ESCALATION_PAYLOAD, runner=_fake_runner(raises=RuntimeError("boom")))
    assert result is None
    decided = [r for r in judge_ledger.read_records(path) if r["kind"] == "decided"][0]
    assert decided["stage"] == "call"
    assert decided["malformed"] is True
    assert decided["timed_out"] is False
    assert "raised" in decided["reason"]


def test_outcome_7c_judge_disabled(monkeypatch, tmp_path):
    path = _use_ledger(monkeypatch, tmp_path)
    monkeypatch.setenv(_esc._OUTAGE_ESCALATION_KILLSWITCH_ENV, "0")
    judge_ledger.hook_start("escalation_diagnosis")
    result = _esc.decide(ESCALATION_PAYLOAD, runner=_fake_runner(stdout="YES"))
    assert result is None
    decided = [r for r in judge_ledger.read_records(path) if r["kind"] == "decided"][0]
    assert decided["stage"] == "disabled"
    assert decided["verdict"] is False


# --- outcome 8: verdict rendered and discarded by post-judge except ---------

def test_outcome_8_discarded_by_post_judge_except(monkeypatch, tmp_path):
    path = _use_ledger(monkeypatch, tmp_path)

    def _raise(*a, **k):
        raise RuntimeError("decide blew up")

    monkeypatch.setattr(_esc, "decide", _raise)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(ESCALATION_PAYLOAD)))
    assert _esc.main() == 0
    records = judge_ledger.read_records(path)
    assert _kinds(records) == ["hook_start", "discarded"]
    assert "decide blew up" in records[1]["reason"]


# --- outcome 9: verdict rendered but not emitted -----------------------------

def test_outcome_9_verdict_rendered_but_not_emitted(monkeypatch, tmp_path):
    path = _use_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(_esc.advisor, "subprocess_runner", _fake_runner(returncode=0, stdout="YES"))

    def _raise(*a, **k):
        raise RuntimeError("print blew up")

    monkeypatch.setattr(_esc, "deny_with", _raise)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(ESCALATION_PAYLOAD)))
    assert _esc.main() == 0
    records = judge_ledger.read_records(path)
    final = [r for r in records if r["kind"] == "final"][0]
    emitted = [r for r in records if r["kind"] == "emitted"][0]
    assert final["has_directive"] is True
    assert emitted["ok"] is False
    assert emitted["had_directive"] is True


# --- outcome 10: verdict rendered but hook killed before emission ----------

def test_outcome_10_killed_before_emission(monkeypatch, tmp_path):
    path = _use_ledger(monkeypatch, tmp_path)
    judge_ledger.hook_start("escalation_diagnosis")
    judge_ledger.final(has_directive=True)
    # simulates the harness killing the process here: no `emitted` line follows
    records = judge_ledger.read_records(path)
    assert _kinds(records) == ["hook_start", "final"]


# --- outcome 11: hook killed or exited before verdict, outside any call ----

def test_outcome_11_exited_before_any_judge_call(monkeypatch, tmp_path):
    path = _use_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json at all"))
    assert _esc.main() == 0
    records = judge_ledger.read_records(path)
    assert _kinds(records) == ["hook_start"]


# --- two interleaved invocations: outcomes 8 and 9 stay attributable --------

def test_outcomes_8_and_9_stay_separated_across_interleaved_invocations(monkeypatch, tmp_path):
    path = _use_ledger(monkeypatch, tmp_path)

    def _raise(*a, **k):
        raise RuntimeError("first invocation's decide blew up")

    monkeypatch.setattr(_defer, "decide", _raise)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(DEFECTIVE_PAYLOAD)))
    assert _defer.main() == 0

    # restore decide(); this time break emission instead, for the SECOND invocation
    monkeypatch.undo()
    path = tmp_path / "ledger.jsonl"  # monkeypatch.undo() also unset the env var
    monkeypatch.setenv("AGENTCTL_JUDGE_LEDGER", str(path))
    monkeypatch.setattr(_defer.advisor, "subprocess_runner", _fake_runner(returncode=0, stdout="YES"))

    def _raise_print(*a, **k):
        raise RuntimeError("second invocation's emission blew up")

    monkeypatch.setattr("builtins.print", _raise_print)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(DEFECTIVE_PAYLOAD)))
    assert _defer.main() == 0

    records = judge_ledger.read_records(path)
    by_invocation: dict[str, list] = {}
    for r in records:
        by_invocation.setdefault(r["invocation_id"], []).append(r["kind"])
    assert len(by_invocation) == 2
    signatures = sorted(by_invocation.values())
    assert signatures == sorted([
        ["hook_start", "discarded"],
        ["hook_start", "entered", "decided", "final", "emitted"],
    ])
