"""Tests for hook-prompt-reminders.py — the merged UserPromptSubmit reminders.

One hook now carries what eight hooks said separately: context-growth,
language, self-improvement, tracker (+ mount-check), ticket-plan-sync,
tracker-publish, experience-record and instructions-refresh. Each reminder is
tested at its `check_*` function; `main()` is exercised end to end in a
subprocess (stdout contract, judge-child silence, garbage stdin).

Hermetic: HOME, the config root, the Core repo and the settings path are all
pinned under tmp_path; every "upstream" is a local file:// git remote. Cases
carried over from the retired per-reminder test files are marked
`<old file>::<old test>` so none is lost silently; cases dropped with their hook
are listed in DROPPED_CASES with the reason.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = SCRIPTS_DIR / "hook-prompt-reminders.py"

_SPEC = importlib.util.spec_from_file_location("hook_prompt_reminders", HOOK_SCRIPT)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}

DROPPED_CASES = {
    "test_hook_effort_divergence_watch.py::test_a_scale_past_its_threshold_speaks": "the per-prompt effort watch is deleted with no replacement; no code under test remains",
    "test_hook_effort_divergence_watch.py::test_the_same_band_is_said_only_once_per_session": "the per-prompt effort watch is deleted with no replacement; no code under test remains",
    "test_hook_effort_divergence_watch.py::test_a_scale_already_fired_and_awaiting_ack_names_fire_acknowledge": "the per-prompt effort watch is deleted with no replacement; no code under test remains",
    "test_hook_effort_divergence_watch.py::test_an_unarmed_session_is_silent": "the per-prompt effort watch is deleted with no replacement; no code under test remains",
    "test_hook_effort_divergence_watch.py::test_an_inactive_trigger_is_silent": "the per-prompt effort watch is deleted with no replacement; no code under test remains",
    "test_hook_effort_divergence_watch.py::test_no_scale_over_its_threshold_is_silent": "the per-prompt effort watch is deleted with no replacement; no code under test remains",
    "test_hook_effort_divergence_watch.py::test_no_scales_at_all_is_silent": "the per-prompt effort watch is deleted with no replacement; no code under test remains",
    "test_hook_effort_divergence_watch.py::test_a_growing_divergence_speaks_again": "the per-prompt effort watch is deleted with no replacement; no code under test remains",
    "test_hook_effort_divergence_watch.py::test_a_shrinking_or_equal_band_stays_silent": "the per-prompt effort watch is deleted with no replacement; no code under test remains",
    "test_hook_effort_divergence_watch.py::test_band_for_floors_at_one": "the per-prompt effort watch is deleted with no replacement; no code under test remains",
    "test_hook_effort_divergence_watch.py::test_each_scale_keeps_its_own_band": "the per-prompt effort watch is deleted with no replacement; no code under test remains",
    "test_hook_effort_divergence_watch.py::test_the_worst_scale_is_the_one_furthest_past_its_own_trigger": "the per-prompt effort watch is deleted with no replacement; no code under test remains",
    "test_hook_effort_divergence_watch.py::test_a_row_under_its_threshold_is_never_the_worst": "the per-prompt effort watch is deleted with no replacement; no code under test remains",
    "test_hook_effort_divergence_watch.py::test_unparseable_stdin_is_silent": "the per-prompt effort watch is deleted with no replacement; no code under test remains",
    "test_hook_effort_divergence_watch.py::test_a_payload_without_a_session_id_is_silent": "the per-prompt effort watch is deleted with no replacement; no code under test remains",
    "test_hook_effort_divergence_watch.py::test_a_non_dict_verdict_is_silent": "the per-prompt effort watch is deleted with no replacement; no code under test remains",
    "test_hook_effort_divergence_watch.py::test_an_exception_anywhere_is_still_exit_zero_and_silent": "the per-prompt effort watch is deleted with no replacement; no code under test remains",
    "test_hook_effort_divergence_watch.py::test_effort_check_swallows_a_broken_subprocess": "the per-prompt effort watch is deleted with no replacement; no code under test remains",
    "test_hook_effort_divergence_watch.py::test_effort_check_stdout_is_pure_json": "the per-prompt effort watch is deleted with no replacement; no code under test remains",
    "test_hook_effort_divergence_watch.py::test_it_speaks_on_a_divergence_the_accumulator_supplied": "the per-prompt effort watch is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_expand_braces_plain_passthrough": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_expand_braces_two_alternatives": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_expand_braces_three_alternatives": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_expand_braces_multiple_groups": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_glob_matches_plain_wildcard": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_glob_matches_brace_first_alternative": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_glob_matches_brace_second_alternative": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_glob_no_match_outside_brace_alternatives": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_load_registry_valid": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_load_registry_missing_file_returns_none": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_load_registry_malformed_json_returns_none": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_load_registry_non_list_concepts_returns_none": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_check_concepts_warns_when_doc_absent": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_check_concepts_silent_when_doc_in_changeset": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_check_concepts_brace_glob_matches_and_warns": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_check_concepts_no_glob_match_returns_empty": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_check_concepts_section_appears_in_warning": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_check_concepts_no_duplicate_warning_for_same_concept": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_main_concept_warns_when_doc_absent": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_main_concept_silent_when_doc_in_changeset": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_main_unmatched_file_falls_back_to_nearest_readme": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_main_missing_registry_falls_back_to_heuristic": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_main_malformed_registry_no_crash_exit_zero": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_main_effective_cwd_cd_redirect": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_main_effective_cwd_cd_redirect_non_git_vcs": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_main_effective_cwd_falls_back_without_redirect": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_main_dash_c_form_does_not_trigger_hook": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_core_watches_git_and_nothing_else": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_configured_verb_extends_the_watch": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_plugin_changeset_parses_root_then_paths": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_plugin_changeset_absent_or_failing_yields_none": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_plugin_changeset_root_only_yields_empty_path_list": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_plugin_changeset_is_fail_open_on_a_broken_config_root": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
    "test_hook_readme_currency_reminder.py::test_plugin_changeset_is_run_in_the_effective_cwd": "the README-currency commit reminder is deleted with no replacement; no code under test remains",
}


@pytest.fixture(autouse=True)
def hermetic_env(tmp_path, monkeypatch):
    """Pin everything the reminders read from the machine to tmp_path."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "cfg"))
    monkeypatch.setenv("CLAUDE_INSTRUCTIONS_REPO", str(tmp_path / "no-core-repo"))
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(tmp_path / "no-settings.json"))
    monkeypatch.setenv("CLAUDE_CANON_ROOTS_FILE", str(tmp_path / "no-canon-roots"))
    for var in (mod.JUDGE_CHILD_ENV_VAR, "CLAUDE_TASK_MOUNT_ROOT", "CC_CONTEXT_NUDGE_BANDS"):
        monkeypatch.delenv(var, raising=False)
    return home


def turn(**payload) -> "mod.Turn":
    return mod.Turn(payload)


def write_state(session_id: str, state: dict) -> None:
    state_dir = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "agentctl" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / f"{session_id}.json").write_text(json.dumps(state), encoding="utf-8")


def run_main(stdin: bytes, env: dict | None = None, cwd: Path | None = None):
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=stdin,
        capture_output=True,
        env={**os.environ, **(env or {})},
        cwd=str(cwd) if cwd else None,
    )


def run_main_payload(payload: dict, tmp_path: Path, env: dict | None = None):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    payload = {"cwd": str(work), **payload}
    proc = run_main(json.dumps(payload).encode(), env=env, cwd=work)
    return proc.returncode, proc.stdout.decode()


# ===========================================================================
# language
# ===========================================================================

# ports test_hook_language_reminder.py::test_detect_russian_prompt
def test_detect_russian_prompt():
    assert mod.is_cyrillic_prompt("Давай починим баг в движке")


# ports test_hook_language_reminder.py::test_english_prompt_not_detected
def test_english_prompt_not_detected():
    assert not mod.is_cyrillic_prompt("Please fix the bug in the engine")


# ports test_hook_language_reminder.py::test_mostly_english_with_one_cyrillic_word_not_detected
def test_mostly_english_with_one_cyrillic_word_not_detected():
    assert not mod.is_cyrillic_prompt(
        "Run the PROJ-430 pipeline and report the resulting metrics right now да")


# ports test_hook_language_reminder.py::test_ticket_key_alone_not_detected
def test_ticket_key_alone_not_detected():
    assert not mod.is_cyrillic_prompt("look at ABC-123 and PROJ-9")


# ports test_hook_language_reminder.py::test_fires_and_is_advisory
def test_fires_and_is_advisory(tmp_path):
    rc, out = run_main_payload({"prompt": "Сделай ревью этого кода, пожалуйста"}, tmp_path)
    assert rc == 0
    assert "language-reminder" in out
    assert "Russian" in out


# ports test_hook_language_reminder.py::test_silent_on_english
def test_silent_on_english(tmp_path):
    rc, out = run_main_payload({"prompt": "Review this code please"}, tmp_path)
    assert rc == 0
    assert out == ""


# ports test_hook_language_reminder.py::test_silent_on_empty
def test_silent_on_empty(tmp_path):
    rc, out = run_main_payload({"prompt": "   "}, tmp_path)
    assert rc == 0
    assert out == ""


def test_language_reminder_speaks_once_per_session():
    prompt = "Сделай ревью этого кода, пожалуйста"
    first = mod.check_language(turn(prompt=prompt, session_id="s-1"))
    assert len(first) == 1 and first[0].startswith("[language-reminder]")
    assert mod.check_language(turn(prompt=prompt, session_id="s-1")) == []


def test_language_reminder_speaks_again_in_a_new_session():
    prompt = "Сделай ревью этого кода, пожалуйста"
    assert mod.check_language(turn(prompt=prompt, session_id="s-1"))
    assert mod.check_language(turn(prompt=prompt, session_id="s-2"))


def test_language_reminder_stamp_lives_under_the_state_dir(hermetic_env):
    mod.check_language(turn(prompt="Сделай ревью этого кода, пожалуйста", session_id="s-1"))
    stamp = hermetic_env / ".local" / "state" / "claude-language-reminder" / "s-1.stamp"
    assert stamp.exists()


def test_language_reminder_without_session_id_is_throttled_per_day():
    prompt = "Сделай ревью этого кода, пожалуйста"
    assert mod.check_language(turn(prompt=prompt))
    assert mod.check_language(turn(prompt=prompt)) == []


def test_language_reminder_survives_an_unwritable_state_dir(hermetic_env):
    (hermetic_env / ".local").write_text("not a directory", encoding="utf-8")
    assert mod.check_language(turn(prompt="Сделай ревью этого кода, пожалуйста", session_id="s-1"))


def test_language_reminder_does_not_stamp_an_english_prompt(hermetic_env):
    assert mod.check_language(turn(prompt="Review this code please", session_id="s-1")) == []
    assert not (hermetic_env / ".local" / "state" / "claude-language-reminder").exists()


# ===========================================================================
# self-improvement
# ===========================================================================

# ports test_hook_self_improvement_reminder.py::test_positive_signals_fire
@pytest.mark.parametrize(
    "prompt",
    [
        "did you run self-improvement?",
        "запусти self-improvement по этому",
        "the self improvement skill should have run",
        "selfimprovement was skipped",
        "don't do that again",
        "stop doing the extra commits",
        "перестань так делать",
        "не делай так больше",
        "я же просил не коммитить",
        "я же говорил про язык",
        "you shouldn't have pushed",
        "why did you skip the tests",
        "next time ask me first, you keep forgetting",
        "ты сделал не так",
        "почему ты не спросил",
        "в следующий раз ты должен спросить",
    ],
)
def test_positive_signals_fire(prompt):
    lines = mod.check_self_improvement(turn(prompt=prompt))
    assert len(lines) == 1, f"expected a signal for: {prompt!r}"
    assert lines[0].startswith("[self-improvement-reminder]")


# ports test_hook_self_improvement_reminder.py::test_negative_prompts_silent
@pytest.mark.parametrize(
    "prompt",
    [
        "add a function to parse the config file",
        "always validate input before writing",  # bare 'always', no agent-ref
        "never commit secrets to the repo",       # bare 'never'
        "всегда валидируй ввод перед записью",     # bare 'всегда'
        "refactor this module instead of rewriting it",  # 'instead of' but no agent-ref
        "you should review the README too",        # agent-ref but no corrective pattern
        "run the tests and report the result",
        "",
    ],
)
def test_negative_prompts_silent(prompt):
    assert mod.check_self_improvement(turn(prompt=prompt)) == [], f"unexpected signal for: {prompt!r}"


# ports test_hook_self_improvement_reminder.py::test_tier2b_needs_agent_ref
def test_tier2b_needs_agent_ref():
    assert mod.check_self_improvement(turn(prompt="use a map instead of a loop")) == []
    assert mod.check_self_improvement(turn(prompt="you used a loop instead of a map"))


# ports test_hook_self_improvement_reminder.py::test_main_emits_one_line_on_signal
def test_main_emits_one_line_on_signal(tmp_path):
    rc, out = run_main_payload({"prompt": "did you run self-improvement?"}, tmp_path)
    assert rc == 0
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert lines[0].startswith("[self-improvement-reminder]")


# ports test_hook_self_improvement_reminder.py::test_main_silent_inside_a_judge_child
def test_main_silent_inside_a_judge_child(tmp_path):
    """A judge subprocess's own prompt carries feedback-shaped example text, so
    the detector fires on it — which is how a judge came to call itself 126
    levels deep. The marker must win over a prompt that would otherwise fire,
    and silences every reminder, not only this one."""
    rc, out = run_main_payload(
        {"prompt": "did you run self-improvement? ticket ABC-123, Сделай ревью этого кода"},
        tmp_path, env={mod.JUDGE_CHILD_ENV_VAR: "1"})
    assert rc == 0
    assert out.strip() == ""


# ports test_hook_self_improvement_reminder.py::test_main_silent_on_neutral
def test_main_silent_on_neutral(tmp_path):
    rc, out = run_main_payload({"prompt": "add a parser for the config"}, tmp_path)
    assert rc == 0
    assert out.strip() == ""


# ports test_hook_self_improvement_reminder.py::test_main_malformed_stdin_exit_0
def test_main_malformed_stdin_exit_0():
    p = run_main(b"not json at all")
    assert p.returncode == 0
    assert p.stdout.decode().strip() == ""


# ports test_hook_self_improvement_reminder.py::test_main_empty_stdin_exit_0
def test_main_empty_stdin_exit_0():
    p = run_main(b"")
    assert p.returncode == 0
    assert p.stdout.decode().strip() == ""


# ports test_hook_self_improvement_reminder.py::test_main_empty_prompt_exit_0
def test_main_empty_prompt_exit_0(tmp_path):
    rc, out = run_main_payload({"prompt": "   "}, tmp_path)
    assert rc == 0
    assert out.strip() == ""


def test_main_non_object_payload_is_silent():
    p = run_main(b'["did you run self-improvement?"]')
    assert p.returncode == 0
    assert p.stdout.decode().strip() == ""


# ===========================================================================
# tracker (+ mount-check)
# ===========================================================================

def _make_root(tmp: Path, *mount_names: str) -> Path:
    root = tmp / "task-mounts"
    for name in mount_names:
        (root / name).mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    return root


# ports test_hook_tracker_reminder_mount.py::test_fires_on_wrong_mount
def test_fires_on_wrong_mount(tmp_path, monkeypatch):
    root = _make_root(tmp_path, "main", "PROJ-440-foo")
    monkeypatch.setenv("CLAUDE_TASK_MOUNT_ROOT", str(root))
    out = mod.mount_mismatches(["PROJ-440"], str(root / "main"))
    assert out == [("PROJ-440", "main", ["PROJ-440-foo"])], out


# ports test_hook_tracker_reminder_mount.py::test_lists_multiple_matching_mounts
def test_lists_multiple_matching_mounts(tmp_path, monkeypatch):
    root = _make_root(tmp_path, "main", "PROJ-440-a", "PROJ-440-b")
    monkeypatch.setenv("CLAUDE_TASK_MOUNT_ROOT", str(root))
    out = mod.mount_mismatches(["PROJ-440"], str(root / "main" / "sub"))
    assert out == [("PROJ-440", "main", ["PROJ-440-a", "PROJ-440-b"])], out


# ports test_hook_tracker_reminder_mount.py::test_silent_when_already_in_ticket_mount
def test_silent_when_already_in_ticket_mount(tmp_path, monkeypatch):
    root = _make_root(tmp_path, "PROJ-440-foo")
    monkeypatch.setenv("CLAUDE_TASK_MOUNT_ROOT", str(root))
    assert mod.mount_mismatches(["PROJ-440"], str(root / "PROJ-440-foo" / "robot")) == []


# ports test_hook_tracker_reminder_mount.py::test_silent_when_no_matching_mount
def test_silent_when_no_matching_mount(tmp_path, monkeypatch):
    root = _make_root(tmp_path, "main", "OTHER-1-x")
    monkeypatch.setenv("CLAUDE_TASK_MOUNT_ROOT", str(root))
    assert mod.mount_mismatches(["PROJ-440"], str(root / "main")) == []


# ports test_hook_tracker_reminder_mount.py::test_silent_when_cwd_outside_root
def test_silent_when_cwd_outside_root(tmp_path, monkeypatch):
    root = _make_root(tmp_path, "main", "PROJ-440-foo")
    monkeypatch.setenv("CLAUDE_TASK_MOUNT_ROOT", str(root))
    assert mod.mount_mismatches(["PROJ-440"], str(tmp_path / "elsewhere" / "repo")) == []


# ports test_hook_tracker_reminder_mount.py::test_noop_when_no_root
def test_noop_when_no_root(tmp_path, monkeypatch):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("CLAUDE_TASK_MOUNT_ROOT", str(missing))
    assert mod.mount_mismatches(["PROJ-440"], str(missing / "main")) == []


# ports test_hook_tracker_reminder_mount.py::test_end_to_end_stdout
def test_end_to_end_stdout(tmp_path):
    """Full hook run: tracker-reminder line always, mount-check line on mismatch."""
    root = _make_root(tmp_path, "main", "PROJ-440-foo")
    payload = {"prompt": "continue PROJ-440 please", "cwd": str(root / "main")}
    res = run_main(json.dumps(payload).encode(), env={"CLAUDE_TASK_MOUNT_ROOT": str(root)})
    out = res.stdout.decode()
    assert res.returncode == 0, res.returncode
    assert "[tracker-reminder]" in out, out
    assert "[mount-check]" in out, out
    assert "PROJ-440-foo" in out, out


def test_tracker_reminder_names_the_signals():
    lines = mod.check_tracker(turn(prompt="please update ABC-123 on the tracker"))
    assert len(lines) == 1
    assert lines[0].startswith("[tracker-reminder]")
    assert "ticket key(s): ABC-123" in lines[0]
    assert "keyword(s): tracker" in lines[0]
    assert "tracker-management" in lines[0]


def test_tracker_reminder_fires_on_a_keyword_alone():
    assert mod.check_tracker(turn(prompt="создай тикет для этого"))


def test_tracker_reminder_silent_on_a_neutral_prompt():
    assert mod.check_tracker(turn(prompt="add a parser for the config")) == []


@pytest.mark.parametrize("prompt", ["ABC-123", "fix PROJ-9 today", "see SRE-4512 and DATA-1"])
def test_ticket_key_shapes_speak(prompt):
    assert mod.find_ticket_keys(prompt)


@pytest.mark.parametrize(
    "prompt",
    [
        "GPT-4 is a model",
        "compare GPT-5 and LLAMA-3",
        "the X86-64 ABI",
        "register Z0-9",
        "UTF-8 and ISO-8601 and RFC-5322 and SHA-256",
        "COVID-19",
    ],
)
def test_model_names_and_standards_are_not_ticket_keys(prompt):
    assert mod.find_ticket_keys(prompt) == []
    assert mod.check_tracker(turn(prompt=prompt)) == []


def test_a_model_name_does_not_hide_a_real_key():
    assert mod.find_ticket_keys("GPT-4 vs ABC-123") == ["ABC-123"]


def test_tracker_mount_check_is_a_noop_without_a_ticket_key(tmp_path, monkeypatch):
    root = _make_root(tmp_path, "main", "PROJ-440-foo")
    monkeypatch.setenv("CLAUDE_TASK_MOUNT_ROOT", str(root))
    lines = mod.check_tracker(turn(prompt="open a ticket please", cwd=str(root / "main")))
    assert [ln.split("]")[0] for ln in lines] == ["[tracker-reminder"]


# ===========================================================================
# ticket-plan-sync
# ===========================================================================

def test_ticket_plan_sync_fires_on_a_tracker_prompt_with_an_attached_plan():
    write_state("s-1", {"plan_path": "/plans/x.toml"})
    lines = mod.check_ticket_plan_sync(turn(prompt="continue ABC-123", session_id="s-1"))
    assert len(lines) == 1
    assert lines[0].startswith("[ticket-plan-sync]")
    assert "/plans/x.toml" in lines[0]
    assert "verify-ticket-plan-sync.py --plan /plans/x.toml" in lines[0]


def test_ticket_plan_sync_fires_on_a_keyword_too():
    write_state("s-1", {"plan_path": "/plans/x.toml"})
    assert mod.check_ticket_plan_sync(turn(prompt="update the ticket", session_id="s-1"))


def test_ticket_plan_sync_silent_without_a_tracker_mention():
    write_state("s-1", {"plan_path": "/plans/x.toml"})
    assert mod.check_ticket_plan_sync(turn(prompt="add a parser", session_id="s-1")) == []


@pytest.mark.parametrize("state", [{}, {"plan_path": ""}, {"plan_path": None}, {"plan_path": 3}])
def test_ticket_plan_sync_silent_without_a_plan_path(state):
    write_state("s-1", state)
    assert mod.check_ticket_plan_sync(turn(prompt="continue ABC-123", session_id="s-1")) == []


def test_ticket_plan_sync_silent_without_state_or_session():
    assert mod.check_ticket_plan_sync(turn(prompt="continue ABC-123", session_id="s-none")) == []
    assert mod.check_ticket_plan_sync(turn(prompt="continue ABC-123")) == []


def test_ticket_plan_sync_silent_on_corrupt_state():
    state_dir = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "agentctl" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "s-1.json").write_text("{not json", encoding="utf-8")
    assert mod.check_ticket_plan_sync(turn(prompt="continue ABC-123", session_id="s-1")) == []


# ===========================================================================
# tracker-publish
# ===========================================================================

# ports test_tracker_plugin.py::test_hook_lists_unpublished_phases_when_tracker_active
def test_unpublished_phases_lists_the_missing_ones():
    state = {"plugins": {"tracker": {"published_phases": {"plan": {}}}}}
    assert mod.unpublished_phases(state) == ["result", "status"]


def test_unpublished_phases_empty_when_the_plugin_is_inactive():
    assert mod.unpublished_phases({"plugins": {}}) == []
    assert mod.unpublished_phases({}) == []


def test_unpublished_phases_empty_when_all_are_published():
    full = {"plugins": {"tracker": {"published_phases": {"plan": {}, "result": {}, "status": {}}}}}
    assert mod.unpublished_phases(full) == []


def test_tracker_publish_nudges_outside_the_gate():
    write_state("s-1", {"node": "EXECUTING", "plugins": {"tracker": {"published_phases": {"plan": {}}}}})
    lines = mod.check_tracker_publish(turn(prompt="x", session_id="s-1"))
    assert len(lines) == 1
    assert lines[0].startswith("[tracker-publish-reminder] The tracker plugin is active")
    assert "result, status" in lines[0]
    assert "BLOCKED" not in lines[0]


def test_tracker_publish_is_loudest_at_the_resolution_gate():
    write_state("s-1", {"node": "RESOLUTION", "plugins": {"tracker": {"published_phases": {}}}})
    lines = mod.check_tracker_publish(turn(prompt="x", session_id="s-1"))
    assert len(lines) == 1
    assert "resolution gate" in lines[0]
    assert "plan, result, status" in lines[0]
    assert "BLOCKED" in lines[0]


def test_tracker_publish_silent_when_complete_inactive_or_stateless():
    write_state("s-done", {"plugins": {"tracker": {"published_phases": {"plan": {}, "result": {}, "status": {}}}}})
    write_state("s-off", {"node": "RESOLUTION", "plugins": {}})
    assert mod.check_tracker_publish(turn(prompt="x", session_id="s-done")) == []
    assert mod.check_tracker_publish(turn(prompt="x", session_id="s-off")) == []
    assert mod.check_tracker_publish(turn(prompt="x", session_id="s-none")) == []
    assert mod.check_tracker_publish(turn(prompt="x")) == []


# ===========================================================================
# experience-record
# ===========================================================================

def test_experience_record_nudges_outside_the_gate():
    write_state("s-1", {"node": "EXECUTING", "plugins": {"experience": {}}})
    lines = mod.check_experience_record(turn(prompt="x", session_id="s-1"))
    assert len(lines) == 1
    assert lines[0].startswith("[experience-record-reminder] The experience leaf flow has")
    assert "searched, recorded|skipped" in lines[0]
    assert "BLOCKED" not in lines[0]


def test_experience_record_is_loudest_at_the_resolution_gate():
    write_state("s-1", {"node": "RESOLUTION", "plugins": {"experience": {"searched": True}}})
    lines = mod.check_experience_record(turn(prompt="x", session_id="s-1"))
    assert len(lines) == 1
    assert "resolution gate" in lines[0]
    assert "missing phase(s): recorded|skipped" in lines[0]
    assert "BLOCKED" in lines[0]


@pytest.mark.parametrize("bag", [
    {"searched": True, "recorded": True},
    {"searched": True, "skipped": True},
])
def test_experience_record_silent_when_the_flow_is_complete(bag):
    write_state("s-1", {"node": "RESOLUTION", "plugins": {"experience": bag}})
    assert mod.check_experience_record(turn(prompt="x", session_id="s-1")) == []


def test_experience_record_silent_when_inactive_or_stateless():
    write_state("s-1", {"node": "RESOLUTION", "plugins": {}})
    assert mod.check_experience_record(turn(prompt="x", session_id="s-1")) == []
    assert mod.check_experience_record(turn(prompt="x", session_id="s-none")) == []
    assert mod.check_experience_record(turn(prompt="x")) == []


def test_plugin_reminders_arrive_through_main(tmp_path):
    write_state("s-1", {
        "node": "RESOLUTION",
        "plugins": {"tracker": {"published_phases": {}}, "experience": {}},
    })
    rc, out = run_main_payload({"prompt": "go", "session_id": "s-1"}, tmp_path)
    assert rc == 0
    assert "[tracker-publish-reminder]" in out
    assert "[experience-record-reminder]" in out


# ===========================================================================
# context-growth
# ===========================================================================

def _transcript(tmp_path, context_tokens):
    path = tmp_path / "transcript.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for tokens in context_tokens:
            fh.write(json.dumps({
                "message": {"usage": {
                    "input_tokens": tokens,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                }},
            }) + "\n")
    return path


def _growth(monkeypatch, transcript, session_id, state_root):
    monkeypatch.setattr(mod, "STATE_ROOT", str(state_root))
    return mod.check_context_growth(
        turn(transcript_path=str(transcript), session_id=session_id))


# ports test_context_growth_reminder.py::test_a_crossed_band_nudges_once_and_then_stays_quiet
def test_a_crossed_band_nudges_once_and_then_stays_quiet(tmp_path, monkeypatch):
    transcript = _transcript(tmp_path, [130_000])
    session = str(uuid.uuid4())
    state = tmp_path / "state"

    first = _growth(monkeypatch, transcript, session, state)
    assert len(first) == 1 and "context-growth" in first[0]
    assert _growth(monkeypatch, transcript, session, state) == []


# ports test_context_growth_reminder.py::test_a_higher_band_speaks_again_in_the_same_session
def test_a_higher_band_speaks_again_in_the_same_session(tmp_path, monkeypatch):
    session = str(uuid.uuid4())
    state = tmp_path / "state"

    _growth(monkeypatch, _transcript(tmp_path, [130_000]), session, state)
    out = _growth(monkeypatch, _transcript(tmp_path, [300_000]), session, state)
    assert "very large" in out[0]


# ports test_context_growth_reminder.py::test_the_stamp_keeps_its_historical_tmp_path
def test_the_stamp_keeps_its_historical_tmp_path(tmp_path, monkeypatch):
    """The merge must not relocate a path other sessions already own."""
    assert mod.STATE_ROOT == "/tmp"
    assert mod.STATE_PREFIX == "cc-context-nudge-"

    session = str(uuid.uuid4())
    state = tmp_path / "state"
    _growth(monkeypatch, _transcript(tmp_path, [130_000]), session, state)
    assert (state / f"cc-context-nudge-{session}").read_text().strip() == "1"


# ports test_context_growth_reminder.py::test_a_short_session_says_nothing
def test_a_short_session_says_nothing(tmp_path, monkeypatch):
    out = _growth(monkeypatch, _transcript(tmp_path, [4_000]), str(uuid.uuid4()), tmp_path / "state")
    assert out == []


# ports test_context_growth_reminder.py::test_an_unwritable_state_root_still_nudges
def test_an_unwritable_state_root_still_nudges(tmp_path, monkeypatch):
    """Fail-open: losing the stamp costs a repeated nudge, never an error."""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    out = _growth(monkeypatch, _transcript(tmp_path, [130_000]), str(uuid.uuid4()), blocked)
    assert len(out) == 1 and "context-growth" in out[0]


def test_context_growth_without_a_transcript_says_nothing(tmp_path, monkeypatch):
    assert _growth(monkeypatch, tmp_path / "missing.jsonl", str(uuid.uuid4()), tmp_path / "state") == []
    assert mod.check_context_growth(turn(session_id="s-1")) == []


def test_context_growth_bands_are_overridable(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_CONTEXT_NUDGE_BANDS", "1000,2000")
    out = _growth(monkeypatch, _transcript(tmp_path, [1_500]), str(uuid.uuid4()), tmp_path / "state")
    assert len(out) == 1 and "very large" not in out[0]


# ===========================================================================
# main(): composition
# ===========================================================================

def test_main_runs_every_check_and_prints_each_line(monkeypatch, capsys):
    monkeypatch.setattr(mod, "CHECKS", (lambda t: ["first", "second"], lambda t: [], lambda t: ["third"]))
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps({"prompt": "x"})))
    assert mod.main() == 0
    assert capsys.readouterr().out.splitlines() == ["first", "second", "third"]


def test_one_failing_check_does_not_silence_the_others(monkeypatch, capsys):
    def boom(t):
        raise RuntimeError("broken reminder")

    monkeypatch.setattr(mod, "CHECKS", (boom, lambda t: ["still here"]))
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps({"prompt": "x"})))
    assert mod.main() == 0
    assert capsys.readouterr().out.splitlines() == ["still here"]


def test_the_registered_checks_are_the_eight_reminders():
    assert [c.__name__ for c in mod.CHECKS] == [
        "check_context_growth",
        "check_language",
        "check_self_improvement",
        "check_tracker",
        "check_ticket_plan_sync",
        "check_tracker_publish",
        "check_experience_record",
        "check_instructions_refresh",
    ]


def test_several_reminders_speak_in_one_turn(tmp_path):
    rc, out = run_main_payload(
        {"prompt": "ты сделал не так, обнови ABC-123"}, tmp_path)
    assert rc == 0
    tags = [ln.split("]")[0] for ln in out.splitlines()]
    assert tags == ["[language-reminder", "[self-improvement-reminder", "[tracker-reminder"]


# ===========================================================================
# instructions-refresh
# ===========================================================================

def git(*args, cwd, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env={**os.environ, **GIT_ENV},
        check=check,
        capture_output=True,
        text=True,
    )


def make_bare_and_clone(tmp_path: Path, name: str, with_claude_dir: bool = False):
    """Bare '<name>-origin' repo seeded with one commit on main, plus a clone of it."""
    origin = tmp_path / f"{name}-origin.git"
    git("init", "--quiet", "--bare", "-b", "main", str(origin), cwd=tmp_path)

    seed = tmp_path / f"{name}-seed"
    git("clone", "--quiet", str(origin), str(seed), cwd=tmp_path)
    (seed / "README.md").write_text("seed\n")
    git("add", "README.md", cwd=seed)
    if with_claude_dir:
        claude_dir = seed / ".claude"
        claude_dir.mkdir()
        (claude_dir / "marker.md").write_text("layer\n")
        git("add", ".claude", cwd=seed)
    git("commit", "--quiet", "-m", "seed: initial content", cwd=seed)
    git("push", "--quiet", "origin", "main", cwd=seed)

    clone = tmp_path / f"{name}-clone"
    git("clone", "--quiet", "-b", "main", str(origin), str(clone), cwd=tmp_path)
    return origin, clone


def advance_remote(tmp_path: Path, origin: Path, name: str):
    """Push one unrelated commit to origin/main via a throwaway clone, leaving
    any pre-existing clone of `origin` one commit behind."""
    other = tmp_path / f"{name}-advance"
    git("clone", "--quiet", "-b", "main", str(origin), str(other), cwd=tmp_path)
    (other / "ADVANCE.md").write_text("advance\n")
    git("add", "ADVANCE.md", cwd=other)
    git("commit", "--quiet", "-m", f"advance {name}", cwd=other)
    git("push", "--quiet", "origin", "main", cwd=other)


def _default_settings_path(home: Path, core_repo) -> Path:
    """A canonical-and-clean default settings.json — every hook command's root
    is `core_repo` itself, i.e. the canonical checkout under test — so tests
    that are not about the canonical-root axis stay silent on it regardless."""
    settings = home / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [
                    {"command": f"{core_repo}/scripts/hook-example.py", "type": "command"},
                ]}
            ]
        }
    }))
    return settings


def run_hook(home: Path, core_repo, cwd: Path, settings_path: Path | None = None):
    if settings_path is None:
        settings_path = _default_settings_path(home, core_repo)
    env = {
        **os.environ,
        **GIT_ENV,
        "HOME": str(home),
        # Pin agent_home() to the fake HOME too: an inherited CLAUDE_CONFIG_DIR
        # would otherwise point config_root.agent_home() (and the canon-roots
        # file derived from it) at the machine's real config root.
        "CLAUDE_CONFIG_DIR": str(home),
        "CLAUDE_INSTRUCTIONS_REPO": str(core_repo),
        # Always explicit: the fallback reads the machine's LIVE settings.json,
        # which would make pass/fail depend on whatever machine runs the test.
        "CLAUDE_SETTINGS_PATH": str(settings_path),
    }
    payload = json.dumps({"session_id": "s-1", "prompt": "hi", "cwd": str(cwd)})
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=payload,
        env=env,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def stamp_path(home: Path) -> Path:
    return home / ".local" / "state" / "claude-instructions-refresh.stamp"


# (a) forced-due + Core behind -> nudge naming Core + its pull command

# ports test_hook_instructions_refresh.py::test_core_behind_emits_nudge
def test_core_behind_emits_nudge(tmp_path):
    home = tmp_path / "home2"
    home.mkdir()
    origin, clone = make_bare_and_clone(tmp_path, "core")
    advance_remote(tmp_path, origin, "core")

    proc = run_hook(home, core_repo=clone, cwd=clone)

    assert proc.returncode == 0
    assert "[instructions-refresh]" in proc.stdout
    assert "Core" in proc.stdout
    assert "sync-instructions-repo.sh pull" in proc.stdout
    assert "1 commit(s) behind" in proc.stdout
    assert "AskUserQuestion" in proc.stdout


# (b) within throttle window (stamp = current session key) -> silent

# ports test_hook_instructions_refresh.py::test_within_throttle_window_silent
def test_within_throttle_window_silent(tmp_path):
    home = tmp_path / "home2"
    home.mkdir()
    origin, clone = make_bare_and_clone(tmp_path, "core")
    advance_remote(tmp_path, origin, "core")

    stamp = stamp_path(home)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    # run_hook posts session_id="s-1"; a stamp holding that key gates the run.
    stamp.write_text("s-1", encoding="utf-8")

    proc = run_hook(home, core_repo=clone, cwd=clone)

    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# (b2) stamp from a DIFFERENT session -> a new session re-fires the offer

# ports test_hook_instructions_refresh.py::test_new_session_id_refires
def test_new_session_id_refires(tmp_path):
    home = tmp_path / "home2"
    home.mkdir()
    origin, clone = make_bare_and_clone(tmp_path, "core")
    advance_remote(tmp_path, origin, "core")

    stamp = stamp_path(home)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text("s-old", encoding="utf-8")

    proc = run_hook(home, core_repo=clone, cwd=clone)

    assert proc.returncode == 0
    assert "[instructions-refresh]" in proc.stdout
    assert "Core" in proc.stdout
    # the new session's key is now recorded, gating its own later prompts
    assert stamp.read_text(encoding="utf-8").strip() == "s-1"


# (b3) no session_id in payload -> falls back to the per-calendar-day key

# ports test_hook_instructions_refresh.py::test_missing_session_id_falls_back_to_daily_key
def test_missing_session_id_falls_back_to_daily_key(tmp_path):
    import datetime as dt

    home = tmp_path / "home2"
    home.mkdir()
    origin, clone = make_bare_and_clone(tmp_path, "core")
    advance_remote(tmp_path, origin, "core")

    settings_path = _default_settings_path(home, clone)
    env = {
        **os.environ,
        **GIT_ENV,
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(home),
        "CLAUDE_INSTRUCTIONS_REPO": str(clone),
        "CLAUDE_SETTINGS_PATH": str(settings_path),
    }
    payload = json.dumps({"prompt": "hi", "cwd": str(clone)})  # no session_id

    def run_no_sid():
        return subprocess.run(
            [sys.executable, str(HOOK_SCRIPT)],
            input=payload, env=env, cwd=str(clone),
            capture_output=True, text=True,
        )

    first = run_no_sid()
    assert first.returncode == 0
    assert "[instructions-refresh]" in first.stdout
    assert stamp_path(home).read_text(encoding="utf-8").strip() == dt.date.today().isoformat()

    second = run_no_sid()
    assert second.returncode == 0
    assert second.stdout.strip() == ""


# (c) up-to-date (behind=0) -> silent

# ports test_hook_instructions_refresh.py::test_up_to_date_silent
def test_up_to_date_silent(tmp_path):
    home = tmp_path / "home2"
    home.mkdir()
    _origin, clone = make_bare_and_clone(tmp_path, "core")
    # A session cwd distinct from the Core repo itself — otherwise cwd would BE
    # the primary Core checkout and the relocate OFFER (an orthogonal axis, see
    # test_primary_core_checkout_emits_relocate_offer) would also fire.
    session_cwd = tmp_path / "session-cwd"
    session_cwd.mkdir()

    proc = run_hook(home, core_repo=clone, cwd=session_cwd)

    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# (d) fetch failure / non-git cwd -> silent, exit 0 (fail-open)

# ports test_hook_instructions_refresh.py::test_missing_core_repo_and_non_git_cwd_silent
def test_missing_core_repo_and_non_git_cwd_silent(tmp_path):
    home = tmp_path / "home2"
    home.mkdir()
    non_git_cwd = tmp_path / "not-a-repo"
    non_git_cwd.mkdir()
    missing_core = tmp_path / "does-not-exist"

    proc = run_hook(home, core_repo=missing_core, cwd=non_git_cwd)

    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# (e) project-layer repo behind while Core current -> nudge names project layer

# ports test_hook_instructions_refresh.py::test_project_layer_behind_emits_nudge
def test_project_layer_behind_emits_nudge(tmp_path):
    home = tmp_path / "home2"
    home.mkdir()
    _core_origin, core_clone = make_bare_and_clone(tmp_path, "core")

    proj_origin, proj_clone = make_bare_and_clone(tmp_path, "project", with_claude_dir=True)
    advance_remote(tmp_path, proj_origin, "project")

    proc = run_hook(home, core_repo=core_clone, cwd=proj_clone)

    assert proc.returncode == 0
    assert "Project" in proc.stdout
    assert "Core" not in proc.stdout
    assert f"git -C {proj_clone} pull --ff-only" in proc.stdout
    assert "1 commit(s) behind" in proc.stdout


# ports test_hook_instructions_refresh.py::test_project_layer_without_claude_dir_ignored
def test_project_layer_without_claude_dir_ignored(tmp_path):
    home = tmp_path / "home2"
    home.mkdir()
    _core_origin, core_clone = make_bare_and_clone(tmp_path, "core")

    proj_origin, proj_clone = make_bare_and_clone(tmp_path, "project", with_claude_dir=False)
    advance_remote(tmp_path, proj_origin, "project")

    proc = run_hook(home, core_repo=core_clone, cwd=proj_clone)

    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# (f) stamp is written on fire and gates a second same-session run

# ports test_hook_instructions_refresh.py::test_stamp_written_on_fire_gates_second_run
def test_stamp_written_on_fire_gates_second_run(tmp_path):
    home = tmp_path / "home2"
    home.mkdir()
    origin, clone = make_bare_and_clone(tmp_path, "core")
    advance_remote(tmp_path, origin, "core")

    stamp = stamp_path(home)
    assert not stamp.exists()

    first = run_hook(home, core_repo=clone, cwd=clone)
    assert first.returncode == 0
    assert "[instructions-refresh]" in first.stdout
    assert stamp.exists()

    second = run_hook(home, core_repo=clone, cwd=clone)
    assert second.returncode == 0
    assert second.stdout.strip() == ""


# (g) deploy-integrity: Core checkout off the default branch -> deploy warn

# ports test_hook_instructions_refresh.py::test_off_main_branch_emits_warn
def test_off_main_branch_emits_warn(tmp_path):
    home = tmp_path / "home2"
    home.mkdir()
    _origin, clone = make_bare_and_clone(tmp_path, "core")
    git("switch", "--quiet", "-c", "feat/x", cwd=clone)

    proc = run_hook(home, core_repo=clone, cwd=clone)

    assert proc.returncode == 0
    assert "[instructions-deploy]" in proc.stdout
    assert "not main" in proc.stdout
    assert "feat/x" in proc.stdout
    assert f"git -C {clone} switch main" in proc.stdout


# (h) deploy-integrity: any settings.json hook root other than the CANONICAL
#     Core checkout -> deploy warn. len(roots) > 1 is NOT the norm — a single
#     root that everyone agrees on is still a violation if it isn't canonical.

# ports test_hook_instructions_refresh.py::test_multi_non_canonical_roots_emits_warn
def test_multi_non_canonical_roots_emits_warn(tmp_path):
    home = tmp_path / "home2"
    home.mkdir()
    _origin, clone = make_bare_and_clone(tmp_path, "core")

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [
                    {"command": "/root-a/scripts/hook-one.py", "type": "command"},
                    {"command": "/root-b/scripts/hook-two.py", "type": "command"},
                ]},
            ]
        }
    }))

    proc = run_hook(home, core_repo=clone, cwd=clone, settings_path=settings)

    assert proc.returncode == 0
    assert "[instructions-deploy]" in proc.stdout
    assert "2 non-canonical checkout root(s)" in proc.stdout
    assert "/root-a" in proc.stdout
    assert "/root-b" in proc.stdout
    assert str(clone) in proc.stdout  # names the canonical root too


# ports test_hook_instructions_refresh.py::test_single_non_canonical_root_emits_warn
def test_single_non_canonical_root_emits_warn(tmp_path):
    """Every hook command agrees on ONE root, but it is a feature worktree, not canonical."""
    home = tmp_path / "home2"
    home.mkdir()
    _origin, clone = make_bare_and_clone(tmp_path, "core")
    session_cwd = tmp_path / "session-cwd"  # kept distinct from the Core repo — see test_up_to_date_silent
    session_cwd.mkdir()
    feature_worktree = tmp_path / "feature-worktree"

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [
                    {"command": f"{feature_worktree}/scripts/hook-one.py", "type": "command"},
                    {"command": f"{feature_worktree}/scripts/hook-two.py", "type": "command"},
                ]},
            ]
        }
    }))

    proc = run_hook(home, core_repo=clone, cwd=session_cwd, settings_path=settings)

    assert proc.returncode == 0
    assert "[instructions-deploy]" in proc.stdout
    assert "1 non-canonical checkout root(s)" in proc.stdout
    assert str(feature_worktree) in proc.stdout
    assert str(clone) in proc.stdout  # names the canonical root


# ports test_hook_instructions_refresh.py::test_mixed_canonical_and_non_canonical_emits_warn
def test_mixed_canonical_and_non_canonical_emits_warn(tmp_path):
    home = tmp_path / "home2"
    home.mkdir()
    _origin, clone = make_bare_and_clone(tmp_path, "core")
    session_cwd = tmp_path / "session-cwd"
    session_cwd.mkdir()
    feature_worktree = tmp_path / "feature-worktree"

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [
                    {"command": f"{clone}/scripts/hook-one.py", "type": "command"},
                    {"command": f"{feature_worktree}/scripts/hook-two.py", "type": "command"},
                ]},
            ]
        }
    }))

    proc = run_hook(home, core_repo=clone, cwd=session_cwd, settings_path=settings)

    assert proc.returncode == 0
    assert "[instructions-deploy]" in proc.stdout
    assert "1 non-canonical checkout root(s)" in proc.stdout
    assert str(feature_worktree) in proc.stdout


# (i) deploy-integrity: every hook root IS the canonical checkout -> silent

# ports test_hook_instructions_refresh.py::test_all_canonical_settings_silent
def test_all_canonical_settings_silent(tmp_path):
    home = tmp_path / "home2"
    home.mkdir()
    _origin, clone = make_bare_and_clone(tmp_path, "core")
    session_cwd = tmp_path / "session-cwd"
    session_cwd.mkdir()

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [
                    {"command": f"{clone}/scripts/hook-one.py", "type": "command"},
                    {"command": f"{clone}/scripts/hook-two.py", "type": "command"},
                ]},
            ]
        }
    }))

    proc = run_hook(home, core_repo=clone, cwd=session_cwd, settings_path=settings)

    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# ports test_hook_instructions_refresh.py::test_symlink_and_trailing_slash_root_silent
def test_symlink_and_trailing_slash_root_silent(tmp_path):
    """A root that differs from the canonical one only by a symlink or a
    trailing slash must resolve equal, not warn."""
    home = tmp_path / "home2"
    home.mkdir()
    _origin, clone = make_bare_and_clone(tmp_path, "core")
    session_cwd = tmp_path / "session-cwd"
    session_cwd.mkdir()
    symlinked_clone = tmp_path / "core-symlink"
    symlinked_clone.symlink_to(clone, target_is_directory=True)

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [
                    {"command": f"{symlinked_clone}/scripts/hook-one.py", "type": "command"},
                    # double slash before the marker -> captured root keeps a
                    # trailing slash; must still resolve equal to `clone`.
                    {"command": f"{clone}//scripts/hook-two.py", "type": "command"},
                ]},
            ]
        }
    }))

    proc = run_hook(home, core_repo=clone, cwd=session_cwd, settings_path=settings)

    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# ports test_hook_instructions_refresh.py::test_no_hook_commands_silent
def test_no_hook_commands_silent(tmp_path):
    home = tmp_path / "home2"
    home.mkdir()
    _origin, clone = make_bare_and_clone(tmp_path, "core")
    session_cwd = tmp_path / "session-cwd"
    session_cwd.mkdir()

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {}}))

    proc = run_hook(home, core_repo=clone, cwd=session_cwd, settings_path=settings)

    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# (j) deploy-integrity: malformed settings.json -> fail-open silence

# ports test_hook_instructions_refresh.py::test_malformed_settings_fails_open
def test_malformed_settings_fails_open(tmp_path):
    home = tmp_path / "home2"
    home.mkdir()
    _origin, clone = make_bare_and_clone(tmp_path, "core")
    session_cwd = tmp_path / "session-cwd"
    session_cwd.mkdir()

    settings = tmp_path / "settings.json"
    settings.write_text("{not valid json")

    proc = run_hook(home, core_repo=clone, cwd=session_cwd, settings_path=settings)

    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# (k) worktree-fresh: a linked worktree behind origin/main -> rebase OFFER,
#     with the behind-count read only AFTER a fetch

# ports test_hook_instructions_refresh.py::test_linked_worktree_behind_emits_rebase_offer
def test_linked_worktree_behind_emits_rebase_offer(tmp_path):
    home = tmp_path / "home2"
    home.mkdir()
    origin, clone = make_bare_and_clone(tmp_path, "wt")
    worktree = tmp_path / "wt-linked"
    git("worktree", "add", "--quiet", "-b", "wt-branch", str(worktree), "main", cwd=clone)
    # Advances origin/main AFTER the worktree's own origin/main ref was last
    # updated — the OFFER only appears if check_worktree_fresh fetches before
    # counting; a stale-ref count would read 0-behind forever.
    advance_remote(tmp_path, origin, "wt")

    missing_core = tmp_path / "does-not-exist"
    proc = run_hook(home, core_repo=missing_core, cwd=worktree)

    assert proc.returncode == 0
    assert "[worktree-fresh]" in proc.stdout
    assert str(worktree) in proc.stdout
    assert "1 commit(s) behind" in proc.stdout
    assert f"git -C {worktree} rebase origin/main" in proc.stdout


# ports test_hook_instructions_refresh.py::test_linked_worktree_up_to_date_silent
def test_linked_worktree_up_to_date_silent(tmp_path):
    home = tmp_path / "home2"
    home.mkdir()
    _origin, clone = make_bare_and_clone(tmp_path, "wt")
    worktree = tmp_path / "wt-linked"
    git("worktree", "add", "--quiet", "-b", "wt-branch", str(worktree), "main", cwd=clone)

    missing_core = tmp_path / "does-not-exist"
    proc = run_hook(home, core_repo=missing_core, cwd=worktree)

    assert proc.returncode == 0
    assert "[worktree-fresh]" not in proc.stdout


# ports test_hook_instructions_refresh.py::test_primary_checkout_no_worktree_fresh_offer
def test_primary_checkout_no_worktree_fresh_offer(tmp_path):
    home = tmp_path / "home2"
    home.mkdir()
    origin, clone = make_bare_and_clone(tmp_path, "wt")
    advance_remote(tmp_path, origin, "wt")

    # The primary (non-linked) checkout of a behind repo — out of scope for
    # worktree-fresh (that is the "Core"/"Project" nudge's job).
    proc = run_hook(home, core_repo=clone, cwd=clone)

    assert proc.returncode == 0
    assert "[worktree-fresh]" not in proc.stdout


# (l) relocate: primary Core checkout or a registered canon-roots entry ->
#     relocation OFFER; a linked worktree or an unregistered sibling stays silent

# ports test_hook_instructions_refresh.py::test_primary_core_checkout_emits_relocate_offer
def test_primary_core_checkout_emits_relocate_offer(tmp_path):
    home = tmp_path / "home2"
    home.mkdir()
    _origin, clone = make_bare_and_clone(tmp_path, "core")

    proc = run_hook(home, core_repo=clone, cwd=clone)

    assert proc.returncode == 0
    assert "[relocate]" in proc.stdout
    assert "session-isolate.sh" in proc.stdout


# ports test_hook_instructions_refresh.py::test_linked_worktree_of_core_no_relocate_offer
def test_linked_worktree_of_core_no_relocate_offer(tmp_path):
    home = tmp_path / "home2"
    home.mkdir()
    _origin, clone = make_bare_and_clone(tmp_path, "core")
    worktree = tmp_path / "core-linked"
    git("worktree", "add", "--quiet", "-b", "wt-branch", str(worktree), "main", cwd=clone)

    proc = run_hook(home, core_repo=clone, cwd=worktree)

    assert proc.returncode == 0
    assert "[relocate]" not in proc.stdout


def _run_with_canon_roots(tmp_path: Path, roots_file: Path, cwd: Path):
    home = tmp_path / "home2"
    home.mkdir()
    missing_core = tmp_path / "does-not-exist"
    env = {
        **os.environ,
        **GIT_ENV,
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(home),
        "CLAUDE_INSTRUCTIONS_REPO": str(missing_core),
        "CLAUDE_SETTINGS_PATH": str(_default_settings_path(home, missing_core)),
        "CLAUDE_CANON_ROOTS_FILE": str(roots_file),
    }
    payload = json.dumps({"session_id": "s-1", "prompt": "hi", "cwd": str(cwd)})
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=payload, env=env, cwd=str(cwd),
        capture_output=True, text=True,
    )


# ports test_hook_instructions_refresh.py::test_canon_roots_entry_emits_relocate_offer
def test_canon_roots_entry_emits_relocate_offer(tmp_path):
    anchor = tmp_path / "org-mirror"
    inside = anchor / "sub" / "dir"
    inside.mkdir(parents=True)
    roots_file = tmp_path / "canon-roots.local"
    roots_file.write_text(f"{anchor}\n")

    proc = _run_with_canon_roots(tmp_path, roots_file, inside)

    assert proc.returncode == 0
    assert "[relocate]" in proc.stdout
    assert "session-isolate.sh" in proc.stdout


# ports test_hook_instructions_refresh.py::test_sibling_of_canon_root_no_relocate_offer
def test_sibling_of_canon_root_no_relocate_offer(tmp_path):
    anchor = tmp_path / "org-mirror"
    anchor.mkdir()
    sibling = tmp_path / "org-mirror-sibling"  # shares a string prefix, not a path-part prefix
    sibling.mkdir()
    roots_file = tmp_path / "canon-roots.local"
    roots_file.write_text(f"{anchor}\n")

    proc = _run_with_canon_roots(tmp_path, roots_file, sibling)

    assert proc.returncode == 0
    assert "[relocate]" not in proc.stdout


def test_refresh_offer_arrives_through_the_merged_hook_beside_other_reminders(tmp_path):
    home = tmp_path / "home2"
    home.mkdir()
    origin, clone = make_bare_and_clone(tmp_path, "core")
    advance_remote(tmp_path, origin, "core")
    session_cwd = tmp_path / "session-cwd"
    session_cwd.mkdir()
    env = {
        **os.environ, **GIT_ENV,
        "HOME": str(home), "CLAUDE_CONFIG_DIR": str(home),
        "CLAUDE_INSTRUCTIONS_REPO": str(clone),
        "CLAUDE_SETTINGS_PATH": str(_default_settings_path(home, clone)),
    }
    payload = json.dumps({"session_id": "s-1", "prompt": "обнови ABC-123", "cwd": str(session_cwd)})
    proc = subprocess.run([sys.executable, str(HOOK_SCRIPT)], input=payload, env=env,
                          cwd=str(session_cwd), capture_output=True, text=True)
    tags = [ln.split("]")[0] for ln in proc.stdout.splitlines()]
    assert tags == ["[language-reminder", "[tracker-reminder", "[instructions-refresh"]
