#!/usr/bin/env python3
"""Standing, resumable self-improvement scan — the mechanized half of a
discipline that today lives only in prose.

Difficulty removed: two recurring pieces of self-improvement work are already
DOCUMENTED but stay MANUAL every time. `memory-global/leaves/backlog-triage-
practice.md` names its own gap #2 — no cross-source priority digest script
exists, only a scratchpad `score-backlog.py` that was never committed — for
reconciling the Core + Org backlog against the published Triage Board. Core
issue #144 is the filed form of the other half: nothing periodic reads the
session transcripts, so "where does the quota go" is answered once and then
decays. This module supplies the shared core two resumable producers build on:
a `Finding` model, registration as a second external producer in the existing
`self_diagnose_store.py`, and one resume seam per producer.

This script only REPORTS and RECOMMENDS. It never files a difficulty, never
dispatches a specialist, and never edits repo content — asserted by
`scripts/tests/test_improvement_scan.py` via `ast_purity.impure_names`, the
same purity predicate `agentctl/gates.py`'s guardians are held to.

Checkpoint scheme, per producer — deliberately NOT unified into one primitive
(see this stage's `principle` in the plan for the full argument):

  telemetry (`backlog` producer's sibling)  -> mtime-gated upsert, following
      `policy-scorecard.upsert`'s scheme (`scripts/policy-scorecard.py`). The
      resume unit is a SESSION, and a session's own transcript file already
      carries an mtime we control and can compare cheaply. `LedgerCursor`
      re-processes a session only when its stored mtime has grown.

  backlog                                   -> frozen-baseline-JSON delta,
      following `spawn-outcome-report.py`'s `--freeze-baseline` scheme. The
      resume unit is a backlog ITEM we do not own (a Core GitHub issue, an Org
      ticket) that mutates on its own schedule with no local timestamp we
      control. The durable prior state must therefore be a full snapshot to
      diff against — `PriorBoard` — not a per-item watermark.

Rejected: a write-once eligibility stamp (a bool cannot carry the per-item
state either producer needs) and calendar-bucket dedup (an item or session
straddling a bucket boundary would be silently dropped, the exact failure mode
SARIF's `partialFingerprints` design note warns against for content identity).

Finding IDENTITY is content-derived in both seams, never position- or
session-derived: `PriorBoard.item_digest` hashes an item's own normalized
text+status, and the store's own `finding_key` (self_diagnose_store.py) hashes
(kind, path) — never a scan's ordinal position in either list.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import self_diagnose_store as sds  # noqa: E402

BOARD_SCHEMA = 1

# The closed vocabulary a Finding's `recommended_next_step` must belong to.
# Closed rather than free text so a recommendation can never silently become an
# instruction to do something else — validated at construction, not at render
# time, so an invalid value fails at the producer that emitted it.
RECOMMENDED_NEXT_STEPS = frozenset({"self-improvement", "planner", "file-difficulty"})


# --- the shared finding model ------------------------------------------------

@dataclass(frozen=True)
class CostSignal:
    """One finding's measured (or explicitly unmeasured) impact.

    `measured=False` is not a sentinel value hidden in the numeric fields — it
    is its own field, so a finding with no cost data band is distinguishable
    from a finding that measured a cost of exactly zero. `basis` names WHERE the
    number came from (a ledger, a count, a manual estimate) so a report reader
    can judge the number rather than merely see it.
    """

    usd_per_week: "float | None" = None
    attention_per_week: "float | None" = None
    stability_per_week: "float | None" = None
    basis: str = ""
    measured: bool = False

    def __post_init__(self) -> None:
        if self.measured and self.basis == "":
            raise ValueError("a measured CostSignal must name its basis")


@dataclass(frozen=True)
class Finding:
    """One standing improvement-scan finding, from either producer.

    `signal` is the STABLE slug that becomes the store key's path component —
    content-derived, never a scan's ordinal position (see the module
    docstring's SARIF citation) — so the same underlying condition produces the
    same store row on every run, exactly like `self_diagnose_store.finding_key`
    already requires of every other producer.
    """

    kind: str
    signal: str
    title: str
    functional_ground: str
    evidence: "tuple[str, ...]"
    cost_signal: CostSignal
    source_ref: str
    recommended_next_step: str

    def __post_init__(self) -> None:
        if not self.kind:
            raise ValueError("Finding.kind must not be empty")
        if not self.signal:
            raise ValueError("Finding.signal must not be empty")
        if self.recommended_next_step not in RECOMMENDED_NEXT_STEPS:
            raise ValueError(
                f"recommended_next_step {self.recommended_next_step!r} is not one of "
                f"{sorted(RECOMMENDED_NEXT_STEPS)}"
            )
        if not isinstance(self.evidence, tuple):
            object.__setattr__(self, "evidence", tuple(self.evidence))


def _finding_record(finding: Finding) -> dict:
    """Adapt a Finding to the store's generic (kind, path, detail) shape.

    The store's schema predates this producer and stays generic across all
    three producers; the richer fields (`cost_signal`, `evidence`) live only in
    the Finding object the `report` renderer (stage 5) consumes directly, not
    in the durable closure-state row.
    """
    return {"kind": finding.kind, "path": finding.signal, "detail": finding.title}


def store_findings(
    findings: Iterable[Finding], *, store_path: "str | Path | None" = None
) -> "list[dict]":
    """Upsert this scan's findings under the improvement-scan source.

    Delegates entirely to `self_diagnose_store.upsert_findings`, which
    source-partitions its resolve-out — this call can only retire rows it
    could itself have produced, so it can never resolve away self-diagnose's or
    policy-scorecard's rows.
    """
    records = [_finding_record(f) for f in findings]
    return sds.upsert_findings(records, path=store_path, source=sds.SOURCE_IMPROVEMENT_SCAN)


# --- backlog resume seam: frozen-baseline-JSON delta -------------------------

def item_digest(text: str, status: str) -> str:
    """Content identity of one backlog item's classifiable state.

    Hashes the item's own normalized text and status — NOT its position in any
    list and NOT a timestamp — so "unchanged" is decided by content, matching
    the module docstring's rejection of calendar-bucket and position-based
    identity.
    """
    normalized = f"{status}\0{' '.join(text.split())}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class PriorBoardItem:
    classification: "str | None"
    score: "float | None"
    rank: "int | None"
    source_digest: str


@dataclass(frozen=True)
class PriorBoard:
    schema: int
    generated_at: str
    items: "dict[str, PriorBoardItem]" = field(default_factory=dict)

    def is_unchanged(self, item_ref: str, text: str, status: str) -> bool:
        prior = self.items.get(item_ref)
        return prior is not None and prior.source_digest == item_digest(text, status)


def _empty_board(now: "datetime | None" = None) -> PriorBoard:
    now = now or datetime.now(timezone.utc)
    return PriorBoard(schema=BOARD_SCHEMA, generated_at=now.isoformat(), items={})


def load_prior_board(path: "str | Path") -> PriorBoard:
    """Fail-open: a missing, empty or corrupt board yields an empty board — a
    board that fails to load must never be read as "everything unchanged",
    since that would silently suppress every finding it should have surfaced.
    """
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return _empty_board()
    if not isinstance(raw, dict) or raw.get("schema") != BOARD_SCHEMA:
        return _empty_board()
    items = {}
    for ref, entry in (raw.get("items") or {}).items():
        if not isinstance(entry, dict) or "source_digest" not in entry:
            continue
        items[str(ref)] = PriorBoardItem(
            classification=entry.get("classification"),
            score=entry.get("score"),
            rank=entry.get("rank"),
            source_digest=str(entry["source_digest"]),
        )
    return PriorBoard(
        schema=BOARD_SCHEMA, generated_at=str(raw.get("generated_at", "")), items=items
    )


def write_board(board: PriorBoard, path: "str | Path") -> None:
    """Replace the board atomically, mirroring self_diagnose_store.save_rows so
    a crash mid-write leaves the previous board intact rather than truncated.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": board.schema,
        "generated_at": board.generated_at,
        "items": {
            ref: {
                "classification": item.classification,
                "score": item.score,
                "rank": item.rank,
                "source_digest": item.source_digest,
            }
            for ref, item in board.items.items()
        },
    }
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


# --- telemetry resume seam: mtime-gated upsert -------------------------------

@dataclass
class LedgerCursor:
    """Per-session mtime watermark, following `policy-scorecard.upsert`'s
    scheme exactly: a session is due for re-processing only when its stored
    mtime has grown. Persisted ALONGSIDE the store (its own file), never
    embedded inside it — the store's rows are closure state for findings, the
    cursor is progress state for a scan, and conflating them would make a hand
    edit to one accidentally corrupt the other's invariants.
    """

    sessions: "dict[str, float]" = field(default_factory=dict)

    @classmethod
    def load(cls, path: "str | Path") -> "LedgerCursor":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        sessions = {}
        for sid, mtime in raw.items():
            try:
                sessions[str(sid)] = float(mtime)
            except (TypeError, ValueError):
                continue
        return cls(sessions=sessions)

    def save(self, path: "str | Path") -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self.sessions, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)

    def is_due(self, session_id: str, mtime: float) -> bool:
        """True unless this exact mtime was already processed for this session."""
        return self.sessions.get(session_id) != mtime

    def mark(self, session_id: str, mtime: float) -> None:
        self.sessions[session_id] = mtime


# --- CLI ----------------------------------------------------------------

def _cmd_backlog(args: argparse.Namespace) -> int:
    print("improvement-scan backlog: not implemented in this stage", file=sys.stderr)
    return 2


def _cmd_telemetry(args: argparse.Namespace) -> int:
    print("improvement-scan telemetry: not implemented in this stage", file=sys.stderr)
    return 2


def _cmd_report(args: argparse.Namespace) -> int:
    print("improvement-scan report: not implemented in this stage", file=sys.stderr)
    return 2


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="never write, only print")
    sub = parser.add_subparsers(dest="command", required=True)

    p_backlog = sub.add_parser("backlog", help="reconcile Core + Org backlog against the Triage Board")
    p_backlog.set_defaults(func=_cmd_backlog)

    p_telemetry = sub.add_parser("telemetry", help="scan recent session telemetry for recurring difficulties")
    p_telemetry.set_defaults(func=_cmd_telemetry)

    p_report = sub.add_parser("report", help="render the unified ranked report from stored findings")
    p_report.set_defaults(func=_cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
