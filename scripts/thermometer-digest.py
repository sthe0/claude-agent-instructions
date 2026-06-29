#!/usr/bin/env python3
"""thermometer-digest — read-only instrument for the σ build-trigger (ADR-0002).

The σ (principle-revision) operator is *deferred* behind a pre-registered build-trigger
(docs/trigger-thermometer.md). This digest is the thermometer that makes that trigger
observable without building σ. It:

  - **measures condition (A)** — re-refutation of an *already-promoted* principle: a
    `tier: 1` experience leaf (a tier-1 difficulty, the σ-fuel) whose functional ground
    matches a principle already lifted into `memory-global/leaves/principles/`. ≥
    `principle-promotion-threshold` (Rule-of-Three) re-refutations of one promoted
    principle FLAGS — the manual path has sprung a leak;
  - **reports the cheap (C) proxy** — corpus size + near-duplicate density — as plain
    numbers (report-only, never a flag on its own);
  - **logs (B) and the dear-(C) discriminator as DEFERRED** (out of scope), naming the
    observable that activates each — no silent cap (ADR-0002 / trigger-thermometer.md).

It is strictly READ-ONLY: it measures and surfaces, it **does not decide** to build σ and
**builds nothing**. When (A) flags, the decision routes through the normal
planner → approval → developer spine like any other Core change.

Clustering / similarity / threshold reading are REUSED from record-experience.py — there
is one ranking engine, not a parallel one (the same discipline as core-difficulty-digest.py).
"""
from __future__ import annotations

import argparse
import importlib.util
import json as _json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
PRINCIPLES_DIR = REPO_ROOT / "memory-global/leaves/principles"

# Reuse record-experience.py's ranking + clustering primitives (hyphenated module → load
# by path). No new similarity measure, no new threshold — same engine as promote-scan.
# NB: a caller that importlib-loads *this* module must register it in sys.modules before
# exec_module — the @dataclass classes below resolve cls.__module__ through sys.modules.
_REC_SPEC = importlib.util.spec_from_file_location(
    "record_experience", SCRIPTS_DIR / "record-experience.py"
)
_rec = importlib.util.module_from_spec(_REC_SPEC)
_REC_SPEC.loader.exec_module(_rec)
FRONTMATTER = _rec.FRONTMATTER
section_span = _rec.section_span
_similarity = _rec._similarity
cluster_by_ground = _rec.cluster_by_ground
read_threshold = _rec.read_threshold
experience_dir = _rec.experience_dir
JOIN_RATIO = _rec.JOIN_RATIO
DEFAULT_PRINCIPLE_PROMOTION_THRESHOLD = _rec.DEFAULT_PRINCIPLE_PROMOTION_THRESHOLD


# --------------------------------------------------------------------------
# leaf reading
# --------------------------------------------------------------------------
@dataclass
class Leaf:
    name: str
    ground: str          # description + the ranking section body (the comparison ground)
    tier: int = 0        # difficulty tier (ADR-0002); absent frontmatter ⇒ 0


def _read_leaf(path: Path, section: str) -> Leaf:
    """Build a Leaf from a markdown file: ground = description + `## <section>` body,
    tier from the optional `tier:` frontmatter key (absent ⇒ 0)."""
    text = path.read_text(encoding="utf-8")
    fm = FRONTMATTER.match(text)
    desc, tier = "", 0
    if fm:
        dm = re.search(r"^description:\s*(.*)$", fm.group(1), re.MULTILINE)
        desc = dm.group(1).strip() if dm else ""
        tm = re.search(r"^tier:\s*(\d+)", fm.group(1), re.MULTILINE)
        tier = int(tm.group(1)) if tm else 0
    span = section_span(text, section)
    body = text[span[0]:span[1]] if span else ""
    return Leaf(name=path.name, ground=f"{desc} {body}", tier=tier)


def _load_dir(directory: Path, section: str) -> list[Leaf]:
    if not directory.is_dir():
        return []
    return [
        _read_leaf(p, section)
        for p in sorted(directory.glob("*.md"))
        if p.name != "MEMORY.md"
    ]


# --------------------------------------------------------------------------
# condition (A) — re-refutation of an already-promoted principle
# --------------------------------------------------------------------------
@dataclass
class PrincipleHits:
    principle: str                       # promoted principle leaf name
    refutations: list[str] = field(default_factory=list)  # tier-1 leaf names matching it

    @property
    def count(self) -> int:
        return len(self.refutations)


def measure_condition_a(
    experience: list[Leaf], principles: list[Leaf], threshold: int,
    join_ratio: float = JOIN_RATIO,
) -> list[PrincipleHits]:
    """For every tier-1 experience leaf, find the best-matching promoted principle (overlap
    ≥ join_ratio) and record it as a re-refutation. Returns per-principle hit tallies, heaviest
    first. A principle with ≥ threshold re-refutations is the (A) firing condition."""
    hits: dict[str, PrincipleHits] = {p.name: PrincipleHits(principle=p.name) for p in principles}
    for leaf in experience:
        if leaf.tier < 1:
            continue  # only tier-1 difficulties are σ-fuel
        best_name, best_sim = None, 0.0
        for p in principles:
            sim = _similarity(p.ground, leaf.ground)
            if sim > best_sim:
                best_name, best_sim = p.name, sim
        if best_name is not None and best_sim >= join_ratio:
            hits[best_name].refutations.append(leaf.name)
    tallied = [h for h in hits.values() if h.count > 0]
    tallied.sort(key=lambda h: -h.count)
    return tallied


# --------------------------------------------------------------------------
# cheap (C) proxy — corpus size + near-duplicate density (report-only)
# --------------------------------------------------------------------------
@dataclass
class CheapC:
    corpus_size: int
    near_duplicate_pairs: int
    largest_cluster: int


def measure_cheap_c(experience: list[Leaf], join_ratio: float = JOIN_RATIO) -> CheapC:
    """Cheap proliferation proxy: how many leaves, how many near-duplicate pairs, and the
    biggest same-ground cluster. Plain numbers — the discriminating clause ('growth WITHOUT
    reformulation') is the dear-(C) discriminator, which is DEFERRED (see digest footer)."""
    pairs = 0
    for i in range(len(experience)):
        for j in range(i + 1, len(experience)):
            if _similarity(experience[i].ground, experience[j].ground) >= join_ratio:
                pairs += 1
    groups = cluster_by_ground(experience, lambda lf: lf.ground, join_ratio)
    largest = max((len(g) for g in groups), default=0)
    return CheapC(corpus_size=len(experience), near_duplicate_pairs=pairs, largest_cluster=largest)


# The deferred signals, named with their activation observable (no silent cap — ADR-0002).
DEFERRED = [
    {
        "signal": "(B) rising missed-promotion rate",
        "why_deferred": "needs the tier tag's accumulated history + a temporal series the young "
                        "corpus does not yet have (instrument-before-baseline)",
        "activates_when": "the tier tag has ≥1 full scan-window of history OR condition (A) first flags",
    },
    {
        "signal": "(C) reframing-discriminator (growth WITHOUT reformulation)",
        "why_deferred": "needs semantic diff-history judgement; the cheap proxy above only counts",
        "activates_when": "the cheap (C) proxy first reports numbers a human judges as floor-pressure",
    },
]


# --------------------------------------------------------------------------
# digest assembly
# --------------------------------------------------------------------------
def build_digest(scope: str, project_dir: str | None, threshold: int | None) -> dict:
    thr = threshold if threshold is not None else read_threshold(
        "principle-promotion-threshold", DEFAULT_PRINCIPLE_PROMOTION_THRESHOLD
    )
    experience = _load_dir(experience_dir(scope, project_dir), "Difficulty")
    principles = _load_dir(PRINCIPLES_DIR, "Principle")
    a_hits = measure_condition_a(experience, principles, thr)
    cheap_c = measure_cheap_c(experience)
    flagged = [h for h in a_hits if h.count >= thr]
    return {
        "threshold": thr,
        "condition_a": {
            "tier1_leaves": sum(1 for lf in experience if lf.tier >= 1),
            "promoted_principles": len(principles),
            "hits": [{"principle": h.principle, "refutations": h.refutations, "count": h.count}
                     for h in a_hits],
            "flagged": [h.principle for h in flagged],
            "fired": bool(flagged),
        },
        "cheap_c": {
            "corpus_size": cheap_c.corpus_size,
            "near_duplicate_pairs": cheap_c.near_duplicate_pairs,
            "largest_cluster": cheap_c.largest_cluster,
            "note": "report-only; never flags on its own",
        },
        "deferred": DEFERRED,
        "decides": False,
        "builds": False,
    }


def _format(d: dict) -> str:
    a = d["condition_a"]
    c = d["cheap_c"]
    lines = ["thermometer-digest (σ build-trigger, ADR-0002) — read-only; measures, never decides."]
    lines.append(f"  threshold (principle-promotion-threshold): {d['threshold']}")
    lines.append(
        f"  (A) re-refutation: {a['tier1_leaves']} tier-1 leaf/leaves vs "
        f"{a['promoted_principles']} promoted principle(s)"
    )
    if a["hits"]:
        for h in a["hits"]:
            mark = " ← FIRED" if h["count"] >= d["threshold"] else ""
            lines.append(f"      {h['count']}× {h['principle']}{mark}")
    else:
        lines.append("      no tier-1 leaf matches a promoted principle — (A) silent")
    if a["fired"]:
        lines.append(f"  ⚠ (A) FIRED for {a['flagged']} — route a build of the tag+registry seam "
                     f"through planner → approval → developer (this digest only flags).")
    lines.append(
        f"  (C) cheap proxy [report-only]: corpus={c['corpus_size']}, "
        f"near-dup pairs={c['near_duplicate_pairs']}, largest cluster={c['largest_cluster']}"
    )
    lines.append("  deferred (out of scope; scheduled, not dropped):")
    for item in d["deferred"]:
        lines.append(f"      • {item['signal']} — activates when: {item['activates_when']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--scope", choices=["global", "project"], default="global")
    p.add_argument("--project-dir")
    p.add_argument("--threshold", type=int, default=None,
                   help="override principle-promotion-threshold from config.md")
    p.add_argument("--json", action="store_true", dest="json_out", default=False,
                   help="emit the digest as JSON instead of human-readable text")
    a = p.parse_args(argv)
    d = build_digest(a.scope, a.project_dir, a.threshold)
    if a.json_out:
        print(_json.dumps(d, indent=2))
    else:
        print(_format(d))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
