"""Tests for hook-escalation-diagnosis-gate.py — the PRE-EMPTIVE PreToolUse gate
that denies an un-diagnosed external-service-failure escalation AskUserQuestion.

Matrix (all read from the module's own helpers, so the declare / overcome-
difficulty conditions are exercised without standing up a full agentctl session):
  escalation + no-declare + no-OD          -> deny
  escalation + overcome-difficulty invoked -> allow
  escalation + declare present             -> allow
  non-escalation ask                       -> allow
  malformed payload                        -> allow (fail-open)
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
HOOK = SCRIPTS_DIR / "hook-escalation-diagnosis-gate.py"


def _load():
    spec = importlib.util.spec_from_file_location("hook_escalation_diagnosis_gate", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load()

# A body that fires outage_escalation_detect (present-tense failure + ask frame).
ESC_BODY = "Сервис недоступен, не отвечает. К кому обратиться за доступом?"


def _ask_payload(question: str, options=None, **extra) -> dict:
    q = {"question": question, "options": options or []}
    return {
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": [q]},
        **extra,
    }


def _run_main(payload: dict, monkeypatch, capsys) -> str:
    """Drive main() in-process with a stubbed stdin, return the permissionDecision
    ('allow' when no output)."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    rc = _mod.main()
    assert rc == 0
    out = capsys.readouterr().out.strip()
    if not out:
        return "allow"
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"]


# --- deny/allow matrix through main() ---------------------------------------

def test_escalation_no_declare_no_od_denies(monkeypatch, capsys):
    monkeypatch.setattr(_mod, "_overcome_difficulty_invoked", lambda p: False)
    monkeypatch.setattr(_mod, "_difficulty_declared", lambda s: False)
    monkeypatch.setattr(_mod.advisor, "judge_outage_escalation", lambda *a, **kw: True)
    assert _run_main(_ask_payload(ESC_BODY), monkeypatch, capsys) == "deny"


def test_escalation_with_overcome_difficulty_allows(monkeypatch, capsys):
    # Judge stubbed True so the "allow" is driven by the OD check, not by an
    # accidental fail-open of a live (slow, non-deterministic) model call.
    monkeypatch.setattr(_mod.advisor, "judge_outage_escalation", lambda *a, **kw: True)
    monkeypatch.setattr(_mod, "_overcome_difficulty_invoked", lambda p: True)
    monkeypatch.setattr(_mod, "_difficulty_declared", lambda s: False)
    assert _run_main(_ask_payload(ESC_BODY), monkeypatch, capsys) == "allow"


def test_escalation_with_declare_present_allows(monkeypatch, capsys):
    monkeypatch.setattr(_mod.advisor, "judge_outage_escalation", lambda *a, **kw: True)
    monkeypatch.setattr(_mod, "_overcome_difficulty_invoked", lambda p: False)
    monkeypatch.setattr(_mod, "_difficulty_declared", lambda s: True)
    assert _run_main(_ask_payload(ESC_BODY), monkeypatch, capsys) == "allow"


def test_non_escalation_ask_allows(monkeypatch, capsys):
    monkeypatch.setattr(_mod, "_overcome_difficulty_invoked", lambda p: False)
    monkeypatch.setattr(_mod, "_difficulty_declared", lambda s: False)
    assert _run_main(_ask_payload("Which approach do you prefer for the parser?"),
                     monkeypatch, capsys) == "allow"


def test_option_text_drives_detection(monkeypatch, capsys):
    # The failure cue may live in an OPTION description, not the question stem.
    monkeypatch.setattr(_mod, "_overcome_difficulty_invoked", lambda p: False)
    monkeypatch.setattr(_mod, "_difficulty_declared", lambda s: False)
    monkeypatch.setattr(_mod.advisor, "judge_outage_escalation", lambda *a, **kw: True)
    payload = _ask_payload(
        "Что делать?",
        options=[{"label": "Retry", "description": "Сервис недоступен и не отвечает"}],
    )
    assert _run_main(payload, monkeypatch, capsys) == "deny"


# --- fail-open ---------------------------------------------------------------

def test_malformed_payload_allows(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json at all"))
    assert _mod.main() == 0
    assert capsys.readouterr().out.strip() == ""


def test_ignores_other_tools(monkeypatch, capsys):
    payload = {"tool_name": "Bash", "tool_input": {"command": "curl x"}}
    assert _run_main(payload, monkeypatch, capsys) == "allow"


# --- helper units ------------------------------------------------------------

def test_ask_text_collects_all_user_facing_strings():
    ti = {"questions": [
        {"question": "q1", "header": "h1",
         "options": [{"label": "l1", "description": "d1"}, {"label": "l2"}]},
        {"question": "q2", "options": []},
    ]}
    text = _mod._ask_text(ti)
    for token in ("q1", "h1", "l1", "d1", "l2", "q2"):
        assert token in text


def test_ask_text_tolerates_garbage():
    assert _mod._ask_text({}) == ""
    assert _mod._ask_text({"questions": "nope"}) == ""
    assert _mod._ask_text(None) == ""  # type: ignore[arg-type]


def test_gate_decision_pure():
    assert _mod.gate_decision(True, False, False)[0] == "deny"
    assert _mod.gate_decision(True, True, False)[0] == "allow"
    assert _mod.gate_decision(True, False, True)[0] == "allow"
    assert _mod.gate_decision(False, False, False)[0] == "allow"


def test_overcome_difficulty_invoked_reads_transcript(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text("\n".join([
        json.dumps({"message": {"role": "user", "content": "hi"}}),
        json.dumps({"message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Skill",
             "input": {"skill": "overcome-difficulty"}}]}}),
    ]) + "\n", encoding="utf-8")
    assert _mod._overcome_difficulty_invoked(str(t)) is True


def test_overcome_difficulty_absent_returns_false(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "Skill", "input": {"skill": "developer"}}]}}) + "\n",
        encoding="utf-8")
    assert _mod._overcome_difficulty_invoked(str(t)) is False


def test_overcome_difficulty_failsafe_on_missing_file(tmp_path):
    assert _mod._overcome_difficulty_invoked(str(tmp_path / "absent.jsonl")) is False
    assert _mod._overcome_difficulty_invoked(None) is False


def test_difficulty_declared_failsafe_on_unknown_session():
    # No live session state for a random id -> fail-safe False, gate falls back
    # to the other guards (never a spurious allow-suppression).
    assert _mod._difficulty_declared("no-such-session-xyz") is False
    assert _mod._difficulty_declared("") is False


# --- end-to-end via subprocess: exit 0, deny JSON on stdout -----------------

def _run_subprocess(payload: dict, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def test_deny_on_escalation_no_context_real_transcript_read(monkeypatch, capsys, tmp_path):
    # A real transcript with no overcome-difficulty and a random session id (no
    # state) -> both context guards are absent (read for real, not monkeypatched)
    # -> deny, once the semantic judge confirms a genuine escalation. The judge
    # itself is stubbed True here: a live model call is not a reliable test seam
    # (see prose_binary_ask's tests, which never drive main() via a real
    # subprocess either) -- test_subprocess_allow_when_overcome_difficulty_in_transcript
    # below is what actually proves the real-subprocess, fail-open path.
    monkeypatch.setattr(_mod.advisor, "judge_outage_escalation", lambda *a, **kw: True)
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"message": {"role": "user", "content": "hi"}}) + "\n",
                 encoding="utf-8")
    payload = _ask_payload(ESC_BODY, transcript_path=str(t),
                           session_id="no-such-session-xyz")
    assert _run_main(payload, monkeypatch, capsys) == "deny"


def test_subprocess_allow_when_overcome_difficulty_in_transcript(tmp_path):
    # Kill-switch disabled so the judge fails open fast/deterministically without
    # a live model subprocess -- this test proves the real main() subprocess path,
    # not the judge itself (that's the fake-runner tests further down).
    t = tmp_path / "t.jsonl"
    t.write_text("\n".join([
        json.dumps({"message": {"role": "user", "content": "hi"}}),
        json.dumps({"message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Skill",
             "input": {"skill": "overcome-difficulty"}}]}}),
    ]) + "\n", encoding="utf-8")
    payload = _ask_payload(ESC_BODY, transcript_path=str(t),
                           session_id="no-such-session-xyz")
    env = dict(os.environ, **{_mod._OUTAGE_ESCALATION_KILLSWITCH_ENV: "0"})
    p = _run_subprocess(payload, env=env)
    assert p.returncode == 0
    assert p.stdout.strip() == ""  # allow


# --- prefilter-AND-judge conjunction (regex-not-for-semantic-classification) --

def _fake_runner(text: str, code: int = 0):
    from agentctl.dispatch import RunResult

    def run(argv, **kwargs):
        return RunResult(code, text, "")

    return run


def test_false_positive_escalation_no_deny_when_judge_says_no(monkeypatch):
    # The prefilter fires on ESC_BODY's failure-cue + ask-frame regex, but the
    # semantic judge recognizes it as a paraphrase/meta-mention, not a genuine
    # escalation -- the false-positive scenario this stage's plan exists to fix.
    assert _mod.decide(
        _ask_payload(ESC_BODY), runner=_fake_runner("NO"),
    ) is None


def test_genuine_escalation_denies_when_judge_confirms():
    assert _mod.decide(
        _ask_payload(ESC_BODY), runner=_fake_runner("YES"),
    ) == _mod._DENY_REASON


def test_prefilter_negative_never_invokes_judge():
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        from agentctl.dispatch import RunResult
        return RunResult(0, "YES", "")

    assert _mod.decide(
        _ask_payload("Which approach do you prefer for the parser?"), runner=run,
    ) is None
    assert calls == []


def test_missing_runner_fails_open_no_deny():
    assert _mod.decide(_ask_payload(ESC_BODY), runner=None) is None


def test_raising_runner_fails_open_no_deny():
    def run(argv, **kwargs):
        raise RuntimeError("boom")

    assert _mod.decide(_ask_payload(ESC_BODY), runner=run) is None


def test_killswitch_disabled_never_forces_deny(monkeypatch):
    # Disabling the kill-switch always fails open to False -- it can never be
    # used to force a deny, only to suppress one.
    monkeypatch.setenv(_mod._OUTAGE_ESCALATION_KILLSWITCH_ENV, "0")
    assert _mod.decide(_ask_payload(ESC_BODY), runner=_fake_runner("YES")) is None
