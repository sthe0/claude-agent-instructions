#!/usr/bin/env python3
"""core-difficulty-digest — pull, normalize, cluster-by-functional-ground, mass, flag.

ADR-0001 § Difficulty-accumulation mechanism, stage 2 (Aggregation). Pulls difficulty records
from every configured channel, normalizes them to the common schema (the adapters already
return ``DifficultyRecord``), and clusters them by *functional ground* — the channel-agnostic
join key. Clustering REUSES record-experience.py's search-before-record ranking (that search IS
the clustering — there is no separate clustering engine). Each cluster's mass = Σ severity
weight; a cluster is FLAGGED when its mass ≥ `core-difficulty-mass-threshold` (read by key from
config.md) or it contains any critical item.

The digest only FLAGS and surfaces — it never edits Core. An author routes a flagged cluster
through planner → approval → developer.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from difficulty_channel import DifficultyRecord, Severity, get_channel  # noqa: E402
import difficulty_channel.adapters  # noqa: E402,F401  (registers startrek/external)

REPO_ROOT = SCRIPTS_DIR.parent
CONFIG_PATH = REPO_ROOT / "config.md"
MASS_THRESHOLD_KEY = "core-difficulty-mass-threshold"
DEFAULT_MASS_THRESHOLD = 8  # safe fallback until stage 13 calibrates the config value

# Reuse the record-experience ranking + clustering primitive (hyphenated module -> load by path).
_REC_SPEC = importlib.util.spec_from_file_location(
    "record_experience", SCRIPTS_DIR / "record-experience.py"
)
_rec = importlib.util.module_from_spec(_REC_SPEC)
_REC_SPEC.loader.exec_module(_rec)
tokenize = _rec.tokenize
term_score = _rec.term_score
JOIN_RATIO = _rec.JOIN_RATIO
_similarity = _rec._similarity
cluster_by_ground = _rec.cluster_by_ground


@dataclass
class Cluster:
    functional_ground: str           # representative ground (first record's)
    items: list[DifficultyRecord] = field(default_factory=list)

    @property
    def mass(self) -> int:
        return sum(r.severity.mass for r in self.items)

    @property
    def has_critical(self) -> bool:
        return any(r.severity is Severity.CRITICAL for r in self.items)

    @property
    def reporters(self) -> set[str]:
        # who/what filed the reports in this cluster (a record carries a reporter, not a channel
        # name — a live adapter sets reporter from the submitting identity).
        return {r.reporter for r in self.items}


def cluster_records(records: list[DifficultyRecord], join_ratio: float = JOIN_RATIO) -> list[Cluster]:
    """Group records by functional ground. A record joins the best-matching existing cluster
    when overlap ≥ join_ratio, else opens a new one. Same ground from two channels → one cluster."""
    groups = cluster_by_ground(records, lambda r: r.functional_ground, join_ratio)
    return [Cluster(functional_ground=g[0].functional_ground, items=g) for g in groups]


def is_flagged(cluster: Cluster, threshold: int) -> bool:
    return cluster.has_critical or cluster.mass >= threshold


def read_mass_threshold(config_path: Path = CONFIG_PATH, override: int | None = None) -> int:
    """Threshold by key from config.md; override wins; non-integer (placeholder) → default."""
    if override is not None:
        return override
    try:
        for line in config_path.read_text(encoding="utf-8").splitlines():
            if "`" + MASS_THRESHOLD_KEY + "`" in line and line.lstrip().startswith("|"):
                cells = [c.strip().strip("`") for c in line.split("|")]
                for cell in cells:
                    if cell.isdigit():
                        return int(cell)
    except FileNotFoundError:
        pass
    return DEFAULT_MASS_THRESHOLD


def pull_all(channel_names: list[str], since: str | None = None) -> list[DifficultyRecord]:
    records: list[DifficultyRecord] = []
    for name in channel_names:
        try:
            records.extend(get_channel(name).pull(since=since))
        except NotImplementedError:
            # a stub adapter (e.g. external) is configured but not yet pullable — skip it
            print(f"core-difficulty-digest: channel {name!r} is a stub (skipped)", file=sys.stderr)
    return records


def digest(records: list[DifficultyRecord], threshold: int) -> list[Cluster]:
    """Cluster + return only the flagged clusters, heaviest first."""
    clusters = cluster_records(records)
    flagged = [c for c in clusters if is_flagged(c, threshold)]
    flagged.sort(key=lambda c: (-int(c.has_critical), -c.mass))
    return flagged


def _format(flagged: list[Cluster]) -> str:
    if not flagged:
        return "core-difficulty-digest: no flagged clusters."
    lines = [f"core-difficulty-digest: {len(flagged)} flagged cluster(s):"]
    for c in flagged:
        crit = " CRITICAL" if c.has_critical else ""
        lines.append(
            f"  [mass {c.mass}{crit}] {c.functional_ground!r} "
            f"— {len(c.items)} report(s) by {sorted(c.reporters)}"
        )
    lines.append("  → route each through planner → approval → developer (the digest only flags).")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--channel", action="append", default=[], dest="channels",
                   help="channel name to pull (repeatable); default: all but the null double")
    p.add_argument("--since", default=None)
    p.add_argument("--threshold", type=int, default=None, help="override mass threshold")
    a = p.parse_args(argv)
    channels = a.channels or ["startrek", "github"]  # both implemented channels by default
    threshold = read_mass_threshold(override=a.threshold)
    records = pull_all(channels, since=a.since)
    print(_format(digest(records, threshold)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
