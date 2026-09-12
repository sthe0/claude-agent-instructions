"""A re-SELECTED material must be visible to the coverage gate; a re-WORDED one must not.

Defect 4 of the SMD act-modelling rework. The engine classifies a difficulty's address as
"ресурсное" — material or means — but the CHANGE half of `replan_coverage_blockers` could
not see a material at all: `subject.material` is prose, and prose is excluded from the
operative surface on purpose, so that no amount of narrative rewriting satisfies the gate.

These tests pin BOTH halves of the resulting arrangement, because either alone is a defect:

  * the typed projections (`material_refs`/`knowledge_refs`) ARE in the surface, so a
    re-selection registers as a change;
  * the prose (`material`, `knowledge`) is STILL OUT of it, so a rewrite registers as
    nothing. A future change that admits prose to make the gate "see the material" would
    restore the blocker's appearance while destroying the property it exists for, and it
    is `test_material_prose_change_is_not_a_change` that must go red when it does.

Three further tests pin design decisions of the projection itself — the sorted order, the
declared-only contribution, and the string encoding — each of which is invisible in normal
use and each of which a plausible "tidying" would reverse. The residual is deliberate and
NOT pinned here because nothing can pin it: a projection is a DECLARATION, so appending a
path satisfies the gate without any re-selection having happened.
"""
import pytest

from agentctl import gates
from agentctl.plan import parse_plan
from agentctl.state import Critique
from ast_purity import impure_names


def _stage(index=1, *, means="Edit", method="do", material="the material prose",
           knowledge=None, material_refs=None, knowledge_refs=None,
           verify_venue_at_final=None, conditions=None, invariants=None):
    s = {
        "index": index, "title": "s", "executor": "in_thread",
        "expected_result_image": "img", "done_criterion": "dc",
        "means": means, "method": method, "material": material,
    }
    for key, value in (
        ("knowledge", knowledge),
        ("material_refs", material_refs),
        ("knowledge_refs", knowledge_refs),
        ("verify_venue_at_final", verify_venue_at_final),
        ("conditions", conditions),
        ("invariants", invariants),
    ):
        if value is not None:
            s[key] = value
    return s


def _doc(stages, **meta_kw):
    meta = {"task_id": "t"}
    meta.update(meta_kw)
    return parse_plan({"meta": meta, "stage": stages})


def _critique(**kw):
    base = dict(functional_ground="fg", replanning_task="rt",
                invariants_to_preserve=[], differences_to_remove=["some difference"])
    base.update(kw)
    return Critique(**base)


def _blockers(old, new, **crit_kw):
    return gates.replan_coverage_blockers(old, new, _critique(**crit_kw))


# --- refs differ: a re-selection is a change ---------------------------------------

def test_material_refs_change_is_a_change():
    old = _doc([_stage(material_refs=["scripts/agentctl/gates.py"])])
    new = _doc([_stage(material_refs=["scripts/agentctl/plan.py"])])
    assert gates._operative_surface(old) != gates._operative_surface(new)
    assert _blockers(old, new) == []


def test_knowledge_refs_change_is_a_change():
    old = _doc([_stage(knowledge_refs=["docs/architecture/ADR-0004-a-priori-typing.md"])])
    new = _doc([_stage(knowledge_refs=["memory-global/leaves/plan-activity-ontology.md"])])
    assert gates._operative_surface(old) != gates._operative_surface(new)
    assert _blockers(old, new) == []


def test_declaring_a_first_ref_is_a_change():
    """The absent -> declared transition, which the declared-only contribution below
    makes a change of tuple LENGTH rather than of a component's value."""
    old = _doc([_stage()])
    new = _doc([_stage(material_refs=["scripts/agentctl/gates.py"])])
    assert gates._operative_surface(old) != gates._operative_surface(new)
    assert _blockers(old, new) == []


# --- prose differs: a rewording is not ---------------------------------------------

def test_material_prose_change_is_not_a_change():
    """The property the whole projection exists to preserve. If this goes red because
    `subject.material` was admitted to the surface, the CHANGE half has become
    satisfiable by rewriting a narrative — which is what it was built to refuse."""
    old = _doc([_stage(material="the material prose")])
    new = _doc([_stage(material="a wholly rewritten narrative naming the same objects")])
    assert gates._operative_surface(old) == gates._operative_surface(new)
    blockers = _blockers(old, new)
    assert blockers and "operative surface" in blockers[0]


def test_knowledge_prose_change_is_not_a_change():
    old = _doc([_stage(knowledge_refs=["k.md"], knowledge="what the stage relies on")])
    new = _doc([_stage(knowledge_refs=["k.md"], knowledge="the same reliance, restated")])
    assert gates._operative_surface(old) == gates._operative_surface(new)
    blockers = _blockers(old, new)
    assert blockers and "operative surface" in blockers[0]


def test_surrounding_whitespace_on_a_ref_is_not_a_change():
    """Leading/trailing whitespace is an authoring artifact of a TOML list entry, not a
    re-selection, so it is stripped before the ref enters the surface. A future change
    that compares refs byte-raw would make re-indenting a list satisfy the CHANGE half."""
    old = _doc([_stage(material_refs=["scripts/agentctl/gates.py"])])
    new = _doc([_stage(material_refs=["  scripts/agentctl/gates.py  "])])
    assert gates._operative_surface(old) == gates._operative_surface(new)


def test_a_case_distinct_ref_is_a_different_referent():
    """Refs are structural identifiers, so they are NOT casefolded — the one departure
    from the surface's every-string-normalized rule. `Stage` and `stage` are two symbols
    and a tree tracks `Gates.py` and `gates.py` as two files, so re-selecting between them
    is a genuine re-selection. A future tidying that routes refs through
    `text_shape.normalize_string` for consistency would make that re-selection invisible
    and block the very replan this component exists to admit."""
    old = _doc([_stage(material_refs=["scripts/agentctl/gates.py:Stage"])])
    new = _doc([_stage(material_refs=["scripts/agentctl/gates.py:stage"])])
    assert gates._operative_surface(old) != gates._operative_surface(new)


def test_reordering_refs_is_not_a_change():
    """Each list is sorted before it enters the surface. Treating a shuffle as a
    re-selection would make the CHANGE half satisfiable by permuting a list, which is
    the same defect as satisfying it by rewording."""
    old = _doc([_stage(material_refs=["a.py", "b.py"])])
    new = _doc([_stage(material_refs=["b.py", "a.py"])])
    assert gates._operative_surface(old) == gates._operative_surface(new)


# --- the PRESERVE half is not weakened by the widening -----------------------------

def test_preserve_half_still_fires_after_the_widening():
    """Asserted AFTER the surface was widened, because the stage's invariant is that no
    existing blocker is WEAKENED and this is the case that executes it: a declared
    similarity absent from every stage's conditions/invariants must still block, and a
    re-selected material must not buy it off."""
    old = _doc([_stage(conditions="keep idempotency", material_refs=["a.py"])])
    new = _doc([_stage(conditions="a different condition entirely", material_refs=["b.py"])])
    blockers = _blockers(old, new, invariants_to_preserve=["keep idempotency"],
                        differences_to_remove=[])
    assert blockers and "keep idempotency" in blockers[0]


def test_preserve_half_still_passes_on_a_carried_item():
    old = _doc([_stage(material_refs=["a.py"])])
    new = _doc([_stage(conditions="keep idempotency", material_refs=["b.py"])])
    assert _blockers(old, new, invariants_to_preserve=["keep idempotency"],
                     differences_to_remove=[]) == []


# --- schema-23 identity, and the sort it constrains --------------------------------

def test_a_stage_declaring_neither_field_keeps_its_prior_surface():
    """A plan written before these fields existed must produce the surface it produced
    then — so the projection contributes NOTHING when both lists are empty, rather than
    a pair of empty tuples. Same declared-only rule as verify_venue_at_final."""
    absent = _doc([_stage()])
    empty = _doc([_stage(material_refs=[], knowledge_refs=[])])
    assert gates._operative_surface(absent) == gates._operative_surface(empty)
    stage_tuple = gates._operative_surface(absent)[0][0]
    assert gates._refs_projection(absent.stages[0].subject) == ()
    assert len(gates._operative_surface(empty)[0][0]) == len(stage_tuple)


def test_two_conditional_components_do_not_collide_in_the_sort():
    """Regression lock for why the projection is encoded as a STRING. The per-stage
    tuples are `sorted`, and with two conditional components of different types two
    stages tying on every unconditional field — one declaring only verify_venue_at_final,
    the other only refs — would reach a str-vs-tuple comparison and raise TypeError. Any
    third conditional component must be a string for the same reason."""
    doc = _doc(
        [
            _stage(1, verify_venue_at_final="repo_root"),
            _stage(2, material_refs=["a.py"]),
        ],
        repo_root="/repo", delivery_worktree="/repo-wt",
    )
    assert gates._operative_surface(doc)  # would raise TypeError, not fail an assert


def test_material_and_knowledge_projections_are_distinguishable():
    """The two lists are one grouped component; declaring the same ref on the other
    projection is a different declaration and must register as one."""
    as_material = _doc([_stage(material_refs=["a.py"])])
    as_knowledge = _doc([_stage(knowledge_refs=["a.py"])])
    assert gates._operative_surface(as_material) != gates._operative_surface(as_knowledge)


# --- purity: the surface reads the plan and reaches nothing ------------------------

@pytest.mark.parametrize("fn", [
    gates._refs_projection, gates._operative_surface, gates.replan_coverage_blockers,
])
def test_surface_stays_pure(fn):
    """The existing assertion, invoked rather than re-implemented: `impure_names` admits
    file I/O (gates.py already reads and hashes plan bytes) and rejects any
    {subprocess, socket, urllib, requests, http} reach. Nothing this stage added may
    change that — the semantic cognition of this rework lives on the submission seam."""
    assert impure_names(fn) == set()
