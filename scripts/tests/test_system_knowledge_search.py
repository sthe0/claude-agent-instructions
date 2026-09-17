"""record-experience.py search over the system-knowledge tier.

Retrieval-augmented planning needs `search --tier system-knowledge` to surface durable
system facts BEFORE an approach is designed, not only `experience`/`principles`. These
tests assert:
  - the system-knowledge tier is searchable at both scopes (global -> memory-global/leaves/
    system-knowledge, project -> <project_dir>/.claude/agent-memory/system-knowledge);
  - it reuses the existing "Difficulty"-section ranking convention (no bespoke whole-body
    ranking basis), matching leaf-schema.md's difficulty-lead requirement for this tier;
  - a known leaf is returned for a matching query at each scope;
  - the existing experience/principles tier behaviour is unchanged.
"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

# record-experience.py is hyphenated — not an importable module name.
_SPEC = importlib.util.spec_from_file_location(
    "record_experience",
    Path(__file__).resolve().parents[1] / "record-experience.py",
)
rec = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rec)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _args(keywords, *, tier="experience", scope="global", project_dir=None, domain=None):
    return SimpleNamespace(keywords=keywords, tier=tier, scope=scope, project_dir=project_dir, domain=domain)


def test_system_knowledge_tier_uses_difficulty_section():
    # Reuse the existing Difficulty-led ranking convention — same as experience, not a new basis.
    assert rec.TIER_SECTION["system-knowledge"] == "Difficulty"


def test_system_knowledge_tier_root_resolves_global():
    root = rec.search_root("global", None, "system-knowledge")
    assert root == REPO_ROOT / "memory-global/leaves/system-knowledge"
    assert root.is_dir(), "global system-knowledge tier directory must exist to be searchable"


def test_system_knowledge_tier_root_resolves_project(tmp_path):
    root = rec.search_root("project", str(tmp_path), "system-knowledge")
    assert root == tmp_path / ".claude/agent-memory/system-knowledge"


def test_known_global_leaf_returned_for_matching_query(capsys):
    rc = rec.cmd_search(_args(
        "cross-session scope isolation two live sessions overlap",
        tier="system-knowledge",
    ))
    assert rc == 0
    out = capsys.readouterr().out
    assert "cross-session-scope-isolation.md" in out
    assert "system-knowledge leaf" in out  # tier-aware wording


def test_known_project_leaf_returned_for_matching_query(tmp_path, capsys):
    leaf_dir = tmp_path / ".claude/agent-memory/system-knowledge"
    leaf_dir.mkdir(parents=True)
    (leaf_dir / "widget-queue-retry-gotcha.md").write_text(
        "---\n"
        "name: widget-queue-retry-gotcha\n"
        'description: "Widget queue retries silently drop the idempotency key."\n'
        "type: reference\n"
        "created: 2026-01-01\n"
        "last_verified: 2026-01-01\n"
        "---\n\n"
        "# Widget queue retry gotcha\n\n"
        "> **Difficulty:** a naive retry against the widget queue silently drops the\n"
        "> idempotency key, causing duplicate processing.\n\n"
        "## Guidance\nAlways pass the key explicitly on retry.\n"
    )
    rc = rec.cmd_search(_args(
        "widget queue retry idempotency key duplicate",
        tier="system-knowledge",
        scope="project",
        project_dir=str(tmp_path),
    ))
    assert rc == 0
    out = capsys.readouterr().out
    assert "widget-queue-retry-gotcha.md" in out


def test_experience_and_principles_tiers_unchanged():
    # Invariant: adding a third tier does not disturb the other two.
    assert rec.search_root("global", None, "experience") == rec.experience_dir("global", None)
    assert rec.TIER_SECTION["experience"] == "Difficulty"
    assert rec.TIER_SECTION["principles"] == "Principle"
