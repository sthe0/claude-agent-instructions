"""Tests for semantic_judge — the language-agnostic model-backed perception half of the
end-of-turn gate's three text classifiers (binary_ask / si_feedback / outage_escalation).

What these PIN:
  - the fail-open contract: a disabled kind, a None runner, a non-zero exit, empty or
    unparseable output, and a raising runner ALL return None (never a false True/False),
    so control flow is byte-identical to judge-absent;
  - the gating layer (SEMANTIC_JUDGE env override, config mode, per-kind kill-switch),
    including that a disabled kind spawns NO subprocess;
  - the live runner launches `claude -p --model <model>` with AGENT_RECURSION_DEPTH>=1 in
    the child env (no hook->claude->hook recursion);
  - language-agnosticism: the module never inspects the input language — an EN and a RU
    string with the same verdict return the same result.
"""
from __future__ import annotations

import subprocess

import pytest

import semantic_judge
from agentctl.config import Thresholds
from agentctl.dispatch import RunResult

KINDS = semantic_judge.KINDS


def _runner(text, code=0):
    def runner(argv):
        return RunResult(code, stdout=text, stderr="")
    return runner


def _raising_runner(argv):
    raise RuntimeError("boom")


def _recording_runner():
    calls = []

    def runner(argv):
        calls.append(argv)
        return RunResult(0, stdout="YES\nbecause", stderr="")

    return runner, calls


# ── verdict parsing (per kind) ────────────────────────────────────────────────

class TestVerdictParsing:
    @pytest.mark.parametrize("kind", KINDS)
    def test_yes_is_true(self, kind):
        assert semantic_judge.judge(kind, "x", runner=_runner("YES\nreason"), enabled=True) is True

    @pytest.mark.parametrize("kind", KINDS)
    def test_no_is_false(self, kind):
        assert semantic_judge.judge(kind, "x", runner=_runner("NO\nreason"), enabled=True) is False

    @pytest.mark.parametrize("kind", KINDS)
    def test_first_line_only_head_matters(self, kind):
        # A leading YES/NO on the first non-empty line decides; junk after it is ignored.
        assert semantic_judge.judge(kind, "x", runner=_runner("  yes it does  \nreason"), enabled=True) is True

    @pytest.mark.parametrize("kind", KINDS)
    def test_unparseable_head_is_none(self, kind):
        assert semantic_judge.judge(kind, "x", runner=_runner("MAYBE\n..."), enabled=True) is None


# ── fail-open contract (per kind) ─────────────────────────────────────────────

class TestFailOpen:
    @pytest.mark.parametrize("kind", KINDS)
    def test_runner_none_is_none(self, kind):
        assert semantic_judge.judge(kind, "x", runner=None, enabled=True) is None

    @pytest.mark.parametrize("kind", KINDS)
    def test_non_zero_exit_is_none(self, kind):
        assert semantic_judge.judge(kind, "x", runner=_runner("YES", code=1), enabled=True) is None

    @pytest.mark.parametrize("kind", KINDS)
    def test_empty_stdout_is_none(self, kind):
        assert semantic_judge.judge(kind, "x", runner=_runner("  \n  \n"), enabled=True) is None

    @pytest.mark.parametrize("kind", KINDS)
    def test_raising_runner_is_none(self, kind):
        assert semantic_judge.judge(kind, "x", runner=_raising_runner, enabled=True) is None

    def test_unknown_kind_is_none_no_runner_call(self):
        runner, calls = _recording_runner()
        assert semantic_judge.judge("nonexistent", "x", runner=runner, enabled=True) is None
        assert calls == []


# ── gating: resolve_enabled + per-kind kill-switch ────────────────────────────

class TestResolveEnabled:
    def test_env_force_on_overrides_config_off(self, monkeypatch):
        monkeypatch.setenv("SEMANTIC_JUDGE", "1")
        thr = Thresholds({"semantic-judge-mode": "off"})
        assert semantic_judge.resolve_enabled("binary_ask", thresholds=thr) is True

    def test_env_force_off_overrides_config_on(self, monkeypatch):
        monkeypatch.setenv("SEMANTIC_JUDGE", "0")
        thr = Thresholds({"semantic-judge-mode": "on",
                          "semantic-judge-kinds": "binary_ask"})
        assert semantic_judge.resolve_enabled("binary_ask", thresholds=thr) is False

    def test_config_on_kind_in_list_enables(self, monkeypatch):
        monkeypatch.delenv("SEMANTIC_JUDGE", raising=False)
        thr = Thresholds({"semantic-judge-mode": "on",
                          "semantic-judge-kinds": "binary_ask,outage_escalation,si_feedback"})
        for kind in KINDS:
            assert semantic_judge.resolve_enabled(kind, thresholds=thr) is True

    def test_config_on_kind_absent_disables(self, monkeypatch):
        monkeypatch.delenv("SEMANTIC_JUDGE", raising=False)
        thr = Thresholds({"semantic-judge-mode": "on",
                          "semantic-judge-kinds": "binary_ask,outage_escalation"})
        assert semantic_judge.resolve_enabled("si_feedback", thresholds=thr) is False

    def test_config_off_disables(self, monkeypatch):
        monkeypatch.delenv("SEMANTIC_JUDGE", raising=False)
        thr = Thresholds({"semantic-judge-mode": "off"})
        assert semantic_judge.resolve_enabled("binary_ask", thresholds=thr) is False

    def test_missing_mode_key_fails_off(self, monkeypatch):
        monkeypatch.delenv("SEMANTIC_JUDGE", raising=False)
        thr = Thresholds({})
        assert semantic_judge.resolve_enabled("binary_ask", thresholds=thr) is False

    def test_missing_kinds_key_defaults_all_enabled(self, monkeypatch):
        monkeypatch.delenv("SEMANTIC_JUDGE", raising=False)
        thr = Thresholds({"semantic-judge-mode": "on"})
        for kind in KINDS:
            assert semantic_judge.resolve_enabled(kind, thresholds=thr) is True

    def test_disabled_kind_spawns_no_subprocess(self, monkeypatch):
        """The per-kind kill-switch must short-circuit BEFORE the runner is called."""
        monkeypatch.delenv("SEMANTIC_JUDGE", raising=False)
        thr = Thresholds({"semantic-judge-mode": "on",
                          "semantic-judge-kinds": "binary_ask,outage_escalation"})
        runner, calls = _recording_runner()
        # enabled=None => judge consults resolve_enabled, which disables si_feedback here.
        result = semantic_judge.judge("si_feedback", "теперь сделай X", runner=runner, thresholds=thr)
        assert result is None
        assert calls == []


# ── live runner: argv shape + recursion-guarded env ───────────────────────────

class TestLiveRunner:
    def test_judge_argv_starts_with_cheap_model(self):
        runner, calls = _recording_runner()
        thr = Thresholds({"semantic-judge-model": "sonnet"})
        semantic_judge.judge("binary_ask", "x", runner=runner, thresholds=thr, enabled=True)
        assert calls[0][:4] == ["claude", "-p", "--model", "sonnet"]

    def test_judge_argv_uses_configured_model(self):
        runner, calls = _recording_runner()
        thr = Thresholds({"semantic-judge-model": "haiku"})
        semantic_judge.judge("binary_ask", "x", runner=runner, thresholds=thr, enabled=True)
        assert calls[0][3] == "haiku"

    def test_subprocess_runner_sets_recursion_depth_ge_1(self, monkeypatch):
        monkeypatch.delenv("AGENT_RECURSION_DEPTH", raising=False)
        seen = {}

        def fake_run(argv, **kwargs):
            seen["env"] = kwargs.get("env")

            class P:
                returncode = 0
                stdout = "YES\nr"
                stderr = ""

            return P()

        monkeypatch.setattr(subprocess, "run", fake_run)
        semantic_judge.subprocess_runner(["claude", "-p", "x"], timeout=1)
        assert int(seen["env"]["AGENT_RECURSION_DEPTH"]) >= 1

    def test_subprocess_runner_increments_existing_depth(self, monkeypatch):
        monkeypatch.setenv("AGENT_RECURSION_DEPTH", "2")

        def fake_run(argv, **kwargs):
            seen_env.update(kwargs.get("env") or {})

            class P:
                returncode = 0
                stdout = ""
                stderr = ""

            return P()

        seen_env = {}
        monkeypatch.setattr(subprocess, "run", fake_run)
        semantic_judge.subprocess_runner(["claude", "-p", "x"], timeout=1)
        assert int(seen_env["AGENT_RECURSION_DEPTH"]) >= 3

    def test_subprocess_runner_timeout_returns_non_zero_not_raise(self, monkeypatch):
        def raise_timeout(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

        monkeypatch.setattr(subprocess, "run", raise_timeout)
        result = semantic_judge.subprocess_runner(["claude", "-p", "x"], timeout=1)
        assert result.returncode != 0


# ── language-agnosticism ──────────────────────────────────────────────────────

class TestLanguageAgnostic:
    def test_same_verdict_for_en_and_ru(self):
        """The module never inspects the input language: the same stubbed YES verdict is
        returned for an English and a Russian binary-confirm question."""
        en = "Do you want me to publish v11 now?"
        ru = "Хочешь, чтобы я опубликовал v11 сейчас?"
        yes = _runner("YES\nit is a binary confirm ask")
        assert semantic_judge.judge("binary_ask", en, runner=yes, enabled=True) is True
        assert semantic_judge.judge("binary_ask", ru, runner=yes, enabled=True) is True

    def test_si_feedback_ru_correction_true(self):
        ru = "Отвечай на моём языке, а не по-английски."
        assert semantic_judge.judge("si_feedback", ru, runner=_runner("YES\ncorrection"), enabled=True) is True
