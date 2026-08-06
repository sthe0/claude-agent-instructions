"""Tests for hook-deferring-disposition-gate.py — the PreToolUse gate that denies
an ask whose every option defers or refuses work the agent could do itself.

The judge never reaches a live model here: a fake runner supplies its YES/NO
answer, so the judge's own parsing, the regex prefilter and the deny-JSON
assembly all run for real while the test stays deterministic.

Matrix:
  defective ask (nothing fixes it now)      -> deny
  forced deferral (work is someone else's)  -> allow, judge consulted
  no work at all (a preference)             -> allow, judge never consulted
  judge raises                              -> allow, exit 0 (fail-open)
"""
from __future__ import annotations

import ast
import importlib.util
import io
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
HOOK = SCRIPTS_DIR / "hook-deferring-disposition-gate.py"


def _load():
    spec = importlib.util.spec_from_file_location("hook_deferring_disposition_gate", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load()


# --- fixtures: the three asks the gate must tell apart ------------------------

# The ask that provoked this gate: a defect the agent had diagnosed and had the
# rights to fix, offered as a menu in which no branch fixes it.
DEFECTIVE = {
    "question": "Что делать с найденным дефектом критерия в arc-mounts-gc.sh?",
    "options": [
        {"label": "Да, завести отдельной задачей (Рекомендую)",
         "description": "зафиксировать в бэклоге, чинить отдельно"},
        {"label": "Не трогать", "description": "оставить как есть"},
    ],
}

# Same deferral vocabulary, legitimate: the work is not the agent's to do.
FORCED_DEFERRAL = {
    "question": "Кто чинит просроченный сертификат?",
    "options": [
        {"label": "Передать владельцу сервиса (Рекомендую)"},
        {"label": "Завести тикет дежурному"},
    ],
}

# No work is being postponed because there is no work — a plain preference.
NO_WORK = {
    "question": "Какой язык ответов?",
    "options": [{"label": "Русский (Рекомендую)"}, {"label": "Английский"}],
}

# The question stem itself carries a defer-shaped cue word ("задачу") while
# every option is a plain confirm — the false-positive class S1 exists to
# avoid: the PREFILTER must key off the option text, not the stem.
STEM_CUE_ONLY = {
    "question": "Считаем задачу решённой?",
    "options": [{"label": "Да (Рекомендую)"}, {"label": "Нет"}],
}


def _payload(question: dict, **extra) -> dict:
    return {"tool_name": "AskUserQuestion", "tool_input": {"questions": [question]}, **extra}


def _runner(answer: str, calls: list | None = None):
    from agentctl.dispatch import RunResult

    def run(argv, **kwargs):
        if calls is not None:
            calls.append(argv)
        return RunResult(0, answer, "")

    return run


class _FakeTime:
    """Stand-in for the hook's module-level `time` import: `.monotonic()`
    returns values from `sequence` in call order (holding the last value once
    exhausted), so budget/deadline tests are deterministic and instant — no
    test here actually sleeps."""

    def __init__(self, sequence):
        self._values = list(sequence)
        self._i = 0

    def monotonic(self):
        value = self._values[self._i]
        if self._i < len(self._values) - 1:
            self._i += 1
        return value


def _run_main(payload: dict, runner, monkeypatch, capsys) -> str:
    """Drive main() end-to-end with a stubbed stdin and a fake judge runner;
    return the permissionDecision ('allow' when the hook prints nothing)."""
    monkeypatch.setattr(_mod.advisor, "subprocess_runner", runner)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert _mod.main() == 0
    out = capsys.readouterr().out.strip()
    if not out:
        return "allow"
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"]


# --- the three asks ----------------------------------------------------------

def test_defective_ask_is_denied(monkeypatch, capsys):
    calls: list = []
    assert _run_main(_payload(DEFECTIVE), _runner("YES", calls), monkeypatch, capsys) == "deny"
    assert calls, "the judge must be consulted before a deny"


def test_defective_ask_deny_carries_actionable_reason():
    decision = _mod.decide(_payload(DEFECTIVE), runner=_runner("YES"))
    reason = decision["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason.startswith(_mod._DENY_REASON)


def test_defective_ask_deny_names_the_offending_question(monkeypatch):
    # The DEFECTIVE fixture's own stem must show up verbatim (it's well under
    # _STEM_MAX_CHARS) so the agent knows exactly which menu to rewrite.
    monkeypatch.setattr(_mod, "time", _FakeTime([0.0, 0.0]))
    decision = _mod.decide(_payload(DEFECTIVE), runner=_runner("YES"))
    reason = decision["hookSpecificOutput"]["permissionDecisionReason"]
    assert 'question #1: "' + DEFECTIVE["question"] + '".' in reason


def test_forced_deferral_is_allowed(monkeypatch, capsys):
    # The allow must come from the JUDGE, not from a prefilter miss — otherwise
    # the gate would be passing this ask for the wrong reason.
    calls: list = []
    assert _run_main(_payload(FORCED_DEFERRAL), _runner("NO", calls), monkeypatch, capsys) == "allow"
    assert calls, "the prefilter must fire on this ask and hand it to the judge"


def test_ask_without_work_is_allowed_without_consulting_the_judge(monkeypatch, capsys):
    calls: list = []
    assert _run_main(_payload(NO_WORK), _runner("YES", calls), monkeypatch, capsys) == "allow"
    assert calls == [], "an ask that postpones nothing must not cost a judge call"


def test_prefilter_ignores_a_defer_cue_in_the_question_stem_only(monkeypatch, capsys):
    calls: list = []
    assert _run_main(_payload(STEM_CUE_ONLY), _runner("YES", calls), monkeypatch, capsys) == "allow"
    assert calls == [], "a defer cue in the stem alone (not in any option) must not cost a judge call"


def test_multi_question_ask_denied_when_only_the_second_menu_is_defective(monkeypatch, capsys):
    # Question 1 is legitimate (an option does the work now) and never fires
    # the prefilter; question 2 is the defective menu. The gate must not stop
    # at the first, clean question — it must reach and deny on the second, and
    # the reason must name #2 (not #1) as the offending menu — otherwise the
    # agent would be pointed at the wrong question to rewrite.
    payload = {
        "tool_name": "AskUserQuestion",
        "tool_input": {
            "questions": [
                {
                    "question": "Что делать с багом X?",
                    "options": [{"label": "Починить сейчас (Рекомендую)"}],
                },
                DEFECTIVE,
            ]
        },
    }
    assert _run_main(payload, _runner("YES"), monkeypatch, capsys) == "deny"

    decision = _mod.decide(payload, runner=_runner("YES"))
    reason = decision["hookSpecificOutput"]["permissionDecisionReason"]
    assert 'question #2: "' + DEFECTIVE["question"] + '"' in reason


# --- ask-wide judge budget (_ASK_JUDGE_BUDGET_S) ------------------------------
#
# Over n=18 this judge runs at median 17.43s, p90 37.58s, max 39.99s
# (lib/judge_latency.py), so a multi-question ask judging every fired menu would
# run several times past any timeout that may sit in front of an interactive
# menu. decide() instead opens ONE _ASK_JUDGE_BUDGET_S=45 deadline for the whole
# call and gives each judge call whatever remains, refusing (fail-open) once too
# little remains for a call to plausibly finish (_ASK_JUDGE_MIN_CALL_S=38, this
# judge's ceil(p90)). These tests inject a fake monotonic clock so no test here
# waits on a real timer.

def test_budget_exhausted_after_first_menu_skips_second_judge_call(monkeypatch):
    """The declared K=1 limit, pinned rather than left as a hope: a second fired
    menu is reached only if the first call returned with the 38s floor still left,
    i.e. in under 7s — faster than the 10.29s fastest run ever measured. So a
    multi-menu ask is judged on its first fired menu and allowed on the rest, and
    the hook's whole budget is also its per-call ceiling."""
    payload = {
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": [DEFECTIVE, DEFECTIVE]},
    }
    calls: list = []
    # deadline calc -> 0.0 (deadline=45.0); Q1 remaining calc -> 0.0 (remaining
    # 45.0, judged); Q2 remaining calc -> 10.0 (remaining 35.0 < the 38 floor).
    monkeypatch.setattr(_mod, "time", _FakeTime([0.0, 0.0, 10.0]))
    assert _mod.decide(payload, runner=_runner("NO", calls)) is None
    assert len(calls) == 1, "budget exhaustion must stop judging before the second menu"


def test_remaining_below_the_floor_never_calls_the_judge(monkeypatch):
    calls: list = []
    # deadline calc -> 0.0 (deadline=45.0); Q1 remaining calc -> 10.0 (remaining
    # 35.0 < the 38 floor) -- exhausted before even the first call.
    monkeypatch.setattr(_mod, "time", _FakeTime([0.0, 10.0]))
    assert _mod.decide(_payload(DEFECTIVE), runner=_runner("YES", calls)) is None
    assert calls == [], "a budget already below the floor must not spend it on a doomed call"


def test_call_timeout_is_the_remaining_budget_and_stays_under_the_per_call_ceiling(monkeypatch):
    seen: dict = {}

    def run(argv, **kwargs):
        seen.update(kwargs)
        from agentctl.dispatch import RunResult

        return RunResult(0, "NO", "")

    # deadline calc -> 0.0 (deadline=45.0); Q1 remaining calc -> 5.0 (remaining 40.0).
    monkeypatch.setattr(_mod, "time", _FakeTime([0.0, 5.0]))
    _mod.decide(_payload(DEFECTIVE), runner=run)
    assert seen.get("timeout") == 40.0
    assert _mod._ASK_JUDGE_MIN_CALL_S <= seen["timeout"] <= _mod._ASK_JUDGE_BUDGET_S
    # Drawn from this hook's own budget, not inherited from advisor's last-resort
    # default -- which is sized for a caller with no timeout above it at all and
    # is therefore not a bound this hook can honour.
    assert seen["timeout"] != _mod.advisor._DEFERRING_DISPOSITION_TIMEOUT_S


def test_the_per_call_ceiling_is_this_hooks_own_constant():
    """The defect this pins was live in this file's subject: decide() passed
    advisor._DEFERRING_DISPOSITION_TIMEOUT_S as the cap it handed
    next_call_timeout — a FOREIGN constant, owned by a module that knows nothing
    of this hook's registration, which merely happened to be numerically
    survivable. Read structurally, from the source: a value check cannot tell two
    equal numbers apart, and under the family ceiling rule they can be equal.

    decide() now draws timeout AND remaining from one combined read
    (JudgeBudget.remaining_and_timeout) instead of two separate calls — see
    that method's docstring — so the cap lives on that call now, not on
    next_call_timeout directly."""
    tree = ast.parse(Path(_mod.__file__).read_text(encoding="utf-8"))
    caps = [
        node.args[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", None) in ("next_call_timeout", "remaining_and_timeout")
        and node.args
    ]
    assert caps, "decide() no longer draws its call timeout from a budget"
    for cap in caps:
        assert isinstance(cap, ast.Name) and cap.id == "_ASK_JUDGE_BUDGET_S", (
            "the per-call ceiling must be this hook's own _ASK_JUDGE_BUDGET_S, "
            f"not {ast.dump(cap)}"
        )


# --- fail-open ---------------------------------------------------------------

def test_raising_judge_fails_open(monkeypatch, capsys):
    def run(argv, **kwargs):
        raise RuntimeError("boom")

    assert _run_main(_payload(DEFECTIVE), run, monkeypatch, capsys) == "allow"


def test_missing_runner_fails_open():
    assert _mod.decide(_payload(DEFECTIVE), runner=None) is None


def test_killswitch_off_never_denies(monkeypatch):
    monkeypatch.setenv(_mod._DEFERRING_DISPOSITION_KILLSWITCH_ENV, "0")
    assert _mod.decide(_payload(DEFECTIVE), runner=_runner("YES")) is None


def test_malformed_payload_allows(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json at all"))
    assert _mod.main() == 0
    assert capsys.readouterr().out.strip() == ""


def test_non_dict_json_payload_allows(monkeypatch, capsys):
    # Valid JSON that parses cleanly but isn't an object — distinct from
    # test_malformed_payload_allows (invalid JSON), targets main()'s own
    # `if not isinstance(payload, dict)` guard specifically.
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(["not", "a", "dict"])))
    assert _mod.main() == 0
    assert capsys.readouterr().out.strip() == ""


def test_other_tools_are_ignored():
    assert _mod.decide({"tool_name": "Bash", "tool_input": {"command": "ls"}},
                       runner=_runner("YES")) is None


# --- helper units ------------------------------------------------------------

def test_truncate_stem_truncates_a_stem_longer_than_the_limit():
    stem = "a" * (_mod._STEM_MAX_CHARS + 40)
    truncated = _mod._truncate_stem(stem)
    assert len(truncated) <= _mod._STEM_MAX_CHARS
    assert truncated.endswith("…")


def test_prefilter_reads_option_text_not_only_the_question():
    # The deferral may live entirely in an option, with a neutral question stem.
    text = _mod._ask_text({"questions": [{
        "question": "Как поступим?",
        "options": [{"label": "Ок", "description": "оставить как есть"}],
    }]})
    assert _mod._prefilter(text) is True


def test_prefilter_tolerates_garbage():
    assert _mod._prefilter("") is False
    assert _mod._prefilter(None) is False  # type: ignore[arg-type]
    assert _mod._ask_text({"questions": "nope"}) == ""
    assert _mod._ask_text(None) == ""  # type: ignore[arg-type]
