"""R10: a durable per-stage evidence directory (outside every OS-temp scratch
root), a submission-time check refusing output_artifacts that resolve under
one, a fixed '## Checkpoints' section on every developer prompt, and a
spawn-time (not grants_sha256-affecting) write grant onto that directory.

Every reference to an R10-only symbol lives INSIDE a test function body (never
at module import time) so this file still collects cleanly against the
pre-lever commit — pytest reports a normal per-test FAILED (AttributeError/
TypeError/AssertionError) rather than a whole-file collection error, which is
what the plan's nc.sh negative control requires (exit code 1, not 2)."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import pytest

from agentctl import cli
from agentctl.dispatch import RunResult
from agentctl.plan import load_plan, parse_plan
from agentctl.submission import submission_violations

from test_stage_grants import _to_executing, ns

SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"


def _load():
    spec = importlib.util.spec_from_file_location("spawn_specialist_r10_evidence", SCRIPT)
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
    """The `=== command (not executed) ===` half of --dry-run's stdout, not
    the `=== assembled prompt ===` half (which inlines the whole raw plan
    text) -- mirrors test_spawn_grants_integration.py's own helper."""
    return out.split("=== command (not executed) ===", 1)[1]


def _args(tmp_path, **overrides):
    plan = tmp_path / "plan.toml"
    plan.write_text('[meta]\ntask_id = "t"\n', encoding="utf-8")
    base = dict(
        plan=plan,
        constraints="",
        context_dossier=None,
        done_criterion="do the thing",
        criterion_type="measurable",
        continue_worktree=None,
        kind="developer",
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _plan_data(*, output_artifacts=None, ephemeral_artifacts_waiver=None, verify_command="true"):
    """A one-stage SUBSTANTIVE plan dict, parse_plan-ready -- à la
    test_convergence_r2_negative_control.py's own `_plan_data`, extended with
    an `output_artifacts` / `ephemeral_artifacts_waiver` knob. Carries its own
    `negative_control_waiver` so R2's unrelated requirement never pollutes the
    violations list this file's tests inspect."""
    stage = {
        "index": 1,
        "title": "Fix the bug",
        "executor": "in_thread",
        "expected_result_image": "tests pass with no failures",
        "criterion_type": "measurable",
        "done_criterion": "the check passes",
        "verify_command": verify_command,
        "negative_control_waiver": "n/a for this fixture",
        "material": "the module",
        "means": "pytest",
        "method": "run the test suite",
        "conditions": "c",
        "invariants": "inv",
        "capability_required": "cap",
        "principle": {
            "statement": "statement 1",
            "source": "src",
            "derivation": "der follows from src",
            "confidence": "high",
            "refutation": "ref",
        },
    }
    if output_artifacts is not None:
        stage["output_artifacts"] = output_artifacts
    if ephemeral_artifacts_waiver is not None:
        stage["ephemeral_artifacts_waiver"] = ephemeral_artifacts_waiver
    return {
        "meta": {
            "task_id": "r10-test",
            "goal": "g",
            "done_criterion": "d",
            "criterion_type": "measurable",
            "weight_class": "substantive",
            "external_research": "n/a",
        },
        "stage": [stage],
    }


# --- #1: evidence-dir command creates a durable directory -------------------


def test_evidence_dir_command_creates_durable_dir(store, tmp_path, monkeypatch):
    """Exercises the real (non-override) resolution order end to end: with
    $AGENTCTL_EVIDENCE_ROOT unset, an absolute $XDG_STATE_HOME names the
    root, and the scratch-root refusal is checked against the actual
    resolved path -- not skipped. $AGENTCTL_SCRATCH_ROOTS is repointed at an
    unrelated, non-existent directory so the real default roots (which
    otherwise include /tmp, under which tmp_path itself lives) do not
    accidentally flag the resolved path."""
    monkeypatch.delenv("AGENTCTL_EVIDENCE_ROOT", raising=False)
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg))
    monkeypatch.setenv("AGENTCTL_SCRATCH_ROOTS", str(tmp_path / "sneaky-scratch-only"))

    directive = cli.cmd_evidence_dir(ns(session="ev-sess", stage=1), store=store)
    assert directive.ok
    path = directive.data["evidence_dir"]
    assert path == str(xdg / "agentctl-evidence" / "ev-sess" / "stage-1")
    assert Path(path).is_dir()

    # Idempotent: a second call returns the same path and does not fail on an
    # already-existing directory.
    directive2 = cli.cmd_evidence_dir(ns(session="ev-sess", stage=1), store=store)
    assert directive2.ok
    assert directive2.data["evidence_dir"] == path
    assert Path(path).is_dir()


def test_evidence_dir_relative_or_unset_xdg_falls_back_to_home(tmp_path, monkeypatch):
    """A relative (or absent) $XDG_STATE_HOME is not a usable root -- the
    resolution falls back to ~/.local/state/agentctl-evidence, exercised here
    with $HOME monkeypatched to an isolated directory so the real home is
    never touched."""
    monkeypatch.delenv("AGENTCTL_EVIDENCE_ROOT", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGENTCTL_SCRATCH_ROOTS", str(tmp_path / "sneaky-scratch-only"))

    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    path_unset = cli.evidence_dir_for("ev-sess", 1)
    assert path_unset == home / ".local" / "state" / "agentctl-evidence" / "ev-sess" / "stage-1"

    monkeypatch.setenv("XDG_STATE_HOME", "relative/not/absolute")
    path_relative = cli.evidence_dir_for("ev-sess", 1)
    assert path_relative == path_unset


def test_evidence_dir_validate_add_dir_refusal_raises(monkeypatch):
    """A root that resolves outside every scratch root but that
    grants.validate_add_dir itself refuses (here: the protected agentctl
    state directory — see evidence_dir_for's own docstring) raises
    EvidenceDirError rather than silently falling back."""
    from lib import config_root

    monkeypatch.setenv("AGENTCTL_EVIDENCE_ROOT", str(config_root.agentctl_state_dir()))
    with pytest.raises(cli.EvidenceDirError, match="refused"):
        cli.evidence_dir_for("ev-sess", 1)


def test_evidence_dir_cli_exits_nonzero_on_refusal(tmp_path, monkeypatch, capsys):
    """The `evidence-dir` CLI command surfaces a refusal as a failed
    Directive, and `cli.main` maps a not-ok Directive to exit code 1."""
    from lib import config_root

    monkeypatch.setenv("AGENTCTL_EVIDENCE_ROOT", str(config_root.agentctl_state_dir()))
    rc = cli.main([
        "--state-root", str(tmp_path / "state"),
        "evidence-dir", "--session", "ev-sess", "--stage", "1",
    ])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False


def test_evidence_dir_under_scratch_root_fails_loudly_without_override(tmp_path, monkeypatch):
    """With $AGENTCTL_EVIDENCE_ROOT unset (the conftest autouse fixture's own
    override deleted), a $XDG_STATE_HOME that itself resolves under an OS-temp
    scratch root must be refused outright -- no silent fallback to another
    root. `tmp_path` is itself under the default scratch-root set (pytest's
    basetemp lives under /tmp or $TMPDIR), so setting $XDG_STATE_HOME to it
    exercises the refusal without needing $AGENTCTL_SCRATCH_ROOTS at all."""
    monkeypatch.delenv("AGENTCTL_EVIDENCE_ROOT", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    with pytest.raises(cli.EvidenceDirError, match="scratch root"):
        cli.evidence_dir_for("ev-sess", 1)


# --- #2: dispatch's Directive carries data.evidence_dir ---------------------


def test_dispatch_directive_names_evidence_dir(store, fixtures_dir):
    sid = "r10-dispatch-evidence"
    _to_executing(store, sid, fixtures_dir)
    runner = lambda argv: RunResult(0, stdout="python3 spawn-specialist.py --kind developer ...\n")

    d = cli.cmd_dispatch(
        ns(session=sid, budget="medium", complexity="medium", dry_run=True),
        store=store, runner=runner,
    )
    assert d.ok is True
    expected = str(cli.evidence_dir_for(sid, 1))
    assert d.data["evidence_dir"] == expected


# --- #3: an output_artifacts entry under a scratch root is a violation ------


def test_output_artifact_under_scratch_root_is_a_violation():
    doc = parse_plan(_plan_data(output_artifacts=["/tmp/scratch-artifact.txt"]))
    problems = submission_violations(doc)
    assert any("scratch root" in p for p in problems), problems


def test_ephemeral_artifacts_waiver_is_accepted():
    doc = parse_plan(_plan_data(
        output_artifacts=["/tmp/scratch-artifact.txt"],
        ephemeral_artifacts_waiver="only an ephemeral run, nothing durable to commit",
    ))
    problems = submission_violations(doc)
    assert not any("scratch root" in p for p in problems), problems


def test_verify_command_mentioning_tmp_is_never_flagged():
    doc = parse_plan(_plan_data(
        output_artifacts=["scripts/tests/test_mod.py"],
        verify_command="cat /tmp/whatever.txt",
    ))
    problems = submission_violations(doc)
    assert not any("scratch root" in p for p in problems), problems


# --- #4: every developer prompt carries a fixed '## Checkpoints' section ----


def test_developer_prompt_carries_checkpoint_section(tmp_path):
    args = _args(tmp_path, kind="developer")
    evidence_dir = str(tmp_path / "evidence" / "sess" / "stage-1")
    prompt = MOD.assemble_prompt(args, depth=1, permissions="", evidence_dir=evidence_dir)
    assert "## Checkpoints" in prompt
    assert evidence_dir in prompt


def test_developer_prompt_checkpoint_section_notes_missing_evidence_dir(tmp_path):
    args = _args(tmp_path, kind="developer")
    prompt = MOD.assemble_prompt(args, depth=1, permissions="", evidence_dir=None)
    assert "## Checkpoints" in prompt
    assert "none provided" in prompt


def test_non_developer_prompt_omits_checkpoint_section(tmp_path):
    args = _args(tmp_path, kind="thinker")
    prompt = MOD.assemble_prompt(args, depth=1, permissions="")
    assert "## Checkpoints" not in prompt


# --- #5: a developer spawn gets a write grant for its evidence dir ----------


def test_developer_spawn_can_write_evidence_dir(store, fixtures_dir, tmp_path, monkeypatch, capsys):
    # A sibling of tmp_path/"state", not tmp_path itself -- plans_dir() gets an
    # Edit-deny for `developer` (it may read, never write, the plans tree), and
    # nesting the evidence dir under that same root would make it collide with
    # that unrelated, pre-existing deny purely as a test-setup artifact.
    monkeypatch.setattr(MOD, "plans_dir", lambda: tmp_path / "plansdir")
    sid = "r10-spawn-evidence-write"
    plan_path = str(fixtures_dir / "plan_two_stage.toml")
    _to_executing(store, sid, fixtures_dir, plan_path=plan_path)

    expected_evidence_dir = str(cli.evidence_dir_for(sid, 1))

    rc = MOD.main(_dry_run_argv(sid, "developer", 1, tmp_path / "state", Path(plan_path)))
    assert rc == 0
    out = capsys.readouterr().out
    cmd_section = _command_section(out)

    assert "--add-dir" in cmd_section
    assert expected_evidence_dir in cmd_section

    expected_allow, _expected_deny = MOD.stage_grant_rules(
        [{"path": expected_evidence_dir, "mode": "write"}]
    )
    for rule in expected_allow:
        assert f'"{rule}"' in cmd_section, (rule, cmd_section)


def test_developer_spawn_fails_loudly_on_evidence_dir_refusal(
    store, fixtures_dir, tmp_path, monkeypatch, capsys,
):
    """A refused evidence directory (here: $AGENTCTL_EVIDENCE_ROOT pointed at
    the protected agentctl state directory, which grants.validate_add_dir
    refuses) must fail the spawn outright, on stderr, naming the real cause —
    never the generic 'no --session/--stage-index' text, which would be
    false here."""
    from lib import config_root

    monkeypatch.setattr(MOD, "plans_dir", lambda: tmp_path / "plansdir")
    monkeypatch.setenv("AGENTCTL_EVIDENCE_ROOT", str(config_root.agentctl_state_dir()))
    sid = "r10-spawn-evidence-refused"
    plan_path = str(fixtures_dir / "plan_two_stage.toml")
    _to_executing(store, sid, fixtures_dir, plan_path=plan_path)

    rc = MOD.main(_dry_run_argv(sid, "developer", 1, tmp_path / "state", Path(plan_path)))
    assert rc == 2
    captured = capsys.readouterr()
    assert "refused" in captured.err
    assert "no --session/--stage-index" not in captured.err
    assert "=== command (not executed) ===" not in captured.out


def test_non_developer_spawn_gets_no_checkpoint_or_evidence_grant(
    store, fixtures_dir, tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(MOD, "plans_dir", lambda: tmp_path / "plansdir")
    sid = "r10-non-developer-kind"
    plan_path = str(fixtures_dir / "plan_two_stage.toml")
    _to_executing(store, sid, fixtures_dir, plan_path=plan_path)

    expected_evidence_dir = str(cli.evidence_dir_for(sid, 1))

    rc = MOD.main(_dry_run_argv(sid, "thinker", 1, tmp_path / "state", Path(plan_path)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "## Checkpoints" not in out
    assert expected_evidence_dir not in out


# --- #6: the evidence-dir grant never moves an already-approved plan's ------
# --- grants_sha256 -----------------------------------------------------------

# Computed at commit f8232ea (the pre-R10 tip) via `agentctl.plan.grants_sha256`
# against the unmodified `fixtures/plan_two_stage.toml` fixture -- see the R10
# stage's own procedure. `grants_sha256` reads ONLY the PlanDoc's own declared
# and derived grants (agentctl/plan.py's `_grants_effective_map`), never the
# spawn-time evidence-dir add_dir this file's other tests exercise, so the
# SAME digest must still come back from the current code for the same fixture.
GOLDEN_GRANTS_SHA256 = "1f8190be49094ff4050c2f164d215e737263104665c69da513f23f174db6a31c"


def test_evidence_dir_grant_keeps_approved_grants_sha256(store, fixtures_dir, tmp_path, monkeypatch):
    from agentctl.grants import derive_stage_grants
    from agentctl.plan import grants_sha256

    plan_path = str(fixtures_dir / "plan_two_stage.toml")
    doc = load_plan(plan_path)
    assert grants_sha256(doc) == GOLDEN_GRANTS_SHA256

    stage1 = next(s for s in doc.stages if s.index == 1)
    venue = doc.meta.delivery_worktree or doc.meta.repo_root or "."
    derived, _dropped = derive_stage_grants(stage1, venue=venue)

    monkeypatch.setattr(MOD, "plans_dir", lambda: tmp_path / "plansdir")
    sid = "r10-grants-sha-fixed"
    _to_executing(store, sid, fixtures_dir, plan_path=plan_path)

    # This is the R10-only reference: cli.evidence_dir_for does not exist on
    # the pre-lever commit, so this line is what makes the whole test fail
    # there (AttributeError), rather than the digest comparison above, which
    # already holds pre-lever too (grants_sha256 itself is untouched by R10).
    expected_evidence_dir = str(cli.evidence_dir_for(sid, 1))

    # Never part of the declared+derived grant set grants_sha256 hashes.
    assert not any(d.path == expected_evidence_dir for d in derived.add_dirs)

    rc = MOD.main(_dry_run_argv(sid, "developer", 1, tmp_path / "state", Path(plan_path)))
    assert rc == 0

    # The engine-side check: cmd_stage_grants re-derives the stage's grants
    # from the approved-plan snapshot and compares the result's hash against
    # state.approved_grants_sha256 (see its own docstring), reporting a
    # mismatch via data["error"] rather than raising. A real spawn resolving
    # and printing the evidence-dir add-dir/write grant must not have moved
    # that binding -- the spawn-time mechanism never touches the PlanDoc
    # grants_sha256 is computed from, so this re-derivation must still agree.
    report = cli.cmd_stage_grants(ns(session=sid, stage=1, json=True), store=store)
    assert report.ok
    report_data = json.loads(report.detail)
    assert report_data["error"] is None
    assert any(e.get("path") == expected_evidence_dir for e in report_data["grants"]) is False
    assert grants_sha256(doc) == GOLDEN_GRANTS_SHA256
