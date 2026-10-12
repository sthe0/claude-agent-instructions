"""The memory-curator kind gets Edit rights on the memory roots of its workdir and
nothing else, and its Bash baseline can read and run the split subcommands but
cannot commit, fetch, rebase, push or write through a general-purpose verb."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"


def _load():
    spec = importlib.util.spec_from_file_location("spawn_specialist_memory_write", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()
SCRIPTS = str(MOD.SCRIPTS_DIR)


def _worktree(tmp_path: Path, *, project_memory: bool = False) -> Path:
    (tmp_path / "memory-global" / "leaves").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    if project_memory:
        (tmp_path / ".claude" / "agent-memory").mkdir(parents=True)
    return tmp_path


def _edit_rules(settings: dict) -> list[str]:
    allow = settings.get("permissions", {}).get("allow", [])
    return [r for r in allow if r.startswith(("Edit(", "Write("))]


def test_memory_curator_write_scope_(tmp_path):
    worktree = _worktree(tmp_path, project_memory=True)
    rules = _edit_rules(MOD.build_child_settings("memory-curator", workdir=str(worktree)))
    assert rules, "memory-curator received no Edit rule"
    roots = (f"/{worktree}/memory-global/", f"/{worktree}/.claude/agent-memory/")
    for rule in rules:
        assert rule.startswith("Edit(") and rule.endswith("/**)"), rule
        assert rule[len("Edit("):].startswith(roots), f"rule outside a memory root: {rule}"
    assert len(rules) == 2


def test_memory_curator_write_scope_follows_the_roots_that_exist(tmp_path):
    worktree = _worktree(tmp_path)
    rules = _edit_rules(MOD.build_child_settings("memory-curator", workdir=str(worktree)))
    assert rules == [f"Edit(/{worktree}/memory-global/**)"]


def test_memory_curator_write_scope_is_not_granted_to_other_kinds(tmp_path):
    worktree = _worktree(tmp_path, project_memory=True)
    for kind in ("developer", "thinker", "code-reviewer", "tech-writer", "planner", "default"):
        settings = MOD.build_child_settings(kind, workdir=str(worktree))
        leaked = [r for r in _edit_rules(settings) if "memory" in r]
        assert not leaked, f"{kind} received memory write rules: {leaked}"


def test_memory_curator_without_workdir_gets_no_edit_rule():
    assert not _edit_rules(MOD.build_child_settings("memory-curator"))


def _script(name: str) -> list[str]:
    return [f"Bash(python3 {SCRIPTS}/{name}:*)", f"Bash(python3 scripts/{name}:*)"]


_EXPECTED_BASH = {
    "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)",
    "Bash(grep:*)", "Bash(rg:*)", "Bash(stat:*)", "Bash(pwd)",
    "Bash(shasum:*)", "Bash(sha256sum:*)",
    "Bash(git rev-parse:*)", "Bash(git status:*)", "Bash(git diff:*)",
    "Bash(git log:*)", "Bash(git show:*)",
    *(rule for name in (
        "record-experience.py search", "record-experience.py promote",
        "record-experience.py split-apply", "record-experience.py split-verify",
        "record-experience.py split-contexts",
        "verify-experience-leaf.py", "verify-leaf-structure.py",
        "verify-memory-index.py", "self-diagnose.py",
        "agentctl-cli.py classify", "agentctl-cli.py status",
    ) for rule in _script(name)),
}

_WRITING_COMMANDS = re.compile(
    r"^Bash\((git (commit|push|fetch|rebase|merge|checkout|restore|add|reset)"
    r"|sed|tee|cp|mv|rm|find|python3(?! (/|scripts/))|python3 -[a-z]|bash|sh"
    r"|echo|printf|touch|mkdir|dd|awk|xargs)\b"
)


def test_memory_curator_baseline_has_no_commit_():
    baseline = set(MOD.KIND_BASELINES["memory-curator"])
    assert baseline <= _EXPECTED_BASH, f"unexpected rules: {sorted(baseline - _EXPECTED_BASH)}"
    for rule in baseline:
        assert rule.startswith("Bash("), rule
        assert rule != "Bash", "unscoped Bash"
        assert not _WRITING_COMMANDS.match(rule), f"writing command granted: {rule}"
    for forbidden in ("git commit", "git push", "git fetch", "git rebase"):
        assert not any(rule.startswith(f"Bash({forbidden}") for rule in baseline)


def test_memory_curator_baseline_names_every_split_subcommand():
    baseline = set(MOD.KIND_BASELINES["memory-curator"])
    for sub in ("split-contexts", "split-apply", "split-verify", "promote", "search"):
        assert f"Bash(python3 {SCRIPTS}/record-experience.py {sub}:*)" in baseline
