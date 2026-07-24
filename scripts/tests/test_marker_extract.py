"""Dependency-injected tests for lib.marker_extract — zero real model calls.

Every test either supplies a fake `runner` (a callable returning a canned
RunResult, matching the injectable pattern in agentctl.dispatch/advisor) or
monkeypatches the kill switch / `shutil.which` guard. None of these tests
invoke a real `claude -p` process.
"""
from __future__ import annotations

import pytest

from lib import marker_extract
from lib.planner_plan_check import RETURN_MARKERS

# spawn-cursor-escape.py's own, deliberately different vocabulary — the caller
# passes `allowed`, so the module must never assume the specialist set.
ESCAPE_MARKERS = ("RESOLVED", "INVESTIGATION", "LOOP_DETECTED")


def _reply(marker: str = "COMPLETED", digest: str = "did the thing", plan: str = "NONE") -> str:
    return f"MARKER: {marker}\nDIGEST: {digest}\nPLAN: {plan}\n"


def _fake_runner(stdout: str = "", returncode: int = 0, stderr: str = ""):
    def run(argv, **kwargs):
        return marker_extract.RunResult(returncode, stdout, stderr)

    return run


def _raising_runner(exc: Exception):
    def run(argv, **kwargs):
        raise exc

    return run


# --- (a) a well-formed reply resolves, for every marker in both vocabularies ---


@pytest.mark.parametrize("marker", RETURN_MARKERS)
def test_every_specialist_marker_resolves(marker):
    result = marker_extract.extract(
        "body", allowed=RETURN_MARKERS, runner=_fake_runner(_reply(marker))
    )
    assert result.marker == marker
    assert result.digest == "did the thing"
    assert result.plan_path is None
    assert result.degraded is False


@pytest.mark.parametrize("marker", ESCAPE_MARKERS)
def test_every_escape_marker_resolves(marker):
    result = marker_extract.extract(
        "body", allowed=ESCAPE_MARKERS, runner=_fake_runner(_reply(marker))
    )
    assert result.marker == marker


def test_plan_line_is_carried_when_present():
    result = marker_extract.extract(
        "body",
        allowed=RETURN_MARKERS,
        runner=_fake_runner(_reply("PLAN-READY", plan="/home/u/plans/x.toml")),
    )
    assert result.plan_path == "/home/u/plans/x.toml"


# --- (b1) role hint SHAPE ------------------------------------------------------


def test_hints_by_kind_rows_are_subsets_of_return_markers():
    for kind, markers in marker_extract.HINTS_BY_KIND.items():
        assert markers, f"{kind!r} has an empty hint tuple"
        for marker in markers:
            assert marker in RETURN_MARKERS, f"{kind!r} hints an unknown marker {marker!r}"


def test_hint_markers_for_covers_the_role_specific_markers():
    assert "PLAN-READY" in marker_extract.hint_markers_for("planner")
    assert "REVIEW" in marker_extract.hint_markers_for("thinker")


def test_hint_markers_for_unlisted_kind_is_the_full_vocabulary():
    # An incomplete table must cost prompt sharpness, never acceptance: an
    # unknown role asks the flat closed-set question instead of narrowing.
    assert marker_extract.hint_markers_for("no-such-role") == RETURN_MARKERS
    assert marker_extract.hint_markers_for(None) == RETURN_MARKERS


# --- (b2) role hint ACCEPTANCE: a hint never causes a rejection ----------------


@pytest.mark.parametrize(
    "kind,unusual",
    [
        ("developer", "REPLAN"),
        ("code-reviewer", "PERMISSION-REQUEST"),
        ("tech-writer", "REVIEW"),
        ("yandex-cloud-expert", "PLAN-READY"),
        ("planner", "REVIEW"),
    ],
)
def test_marker_unusual_for_the_hinted_role_is_still_accepted(kind, unusual):
    result = marker_extract.extract(
        "body",
        allowed=RETURN_MARKERS,
        hint=marker_extract.hint_markers_for(kind),
        runner=_fake_runner(_reply(unusual)),
    )
    assert result.marker == unusual, result.reason


# --- (b3) rejection is OFF-VOCABULARY only ------------------------------------


@pytest.mark.parametrize("token", ["DONE", "APPROVED", "BANANA"])
@pytest.mark.parametrize("kind", ["developer", "planner", None])
def test_token_in_no_vocabulary_is_rejected_regardless_of_kind(token, kind):
    result = marker_extract.extract(
        "body",
        allowed=RETURN_MARKERS,
        hint=marker_extract.hint_markers_for(kind) if kind else (),
        runner=_fake_runner(_reply(token)),
    )
    assert result.marker is None
    assert token in result.reason


def test_build_prompt_with_a_hint_still_offers_every_allowed_token():
    prompt = marker_extract.build_prompt("body", RETURN_MARKERS, hint=("COMPLETED",))
    for token in RETURN_MARKERS:
        assert token in prompt, f"hint narrowed the offered vocabulary: {token} missing"


def test_build_prompt_lists_only_the_allowed_set_on_its_allowed_line():
    prompt = marker_extract.build_prompt("x", ESCAPE_MARKERS)
    allowed_line = prompt.split("Allowed markers:")[1].split("\n")[0]
    assert "RESOLVED, INVESTIGATION, LOOP_DETECTED" in allowed_line
    assert "COMPLETED" not in allowed_line


def test_build_prompt_embeds_the_result_text():
    prompt = marker_extract.build_prompt("the exact specialist output", RETURN_MARKERS)
    assert "the exact specialist output" in prompt


def test_build_prompt_omits_the_hint_sentence_when_no_hint():
    assert "usual choices for this role" not in marker_extract.build_prompt("x", RETURN_MARKERS)


# --- (c) FAIL CLOSED: every non-success path yields marker=None, not degraded --


@pytest.mark.parametrize(
    "label,runner",
    [
        ("none_verdict", _fake_runner(_reply("NONE"))),
        ("off_vocabulary", _fake_runner(_reply("APPROVED"))),
        ("empty_stdout", _fake_runner("")),
        ("unparseable_reply", _fake_runner("I think it is probably completed?\n")),
        ("nonzero_exit", _fake_runner("", returncode=1, stderr="boom")),
        ("runner_raises", _raising_runner(RuntimeError("exploded"))),
        (
            "timeout_runresult",
            _fake_runner("", returncode=1, stderr="marker extractor timed out after 30s"),
        ),
    ],
)
def test_every_failure_path_fails_closed(label, runner):
    result = marker_extract.extract("body", allowed=RETURN_MARKERS, runner=runner)
    assert result.marker is None, label
    # degraded is reserved for the one OBSERVABLE condition (claude absent); a
    # judgement-bearing failure must route to MALFORMED, not to the legacy scan.
    assert result.degraded is False, label
    assert result.reason, label


def test_subprocess_runner_converts_a_timeout_into_a_failed_runresult(monkeypatch):
    import subprocess

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))

    monkeypatch.setattr(marker_extract.subprocess, "run", fake_run)
    result = marker_extract.subprocess_runner(["claude"], timeout=7)
    assert result.returncode == 1
    assert "timed out after 7s" in result.stderr


# --- (g2) kill switch: the ENV VAR itself, pinned to the documented value ------


def test_enabled_is_true_when_unset(monkeypatch):
    monkeypatch.delenv(marker_extract.ENV_KILL_SWITCH, raising=False)
    assert marker_extract.enabled() is True


def test_enabled_is_false_only_for_the_exact_documented_value(monkeypatch):
    monkeypatch.setenv(marker_extract.ENV_KILL_SWITCH, "0")
    assert marker_extract.enabled() is False
    # Pinned to "0", not to truthiness — "1" and any other value keep it on.
    monkeypatch.setenv(marker_extract.ENV_KILL_SWITCH, "1")
    assert marker_extract.enabled() is True


def test_extractor_available_reflects_shutil_which(monkeypatch):
    monkeypatch.setattr(marker_extract.shutil, "which", lambda name: "/usr/bin/claude")
    assert marker_extract.extractor_available() is True
    monkeypatch.setattr(marker_extract.shutil, "which", lambda name: None)
    assert marker_extract.extractor_available() is False


def test_model_defaults_to_haiku_and_honours_the_env_override(monkeypatch):
    monkeypatch.delenv(marker_extract.ENV_MODEL, raising=False)
    assert marker_extract.model() == "haiku"
    monkeypatch.setenv(marker_extract.ENV_MODEL, "sonnet")
    assert marker_extract.model() == "sonnet"


# --- build_extraction: the shared call-site guard ------------------------------


def _spy_runner(calls):
    def run(argv, **kwargs):
        calls.append(argv)
        return marker_extract.RunResult(0, _reply("COMPLETED"), "")

    return run


def test_build_extraction_returns_none_and_never_runs_under_the_kill_switch(monkeypatch):
    monkeypatch.setenv(marker_extract.ENV_KILL_SWITCH, "0")
    calls: list = []
    assert marker_extract.build_extraction("x", kind="developer", runner=_spy_runner(calls)) is None
    assert calls == []


# --- (h) DEGRADE: claude absent routes to legacy AND is marked -----------------


def test_build_extraction_degrades_when_claude_is_absent(monkeypatch):
    monkeypatch.delenv(marker_extract.ENV_KILL_SWITCH, raising=False)
    monkeypatch.setattr(marker_extract.shutil, "which", lambda name: None)
    calls: list = []
    result = marker_extract.build_extraction("x", kind="developer", runner=_spy_runner(calls))
    assert result is not None
    assert result.marker is None
    assert result.degraded is True
    assert "PATH" in result.reason
    assert calls == []


def test_build_extraction_runs_the_pass_when_reachable(monkeypatch):
    monkeypatch.delenv(marker_extract.ENV_KILL_SWITCH, raising=False)
    monkeypatch.setattr(marker_extract.shutil, "which", lambda name: "/usr/bin/claude")
    calls: list = []
    result = marker_extract.build_extraction("x", kind="developer", runner=_spy_runner(calls))
    assert result is not None and result.marker == "COMPLETED"
    assert result.degraded is False
    assert len(calls) == 1


def test_build_extraction_hints_from_the_kind_without_narrowing_acceptance(monkeypatch):
    monkeypatch.delenv(marker_extract.ENV_KILL_SWITCH, raising=False)
    monkeypatch.setattr(marker_extract.shutil, "which", lambda name: "/usr/bin/claude")
    seen: list = []

    def run(argv, **kwargs):
        seen.append(argv[-1])
        return marker_extract.RunResult(0, _reply("REVIEW"), "")

    result = marker_extract.build_extraction("x", kind="code-reviewer", runner=run)
    assert "usual choices for this role" in seen[0]
    # REVIEW is not among code-reviewer's hinted markers, yet it is accepted.
    assert result is not None and result.marker == "REVIEW"


# --- (i) PROMPT BOUNDING ------------------------------------------------------


def test_prompt_is_bounded_and_keeps_both_head_and_tail():
    body = "HEAD-SENTINEL" + ("x" * 100_000) + "TAIL-SENTINEL"
    prompt = marker_extract.build_prompt(body, RETURN_MARKERS)
    assert len(prompt) < 13_000, len(prompt)
    # Head AND tail: the protocol tolerates the marker at the top as well as at
    # the bottom, so a tail-only window would systematically miss one of them.
    assert "HEAD-SENTINEL" in prompt
    assert "TAIL-SENTINEL" in prompt
    assert "characters elided" in prompt


def test_short_text_is_passed_through_whole():
    prompt = marker_extract.build_prompt("short body", RETURN_MARKERS)
    assert "characters elided" not in prompt
