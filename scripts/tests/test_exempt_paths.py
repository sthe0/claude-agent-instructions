"""agentctl.exempt_paths: the single source of truth for which edits the engine
gate governs. Memory (all three scopes), /tmp, and plan artifacts are exempt;
everything else with a production extension is gated — including the agent's own
config and instructions (CLAUDE.md, skills/**, settings*.json, the Cursor mirror)."""
from __future__ import annotations

import pytest

from agentctl.exempt_paths import is_engine_exempt, is_gated_path, is_production_file


# --- is_engine_exempt: only memory / scratch / plans -----------------------

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
        "/home/u/.claude/plans/some-task.md",
        "/home/u/.claude/plans/some-task/stage-1.md",
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


def test_plan_md_is_not_gated():
    # plans are authored before EXECUTING; gating them would break the planner
    assert is_gated_path("/home/u/.claude/plans/task.md") is False
