"""Spawn-level coverage of `--review-topo <pair>`: refusal paths, the
`--dry-run` TOPO-VIEW line (and its no-write guarantee), a real materialized
invocation's view directory and permission grants, and the ceiling-refusal
message naming both the flag and the planned whole-plan driver.

Pure-`PlanDoc`-level coverage of the underlying reliance/rendering helpers
lives in `test_topo_review_bundle.py`; this file drives `spawn-specialist.py`
end to end (module-loading/argv/DryRun pattern mirrors
`test_spawn_stage2_image.py`). The two-stage fixture's stage 2 relies on
stage 1, so its only pair is `2-1` (its units are `unit:base`, `unit:1`,
`unit:2`); a plan whose order coverage names a stage adds `base-<s>` pairs.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shlex
from pathlib import Path

import pytest

from agentctl.plan import load_plan_with_digest
from agentctl.render import render_pair_review_bundle, render_unit_review_bundle

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS_DIR / "spawn-specialist.py"
TOPO_PLAN_LABEL = (
    "## Working plan — topological review unit or pair "
    "(projected; the full plan is never inlined for --review-topo — "
    "see § File-access scope for the per-pair view directory)"
)
DONE_HEADING = "## Done criterion for this step"


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


def _plan_sha(plan: Path) -> str:
    return hashlib.sha256(plan.read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def cost_log(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "spawn-costs.jsonl"
    monkeypatch.setattr(MOD, "COST_LOG", path)
    return path


@pytest.fixture
def topo_units_dir(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "topo-units"
    monkeypatch.setenv("AGENTCTL_TOPO_UNITS_DIR", str(directory))
    return directory


@pytest.fixture
def two_stage_plan(fixtures_dir) -> Path:
    return fixtures_dir / "plan_two_stage.toml"


@pytest.fixture
def plans_dir_patch(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(MOD, "plans_dir", lambda: directory)
    return directory


class _FakeProc:
    def __init__(self) -> None:
        self.returncode = 0
        self.pid = 424242

    def communicate(self, input=None):
        return ('{"result": "COMPLETED: ok", "cost_usd": 0}', "")


def _stub_child_launch(monkeypatch, tmp_path, on_launch=lambda cmd: None) -> list[dict]:
    """Stub every real-process/side-channel seam of a non-dry-run spawn; returns
    the list the cost-log rows are appended to."""
    logged: list[dict] = []

    def fake_launch(cmd, **kwargs):
        on_launch(cmd)
        return _FakeProc()

    monkeypatch.setattr(MOD.proc_tree, "launch_supervised", fake_launch)
    monkeypatch.setattr(MOD.proc_tree, "install_teardown", lambda p: None)
    monkeypatch.setattr(MOD.proc_tree, "kill_tree", lambda p: None)
    monkeypatch.setattr(MOD, "_snapshot_transcripts", lambda *a, **k: set())
    monkeypatch.setattr(MOD, "_discover_transcript_path", lambda *a, **k: None)
    monkeypatch.setattr(MOD, "permissions_digest", lambda *a, **k: "")
    monkeypatch.setattr(MOD, "deregister_child_scope", lambda *a, **k: None)
    monkeypatch.setattr(MOD, "log_cost_entry", lambda entry: logged.append(entry))
    monkeypatch.setattr(MOD.shutil, "which", lambda name: "/usr/bin/claude")
    sysprompt = tmp_path / "sysprompt.md"
    sysprompt.write_text("system prompt", encoding="utf-8")
    monkeypatch.setattr(MOD, "composed_system_prompt_file", lambda skill: sysprompt)
    return logged


def test_ts5_refused_with_a_non_thinker_kind(capsys, topo_units_dir, two_stage_plan):
    result = _dry_run(capsys, _base_argv("developer", two_stage_plan) + ["--review-topo", "2-1"])
    assert result.rc == 2
    assert "--kind thinker" in result.err


def test_ts5_refused_combined_with_stage_index(capsys, topo_units_dir, two_stage_plan):
    argv = _base_argv("thinker", two_stage_plan) + [
        "--review-topo", "2-1", "--stage-index", "1",
    ]
    result = _dry_run(capsys, argv)
    assert result.rc == 2
    assert "--stage-index" in result.err


def test_ts5_accepted_combined_with_plan_brief(capsys, topo_units_dir, two_stage_plan):
    # --review-topo replaces the whole-plan/brief projection outright, so it
    # never checks --stage-index eligibility; --plan-brief is accordingly a
    # harmless no-op alongside it, not a refusal.
    argv = _base_argv("thinker", two_stage_plan) + ["--review-topo", "2-1", "--plan-brief"]
    result = _dry_run(capsys, argv)
    assert result.rc == 0


def test_ts5_argparse_error_on_review_stages(capsys, topo_units_dir, two_stage_plan):
    argv = _base_argv("thinker", two_stage_plan) + ["--review-stages", "1"]
    with pytest.raises(SystemExit) as excinfo:
        MOD.main(argv)
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert "unrecognized arguments" in captured.err


def test_ts5_refused_on_an_unparseable_pair(capsys, topo_units_dir, two_stage_plan):
    argv = _base_argv("thinker", two_stage_plan) + ["--review-topo", "not-a-pair"]
    result = _dry_run(capsys, argv)
    assert result.rc == 2
    assert "not-a-pair" in result.err
    assert not topo_units_dir.exists()


def test_ts5_refused_on_a_comma_list_naming_pairs(capsys, topo_units_dir, two_stage_plan):
    argv = _base_argv("thinker", two_stage_plan) + ["--review-topo", "2-1,plan-2"]
    result = _dry_run(capsys, argv)
    assert result.rc == 2
    assert "--pairs" in result.err
    assert not topo_units_dir.exists()


@pytest.mark.parametrize("pair", ["1-2", "plan-1", "9-1", "base-plan"])
def test_ts5_refused_on_a_pair_the_plan_does_not_have(capsys, topo_units_dir, two_stage_plan, pair):
    argv = _base_argv("thinker", two_stage_plan) + ["--review-topo", pair]
    result = _dry_run(capsys, argv)
    assert result.rc == 2
    assert pair in result.err
    assert not topo_units_dir.exists()


@pytest.mark.parametrize("unit_form", ["2", "order"])
def test_ts5_refused_on_a_unit_form_id(capsys, topo_units_dir, two_stage_plan, unit_form):
    argv = _base_argv("thinker", two_stage_plan) + ["--review-topo", unit_form]
    result = _dry_run(capsys, argv)
    assert result.rc == 2
    assert unit_form in result.err
    assert not topo_units_dir.exists()


@pytest.fixture
def order_plan(tmp_path) -> Path:
    """A plan whose order's coverage names stage 1 and stage 2 through the
    stage-addressed `verify_command` control -- each an ordinary edge of
    `unit:base`, so the plan's pairs are `base-1`, `base-2` and `2-1`."""
    plan_path = tmp_path / "order_plan.toml"
    plan_path.write_text(
        '[meta]\n'
        'task_id = "order-demo"\n'
        '[meta.order]\n'
        'requirements = [{id = "R1", text = "r1"}]\n'
        '[meta.order.coverage]\n'
        'R1 = ["stage 1 verify_command", "stage 2 verify_command"]\n'
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
    return plan_path


def test_ts6_dry_run_base_stage_pair_materializes_from_a_plan_with_an_order_block(capsys, topo_units_dir, order_plan):
    # E5: a coverage entry is an ordinary typed edge of `unit:base`, reviewed as the
    # `base-<s>` pair; `base-plan` / `plan-<s>` no longer exist.
    argv = _base_argv("thinker", order_plan) + ["--review-topo", "base-1"]
    result = _dry_run(capsys, argv)
    assert result.rc == 0
    view_dir = topo_units_dir / _plan_sha(order_plan) / "view-base-1"
    assert f"TOPO-VIEW: {view_dir} files=stage-1.md" in result.out.splitlines()
    assert "# Topological review pair: base-1" in result.prompt
    assert [d for d in result.add_dirs if d.startswith(str(topo_units_dir))] == [str(view_dir)]


@pytest.mark.parametrize("retired", ["base-plan", "plan-1", "plan-2"])
def test_ts6_retired_pair_ids_are_refused_even_with_an_order_block(capsys, topo_units_dir, order_plan, retired):
    result = _dry_run(capsys, _base_argv("thinker", order_plan) + ["--review-topo", retired])
    assert result.rc == 2
    assert retired in result.err
    assert not topo_units_dir.exists()


@pytest.mark.parametrize("unit", ["unit:base", "unit:1", "unit:2"])
def test_ts6_dry_run_unit_bundle_is_self_contained_with_no_view_directory(capsys, topo_units_dir, order_plan, plans_dir_patch, unit):
    argv = _base_argv("thinker", order_plan) + ["--review-topo", unit]
    result = _dry_run(capsys, argv)
    assert result.rc == 0
    # No view directory is named, granted or written for a unit.
    assert [line for line in result.out.splitlines() if line.startswith("TOPO-VIEW:")] == [
        "TOPO-VIEW: none files=",
    ]
    assert f"# Topological review unit: {unit}" in result.prompt
    assert "# Topological review pair:" not in result.prompt
    assert [d for d in result.add_dirs if d.startswith(str(topo_units_dir))] == []
    assert str(plans_dir_patch) not in result.add_dirs
    assert not any(topo_units_dir.name in rule for rule in result.allow)
    assert not any(str(plans_dir_patch) in rule for rule in result.allow)
    assert not topo_units_dir.exists()


def test_ts6_unit_bundle_body_is_exactly_the_unit_bundle(capsys, topo_units_dir, order_plan):
    argv = _base_argv("thinker", order_plan) + ["--review-topo", "unit:1"]
    result = _dry_run(capsys, argv)
    assert result.rc == 0
    doc, _, sha = load_plan_with_digest(order_plan)
    body = result.prompt.split(f"\n{TOPO_PLAN_LABEL}\n\n", 1)[1]
    body = body.split(f"\n\n{DONE_HEADING}\n", 1)[0]
    assert body == render_unit_review_bundle(doc, "unit:1", plan_sha256=sha)


@pytest.mark.parametrize("unit", ["unit:plan", "unit:9", "unit:"])
def test_ts6_refused_on_a_unit_the_plan_does_not_have(capsys, topo_units_dir, order_plan, unit):
    result = _dry_run(capsys, _base_argv("thinker", order_plan) + ["--review-topo", unit])
    assert result.rc == 2
    assert unit in result.err
    assert not topo_units_dir.exists()


def test_ts6_unit_real_spawn_grants_no_view_directory_and_writes_no_tree(capsys, topo_units_dir, order_plan, monkeypatch, tmp_path):
    seen: list[list[str]] = []
    _stub_child_launch(monkeypatch, tmp_path, on_launch=seen.append)
    argv = [a for a in _base_argv("thinker", order_plan) if a != "--dry-run"]
    argv += ["--review-topo", "unit:base"]
    assert MOD.main(argv) == 0
    assert len(seen) == 1
    cmd = seen[0]
    add_dirs = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "--add-dir"]
    assert [d for d in add_dirs if d.startswith(str(topo_units_dir))] == []
    assert not topo_units_dir.exists()


def test_ts1_dry_run_plan_body_is_exactly_the_topo_bundle(capsys, topo_units_dir, two_stage_plan, tmp_path):
    dossier = tmp_path / "dossier.md"
    dossier.write_text("dossier-sentinel", encoding="utf-8")
    argv = _base_argv("thinker", two_stage_plan) + [
        "--review-topo", "2-1",
        "--constraints", "constraint-sentinel",
        "--context-dossier", str(dossier),
    ]
    result = _dry_run(capsys, argv)
    assert result.rc == 0

    doc, _, sha = load_plan_with_digest(two_stage_plan)
    view_dir = topo_units_dir / sha / "view-2-1"
    expected_bundle = render_pair_review_bundle(doc, "2-1", plan_sha256=sha, view_dir=view_dir)
    body = result.prompt.split(f"\n{TOPO_PLAN_LABEL}\n\n", 1)[1]
    body = body.split(f"\n\n{DONE_HEADING}\n", 1)[0]
    assert body == expected_bundle

    lines = result.prompt.splitlines()
    assert any(line.startswith("AGENT_RECURSION_DEPTH=") for line in lines)
    for heading in (
        "## Constraints",
        "## Context dossier (what you may not infer from CLAUDE.md / repo / memory)",
        "## File-access scope",
    ):
        assert heading in lines
    assert "constraint-sentinel" in lines
    assert "dossier-sentinel" in lines
    assert "[[stage]]" not in result.prompt


def test_ts1_envelope_is_unchanged_only_the_plan_section_is_replaced(two_stage_plan, tmp_path):
    dossier = tmp_path / "dossier.md"
    dossier.write_text("dossier-sentinel", encoding="utf-8")
    args = MOD.build_parser().parse_args(
        [a for a in _base_argv("thinker", two_stage_plan) if a != "--dry-run"]
        + ["--constraints", "constraint-sentinel", "--context-dossier", str(dossier)]
    )
    kwargs = {
        "workdir": "/tmp/wd",
        "permission_mode": "default",
        "add_dir_paths": ["/tmp/view-2-1"],
    }
    bundle = "# Topological review pair: 2-1\n\nbundle-sentinel"
    plain = MOD.assemble_prompt(args, 3, "perm-sentinel", **kwargs)
    topo = MOD.assemble_prompt(args, 3, "perm-sentinel", topo_bundle=bundle, **kwargs)

    plan_text = MOD.argv_text.read_required_file(args.plan, "--plan")
    whole_plan_section = f"\n## Working plan\n\n{plan_text}\n\n{DONE_HEADING}\n"
    assert plain.count(whole_plan_section) == 1
    assert topo == plain.replace(
        whole_plan_section, f"\n{TOPO_PLAN_LABEL}\n\n{bundle}\n\n{DONE_HEADING}\n"
    )


def test_ts1_file_access_scope_names_the_view_directory_not_plans_dir(capsys, topo_units_dir, two_stage_plan, plans_dir_patch):
    argv = _base_argv("thinker", two_stage_plan) + ["--review-topo", "2-1"]
    result = _dry_run(capsys, argv)
    scope_section = result.prompt.split("## File-access scope", 1)[1]
    view_dir = topo_units_dir / _plan_sha(two_stage_plan) / "view-2-1"
    assert str(plans_dir_patch) not in scope_section
    assert f"- Additional directory: `{view_dir}`" in scope_section.splitlines()


def test_ts2_exactly_one_add_dir_is_the_view_directory_not_plans_directory(capsys, topo_units_dir, plans_dir_patch, two_stage_plan):
    argv = _base_argv("thinker", two_stage_plan) + ["--review-topo", "2-1"]
    result = _dry_run(capsys, argv)
    view_dir = topo_units_dir / _plan_sha(two_stage_plan) / "view-2-1"
    assert str(plans_dir_patch) not in result.add_dirs
    assert [d for d in result.add_dirs if d.startswith(str(topo_units_dir))] == [str(view_dir)]


def test_ts2_permissions_grant_read_only_view_directory_no_plans_rules(capsys, topo_units_dir, plans_dir_patch, two_stage_plan):
    argv = _base_argv("thinker", two_stage_plan) + ["--review-topo", "2-1"]
    result = _dry_run(capsys, argv)
    view_dir = str(topo_units_dir / _plan_sha(two_stage_plan) / "view-2-1")
    assert any(view_dir in rule and rule.startswith("Read(") for rule in result.allow)
    assert any(view_dir in rule and rule.startswith("Edit(") for rule in result.deny)
    assert not any(str(plans_dir_patch) in rule for rule in result.allow)
    assert not any(str(plans_dir_patch) in rule for rule in result.deny)
    # plans_permission_rules' own shasum grant (paired 1:1 with its Read grant)
    # is absent -- distinct from any baseline shasum rule a kind may carry.
    assert not any("shasum -a 256" in rule for rule in result.allow)


def test_ts7_dry_run_prints_the_exact_topo_view_line(capsys, topo_units_dir, two_stage_plan):
    argv = _base_argv("thinker", two_stage_plan) + ["--review-topo", "2-1"]
    result = _dry_run(capsys, argv)
    assert result.rc == 0
    view_dir = topo_units_dir / _plan_sha(two_stage_plan) / "view-2-1"
    assert [line for line in result.out.splitlines() if line.startswith("TOPO-VIEW:")] == [
        f"TOPO-VIEW: {view_dir} files=stage-1.md",
    ]


def test_tb14_dry_run_writes_nothing(capsys, topo_units_dir, two_stage_plan, cost_log):
    argv = _base_argv("thinker", two_stage_plan) + ["--review-topo", "2-1"]
    result = _dry_run(capsys, argv)
    assert result.rc == 0
    assert not topo_units_dir.exists()
    assert not cost_log.exists()


def test_ts7_view_directory_is_materialized_before_the_child_launches(capsys, topo_units_dir, two_stage_plan, monkeypatch, tmp_path):
    view_dir = topo_units_dir / _plan_sha(two_stage_plan) / "view-2-1"
    seen_at_launch: list[set[str]] = []

    def check_view_dir(cmd):
        assert cmd[cmd.index("--add-dir") + 1] == str(view_dir)
        seen_at_launch.append({p.name for p in view_dir.iterdir()})

    _stub_child_launch(monkeypatch, tmp_path, on_launch=check_view_dir)
    argv = [a for a in _base_argv("thinker", two_stage_plan) if a != "--dry-run"]
    argv += ["--review-topo", "2-1"]
    assert MOD.main(argv) == 0
    assert seen_at_launch == [{"stage-1.md"}]


def test_ts3_oversize_bundle_refused_pre_spawn_naming_split_and_override(capsys, topo_units_dir, two_stage_plan, monkeypatch):
    monkeypatch.setattr(MOD, "dispatch_prompt_ceiling_chars", lambda model: 1)
    argv = _base_argv("thinker", two_stage_plan) + ["--review-topo", "2-1"]
    rc = MOD.main(argv)
    err = capsys.readouterr().err
    assert rc == 5
    assert "split" in err
    assert "override" in err
    assert "never raises the ceiling" in err


def test_ts4_ceiling_is_216000_chars_144000_tokens_for_every_model(topo_units_dir):
    for model in ("opus", "sonnet", "haiku"):
        assert MOD.dispatch_prompt_ceiling_tokens(model) == 144_000
        assert MOD.dispatch_prompt_ceiling_chars(model) == 216_000


def test_ts9_whole_plan_ceiling_refusal_names_review_topo_and_the_planned_driver(capsys, topo_units_dir, two_stage_plan, monkeypatch):
    monkeypatch.setattr(MOD, "dispatch_prompt_ceiling_chars", lambda model: 1)
    argv = _base_argv("thinker", two_stage_plan)  # no --review-topo at all
    result = _dry_run(capsys, argv)
    assert result.rc == 5
    assert "--review-topo <pair>" in result.err
    assert "scripts/plan-review-topological.py" in result.err
    assert "planned" in result.err


def test_ts9_review_topo_ceiling_refusal_log_row_carries_pair_and_digest(capsys, topo_units_dir, two_stage_plan, monkeypatch):
    logged: list[dict] = []
    monkeypatch.setattr(MOD, "log_refused", lambda reason, fields: logged.append({"reason": reason, **fields}))
    monkeypatch.setattr(MOD, "dispatch_prompt_ceiling_chars", lambda model: 1)
    argv = _base_argv("thinker", two_stage_plan) + ["--review-topo", "2-1"]
    result = _dry_run(capsys, argv)
    assert result.rc == 5
    rows = [row for row in logged if row["reason"] == "prompt-too-large"]
    assert len(rows) == 1
    assert rows[0]["review_pair"] == "2-1"
    assert rows[0]["plan_sha256"] == _plan_sha(two_stage_plan)
    assert "review_topo_unit" not in rows[0]


def test_ts8_cost_log_row_carries_review_pair_and_plan_sha256(capsys, topo_units_dir, two_stage_plan, monkeypatch, tmp_path):
    logged = _stub_child_launch(monkeypatch, tmp_path)
    argv = [a for a in _base_argv("thinker", two_stage_plan) if a != "--dry-run"]
    argv += ["--review-topo", "2-1"]
    assert MOD.main(argv) == 0
    assert len(logged) == 1
    assert logged[0]["review_pair"] == "2-1"
    assert logged[0]["plan_sha256"] == _plan_sha(two_stage_plan)
    assert "review_topo_unit" not in logged[0]


def test_ts8_ordinary_spawn_cost_log_row_carries_none_for_both_fields(capsys, topo_units_dir, two_stage_plan, monkeypatch, tmp_path):
    logged = _stub_child_launch(monkeypatch, tmp_path)
    argv = [a for a in _base_argv("developer", two_stage_plan) if a != "--dry-run"]
    assert MOD.main(argv) == 0
    assert len(logged) == 1
    assert logged[0]["review_pair"] is None
    assert logged[0]["plan_sha256"] is None
