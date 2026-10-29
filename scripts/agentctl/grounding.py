"""Grounding check at the plan-submission seam.

A stage of a substantive plan rests on knowledge, never directly on a prescription: its
`[stage.principle].source` names only regularities, experience leaves and cases, each as
`[project:]<kind>:<slug>`, separated by `;`. A ref is resolved structurally -- by slug, corpus
area and frontmatter `schema:` -- never by a reading of what the leaf says. A principle ref is
recognized so its refusal can name the regularities the principle is grounded in.

Callers reach every seam as `grounding.<name>` so a control can replace one at its module
attribute.
"""
from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from pathlib import Path

from lib import principle_provenance as pp

from .submission import is_substantive

# kind -> (corpus area, frontmatter schema)
GROUND_KINDS = {
    "regularity": ("regularity", "regularity/v1"),
    "experience": ("experience", "difficulty/v1"),
    "case": ("case", "case/v1"),
}
REFUSED_KINDS = frozenset({"principle"})

_REF = re.compile(r"^(?:(project):)?([a-z]+):([a-z0-9][a-z0-9-]*)$")
_SCHEMA = re.compile(r"^schema[ \t]*:(.*)$", re.MULTILINE)


@dataclass(frozen=True)
class GroundRef:
    tier: str
    kind: str
    slug: str

    def text(self) -> str:
        return ("project:" if self.tier == "project" else "") + f"{self.kind}:{self.slug}"


def engine_leaves_root() -> Path:
    return Path(__file__).resolve().parents[2] / "memory-global" / "leaves"


def schema_of(text: str) -> str:
    front = pp.FRONTMATTER.match(text)
    found = _SCHEMA.search(front.group(1)) if front else None
    return found.group(1).strip() if found else ""


def parse_ground_refs(source: str):
    refs: list[GroundRef] = []
    bad: list[str] = []
    known = set(GROUND_KINDS) | set(REFUSED_KINDS)
    for item in (source or "").split(";"):
        item = item.strip()
        if not item:
            continue
        found = _REF.match(item)
        if found and found.group(2) in known:
            refs.append(GroundRef(found.group(1) or "global", found.group(2), found.group(3)))
        else:
            bad.append(item)
    return refs, bad


fallback_tiers = pp.fallback_tiers


def principle_hint(slug: str, corpus, tier: str = "global") -> str:
    leaf = leaf_for(GroundRef(tier, "principle", slug), corpus)
    grounds = pp.grounded_regularities(leaf) if leaf is not None else []
    if not grounds:
        return "a principle is a norm, not a ground; cite a regularity: regularity:<slug>"
    cited = []
    for g in grounds:
        found = leaf_for(GroundRef(leaf.tier, "regularity", g), corpus)
        cited.append(GroundRef(found.tier if found is not None else "global", "regularity", g).text())
    return ("a principle is a norm, not a ground; cite the regularities it is grounded in: "
            + ", ".join(cited))


def admits_staged(leaf_text: str, task_id) -> bool:
    return bool(task_id) and pp.field_value(leaf_text, pp.INTRODUCED_BY) == task_id


def load_ground_corpus(global_leaves, project_root=None, staged_leaves=None, task_id=None):
    corpus = pp.load_corpus(global_leaves, project_root)
    if staged_leaves is None:
        return corpus
    known = {leaf.slug for leaf in corpus.items if leaf.tier == "global"}
    extra = [leaf for leaf in pp.load_corpus(staged_leaves).items
             if leaf.tier == "global" and leaf.slug not in known
             and admits_staged(leaf.text, task_id)]
    return dataclasses.replace(corpus, items=corpus.items + extra)


def leaf_for(ref: GroundRef, corpus):
    """The leaf `ref` cites: its own kind's area in its own tier first, so a same-slug leaf of
    another area cannot shadow it; failing that any leaf of that slug in that tier, which
    `kind_matches` then refuses. A project ref falls back to the global tier when the project
    holds no such leaf."""
    area = GROUND_KINDS[ref.kind][0] if ref.kind in GROUND_KINDS else ref.kind
    tiers = fallback_tiers(ref.tier)
    for tier in tiers:
        in_area = [leaf for leaf in corpus.of(area, tier) if leaf.slug == ref.slug]
        if in_area:
            return in_area[0]
    for tier in tiers:
        elsewhere = [leaf for leaf in corpus.items if leaf.slug == ref.slug and leaf.tier == tier]
        if elsewhere:
            return elsewhere[0]
    return None


def kind_matches(ref: GroundRef, leaf) -> bool:
    pair = GROUND_KINDS.get(ref.kind)
    return pair is not None and (leaf.area, schema_of(leaf.text)) == pair


def source_problems(source: str, corpus) -> list[str]:
    if not (source or "").strip():
        return ["empty: cite at least one regularity, experience or case"]
    refs, bad = parse_ground_refs(source)
    out = [f"source item {item!r} is not a ground reference "
           f"`[project:]<kind>:<slug>` with kind in {sorted(GROUND_KINDS)}" for item in bad]
    for ref in refs:
        if ref.kind in REFUSED_KINDS:
            out.append(f"{ref.text()}: {principle_hint(ref.slug, corpus, ref.tier)}")
            continue
        leaf = leaf_for(ref, corpus)
        if leaf is None:
            out.append(f"{ref.text()}: no such leaf")
        elif not kind_matches(ref, leaf):
            area, schema = GROUND_KINDS[ref.kind]
            out.append(f"{ref.text()}: the leaf is {leaf.area} / {schema_of(leaf.text) or 'no schema'}, "
                       f"not {area} / {schema}")
    return out


def grounding_violations(doc, *, corpus, exempt_stages=frozenset()) -> list[str]:
    if not is_substantive(doc):
        return []
    out: list[str] = []
    for stage in doc.stages:
        if stage.index in exempt_stages or stage.principle is None:
            continue
        out.extend(f"stage {stage.index} [stage.principle].source: {problem}"
                   for problem in source_problems(stage.principle.source, corpus))
    return out
