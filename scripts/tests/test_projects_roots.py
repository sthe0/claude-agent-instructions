"""The projects-domain accessors: `projects_roots()` and `iter_transcripts()`.

Difficulty pinned here: every consumer used to compute `agent_home() / "projects"`
itself, which answers "where is the system installed" — a *different* question
from "where have sessions written". On a machine where both a bare `claude` and
the `claude-agent` launcher are in use, sessions exist under BOTH roots, and a
report that picks either one is wrong silently: it prints a smaller number, never
an error. These tests pin the union, the dedup, and the missing-root tolerance
that make the shared accessor safe to substitute for eleven hand-rolled joins.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from lib import config_root  # noqa: E402


def _both(monkeypatch, agent: Path, harness: Path) -> None:
    """Make agent_home() -> `agent` and harness_config_root() -> `harness`.

    agent_home()'s own order puts CLAUDE_CONFIG_DIR ahead of CLAUDE_AGENT_HOME,
    so the divergent case cannot be expressed through the environment alone —
    that ordering is exactly what makes the two roots coincide under the
    `claude-agent` launcher. Patch the accessors directly.
    """
    monkeypatch.setattr(config_root, "agent_home", lambda: agent)
    monkeypatch.setattr(config_root, "harness_config_root", lambda: harness)


# ---------------------------------------------------------------------------
# projects_roots(): the union
# ---------------------------------------------------------------------------

def test_two_distinct_roots_yield_both_agent_root_first(monkeypatch, tmp_path):
    agent = tmp_path / "agent-root"
    harness = tmp_path / "harness-root"
    (agent / "projects").mkdir(parents=True)
    (harness / "projects").mkdir(parents=True)
    _both(monkeypatch, agent, harness)

    assert config_root.projects_roots() == [
        agent / "projects",
        harness / "projects",
    ]


def test_coincident_roots_yield_exactly_one_entry(monkeypatch, tmp_path):
    """The majority-of-machines case, and the invariant every converted report
    depends on: when the launcher exports CLAUDE_CONFIG_DIR=$CLAUDE_AGENT_HOME
    the union must NOT double-count every session."""
    root = tmp_path / "one-root"
    (root / "projects").mkdir(parents=True)
    _both(monkeypatch, root, root)

    assert config_root.projects_roots() == [root / "projects"]


def test_a_root_without_a_projects_dir_is_skipped_not_raised_on(monkeypatch, tmp_path):
    agent = tmp_path / "agent-root"
    harness = tmp_path / "harness-root"
    (agent / "projects").mkdir(parents=True)
    harness.mkdir()  # exists, but has never held a session
    _both(monkeypatch, agent, harness)

    assert config_root.projects_roots() == [agent / "projects"]


def test_neither_root_has_projects_yields_empty_not_error(monkeypatch, tmp_path):
    agent = tmp_path / "agent-root"
    harness = tmp_path / "harness-root"
    agent.mkdir()
    harness.mkdir()
    _both(monkeypatch, agent, harness)

    assert config_root.projects_roots() == []


def test_a_symlinked_duplicate_collapses(monkeypatch, tmp_path):
    """A half-migrated machine can have ~/.claude/projects symlinked at the
    isolated root. Two different-looking paths, one directory — dedup is by
    resolved identity, or every transcript is counted twice."""
    real = tmp_path / "real-root"
    (real / "projects").mkdir(parents=True)
    link = tmp_path / "link-root"
    link.mkdir()
    (link / "projects").symlink_to(real / "projects")
    _both(monkeypatch, real, link)

    assert config_root.projects_roots() == [real / "projects"]


def test_a_project_present_under_both_roots_yields_both_directories(monkeypatch, tmp_path):
    """Dedup is by ROOT, never by project: the same cwd-hash under two roots is
    two different session sets, and a `--project` selector must union them."""
    agent = tmp_path / "agent-root"
    harness = tmp_path / "harness-root"
    name = "-Users-the0-somewhere"
    (agent / "projects" / name).mkdir(parents=True)
    (harness / "projects" / name).mkdir(parents=True)
    _both(monkeypatch, agent, harness)

    assert [r / name for r in config_root.projects_roots()] == [
        agent / "projects" / name,
        harness / "projects" / name,
    ]


# ---------------------------------------------------------------------------
# iter_transcripts(): the transcript view layered on top
# ---------------------------------------------------------------------------

def test_iter_transcripts_spans_both_roots_and_sorts(monkeypatch, tmp_path):
    agent = tmp_path / "agent-root"
    harness = tmp_path / "harness-root"
    a = agent / "projects" / "proj-a" / "s1.jsonl"
    b = harness / "projects" / "proj-b" / "s2.jsonl"
    for p in (a, b):
        p.parent.mkdir(parents=True)
        p.write_text("{}\n", encoding="utf-8")
    _both(monkeypatch, agent, harness)

    assert config_root.iter_transcripts() == sorted([a, b])


def test_iter_transcripts_default_pattern_excludes_subagent_transcripts(monkeypatch, tmp_path):
    """The default selection is the main-session layout `<project>/<session>.jsonl`.

    Sub-agent transcripts live one level deeper (`<session>/subagents/*.jsonl`).
    Folding them in would change WHAT the converted reports count, not just where
    they look — the one thing this refactor must not do.
    """
    root = tmp_path / "root"
    main = root / "projects" / "proj" / "s1.jsonl"
    sub = root / "projects" / "proj" / "s1" / "subagents" / "a.jsonl"
    for p in (main, sub):
        p.parent.mkdir(parents=True)
        p.write_text("{}\n", encoding="utf-8")
    _both(monkeypatch, root, root)

    assert config_root.iter_transcripts() == [main]
    assert config_root.iter_transcripts("**/*.jsonl") == sorted([main, sub])


def test_iter_transcripts_with_no_roots_yields_empty(monkeypatch, tmp_path):
    agent = tmp_path / "a"
    harness = tmp_path / "h"
    agent.mkdir()
    harness.mkdir()
    _both(monkeypatch, agent, harness)

    assert config_root.iter_transcripts() == []


# ---------------------------------------------------------------------------
# The converted sites: each one looks through the accessor, not past it
# ---------------------------------------------------------------------------

def _load(name: str):
    """Load a hyphenated script by path (the repo idiom).

    Registered in sys.modules BEFORE exec: `dataclasses` resolves a string
    annotation by looking the defining module up there, and a module absent from
    sys.modules makes that lookup return None mid-decorator.
    """
    mod_name = f"_projects_roots_{name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPTS_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(mod_name, None)
        raise
    return mod


@pytest.fixture
def two_roots(monkeypatch, tmp_path):
    """Two populated roots, with the SAME project name under each — so a site
    that silently keeps one root returns half the sessions and fails here."""
    agent = tmp_path / "agent-root" / "projects"
    harness = tmp_path / "harness-root" / "projects"
    # The sanitized form of the cwd `/tmp/x`, so the `--cwd`-style entry points
    # can be driven with a real path rather than a patched sanitizer.
    name = "-tmp-x"
    for root, stem in ((agent, "from-agent"), (harness, "from-harness")):
        d = root / name
        d.mkdir(parents=True)
        # A timestamp, because a consumer that filters by window (policy-scorecard)
        # drops a transcript carrying none — which would make this fixture prove
        # "found nothing" for the wrong reason.
        (d / f"{stem}.jsonl").write_text(
            '{"timestamp": "2026-07-01T00:00:00+00:00"}\n', encoding="utf-8")
    monkeypatch.setattr(config_root, "agent_home", lambda: agent.parent)
    monkeypatch.setattr(config_root, "harness_config_root", lambda: harness.parent)
    return agent, harness, name


def test_skill_usage_audit_finds_transcripts_in_both_roots(two_roots):
    agent, harness, name = two_roots
    mod = _load("skill-usage-audit")

    assert {p.stem for p in mod.find_transcripts(None)} == {"from-agent", "from-harness"}
    # The --cwd path unions the same project name across roots too.
    assert {p.stem for p in mod.find_transcripts("/tmp/x")} == {"from-agent", "from-harness"}
    assert mod.find_transcripts("/tmp/absent") == []


def test_rule_salience_report_finds_transcripts_in_both_roots(two_roots):
    mod = _load("rule-salience-report")
    assert {p.stem for p in mod.find_transcripts()} == {"from-agent", "from-harness"}


def test_tool_usage_report_unions_one_project_across_roots(two_roots, monkeypatch):
    agent, harness, name = two_roots
    mod = _load("tool-usage-report")
    monkeypatch.setattr(mod, "sanitize_cwd", lambda cwd: name)

    class _Args:
        transcript = None
        cwd = "/anything"

    assert {p.stem for p in mod.resolve_transcripts(_Args())} == {
        "from-agent", "from-harness"}


def test_cost_report_resolve_project_unions_across_roots(two_roots):
    _agent, _harness, name = two_roots
    mod = _load("cost-report")
    assert {p.stem for p in mod.resolve_project(name)} == {"from-agent", "from-harness"}


def test_policy_scorecard_in_window_files_unions_across_roots(two_roots):
    _agent, _harness, name = two_roots
    mod = _load("policy-scorecard")

    named = {p.stem for p in mod.in_window_files(days=36500, project=name)}
    every = {p.stem for p in mod.in_window_files(days=36500, project=None)}
    assert named == {"from-agent", "from-harness"}
    assert every == {"from-agent", "from-harness"}


def test_agent_stats_project_sessions_union_across_roots(two_roots):
    _agent, _harness, name = two_roots
    mod = _load("agent-stats")
    found: set[str] = set()
    for root in config_root.projects_roots():
        found |= mod.project_sessions(root, name)
    assert found == {"from-agent", "from-harness"}


def test_stamp_memory_dates_personal_scope_spans_both_roots(monkeypatch, tmp_path):
    agent = tmp_path / "agent-root" / "projects"
    harness = tmp_path / "harness-root" / "projects"
    for root, stem in ((agent, "from-agent"), (harness, "from-harness")):
        mem = root / "proj" / "memory"
        mem.mkdir(parents=True)
        (mem / f"{stem}.md").write_text("---\nname: x\n---\n", encoding="utf-8")
    monkeypatch.setattr(config_root, "agent_home", lambda: agent.parent)
    monkeypatch.setattr(config_root, "harness_config_root", lambda: harness.parent)

    mod = _load("stamp-memory-dates")
    assert {p.stem for p in mod.iter_leaves("personal", None)} == {
        "from-agent", "from-harness"}


def test_self_diagnose_memory_roots_span_both_roots(monkeypatch, tmp_path):
    """self-diagnose hardcoded `~/.claude-agent/projects`, so it ignored
    $CLAUDE_CONFIG_DIR outright — the same defect one spelling further out."""
    agent = tmp_path / "agent-root" / "projects"
    harness = tmp_path / "harness-root" / "projects"
    for root in (agent, harness):
        (root / "proj" / "memory").mkdir(parents=True)
    monkeypatch.setattr(config_root, "agent_home", lambda: agent.parent)
    monkeypatch.setattr(config_root, "harness_config_root", lambda: harness.parent)
    monkeypatch.chdir(tmp_path)

    mod = _load("self-diagnose")
    roots = mod.default_memory_roots()
    assert agent / "proj" / "memory" in roots
    assert harness / "proj" / "memory" in roots


def test_spawn_specialist_snapshots_transcripts_from_both_roots(monkeypatch, tmp_path):
    """A child spawned by a bare-`claude` manager writes its transcript under the
    HARNESS root. Reading only the agent root made auto-discovery return None and
    the spawn silently lose its transcript pointer."""
    agent = tmp_path / "agent-root" / "projects"
    harness = tmp_path / "harness-root" / "projects"
    a = agent / "proj" / "s1.jsonl"
    # Deliberately nested: the snapshot uses the recursive pattern, because a
    # sub-agent transcript is still a transcript for "did a new file appear".
    b = harness / "proj" / "s2" / "subagents" / "child.jsonl"
    for p in (a, b):
        p.parent.mkdir(parents=True)
        p.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(config_root, "agent_home", lambda: agent.parent)
    monkeypatch.setattr(config_root, "harness_config_root", lambda: harness.parent)

    mod = _load("spawn-specialist")
    assert mod._snapshot_transcripts() == {a, b}


def test_spawn_specialist_discovers_a_new_transcript_under_the_harness_root(
        monkeypatch, tmp_path):
    agent = tmp_path / "agent-root" / "projects"
    harness = tmp_path / "harness-root" / "projects"
    agent.mkdir(parents=True)
    fresh = harness / "proj" / "new.jsonl"
    fresh.parent.mkdir(parents=True)
    fresh.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(config_root, "agent_home", lambda: agent.parent)
    monkeypatch.setattr(config_root, "harness_config_root", lambda: harness.parent)

    mod = _load("spawn-specialist")
    assert mod._discover_transcript_path(set(), timeout=1.0) == fresh


# ---------------------------------------------------------------------------
# The deletion itself: a missed consumer must not keep working on one root
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("script,const", [
    ("skill-usage-audit", "PROJECTS_ROOT"),
    ("tool-usage-report", "PROJECTS_ROOT"),
    ("rule-salience-report", "PROJECTS_ROOT"),
    ("policy-scorecard", "PROJECTS_DIR"),
    ("cost-report", "PROJECTS_DIR"),
    ("agent-stats", "PROJECTS_DIR"),
])
def test_the_single_root_module_constants_are_gone(script, const):
    """Deleting rather than retyping is what makes a missed consumer fail loudly
    on any executed path. It is only half the net — a consumer behind a flag
    (lint-prose-length.scan_dynamic_injection) is caught by the repo grep, not
    by this. Both halves are the stage's control."""
    assert not hasattr(_load(script), const)
