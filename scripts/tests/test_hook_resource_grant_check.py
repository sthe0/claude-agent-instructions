"""The resource-grant PreToolUse hook (`scripts/hook-resource-grant-check.py`): activation, the kill
switch, each decision class, per-segment rule matching, the decision log and the failure modes.

The opaque-code judge is always a stub; no `claude` process is started. The cases of the denial
fixture are replayed through the same entry point in `test_resource_grant_replay.py`.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import resource_grant_hook_support as support
import test_tool_contracts_segments as seg_tests
from agentctl import config as agentctl_config
from agentctl.dispatch import RunResult
from lib import config_root
from test_resource_grant_replay import JudgeStub

CONSTANTS = {"resource-grant-hook": "on", "resource-grant-hook-timeout-s": "20", "resource-grant-judge-timeout-s": "15"}
ROW_FIELDS = {"ts", "tool", "segment_sha", "decision", "reason_class", "resources", "venue", "matched"}


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("CDPATH", raising=False)
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONSAFEPATH", "AGENTCTL_RESOURCE_HOOK"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(tmp_path / "agent-home"))


@pytest.fixture(scope="module")
def hook():
    return support.load_hook()


@pytest.fixture(scope="module")
def spawn():
    return support.load_spawn()


class Rig:
    def __init__(self, hook, spawn, tmp_path):
        self.hook, self.spawn, self.tmp = hook, spawn, tmp_path
        self.venue = seg_tests._init_repo(tmp_path / "venue", "feat")
        seg_tests._git(self.venue, "remote", "add", "origin", str(tmp_path / "origin"))
        self.outside = tmp_path / "outside"
        self.outside.mkdir()
        self.scratch = tmp_path / support.PARENT_SESSION / "scratchpad"
        self.scratch.mkdir(parents=True)

    def child(self, **kw):
        kw.setdefault("branch", "feat")
        kw.setdefault("allow", [])
        return support.link_child(self.spawn, self.tmp / "state", self.venue, **kw)

    def call(self, child, tool="Bash", *, env=None, judge=None, constants=CONSTANTS, **tool_input):
        payload = child.payload(tool, **tool_input)
        answer = self.hook.run(payload, env or child.env, runner=judge or JudgeStub([]),
                               cache_dir=self.tmp / "judge-cache", constants=constants)
        decision = json.loads(answer)["hookSpecificOutput"]["permissionDecision"] if answer else "none"
        rows = child.log_rows()
        return decision, (rows[-1] if rows else None), answer

    def bash(self, child, command, **kw):
        return self.call(child, "Bash", command=command, **kw)


@pytest.fixture
def rig(hook, spawn, tmp_path):
    return Rig(hook, spawn, tmp_path)


# --- activation and the kill switch ---------------------------------------------------------------

def test_inactive_without_a_recursion_depth_writes_nothing(rig):
    child = rig.child()
    env = {k: v for k, v in child.env.items() if k != "AGENT_RECURSION_DEPTH"}
    decision, row, answer = rig.bash(child, f"touch {rig.outside}/f", env=env)
    assert (decision, answer, row) == ("none", "", None)
    assert not (Path(os.environ["CLAUDE_AGENT_HOME"]) / "agentctl").exists()


def test_inactive_without_a_handoff_record_is_logged_unlinked(rig):
    child = rig.child()
    decision, _, answer = rig.bash(child, f"touch {rig.outside}/f", env={"AGENT_RECURSION_DEPTH": "1"})
    assert (decision, answer) == ("none", "")
    log = config_root.agentctl_state_dir() / "resource-uses" / "_unlinked.jsonl"
    assert json.loads(log.read_text().splitlines()[-1])["reason_class"] == "inactive"


def test_inactive_when_the_record_bytes_do_not_match_the_digest(rig):
    child = rig.child()
    path, digest = child.env["AGENTCTL_NORM_HANDOFF"].rsplit(":", 1)
    Path(path).write_text(Path(path).read_text() + " ")
    decision, _, answer = rig.bash(child, f"touch {rig.outside}/f")
    assert (decision, answer) == ("none", "")


def test_config_row_off_is_the_kill_switch(rig):
    child = rig.child()
    decision, row, _ = rig.bash(child, f"touch {rig.outside}/f", constants={**CONSTANTS, "resource-grant-hook": "off"})
    assert (decision, row["reason_class"]) == ("none", "kill-switch")


def test_env_zero_is_the_kill_switch_and_env_one_beats_the_row(rig):
    child = rig.child()
    decision, row, _ = rig.bash(child, f"touch {rig.outside}/f", env={**child.env, "AGENTCTL_RESOURCE_HOOK": "0"})
    assert (decision, row["reason_class"]) == ("none", "kill-switch")
    decision, row, _ = rig.bash(child, f"touch {rig.outside}/f", env={**child.env, "AGENTCTL_RESOURCE_HOOK": "1"},
                                constants={**CONSTANTS, "resource-grant-hook": "off"})
    assert (decision, row["reason_class"]) == ("deny", "uncovered-write")


def test_config_md_carries_both_hook_rows():
    values = agentctl_config.parse_config_md()
    assert values["resource-grant-hook"] == "on"
    assert agentctl_config.resource_grant_hook_timeout_s() == int(values["resource-grant-hook-timeout-s"]) == 20


# --- Bash decisions -------------------------------------------------------------------------------

def test_covered_push_of_the_declared_branch_is_allowed_and_logged(rig):
    decision, row, _ = rig.bash(rig.child(), "git push origin feat")
    assert (decision, row["reason_class"]) == ("allow", "covered")
    assert row["tool"] == "Bash" and row["venue"] == str(rig.venue)
    assert {"kind": "vcs_ref", "op": "push", "ref": "feat", "remote": "origin"} in row["resources"]


def test_commit_under_a_wildcard_grant_matches_the_rule_pattern(rig):
    rule = f"Bash(git -C {rig.venue} commit:*)"
    decision, row, _ = rig.bash(rig.child(rules=[rule]), f"git -C {rig.venue} commit -q -m x")
    assert (decision, row["reason_class"], row["matched"]) == ("allow", "covered", [rule])


def test_uncovered_write_is_denied_with_a_permission_request_template(rig):
    child = rig.child()
    decision, row, answer = rig.bash(child, f"touch {rig.outside}/f")
    assert (decision, row["reason_class"]) == ("deny", "uncovered-write")
    reason = json.loads(answer)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "PERMISSION-REQUEST:" in reason and f"Resource: file write {rig.outside}/f" in reason
    assert f"Rule: Bash(touch {rig.outside}/f)" in reason


def test_a_push_of_an_unapproved_branch_is_denied(rig):
    child = rig.child()
    decision, row, _ = rig.bash(child, "git push origin HEAD:refs/heads/main")
    assert (decision, row["reason_class"]) == ("deny", "uncovered-write")
    assert {"kind": "vcs_ref", "op": "push", "ref": "main", "remote": "origin"} in row["resources"]


def test_unresolved_segment_makes_no_decision(rig):
    child = rig.child()
    decision, row, _ = rig.bash(child, 'echo "$(date)"')
    assert (decision, row["reason_class"]) == ("none", "undeterminable")


def test_allow_entry_matching_the_segment_leaves_it_to_the_harness(rig):
    entry = f"Bash(touch {rig.outside}/f)"
    child = rig.child(allow=[entry])
    decision, row, answer = rig.bash(child, f"touch {rig.outside}/f")
    assert (decision, answer, row["reason_class"], row["matched"]) == ("none", "", "harness-allowed", [entry])


def test_only_a_kind_baseline_entry_matches_a_write_the_record_lacks(rig):
    todays = support.link_child(rig.spawn, rig.tmp / "state-1", rig.venue, branch=None, mode="default")
    command = "git commit -q -m x"
    decision, row, _ = rig.bash(todays, command)
    assert (decision, row["reason_class"], row["matched"]) == ("none", "harness-allowed", ["Bash(git commit:*)"])
    bare = support.link_child(rig.spawn, rig.tmp / "state-2", rig.venue, branch=None, mode="default", allow=[])
    decision, row, _ = rig.bash(bare, command)
    assert (decision, row["reason_class"]) == ("deny", "uncovered-write")


def test_a_commit_on_a_branch_the_record_does_not_declare_is_denied(rig):
    venue_write = support.R.FileResource(str(rig.venue), "write")
    child = rig.child(branch="other", allow=[], mode="default", extra_resources=[venue_write])
    decision, row, _ = rig.bash(child, "git commit -q -m x")
    assert (decision, row["reason_class"]) == ("deny", "uncovered-write")
    assert {"kind": "vcs_ref", "op": "commit", "ref": "feat", "remote": "."} in row["resources"]


@pytest.mark.parametrize("joiner",["&&", "||", ";", "|", "\n"])
def test_an_allow_entry_for_the_prefix_does_not_shelter_an_uncovered_tail(rig, joiner):
    child = rig.child(allow=["Bash(git status:*)"])
    decision, row, _ = rig.bash(child, f"git status {joiner} touch {rig.outside}/f")
    assert (decision, row["reason_class"]) == ("deny", "uncovered-write")


def test_an_allow_entry_does_not_match_a_segment_with_a_redirect(rig):
    child = rig.child(allow=["Bash(git status:*)"])
    decision, row, _ = rig.bash(child, f"git status > {rig.outside}/f")
    assert (decision, row["reason_class"]) == ("deny", "uncovered-write")


def test_mode_that_already_runs_the_write_suppresses_the_deny(rig):
    child = rig.child(mode="default")
    assert rig.bash(child, "touch f")[0] == "deny"
    child.mode = "acceptEdits"
    decision, row, _ = rig.bash(child, "touch f")
    assert (decision, row["reason_class"], row["matched"]) == ("none", "harness-allowed", ["mode:acceptEdits"])
    assert rig.bash(child, f"touch {rig.outside}/f")[0] == "deny"


def test_rule_pattern_covers_the_segment_it_matches(rig):
    child = rig.child(extra_patterns=["Bash(python3 -m pytest:*)"])
    decision, row, _ = rig.bash(child, f"python3 -m pytest -q {rig.venue}/tests")
    assert (decision, row["reason_class"], row["matched"]) == ("allow", "covered", ["Bash(python3 -m pytest:*)"])


def test_rule_pattern_is_matched_per_segment_not_per_call(rig):
    child = rig.child(extra_patterns=["Bash(python3 -m pytest:*)"])
    decision, row, _ = rig.bash(child, f"python3 -m pytest -q && touch {rig.outside}/f")
    assert (decision, row["reason_class"]) == ("deny", "uncovered-write")
    assert row["matched"] == ["Bash(python3 -m pytest:*)"]


def test_rule_pattern_does_not_match_a_redirect_or_a_longer_word(rig):
    child = rig.child(extra_patterns=["Bash(python3 -m pytest:*)"])
    for command in (f"python3 -m pytest -q > {rig.outside}/f", f"python3 -m pytestx {rig.outside}/f"):
        decision, row, _ = rig.bash(child, command)
        assert decision != "allow" and "Bash(python3 -m pytest:*)" not in row["matched"], command


def test_a_read_only_segment_reads_only_inside_the_approved_resources(rig):
    (rig.scratch / "note.txt").write_text("x")
    (rig.outside / "secret.txt").write_text("x")
    child = rig.child()
    decision, row, _ = rig.bash(child, f"cat {rig.scratch}/note.txt")
    assert (decision, row["reason_class"], row["matched"]) == ("allow", "covered", ["parent-scratchpad"])
    decision, row, _ = rig.bash(child, f"cat {rig.outside}/secret.txt")
    assert (decision, row["reason_class"]) == ("none", "undeterminable")
    (rig.venue / "a.txt").write_text("x")
    decision, row, _ = rig.bash(child, f"cat {rig.venue}/a.txt")
    assert (decision, row["matched"]) == ("allow", ["record-resource"])
    assert rig.bash(child, "cat ~/.ssh/id_rsa")[0] == "none"


def test_a_scratchpad_symlink_to_the_outside_is_not_scratchpad(rig):
    (rig.outside / "secret.txt").write_text("x")
    (rig.scratch / "link").symlink_to(rig.outside)
    child = rig.child()
    assert rig.bash(child, f"cat {rig.scratch}/link/secret.txt")[0] == "none"
    assert rig.call(child, "Read", file_path=f"{rig.scratch}/link/secret.txt")[0] == "none"


def test_a_scratchpad_layout_outside_the_scratch_roots_is_not_scratchpad(rig, monkeypatch):
    elsewhere = rig.tmp / "elsewhere" / support.PARENT_SESSION / "scratchpad"
    elsewhere.mkdir(parents=True)
    (elsewhere / "n.txt").write_text("x")
    monkeypatch.setattr(rig.hook, "scratch_roots", lambda: {str(rig.tmp / "scratch-root")})
    child = rig.child()
    assert rig.bash(child, f"cat {elsewhere}/n.txt")[0] == "none"
    assert rig.call(child, "Read", file_path=f"{elsewhere}/n.txt")[0] == "none"


def test_scratchpad_of_another_session_is_not_the_parents(rig):
    other = rig.tmp / "other-session" / "scratchpad"
    other.mkdir(parents=True)
    (other / "n.txt").write_text("x")
    assert rig.bash(rig.child(), f"cat {other}/n.txt")[0] == "none"


# --- opaque code ----------------------------------------------------------------------------------

STDIN_CODE = "python3 - <<'E'\nimport json\nprint(json.dumps({}))\nE"


def test_opaque_code_whose_judged_effects_are_covered_is_allowed(rig):
    judge = JudgeStub([])
    decision, row, _ = rig.bash(rig.child(), STDIN_CODE, judge=judge)
    assert (decision, row["reason_class"]) == ("allow", "opaque-judged") and judge.calls == 1


def test_opaque_code_whose_judged_effect_is_uncovered_is_denied(rig):
    effect = {"kind": "file", "op": "write", "path": f"{rig.outside}/f"}
    decision, row, _ = rig.bash(rig.child(), STDIN_CODE, judge=JudgeStub([effect]))
    assert (decision, row["reason_class"]) == ("deny", "uncovered-write")
    assert {"kind": "file", "mode": "write", "path": f"{rig.outside}/f"} in row["resources"]


def test_a_judge_that_fails_is_never_an_allow(rig):
    def broken(argv, *, timeout, stdin=""):
        return RunResult(1, "", "boom", timed_out=True)

    decision, row, answer = rig.bash(rig.child(), STDIN_CODE, judge=broken)
    assert (decision, answer, row["reason_class"]) == ("none", "", "undeterminable")


def test_python_environment_in_the_hook_process_makes_opaque_code_undeterminable(rig, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(rig.outside))
    judge = JudgeStub([])
    decision, row, _ = rig.bash(rig.child(), STDIN_CODE, judge=judge)
    assert (decision, row["reason_class"]) == ("none", "undeterminable") and judge.calls == 0


# --- Edit, Write, Read ----------------------------------------------------------------------------

@pytest.mark.parametrize("tool, key", [("Edit", "file_path"), ("Write", "file_path"), ("MultiEdit", "file_path"),
                                       ("NotebookEdit", "notebook_path")])
def test_edit_tools_allow_a_covered_file_and_never_deny(rig, tool, key):
    child = rig.child(mode="acceptEdits")
    decision, row, _ = rig.call(child, tool, **{key: str(rig.venue / "a.py")})
    assert (decision, row["reason_class"], row["matched"]) == ("allow", "covered", ["record-resource"])
    decision, row, answer = rig.call(child, tool, **{key: str(rig.outside / "a.py")})
    assert (decision, answer, row["reason_class"]) == ("none", "", "uncovered-write")


def test_edit_of_a_relative_path_is_resolved_against_the_cwd(rig):
    decision, _, _ = rig.call(rig.child(), "Write", file_path="sub/a.py", content="x")
    assert decision == "allow"
    assert rig.call(rig.child(), "Write", file_path="../outside/a.py", content="x")[0] == "none"


def test_read_is_allowed_only_for_record_resources_and_the_parent_scratchpad(rig):
    child = rig.child()
    assert rig.call(child, "Read", file_path=str(rig.scratch / "n.txt"))[1]["matched"] == ["parent-scratchpad"]
    assert rig.call(child, "Read", file_path=str(rig.venue / "a.py"))[0] == "allow"
    decision, row, answer = rig.call(child, "Read", file_path=str(rig.outside / "a.py"))
    assert (decision, answer, row["reason_class"]) == ("none", "", "undeterminable")
    assert rig.call(child, "Read", file_path=str(rig.venue.parent / "other" / ".." / "outside" / "a.py"))[0] == "none"


def test_other_tools_make_no_decision(rig):
    decision, row, _ = rig.call(rig.child(), "Glob", pattern="*")
    assert (decision, row["reason_class"]) == ("none", "undeterminable")


# --- the decision log and failures ----------------------------------------------------------------

def test_every_call_appends_one_complete_row_per_reason_class(rig):
    child = rig.child(rules=[f"Bash(git -C {rig.venue} commit:*)"])
    rig.bash(child, f"git -C {rig.venue} commit -q -m x")
    rig.bash(child, f"touch {rig.outside}/f")
    rig.bash(child, 'echo "$(date)"')
    rig.bash(child, STDIN_CODE)
    rig.bash(child, "git status", env=child.env, constants={**CONSTANTS, "resource-grant-hook": "off"})
    harness = rig.child(allow=["Bash(git status:*)"])
    rig.bash(harness, "git status")
    rows = child.log_rows() + [r for r in harness.log_rows() if r["reason_class"] == "harness-allowed"]
    assert {r["reason_class"] for r in rows} == {"covered", "uncovered-write", "undeterminable", "opaque-judged",
                                                 "kill-switch", "harness-allowed"}
    assert all(set(r) == ROW_FIELDS for r in rows)
    assert all(len(r["segment_sha"]) == 64 for r in rows)


def test_a_hook_error_is_logged_and_never_an_allow(rig, hook, monkeypatch):
    def explode(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(hook, "_approved", explode)
    decision, row, answer = rig.bash(rig.child(), f"touch {rig.outside}/f")
    assert (decision, answer, row["reason_class"]) == ("none", "", "error")


def test_malformed_input_is_ignored(rig, hook):
    child = rig.child()
    assert hook.run("not json", child.env) == ""
    assert hook.run("[]", child.env) == ""


# --- the script entry point -----------------------------------------------------------------------

def _run_script(rig, child, payload, extra_env=None):
    env = {**os.environ, **child.env, **(extra_env or {})}
    return subprocess.run([sys.executable, str(support.HOOK_FILE)], input=payload, env=env, text=True,
                          capture_output=True, timeout=60)


def test_script_denies_through_stdout_and_exits_zero(rig):
    child = rig.child()
    done = _run_script(rig, child, child.payload("Bash", command=f"touch {rig.outside}/f"))
    answer = json.loads(done.stdout)["hookSpecificOutput"]
    assert (done.returncode, answer["hookEventName"], answer["permissionDecision"]) == (0, "PreToolUse", "deny")
    assert child.log_rows()[-1]["decision"] == "deny"


def test_script_with_unreadable_stdin_exits_zero_silently(rig):
    done = _run_script(rig, rig.child(), "{")
    assert (done.returncode, done.stdout) == (0, "")


def test_script_ignores_pythonpath_shadowing_agentctl(rig):
    shadow = rig.tmp / "shadow"
    (shadow / "agentctl").mkdir(parents=True)
    (shadow / "agentctl" / "__init__.py").write_text("raise SystemExit(9)\n")
    child = rig.child()
    done = _run_script(rig, child, child.payload("Bash", command=f"touch {rig.outside}/f"),
                       {"PYTHONPATH": str(shadow)})
    assert json.loads(done.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- registration in the child's --settings -------------------------------------------------------

def _dry_run_command_line(out: str) -> str:
    return out.split("=== command (not executed) ===", 1)[1].strip().splitlines()[0]


def _printed_argument(command_line: str, flag: str) -> str:
    """The value following `flag` in the `--dry-run` command line (quoted items are Python reprs)."""
    import ast

    head, found, rest = command_line.partition(f"{flag} ")
    assert found, f"{flag} not in the command line"
    if rest[0] not in "'\"":
        return rest.split(" ", 1)[0]
    end = 1
    while rest[end] != rest[0]:
        end += 2 if rest[end] == "\\" else 1
    return ast.literal_eval(rest[: end + 1])


def engine_dry_run(spawn, store, fixtures, tmp_path, monkeypatch, capsys, session):
    """Drive a real engine session to EXECUTING and print `spawn-specialist.py --dry-run` for stage 1."""
    from test_stage_grants import _to_executing, _write_declared_grants_plan

    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(spawn, "plans_dir", lambda: plans)
    plan = _write_declared_grants_plan(fixtures, plans)
    _to_executing(store, session, fixtures, plan_path=plan)
    argv = ["--kind", "developer", "--plan", plan, "--done-criterion", "tests green", "--criterion-type", "measurable",
            "--complexity", "medium", "--effort", "high", "--session", session, "--stage-index", "1",
            "--state-root", str(tmp_path / "state"), "--dry-run"]
    assert spawn.main(argv) == 0
    return capsys.readouterr().out


def test_dry_run_registers_the_hook_with_the_configured_timeout(spawn, store, venued_fixtures_dir, tmp_path,
                                                                monkeypatch, capsys):
    out = engine_dry_run(spawn, store, venued_fixtures_dir, tmp_path, monkeypatch, capsys, "hook-registration")
    command_line = _dry_run_command_line(out)
    settings = json.loads(_printed_argument(command_line, "--settings"))
    [group] = settings["hooks"]["PreToolUse"]
    [entry] = group["hooks"]
    assert group["matcher"] == spawn.RESOURCE_GRANT_HOOK_MATCHER
    assert entry["timeout"] == agentctl_config.resource_grant_hook_timeout_s()
    assert str(support.HOOK_FILE) in entry["command"]
    assert "Bash(git status:*)" in settings["permissions"]["allow"]
    add_dirs = re.findall(r"--add-dir (\S+)", command_line)
    assert add_dirs
    print("child payload:", json.dumps({"hook": group, "allow": settings["permissions"]["allow"],
                                         "add_dirs": add_dirs}, sort_keys=True))


def test_dry_run_of_an_unlinked_spawn_registers_no_hook(spawn, venued_fixtures_dir, tmp_path, monkeypatch, capsys):
    plan = venued_fixtures_dir / "plan_two_stage.toml"
    argv = ["--kind", "developer", "--plan", str(plan), "--done-criterion", "tests green",
            "--criterion-type", "measurable", "--complexity", "medium", "--effort", "high", "--dry-run"]
    assert spawn.main(argv) == 0
    settings = json.loads(_printed_argument(_dry_run_command_line(capsys.readouterr().out), "--settings"))
    assert support.HOOK_FILE.name not in json.dumps(settings.get("hooks", {}))


def test_dry_run_registers_nothing_when_the_kill_switch_is_off(spawn, store, venued_fixtures_dir, tmp_path,
                                                               monkeypatch, capsys):
    monkeypatch.setenv("AGENTCTL_RESOURCE_HOOK", "0")
    out = engine_dry_run(spawn, store, venued_fixtures_dir, tmp_path, monkeypatch, capsys, "hook-kill-switch")
    settings = json.loads(_printed_argument(_dry_run_command_line(out), "--settings"))
    assert support.HOOK_FILE.name not in json.dumps(settings.get("hooks", {}))
