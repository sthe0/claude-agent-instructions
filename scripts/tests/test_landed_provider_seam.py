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
    assert "--provider=fake" in argv
    assert "--token=tok with space" in argv
    assert "--target=trunk" in argv


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


_LEGACY_CRITERION_PATHS = (
    "criterion.criterion_type", "criterion.done_criterion", "criterion.verify_command",
    "criterion.expected_exit", "criterion.verify_venue", "criterion.verify_kind",
    "criterion.landed.target", "criterion.landed.delivered_stage",
    "criterion.landed.remote", "criterion.verify_venue_at_final",
    "criterion.negative_control", "criterion.negative_control_waiver",
)
# The criterion element key of provider_stage("git") / a stage with no landed spec, as
# computed from the pre-provider field list (the same one _LEGACY_CRITERION_PATHS holds).
GOLDEN_GIT_CRITERION_KEY = "af24ec7f49290d4a81476889e4254374c9a9d98136bbc45c1a0e361c9d7f61d9"
GOLDEN_NO_LANDED_CRITERION_KEY = "073775f54942bf8a7ff47828bfe16266cdaffaedf01d048088f73c99d419ac6d"


def _legacy_criterion_key(stage) -> str:
    import hashlib
    from agentctl.plan import _leaf_values
    payload = repr(("criterion", tuple(_leaf_values(stage, p) for p in _LEGACY_CRITERION_PATHS)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _no_landed_stage():
    stage = provider_stage()
    stage.criterion.landed = None
    stage.criterion.verify_kind = None
    return stage


@pytest.mark.parametrize("make,golden", [
    (lambda: provider_stage(provider="git"), GOLDEN_GIT_CRITERION_KEY),
    (_no_landed_stage, GOLDEN_NO_LANDED_CRITERION_KEY),
])
def test_default_provider_keeps_the_pre_provider_criterion_question_key(make, golden):
    from agentctl.plan import stage_question_key
    stage = make()
    assert stage_question_key(stage, "criterion") == _legacy_criterion_key(stage) == golden


def test_a_non_default_provider_moves_the_criterion_question_key():
    from agentctl.plan import stage_question_key
    assert (stage_question_key(provider_stage("fake"), "criterion")
            != stage_question_key(provider_stage("git"), "criterion"))


def test_a_provider_change_is_visible_on_the_operative_and_final_check_surfaces():
    from agentctl import gates
    one, two = parse_plan(_plan_data("fake")), parse_plan(_plan_data("other"))
    assert gates._operative_surface(one) != gates._operative_surface(two)
    assert gates._final_check_surface(one.meta) != gates._final_check_surface(two.meta)
    assert gates._landed_sort_key(None) < gates._landed_sort_key(LandedSpec("a", 1))


def test_a_provider_that_exits_the_process_is_broken_not_fatal(tmp_path, monkeypatch):
    from agentctl.landed_providers import LandedProviderBroken, load_provider
    install_provider(tmp_path, monkeypatch, source="import sys\nsys.exit(0)\n")
    with pytest.raises(LandedProviderBroken):
        load_provider("fake")


def test_is_landed_exiting_the_process_is_exit_97(tmp_path, monkeypatch):
    source = "def freeze(v):\n    return 'x'\n\ndef is_landed(t, g):\n    raise SystemExit(0)\n"
    plugin_dir = install_provider(tmp_path, monkeypatch, source=source)
    result = run_check(env={"CLAUDE_LANDED_CHECK_PLUGIN_DIR": str(plugin_dir)})
    assert result.returncode == 97


def test_freeze_exiting_the_process_leaves_the_stamp_untouched(tmp_path, monkeypatch):
    from agentctl import cli
    source = "def freeze(v):\n    raise SystemExit(3)\n\ndef is_landed(t, g):\n    return True\n"
    install_provider(tmp_path, monkeypatch, source=source)
    venue = tmp_path / "wt"
    venue.mkdir()
    stage = provider_stage(delivered_head="earlier")
    state = SessionState(session_id="p6", task_id="t", stages=[stage], repo_root=str(venue))
    cli._freeze_delivered_head(state, stage, None)
    assert stage.outcome.delivered_head == "earlier"


def test_a_token_that_looks_like_a_flag_reaches_the_provider(tmp_path, monkeypatch):
    plugin_dir = install_provider(tmp_path, monkeypatch, verdict="True")
    stage = provider_stage(delivered_head="--weird")
    state = SessionState(session_id="p7", task_id="t", stages=[stage], repo_root=str(tmp_path))
    command, _ = state.render_landed_command(stage.criterion.landed)
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True,
                            env={**os.environ, "CLAUDE_LANDED_CHECK_PLUGIN_DIR": str(plugin_dir)})
    assert result.returncode == 0, result.stderr


def test_a_usage_error_is_exit_97_not_a_verdict():
    result = subprocess.run([sys.executable, str(CHECK_SCRIPT), "--bogus"],
                            capture_output=True, text=True)
    assert result.returncode == 97


def test_a_provider_name_with_a_trailing_newline_is_rejected():
    from agentctl.landed_providers import PROVIDER_NAME_RE
    assert PROVIDER_NAME_RE.fullmatch("fake\n") is None


@pytest.mark.parametrize("verdict,ok,refused", [("True", True, False), ("False", False, False),
                                                ("None", False, True)])
def test_landed_check_result_end_to_end_with_a_real_plugin(tmp_path, monkeypatch, verdict, ok, refused):
    from agentctl import cli
    install_provider(tmp_path, monkeypatch, verdict=verdict)
    stage = provider_stage(delivered_head="tok")
    state = SessionState(session_id="p8", task_id="t", stages=[stage], repo_root=str(tmp_path))
    got_ok, refusal, _result = cli._landed_check_result(state, stage.criterion.landed, None)
    assert got_ok is ok
    assert (refusal is not None) is refused
    if refused:
        assert "fake" in refusal and "cannot decide" in refusal
        assert "origin" not in refusal


def test_a_missing_plugin_is_a_refusal_naming_the_provider_and_its_reason(tmp_path, monkeypatch):
    from agentctl import cli
    monkeypatch.setenv("CLAUDE_LANDED_CHECK_PLUGIN_DIR", str(tmp_path / "empty"))
    stage = provider_stage(delivered_head="tok")
    state = SessionState(session_id="p9", task_id="t", stages=[stage], repo_root=str(tmp_path))
    ok, refusal, _result = cli._landed_check_result(state, stage.criterion.landed, None)
    assert ok is False and "fake" in refusal and "no landed provider plugin" in refusal


def test_a_non_git_landed_stage_declares_no_git_ref_resource():
    from agentctl import plan_resources
    assert plan_resources._stage_landed_resources(provider_stage("fake")) == []
    assert len(plan_resources._stage_landed_resources(provider_stage("git"))) == 1


def test_renderings_name_the_provider_not_an_origin_ref():
    from agentctl import render
    text = "\n".join(render.render_final_checks_md(parse_plan(_plan_data("fake"))))
    assert "fake" in text and "origin" not in text
    assert "`origin/trunk`" in LandedSpec("trunk", 1).refs_phrase()
