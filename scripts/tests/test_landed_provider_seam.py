"""The landed-check provider seam: a non-git delivery proves it reached trunk through a
machine-local plugin, loaded lazily, with typed failures that never fail a stage.

A fake provider module is written into tmp_path and found through
CLAUDE_LANDED_CHECK_PLUGIN_DIR. New symbols (`agentctl.landed_providers`, the
`provider` key, scripts/landed-provider-check.py) are referenced inside test bodies
and fixtures only, so the pre-change tree fails these tests on assertions rather than
at collection.
"""
from __future__ import annotations

import importlib.util
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from agentctl.plan import PlanError, parse_plan
from agentctl.state import (
    Actor,
    CheckKind,
    Criterion,
    CriterionType,
    LandedSpec,
    Means,
    Outcome,
    SessionState,
    Stage,
    StageStatus,
    Subject,
)

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
CHECK_SCRIPT = SCRIPTS_DIR / "landed-provider-check.py"

FAKE_PROVIDER = """\
VERDICT = {verdict}

def freeze(venue):
    return "tok-" + venue.rsplit("/", 1)[-1]

def is_landed(token, target):
    return VERDICT
"""


def install_provider(tmp_path: Path, monkeypatch, name="fake", source=None, verdict="True") -> Path:
    plugin_dir = tmp_path / "plugins"
    (plugin_dir / "providers").mkdir(parents=True)
    (plugin_dir / "providers" / f"{name}.py").write_text(
        source if source is not None else FAKE_PROVIDER.format(verdict=verdict))
    monkeypatch.setenv("CLAUDE_LANDED_CHECK_PLUGIN_DIR", str(plugin_dir))
    return plugin_dir


def run_check(provider="fake", token="tok", target="trunk", env=None):
    return subprocess.run(
        [sys.executable, str(CHECK_SCRIPT), "--provider", provider, "--token", token,
         "--target", target],
        env={**os.environ, **(env or {})}, capture_output=True, text=True,
    )


def provider_stage(provider="fake", *, delivered_head=None, venue="delivery"):
    return Stage(
        index=1, title="s1",
        subject=Subject(material="m", result="img"),
        means=Means(means="Edit", method="do"),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(
            criterion_type=CriterionType.MEASURABLE.value, done_criterion="c",
            verify_kind=CheckKind.LANDED.value, verify_venue=venue,
            landed=LandedSpec(target="trunk", delivered_stage=1, provider=provider),
        ),
        outcome=Outcome(status=StageStatus.PASSED.value, delivered_head=delivered_head),
    )


def test_provider_plugin_is_discovered_through_the_env_dir(tmp_path, monkeypatch):
    from agentctl.landed_providers import load_provider
    install_provider(tmp_path, monkeypatch)
    module = load_provider("fake")
    assert module.freeze("/some/venue") == "tok-venue"
    assert module.is_landed("t", "trunk") is True


def test_missing_plugin_is_file_not_found_and_cli_exits_97(tmp_path, monkeypatch):
    from agentctl.landed_providers import load_provider
    monkeypatch.setenv("CLAUDE_LANDED_CHECK_PLUGIN_DIR", str(tmp_path / "empty"))
    with pytest.raises(FileNotFoundError):
        load_provider("fake")
    result = run_check(env={"CLAUDE_LANDED_CHECK_PLUGIN_DIR": str(tmp_path / "empty")})
    assert result.returncode == 97
    assert "fake" in result.stderr


@pytest.mark.parametrize("source", [
    "raise RuntimeError('boom at import')\n",
    "def freeze(venue):\n    return 'x'\n",
])
def test_broken_plugin_is_typed_and_cli_exits_97(tmp_path, monkeypatch, source):
    from agentctl.landed_providers import LandedProviderBroken, load_provider
    plugin_dir = install_provider(tmp_path, monkeypatch, source=source)
    with pytest.raises(LandedProviderBroken):
        load_provider("fake")
    result = run_check(env={"CLAUDE_LANDED_CHECK_PLUGIN_DIR": str(plugin_dir)})
    assert result.returncode == 97


@pytest.mark.parametrize("verdict,code", [("True", 0), ("False", 1), ("None", 97)])
def test_cli_maps_provider_verdict_to_exit_code(tmp_path, monkeypatch, verdict, code):
    plugin_dir = install_provider(tmp_path, monkeypatch, verdict=verdict)
    result = run_check(env={"CLAUDE_LANDED_CHECK_PLUGIN_DIR": str(plugin_dir)})
    assert result.returncode == code, result.stderr


def test_raising_provider_is_exit_97_with_a_reason(tmp_path, monkeypatch):
    source = "def freeze(v):\n    return 'x'\n\ndef is_landed(t, g):\n    raise OSError('vcs down')\n"
    plugin_dir = install_provider(tmp_path, monkeypatch, source=source)
    result = run_check(env={"CLAUDE_LANDED_CHECK_PLUGIN_DIR": str(plugin_dir)})
    assert result.returncode == 97
    assert "vcs down" in result.stderr


def test_git_is_built_in_and_names_must_be_identifiers(tmp_path, monkeypatch):
    from agentctl.landed_providers import load_provider
    install_provider(tmp_path, monkeypatch, name="git")
    with pytest.raises(ValueError):
        load_provider("git")
    with pytest.raises(ValueError):
        load_provider("../evil")


def test_freeze_stamps_the_provider_token(tmp_path, monkeypatch):
    from agentctl import cli
    install_provider(tmp_path, monkeypatch)
    venue = tmp_path / "wt"
    venue.mkdir()
    stage = provider_stage()
    state = SessionState(session_id="p1", task_id="t", stages=[stage], repo_root=str(venue))
    cli._freeze_delivered_head(state, stage, None)
    assert stage.outcome.delivered_head == "tok-wt"


def test_freeze_with_a_missing_plugin_leaves_the_stamp_untouched(tmp_path, monkeypatch):
    from agentctl import cli
    monkeypatch.setenv("CLAUDE_LANDED_CHECK_PLUGIN_DIR", str(tmp_path / "empty"))
    venue = tmp_path / "wt"
    venue.mkdir()
    stage = provider_stage(delivered_head="earlier")
    state = SessionState(session_id="p2", task_id="t", stages=[stage], repo_root=str(venue))
    cli._freeze_delivered_head(state, stage, None)
    assert stage.outcome.delivered_head == "earlier"


def test_render_for_a_provider_runs_the_cli_with_the_frozen_token(tmp_path):
    stage = provider_stage(delivered_head="tok with space")
    state = SessionState(session_id="p3", task_id="t", stages=[stage], repo_root=str(tmp_path))
    command, refusal = state.render_landed_command(stage.criterion.landed)
    assert refusal is None
    argv = shlex.split(command)
    assert Path(argv[1]).name == "landed-provider-check.py"
    assert argv[argv.index("--provider") + 1] == "fake"
    assert argv[argv.index("--token") + 1] == "tok with space"
    assert argv[argv.index("--target") + 1] == "trunk"


def test_render_for_a_provider_refuses_while_the_token_is_unstamped(tmp_path):
    stage = provider_stage()
    state = SessionState(session_id="p4", task_id="t", stages=[stage], repo_root=str(tmp_path))
    command, refusal = state.render_landed_command(stage.criterion.landed)
    assert command is None
    assert "token" in refusal


def test_default_provider_renders_the_git_check_unchanged(tmp_path):
    stage = provider_stage(provider="git", delivered_head="deadbeef")
    state = SessionState(session_id="p5", task_id="t", stages=[stage], repo_root=str(tmp_path))
    state.plan_task_id = "t"
    command, refusal = state.render_landed_command(stage.criterion.landed)
    assert refusal is None
    assert "merge-base --is-ancestor" in command
    assert "landed-provider-check" not in command
    assert repr(LandedSpec(target="main", delivered_stage=1)) == (
        "LandedSpec(target='main', delivered_stage=1, remote='origin')")


def _plan_data(provider=None, target="trunk"):
    landed = {"target": target, "delivered_stage": 1}
    if provider is not None:
        landed["provider"] = provider
    return {
        "meta": {
            "task_id": "seam-test", "goal": "g", "done_criterion": "d",
            "criterion_type": "measurable", "weight_class": "substantive",
            "external_research": "n/a",
        },
        "stage": [{
            "index": 1, "title": "Deliver", "executor": "in_thread",
            "expected_result_image": "the change exists", "criterion_type": "measurable",
            "done_criterion": "the check passes", "verify_command": "true",
            "verify_venue": "delivery", "negative_control_waiver": "n/a",
            "material": "the module", "means": "edit", "method": "do it",
            "conditions": "c", "invariants": "inv", "capability_required": "cap",
            "principle": {"statement": "s", "source": "src", "derivation": "d follows from src",
                          "confidence": "high", "refutation": "ref"},
        }],
        "final_check": [{"kind": "landed", "label": "landed", "landed": landed}],
    }


def test_plan_with_a_provider_parses_and_skips_the_git_ref_regex():
    doc = parse_plan(_plan_data("fake", target="trunk@{weird ref}"))
    assert doc.meta.final_check[0].landed.provider == "fake"


def test_plan_without_a_provider_defaults_to_git_and_keeps_the_ref_regex():
    assert parse_plan(_plan_data()).meta.final_check[0].landed.provider == "git"
    with pytest.raises(PlanError):
        parse_plan(_plan_data(target="not a ref"))


@pytest.mark.parametrize("bad", ["../x", "a b", "1abc", ""])
def test_plan_with_an_invalid_provider_name_is_a_plan_error(bad):
    with pytest.raises(PlanError):
        parse_plan(_plan_data(bad))


def test_importing_agentctl_modules_does_not_load_any_plugin(tmp_path, monkeypatch):
    plugin_dir = install_provider(tmp_path, monkeypatch, source="raise SystemExit('loaded')\n")
    probe = (
        "import sys; import agentctl.plan, agentctl.cli, agentctl.state; "
        "sys.exit(1 if any('_plugin_landed_providers' in m for m in sys.modules) else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=SCRIPTS_DIR, capture_output=True, text=True,
        env={**os.environ, "CLAUDE_LANDED_CHECK_PLUGIN_DIR": str(plugin_dir)},
    )
    assert importlib.util.find_spec("agentctl.landed_providers") is not None
    assert result.returncode == 0, result.stderr
