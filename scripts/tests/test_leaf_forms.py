"""A leaf is found by what it says it is, and the given and ontology forms have their own rules.

A regularity/v1, principle/v1 or ontology/v1 leaf resolves by its frontmatter `schema:` wherever the
file lies; a regularity may be given (from code, a document or a measurement) instead of induced; an
`ontology:<slug>` ref resolves to an ontology/v1 leaf or a Core ADR. Each test builds a leaves tree
under tmp_path and runs the rule through the library, the grounding seam, the verifier or the search
in-process, so replacing one seam turns exactly the tests of that rule red by assertion.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from agentctl import gates, grounding  # noqa: E402
from lib import leaf_forms  # noqa: E402
from lib import principle_provenance as pp  # noqa: E402

DATE = "2026-10-29"
GIVEN_FILE = "scripts/lib/widget.py"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RE = _load("record_experience_leaf_forms", "record-experience.py")
VLS = _load("verify_leaf_structure_leaf_forms", "verify-leaf-structure.py")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCTL_EDIT_LEDGER", str(tmp_path / "edit-ledger.jsonl"))
    monkeypatch.setenv("CLAUDE_MEMORY_RETRIEVAL_LEDGER", str(tmp_path / "retrieval.jsonl"))
    monkeypatch.setenv("AGENTCTL_ADVISOR", "0")
    monkeypatch.setenv(RE.worktree_route.GUARD_ENV, "1")


def _fm(**fields) -> str:
    return "---\n" + "".join(f"{k}: {v}\n" for k, v in fields.items() if v is not None) + "---\n"


def _regularity(schema_extra=None, sections=("Regularity", "Generality", "Refutation", "Induced from"),
                **fields) -> str:
    head = _fm(name="widget-regularity", description="a sample", type="reference",
               schema="regularity/v1", generality=1, created=DATE, last_verified=DATE,
               **(schema_extra or {}), **fields)
    return head + "\n# Sample\n\n" + "".join(f"## {s}\n\nWidgets drift.\n\n" for s in sections)


def _given(**fields) -> str:
    return _regularity(sections=("Regularity", "Given", "Refutation"), given="code",
                       given_source=GIVEN_FILE, **fields)


def _ontology(sections=("Distinction", "Difficulty", "Adequacy")) -> str:
    return (_fm(name="widget-distinction", description="a sample", type="reference",
                schema="ontology/v1", created=DATE, last_verified=DATE)
            + "\n# Sample\n\n" + "".join(f"## {s}\n\nA widget is not a gadget.\n\n" for s in sections))


class Tree:
    """A repository with `memory-global/leaves/`, a `docs/adr/` and the file a given leaf cites."""

    def __init__(self, tmp: Path) -> None:
        self.repo = tmp / "repo"
        self.leaves = self.repo / "memory-global" / "leaves"
        self.adr = self.repo / "docs" / "adr"
        for sub in ("regularities", "principles", "experience"):
            (self.leaves / sub).mkdir(parents=True)
        self.adr.mkdir(parents=True)
        (self.repo / GIVEN_FILE).parent.mkdir(parents=True)
        (self.repo / GIVEN_FILE).write_text("WIDGET = 1\n", encoding="utf-8")

    def write(self, rel: str, text: str) -> Path:
        path = self.leaves / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def corpus(self) -> pp.Corpus:
        return pp.load_corpus(self.leaves)

    def problems(self, source: str) -> list[str]:
        return grounding.source_problems(source, self.corpus(), self.adr)

    def findings(self) -> set[tuple[str, Path]]:
        return {(f.kind, f.path) for f in pp.unaccounted(self.corpus())}


@pytest.fixture
def tree(tmp_path) -> Tree:
    return Tree(tmp_path)


def test_schema_area_outside_dirs(tree):
    regularity = tree.write("misc/widget-regularity.md", _regularity(induced_from="[]"))
    principle = tree.write("misc/widget-principle.md", _fm(
        name="widget-principle", description="a sample", type="reference", schema="principle/v1",
        generality=1, created=DATE, last_verified=DATE) + "\n## Principle\n\nRealign widgets.\n")
    ontology = tree.write("system-knowledge/widget-distinction.md", _ontology())
    legacy = tree.write("regularities/legacy.md", "---\nname: legacy\n---\n\n# Legacy\n")
    areas = {leaf.path: leaf.area for leaf in tree.corpus().items}
    assert areas[regularity] == "regularity"
    assert areas[principle] == "principle"
    assert areas[ontology] == "ontology"
    assert areas[legacy] == "regularity"


def test_in_place_regularity_resolves(tree):
    tree.write("misc/widget-regularity.md", _regularity(induced_from="[]"))
    assert tree.problems("regularity:widget-regularity") == []
    assert tree.problems("regularity:no-such-regularity") == [
        "regularity:no-such-regularity: no such leaf"]


def test_in_place_regularity_snapshotted(tree):
    tree.write("misc/widget-regularity.md", _regularity(induced_from="[]"))
    target = tree.write("principles/widget-principle.md", "---\nschema: principle/v1\n---\n")
    assert "regularity:widget-regularity" in gates.snapshot_regularities(str(target))


def test_given_root_accepted(tree):
    given = tree.write("regularities/widget-regularity.md", _given())
    induced = tree.write("regularities/bare-regularity.md", _regularity(induced_from="[]"))
    assert leaf_forms.given_provenance(given.read_text(encoding="utf-8")) == "code"
    assert leaf_forms.given_provenance(induced.read_text(encoding="utf-8")) is None
    found = tree.findings()
    assert not {kind for kind, path in found if path == given}
    assert {kind for kind, path in found if path == induced}


def test_given_source_missing_flagged(tree):
    missing = tree.write("regularities/missing-regularity.md", _given().replace(GIVEN_FILE, "scripts/none.py"))
    outside = tree.write("regularities/outside-regularity.md", _given().replace(GIVEN_FILE, "../elsewhere.py"))
    nameless = tree.write("regularities/nameless-regularity.md", _given().replace(f"given_source: {GIVEN_FILE}\n", ""))
    sound = tree.write("regularities/sound-regularity.md", _given())
    found = tree.findings()
    assert (pp.GIVEN_SOURCE, missing) in found
    assert (pp.GIVEN_SOURCE, outside) in found
    assert (pp.GIVEN_SOURCE, nameless) in found
    assert (pp.GIVEN_SOURCE, sound) not in found


def test_given_sections_accepted(tree):
    assert leaf_forms.regularity_section_problems(_given()) == []
    assert leaf_forms.regularity_section_problems(_given().replace("## Given", "## Other")) == ["## Given"]
    assert leaf_forms.regularity_section_problems(
        _regularity(sections=("Regularity", "Given", "Refutation"))) == ["## Generality", "## Induced from"]


def test_leaf_structure_accepts_given_form():
    assert VLS.check_content(_given(), "memory-global/leaves/regularities/widget-regularity.md") is None
    refused = VLS.check_content(_given().replace("## Given", "## Other"),
                                "memory-global/leaves/regularities/widget-regularity.md")
    assert refused and "## Given" in refused


def test_ontology_sections_checked():
    assert leaf_forms.ontology_section_problems(_ontology()) == []
    assert leaf_forms.ontology_section_problems(_ontology(("Distinction", "Difficulty"))) == ["## Adequacy"]
    assert leaf_forms.ontology_section_problems(_regularity(induced_from="[]")) == []


def test_leaf_structure_refuses_ontology_without_adequacy():
    path = "memory-global/leaves/system-knowledge/widget-distinction.md"
    assert VLS.check_content(_ontology(), path) is None
    refused = VLS.check_content(_ontology(("Distinction", "Difficulty")), path)
    assert refused and "## Adequacy" in refused


def test_ontology_ref_accepted(tree):
    tree.write("system-knowledge/widget-distinction.md", _ontology())
    assert tree.problems("ontology:widget-distinction") == []


def test_ontology_adr_ref_accepted(tree):
    (tree.adr / "0042-widget-decision.md").write_text("# Widget decision\n", encoding="utf-8")
    assert tree.problems("ontology:0042-widget-decision") == []


def test_ontology_kind_mismatch_refused(tree):
    tree.write("regularities/widget-regularity.md", _regularity(induced_from="[]"))
    problems = tree.problems("ontology:widget-regularity")
    assert problems and "ontology" in problems[0] and "widget-regularity" in problems[0]


def test_ontology_unresolved_refused(tree):
    problems = tree.problems("ontology:no-such-distinction")
    assert len(problems) == 1 and "no-such-distinction" in problems[0]
    assert tree.problems("project:ontology:no-such-distinction")
    assert leaf_forms.TIER_AREAS["ontology"] == "ontology"


def test_search_finds_in_place_regularity(tree, monkeypatch, capsys):
    tree.write("misc/widget-regularity.md", _regularity(induced_from="[]"))
    tree.write("misc/gadget-regularity.md", _regularity(induced_from="[]").replace("Widgets", "Gadgets"))
    (tree.leaves / "regularities" / "MEMORY.md").write_text("# regularities\n", encoding="utf-8")
    monkeypatch.setattr(RE, "REPO_ROOT", tree.repo)
    assert RE.main(["search", "--tier", "regularities", "widgets"]) == 0
    out = capsys.readouterr().out
    assert "widget-regularity.md" in out and "gadget-regularity.md" not in out
