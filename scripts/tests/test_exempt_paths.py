"""agentctl.exempt_paths: the single source of truth for which edits the engine
gate governs. Memory (all three scopes) and /tmp are unconditionally exempt;
everything else with a production extension is gated — including the agent's own
config and instructions (CLAUDE.md, skills/**, settings*.json, the Cursor mirror)
and plan artifacts (~/.claude/plans/, which is_plan_file identifies for the gate's
node-aware rule)."""
from __future__ import annotations

import pytest

from agentctl.exempt_paths import (
    is_engine_exempt,
    is_gated_path,
    is_plan_file,
    is_production_file,
)


# --- is_engine_exempt: only memory / scratch -------------------------------

@pytest.mark.parametrize(
    "path",
    [
        "/tmp/cc-scratch/x.py",
        "/home/u/.claude/projects/abc/memory/MEMORY.md",
        "/home/u/.claude/projects/abc/memory/leaves/foo.md",
        "/home/u/.claude/memory-global/MEMORY.md",
        "/home/u/.claude/memory-global/leaves/experience/bar.md",
        "/home/u/proj/.claude/agent-memory/MEMORY.md",
        "/home/u/proj/.claude/agent-memory/experience/baz.md",
    ],
)
def test_exempt_paths_are_exempt(path):
    assert is_engine_exempt(path) is True
    assert is_gated_path(path) is False


@pytest.mark.parametrize(
    "path",
    [
        "/home/u/.claude/CLAUDE.md",
        "/home/u/.claude/settings.json",
        "/home/u/.claude/settings.local.json",
        "/home/u/claude-agent-instructions/CLAUDE.md",
        "/home/u/claude-agent-instructions/skills/self-improvement/SKILL.md",
        "/home/u/claude-agent-instructions/skills/self-improvement/policy.md",
        "/home/u/claude-agent-instructions/scripts/agentctl/cli.py",
        "/home/u/claude-agent-instructions/cursor/rules/claude-code-sync.mdc",
        "/work/project/module.py",
        "/work/project/README.md",
    ],
)
def test_non_memory_paths_are_not_exempt(path):
    assert is_engine_exempt(path) is False


# --- is_production_file: extension gate ------------------------------------

@pytest.mark.parametrize(
    "path",
    [
        "/x/a.py", "/x/a.sh", "/x/a.json", "/x/a.md", "/x/a.mdc",
        "/x/a.yaml", "/x/a.toml", "/x/a.ts",
    ],
)
def test_production_extensions(path):
    assert is_production_file(path) is True


@pytest.mark.parametrize("path", ["/x/a.txt", "/x/a.log", "/x/a.png", "/x/NOTES", ""])
def test_non_production_extensions(path):
    assert is_production_file(path) is False


# --- is_gated_path composes the two ----------------------------------------

def test_claude_md_is_gated():
    # the headline case: the agent's own instructions now flow through the spine
    assert is_gated_path("/home/u/.claude/CLAUDE.md") is True


def test_memory_md_is_not_gated():
    # memory .md is a production extension but rescued by the exempt list
    assert is_gated_path("/home/u/.claude/memory-global/leaves/foo.md") is False


def test_plan_md_is_gated():
    # plans are no longer unconditionally exempt: they are gated, and the gate
    # applies a node-aware rule (writable only at a planning-position node)
    assert is_engine_exempt("/home/u/.claude/plans/task.md") is False
    assert is_gated_path("/home/u/.claude/plans/task.md") is True


# --- is_plan_file: identifies a plan path for the node-aware rule ----------

@pytest.mark.parametrize(
    "path",
    [
        "/home/u/.claude/plans/task.md",
        "/home/u/.claude/plans/some-task/stage-1.md",
        "/home/u/.claude/plans/nested/deep/x.toml",
        "/home/u/.claude-agent/plans/task.md",
        "/home/u/.claude-agent/plans/some-task/stage-1.md",
    ],
)
def test_is_plan_file_true(path):
    assert is_plan_file(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "/home/u/.claude/CLAUDE.md",
        "/home/u/.claude/memory-global/leaves/foo.md",
        "/work/project/module.py",
        "/home/u/plans/task.md",  # not under .claude/plans/
        "",
    ],
)
def test_is_plan_file_false(path):
    assert is_plan_file(path) is False
