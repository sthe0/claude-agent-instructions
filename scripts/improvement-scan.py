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
import importlib.util
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
from difficulty_channel import DifficultyRecord, Severity, get_channel, is_registered  # noqa: E402
from difficulty_channel.adapters import load_adapter  # noqa: E402
from difficulty_channel.port import StreamUnsupported  # noqa: E402

REPO_ROOT = SCRIPT_DIR.parent
CONFIG_PATH = REPO_ROOT / "config.md"

# Reuse the existing channel-set and clustering primitives rather than re-deriving them
# (both hyphenated filenames -> load by path, the same idiom core-difficulty-digest.py itself
# uses for record-experience.py). `core-difficulty-digest.py` is never modified by this stage.
_DIGEST_SPEC = importlib.util.spec_from_file_location(
    "core_difficulty_digest_for_improvement_scan", SCRIPT_DIR / "core-difficulty-digest.py"
)
_digest = importlib.util.module_from_spec(_DIGEST_SPEC)
sys.modules[_DIGEST_SPEC.name] = _digest  # dataclass() needs cls.__module__ resolvable in sys.modules
_DIGEST_SPEC.loader.exec_module(_digest)
default_channels = _digest.default_channels

_REC_SPEC = importlib.util.spec_from_file_location(
    "record_experience_for_improvement_scan", SCRIPT_DIR / "record-experience.py"
)
_rec_for_scan = importlib.util.module_from_spec(_REC_SPEC)
sys.modules[_REC_SPEC.name] = _rec_for_scan
_REC_SPEC.loader.exec_module(_rec_for_scan)
cluster_by_ground = _rec_for_scan.cluster_by_ground

# The telemetry producer's only two subprocess reaches (policy-scorecard.py's ledger
# upsert, record-experience.py's dedup search) live in this sibling module, never here —
# see improvement_scan_shell.py's docstring for why, and
# test_module_never_shells_out_or_reaches_the_network for the invariant this preserves.
import improvement_scan_shell as shell  # noqa: E402
from agentctl.cost import COST_LOG as SPAWN_LEDGER_DEFAULT, read_rows as read_spawn_rows  # noqa: E402

BOARD_SCHEMA = 1

# The closed vocabulary a Finding's `recommended_next_step` must belong to.
# Closed rather than free text so a recommendation can never silently become an
# instruction to do something else — validated at construction, not at render
# time, so an invalid value fails at the producer that emitted it.
RECOMMENDED_NEXT_STEPS = frozenset({"self-improvement", "planner", "file-difficulty"})

# --- backlog triage rubric: closed vocabularies + config-keyed weights ------
# Transcribed from memory-global/leaves/backlog-triage-practice.md, not reinvented.
# score = breadth_weight * recurrence_mass / cost_to_resolve_usd
# cost_to_resolve_usd = budget_tier_usd / in_flight_coefficient

BREADTH_WEIGHTS = {"narrow": 1, "shared-mechanism": 3, "universal": 8}
IN_FLIGHT_COEFFICIENTS = {"none": 1.0, "clear-direction": 0.5, "plan-approved": 0.3}

# cost_to_resolve tier -> the config.md key carrying its dollar figure. The values
# ($1.00/$3.00/$8.00) are read BY KEY, never hardcoded, per CLAUDE.md's rule-vs-
# perception split; only the fallback (used when config.md is unreadable) is a literal.
_BUDGET_TIER_KEYS = {
    "small": "budget-small-usd",
    "medium": "budget-medium-usd",
    "large": "budget-large-usd",
}
_BUDGET_TIER_FALLBACKS = {"small": 1.00, "medium": 3.00, "large": 8.00}


def _read_config_float(key: str, config_path: "str | Path" = CONFIG_PATH) -> "float | None":
    """The first float-parseable cell on config.md's ``| `key` |`` row, or None.

    `core_difficulty_digest.read_mass_threshold` already reads config.md by key but
    matches cells with `cell.isdigit()`, which rejects the float budget values
    (`1.00`/`3.00`/`8.00`) this rubric needs — hence this separate, float-permissive
    reader rather than reusing that one.
    """
    try:
        lines = Path(config_path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    marker = f"`{key}`"
    for line in lines:
        if marker not in line or not line.lstrip().startswith("|"):
            continue
        for cell in line.split("|"):
            cell = cell.strip().strip("`")
            try:
                return float(cell)
            except ValueError:
                continue
    return None


def read_budget_usd(tier: str, config_path: "str | Path" = CONFIG_PATH) -> float:
    value = _read_config_float(_BUDGET_TIER_KEYS[tier], config_path)
    return value if value is not None else _BUDGET_TIER_FALLBACKS[tier]


def _validate_vocab(field_name: str, value, vocab) -> None:
    if value not in vocab:
        raise ValueError(f"{field_name} {value!r} is not one of {sorted(vocab)}")


def score_item(
    breadth: str,
    recurrence_mass: float,
    cost_to_resolve: str,
    in_flight: str,
    *,
    config_path: "str | Path" = CONFIG_PATH,
) -> float:
    """The deterministic half of the triage rubric — every input is a closed-vocabulary
    enum value the model supplied, never free text classified by regex."""
    _validate_vocab("breadth", breadth, BREADTH_WEIGHTS)
    _validate_vocab("cost_to_resolve", cost_to_resolve, _BUDGET_TIER_KEYS)
    _validate_vocab("in_flight", in_flight, IN_FLIGHT_COEFFICIENTS)
    cost_to_resolve_usd = read_budget_usd(cost_to_resolve, config_path) / IN_FLIGHT_COEFFICIENTS[in_flight]
    return BREADTH_WEIGHTS[breadth] * recurrence_mass / cost_to_resolve_usd


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
    # Carried alongside classification/score so a re-emitted Finding for an UNCHANGED
    # item never needs a fresh classifier call or a live re-pull to reconstruct itself.
    title: str = ""
    functional_ground: str = ""
    evidence: "tuple[str, ...]" = ()
    recommended_next_step: str = "planner"
    blocked_by: "tuple[str, ...]" = ()


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
            title=str(entry.get("title", "")),
            functional_ground=str(entry.get("functional_ground", "")),
            evidence=tuple(entry.get("evidence") or ()),
            recommended_next_step=str(entry.get("recommended_next_step", "planner")),
            blocked_by=tuple(entry.get("blocked_by") or ()),
        )
    return PriorBoard(
        schema=BOARD_SCHEMA, generated_at=str(raw.get("generated_at", "")), items=items
    )


def write_board(board: PriorBoard, path: "str | Path") -> None:
    """Replace the board atomically, mirroring self_diagnose_store.save_rows so
    a crash mid-write leaves the previous board intact rather than truncated.
    """
    payload = {
        "schema": board.schema,
        "generated_at": board.generated_at,
        "items": {
            ref: {
                "classification": item.classification,
                "score": item.score,
                "rank": item.rank,
                "source_digest": item.source_digest,
                "title": item.title,
                "functional_ground": item.functional_ground,
                "evidence": list(item.evidence),
                "recommended_next_step": item.recommended_next_step,
                "blocked_by": list(item.blocked_by),
            }
            for ref, item in board.items.items()
        },
    }
    _atomic_write_json(payload, path)


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


# --- telemetry producer: deterministic detector table ------------------------
# Each detector reads only closed-vocabulary/numeric fields off a policy-ledger row
# (policy-scorecard.py's _scan_session shape) and, where relevant, the spawn-cost
# rows joined to it by session_id — never free text, per CLAUDE.md's rule/perception
# split. A firing detector reports WHAT crossed its threshold; it never authors a
# functional_ground — that is the model's job in the grounds-intake pass below.

DEFAULT_POLICY_LEDGER = Path.home() / ".local" / "log" / "claude-policy-ledger.jsonl"
DEFAULT_TELEMETRY_CURSOR = Path.home() / ".local" / "state" / "claude-improvement-scan-telemetry-cursor.json"
DEFAULT_TELEMETRY_DAYS = 7
EVIDENCE_SCHEMA = 1

REPLAN_PRESSURE_CONFIG_KEY = "effort-replan-absolute"
_REPLAN_PRESSURE_FALLBACK = 3.0

# A session costing >= 3x the large-tier budget is a standing cost outlier worth
# naming to a human, not a one-off spend spike absorbed by variance.
COST_CONCENTRATION_MULTIPLE = 3.0
# Any missed-delegation cluster policy-scorecard already found is worth surfacing —
# the metric itself is already a threshold-scored signal, not a raw count needing headroom.
DELEGATION_MISS_MIN_CLUSTERS = 1
# Any recorded malformed/non-zero-exit spawn is worth surfacing — spawn failures are
# rare by construction (spawn-specialist.py retries transient errors internally).
SPAWN_FAILURE_MIN_COUNT = 1
# CLAUDE.md's own stated overcome-difficulty trigger: "two or more process corrections
# in a row" — this detector operationalizes that literal threshold.
ATTENTION_BURN_MIN_CORRECTIONS = 2


def _detect_cost_concentration(row: dict, spawn_rows: "list[dict]", *, config_path) -> "dict | None":
    threshold = read_budget_usd("large", config_path) * COST_CONCENTRATION_MULTIPLE
    cost = row.get("cost_usd") or 0.0
    if cost < threshold:
        return None
    return {
        "detector": "cost-concentration",
        "measured": {"cost_usd": cost, "threshold_usd": threshold},
        "description": (
            f"session cost ${cost:.2f} >= {COST_CONCENTRATION_MULTIPLE:g}x "
            f"the large-tier budget (${threshold:.2f})"
        ),
    }


def _detect_replan_pressure(row: dict, spawn_rows: "list[dict]", *, config_path) -> "dict | None":
    threshold = _read_config_float(REPLAN_PRESSURE_CONFIG_KEY, config_path)
    if threshold is None:
        threshold = _REPLAN_PRESSURE_FALLBACK
    replans = (row.get("effectiveness") or {}).get("replans") or 0
    if replans < threshold:
        return None
    return {
        "detector": "replan-pressure",
        "measured": {"replans": replans, "threshold": threshold},
        "description": f"{replans} replan(s) >= the {REPLAN_PRESSURE_CONFIG_KEY} threshold ({threshold:g})",
    }


def _detect_delegation_misses(row: dict, spawn_rows: "list[dict]", *, config_path) -> "dict | None":
    clusters = row.get("missed_delegation_clusters") or 0
    if clusters < DELEGATION_MISS_MIN_CLUSTERS:
        return None
    return {
        "detector": "delegation-misses",
        "measured": {"missed_delegation_clusters": clusters},
        "description": f"{clusters} missed-delegation cluster(s) >= threshold ({DELEGATION_MISS_MIN_CLUSTERS})",
    }


def _detect_spawn_process_failures(row: dict, spawn_rows: "list[dict]", *, config_path) -> "dict | None":
    failures = [r for r in spawn_rows if r.get("malformed") or (r.get("exit_code") or 0) != 0]
    if len(failures) < SPAWN_FAILURE_MIN_COUNT:
        return None
    return {
        "detector": "spawn-process-failure",
        "measured": {"failed_spawns": len(failures), "total_spawns": len(spawn_rows)},
        "description": f"{len(failures)} spawned-process failure(s) >= threshold ({SPAWN_FAILURE_MIN_COUNT})",
    }


def _detect_attention_burn(row: dict, spawn_rows: "list[dict]", *, config_path) -> "dict | None":
    corrections = (row.get("attention") or {}).get("corrections") or 0
    if corrections < ATTENTION_BURN_MIN_CORRECTIONS:
        return None
    return {
        "detector": "attention-burn",
        "measured": {"corrections": corrections},
        "description": (
            f"{corrections} user correction(s) >= threshold ({ATTENTION_BURN_MIN_CORRECTIONS}) "
            "— CLAUDE.md's own overcome-difficulty trigger"
        ),
    }


TELEMETRY_DETECTORS = (
    _detect_cost_concentration,
    _detect_replan_pressure,
    _detect_delegation_misses,
    _detect_spawn_process_failures,
    _detect_attention_burn,
)


def run_detectors(
    row: dict, spawn_rows: "list[dict]", *, config_path: "str | Path" = CONFIG_PATH
) -> "list[dict]":
    items = []
    for detector in TELEMETRY_DETECTORS:
        result = detector(row, spawn_rows, config_path=config_path)
        if result is None:
            continue
        result = dict(result)
        result["session_id"] = row.get("session_id")
        result["project"] = row.get("project")
        result["date"] = row.get("date")
        items.append(result)
    return items


def _load_ledger_rows(path: "str | Path") -> "list[dict]":
    """Tolerant JSONL read mirroring policy-scorecard.load_ledger's fail-open,
    later-line-wins parsing. This module only ever READS this file — writing it
    stays the sole job of policy-scorecard.py's own upsert (see module docstring)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    rows: "dict[str, dict]" = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("session_id"):
            rows[row["session_id"]] = row
    return list(rows.values())


def _group_spawn_rows_by_session(spawn_rows: "Iterable[dict]") -> "dict[str, list[dict]]":
    by_session: "dict[str, list[dict]]" = {}
    for r in spawn_rows:
        sid = r.get("session_id")
        if sid:
            by_session.setdefault(sid, []).append(r)
    return by_session


def scan_telemetry(
    ledger_rows: "list[dict]",
    spawn_rows: "list[dict]",
    cursor: LedgerCursor,
    *,
    config_path: "str | Path" = CONFIG_PATH,
) -> "tuple[list[dict], list[tuple[str, float]]]":
    """Run every detector against every ledger row the cursor still owes a pass to.

    Returns (evidence_items, due_marks). `due_marks` — the (session_id, mtime)
    pairs the caller must feed to `cursor.mark` — is returned rather than
    applied here, so the caller can defer marking until AFTER a successful
    evidence write: a crash between scan and persist must reprocess the
    session next run, never silently skip it.
    """
    by_session = _group_spawn_rows_by_session(spawn_rows)
    items: "list[dict]" = []
    due_marks: "list[tuple[str, float]]" = []
    for row in ledger_rows:
        session_id = row.get("session_id")
        mtime = row.get("mtime")
        if session_id is None or mtime is None:
            continue
        if not cursor.is_due(session_id, mtime):
            continue
        due_marks.append((session_id, mtime))
        items.extend(run_detectors(row, by_session.get(session_id, []), config_path=config_path))
    return items, due_marks


def build_evidence_bundle(
    items: "list[dict]",
    *,
    days: int,
    sessions_scanned: int,
    degraded_refresh: bool,
    degraded_reason: "str | None",
    now: "datetime | None" = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    return {
        "schema": EVIDENCE_SCHEMA,
        "generated_at": now.isoformat(),
        "days": days,
        "sessions_scanned": sessions_scanned,
        "degraded_refresh": degraded_refresh,
        "degraded_reason": degraded_reason,
        "items": items,
    }


# --- telemetry producer: grounds intake, dedup, store ------------------------

def _ground_signal(detector: str, functional_ground: str) -> str:
    """The store-key identity: detector + ground TEXT, never a session id — so the
    same standing pattern collapses to one row across runs and sessions instead of
    minting a fresh key (and a `times_surfaced` stuck at 1) every time it fires."""
    normalized = f"{detector}\0{' '.join(functional_ground.split())}"
    return "telemetry-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _board_ground_match(board: "PriorBoard | None", functional_ground: str) -> "str | None":
    """A cheap, subprocess-free pre-check: has the backlog board already got an item
    carrying this exact ground text? A match here short-circuits the (costlier)
    record-experience subprocess search below — the two dedup against different
    stores, but either one finding the ground already tracked is sufficient."""
    if board is None:
        return None
    normalized = " ".join(functional_ground.split())
    for ref, item in board.items.items():
        if " ".join(item.functional_ground.split()) == normalized:
            return ref
    return None


def build_findings_from_grounds(
    grounds: "list[dict]",
    *,
    board: "PriorBoard | None" = None,
    scope: str = "global",
) -> "tuple[list[Finding], list[dict]]":
    """For every model-supplied ground: dedup against the backlog board (if given)
    and existing experience leaves, recording every outcome — a dedup match is
    NEVER silently dropped, only excluded from the returned findings — then build
    survivors into Findings keyed by detector+ground, never by session id.
    """
    findings: "list[Finding]" = []
    dedup_log: "list[dict]" = []
    for g in grounds:
        detector = g["detector"]
        ground_text = g["functional_ground"]

        board_ref = _board_ground_match(board, ground_text)
        if board_ref is not None:
            dedup_log.append({
                "detector": detector, "functional_ground": ground_text,
                "outcome": "board-match", "detail": board_ref,
            })
            continue

        ok, found, output = shell.search_experience(ground_text.split(), scope=scope)
        if not ok:
            dedup_log.append({
                "detector": detector, "functional_ground": ground_text,
                "outcome": "search-failed", "detail": output,
            })
        elif found:
            dedup_log.append({
                "detector": detector, "functional_ground": ground_text,
                "outcome": "dedup-match", "detail": output,
            })
            continue
        else:
            dedup_log.append({
                "detector": detector, "functional_ground": ground_text,
                "outcome": "no-match", "detail": output,
            })

        cost = g.get("cost_signal") or {}
        findings.append(
            Finding(
                kind=sds.KIND_TELEMETRY_PATTERN,
                signal=_ground_signal(detector, ground_text),
                title=g.get("title", detector),
                functional_ground=ground_text,
                evidence=tuple(g.get("evidence_refs") or ()),
                cost_signal=CostSignal(
                    usd_per_week=cost.get("usd_per_week"),
                    attention_per_week=cost.get("attention_per_week"),
                    stability_per_week=cost.get("stability_per_week"),
                    basis=cost.get("basis", ""),
                    measured=cost.get("measured", False),
                ),
                source_ref=detector,
                recommended_next_step=g.get("recommended_next_step", "self-improvement"),
            )
        )
    return findings, dedup_log


# --- backlog producer: cross-source collection ------------------------------

_STREAMS = ("report", "backlog")


def collect_records(channel_names: "Iterable[str]") -> "tuple[list[DifficultyRecord], list[dict]]":
    """Pull every stream of every named channel; never abort on one channel's failure.

    Returns (records, coverage_gaps). A gap names {channel, stream, reason}, with
    `reason` either "unsupported" (StreamUnsupported) or "collection-failed: <exc>"
    (any other exception, including channel resolution itself) — never silenced,
    never aborting collection of the remaining (channel, stream) pairs.
    """
    records: "list[DifficultyRecord]" = []
    coverage_gaps: "list[dict]" = []
    for name in channel_names:
        try:
            if not is_registered(name):
                load_adapter(name)
            channel = get_channel(name)
        except Exception as exc:  # noqa: BLE001 - a channel must never abort the whole scan
            for stream in _STREAMS:
                coverage_gaps.append(
                    {"channel": name, "stream": stream, "reason": f"collection-failed: {exc}"}
                )
            continue
        for stream in _STREAMS:
            try:
                records.extend(channel.pull_stream(stream=stream))
            except StreamUnsupported:
                coverage_gaps.append({"channel": name, "stream": stream, "reason": "unsupported"})
            except Exception as exc:  # noqa: BLE001 - same non-aborting contract as above
                coverage_gaps.append(
                    {"channel": name, "stream": stream, "reason": f"collection-failed: {exc}"}
                )
    return records, coverage_gaps


def _item_ref(record: DifficultyRecord) -> str:
    """The Phase-A diff key: the record's own stable ref, or (documented fallback,
    exercised by no named test) a content hash when a channel supplies none."""
    if record.ref:
        return record.ref
    return "noref:" + item_digest(record.functional_ground + "\0" + record.reporter, record.ts)


def _backlog_text(record: DifficultyRecord) -> str:
    """The subset of a record's fields that make it "the same item" for the four-bucket
    diff — deliberately excluding `ts`: a date-only edit (e.g. GitHub's updated_at ticking
    on an unrelated event) must not itself flip an item from unchanged to changed."""
    return "\n".join([record.target, record.functional_ground, record.evidence, record.cost_estimate])


def diff_backlog(
    records: "list[DifficultyRecord]", prior: PriorBoard
) -> "tuple[list, list, list[str], list[str]]":
    """The four-bucket reconciliation: (new, changed, unchanged_refs, closed_refs).

    `new`/`changed` are lists of (item_ref, DifficultyRecord); `closed` is every prior
    item_ref absent from this run's live set — dropped without re-scoring, never
    re-derived from a heuristic about what "probably" closed it.
    """
    live_refs: "set[str]" = set()
    new_items: "list[tuple[str, DifficultyRecord]]" = []
    changed_items: "list[tuple[str, DifficultyRecord]]" = []
    unchanged_refs: "list[str]" = []
    for record in records:
        ref = _item_ref(record)
        live_refs.add(ref)
        text = _backlog_text(record)
        if prior.is_unchanged(ref, text, "open"):
            unchanged_refs.append(ref)
        elif ref in prior.items:
            changed_items.append((ref, record))
        else:
            new_items.append((ref, record))
    closed_refs = [ref for ref in prior.items if ref not in live_refs]
    return new_items, changed_items, unchanged_refs, closed_refs


def build_worklist(
    new_items: "list[tuple[str, DifficultyRecord]]",
    changed_items: "list[tuple[str, DifficultyRecord]]",
    coverage_gaps: "list[dict]",
    closed_refs: "list[str]",
    *,
    now: "datetime | None" = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    items = []
    for bucket, batch in (("new", new_items), ("changed", changed_items)):
        for ref, record in batch:
            items.append(
                {
                    "item_ref": ref,
                    "bucket": bucket,
                    "title": record.target,
                    "functional_ground": record.functional_ground,
                    "severity": record.severity.value,
                    "reporter": record.reporter,
                    "evidence": record.evidence,
                    "cost_estimate": record.cost_estimate,
                    "source_digest": item_digest(_backlog_text(record), "open"),
                }
            )
    return {
        "schema": BOARD_SCHEMA,
        "generated_at": now.isoformat(),
        "coverage_gaps": coverage_gaps,
        # Not itself an "item" (per the Phase-A contract, only new+changed are) — bookkeeping
        # Phase B needs, since it has no live channel access of its own to re-derive "closed".
        "closed_refs": list(closed_refs),
        "items": items,
    }


def _atomic_write_json(payload: dict, path: "str | Path") -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def write_worklist(worklist: dict, path: "str | Path") -> None:
    _atomic_write_json(worklist, path)


# --- backlog producer: classification + the deterministic scoring half -----

def _constrained_rank(refs: "list[str]", items: "dict[str, PriorBoardItem]") -> "list[str]":
    """Order `refs` by score descending, with a HARD partial order from `blocked_by`
    edges: a blocked item never precedes its blocker, regardless of score. A blocker
    absent from this board (already closed, or never existed) imposes no constraint.
    A cycle among the remaining items gives up enforcing it (falls back to score order
    for just those items) rather than looping forever.
    """
    remaining = sorted(refs, key=lambda r: (-(items[r].score or 0.0), r))
    result: "list[str]" = []
    guard = len(remaining) + 1
    while remaining and guard:
        guard -= 1
        ready = [
            r for r in remaining
            if all(b not in remaining or b in result for b in items[r].blocked_by)
        ]
        if not ready:
            ready = remaining  # a cycle: stop enforcing the order for what's left
        pick = ready[0]
        result.append(pick)
        remaining.remove(pick)
    return result


def classify_and_score(
    prior: PriorBoard,
    classified: "dict[str, dict]",
    closed_refs: "Iterable[str]",
    *,
    config_path: "str | Path" = CONFIG_PATH,
    now: "datetime | None" = None,
) -> "tuple[PriorBoard, list[Finding], list[str]]":
    """Phase B: validate, cluster, score, rank, and merge. Returns (board, findings,
    no_urgency_signal_refs).

    Every classification vocabulary field is validated up front (test case: an
    out-of-vocabulary value is rejected) before any item is scored, so a single bad
    input never gets a partial, misleading board written from it.
    """
    now = now or datetime.now(timezone.utc)
    closed = set(closed_refs)

    for ref, c in classified.items():
        _validate_vocab("breadth", c.get("breadth"), BREADTH_WEIGHTS)
        _validate_vocab("cost_to_resolve", c.get("cost_to_resolve"), _BUDGET_TIER_KEYS)
        _validate_vocab("in_flight", c.get("in_flight"), IN_FLIGHT_COEFFICIENTS)
        _validate_vocab(
            "recommended_next_step", c.get("recommended_next_step"), RECOMMENDED_NEXT_STEPS
        )

    # Every unchanged prior item is carried forward VERBATIM: no re-classification, no
    # re-clustering, no rescoring — only a global rank recompute touches it (below).
    carried = {
        ref: item
        for ref, item in prior.items.items()
        if ref not in closed and ref not in classified
    }

    clusters = cluster_by_ground(
        list(classified.items()), lambda kv: kv[1].get("functional_ground", "")
    )
    cluster_size = {ref: len(group) for group in clusters for ref, _c in group}

    no_urgency_signal: "list[str]" = []
    fresh: "dict[str, PriorBoardItem]" = {}
    findings: "list[Finding]" = []
    for ref, c in classified.items():
        severity = Severity.parse(c.get("severity", "medium"))
        other_cluster_count = cluster_size.get(ref, 1) - 1
        recurrence_mass = severity.mass + other_cluster_count
        evidence = tuple(e for e in (c.get("evidence"),) if e)
        if other_cluster_count == 0:
            # Operationalized "no severity signal AND no cluster" as a singleton cluster:
            # every DifficultyRecord always carries a severity (the GitHub adapter
            # defaults an unlabeled issue to MEDIUM), so "no signal" can't be observed
            # post-parse — a lone item with no cluster-mates is the closest proxy.
            no_urgency_signal.append(ref)
            fresh[ref] = PriorBoardItem(
                classification="no-urgency-signal",
                score=None,
                rank=None,
                source_digest=c.get("source_digest", ""),
                title=c.get("title", ref),
                functional_ground=c.get("functional_ground", ""),
                evidence=evidence,
                recommended_next_step=c["recommended_next_step"],
                blocked_by=tuple(c.get("blocked_by") or ()),
            )
            continue
        score = score_item(
            c["breadth"], recurrence_mass, c["cost_to_resolve"], c["in_flight"],
            config_path=config_path,
        )
        fresh[ref] = PriorBoardItem(
            classification=c["breadth"],
            score=score,
            rank=None,
            source_digest=c.get("source_digest", ""),
            title=c.get("title", ref),
            functional_ground=c.get("functional_ground", ""),
            evidence=evidence,
            recommended_next_step=c["recommended_next_step"],
            blocked_by=tuple(c.get("blocked_by") or ()),
        )

    all_items = {**carried, **fresh}
    scored_refs = [ref for ref, item in all_items.items() if item.score is not None]
    order = _constrained_rank(scored_refs, all_items)
    rank_of = {ref: i + 1 for i, ref in enumerate(order)}

    final_items: "dict[str, PriorBoardItem]" = {}
    for ref, item in all_items.items():
        final_items[ref] = PriorBoardItem(
            classification=item.classification,
            score=item.score,
            rank=rank_of.get(ref),
            source_digest=item.source_digest,
            title=item.title,
            functional_ground=item.functional_ground,
            evidence=item.evidence,
            recommended_next_step=item.recommended_next_step,
            blocked_by=item.blocked_by,
        )
        if final_items[ref].rank is not None:
            findings.append(
                Finding(
                    kind="backlog-item",
                    signal=ref,
                    title=final_items[ref].title,
                    functional_ground=final_items[ref].functional_ground,
                    evidence=final_items[ref].evidence,
                    cost_signal=CostSignal(measured=False, basis="backlog triage rubric (proxy, not measured)"),
                    source_ref=ref,
                    recommended_next_step=final_items[ref].recommended_next_step,
                )
            )

    board = PriorBoard(schema=BOARD_SCHEMA, generated_at=now.isoformat(), items=final_items)
    return board, findings, no_urgency_signal


# --- CLI ----------------------------------------------------------------

def _run_backlog_phase_a(args: argparse.Namespace) -> int:
    prior = load_prior_board(args.prior) if args.prior else _empty_board()
    channels = args.channels or default_channels()
    records, coverage_gaps = collect_records(channels)
    new_items, changed_items, unchanged_refs, closed_refs = diff_backlog(records, prior)
    worklist = build_worklist(new_items, changed_items, coverage_gaps, closed_refs)
    write_worklist(worklist, args.emit_worklist)
    print(
        f"improvement-scan backlog (phase A): {len(new_items)} new, {len(changed_items)} changed, "
        f"{len(unchanged_refs)} unchanged, {len(closed_refs)} closed, "
        f"{len(coverage_gaps)} coverage gap(s) -> {args.emit_worklist}"
    )
    return 0


def _run_backlog_phase_b(args: argparse.Namespace) -> int:
    prior = load_prior_board(args.prior) if args.prior else _empty_board()
    try:
        payload = json.loads(Path(args.classifications).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"improvement-scan backlog (phase B): cannot read classifications: {exc}", file=sys.stderr)
        return 2
    closed_refs = payload.get("closed_refs") or []
    classified = payload.get("items") or {}

    try:
        board, findings, no_urgency_signal = classify_and_score(prior, classified, closed_refs)
    except ValueError as exc:
        print(f"improvement-scan backlog (phase B): {exc}", file=sys.stderr)
        return 2

    write_board(board, args.out)
    store_findings(findings, store_path=args.store)
    print(
        f"improvement-scan backlog (phase B): {len(board.items)} item(s) on the board "
        f"({len(no_urgency_signal)} no-urgency-signal), {len(findings)} finding(s) stored -> {args.out}"
    )
    if no_urgency_signal:
        print("  no urgency signal: " + ", ".join(sorted(no_urgency_signal)), file=sys.stderr)
    return 0


def _cmd_backlog(args: argparse.Namespace) -> int:
    if args.emit_worklist:
        return _run_backlog_phase_a(args)
    if args.classifications and args.out:
        return _run_backlog_phase_b(args)
    print(
        "improvement-scan backlog: pass either --emit-worklist (phase A) or "
        "--classifications/--out (phase B)",
        file=sys.stderr,
    )
    return 2


def _run_telemetry_scan(args: argparse.Namespace) -> int:
    ledger_path = Path(args.ledger) if args.ledger else DEFAULT_POLICY_LEDGER
    spawn_ledger_path = Path(args.spawn_ledger) if args.spawn_ledger else SPAWN_LEDGER_DEFAULT
    cursor_path = Path(args.cursor) if args.cursor else DEFAULT_TELEMETRY_CURSOR

    ok, message = shell.refresh_policy_ledger(args.days, ledger_path=ledger_path)
    degraded = not ok
    if degraded:
        print(f"improvement-scan telemetry: degraded — ledger refresh failed: {message}", file=sys.stderr)

    cursor = LedgerCursor.load(cursor_path)
    ledger_rows = _load_ledger_rows(ledger_path)
    spawn_rows = read_spawn_rows(spawn_ledger_path)
    items, due_marks = scan_telemetry(ledger_rows, spawn_rows, cursor)

    bundle = build_evidence_bundle(
        items, days=args.days, sessions_scanned=len(due_marks),
        degraded_refresh=degraded, degraded_reason=(message if degraded else None),
    )

    if not args.dry_run:
        _atomic_write_json(bundle, args.emit_evidence)
        for session_id, mtime in due_marks:
            cursor.mark(session_id, mtime)
        cursor.save(cursor_path)

    print(
        f"improvement-scan telemetry (scan): {len(due_marks)} session(s) due, "
        f"{len(items)} evidence item(s) -> {args.emit_evidence}" + (" [degraded]" if degraded else "")
    )
    return 1 if degraded else 0


def _run_telemetry_grounds(args: argparse.Namespace) -> int:
    try:
        grounds = json.loads(Path(args.grounds).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"improvement-scan telemetry (grounds): cannot read grounds: {exc}", file=sys.stderr)
        return 2
    if not isinstance(grounds, list):
        print("improvement-scan telemetry (grounds): grounds file must be a JSON list", file=sys.stderr)
        return 2

    board = load_prior_board(args.board) if args.board else None
    findings, dedup_log = build_findings_from_grounds(grounds, board=board)
    stored = [] if args.dry_run else store_findings(findings, store_path=args.store)

    print(
        f"improvement-scan telemetry (grounds): {len(grounds)} ground(s) in, "
        f"{len(stored)} finding(s) stored, {len(dedup_log)} dedup outcome(s) logged"
    )
    for entry in dedup_log:
        if entry["outcome"] != "no-match":
            print(f"  {entry['outcome']}: {entry['detector']} — {entry['detail'][:120]}", file=sys.stderr)
    return 0


def _cmd_telemetry(args: argparse.Namespace) -> int:
    if args.emit_evidence:
        return _run_telemetry_scan(args)
    if args.grounds:
        return _run_telemetry_grounds(args)
    print(
        "improvement-scan telemetry: pass either --emit-evidence (scan) or --grounds (store)",
        file=sys.stderr,
    )
    return 2


def _cmd_report(args: argparse.Namespace) -> int:
    print("improvement-scan report: not implemented in this stage", file=sys.stderr)
    return 2


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="never write, only print")
    sub = parser.add_subparsers(dest="command", required=True)

    p_backlog = sub.add_parser("backlog", help="reconcile Core + Org backlog against the Triage Board")
    p_backlog.add_argument("--prior", default=None, help="path to the existing board.json")
    p_backlog.add_argument(
        "--emit-worklist", default=None,
        help="phase A: collect + diff against --prior, write the new+changed worklist here",
    )
    p_backlog.add_argument(
        "--classifications", default=None,
        help="phase B: model-supplied classifications file (see --emit-worklist's output shape)",
    )
    p_backlog.add_argument("--out", default=None, help="phase B: write the merged board here")
    p_backlog.add_argument(
        "--channel", action="append", default=[], dest="channels",
        help="channel to pull from (repeatable); default: core-difficulty-digest's default_channels()",
    )
    p_backlog.add_argument("--store", default=None, help="findings store path (phase B only)")
    p_backlog.set_defaults(func=_cmd_backlog)

    p_telemetry = sub.add_parser("telemetry", help="scan recent session telemetry for recurring difficulties")
    p_telemetry.add_argument(
        "--emit-evidence", default=None,
        help="scan mode: refresh the policy ledger, run detectors, write the evidence bundle here",
    )
    p_telemetry.add_argument("--days", type=int, default=DEFAULT_TELEMETRY_DAYS, help="scan mode: policy-scorecard --days window")
    p_telemetry.add_argument("--ledger", default=None, help="scan mode: policy ledger path (default: ~/.local/log/claude-policy-ledger.jsonl)")
    p_telemetry.add_argument("--spawn-ledger", default=None, help="scan mode: spawn-cost ledger path (default: agentctl.cost.COST_LOG)")
    p_telemetry.add_argument("--cursor", default=None, help="scan mode: LedgerCursor state path")
    p_telemetry.add_argument(
        "--grounds", default=None,
        help="store mode: model-supplied functional-ground proposals (JSON list) for a prior evidence bundle",
    )
    p_telemetry.add_argument("--board", default=None, help="store mode: backlog board.json to dedup grounds against")
    p_telemetry.add_argument("--store", default=None, help="store mode: findings store path")
    p_telemetry.set_defaults(func=_cmd_telemetry)

    p_report = sub.add_parser("report", help="render the unified ranked report from stored findings")
    p_report.set_defaults(func=_cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
