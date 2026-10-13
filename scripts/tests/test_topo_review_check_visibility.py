"""A `--review-topo` reviewer, unit or pair, can read the check scripts its plan names.

A plan's `verify_command`, `negative_control` and `[[final_check]]` often run a script
that lives outside the repo; a reviewer that cannot open it reviews the check blind.
`review_check_script_dirs` derives, from the plan alone, the directories of those
scripts, and the spawn grants each one read-only (`--add-dir`, a `Read` allow, an `Edit`
deny) and lists the script paths in the brief. A directory the reviewer must never see
(the plans root, the config root, the home, the temp root, the topo-units root, or any
ancestor of these) is never granted, whatever script sits in it.

The tests reach the grant through the spawn seam (`MOD.main` dry-run), not through the
function, and move every excluded root under `tmp_path` so that a granted directory is
one the test chose.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from argparse import Namespace
from dataclasses import dataclass
from pathlib import Path

import pytest

from test_spawn_topo_review import MOD, DryRun, _base_argv, _dry_run

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
DRIVER_SCRIPT = SCRIPTS_DIR / "plan-review-topological.py"
CHECK_SCRIPTS_HEADING = "## Readable check scripts"
OWN_ENV = "AGENTCTL_OWN_SESSION"
HANDOFF_ENV = "AGENTCTL_NORM_HANDOFF"

PLAN_HEAD = """[meta]
weight_class = "small_change"
task_id = "demo-check-visibility"
goal = "Demonstrate check-script visibility"
done_criterion = "both stages PASSED"
criterion_type = "measurable"

"""
STAGES = """
[[stage]]
index = 1
title = "Scaffold module"
executor = "spawn:developer"
expected_result_image = "module exists"
criterion_type = "measurable"
done_criterion = "the check passes"
depends_on = []
output_artifacts = ["mod.py"]
verify_command = '''{verify}'''
expected_exit = 0

[[stage]]
index = 2
title = "Add tests"
executor = "spawn:developer"
expected_result_image = "tests green"
criterion_type = "measurable"
done_criterion = "the tests pass"
depends_on = [1]
output_artifacts = ["tests/test_mod.py"]
"""


@dataclass
class World:
    root: Path
    home: Path
    agent_home: Path
    plans: Path
    plan_dir: Path
    systmp: Path
    topo: Path
    checks: Path

    def script(self, directory: Path, name: str = "check.sh") -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text("exit 0\n", encoding="utf-8")
        return path

    def plan(self, *finals: str, verify: str = "true") -> Path:
        text = PLAN_HEAD + "".join(
            f"[[final_check]]\ncommand = '''{command}'''\nexpected_exit = 0\n\n" for command in finals
        ) + STAGES.format(verify=verify)
        path = self.plan_dir / "plan.toml"
        path.write_text(text, encoding="utf-8")
        return path


@pytest.fixture
def world(tmp_path, monkeypatch) -> World:
    root = tmp_path.resolve()
    home = root / "home"
    agent_home = home / ".agent"
    layout = World(
        root=root,
        home=home,
        agent_home=agent_home,
        plans=agent_home / "plans",
        plan_dir=root / "planfile",
        systmp=root / "systmp",
        topo=agent_home / "agentctl" / "topo-units",
        checks=root / "checks",
    )
    for directory in (layout.plans, layout.plan_dir, layout.systmp, layout.topo, layout.checks):
        directory.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(tempfile, "tempdir", str(layout.systmp))
    monkeypatch.setattr(MOD, "agent_home", lambda: agent_home, raising=False)
    monkeypatch.setattr(MOD, "plans_dir", lambda: layout.plans)
    monkeypatch.setattr(MOD, "COST_LOG", root / "spawn-costs.jsonl")
    monkeypatch.setenv("AGENTCTL_TOPO_UNITS_DIR", str(layout.topo))
    return layout


def _spawn(capsys, plan: Path, pair: str) -> DryRun:
    result = _dry_run(capsys, _base_argv("thinker", plan) + ["--review-topo", pair])
    assert result.rc == 0, f"setup: the dry-run spawn of {pair} failed: {result.err!r}"
    return result


def _read_dirs(result: DryRun) -> list[Path]:
    """Directories the spawn grants for reading outside the topo-units root."""
    topo = Path(os.environ["AGENTCTL_TOPO_UNITS_DIR"])
    return sorted(
        Path(d) for d in result.add_dirs
        if f"Read(/{d}/**)" in result.allow and topo not in Path(d).parents
    )


def _assert_granted(result: DryRun, directory: Path, who: str) -> None:
    assert directory in _read_dirs(result), (
        f"BEHAVIOUR: {who} is not granted read access to the check-script directory "
        f"{directory}: add-dirs={result.add_dirs}, allow={result.allow}"
    )


def test_pair_reviewer_can_read_check_script_dir(world, capsys):
    script = world.script(world.checks)
    plan = world.plan(verify=f"bash {script} --flag")
    _assert_granted(_spawn(capsys, plan, "2-1"), world.checks, "a pair reviewer")


def test_unit_reviewer_can_read_check_script_dir(world, capsys):
    script = world.script(world.checks)
    plan = world.plan(verify=f"python3 {script} 2 12")
    _assert_granted(_spawn(capsys, plan, "unit:1"), world.checks, "a unit reviewer")


def test_base_unit_reviewer_reads_final_check_script_dir(world, capsys):
    script = world.script(world.checks, "final.sh")
    plan = world.plan(f"sh {script}")
    _assert_granted(_spawn(capsys, plan, "unit:base"), world.checks, "the base unit reviewer")


def test_brief_names_readable_check_scripts(world, capsys):
    script = world.script(world.checks)
    plan = world.plan(verify=f"bash {script}")
    result = _spawn(capsys, plan, "unit:1")
    assert CHECK_SCRIPTS_HEADING in result.prompt, (
        f"BEHAVIOUR: the brief has no `{CHECK_SCRIPTS_HEADING}` section"
    )
    section = result.prompt.split(CHECK_SCRIPTS_HEADING, 1)[1].split("\n## ", 1)[0]
    assert str(script) in section, f"BEHAVIOUR: the brief section does not list {script}"
    assert "read" in section.lower() and "never edit" in section.lower(), (
        f"BEHAVIOUR: the brief section does not grant reading and forbid editing: {section!r}"
    )


def test_check_script_dir_grant_is_read_only(world, capsys):
    script = world.script(world.checks)
    plan = world.plan(verify=f"bash {script}")
    for pair in ("unit:1", "2-1"):
        result = _spawn(capsys, plan, pair)
        rule = f"/{world.checks}/**"
        assert f"Read({rule})" in result.allow and f"Edit({rule})" in result.deny, (
            f"BEHAVIOUR: {pair} lacks the Read allow / Edit deny pair for {world.checks}: "
            f"allow={result.allow}, deny={result.deny}"
        )
        writable = [r for r in result.allow if str(world.checks) in r and not r.startswith("Read(")]
        assert not writable, f"{pair} is granted more than reading {world.checks}: {writable}"


def _driver_module():
    spec = importlib.util.spec_from_file_location("plan_review_topological_visibility", DRIVER_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _check_rules(result: DryRun) -> tuple[list[str], list[str], list[Path]]:
    dirs = _read_dirs(result)
    mentions = lambda rules: sorted(r for r in rules if any(str(d) in r for d in dirs))  # noqa: E731
    return mentions(result.allow), mentions(result.deny), dirs


def test_same_grant_for_root_and_linked_child_driver(world, monkeypatch, capsys):
    script = world.script(world.checks)
    plan = world.plan(verify=f"bash {script}")
    driver = _driver_module()
    seen = {}
    for who, env in (
        ("root", {"AGENT_RECURSION_DEPTH": "0"}),
        ("child", {
            OWN_ENV: "fd5140d5-1983-4bd3-9f4c-1558815f5213",
            HANDOFF_ENV: "/nowhere/h.json:" + "0" * 64,
            "AGENT_RECURSION_DEPTH": "1",
        }),
    ):
        for name in (OWN_ENV, HANDOFF_ENV):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        argv = driver.build_spawn_argv(
            Namespace(model=None, complexity="high"), "unit:1", str(plan), dry_run=True,
        )
        assert argv.count("--dry-run") == 1, f"setup: the driver's {who} argv is not a dry-run"
        result = DryRun(*_run_main(capsys, argv))
        seen[who] = _check_rules(result)
    assert seen["root"][2], f"BEHAVIOUR: the root driver's reviewer reads no check-script directory"
    assert seen["root"] == seen["child"], (
        f"BEHAVIOUR: the root and the linked child driver grant different check-script rules: {seen}"
    )


def _run_main(capsys, argv):
    try:
        rc = MOD.main(argv)
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else 1
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _granted_among(world, capsys, *scripts: Path) -> list[Path]:
    """Directories granted when each script is the plan's final check, beside the one control
    script in a directory that is never excluded."""
    control = world.script(world.checks, "control.sh")
    plan = world.plan(f"bash {control}", *[f"bash {s}" for s in scripts])
    result = _spawn(capsys, plan, "unit:base")
    _assert_granted(result, world.checks, "a reviewer, for a script in an ordinary directory")
    return _read_dirs(result)


def test_plans_root_never_granted_to_topo_reviewer(world, capsys):
    in_plans = world.script(world.plans)
    in_plan_dir = world.script(world.plan_dir)
    via_dotdot = Path(f"{world.checks}/../home/.agent/plans/dotdot.sh")
    world.script(world.plans, "dotdot.sh")
    link = world.checks / "link.sh"
    link.symlink_to(world.script(world.plans, "target.sh"))
    ordinary_below = world.script(world.plans / "task-checks", "below.sh")
    granted = _granted_among(world, capsys, in_plans, in_plan_dir, via_dotdot, link)
    _assert_not_granted_all(world, granted, [world.plans, world.plan_dir])
    assert world.plans / "task-checks" not in granted
    below = _granted_among(world, capsys, ordinary_below)
    assert world.plans / "task-checks" in below, (
        "BEHAVIOUR: a checks directory below the plans root is withheld with the root"
    )


def _assert_not_granted_all(world, granted: list[Path], excluded: list[Path]) -> None:
    leaked = [d for d in excluded if d in granted]
    assert not leaked, f"BEHAVIOUR: withheld directories were granted: {leaked}"


def test_agent_home_or_home_never_granted(world, capsys):
    scripts = [
        world.script(world.home),
        world.script(world.agent_home),
        world.script(world.agent_home / "skills"),
    ]
    granted = _granted_among(world, capsys, *scripts)
    _assert_not_granted_all(world, granted, [world.home, world.agent_home, world.agent_home / "skills"])
    deeper = world.script(world.agent_home / "skills" / "mine")
    assert world.agent_home / "skills" / "mine" in _granted_among(world, capsys, deeper), (
        "BEHAVIOUR: a grandchild of the config root is withheld with its direct children"
    )


def test_temp_root_never_granted(world, capsys):
    granted = _granted_among(world, capsys, world.script(world.systmp))
    _assert_not_granted_all(world, granted, [world.systmp])
    nested = world.script(world.systmp / "run")
    assert world.systmp / "run" in _granted_among(world, capsys, nested), (
        "BEHAVIOUR: a directory below the temp root is withheld with the root"
    )


def test_topo_units_dir_and_parent_never_granted(world, capsys):
    scripts = [world.script(world.topo), world.script(world.topo.parent)]
    granted = _granted_among(world, capsys, *scripts)
    _assert_not_granted_all(world, granted, [world.topo, world.topo.parent])


def test_ancestor_of_excluded_root_never_granted(world, capsys):
    ancestor_of_all = world.script(world.root)
    granted = _granted_among(world, capsys, ancestor_of_all)
    _assert_not_granted_all(world, granted, [world.root])


def test_non_script_check_grants_nothing_new(world, capsys):
    control = world.script(world.checks, "control.sh")
    others = [world.script(world.root / f"other{i}") for i in range(6)]
    plan = world.plan(
        f"bash {control}",
        f"echo {others[0]}",
        f"cat {others[1]}",
        f"bash {others[2]} && true",
        f"bash {world.root}/other3/missing.sh",
        "bash scripts/relative.sh",
        f"python3 -c 'print(1)' {others[4]}",
        f"grep -q marker {others[5]}",
        verify="true",
    )
    granted = _read_dirs(_spawn(capsys, plan, "unit:base"))
    assert world.checks in granted, "BEHAVIOUR: the control script's directory is not granted"
    assert granted == [world.checks], (
        f"BEHAVIOUR: a check that is not a single script call widened the grant: {granted}"
    )
