"""End-to-end coverage that a REAL `agentctl` session's stage grants actually
reach a spawned child's `--dry-run` output through `spawn-specialist.py`'s own
`main()` entrypoint -- not just through the pure `load_engine_stage_grants`/
`build_child_settings`/`stage_grant_*` unit calls test_spawn_specialist_grants.py
already covers, and not just through `cli.cmd_stage_grants`/`cmd_grant_stats`/
`cmd_resolve_permission` directly the way test_stage_grants.py's own section (4)
already does (declared-through-submit-approve, derived DR-O, runtime once/stage
scope, grant-stats zero counts, and PERMISSION-REQUEST -> materialization_defect
routing are all exercised there already -- duplicating them here would just be
re-testing agentctl's own cli.py through a second, more expensive seam).

What's missing everywhere else, and what this file adds: driving a real session
to EXECUTING via classify -> submit_plan -> approve -> partition -> next_stage
(the same `_to_executing` fixture flow), then invoking
`spawn-specialist.py --session <sid> --stage-index <n> --kind <kind> --dry-run`
as `main()` actually would be invoked in production, and asserting the printed
`--settings` JSON / `--add-dir` argv / prompt "## File-access scope" section
reflect the stage's real declared and runtime grants, with the correct kind
gating (a spawn whose --kind doesn't match the stage's own declared executor
gets no engine grants at all, per `load_engine_stage_grants`'s own contract).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from test_stage_grants import _to_executing, _write_declared_grants_plan, ns
from agentctl import cli

SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"


def _load():
    spec = importlib.util.spec_from_file_location("spawn_specialist_grants_integration", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()


def _dry_run_argv(sid: str, kind: str, stage_index: int, state_root: Path, plan: Path) -> list[str]:
    return [
        "--kind", kind,
        "--plan", str(plan),
        "--done-criterion", "tests green",
        "--criterion-type", "measurable",
        "--complexity", "medium",
        "--effort", "high",
        "--session", sid,
        "--stage-index", str(stage_index),
        "--state-root", str(state_root),
        "--dry-run",
    ]


def _command_section(out: str) -> str:
    """The `=== command (not executed) ===` half of --dry-run's stdout --
    isolated from the `=== assembled prompt ===` half, which inlines the
    WHOLE plan file's raw text (including any `[stage.grants]` TOML block),
    so a bare substring check against the full output would false-positive
    on the plan's own source text rather than the materialized --settings
    JSON this file means to assert on."""
    return out.split("=== command (not executed) ===", 1)[1]


def test_dry_run_reflects_a_declared_grant_for_the_matching_kind(
    store, fixtures_dir, tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(MOD, "plans_dir", lambda: tmp_path)
    sid = "dry-run-declared-grant-match"
    plan_path = _write_declared_grants_plan(fixtures_dir, tmp_path)
    _to_executing(store, sid, fixtures_dir, plan_path=plan_path)

    rc = MOD.main(_dry_run_argv(sid, "developer", 1, tmp_path / "state", Path(plan_path)))
    assert rc == 0
    out = capsys.readouterr().out
    # the declared rule reaches the --settings JSON embedded in the printed cmd
    assert '"Bash(git status:*)"' in _command_section(out)
    # ... and its provenance reaches the prompt's File-access scope section
    assert "Stage grants (provenance):" in out
    assert "- `Bash(git status:*)` — declared" in out


def test_dry_run_omits_engine_grants_for_a_kind_that_is_not_the_stages_executor(
    store, fixtures_dir, tmp_path, monkeypatch, capsys,
):
    """Stage 1's executor is "spawn:developer" (plan_two_stage.toml). A thinker
    spawn dispatched against the same stage/session must fall back to
    KIND_BASELINES-only settings -- the declared grant must not leak to a kind
    the plan never authorized to receive it."""
    monkeypatch.setattr(MOD, "plans_dir", lambda: tmp_path)
    sid = "dry-run-kind-mismatch"
    plan_path = _write_declared_grants_plan(fixtures_dir, tmp_path)
    _to_executing(store, sid, fixtures_dir, plan_path=plan_path)

    rc = MOD.main(_dry_run_argv(sid, "thinker", 1, tmp_path / "state", Path(plan_path)))
    assert rc == 0
    out = capsys.readouterr().out
    # "Bash(git status:*)" is itself part of thinker's own KIND_BASELINES
    # (read-only inspection), so its mere presence proves nothing here -- the
    # absence of a provenance section is the signal that no engine grant for
    # this stage/kind pair was merged in.
    assert "Stage grants (provenance):" not in out


def test_dry_run_reflects_a_runtime_once_scope_grant_and_its_provenance(
    store, fixtures_dir, tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(MOD, "plans_dir", lambda: tmp_path)
    sid = "dry-run-runtime-once-grant"
    plan_path = str(fixtures_dir / "plan_two_stage.toml")
    _to_executing(store, sid, fixtures_dir, plan_path=plan_path)

    state = store.load(sid)
    state.permission_request = cli.PermissionRequest(action="need pytest", stage_index=1, raw="need pytest")
    store.save(state)
    resolved = cli.cmd_resolve_permission(
        ns(session=sid, decision="granted", scope="once",
           rules=["Bash(python3 -m pytest scripts/tests/test_mod.py:*)"], add_dirs=None),
        store=store,
    )
    assert resolved.ok

    rc = MOD.main(_dry_run_argv(sid, "developer", 1, tmp_path / "state", Path(plan_path)))
    assert rc == 0
    out = capsys.readouterr().out
    assert '"Bash(python3 -m pytest scripts/tests/test_mod.py:*)"' in _command_section(out)
    assert "- `Bash(python3 -m pytest scripts/tests/test_mod.py:*)` — runtime" in out

    # the once-scope grant is consumed only by a real dispatch, never by a
    # --dry-run inspection -- re-running the same dry-run must show it again.
    rc_again = MOD.main(_dry_run_argv(sid, "developer", 1, tmp_path / "state", Path(plan_path)))
    assert rc_again == 0
    out_again = capsys.readouterr().out
    assert '"Bash(python3 -m pytest scripts/tests/test_mod.py:*)"' in _command_section(out_again)


def test_dry_run_with_no_session_or_stage_index_gets_baseline_only(
    store, fixtures_dir, tmp_path, monkeypatch, capsys,
):
    """The same declared-grant session/stage exists, but a dry-run that omits
    --session/--stage-index (e.g. an ad hoc developer spawn outside the engine
    spine) must never pick up engine grants implicitly."""
    monkeypatch.setattr(MOD, "plans_dir", lambda: tmp_path)
    plan_path = _write_declared_grants_plan(fixtures_dir, tmp_path)
    argv = [
        "--kind", "developer",
        "--plan", plan_path,
        "--done-criterion", "tests green",
        "--criterion-type", "measurable",
        "--complexity", "medium",
        "--effort", "high",
        "--dry-run",
    ]
    rc = MOD.main(argv)
    assert rc == 0
    out = capsys.readouterr().out
    # "Bash(git status:*)" is itself part of developer's own KIND_BASELINES,
    # so its mere presence proves nothing here -- the absence of a
    # provenance section is the signal that no engine grants were loaded at
    # all (no --session/--stage-index given).
    assert "Stage grants (provenance):" not in out
