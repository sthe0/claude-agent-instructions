"""The project memory tier has the root's distinction between knowledge and prescription.

A project keeps its knowledge as regularities (`regularities/`, induced from its experience) and its
norms as principles (`principles/`, grounded in regularities: the project's own first, then the
global ones). A project principle is no longer a generalization target, so `induced_from` on one is
flagged like on a global principle. Each test builds a two-tier memory under tmp_path and runs the
rule through the library, the grounding seam, `promote` or the verifier's `main(argv)` in-process,
so replacing one seam turns exactly the tests of that rule red by assertion.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from lib import principle_provenance as pp  # noqa: E402

DATE = "2026-10-29"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RE = _load("record_experience_project_tier", "record-experience.py")
VPP = _load("verify_principle_provenance_project_tier", "verify-principle-provenance.py")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCTL_EDIT_LEDGER", str(tmp_path / "edit-ledger.jsonl"))
    monkeypatch.setenv("CLAUDE_MEMORY_RETRIEVAL_LEDGER", str(tmp_path / "retrieval.jsonl"))
    monkeypatch.setenv("AGENTCTL_ADVISOR", "0")
    monkeypatch.setenv(RE.worktree_route.GUARD_ENV, "1")


def _main(module, *argv) -> int:
    try:
        return module.main([str(a) for a in argv])
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1


def _fm(**fields) -> str:
    return "---\n" + "".join(f"{k}: {v}\n" for k, v in fields.items() if v is not None) + "---\n"


class Memory:
    """A global `leaves/` tree and a project memory dir; `repo` is the project's repo dir."""

    def __init__(self, tmp: Path) -> None:
        self.root = tmp / "mem"
        self.leaves = self.root / "leaves"
        self.repo = tmp / "acme-widgets"
        self.project = self.repo / ".claude" / "agent-memory"
        self.dirs = {"global": self.leaves, "project": self.project}
        for base in self.dirs.values():
            for sub in ("experience", "regularities", "principles"):
                (base / sub).mkdir(parents=True, exist_ok=True)

    def _write(self, tier, sub, slug, text) -> Path:
        path = self.dirs[tier] / sub / f"{slug}.md"
        path.write_text(text, encoding="utf-8")
        return path

    def _point(self, tier, sub, slug, sources) -> None:
        index = self.dirs[tier] / sub / pp.INDEX
        old = index.read_text(encoding="utf-8") if index.exists() else f"# {sub}\n\n"
        sources = ", ".join(f"`{s}`" for s in sources)
        index.write_text(f"{old}- [{slug}]({slug}.md) — sample {pp.ARROW}{sources}.\n", encoding="utf-8")

    def experience(self, slug, into=(), tier="project") -> Path:
        return self._write(tier, "experience", slug, _fm(
            name=slug, description="a sample", type="reference", schema="difficulty/v1",
            generalized_into=pp.format_list(into), resolution_confirmed_by_user='"yes"',
            created=DATE, last_verified=DATE) + "\n# Sample\n\n## Difficulty\n\nA widget drifts.\n")

    def regularity(self, slug, induced=(), tier="project", rule_derived=None) -> Path:
        path = self._write(tier, "regularities", slug, _fm(
            name=slug, description="a sample", type="reference", schema="regularity/v1",
            generality=1, induced_from=pp.format_list(induced),
            rule_derived=f'"{rule_derived}"' if rule_derived else None,
            created=DATE, last_verified=DATE) + "\n# Sample\n\n## Regularity\n\nWidgets drift.\n")
        self._point(tier, "regularities", slug, induced)
        return path

    def principle(self, slug, grounded=(), induced=None, tier="project", point=None) -> Path:
        path = self._write(tier, "principles", slug, _fm(
            name=slug, description="a sample", type="reference", schema="principle/v1",
            generality=1, grounded_in=pp.format_list([pp.REGULARITY_PREFIX + g for g in grounded]),
            induced_from=None if induced is None else pp.format_list(induced),
            created=DATE, last_verified=DATE) + "\n# Sample\n\n## Principle\n\nRealign widgets.\n")
        self._point(tier, "principles", slug, grounded if point is None else point)
        return path

    def corpus(self) -> pp.Corpus:
        return pp.load_corpus(self.leaves, self.project)

    def findings(self) -> set[tuple[str, Path]]:
        return {(f.kind, f.path) for f in pp.unaccounted(self.corpus())}

    def promote(self, *flags) -> int:
        return _main(RE, "promote", "--root", self.root, "--slug", "new-leaf", "--name", "A new leaf",
                     "--description", "a promoted leaf", "--generality", "1", "--date", DATE,
                     "--statement", "Widgets drift.", *flags)


@pytest.fixture
def mem(tmp_path) -> Memory:
    return Memory(tmp_path)


def test_project_regularity_area(mem):
    path = mem.regularity("widget-regularity", induced=["w1"])
    mem.experience("w1", into=["widget-regularity"])
    leaf = mem.corpus().find("widget-regularity", "project")
    assert (leaf.tier, leaf.area, leaf.path) == ("project", "regularity", path)
    assert [l.slug for l in mem.corpus().of("regularity", "project")] == ["widget-regularity"]
    assert pp.induced_leaves(mem.corpus()) == [leaf]


def test_project_regularity_ref_accepted(mem):
    from agentctl import grounding

    mem.regularity("widget-regularity", induced=["w1"])
    mem.experience("w1", into=["widget-regularity"])
    corpus = mem.corpus()
    assert grounding.source_problems("project:regularity:widget-regularity", corpus) == []
    assert grounding.source_problems("project:experience:w1", corpus) == []
    assert grounding.source_problems("regularity:widget-regularity", corpus) == [
        "regularity:widget-regularity: no such leaf"]


def test_project_principle_needs_grounding(mem):
    bare = mem.principle("bare-principle", grounded=[])
    unknown = mem.principle("unknown-principle", grounded=["no-such-regularity"])
    found = mem.findings()
    assert (pp.UNGROUNDED_PRINCIPLE, bare) in found
    assert (pp.UNKNOWN_REGULARITY, unknown) in found


def test_project_principle_induced_flagged(mem):
    mem.regularity("widget-regularity", rule_derived="seeded from rules")
    principle = mem.principle("widget-principle", grounded=["widget-regularity"], induced=["w1"])
    found = mem.findings()
    assert (pp.PRINCIPLE_INDUCED, principle) in found
    assert not {kind for kind, path in found if path == principle} - {pp.PRINCIPLE_INDUCED}


def test_project_experience_into_regularity(mem):
    listed = mem.experience("w1", into=["widget-regularity"])
    mem.experience("w2", into=["widget-regularity"])
    regularity = mem.regularity("widget-regularity", induced=["w1", "w3"])
    mem.experience("w3", into=[])
    found = mem.findings()
    assert not {(k, p) for k, p in found if p == listed}
    assert not {k for k, _ in found} & {pp.UNKNOWN_PRINCIPLE, pp.NO_SOURCE}
    assert (pp.ASYMMETRIC, regularity) in found
    assert (pp.ASYMMETRIC, mem.project / "experience" / "w2.md") in found


def test_project_grounds_global_fallback(mem):
    from agentctl import grounding

    mem.regularity("global-regularity", rule_derived="seeded from rules", tier="global")
    mem.regularity("local-regularity", rule_derived="seeded from rules")
    mem.principle("widget-principle", grounded=["local-regularity", "global-regularity"])
    corpus = mem.corpus()
    assert grounding.source_problems("project:regularity:global-regularity", corpus) == []
    assert grounding.source_problems("project:regularity:no-such", corpus) == [
        "project:regularity:no-such: no such leaf"]
    assert grounding.principle_hint("widget-principle", corpus, "project").endswith(
        "project:regularity:local-regularity, regularity:global-regularity")
    assert not {k for k, _ in mem.findings()}


def test_project_regularity_wins_over_same_global_slug(mem):
    from agentctl import grounding

    mem.regularity("shared-regularity", rule_derived="seeded from rules", tier="global")
    local = mem.regularity("shared-regularity", rule_derived="seeded from rules")
    found = grounding.leaf_for(grounding.GroundRef("project", "regularity", "shared-regularity"), mem.corpus())
    assert (found.tier, found.path) == ("project", local)
    assert grounding.leaf_for(grounding.GroundRef("global", "regularity", "shared-regularity"),
                              mem.corpus()).tier == "global"


def test_promote_writes_project_regularity(mem):
    leaf = mem.experience("w1")
    assert mem.promote("--induced-from", "w1", "--project-dir", mem.repo) == 0
    written = mem.project / "regularities" / "new-leaf.md"
    assert written.is_file()
    assert pp.field_value(written.read_text(encoding="utf-8"), "schema") == "regularity/v1"
    assert pp.read_induced(written.read_text(encoding="utf-8")) == ["w1"]
    assert "new-leaf.md" in (mem.project / "regularities" / pp.INDEX).read_text(encoding="utf-8")
    assert pp.read_status(leaf.read_text(encoding="utf-8")) == ["new-leaf"]
    assert not (mem.leaves / "regularities" / "new-leaf.md").exists()
    assert not (mem.project / "principles" / "new-leaf.md").exists()
    assert pp.promote_target_dir(mem.leaves, mem.project) == mem.project / "regularities"
    assert pp.promote_target_dir(mem.leaves) == mem.leaves / "regularities"


def test_verify_reads_project_dir(mem, capsys):
    mem.regularity("global-regularity", rule_derived="seeded from rules", tier="global")
    mem.principle("bare-principle", grounded=[])
    assert _main(VPP, "--root", mem.root.parent / "elsewhere") == 0
    root = mem.root.parent / "repo-root"
    (root / pp.LEAVES_REL).parent.mkdir(parents=True)
    (root / pp.LEAVES_REL).symlink_to(mem.leaves)
    assert _main(VPP, "--root", root) == 0
    assert _main(VPP, "--root", root, "--project", mem.repo) == 1
    assert pp.UNGROUNDED_PRINCIPLE in capsys.readouterr().out
    assert _main(VPP, "--root", root, "--project", mem.project) == 1


def test_verify_refuses_a_missing_project_dir(mem, capsys):
    root = mem.root.parent / "repo-root"
    (root / pp.LEAVES_REL).parent.mkdir(parents=True)
    (root / pp.LEAVES_REL).symlink_to(mem.leaves)
    assert _main(VPP, "--root", root, "--project", mem.root.parent / "no-such-repo") == 2
    assert "--project" in capsys.readouterr().err
