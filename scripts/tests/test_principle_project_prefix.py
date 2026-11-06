"""A project principle grounded in `project:regularity:<slug>` yields the same slug as the bare form.

`grounded_regularities` is the reader of a principle's `grounded_in` that the index-pointer check
rests on; both ref forms must reach it as the bare slug, so a pointer naming that slug is no mismatch.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from lib import principle_provenance as pp  # noqa: E402

DATE = "2026-11-06"


def _fm(**fields) -> str:
    return "---\n" + "".join(f"{k}: {v}\n" for k, v in fields.items() if v is not None) + "---\n"


def _principle_text(grounds) -> str:
    return _fm(name="widget-principle", description="a sample", type="reference",
               schema="principle/v1", generality=1, grounded_in=pp.format_list(grounds),
               created=DATE, last_verified=DATE) + "\n# Sample\n\n## Principle\n\nRealign widgets.\n"


def _leaf(grounds):
    return type("Leaf", (), {"text": _principle_text(grounds)})()


@pytest.mark.parametrize("ref", ["regularity:widget-regularity", "project:regularity:widget-regularity"])
def test_both_ref_forms_yield_the_bare_slug(ref):
    assert pp.grounded_regularities(_leaf([ref])) == ["widget-regularity"]


def test_non_regularity_ground_is_still_dropped():
    leaf = _leaf(["project:regularity:widget-regularity", "ontology:widget-kinds"])
    assert pp.grounded_regularities(leaf) == ["widget-regularity"]


def test_project_principle_pointer_naming_its_project_regularity_is_no_mismatch(tmp_path):
    leaves = tmp_path / "mem" / "leaves"
    project = tmp_path / "acme-widgets" / ".claude" / "agent-memory"
    for base in (leaves, project):
        for sub in ("experience", "regularities", "principles"):
            (base / sub).mkdir(parents=True)
    (project / "principles" / "widget-principle.md").write_text(
        _principle_text(["project:regularity:widget-regularity"]), encoding="utf-8")
    (project / "principles" / pp.INDEX).write_text(
        f"# principles\n\n- [widget-principle](widget-principle.md) — sample {pp.ARROW}`widget-regularity`.\n",
        encoding="utf-8")
    corpus = pp.load_corpus(leaves, project)
    assert pp.index_mismatches(corpus) == []
