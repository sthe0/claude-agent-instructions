"""Stage-2 result-image oracle for the spawn permission-grant model.

One named test per oracle point of the stage-2 fix round, each asserting
concrete values (exact rule strings, argv members, exit codes). A test that
fails on the code as it stands when this file was written carries
`xfail(strict=True, reason="stage2-oracle: <point>")`; the fix round removes the
mark as the point turns green, and a strict mark turns an unannounced pass into a
failure, so the marks cannot silently go stale.

Where a point names behaviour whose API the plan does not name, the test drives
the named interface instead (`build_child_settings`, `stage_grant_rules`,
`resolve_permission_mode`, `grants.validate_add_dir`, or the `--dry-run` argv)
and says so in its docstring.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from agentctl import cli, grants, plugins
from agentctl.directive import Directive
from agentctl.dispatch import RunResult
from agentctl.state import Node, StageStatus
from lib import widening_targets
from test_stage_grants import _to_executing, _write_plan_with_grants_block, ns

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS_DIR / "spawn-specialist.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load():
    spec = importlib.util.spec_from_file_location("spawn_specialist_stage2_image", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()

ALL_KINDS = ("developer", "tech-writer", "thinker", "planner", "code-reviewer")
PINNED_MODE = {
    "developer": "acceptEdits",
    "tech-writer": "acceptEdits",
    "thinker": "default",
    "planner": "default",
    "code-reviewer": "default",
}


def _xfail(point: str):
    return pytest.mark.xfail(strict=True, reason=f"stage2-oracle: {point}")


def _file_arg(path: str) -> str:
    return grants.rule_file_arg(path)


def _base_argv(kind: str, plan: Path) -> list[str]:
    return [
        "--kind", kind,
        "--plan", str(plan),
        "--done-criterion", "tests green",
        "--criterion-type", "measurable",
        "--complexity", "medium",
        "--effort", "high",
        "--dry-run",
    ]


class DryRun:
    """Parsed `--dry-run` stdout: the assembled prompt, the printed argv, and
    the `--settings` JSON carried in that argv."""

    def __init__(self, rc: int, out: str, err: str):
        self.rc = rc
        self.out = out
        self.err = err
        self.prompt = ""
        self.argv: list[str] = []
        self.settings: dict = {}
        if "=== command (not executed) ===" not in out:
            return
        prompt_half, command_half = out.rsplit("=== command (not executed) ===", 1)
        self.prompt = prompt_half
        command_line = command_half.strip().splitlines()[0]
        self.argv = shlex.split(command_line)
        self.settings = json.loads(self.argv[self.argv.index("--settings") + 1])

    @property
    def allow(self) -> list[str]:
        return self.settings.get("permissions", {}).get("allow", [])

    @property
    def deny(self) -> list[str]:
        return self.settings.get("permissions", {}).get("deny", [])

    @property
    def add_dirs(self) -> list[str]:
        return [self.argv[i + 1] for i, tok in enumerate(self.argv) if tok == "--add-dir"]

    @property
    def permission_mode(self) -> "str | None":
        if "--permission-mode" not in self.argv:
            return None
        return self.argv[self.argv.index("--permission-mode") + 1]

    @property
    def header_add_dirs(self) -> list[str]:
        return re.findall(r"^- Additional directory: `([^`]*)`$", self.prompt, flags=re.M)


def _dry_run(capsys, argv: list[str]) -> DryRun:
    try:
        rc = MOD.main(argv)
    except SystemExit as exc:  # argparse refusals exit rather than return
        rc = exc.code if isinstance(exc.code, int) else 1
    captured = capsys.readouterr()
    return DryRun(rc, captured.out, captured.err)


def _inject_engine_grants(monkeypatch, entries: list[dict]) -> None:
    """Hand `main()` a fixed engine grant set for whichever --kind it is
    called with, so the materialization of one grant shape can be checked
    across every kind without a per-kind fixture plan."""
    monkeypatch.setattr(MOD, "load_engine_stage_grants", lambda *a, **kw: list(entries))


def _engine_argv(kind: str, plan: Path) -> list[str]:
    return _base_argv(kind, plan) + ["--session", "injected", "--stage-index", "1"]


@pytest.fixture
def plans(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(MOD, "plans_dir", lambda: directory)
    return directory


@pytest.fixture
def two_stage_plan(fixtures_dir) -> Path:
    return fixtures_dir / "plan_two_stage.toml"


def _write_add_dir_denies(path: str) -> list[str]:
    base = _file_arg(path)
    return [
        f"Edit({base}/**/.claude/**)",
        f"Edit({base}/**/settings*.json)",
        f"Edit({base}/**/.git/**)",
        f"Edit({base}/**/.git)",
    ]


# --- point 1: write add_dir materializes allow + guard denies ----------------


@_xfail("1 write add_dir materializes Edit allow plus .claude/settings/.git denies")
@pytest.mark.parametrize("kind", ALL_KINDS)
def test_p01_write_add_dir_materializes_edit_allow_and_guard_denies(
    kind, tmp_path, plans, two_stage_plan, monkeypatch, capsys,
):
    work = tmp_path / "work"
    work.mkdir()
    _inject_engine_grants(monkeypatch, [{"path": str(work), "mode": "write", "provenance": "declared"}])

    run = _dry_run(capsys, _engine_argv(kind, two_stage_plan))

    assert run.rc == 0
    assert run.permission_mode == PINNED_MODE[kind]
    assert str(work) in run.add_dirs
    assert f"Edit({_file_arg(str(work))}/**)" in run.allow
    for rule in _write_add_dir_denies(str(work)):
        assert rule in run.deny


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_p01b_read_add_dir_keeps_its_edit_deny(kind, tmp_path, plans, two_stage_plan, monkeypatch, capsys):
    ro = tmp_path / "ro"
    ro.mkdir()
    _inject_engine_grants(monkeypatch, [{"path": str(ro), "mode": "read", "provenance": "declared"}])

    run = _dry_run(capsys, _engine_argv(kind, two_stage_plan))

    assert run.rc == 0
    assert str(ro) in run.add_dirs
    assert f"Edit({_file_arg(str(ro))}/**)" in run.deny
    assert f"Edit({_file_arg(str(ro))}/**)" not in run.allow


# --- point 2: validate_add_dir(write) refusals --------------------------------

_HOME = str(Path.home())
_XF2 = _xfail("2 validate_add_dir(write) refuses launch surfaces, globs, relative, .git")

_P02_REFUSED = [
    pytest.param(f"{_HOME}/Library/LaunchAgents", id="launch-agents", marks=_XF2),
    pytest.param(f"{_HOME}/Library/LaunchAgents/sub", id="under-launch-agents", marks=_XF2),
    pytest.param(f"{_HOME}/Library", id="library-parent", marks=_XF2),
    pytest.param(f"{_HOME}/.config/systemd/user", id="systemd-user", marks=_XF2),
    pytest.param(f"{_HOME}/.config/systemd/user/sub", id="under-systemd-user", marks=_XF2),
    pytest.param(f"{_HOME}/.config/systemd", id="systemd-parent", marks=_XF2),
    pytest.param(f"{_HOME}/.config", id="config-parent", marks=_XF2),
    pytest.param(f"{_HOME}/*", id="glob-star-home", marks=_XF2),
    pytest.param("/tmp/a?b", id="glob-question", marks=_XF2),
    pytest.param("/tmp/[ab]", id="glob-bracket", marks=_XF2),
    pytest.param("/tmp/{a,b}", id="glob-brace", marks=_XF2),
    pytest.param("relative/dir", id="not-absolute", marks=_XF2),
    pytest.param("/tmp/repo/.git/hooks", id="inside-git-dir", marks=_XF2),
]


@pytest.mark.parametrize("path", _P02_REFUSED)
def test_p02_validate_add_dir_write_refuses(path):
    with pytest.raises(grants.GrantValidationError):
        grants.validate_add_dir(path, "write")


# --- point 3: caller-supplied glob rule still refused -------------------------


def test_p03_validate_rule_still_refuses_caller_edit_glob():
    with pytest.raises(grants.GrantValidationError):
        grants.validate_rule("Edit(//tmp/x/**)")


# --- point 4: no emitted Edit(//.../**) allow has a metacharacter prefix ------


def _glob_edit_allow_prefixes(allow: list[str]) -> list[str]:
    prefixes = []
    for rule in allow:
        m = re.fullmatch(r"Edit\((//.*)/\*\*\)", rule)
        if m:
            prefixes.append(m.group(1))
    return prefixes


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_p04_no_dry_run_emits_edit_glob_allow_with_metachar_prefix(
    kind, tmp_path, plans, two_stage_plan, monkeypatch, capsys,
):
    """The dry-run carries a valid write add_dir (plus the plans-dir grant the
    kind already gets); every `Edit(//<prefix>/**)` ALLOW it emits must have a
    prefix free of glob metacharacters, so the only glob in the rule is the
    trailing `/**` the materializer itself appends."""
    work = tmp_path / "work"
    work.mkdir()
    _inject_engine_grants(monkeypatch, [{"path": str(work), "mode": "write", "provenance": "declared"}])

    run = _dry_run(capsys, _engine_argv(kind, two_stage_plan))

    assert run.rc == 0
    for prefix in _glob_edit_allow_prefixes(run.allow):
        assert not any(ch in prefix for ch in "*?[]{}"), prefix


# --- point 5: write add_dir shadowed by the plans-dir deny is refused ---------


@_xfail("5 write add_dir under the plans dir for a PLANS_READ kind is refused")
@pytest.mark.parametrize("kind", MOD.PLANS_READ_KINDS)
def test_p05_write_add_dir_shadowed_by_plans_deny_is_refused(
    kind, plans, two_stage_plan, monkeypatch, capsys,
):
    """Driven through `main()` with the engine grant set injected, since the
    refusal is a property of the assembled spawn (the plans-dir deny and the
    stage's write add_dir only meet there)."""
    shadowed = plans / "scratch"
    shadowed.mkdir()
    _inject_engine_grants(monkeypatch, [{"path": str(shadowed), "mode": "write", "provenance": "declared"}])

    run = _dry_run(capsys, _engine_argv(kind, two_stage_plan))

    assert run.rc != 0
    assert f"Edit({_file_arg(str(plans))}/**)" in run.err


# --- point 6: planner research rules, absolute-path baselines -----------------

PLANNER_PLAN_GRANTS_RULE = f"Bash(python3 {SCRIPTS_DIR}/agentctl-cli.py plan-grants:*)"
PLANNER_LIST_DENIED_RULE = f"Bash(python3 {SCRIPTS_DIR}/check-spawn-tool-run.py --list-denied:*)"


def _bash_tokens(rule: str) -> "list[str] | None":
    parsed = grants.rule_program_and_arg(rule)
    if parsed is None or parsed[0] != "Bash":
        return None
    return shlex.split(grants.bash_command_from_rule_arg(parsed[1]))


@_xfail("6 planner carries absolute plan-grants/list-denied rules; no relative scripts/ baselines")
def test_p06_planner_research_rules_absolute_and_no_relative_baselines(plans, two_stage_plan, capsys):
    run = _dry_run(capsys, _base_argv("planner", two_stage_plan))

    assert run.rc == 0
    assert PLANNER_PLAN_GRANTS_RULE in run.allow
    assert PLANNER_LIST_DENIED_RULE in run.allow

    offenders = []
    for kind, rules in MOD.KIND_BASELINES.items():
        for rule in rules:
            tokens = _bash_tokens(rule) or []
            if any(t.startswith("scripts/") or t == "check-order-coverage.py" for t in tokens):
                offenders.append((kind, rule))
    assert offenders == []


# --- point 7: prescribed planner commands work from outside the repo ----------


@_xfail("7 header-prescribed planner commands are covered and run from outside the repo")
def test_p07_prescribed_planner_commands_covered_and_run_outside_repo(
    tmp_path, plans, monkeypatch, capsys,
):
    """The prompt header's prescription is checked as the literal command
    string appearing in the assembled prompt, since harness Bash rules match
    the literal command and the plan requires rule and prescription to be
    spelled identically. The fixture plan copy is relabelled `small_change`:
    `plan-grants` loads plans strictly, and the fixture carries only the grant
    shape, not the extra fields a substantive plan must declare."""
    outside = tmp_path / "outside"
    outside.mkdir()
    fixture_plan = tmp_path / "self-grants.toml"
    fixture_plan.write_text(
        (FIXTURES / "grant_plans" / "self-grants.toml").read_text()
        .replace("__CLAUDE_AGENT_HOME__", str(tmp_path / "agent-home"))
        .replace('weight_class = "substantive"', 'weight_class = "small_change"', 1)
    )
    transcript = FIXTURES / "transcript_stops" / "permission-denial.jsonl"
    monkeypatch.chdir(outside)

    run = _dry_run(capsys, _base_argv("planner", fixture_plan))
    assert run.rc == 0

    commands = [
        (
            f"python3 {SCRIPTS_DIR}/agentctl-cli.py plan-grants --plan {fixture_plan} --format compact",
            [sys.executable, str(SCRIPTS_DIR / "agentctl-cli.py"), "plan-grants",
             "--plan", str(fixture_plan), "--format", "compact"],
        ),
        (
            f"python3 {SCRIPTS_DIR}/check-spawn-tool-run.py --list-denied --transcript {transcript}",
            [sys.executable, str(SCRIPTS_DIR / "check-spawn-tool-run.py"), "--list-denied",
             "--transcript", str(transcript)],
        ),
    ]
    for command, exec_argv in commands:
        proc = subprocess.run(exec_argv, cwd=outside, capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, (command, proc.stdout[-2000:], proc.stderr[-2000:])

    materialized = grants.StageGrants(allow=[grants.RuleGrant(r, "baseline") for r in run.allow])
    assert f"python3 {SCRIPTS_DIR}/agentctl-cli.py plan-grants" in run.prompt
    assert f"python3 {SCRIPTS_DIR}/check-spawn-tool-run.py --list-denied" in run.prompt
    for command, _ in commands:
        assert grants.grant_covers_call(materialized, "Bash", {"command": command}), command


# --- point 8: unknown kind pinned to default mode, default baseline -----------

UNKNOWN_KIND = "yandex-cloud-expert"


@_xfail("8 a kind outside KIND_BASELINES gets --permission-mode default and the default baseline")
def test_p08_unknown_kind_gets_default_mode_and_default_baseline(plans, two_stage_plan, capsys):
    assert MOD.skill_path(UNKNOWN_KIND).exists()
    assert UNKNOWN_KIND not in MOD.KIND_BASELINES

    run = _dry_run(capsys, _base_argv(UNKNOWN_KIND, two_stage_plan))
    assert run.rc == 0
    assert run.permission_mode == "default"
    assert run.allow == list(MOD.KIND_BASELINES["default"])

    for kind in ("thinker", "planner", "code-reviewer"):
        other = _dry_run(capsys, _base_argv(kind, two_stage_plan))
        assert other.rc == 0
        assert other.argv[other.argv.index("--permission-mode"):][:2] == ["--permission-mode", "default"]


# --- points 9-11: real engine state -------------------------------------------

DECLARED_RULE = "Bash(git rev-parse:*)"
DERIVED_DRV_RULE = "Bash(git diff --stat:*)"
RUNTIME_RULE = "Bash(foo:*)"


def _engine_plan(fixtures_dir, tmp_path) -> str:
    return _write_plan_with_grants_block(
        fixtures_dir, tmp_path,
        'verify_command = "git diff --stat"\nexpected_exit = 0\n\n'
        f'[stage.grants]\nallow = ["{DECLARED_RULE}"]\n',
        "plan_stage2_image.toml",
    )


def _grant_runtime_once(store, sid: str) -> None:
    state = store.load(sid)
    state.permission_request = cli.PermissionRequest(action="need foo", stage_index=1, raw="need foo")
    store.save(state)
    resolved = cli.cmd_resolve_permission(
        ns(session=sid, decision="granted", scope="once", rules=[RUNTIME_RULE], add_dirs=None),
        store=store,
    )
    assert resolved.ok


def _session_argv(sid: str, tmp_path: Path, plan: str) -> list[str]:
    return _base_argv("developer", Path(plan)) + [
        "--session", sid, "--stage-index", "1", "--state-root", str(tmp_path / "state"),
    ]


def test_p09_three_provenances_on_real_engine_state(store, fixtures_dir, tmp_path, plans, capsys):
    sid = "stage2-three-provenances"
    plan = _engine_plan(fixtures_dir, tmp_path)
    _to_executing(store, sid, fixtures_dir, plan_path=plan)
    _grant_runtime_once(store, sid)

    run = _dry_run(capsys, _session_argv(sid, tmp_path, plan))

    assert run.rc == 0
    for rule in (DECLARED_RULE, DERIVED_DRV_RULE, RUNTIME_RULE):
        assert rule in run.allow
    assert f"- `{DECLARED_RULE}` — declared" in run.prompt
    assert f"- `{DERIVED_DRV_RULE}` — derived:DR-V" in run.prompt
    assert f"- `{RUNTIME_RULE}` — runtime" in run.prompt
    assert re.search(r"^- `[^`]+` — derived:DR-[A-Z]$", run.prompt, flags=re.M)


def test_p10_once_grant_consumed_after_real_dispatch(store, fixtures_dir, tmp_path, plans, capsys):
    sid = "stage2-once-consumed"
    plan = _engine_plan(fixtures_dir, tmp_path)
    _to_executing(store, sid, fixtures_dir, plan_path=plan)
    _grant_runtime_once(store, sid)

    before = _dry_run(capsys, _session_argv(sid, tmp_path, plan))
    assert RUNTIME_RULE in before.allow

    def runner(argv, cwd=None):
        return RunResult(0, stdout="COMPLETED: done\n")

    directive = cli.cmd_dispatch(
        ns(session=sid, budget="medium", complexity="medium", dry_run=False),
        store=store, runner=runner,
    )
    assert directive.marker == "COMPLETED"

    after = _dry_run(capsys, _session_argv(sid, tmp_path, plan))
    assert after.rc == 0
    assert RUNTIME_RULE not in after.allow
    assert f"`{RUNTIME_RULE}`" not in after.prompt
    assert DECLARED_RULE in after.allow


def test_p11_covered_permission_request_is_a_materialization_defect(store, fixtures_dir, tmp_path):
    """"No re-dispatch" is asserted on the engine's own routing: the covered
    request launches the child once and the returned directive routes to
    diagnosis rather than to another dispatch. A caller that invokes
    `cmd_dispatch` again by hand is not refused by the node today; that is
    outside what this point names."""
    sid = "stage2-covered-request"
    plan = _engine_plan(fixtures_dir, tmp_path)
    _to_executing(store, sid, fixtures_dir, plan_path=plan)
    calls = []

    def runner(argv, cwd=None):
        calls.append(argv)
        return RunResult(0, stdout=f"PERMISSION-REQUEST: need rev-parse\nRule: {DECLARED_RULE}\n")

    directive = cli.cmd_dispatch(
        ns(session=sid, budget="medium", complexity="medium", dry_run=False),
        store=store, runner=runner,
    )
    assert directive.marker == "OVERCOME-DIFFICULTY"
    assert directive.action != "dispatch"
    assert len(calls) == 1
    state = store.load(sid)
    assert len(state.materialization_defects) == 1
    assert state.materialization_defects[0]["stage_index"] == 1
    assert state.materialization_defects[0]["tool_name"] == "Bash"
    assert state.stage(1).outcome.status == StageStatus.FAILED.value
    assert state.node == Node.DIAGNOSING.value
    assert state.permission_request is None

    stats = cli.cmd_grant_stats(ns(session=sid, json=True), store=store)
    assert stats.ok
    assert len(stats.data["materialization_defects"]) == 1
    assert stats.data["planning_misses"] == []
    assert stats.data["planning_miss_counts"] == {"asked": 0, "unasked": 0}


# --- point 12: a review spawn carries none of the stage's grants --------------


def test_p12_review_spawn_from_directive_carries_no_stage_grants(
    store, fixtures_dir, tmp_path, plans, monkeypatch, capsys,
):
    """The review spawn is built exactly as the review_dispatch directive
    prescribes (`--workdir <venue>`, never `--session`); the directive is
    fired on the same real session whose developer stage holds acceptEdits and
    a write add_dir."""
    sid = "stage2-review-no-grants"
    plan = _engine_plan(fixtures_dir, tmp_path)
    _to_executing(store, sid, fixtures_dir, plan_path=plan)
    work = tmp_path / "work"
    work.mkdir()
    state = store.load(sid)
    state.permission_request = cli.PermissionRequest(action="need work dir", stage_index=1, raw="need work dir")
    store.save(state)
    assert cli.cmd_resolve_permission(
        ns(session=sid, decision="granted", scope="stage", rules=None, add_dirs=[f"{work}:write"]),
        store=store,
    ).ok

    developer = _dry_run(capsys, _session_argv(sid, tmp_path, plan))
    assert developer.rc == 0
    assert developer.permission_mode == "acceptEdits"
    assert str(work) in developer.add_dirs

    for knob in ("AGENTCTL_PLAN_REVIEW", "AGENTCTL_CODE_REVIEW", "AGENTCTL_REVIEW_DISPATCH"):
        monkeypatch.delenv(knob, raising=False)
    state = store.load(sid)
    plugins.activate(state, "review_dispatch")
    fired = plugins.fire("dispatch", state, Directive(True, state.node, "noop"))
    review = next(p for p in fired if p["plugin"] == "review_dispatch" and p["action"] == "spawn_code_review")
    assert review["data"]["specialist"] == "code-reviewer"
    assert "--workdir" in review["detail"]
    assert "never --session" in review["detail"]

    venue = tmp_path / "venue"
    venue.mkdir()
    reviewer = _dry_run(capsys, _base_argv(review["data"]["specialist"], Path(plan)) + ["--workdir", str(venue)])

    assert reviewer.rc == 0
    assert "--session" not in reviewer.argv
    assert "acceptEdits" not in reviewer.argv
    assert str(work) not in reviewer.add_dirs
    assert all(_file_arg(str(work)) not in rule for rule in reviewer.allow + reviewer.deny)
    assert "Stage grants (provenance):" not in reviewer.prompt


# --- point 13: header add-dir list equals the argv --add-dir set --------------


def test_p13_header_add_dirs_equal_argv_add_dirs(tmp_path, plans, two_stage_plan, monkeypatch, capsys):
    rw = tmp_path / "rw"
    ro = tmp_path / "ro"
    rw.mkdir()
    ro.mkdir()
    _inject_engine_grants(monkeypatch, [
        {"path": str(rw), "mode": "write", "provenance": "declared"},
        {"path": str(ro), "mode": "read", "provenance": "runtime"},
    ])

    run = _dry_run(capsys, _engine_argv("developer", two_stage_plan))

    assert run.rc == 0
    assert {str(plans), str(rw), str(ro)} <= set(run.add_dirs)
    assert set(run.header_add_dirs) == set(run.add_dirs)
    assert len(run.header_add_dirs) == len(run.add_dirs)


# --- point 14: --project-settings accepts only .claude/settings.local.json ----


@_xfail("14 --project-settings accepts only a path ending in .claude/settings.local.json")
def test_p14_project_settings_only_accepts_settings_local_json(tmp_path, plans, two_stage_plan, capsys):
    payload = json.dumps({"permissions": {"allow": ["Bash(make test:*)"]}})
    good = tmp_path / "proj" / ".claude" / "settings.local.json"
    good.parent.mkdir(parents=True)
    good.write_text(payload)
    bad_paths = [
        tmp_path / "settings.json",
        tmp_path / "proj" / ".claude" / "settings.json",
        tmp_path / "proj" / "settings.local.json",
    ]
    for bad in bad_paths:
        bad.write_text(payload)

    accepted = _dry_run(capsys, _base_argv("developer", two_stage_plan) + ["--project-settings", str(good)])
    assert accepted.rc == 0
    assert "Bash(make test:*)" in accepted.allow

    for bad in bad_paths:
        refused = _dry_run(capsys, _base_argv("developer", two_stage_plan) + ["--project-settings", str(bad)])
        assert refused.rc != 0, bad
        assert "Bash(make test:*)" not in refused.allow


# --- point 15: ledger row carries the child's real session/transcript ---------


def _project_dir_name(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


class _FakeProc:
    def __init__(self, on_communicate, stdout: str):
        self._on_communicate = on_communicate
        self._stdout = stdout
        self.returncode = None

    def communicate(self, input=None, timeout=None):  # noqa: A002 - Popen signature
        self._on_communicate()
        self.returncode = 0
        return self._stdout, ""


@_xfail("15 ledger row names the child transcript under the projects dir of --workdir")
def test_p15_ledger_row_has_real_child_ids_under_workdir_projects(
    tmp_path, plans, two_stage_plan, monkeypatch, capsys,
):
    """`--dry-run` returns before any ledger row is written, so the spawn is
    simulated instead: `claude` is resolved, the launch is stubbed with a fake
    child that writes its transcript under the projects dir of --workdir W,
    while an unrelated fresher transcript appears under the projects dir of the
    launching cwd at the same time."""
    workdir = Path(tempfile.gettempdir()) / f"stage2w{uuid.uuid4().hex}"
    workdir.mkdir()
    try:
        config_root = tmp_path / "cfg"
        projects = config_root / "projects"
        projects.mkdir(parents=True)
        launch = tmp_path / "launch"
        launch.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_root))
        monkeypatch.setenv("CLAUDE_AGENT_HOME", str(config_root))
        monkeypatch.chdir(launch)
        ledger = tmp_path / "spawn-costs.jsonl"
        monkeypatch.setattr(MOD, "COST_LOG", ledger)
        monkeypatch.setattr(MOD, "deregister_child_scope", lambda *a, **kw: None)
        monkeypatch.setattr(MOD, "_build_extraction", lambda *a, **kw: None)
        monkeypatch.setattr(MOD.shutil, "which", lambda name: "/usr/bin/claude")
        monkeypatch.setattr(MOD.proc_tree, "install_teardown", lambda proc: None)
        monkeypatch.setattr(MOD.proc_tree, "kill_tree", lambda proc: None)

        child_sid = str(uuid.uuid4())
        other_sid = str(uuid.uuid4())
        child_transcript = projects / _project_dir_name(str(workdir)) / f"{child_sid}.jsonl"
        other_transcript = projects / _project_dir_name(str(launch)) / f"{other_sid}.jsonl"

        def child_writes_transcripts():
            other_transcript.parent.mkdir(parents=True)
            other_transcript.write_text("{}\n")
            later = other_transcript.stat().st_mtime + 100
            os.utime(other_transcript, (later, later))
            child_transcript.parent.mkdir(parents=True)
            child_transcript.write_text("{}\n")

        stdout = json.dumps({"result": "COMPLETED: done", "session_id": child_sid})
        monkeypatch.setattr(
            MOD.proc_tree, "launch_supervised",
            lambda cmd, **kw: _FakeProc(child_writes_transcripts, stdout),
        )

        argv = [a for a in _base_argv("developer", two_stage_plan) if a != "--dry-run"]
        rc = MOD.main(argv + ["--workdir", str(workdir)])
        capsys.readouterr()

        assert rc == 0
        rows = [json.loads(line) for line in ledger.read_text().splitlines()]
        assert len(rows) == 1
        assert rows[0]["child_session_id"] == child_sid
        assert rows[0]["transcript_path"] == str(child_transcript)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --- point 16: no self-widening in any baseline --------------------------------


def test_p16_no_baseline_rule_is_a_settings_channel_or_claude_spawn(plans):
    for kind, rules in MOD.KIND_BASELINES.items():
        for rule in rules:
            tokens = _bash_tokens(rule)
            if tokens is not None:
                assert not widening_targets.is_settings_channel_program(tokens), (kind, rule)
    for kind in ALL_KINDS + (UNKNOWN_KIND,):
        allow = MOD.build_child_settings(kind, plans)["permissions"]["allow"]
        assert not [r for r in allow if r.startswith("Bash(claude -p")], kind
