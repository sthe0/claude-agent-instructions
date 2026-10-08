#!/usr/bin/env python3
"""Standing, resumable self-improvement scan — the mechanized half of a
discipline that today lives only in prose.

Difficulty removed: two recurring pieces of self-improvement work are already
DOCUMENTED but stay MANUAL every time. `memory-global/leaves/backlog-triage-
practice.md` names its own gap #2 — no cross-source priority digest script
exists, only a scratchpad `score-backlog.py` that was never committed — for
reconciling the Core + Org backlog against the board state file. Core
issue #144 is the filed form of the other half: nothing periodic reads the
session transcripts, so "where does the quota go" is answered once and then
decays. This module supplies the shared core two resumable producers build on:
a `Finding` model, registration as a second external producer in the existing
`self_diagnose_store.py`, and one resume seam per producer.

The `loss` subcommand ranks the board by MEASURED loss — distinct sessions whose transcripts
show an item's observable signature, scaled by a model-judged precision, times minutes per
occurrence (`improvement_scan_loss.py`) — instead of the breadth x severity / cost proxy, which
stays as `old_score` and as a tie-break.

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
import re
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import self_diagnose_store as sds  # noqa: E402
from lib import semantic_join  # noqa: E402
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
cluster_by_ground_result = _rec_for_scan.cluster_by_ground_result
cluster_judge_line = _rec_for_scan.cluster_judge_line

# The telemetry producer's only two subprocess reaches (policy-scorecard.py's ledger
# upsert, record-experience.py's dedup search) live in this sibling module, never here —
# see improvement_scan_shell.py's docstring for why, and
# test_module_never_shells_out_or_reaches_the_network for the invariant this preserves.
import improvement_scan_shell as shell  # noqa: E402
import improvement_scan_loss as loss  # noqa: E402
from agentctl import advisor  # noqa: E402
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


# A backlog item's `cost_estimate` is free text (a model's best guess) — only a value
# shaped EXACTLY like a rate becomes a measured CostSignal; a range, a prose estimate,
# or a non-matching currency falls through to `measured=False` rather than guessing a
# normalization (CLAUDE.md's rule-vs-perception split: the structural match is the
# rule, and the model's classification of significance stays out of this parse).
_WEEKS_PER_MONTH = 4.345
_USD_RATE_RE = re.compile(r"^\$?(\d+(?:\.\d+)?)\s*/\s*(week|wk|month|mo)$", re.IGNORECASE)
_TOKEN_RATE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*tokens?\s*/\s*(week|wk|month|mo)$", re.IGNORECASE)


def parse_backlog_cost_rate(cost_estimate: "str | None") -> CostSignal:
    """Structurally parse a backlog item's `cost_estimate` into a CostSignal.

    Matches `$40/week`, `40/month`, `120 tokens/week` (case-insensitive); a monthly
    rate is normalized to weekly by dividing by `_WEEKS_PER_MONTH`. Anything else —
    including a range, a prose estimate, or `"not estimable: n/a"` — yields
    `measured=False`, never an ambiguous parse.
    """
    text = (cost_estimate or "").strip()
    for pattern, unit in ((_USD_RATE_RE, "usd"), (_TOKEN_RATE_RE, "tokens")):
        m = pattern.match(text)
        if not m:
            continue
        value = float(m.group(1))
        period = m.group(2).lower()
        weekly = value if period in ("week", "wk") else value / _WEEKS_PER_MONTH
        return CostSignal(
            usd_per_week=weekly,
            basis=f"backlog cost_estimate rate ({unit}): {text!r}",
            measured=True,
        )
    return CostSignal(measured=False, basis="backlog triage rubric (proxy, not measured)")


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
    # The triage rubric's score, carried through ONLY so the `report` renderer can
    # order the unmeasured band without re-deriving it; never compared against a
    # measured CostSignal (the two bands never mix — see `_rank_findings`).
    proxy_score: "float | None" = None
    # Telemetry store keys this backlog item says it addresses; resolved against the
    # report's open telemetry rows at report time, never against a persisted cost.
    addresses: "tuple[str, ...]" = ()

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

    The store's schema predates this producer and stays generic across all three
    producers, so the richer fields (`cost_signal`, `evidence`, `functional_ground`,
    `recommended_next_step`, `proxy_score`) are JSON-encoded into `detail` — the only
    field the generic row shape leaves free — so the `report` renderer (stage 5) can
    reconstruct a full Finding from a bare store file, without needing the run that
    produced it still in memory. Confirmed safe: no existing consumer of an advisory
    (non-actionable) row's `detail` prints it as human text (self-diagnose-due's
    `report()` only counts advisory rows; `describe()` is called on actionable rows
    only) — see `finding_key`'s callers in self_diagnose_store.py.
    """
    payload = {
        "title": finding.title,
        "functional_ground": finding.functional_ground,
        "evidence": list(finding.evidence),
        "cost_signal": {
            "usd_per_week": finding.cost_signal.usd_per_week,
            "attention_per_week": finding.cost_signal.attention_per_week,
            "stability_per_week": finding.cost_signal.stability_per_week,
            "basis": finding.cost_signal.basis,
            "measured": finding.cost_signal.measured,
        },
        "source_ref": finding.source_ref,
        "recommended_next_step": finding.recommended_next_step,
        "proxy_score": finding.proxy_score,
    }
    if finding.addresses:
        payload["addresses"] = list(finding.addresses)
    return {
        "kind": finding.kind,
        "path": finding.signal,
        "detail": json.dumps(payload, ensure_ascii=False),
    }


def store_findings(
    findings: Iterable[Finding],
    *,
    kinds: "frozenset[str]",
    store_path: "str | Path | None" = None,
) -> "list[dict]":
    """Upsert this scan's findings under the improvement-scan source.

    Delegates entirely to `self_diagnose_store.upsert_findings`, which
    source- and kind-partitions its resolve-out — this call can only retire rows
    of `kinds` it could itself have produced, so it can never resolve away
    self-diagnose's, policy-scorecard's, or the other improvement-scan
    producer's rows.
    """
    records = [_finding_record(f) for f in findings]
    return sds.upsert_findings(
        records, path=store_path, source=sds.SOURCE_IMPROVEMENT_SCAN, kinds=kinds
    )


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
    # The raw classified `cost_estimate` text, carried so a re-derived Finding for a
    # CARRIED (unchanged) item can recompute its CostSignal via
    # `parse_backlog_cost_rate` without a fresh classifier call.
    cost_estimate: str = ""
    # Whether the live record carried an explicit severity label when last classified (a
    # labeled singleton is scorable), and the size of the cluster it was scored in.
    severity_labeled: bool = False
    cluster_size: int = 1
    # True when a nominated clustering pair involving this item got no genuine judge verdict,
    # so its cluster_size is a lower bound, not a confirmed count.
    unjudged: bool = False
    addresses: "tuple[str, ...]" = ()
    # The rubric inputs `loss.py`'s tie-break reads; carried so a rank recompute never needs
    # the live record. `old_score` is the legacy rubric score computed for EVERY item (a
    # missing severity counts as the lowest mass) — a tie-breaker and a column, never the rank.
    severity: str = ""
    cost_to_resolve: str = ""
    old_score: "float | None" = None
    # Measured-loss inputs (all additive and defaulted: an old board loads unchanged).
    # `signatures` are observable strings the transcripts would show; `precision` was judged
    # on the signature set whose digest is `signatures_digest` and is stale under any other.
    signatures: "tuple[str, ...]" = ()
    minutes_per_occurrence: "float | None" = None
    minutes_basis: str = ""
    precision: "float | None" = None
    precision_sample: "dict | None" = None
    signatures_digest: str = ""
    family: str = ""
    silent_estimate: str = ""
    loss_measurement: "dict | None" = None


@dataclass(frozen=True)
class PriorBoard:
    schema: int
    generated_at: str
    items: "dict[str, PriorBoardItem]" = field(default_factory=dict)

    def is_unchanged(self, item_ref: str, text: str, status: str) -> bool:
        prior = self.items.get(item_ref)
        return prior is not None and prior.source_digest == item_digest(text, status)


def board_state_path() -> Path:
    """The durable board: a local file the next run reads back. Resolved at call time."""
    override = os.environ.get("IMPROVEMENT_SCAN_BOARD_STATE")
    if override:
        return Path(override)
    return Path.home() / ".local" / "state" / "improvement-scan" / "board.json"


def _empty_board(now: "datetime | None" = None) -> PriorBoard:
    now = now or datetime.now(timezone.utc)
    return PriorBoard(schema=BOARD_SCHEMA, generated_at=now.isoformat(), items={})


def load_prior_board(path: "str | Path") -> PriorBoard:
    """Fail-open: a missing, empty or corrupt board yields an empty board — a
    board that fails to load must never be read as "everything unchanged",
    since that would silently suppress every finding it should have surfaced.
    """
    raw = _read_board_json(Path(path))
    if raw is None:
        return _empty_board()
    return _prior_from_raw(raw)


def _read_board_json(path: Path) -> "dict | None":
    """The parsed board dict, or None when it is unreadable or of another schema."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return raw if isinstance(raw, dict) and raw.get("schema") == BOARD_SCHEMA else None


def _float_or_none(value) -> "float | None":
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _prior_from_raw(raw: dict) -> PriorBoard:
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
            cost_estimate=str(entry.get("cost_estimate", "")),
            severity_labeled=entry.get("severity_labeled") is True,
            cluster_size=int(entry.get("cluster_size", 1)),
            unjudged=entry.get("unjudged") is True,
            addresses=tuple(str(a) for a in (entry.get("addresses") or ())),
            severity=str(entry.get("severity", "")),
            cost_to_resolve=str(entry.get("cost_to_resolve", "")),
            old_score=_float_or_none(entry.get("old_score")),
            signatures=tuple(str(s) for s in (entry.get("signatures") or ())),
            minutes_per_occurrence=_float_or_none(entry.get("minutes_per_occurrence")),
            minutes_basis=str(entry.get("minutes_basis", "")),
            precision=_float_or_none(entry.get("precision")),
            precision_sample=(
                entry["precision_sample"] if isinstance(entry.get("precision_sample"), dict) else None
            ),
            signatures_digest=str(entry.get("signatures_digest", "")),
            family=str(entry.get("family", "")),
            silent_estimate=str(entry.get("silent_estimate", "")),
            loss_measurement=(
                entry["loss_measurement"] if isinstance(entry.get("loss_measurement"), dict) else None
            ),
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
                "cost_estimate": item.cost_estimate,
                "severity_labeled": item.severity_labeled,
                "cluster_size": item.cluster_size,
                "unjudged": item.unjudged,
                "addresses": list(item.addresses),
                "severity": item.severity,
                "cost_to_resolve": item.cost_to_resolve,
                "old_score": item.old_score,
                "signatures": list(item.signatures),
                "minutes_per_occurrence": item.minutes_per_occurrence,
                "minutes_basis": item.minutes_basis,
                "precision": item.precision,
                "precision_sample": item.precision_sample,
                "signatures_digest": item.signatures_digest,
                "family": item.family,
                "silent_estimate": item.silent_estimate,
                "loss_measurement": item.loss_measurement,
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
    attention = row.get("attention") or {}
    corrections = attention.get("corrections") or 0
    if corrections < ATTENTION_BURN_MIN_CORRECTIONS:
        return None
    unjudged = attention.get("corrections_unjudged") or 0
    description = (
        f"{corrections} user correction(s) >= threshold ({ATTENTION_BURN_MIN_CORRECTIONS}) "
        "— CLAUDE.md's own overcome-difficulty trigger"
    )
    if unjudged:
        description += f"; {unjudged} further flagged prompt(s) the judge did not decide"
    return {
        "detector": "attention-burn",
        "measured": {"corrections": corrections, "corrections_unjudged": unjudged},
        "description": description,
    }


CONDITION4_GAP_THRESHOLD_CONFIG_KEY = "principle-promotion-threshold"
ESCALATION_LEDGER_ENV = "AGENTCTL_ESCALATION_LEDGER"
DEFAULT_ESCALATION_LEDGER = Path.home() / ".local" / "log" / "claude-plan-review-escalations.jsonl"


def _detect_condition4_gap_recurrence(row: dict, spawn_rows: "list[dict]", *, config_path) -> "dict | None":
    threshold = _read_config_float(CONDITION4_GAP_THRESHOLD_CONFIG_KEY, config_path)
    if threshold is None:
        return None
    path = Path(os.environ.get(ESCALATION_LEDGER_ENV) or DEFAULT_ESCALATION_LEDGER)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    last_outcome: "dict[tuple, str]" = {}
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        key = tuple(entry.get(k) for k in ("session", "plan_path", "unit"))
        outcome = entry.get("outcome")
        if None in key or outcome is None:
            continue
        last_outcome[key] = outcome
    confirmed = sum(1 for outcome in last_outcome.values() if outcome == "confirmed-gap")
    if confirmed < threshold:
        return None
    return {
        "detector": "condition4-gap-recurrence",
        "measured": {"confirmed_pairs": confirmed, "threshold": threshold},
        "description": (
            f"{confirmed} net-confirmed condition-4 plan-review gap(s) across reviewed pairs "
            f">= the {CONDITION4_GAP_THRESHOLD_CONFIG_KEY} threshold ({threshold:g}) "
            "— planners keep leaving interfaces incomplete"
        ),
    }


TELEMETRY_DETECTORS = (
    _detect_cost_concentration,
    _detect_replan_pressure,
    _detect_delegation_misses,
    _detect_spawn_process_failures,
    _detect_attention_burn,
    _detect_condition4_gap_recurrence,
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


def _board_ground_match(
    board: "PriorBoard | None", functional_ground: str, judge=None, budget=None
) -> "tuple[str | None, bool]":
    """(matched board ref | None, board undecided) for this ground. Identical text joins
    outright; otherwise word overlap nominates board items and a judge alone decides
    "same difficulty?". A match short-circuits the (costlier) record-experience
    subprocess search below — the two dedup against different stores, but either one
    finding the ground already tracked is sufficient. `undecided` is True when a
    nominated board item went unjudged, so the caller can report the dedup as
    incomplete rather than as a clean no-match."""
    if board is None:
        return None, False
    result = semantic_join.judged_match(
        functional_ground, list(board.items.items()),
        lambda entry: entry[1].functional_ground, judge=judge, budget=budget,
        k=semantic_join.K_FILING,
    )
    if result.outcome == "match":
        return result.candidate[0], False
    return None, result.outcome == "unjudged"


DEDUP_OUTCOMES = ("no-match", "dedup-match", "board-match", "search-failed", "judge-unavailable")


def _default_judge_runner():
    return advisor.subprocess_runner


def _parse_search_hits(output: str) -> "list[tuple[str, str]]":
    """The ranked hits `record-experience.py search` lists, as (leaf name, description)
    in listing order. Each hit is a `  [score] name.md` line followed by its
    description line; the `extend --leaf` helper line after it is not a hit."""
    hits: "list[tuple[str, str]]" = []
    lines = output.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"\s*\[\s*\d+\]\s+(\S+\.md)\s*$", line)
        if m and i + 1 < len(lines):
            hits.append((m.group(1), lines[i + 1].strip()))
    return hits


_CANDIDATE_EXCERPT_CHARS = 2000


def _parse_search_paths(output: str) -> "dict[str, str]":
    """Leaf name -> absolute path, from the `extend --leaf <path>` line after each hit."""
    paths: "dict[str, str]" = {}
    name = None
    for line in output.splitlines():
        m = re.match(r"\s*\[\s*\d+\]\s+(\S+\.md)\s*$", line)
        if m:
            name = m.group(1)
            continue
        m = re.match(r"\s*extend --leaf (\S+)\s*$", line)
        if m and name:
            paths[name] = m.group(1)
            name = None
    return paths


def _candidate_text(description: str, path: "str | None") -> str:
    """The description plus the leaf's `## Difficulty` section (what the search scored
    on), capped; an unreadable leaf gives the description alone."""
    if not path:
        return description
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return description
    m = re.search(r"^## Difficulty[ \t]*\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    if not m:
        return description
    return f"{description}\n\n{m.group(1).strip()}"[:_CANDIDATE_EXCERPT_CHARS]


def _judge_ground_against_candidates(
    ground_text: str, hits: "list[tuple[str, str]]", runner,
    paths: "dict[str, str] | None" = None,
) -> "tuple[str, str]":
    """(outcome, detail) for one ground against its lexically nominated candidates.
    The candidates are only nominees: the judge alone decides "same difficulty?".
    The walk stops at the first YES, and at the first fabricated answer — a judge
    that is down is not asked again for every remaining candidate."""
    enabled = semantic_join.judge_enabled()
    for name, description in hits:
        verdict, reason = advisor.judge_same_difficulty(
            ground_text, _candidate_text(description, (paths or {}).get(name)),
            runner, enabled=enabled,
        )
        if reason:
            return "judge-unavailable", f"{name}: {reason}"
        if verdict:
            return "dedup-match", f"{name}: {description}"
    return "no-match", f"{len(hits)} candidate(s) judged a different difficulty"


def build_findings_from_grounds(
    grounds: "list[dict]",
    *,
    board: "PriorBoard | None" = None,
    scope: str = "global",
    judge_runner=None,
) -> "tuple[list[Finding], list[dict]]":
    """For every model-supplied ground: dedup against the backlog board (if given)
    and existing experience leaves, recording every outcome — a dedup match is
    NEVER silently dropped, only excluded from the returned findings — then build
    survivors into Findings keyed by detector+ground, never by session id.

    Whether a ground is "the same difficulty" as an existing leaf is a question of
    meaning, so a model judge decides it (`advisor.judge_same_difficulty`); the
    keyword search only nominates the leaves to ask about. A judge that cannot
    answer keeps the finding (`judge-unavailable`): a duplicate costs a glance, a
    dropped finding costs the difficulty. A board item left unjudged makes an
    otherwise clean no-match `judge-unavailable` too. One judge budget covers the
    whole invocation.
    """
    findings: "list[Finding]" = []
    dedup_log: "list[dict]" = []
    runner = judge_runner if judge_runner is not None else _default_judge_runner()
    board_judge = None
    budget = semantic_join.env_budget()
    if judge_runner is not None:
        def board_judge(a, b, timeout):
            return advisor.judge_same_difficulty(
                a, b, judge_runner, enabled=semantic_join.judge_enabled(),
                timeout=max(1, int(timeout)))
    for g in grounds:
        detector = g["detector"]
        ground_text = g["functional_ground"]

        board_ref, board_undecided = _board_ground_match(
            board, ground_text, board_judge, budget)
        if board_ref is not None:
            dedup_log.append({
                "detector": detector, "functional_ground": ground_text,
                "outcome": "board-match", "detail": board_ref,
            })
            continue

        ok, _found, output = shell.search_experience(ground_text.split(), scope=scope)
        if not ok:
            outcome, detail = "search-failed", output
        else:
            hits = _parse_search_hits(output)
            if hits:
                outcome, detail = _judge_ground_against_candidates(
                    ground_text, hits, runner, _parse_search_paths(output)
                )
            else:
                outcome, detail = "no-match", output
        if outcome == "no-match" and board_undecided:
            outcome, detail = "judge-unavailable", "a nominated board item was not judged"
        dedup_log.append({
            "detector": detector, "functional_ground": ground_text,
            "outcome": outcome, "detail": detail,
        })
        if outcome == "dedup-match":
            continue

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


def rescore_candidates(
    records: "list[DifficultyRecord]", prior: PriorBoard
) -> "list[tuple[str, DifficultyRecord]]":
    """Unchanged prior items parked as no-urgency-signal or unjudged whose live record now
    carries an explicit severity label — offered for classification again instead of carried verbatim."""
    out = []
    for record in records:
        ref = _item_ref(record)
        item = prior.items.get(ref)
        if (
            item is not None
            and item.classification in ("no-urgency-signal", "unjudged")
            and record.severity_labeled
            and prior.is_unchanged(ref, _backlog_text(record), "open")
        ):
            out.append((ref, record))
    return out


def build_worklist(
    new_items: "list[tuple[str, DifficultyRecord]]",
    changed_items: "list[tuple[str, DifficultyRecord]]",
    coverage_gaps: "list[dict]",
    closed_refs: "list[str]",
    *,
    rescore_items: "list[tuple[str, DifficultyRecord]]" = (),
    now: "datetime | None" = None,
    prior: "PriorBoard | None" = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    closed = set(closed_refs)
    # Board items with neither an observable signature nor a silent-lane estimate: the
    # model's backlog of loss classification. Separate from `items`, which is the
    # new+changed worklist and keeps its contract.
    loss_unclassified = [
        {
            "item_ref": ref,
            "title": item.title,
            "functional_ground": item.functional_ground,
        }
        for ref, item in (prior.items.items() if prior is not None else ())
        if ref not in closed and not item.signatures and not item.silent_estimate.strip()
    ]
    items = []
    for bucket, batch in (
        ("new", new_items), ("changed", changed_items), ("rescore", rescore_items)
    ):
        for ref, record in batch:
            entry = {
                "item_ref": ref,
                "bucket": bucket,
                "title": record.title or record.target,
                "functional_ground": record.functional_ground,
                "severity": record.severity.value,
                "severity_labeled": record.severity_labeled,
                "reporter": record.reporter,
                "evidence": record.evidence,
                "cost_estimate": record.cost_estimate,
                "source_digest": item_digest(_backlog_text(record), "open"),
            }
            if bucket != "new" and prior is not None and ref in prior.items:
                entry["addresses"] = list(prior.items[ref].addresses)
            items.append(entry)
    return {
        "schema": BOARD_SCHEMA,
        "generated_at": now.isoformat(),
        "coverage_gaps": coverage_gaps,
        # Not itself an "item" (per the Phase-A contract, only new+changed are) — bookkeeping
        # Phase B needs, since it has no live channel access of its own to re-derive "closed".
        "closed_refs": list(closed_refs),
        "loss_unclassified": loss_unclassified,
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

def _constrained_rank(
    refs: "list[str]", items: "dict[str, PriorBoardItem]", key=None
) -> "list[str]":
    """Order `refs` by `key` ascending (default: score descending, then ref), with a HARD
    partial order from `blocked_by` edges: a blocked item never precedes its blocker,
    regardless of score. A blocker absent from this board (already closed, or never
    existed) imposes no constraint. A cycle among the remaining items gives up enforcing it
    (falls back to key order for just those items) rather than looping forever.
    """
    return loss.constrained_order(
        refs,
        {r: items[r].blocked_by for r in refs},
        key or (lambda r: (-(items[r].score or 0.0), r)),
    )


_HEX_DIGITS = frozenset("0123456789abcdef")


def _is_store_key(value) -> bool:
    return isinstance(value, str) and len(value) == 12 and set(value) <= _HEX_DIGITS


def parse_addresses(ref: str, value) -> "tuple[str, ...]":
    """Validate a classification's optional `addresses`: a list of 12-hex telemetry store
    keys (the key the report prints). Shape only — a key is deliberately NOT checked
    against the store here, since clusters resolve out between runs."""
    if value is None:
        return ()
    if not isinstance(value, list) or not all(_is_store_key(k) for k in value):
        raise ValueError(
            f"item {ref!r}: 'addresses' must be a list of 12-hex telemetry store keys, "
            f"got {value!r}"
        )
    return tuple(dict.fromkeys(value))


# The model-supplied measured-loss fields, valid both in a full classification and (as any
# non-empty subset, with `addresses`) in an amendment of a carried item.
LOSS_INPUT_KEYS = frozenset({
    "signatures", "minutes_per_occurrence", "minutes_basis", "precision", "precision_sample",
    "family", "silent_estimate",
})
AMENDABLE_KEYS = LOSS_INPUT_KEYS | {"addresses"}


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _nonempty_str(ref: str, key: str, value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"item {ref!r}: {key!r} must be a non-empty string, got {value!r}")
    return value.strip()


def parse_loss_fields(ref: str, c: dict) -> dict:
    """Validate the measured-loss fields present in classification/amendment `c` and return
    them as board-field values (only the keys present). Raises ValueError naming `ref`."""
    out: dict = {}
    if "signatures" in c:
        value = c["signatures"]
        if not isinstance(value, list):
            raise ValueError(f"item {ref!r}: 'signatures' must be a list of non-empty strings, got {value!r}")
        out["signatures"] = tuple(
            dict.fromkeys(_nonempty_str(ref, "signatures", s) for s in value)
        )
    if "minutes_per_occurrence" in c:
        value = c["minutes_per_occurrence"]
        if not _is_number(value) or value <= 0:
            raise ValueError(f"item {ref!r}: 'minutes_per_occurrence' must be a number > 0, got {value!r}")
        out["minutes_per_occurrence"] = float(value)
    if "minutes_basis" in c:
        out["minutes_basis"] = _nonempty_str(ref, "minutes_basis", c["minutes_basis"])
    if "precision" in c:
        value = c["precision"]
        if not _is_number(value) or not 0 <= value <= 1:
            raise ValueError(f"item {ref!r}: 'precision' must be a number in [0, 1], got {value!r}")
        out["precision"] = float(value)
    if "precision_sample" in c:
        value = c["precision_sample"]
        ok = (
            isinstance(value, dict)
            and isinstance(value.get("n"), int) and not isinstance(value.get("n"), bool)
            and isinstance(value.get("true"), int) and not isinstance(value.get("true"), bool)
            and value["n"] >= value["true"] >= 0
        )
        if not ok:
            raise ValueError(
                f"item {ref!r}: 'precision_sample' must be {{n, true}} integers with n >= true >= 0, "
                f"got {value!r}"
            )
        out["precision_sample"] = {"n": value["n"], "true": value["true"]}
    if "family" in c:
        if not isinstance(c["family"], str):
            raise ValueError(f"item {ref!r}: 'family' must be a string, got {c['family']!r}")
        out["family"] = c["family"].strip()
    if "silent_estimate" in c:
        out["silent_estimate"] = _nonempty_str(ref, "silent_estimate", c["silent_estimate"])
    return out


def apply_loss_fields(item: PriorBoardItem, fields: dict) -> PriorBoardItem:
    """`item` with validated loss fields applied. A precision is stamped with the digest of the
    signature set the item carries AFTER this edit, so editing signatures alone leaves an
    earlier precision stale."""
    if not fields:
        return item
    updated = replace(item, **fields)
    if "precision" in fields:
        updated = replace(updated, signatures_digest=loss.signatures_digest(updated.signatures))
    return updated


def classify_and_score(
    prior: PriorBoard,
    classified: "dict[str, dict]",
    closed_refs: "Iterable[str]",
    *,
    config_path: "str | Path" = CONFIG_PATH,
    now: "datetime | None" = None,
    join_stats: "dict | None" = None,
) -> "tuple[PriorBoard, list[Finding], list[str]]":
    """Phase B: validate, cluster, score, rank, and merge. Returns (board, findings,
    no_urgency_signal_refs). When `join_stats` is a dict it is filled with the clustering
    judge's counts (judged_calls, cached_hits, identity_joins, unjudged_items).

    Every classification vocabulary field is validated up front (test case: an
    out-of-vocabulary value is rejected) before any item is scored, so a single bad
    input never gets a partial, misleading board written from it.
    """
    now = now or datetime.now(timezone.utc)
    closed = set(closed_refs)

    addresses_of = {ref: parse_addresses(ref, c.get("addresses")) for ref, c in classified.items()}
    loss_of = {ref: parse_loss_fields(ref, c) for ref, c in classified.items()}
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

    entries = list(classified.items())
    clustering = cluster_by_ground_result(
        entries, lambda kv: kv[1].get("functional_ground", "")
    )
    if join_stats is not None:
        join_stats.update(clustering.stats.as_dict())
    cluster_size = {ref: len(group) for group in clustering.groups for ref, _c in group}
    unjudged_refs = {ref for (ref, _c), flag in zip(entries, clustering.undecided) if flag}

    no_urgency_signal: "list[str]" = []
    fresh: "dict[str, PriorBoardItem]" = {}
    for ref, c in classified.items():
        severity = Severity.parse(c.get("severity", "medium"))
        other_cluster_count = cluster_size.get(ref, 1) - 1
        recurrence_mass = severity.mass + other_cluster_count
        severity_labeled = c.get("severity_labeled") is True
        unjudged = ref in unjudged_refs
        classification: "str | None" = c["breadth"]
        score: "float | None" = None
        if other_cluster_count == 0 and not severity_labeled and unjudged:
            # An undecided nominated pair leaves "no cluster" unconfirmed: park the item as
            # unjudged, not as a confirmed no-urgency singleton.
            classification = "unjudged"
        elif other_cluster_count == 0 and not severity_labeled:
            # "No severity signal AND no cluster": the adapter defaults an unlabeled issue
            # to MEDIUM, so only the record's `severity_labeled` flag tells a stated
            # severity from a defaulted one.
            classification = "no-urgency-signal"
            no_urgency_signal.append(ref)
        else:
            score = score_item(
                c["breadth"], recurrence_mass, c["cost_to_resolve"], c["in_flight"],
                config_path=config_path,
            )
        old_score = score
        if old_score is None:
            # The legacy score for an item the rubric parks: a missing severity counts as the
            # lowest mass. It only breaks ties among equally measured items (loss.py).
            old_score = score_item(
                c["breadth"], Severity.LOW.mass + other_cluster_count, c["cost_to_resolve"],
                c["in_flight"], config_path=config_path,
            )
        item = PriorBoardItem(
            classification=classification,
            score=score,
            rank=None,
            source_digest=c.get("source_digest", ""),
            title=c.get("title", ref),
            functional_ground=c.get("functional_ground", ""),
            evidence=tuple(e for e in (c.get("evidence"),) if e),
            recommended_next_step=c["recommended_next_step"],
            blocked_by=tuple(c.get("blocked_by") or ()),
            cost_estimate=c.get("cost_estimate", ""),
            severity_labeled=severity_labeled,
            cluster_size=cluster_size.get(ref, 1),
            unjudged=unjudged or classification == "unjudged",
            addresses=addresses_of[ref],
            severity=str(c.get("severity") or ""),
            cost_to_resolve=c["cost_to_resolve"],
            old_score=old_score,
        )
        fresh[ref] = apply_loss_fields(_inherit_loss_fields(item, prior.items.get(ref)), loss_of[ref])

    all_items = {**carried, **fresh}
    scored_refs = [ref for ref, item in all_items.items() if item.score is not None]
    order = _constrained_rank(scored_refs, all_items)
    rank_of = {ref: i + 1 for i, ref in enumerate(order)}
    if any(item.loss_measurement for item in all_items.values()):
        # A board that carries measured loss keeps its measured order across a phase B run;
        # `loss` recomputes it after the next measurement.
        rank_of = loss.lane_ranks(loss.build_lanes(all_items))

    final_items = {ref: replace(item, rank=rank_of.get(ref)) for ref, item in all_items.items()}
    board = PriorBoard(schema=BOARD_SCHEMA, generated_at=now.isoformat(), items=final_items)
    return board, emit_board_findings(final_items), no_urgency_signal


def _inherit_loss_fields(item: PriorBoardItem, prior: "PriorBoardItem | None") -> PriorBoardItem:
    """A re-classified item keeps the measured-loss fields its previous board entry held, so
    a changed ticket text does not discard the signatures and precision already judged."""
    if prior is None:
        return item
    return replace(
        item,
        signatures=prior.signatures,
        minutes_per_occurrence=prior.minutes_per_occurrence,
        minutes_basis=prior.minutes_basis,
        precision=prior.precision,
        precision_sample=prior.precision_sample,
        signatures_digest=prior.signatures_digest,
        family=prior.family,
        silent_estimate=prior.silent_estimate,
        loss_measurement=prior.loss_measurement,
    )


def emit_board_findings(items: "dict[str, PriorBoardItem]") -> "list[Finding]":
    """One backlog-item Finding per board item, whatever its severity label or measured state.

    `store_findings` resolves out every same-kind row absent from the newest scan, so both
    phase B and `loss` must emit the WHOLE board through this one function."""
    return [
        Finding(
            kind="backlog-item",
            signal=ref,
            title=item.title,
            functional_ground=item.functional_ground,
            evidence=item.evidence,
            cost_signal=parse_backlog_cost_rate(item.cost_estimate),
            source_ref=ref,
            recommended_next_step=item.recommended_next_step,
            proxy_score=item.old_score if item.old_score is not None else item.score,
            addresses=item.addresses,
        )
        for ref, item in items.items()
    ]


# --- report: one cost-first ranking over both producers' findings ----------
# The stored findings alone are enough to render (a bare `--store <path>`) — the richer
# fields survive there because `_finding_record` JSON-encodes them into `detail`.
# `--board`, when readable, adds the measured-loss / silent / awaiting lanes ahead of them.

# The rendered command TEXT for each closed-vocabulary next step — printed only,
# never executed (no subprocess/network reach anywhere in this section; asserted
# by the module-wide AST purity test).
_NEXT_STEP_COMMANDS = {
    "self-improvement": "Skill(self-improvement)",
    "planner": "Skill(planner)",
}


def _render_next_step(step: str, finding: dict) -> str:
    if step in _NEXT_STEP_COMMANDS:
        return _NEXT_STEP_COMMANDS[step]
    cost = finding["cost_signal"]
    cost_text = cost["basis"] if cost["measured"] else "not estimable: unmeasured"
    return (
        f"scripts/file-difficulty.py --target {finding['path']!r} "
        f"--ground {finding['functional_ground']!r} --cost {cost_text!r}"
    )


def _decode_finding_detail(row: dict) -> dict:
    try:
        return json.loads(row.get("detail") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def _report_findings(store_path: "str | Path | None") -> "list[dict]":
    """Every OPEN improvement-scan row, decoded back into report-shaped dicts."""
    rows = sds.advisory_open(sds.load_rows(store_path))
    out: "list[dict]" = []
    for row in rows:
        if row.get("source") != sds.SOURCE_IMPROVEMENT_SCAN:
            continue
        if row.get("kind") not in (sds.KIND_BACKLOG_ITEM, sds.KIND_TELEMETRY_PATTERN):
            continue
        payload = _decode_finding_detail(row)
        cost = payload.get("cost_signal") or {}
        out.append({
            "key": row.get("key", ""),
            "source": row.get("kind", ""),
            "path": row.get("path", ""),
            "title": payload.get("title", ""),
            "functional_ground": payload.get("functional_ground", ""),
            "evidence": list(payload.get("evidence") or ()),
            "cost_signal": {
                "usd_per_week": cost.get("usd_per_week"),
                "attention_per_week": cost.get("attention_per_week"),
                "stability_per_week": cost.get("stability_per_week"),
                "basis": cost.get("basis", ""),
                "measured": bool(cost.get("measured", False)),
            },
            "proxy_score": payload.get("proxy_score"),
            **({"addresses": list(payload["addresses"])} if payload.get("addresses") else {}),
            "source_ref": payload.get("source_ref", ""),
            "recommended_next_step": payload.get("recommended_next_step", "planner"),
            "first_seen": row.get("first_seen", ""),
            "times_surfaced": row.get("times_surfaced", 0),
            "status": row.get("status", "open"),
        })
    return out


def _cost_sort_key(cost: dict) -> tuple:
    return (
        cost["usd_per_week"] or 0.0,
        cost["attention_per_week"] or 0.0,
        cost["stability_per_week"] or 0.0,
    )


def _proxy_sort_key(finding: dict) -> tuple:
    score = finding.get("proxy_score")
    return (-(score if score is not None else float("-inf")), finding["source_ref"])


def _join_addressed_clusters(findings: "list[dict]") -> "list[dict]":
    """Resolve each backlog finding's `addresses` against the open telemetry rows of this
    same report, at report time. A resolved measured cluster gives the item that cluster's
    cost (the larger of it and the item's own) plus a `via` key; keys matching no open row
    are listed under `dangling`. The cost is copied, never summed across items."""
    telemetry = {
        f["key"]: f for f in findings if f["source"] == sds.KIND_TELEMETRY_PATTERN
    }
    joined: "list[dict]" = []
    for f in findings:
        keys = f.get("addresses") or []
        if not keys:
            joined.append(f)
            continue
        item = {**f, "dangling": [k for k in keys if k not in telemetry]}
        clusters = [
            telemetry[k] for k in keys
            if k in telemetry and telemetry[k]["cost_signal"]["measured"]
        ]
        if clusters:
            best = max(clusters, key=lambda c: _cost_sort_key(c["cost_signal"]))
            own = f["cost_signal"]
            if not own["measured"] or _cost_sort_key(best["cost_signal"]) > _cost_sort_key(own):
                item["cost_signal"] = best["cost_signal"]
                item["via"] = best["key"]
        joined.append(item)
    return joined


def _rank_findings(findings: "list[dict]") -> "list[dict]":
    """Measured band first (cost, then attention, then stability, descending), then
    the unmeasured band (proxy score descending) — NEVER interleaved, even when a
    proxy score is numerically higher than a measured finding's cost. A backlog item
    ranked via an addressed cluster follows that cluster's own row, by proxy score
    then source_ref."""
    findings = _join_addressed_clusters(findings)
    measured = sorted(
        (f for f in findings if f["cost_signal"]["measured"] and not f.get("via")),
        key=lambda f: _cost_sort_key(f["cost_signal"]), reverse=True,
    )
    via_rows: "dict[str, list[dict]]" = {}
    for f in findings:
        if f.get("via"):
            via_rows.setdefault(f["via"], []).append(f)
    measured = [
        r for f in measured
        for r in [f, *sorted(via_rows.get(f["key"], ()), key=_proxy_sort_key)]
    ]
    unmeasured = sorted(
        (f for f in findings if not f["cost_signal"]["measured"]),
        key=_proxy_sort_key,
    )
    return measured + unmeasured


def _format_cost(cost: dict) -> str:
    if not cost["measured"]:
        return "unmeasured (" + (cost["basis"] or "no basis given") + ")"
    parts = []
    if cost["usd_per_week"] is not None:
        parts.append(f"**{cost['usd_per_week']:.2f}/week**")
    if cost["attention_per_week"] is not None:
        parts.append(f"{cost['attention_per_week']:.2f} attention/week")
    if cost["stability_per_week"] is not None:
        parts.append(f"{cost['stability_per_week']:.2f} stability/week")
    return ", ".join(parts) + f" ({cost['basis']})"


def _render_finding_md(f: dict) -> str:
    evidence = ", ".join(f["evidence"]) if f["evidence"] else "(none)"
    via = f" — via {f['via']}" if f.get("via") else ""
    dangling = (
        [f"- **Dangling addresses:** {', '.join(f['dangling'])}"] if f.get("dangling") else []
    )
    return "\n".join([
        f"### {f['title']}",
        f"- **Functional ground:** {f['functional_ground']}",
        f"- **Cost:** {_format_cost(f['cost_signal'])}{via}",
        *dangling,
        f"- **Evidence:** {evidence}",
        f"- **Store key:** `{f['key']}` — {f['status']}, surfaced {f['times_surfaced']}x, "
        f"first seen {f['first_seen']}",
        f"- **Recommended next step:** `{_render_next_step(f['recommended_next_step'], f)}`",
    ])


def _lane_member_refs(lanes: dict) -> "set[str]":
    return (
        {m for row in lanes["loss"] + lanes["silent"] for m in row["members"]}
        | {row["ref"] for row in lanes["awaiting"]}
    )


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _address_notes(members: "list[str]", addresses: "dict[str, tuple[str, ...]]", telemetry: "set[str]") -> str:
    notes = []
    for member in members:
        for key in addresses.get(member, ()):
            notes.append(f"{member}: {'via' if key in telemetry else 'dangling'} `{key}`")
    return "; ".join(notes)


def _render_lanes_md(lanes: dict, ranked: "list[dict]", addresses: "dict[str, tuple[str, ...]]") -> "list[str]":
    telemetry = {f["key"] for f in ranked if f["source"] == sds.KIND_TELEMETRY_PATTERN}
    parts: "list[str]" = []
    if lanes["loss"]:
        parts += [
            "## Measured loss — min/week",
            "",
            "| min/week | sessions/window (raw → est) | min/occurrence | basis | fix cost | members |",
            "|---|---|---|---|---|---|",
        ]
        for row in lanes["loss"]:
            members = ", ".join(row["members"])
            notes = _address_notes(row["members"], addresses, telemetry)
            if notes:
                members += f" ({notes})"
            parts.append(
                f"| **{row['min_per_week']:.1f}** | {row['sessions_hit']} → {row['sessions_est']:.1f} "
                f"/ {row['window_days']}d | {row['min_per_occurrence']:.1f} | {_cell(row['basis'])} "
                f"| {row['fix_cost'] or '?'} | {_cell(members)} |"
            )
        parts.append("")
    return parts


def _render_tail_lanes_md(lanes: dict) -> "list[str]":
    parts: "list[str]" = []
    if lanes["silent"]:
        parts += ["## Silent lane — no observable signature", ""]
        for row in lanes["silent"]:
            parts.append(f"- **{row['ref']}** ({', '.join(row['members'])}) — {_cell(row['silent_estimate'])}")
        parts.append("")
    if lanes["awaiting"]:
        parts += ["## Awaiting loss classification", ""]
        for row in lanes["awaiting"]:
            parts.append(f"- **{row['ref']}** — {_cell(row['title'])}")
        parts.append("")
    return parts


def _render_markdown(
    ranked: "list[dict]", lanes: "dict | None" = None,
    addresses: "dict[str, tuple[str, ...]] | None" = None,
) -> str:
    lanes = lanes or {"loss": [], "silent": [], "awaiting": []}
    shown = _lane_member_refs(lanes)
    ranked = [
        f for f in ranked
        if not (f["source"] == sds.KIND_BACKLOG_ITEM and f["source_ref"] in shown)
    ]
    measured = [f for f in ranked if f["cost_signal"]["measured"]]
    unmeasured = [f for f in ranked if not f["cost_signal"]["measured"]]
    parts = ["# Improvement-scan report", ""]
    parts += _render_lanes_md(lanes, ranked, addresses or {})
    if measured:
        parts.append("## Measured cost signal — ranked by cost, then attention, then stability")
        parts.append("")
        for f in measured:
            parts.append(_render_finding_md(f))
            parts.append("")
    parts += _render_tail_lanes_md(lanes)
    if unmeasured:
        parts.append("## No measured cost signal — ordered by the triage rubric's proxy score")
        parts.append("")
        for f in unmeasured:
            parts.append(_render_finding_md(f))
            parts.append("")
    if not measured and not unmeasured and not (lanes["loss"] or lanes["silent"] or lanes["awaiting"]):
        parts.append("No open improvement-scan findings.")
    return "\n".join(parts).rstrip() + "\n"


def _render_json(ranked: "list[dict]", lanes: "dict | None" = None) -> str:
    lanes = lanes or {"loss": [], "silent": [], "awaiting": []}
    return json.dumps(
        {
            "findings": ranked,
            "loss": lanes["loss"],
            "silent": lanes["silent"],
            "awaiting": lanes["awaiting"],
        },
        ensure_ascii=False, indent=2,
    )


def _board_for_report(board_path: "str | None") -> "PriorBoard | None":
    raw = _read_board_json(Path(board_path)) if board_path else None
    return _prior_from_raw(raw) if raw is not None else None


def _cmd_report(args: argparse.Namespace) -> int:
    ranked = _rank_findings(_report_findings(args.store))
    board = _board_for_report(getattr(args, "board", None))
    lanes = loss.build_lanes(board.items) if board is not None else None
    if args.format == "json":
        print(_render_json(ranked, lanes))
    else:
        addresses = {ref: item.addresses for ref, item in board.items.items()} if board else None
        print(_render_markdown(ranked, lanes, addresses))
    return 0


# --- CLI ----------------------------------------------------------------

def _board_state_arg(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "board_state", None) or board_state_path())


def _load_prior(args: argparse.Namespace) -> PriorBoard:
    if args.prior:
        return load_prior_board(args.prior)
    state = _board_state_arg(args)
    if not state.exists():
        print(f"improvement-scan backlog: cold start — no board state at {state}", file=sys.stderr)
        return _empty_board()
    raw = _read_board_json(state)
    if raw is None:
        # The state file is the only durable copy: never let the next write clobber it unseen.
        note = f"improvement-scan backlog: board state at {state} is unreadable or of another schema"
        if not getattr(args, "dry_run", False):
            backup = state.with_name(state.name + ".bak")
            os.replace(state, backup)
            note += f"; moved to {backup}"
        print(note + " — starting from an empty board", file=sys.stderr)
        return _empty_board()
    return _prior_from_raw(raw)


def _run_backlog_phase_a(args: argparse.Namespace) -> int:
    prior = _load_prior(args)
    channels = args.channels or default_channels()
    records, coverage_gaps = collect_records(channels)
    new_items, changed_items, unchanged_refs, closed_refs = diff_backlog(records, prior)
    rescore_items = rescore_candidates(records, prior)
    worklist = build_worklist(
        new_items, changed_items, coverage_gaps, closed_refs, rescore_items=rescore_items,
        prior=prior,
    )
    write_worklist(worklist, args.emit_worklist)
    print(
        f"improvement-scan backlog (phase A): {len(new_items)} new, {len(changed_items)} changed, "
        f"{len(rescore_items)} rescore, {len(unchanged_refs)} unchanged, {len(closed_refs)} closed, "
        f"{len(coverage_gaps)} coverage gap(s) -> {args.emit_worklist}"
    )
    return 0


_REQUIRED_ITEM_METADATA = ("title", "functional_ground", "severity", "source_digest")


def _merge_worklist_metadata(
    classified: "dict[str, dict]", worklist_items: "list[dict] | None"
) -> "dict[str, dict]":
    """Overlay each classification on its worklist item (the classification wins for
    any field it names). Raises ValueError naming the ref (and field) on an unknown ref,
    on required metadata still missing after the merge, or on a worklist ref left unclassified.
    """
    by_ref = (
        {w.get("item_ref"): w for w in worklist_items} if worklist_items is not None else None
    )
    merged: "dict[str, dict]" = {}
    for ref, c in classified.items():
        base: dict = {}
        if by_ref is not None:
            if ref not in by_ref:
                raise ValueError(f"classified ref {ref!r} is not in the worklist")
            base = {k: v for k, v in by_ref[ref].items() if k not in ("item_ref", "bucket")}
        item = {**base, **c}
        for field in _REQUIRED_ITEM_METADATA:
            if not item.get(field):
                raise ValueError(
                    f"item {ref!r} lacks {field!r} (supply it, or pass --worklist to merge it)"
                )
        merged[ref] = item
    if by_ref is not None:
        unclassified = sorted(r for r in by_ref if r not in classified)
        if unclassified:
            raise ValueError("worklist ref(s) left unclassified: " + ", ".join(map(str, unclassified)))
    return merged


def _read_json_object(path: str, what: str) -> "dict | None":
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"improvement-scan backlog (phase B): cannot read {what}: {exc}", file=sys.stderr)
        return None
    if not isinstance(data, dict):
        print(f"improvement-scan backlog (phase B): {what} is not a JSON object", file=sys.stderr)
        return None
    return data


def _apply_addresses_amendments(
    prior: PriorBoard, items: "dict[str, dict]", worklist_items: "list[dict] | None"
) -> "tuple[PriorBoard, dict[str, dict]]":
    """Split off classifications that carry ONLY amendable keys (`addresses` and the
    measured-loss fields) for an item already on the board (and not in this worklist):
    they set exactly those fields (an empty `addresses` list clears) and change nothing
    else. Every other entry is returned untouched for the normal merge, which rejects an
    unknown ref or an incomplete entry."""
    in_worklist = {w.get("item_ref") for w in worklist_items or ()}
    kept: "dict[str, dict]" = {}
    amended = dict(prior.items)
    for ref, c in items.items():
        if (
            isinstance(c, dict)
            and c
            and set(c) <= AMENDABLE_KEYS
            and ref in prior.items
            and ref not in in_worklist
        ):
            item = prior.items[ref]
            if "addresses" in c:
                item = replace(item, addresses=parse_addresses(ref, c["addresses"]))
            amended[ref] = apply_loss_fields(item, parse_loss_fields(ref, c))
        else:
            kept[ref] = c
    return replace(prior, items=amended), kept


def _run_backlog_phase_b(args: argparse.Namespace) -> int:
    prior = _load_prior(args)
    payload = _read_json_object(args.classifications, "classifications")
    if payload is None:
        return 2
    worklist = None
    if args.worklist:
        worklist = _read_json_object(args.worklist, "worklist")
        if worklist is None:
            return 2
    closed_refs = payload.get("closed_refs") or (worklist or {}).get("closed_refs") or []
    items = payload.get("items") or {}
    if not isinstance(items, dict):
        print(
            "improvement-scan backlog (phase B): classifications 'items' must be an object "
            "keyed by item ref", file=sys.stderr,
        )
        return 2
    try:
        worklist_items = (worklist.get("items") or []) if worklist is not None else None
        prior, items = _apply_addresses_amendments(prior, items, worklist_items)
        classified = _merge_worklist_metadata(items, worklist_items)
    except ValueError as exc:
        print(f"improvement-scan backlog (phase B): {exc}", file=sys.stderr)
        return 2

    try:
        join_stats: dict = {}
        board, findings, no_urgency_signal = classify_and_score(
            prior, classified, closed_refs, join_stats=join_stats)
    except ValueError as exc:
        print(f"improvement-scan backlog (phase B): {exc}", file=sys.stderr)
        return 2

    state = _board_state_arg(args)
    if getattr(args, "dry_run", False):
        print(
            f"improvement-scan backlog (phase B, dry run): would write {len(board.items)} item(s) "
            f"to {state} and store {len(findings)} finding(s)"
        )
        return 0
    write_board(board, state)
    if args.out:
        write_board(board, args.out)
    store_findings(findings, kinds=frozenset([sds.KIND_BACKLOG_ITEM]), store_path=args.store)
    print(
        f"improvement-scan backlog (phase B): {len(board.items)} item(s) on the board "
        f"({len(no_urgency_signal)} no-urgency-signal), {len(findings)} finding(s) stored -> {state}"
    )
    print(cluster_judge_line(join_stats))
    if no_urgency_signal:
        print("  no urgency signal: " + ", ".join(sorted(no_urgency_signal)), file=sys.stderr)
    return 0


def _cmd_backlog(args: argparse.Namespace) -> int:
    if args.emit_worklist:
        return _run_backlog_phase_a(args)
    if args.classifications:
        return _run_backlog_phase_b(args)
    print(
        "improvement-scan backlog: pass either --emit-worklist (phase A) or "
        "--classifications (phase B)",
        file=sys.stderr,
    )
    return 2


def hits_cache_path() -> Path:
    return Path.home() / ".local" / "state" / "improvement-scan" / "loss-hits.json"


def _measure_board(
    board: PriorBoard, transcripts: "list[Path]", cache: dict, *,
    days: int, until: datetime, now: datetime,
) -> "tuple[dict[str, PriorBoardItem], dict[str, tuple], int]":
    """Count hit sessions for every board item that carries signatures and stamp each with
    its `loss_measurement`. Returns (items, specs, transcripts opened)."""
    since = until - timedelta(days=days)
    specs = {
        ref: (item.signatures, loss.issue_number(ref))
        for ref, item in board.items.items() if item.signatures
    }
    sessions, opened = loss.count_sessions(specs, transcripts, cache, since=since, until=until)
    items = {
        ref: (
            replace(item, loss_measurement=loss.measurement_for(
                item, sessions[ref], window_days=days, until=until, now=now))
            if ref in sessions else item
        )
        for ref, item in board.items.items()
    }
    return items, specs, opened


def _write_samples(
    path: str, items: "dict[str, PriorBoardItem]", specs: dict, transcripts: "list[Path]",
    cache: dict, *, days: int, until: datetime, sample_size: int,
) -> int:
    """Excerpts for the model's precision judgment, for every item whose precision is missing
    or was judged on a different signature set. Returns the number of items written."""
    since = until - timedelta(days=days)
    needing = {
        ref: spec for ref, spec in specs.items() if not loss.effective_precision(items[ref])[1]
    }
    samples = loss.build_samples(
        needing, transcripts, cache, since=since, until=until, sample_size=sample_size
    )
    _atomic_write_json({
        "window_days": days,
        "until": until.isoformat(),
        "sample_size": sample_size,
        "items": {
            ref: {
                "title": items[ref].title,
                "signatures": list(items[ref].signatures),
                "signatures_digest": loss.signatures_digest(items[ref].signatures),
                "samples": samples[ref],
            }
            for ref in needing
        },
    }, path)
    return len(needing)


def _cmd_loss(args: argparse.Namespace) -> int:
    if args.days < 1:
        print(f"improvement-scan loss: --days must be at least 1, got {args.days}", file=sys.stderr)
        return 2
    state = _board_state_arg(args)
    raw = _read_board_json(state)
    if raw is None:
        print(f"improvement-scan loss: no readable board state at {state}", file=sys.stderr)
        return 2
    board = _prior_from_raw(raw)
    try:
        until_arg = datetime.fromisoformat(args.until) if args.until else None
    except ValueError:
        print(f"improvement-scan loss: --until {args.until!r} is not an ISO timestamp", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc)
    _since, until = loss.window_bounds(args.days, until_arg)
    transcripts = loss.enumerate_transcripts([Path(p) for p in args.projects_roots] or None)
    cache_path = Path(args.hits_cache) if args.hits_cache else hits_cache_path()
    cache = loss.load_hit_cache(cache_path)

    items, specs, opened = _measure_board(
        board, transcripts, cache, days=args.days, until=until, now=now
    )
    lanes = loss.build_lanes(items)
    ranks = loss.lane_ranks(lanes)
    items = {ref: replace(item, rank=ranks.get(ref)) for ref, item in items.items()}
    measured = PriorBoard(schema=BOARD_SCHEMA, generated_at=now.isoformat(), items=items)
    findings = emit_board_findings(items)

    summary = (
        f"improvement-scan loss: {len(specs)} item(s) with signatures over {len(transcripts)} "
        f"transcript(s) ({opened} opened); lanes: {len(lanes['loss'])} loss, "
        f"{len(lanes['silent'])} silent, {len(lanes['awaiting'])} awaiting"
    )
    if getattr(args, "dry_run", False):
        print(summary + f" — dry run: nothing written, {len(findings)} finding(s) not stored")
        return 0
    loss.save_hit_cache(cache, cache_path)
    sampled = 0
    if args.emit_samples:
        sampled = _write_samples(
            args.emit_samples, items, specs, transcripts, cache,
            days=args.days, until=until, sample_size=args.sample_size,
        )
    write_board(measured, state)
    store_findings(findings, kinds=frozenset([sds.KIND_BACKLOG_ITEM]), store_path=args.store)
    print(summary + f"; {len(findings)} finding(s) stored -> {state}")
    if args.emit_samples:
        print(f"improvement-scan loss: {sampled} item(s) await a precision judgment -> {args.emit_samples}")
    return 0


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
    stored = [] if args.dry_run else store_findings(
        findings, kinds=frozenset([sds.KIND_TELEMETRY_PATTERN]), store_path=args.store
    )

    print(
        f"improvement-scan telemetry (grounds): {len(grounds)} ground(s) in, "
        f"{len(stored)} finding(s) stored, {len(dedup_log)} dedup outcome(s) logged"
    )
    counts = {o: sum(1 for e in dedup_log if e["outcome"] == o) for o in DEDUP_OUTCOMES}
    print("dedup outcomes: " + " ".join(f"{o}={n}" for o, n in counts.items()))
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="never write, only print")
    sub = parser.add_subparsers(dest="command", required=True)

    p_backlog = sub.add_parser("backlog", help="reconcile Core + Org backlog against the board state file")
    p_backlog.add_argument(
        "--prior", default=None,
        help="phase A: board to diff against (default: the board state file; absent = cold start)",
    )
    p_backlog.add_argument(
        "--board-state", default=None,
        help="the durable board state file (default: $IMPROVEMENT_SCAN_BOARD_STATE or "
        "~/.local/state/improvement-scan/board.json)",
    )
    p_backlog.add_argument(
        "--emit-worklist", default=None,
        help="phase A: collect + diff against --prior, write the new+changed worklist here",
    )
    p_backlog.add_argument(
        "--classifications", default=None,
        help="phase B: model-supplied classifications file (see --emit-worklist's output shape)",
    )
    p_backlog.add_argument(
        "--worklist", default=None,
        help="phase B: the phase-A worklist; its item metadata is merged under each classification",
    )
    p_backlog.add_argument(
        "--out", default=None, help="phase B: also write the merged board here (a view copy)"
    )
    p_backlog.add_argument(
        "--channel", action="append", default=[], dest="channels",
        help="channel to pull from (repeatable); default: core-difficulty-digest's default_channels()",
    )
    p_backlog.add_argument("--store", default=None, help="findings store path (phase B only)")
    p_backlog.set_defaults(func=_cmd_backlog)

    p_loss = sub.add_parser(
        "loss", help="count signature hits in session transcripts and rank the board by measured loss"
    )
    p_loss.add_argument(
        "--board-state", default=None,
        help="the durable board state file (default: $IMPROVEMENT_SCAN_BOARD_STATE or "
        "~/.local/state/improvement-scan/board.json)",
    )
    p_loss.add_argument("--store", default=None, help="findings store path")
    p_loss.add_argument(
        "--projects-root", action="append", default=[], dest="projects_roots",
        help="a `projects` directory of transcripts (repeatable); default: every configured root",
    )
    p_loss.add_argument("--days", type=int, default=loss.DEFAULT_WINDOW_DAYS, help="window length in days")
    p_loss.add_argument("--until", default=None, help="window end, ISO timestamp (default: now)")
    p_loss.add_argument(
        "--hits-cache", default=None,
        help="per-transcript hit cache (default: ~/.local/state/improvement-scan/loss-hits.json)",
    )
    p_loss.add_argument(
        "--emit-samples", default=None,
        help="write seeded excerpts for items whose precision is missing or stale here",
    )
    p_loss.add_argument(
        "--sample-size", type=int, default=loss.DEFAULT_SAMPLE_SIZE, help="excerpts per item"
    )
    p_loss.set_defaults(func=_cmd_loss)

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
    p_telemetry.add_argument(
        "--board", default=str(board_state_path()),
        help="store mode: board to dedup grounds against (default: the board state file)",
    )
    p_telemetry.add_argument("--store", default=None, help="store mode: findings store path")
    p_telemetry.set_defaults(func=_cmd_telemetry)

    p_report = sub.add_parser("report", help="render the unified ranked report from stored findings")
    p_report.add_argument("--store", default=None, help="findings store path to render")
    p_report.add_argument(
        "--board", default=str(board_state_path()),
        help="board state file; when readable, its measured-loss, silent and awaiting lanes are "
        "rendered ahead of the stored findings",
    )
    p_report.add_argument("--format", choices=("md", "json"), default="md", help="output format")
    p_report.set_defaults(func=_cmd_report)

    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
