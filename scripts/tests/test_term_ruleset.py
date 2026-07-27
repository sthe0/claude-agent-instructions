"""lib/term_ruleset.py: schema, discovery, compose semantics, matcher (C1 mechanism).

All terms used here are synthetic (zorblex, gizmotron, shared-term) — this
file must never carry a real org-internal identifier, since it lives inside
the very tree the mechanism guards.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lib import term_ruleset as tr

DENY_ZORBLEX = """
[[deny]]
pattern = 'zorblex'
label = "codename"
note = "synthetic test term"
"""

DENY_ZORBLEX_WORD_BOUNDARY = r"""
[[deny]]
pattern = '\bzorblex\b'
"""

EXEMPT_ZORBLEX_PUBLIC = """
[[deny]]
pattern = 'zorblex'

[[exempt]]
pattern = 'zorblex-public'
reason = "publicly-reachable product name; safe to reference"
"""

DENY_GIZMOTRON = """
[[deny]]
pattern = 'gizmotron'
"""

SHARED_TERM_WITH_EXEMPT = """
[[deny]]
pattern = 'shared-term'

[[exempt]]
pattern = 'shared-term-public'
reason = "safe"
"""

SHARED_TERM_NO_EXEMPT = """
[[deny]]
pattern = 'shared-term'
"""

GRANDFATHERED_ZORBLEX = """
[[deny]]
pattern = 'zorblex'

[[grandfather]]
path = "legacy/**"
reason = "TODO(owner): migrate off zorblex by next quarter"
"""

MISSING_EXEMPT_REASON = """
[[deny]]
pattern = 'zorblex'

[[exempt]]
pattern = 'zorblex-public'
"""

MISSING_GRANDFATHER_REASON = """
[[grandfather]]
path = "legacy/**"
"""

MISSING_DENY_PATTERN = """
[[deny]]
label = "no pattern here"
"""

INVALID_TOML = "this is not [ valid toml"


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / f"{name}.toml"
    path.write_text(content, encoding="utf-8")
    return path


# ── load_ruleset: schema validation ─────────────────────────────────────────

def test_load_ruleset_parses_deny_exempt_grandfather(tmp_path):
    rs = tr.load_ruleset(_write(tmp_path, "a", GRANDFATHERED_ZORBLEX))
    assert len(rs.deny) == 1
    assert rs.deny[0].pattern == "zorblex"
    assert len(rs.grandfather) == 1
    assert rs.grandfather[0].path_glob == "legacy/**"


def test_load_ruleset_missing_exempt_reason_raises(tmp_path):
    with pytest.raises(tr.RulesetError, match="reason"):
        tr.load_ruleset(_write(tmp_path, "bad", MISSING_EXEMPT_REASON))


def test_load_ruleset_missing_grandfather_reason_raises(tmp_path):
    with pytest.raises(tr.RulesetError, match="reason"):
        tr.load_ruleset(_write(tmp_path, "bad", MISSING_GRANDFATHER_REASON))


def test_load_ruleset_missing_deny_pattern_raises(tmp_path):
    with pytest.raises(tr.RulesetError, match="pattern"):
        tr.load_ruleset(_write(tmp_path, "bad", MISSING_DENY_PATTERN))


def test_load_ruleset_invalid_toml_raises(tmp_path):
    with pytest.raises(tr.RulesetError):
        tr.load_ruleset(_write(tmp_path, "bad", INVALID_TOML))


# ── scan: content and path hits ─────────────────────────────────────────────

def test_content_hit_fires(tmp_path):
    rs = tr.load_ruleset(_write(tmp_path, "a", DENY_ZORBLEX))
    hits = tr.scan("mentions zorblex here", [rs])
    assert len(hits) == 1
    assert hits[0].source == "content"


def test_path_hit_fires(tmp_path):
    rs = tr.load_ruleset(_write(tmp_path, "a", DENY_ZORBLEX))
    hits = tr.scan("clean content", [rs], path="docs/zorblex-notes.md")
    assert any(h.source == "path" for h in hits)


def test_clean_text_no_hits(tmp_path):
    rs = tr.load_ruleset(_write(tmp_path, "a", DENY_ZORBLEX))
    assert tr.scan("nothing suspicious here", [rs], path="docs/clean.md") == []


def test_word_boundary_avoids_false_positive(tmp_path):
    rs = tr.load_ruleset(_write(tmp_path, "a", DENY_ZORBLEX_WORD_BOUNDARY))
    assert tr.scan("a zorblexy word should not match", [rs]) == []
    assert len(tr.scan("bare zorblex should match", [rs])) == 1


# ── exempt: occurrence-level containment ────────────────────────────────────

def test_exempt_containment_suppresses_hit(tmp_path):
    rs = tr.load_ruleset(_write(tmp_path, "a", EXEMPT_ZORBLEX_PUBLIC))
    assert tr.scan("we use zorblex-public everywhere", [rs]) == []


def test_exempt_does_not_suppress_same_word_elsewhere(tmp_path):
    rs = tr.load_ruleset(_write(tmp_path, "a", EXEMPT_ZORBLEX_PUBLIC))
    hits = tr.scan("zorblex-public is fine but zorblex alone is not", [rs])
    assert len(hits) == 1


def test_exempt_scoped_per_ruleset_not_cross_ruleset(tmp_path):
    """An exempt entry in ruleset A must never blind a hit from ruleset B's deny
    pattern, even when both rulesets deny the identical term — otherwise a
    low-stakes personal ruleset could silently defeat a stricter team one."""
    rs_a = tr.load_ruleset(_write(tmp_path, "a", SHARED_TERM_WITH_EXEMPT))
    rs_b = tr.load_ruleset(_write(tmp_path, "b", SHARED_TERM_NO_EXEMPT))
    hits = tr.scan("shared-term-public mentioned", [rs_a, rs_b])
    assert len(hits) == 1
    assert hits[0].ruleset == "b"


# ── grandfather: path-scoped, whole-ruleset suppression ─────────────────────

def test_grandfather_suppresses_hits_on_matching_path(tmp_path):
    rs = tr.load_ruleset(_write(tmp_path, "a", GRANDFATHERED_ZORBLEX))
    assert tr.scan("has zorblex in it", [rs], path="legacy/old.md") == []


def test_grandfather_does_not_affect_other_paths(tmp_path):
    rs = tr.load_ruleset(_write(tmp_path, "a", GRANDFATHERED_ZORBLEX))
    assert len(tr.scan("has zorblex in it", [rs], path="current/new.md")) == 1


def test_ignore_grandfather_forces_the_hit_back(tmp_path):
    rs = tr.load_ruleset(_write(tmp_path, "a", GRANDFATHERED_ZORBLEX))
    hits = tr.scan("has zorblex in it", [rs], path="legacy/old.md", ignore_grandfather=True)
    assert len(hits) == 1


# ── discover_rulesets: three-tier order, override replaces (not unions) ─────

def test_discovers_personal_dir_ruleset(tmp_path):
    agent_home = tmp_path / "home"
    (agent_home / tr.PERSONAL_DIRNAME).mkdir(parents=True)
    _write(tmp_path, "unused", DENY_ZORBLEX)  # sanity: writing elsewhere doesn't leak in
    (agent_home / tr.PERSONAL_DIRNAME / "personal.toml").write_text(DENY_ZORBLEX, encoding="utf-8")

    rulesets = tr.discover_rulesets(agent_home=agent_home, project_dir=None, env={})
    assert len(rulesets) == 1
    assert rulesets[0].name == "personal"


def test_discovers_team_dir_ruleset_and_unions_with_personal(tmp_path):
    agent_home = tmp_path / "home"
    (agent_home / tr.PERSONAL_DIRNAME).mkdir(parents=True)
    (agent_home / tr.PERSONAL_DIRNAME / "personal.toml").write_text(DENY_ZORBLEX, encoding="utf-8")

    project_dir = tmp_path / "project"
    (project_dir / tr.TEAM_DIRNAME).mkdir(parents=True)
    (project_dir / tr.TEAM_DIRNAME / "team.toml").write_text(DENY_GIZMOTRON, encoding="utf-8")

    rulesets = tr.discover_rulesets(agent_home=agent_home, project_dir=project_dir, env={})
    names = {rs.name for rs in rulesets}
    assert names == {"personal", "team"}


def test_env_override_replaces_discovery_not_unions(tmp_path):
    agent_home = tmp_path / "home"
    (agent_home / tr.PERSONAL_DIRNAME).mkdir(parents=True)
    (agent_home / tr.PERSONAL_DIRNAME / "personal.toml").write_text(DENY_ZORBLEX, encoding="utf-8")

    override_dir = tmp_path / "override_empty"
    override_dir.mkdir()

    # Override points at an EMPTY dir: zero rulesets, even though a Personal-dir
    # ruleset also exists — the override REPLACES the discovery set, not unions.
    rulesets = tr.discover_rulesets(
        agent_home=agent_home, project_dir=None,
        env={tr.TERM_RULESET_DIR_ENV: str(override_dir)},
    )
    assert rulesets == []

    # Without the override, the same Personal-dir ruleset IS discovered.
    rulesets2 = tr.discover_rulesets(agent_home=agent_home, project_dir=None, env={})
    assert len(rulesets2) == 1


def test_no_dirs_present_yields_zero_rulesets_not_an_error(tmp_path):
    agent_home = tmp_path / "home-does-not-exist"
    assert tr.discover_rulesets(agent_home=agent_home, project_dir=None, env={}) == []


# ── self-publication refusal ─────────────────────────────────────────────────

def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)


def test_self_publication_refused_when_ruleset_tracked_in_guarded_repo(tmp_path):
    repo = tmp_path / "guarded-repo"
    _init_git_repo(repo)
    real_ruleset = repo / "leaked.toml"
    real_ruleset.write_text(DENY_ZORBLEX, encoding="utf-8")
    subprocess.run(["git", "add", "leaked.toml"], cwd=repo, check=True, capture_output=True)

    # Mirrors a real agent-home layout, where config files are symlinks INTO
    # the guarded repo — discovery must resolve the symlink before checking
    # `git ls-files`, or the self-publication would slip through undetected.
    agent_home = tmp_path / "home"
    (agent_home / tr.PERSONAL_DIRNAME).mkdir(parents=True)
    (agent_home / tr.PERSONAL_DIRNAME / "leaked.toml").symlink_to(real_ruleset)

    with pytest.raises(tr.RulesetError, match="self-publication"):
        tr.discover_rulesets(
            agent_home=agent_home, project_dir=None, guarded_repo_root=repo, env={},
        )


def test_ruleset_outside_guarded_repo_loads_fine(tmp_path):
    repo = tmp_path / "guarded-repo"
    _init_git_repo(repo)

    agent_home = tmp_path / "home"
    (agent_home / tr.PERSONAL_DIRNAME).mkdir(parents=True)
    (agent_home / tr.PERSONAL_DIRNAME / "personal.toml").write_text(DENY_ZORBLEX, encoding="utf-8")

    rulesets = tr.discover_rulesets(
        agent_home=agent_home, project_dir=None, guarded_repo_root=repo, env={},
    )
    assert len(rulesets) == 1
