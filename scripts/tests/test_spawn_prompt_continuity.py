"""spawn-specialist.py assemble_prompt: --continue-worktree (#43) injects an
explicit 'continue the prior stage, do not fork fresh' section naming the shared
worktree, so a dependent stage's developer builds on the prior stage's
committed-but-un-landed branch instead of forking a fresh worktree off
origin/main. Unset => the prompt is byte-identical to before the flag existed."""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"


def _load():
    spec = importlib.util.spec_from_file_location("spawn_specialist_continuity", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()


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
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_continuation_section_injected_when_continue_worktree_set(tmp_path):
    args = _args(tmp_path, continue_worktree="/repo/.claude/worktrees/task-x")
    prompt = MOD.assemble_prompt(args, depth=1, permissions="")
    assert "## Continue the prior stage" in prompt
    assert "/repo/.claude/worktrees/task-x" in prompt
    assert "do NOT fork fresh" in prompt
    assert "git worktree add" in prompt


def test_continuation_section_absent_when_continue_worktree_none(tmp_path):
    args = _args(tmp_path)
    prompt = MOD.assemble_prompt(args, depth=1, permissions="")
    assert "Continue the prior stage" not in prompt
    assert "fork fresh" not in prompt


def test_continuation_section_absent_when_attribute_missing_entirely(tmp_path):
    """A hand-built Namespace lacking the attribute (predates the flag) must not
    raise — assemble_prompt uses getattr, not args.continue_worktree directly."""
    args = _args(tmp_path)
    del args.continue_worktree
    prompt = MOD.assemble_prompt(args, depth=1, permissions="")
    assert "Continue the prior stage" not in prompt


def test_prompt_byte_identical_to_pre_flag_baseline_when_unset(tmp_path):
    with_none = MOD.assemble_prompt(_args(tmp_path), depth=1, permissions="")
    args_missing_attr = _args(tmp_path)
    del args_missing_attr.continue_worktree
    without_attr = MOD.assemble_prompt(args_missing_attr, depth=1, permissions="")
    assert with_none == without_attr
