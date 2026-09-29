"""Spawn-level coverage of `--review-topo <n|order>`: refusal paths, the
`--dry-run` TOPO-VIEW line (and its no-write guarantee), a real materialized
invocation's view directory and permission grants, and the ceiling-refusal
message naming both the flag and the planned whole-plan driver.

Pure-`PlanDoc`-level coverage of the underlying reliance/rendering helpers
lives in `test_topo_review_bundle.py`; this file drives `spawn-specialist.py`
end to end (module-loading/argv/DryRun pattern mirrors
`test_spawn_stage2_image.py`).
"""
from __future__ import annotations

import importlib.util
import json
import shlex
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS_DIR / "spawn-specialist.py"


def _load():
    spec = importlib.util.spec_from_file_location("spawn_specialist_topo_review", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()


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


def _dry_run(capsys, argv: list[str]) -> DryRun:
    try:
        rc = MOD.main(argv)
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else 1
    captured = capsys.readouterr()
    return DryRun(rc, captured.out, captured.err)


@pytest.fixture
def plans(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(MOD, "plans_dir", lambda: directory)
    return directory


@pytest.fixture
def two_stage_plan(fixtures_dir) -> Path:
    return fixtures_dir / "plan_two_stage.toml"


# --- refusal paths ------------------------------------------------------


def test_refused_with_a_non_thinker_kind(capsys, plans, two_stage_plan):
    result = _dry_run(capsys, _base_argv("developer", two_stage_plan) + ["--review-topo", "1"])
    assert result.rc == 2
    assert "--kind thinker" in result.err


def test_refused_combined_with_stage_index(capsys, plans, two_stage_plan):
    argv = _base_argv("thinker", two_stage_plan) + [
        "--review-topo", "1", "--stage-index", "1",
    ]
    result = _dry_run(capsys, argv)
    assert result.rc == 2
    assert "--stage-index" in result.err


def test_refused_combined_with_plan_brief(capsys, plans, two_stage_plan):
    argv = _base_argv("thinker", two_stage_plan) + ["--review-topo", "1", "--plan-brief"]
    result = _dry_run(capsys, argv)
    assert result.rc == 2
    assert "--plan-brief" in result.err


def test_refused_on_an_unparseable_unit(capsys, plans, two_stage_plan):
    argv = _base_argv("thinker", two_stage_plan) + ["--review-topo", "not-a-unit"]
    result = _dry_run(capsys, argv)
    assert result.rc == 2
    assert "must be an integer stage index or 'order'" in result.err


def test_refused_on_an_unknown_stage_index(capsys, plans, two_stage_plan):
    argv = _base_argv("thinker", two_stage_plan) + ["--review-topo", "99"]
    result = _dry_run(capsys, argv)
    assert result.rc == 2
    assert "no such stage index" in result.err


def test_refused_on_order_when_the_plan_declares_no_order_block(capsys, plans, two_stage_plan):
    argv = _base_argv("thinker", two_stage_plan) + ["--review-topo", "order"]
    result = _dry_run(capsys, argv)
    assert result.rc == 2
    assert "declares no [order] block" in result.err


# --- --dry-run: prints TOPO-VIEW, writes nothing to disk ------------------


def test_dry_run_prints_topo_view_line_and_writes_nothing(capsys, plans, two_stage_plan):
    argv = _base_argv("thinker", two_stage_plan) + ["--review-topo", "2"]
    result = _dry_run(capsys, argv)
    assert result.rc == 0
    topo_root = plans / "_topo_review"
    assert f"TOPO-VIEW: {topo_root}" in result.out
    assert "files=2" in result.out  # own.md + stage 1 (2's sole first-hop neighbour)
    assert not topo_root.exists()


def test_dry_run_prompt_carries_the_topo_bundle_not_the_whole_plan(capsys, plans, two_stage_plan):
    argv = _base_argv("thinker", two_stage_plan) + ["--review-topo", "2"]
    result = _dry_run(capsys, argv)
    assert "## Working plan — topological review unit" in result.prompt
    assert "## Working plan — stage" not in result.prompt
    assert "# Topological review unit: 2" in result.prompt


def test_dry_run_add_dir_is_the_view_directory_not_plans_directory(capsys, plans, two_stage_plan):
    argv = _base_argv("thinker", two_stage_plan) + ["--review-topo", "2"]
    result = _dry_run(capsys, argv)
    assert str(plans) not in result.add_dirs
    assert any(d.endswith("/view-2") for d in result.add_dirs)


def test_dry_run_permissions_grant_read_only_view_directory(capsys, plans, two_stage_plan):
    argv = _base_argv("thinker", two_stage_plan) + ["--review-topo", "2"]
    result = _dry_run(capsys, argv)
    view_dir = [d for d in result.add_dirs if d.endswith("/view-2")][0]
    assert any(view_dir in rule and rule.startswith("Read(") for rule in result.allow)
    assert any(view_dir in rule and rule.startswith("Edit(") for rule in result.deny)


# --- a real (non-dry-run) invocation materializes the view directory ------


def test_non_dry_run_materializes_the_expected_view_directory(capsys, plans, two_stage_plan, monkeypatch):
    # Force the ceiling check to refuse right after materialization, so the
    # test never needs to stub a real `claude` child process -- the view
    # directory write already happened by the time this refusal fires.
    monkeypatch.setattr(MOD, "dispatch_prompt_ceiling_chars", lambda model: 1)
    argv = [a for a in _base_argv("thinker", two_stage_plan) if a != "--dry-run"]
    argv += ["--review-topo", "2"]
    rc = MOD.main(argv)
    captured = capsys.readouterr()
    assert rc == 5
    assert "exceeding the" in captured.err
    plan_sha = MOD.hashlib.sha256(two_stage_plan.read_bytes()).hexdigest()
    view_dir = plans / "_topo_review" / plan_sha / "view-2"
    assert view_dir.is_dir()
    assert {p.name for p in view_dir.iterdir()} == {"own.md", "1.md"}


# --- ceiling-refusal message names both the flag and the planned driver ---


def test_ceiling_refusal_names_review_topo_and_the_planned_driver(capsys, plans, two_stage_plan, monkeypatch):
    monkeypatch.setattr(MOD, "dispatch_prompt_ceiling_chars", lambda model: 1)
    argv = _base_argv("thinker", two_stage_plan)  # no --review-topo at all
    result = _dry_run(capsys, argv)
    assert result.rc == 5
    assert "--review-topo <n|order>" in result.err
    assert "scripts/plan-review-topological.py" in result.err


# --- the "order" unit end to end (dry-run) --------------------------------


def test_dry_run_order_unit_materializes_from_a_plan_with_an_order_block(capsys, plans, tmp_path):
    plan_path = tmp_path / "order_plan.toml"
    plan_path.write_text(
        '[meta]\n'
        'task_id = "order-demo"\n'
        '[meta.order]\n'
        'requirements = [{id = "R1", text = "r1"}]\n'
        '[meta.order.coverage]\n'
        'R1 = ["1", "2"]\n'
        '[[stage]]\n'
        'index = 1\ntitle = "s1"\nexecutor = "in_thread"\n'
        'expected_result_image = "img"\ndone_criterion = "dc"\n'
        'means = "Edit"\nmethod = "do"\nverify_command = "true"\n'
        'output_artifacts = ["a.txt"]\n'
        '[[stage]]\n'
        'index = 2\ntitle = "s2"\nexecutor = "in_thread"\n'
        'expected_result_image = "img"\ndone_criterion = "dc"\n'
        'means = "Edit"\nmethod = "do"\nverify_command = "true"\n',
        encoding="utf-8",
    )
    argv = _base_argv("thinker", plan_path) + ["--review-topo", "order"]
    result = _dry_run(capsys, argv)
    assert result.rc == 0
    assert "files=3" in result.out  # own.md + stage 1 + stage 2
    assert "# Topological review unit: order" in result.prompt
    assert any(d.endswith("/view-order") for d in result.add_dirs)
