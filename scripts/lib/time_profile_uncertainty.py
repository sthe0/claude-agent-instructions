"""Reconstruction error of the time profile, and whether it can change a decision.

Difficulty removed: a per-mechanism figure read from the transcript is silent on the time the
transcript hides -- a Pre/PostToolUse hook or a permission prompt runs inside a tool interval,
a prompt/stop hook that surfaces no output inside `model:gap`, an engine event without `ts`
leaves a hole in node dwell, and `unattributed` is the walker's own residual. Whether that is
small enough to leave telemetry alone depends on the decision the profile feeds (the ranking
of heavy components), not on a percentage.

Each hidden part is a named entry of AMBIGUITY_SOURCES with the keys it can take mass from
(donors) and give mass to (receivers). How much it can move, as a fraction of each donor
key's minutes, is read from the pairs of independent sources that measure the same thing
(`cross_source_pairs`); with no such pair the fraction is 1 -- no evidence of a small error
is not evidence of a small error. `widen_cell` turns one session's minutes into its lo / hi
variant and `uncertainty` feeds those variants through `aggregate(values=...)` under the same
weights.

A heavy component's verdict, in this order:

* `wait for more sessions` -- its SE is null, or its rank against an adjacent heavy component
  is open under +/-SE alone. More telemetry cannot fix sampling noise, so this outranks
  everything below, a board rank flip included.
* `instrument` -- the rank is settled under +/-SE alone but open once the reconstruction
  interval is added, or a board item linked to the component through its `mechanism` changes
  loss rank between the component's lo and hi minutes. The verdict names the ambiguity source
  whose widening opens the rank most, i.e. what to instrument.
* `no instrumentation needed` -- otherwise.

The overall line is derived from the per-component verdicts by `overall_recommendation`.

The rank-flip test needs a board whose items carry `mechanism` and a ranking function; both
arrive through `Ctx` (`board`, `rank_board`). The CLI wires the real ranking function but no
board yet: the verdict activates once board items carry `mechanism` (stage 4 adds the field).
"""
from __future__ import annotations

import copy
import dataclasses
import math
import statistics
from dataclasses import dataclass

from lib import judge_ledger, time_profile
from lib.time_profile import MODEL_GAP, UNATTRIBUTED, USER_WAIT, WAIT_BACKGROUND, Aggregate, Ctx, aggregate, is_overlay

SPAWN_TOOL_KEY = "tool:Bash:spawn-specialist.py"
JUDGE_HOOK_KEYS = frozenset(f"hook:{basename}" for basename in judge_ledger.HOOK_NAME_BY_BASENAME)
PAIR_NAMES = ("spawn_vs_bash", "judge_vs_hook", "effort_vs_active", "node_vs_engine_span")
ANY_PARTITION_KEY = "*"
_MS_PER_MINUTE = 60_000

# `zero_sum`: the donors' mass moves to the receivers; otherwise the donors only gain (their
# lo stays at the point estimate, their hi rises by the fraction).
AMBIGUITY_SOURCES = {
    "tool_interval_hidden_time": {
        "hides": "Pre/PostToolUse hook time and permission-prompt waits inside a tool interval",
        "donors": ("tool:", "skill:", "mcp:"),
        "receivers": ("hook:", USER_WAIT),
        "pairs": ("spawn_vs_bash", "effort_vs_active"),
        "zero_sum": True,
    },
    "model_gap_unexposed_hooks": {
        "hides": "prompt/stop hook time inside model:gap, for hooks that surface no output",
        "donors": (MODEL_GAP,),
        "receivers": ("hook:",),
        "pairs": ("judge_vs_hook", "effort_vs_active"),
        "zero_sum": True,
    },
    "engine_untimed_events": {
        "hides": "engine events without a timestamp, whose dwell is dropped from node:*",
        "donors": ("node:",),
        "receivers": (),
        "pairs": ("node_vs_engine_span",),
        "zero_sum": False,
    },
    # No independent measurement of the residual exists, so it may belong entirely to any
    # other partition key. The 1.0 is deliberate: it is the conservative bound, not a guess.
    "unattributed": {
        "hides": "the walker's residual, which could belong to any partition key",
        "donors": (UNATTRIBUTED,),
        "receivers": (ANY_PARTITION_KEY,),
        "pairs": (),
        "fraction": 1.0,
        "zero_sum": True,
    },
}

VERDICT_INSTRUMENT = "instrument"
VERDICT_WAIT = "wait for more sessions"
VERDICT_NONE = "no instrumentation needed"
LOWER_BOUND = "lower bound"


def _selected(key: str, selectors) -> bool:
    return any(key == s or (s.endswith(":") and key.startswith(s)) for s in selectors)


def receivers_by_source(universe) -> dict:
    """Per source, the keys of `universe` that can receive the mass its donors give up."""
    result = {}
    for name, source in AMBIGUITY_SOURCES.items():
        if ANY_PARTITION_KEY in source["receivers"]:
            result[name] = [k for k in universe if not is_overlay(k) and not _selected(k, source["donors"])]
        else:
            result[name] = [k for k in universe if _selected(k, source["receivers"])]
    return result


def widen_cell(cell: dict, receivers: dict, fractions: dict) -> tuple:
    """The (lo, hi) variants of one session's per-key ms.

    A zero-sum source takes `fraction` of each donor key's ms: the donor's lo drops by it,
    and every receiver's hi rises by the total taken (less what that receiver itself gave
    up). Any other source only raises each donor's hi by `fraction` of its own ms. Keys no
    source names keep lo == hi == their own ms."""
    lo, hi = dict(cell), dict(cell)
    for name, source in AMBIGUITY_SOURCES.items():
        fraction = fractions.get(name, 0.0)
        takes = {k: ms * fraction for k, ms in cell.items() if _selected(k, source["donors"])}
        if not source["zero_sum"]:
            for key, taken in takes.items():
                hi[key] += taken
            continue
        moved = sum(takes.values())
        if moved <= 0:
            continue
        for key, taken in takes.items():
            lo[key] -= taken
        for key in receivers.get(name, ()):
            gain = moved - takes.get(key, 0.0)
            if gain > 0:
                hi[key] = hi.get(key, 0.0) + gain
    return {k: max(v, 0.0) for k, v in lo.items()}, hi


def _relative(a: float, b: float) -> float | None:
    """|a - b| / max(a, b); 1.0 where only one of them reports time, None where neither does."""
    top = max(a, b)
    return abs(a - b) / top if top > 0 else None


def _pair_stats(values: list) -> dict:
    ordered = sorted(values)
    if not ordered:
        return {"n": 0, "median": None, "p90": None}
    return {
        "n": len(ordered), "median": statistics.median(ordered),
        "p90": ordered[max(0, math.ceil(0.9 * len(ordered)) - 1)],
    }


def _session_pairs(profile, state_dir) -> list:
    """(pair name, a, b) per pairing for one session; a side is None where its source did not
    observe the session at all (no engine state file), which excludes the pairing."""
    part, over = profile.partition, profile.overlays
    spawned = sum(ms for k, ms in over.items() if k.startswith("spawn:"))
    judged = sum(ms for k, ms in over.items() if k.startswith("judge:"))
    hooked = sum(part.get(k, 0) for k in JUDGE_HOOK_KEYS)
    pairs = [("spawn_vs_bash", spawned, part.get(SPAWN_TOOL_KEY, 0)), ("judge_vs_hook", judged, hooked)]
    if state_dir is not None:
        active = (profile.last_ts_ms - profile.first_ts_ms) - part.get(USER_WAIT, 0) - part.get(WAIT_BACKGROUND, 0)
        dwell = sum(ms for k, ms in over.items() if k.startswith("node:"))
        window = (profile.first_ts_ms, profile.last_ts_ms)
        pairs.append(("effort_vs_active", time_profile.engine_effort_active_ms(profile.session_id, state_dir), active))
        pairs.append(("node_vs_engine_span", dwell, time_profile.engine_timed_span_ms(profile.session_id, state_dir, window)))
    return pairs


def cross_source_pairs(top, ctx: Ctx) -> dict:
    """Median and p90 relative discrepancy (|a-b| / max(a,b), per session) of each pairing
    of two independent measurements of one thing. A session where one source reports time
    and the other none counts as 1.0; one where neither does, or where a source never
    observed it, is not in the sample. Joined on structural keys only: the session, the
    spawn / judge / hook key, the engine session."""
    samples = {name: [] for name in PAIR_NAMES}
    for profile in top:
        for name, a, b in _session_pairs(profile, ctx.agentctl_state_dir):
            rel = None if a is None or b is None else _relative(a, b)
            if rel is not None:
                samples[name].append(rel)
    return {name: _pair_stats(values) for name, values in samples.items()}


def source_fraction(source: dict, stats: dict) -> float:
    """The share of a donor key's minutes `source` may move: the worst p90 discrepancy over
    its calibrating pairs that have data; its own `fraction` (default 1.0) where it names no
    pair or none has data."""
    fallback = float(source.get("fraction", 1.0))
    seen = [stats[p]["p90"] for p in source.get("pairs", ()) if stats.get(p, {}).get("p90") is not None]
    return min(1.0, max(seen)) if seen else fallback


def heavy_keys(agg: Aggregate, kind: str) -> list:
    """Keys of `kind` ("partition" | "overlay") whose mean minutes per session exceed the
    `unattributed` row's, in decreasing mean; a shell-variable key is not a mechanism."""
    floor = next((r["minutes_per_session"]["mean"] for r in agg.rows if r["key"] == UNATTRIBUTED), 0.0)
    rows = [r for r in agg.rows
            if r["kind"] == kind and not r.get("flag") and r["key"] != UNATTRIBUTED
            and r["minutes_per_session"]["mean"] > floor]
    rows.sort(key=lambda r: (-r["minutes_per_session"]["mean"], r["key"]))
    return [r["key"] for r in rows]


def overall_recommendation(components: list) -> str:
    """The fixed rule: any `instrument` names those keys; else any `wait` (or no component
    at all, which says nothing yet) waits; else no instrumentation is needed."""
    instrument = [c["key"] for c in components if c["verdict"] == VERDICT_INSTRUMENT]
    if instrument:
        return f"{VERDICT_INSTRUMENT}: " + ", ".join(instrument)
    if not components or any(c["verdict"] == VERDICT_WAIT for c in components):
        return VERDICT_WAIT
    return VERDICT_NONE


def _with_minutes(item, minutes: float):
    if dataclasses.is_dataclass(item):
        return dataclasses.replace(item, minutes_per_occurrence=minutes)
    clone = copy.copy(item)
    clone.minutes_per_occurrence = minutes
    return clone


def _board_ranks(board: dict, rows: dict, rank_board) -> dict:
    """Board ranks with every item that names a `mechanism` re-priced at that mechanism's
    measured minutes when present; an item without a measurement keeps its own."""
    priced = {}
    for ref, item in board.items():
        row = rows.get(getattr(item, "mechanism", None))
        minutes = row["minutes_when_present"]["mean"] if row else 0.0
        priced[ref] = _with_minutes(item, minutes) if minutes > 0 else item
    return rank_board(priced)


def _flipped_mechanisms(ctx: Ctx, lo_rows: dict, hi_rows: dict) -> dict:
    """mechanism key -> refs of its linked items whose board rank differs between the lo and hi minutes."""
    board = ctx.board or {}
    linked = {ref: item for ref, item in board.items() if getattr(item, "mechanism", None)}
    if not linked or ctx.rank_board is None:
        return {}
    at_lo, at_hi = _board_ranks(board, lo_rows, ctx.rank_board), _board_ranks(board, hi_rows, ctx.rank_board)
    flipped: dict = {}
    for ref, item in linked.items():
        if at_lo.get(ref) != at_hi.get(ref):
            flipped.setdefault(item.mechanism, []).append(ref)
    return flipped


def _variant_rows(agg: Aggregate, top, fractions: dict, receivers: dict) -> tuple:
    """The aggregate's rows (key -> row) under the lo and under the hi variant of every session."""
    lo_values, hi_values = {}, {}
    for p in top:
        cell = {k: float(ms) for k, ms in {**p.partition, **p.overlays}.items() if ms > 0}
        lo_values[p.session_id], hi_values[p.session_id] = widen_cell(cell, receivers, fractions)
    rows = []
    for values in (lo_values, hi_values):
        variant = aggregate(top, half_life_days=agg.half_life_days, now_ms=agg.now_ms, values=values)
        rows.append({r["key"]: r for r in variant.rows})
    return tuple(rows)


def _overlap(a: tuple, b: tuple) -> bool:
    return not (a[0] > b[1] or b[0] > a[1])


@dataclass
class UncertaintyReport:
    half_life_days: float
    sessions: int
    components: list
    overall: str
    discrepancies: dict
    coverage: dict
    fractions: dict

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def _share(untimed: int, timed: int):
    return untimed / (untimed + timed) if untimed + timed else None


def _coverage(top) -> dict:
    def total(name: str) -> int:
        return sum(p.coverage.get(name, 0) for p in top)

    return {
        "exposed_hook_minutes": total("exposed_hook_ms") / _MS_PER_MINUTE,
        "exposed_hook_label": LOWER_BOUND,
        "node_untimed_share": _share(total("node_untimed_events"), total("node_timed_events")),
        "transcript_untimed_share": _share(total("untimed_events"), total("timed_events")),
    }


def _component_coverage(key: str, top) -> dict:
    if key.startswith("hook:"):
        return {"label": LOWER_BOUND}
    if key.startswith("node:"):
        untimed = sum(p.coverage.get("node_untimed_events", 0) for p in top)
        timed = sum(p.coverage.get("node_timed_events", 0) for p in top)
        return {"untimed_share": _share(untimed, timed)}
    return {}


@dataclass
class _Frame:
    """The aggregate, its sessions and the lo / hi variant rows every verdict is read from."""

    agg: Aggregate
    top: list
    fractions: dict
    receivers: dict
    both: tuple
    row_of: dict
    solo_cache: dict = dataclasses.field(default_factory=dict)

    def mean(self, key: str) -> float:
        return self.row_of[key]["minutes_per_session"]["mean"]

    def se(self, key: str):
        return self.row_of[key]["minutes_per_session"]["se"]

    def recon(self, key: str, rows: tuple) -> tuple:
        lo, hi = (r[key]["minutes_per_session"]["mean"] if key in r else 0.0 for r in rows)
        return min(lo, self.mean(key)), max(hi, self.mean(key))

    def banded(self, key: str, rows: tuple) -> tuple:
        lo, hi = self.recon(key, rows)
        return lo - self.se(key), hi + self.se(key)

    def sampling(self, key: str) -> tuple:
        return self.mean(key) - self.se(key), self.mean(key) + self.se(key)

    def solo(self, name: str) -> tuple:
        if name not in self.solo_cache:
            self.solo_cache[name] = _variant_rows(self.agg, self.top, {name: self.fractions[name]}, self.receivers)
        return self.solo_cache[name]


def _pick_source(frame: _Frame, key: str, neighbors: list) -> str:
    """The ambiguity source that, widened alone, opens the rank against a neighbor; else the
    one that widens `key` and its neighbors most."""
    scored = []
    for order, name in enumerate(AMBIGUITY_SOURCES):
        rows = frame.solo(name)
        width = sum(hi - lo for lo, hi in (frame.recon(k, rows) for k in [key] + neighbors))
        opened = any(_overlap(frame.banded(key, rows), frame.banded(n, rows)) for n in neighbors)
        scored.append((opened, width, -order, name))
    return max(scored)[3]


def _verdict(frame: _Frame, key: str, neighbors: list, flipped: dict) -> tuple:
    """(verdict, source) of one heavy component against its adjacent heavy components."""
    if any(frame.se(k) is None for k in [key] + neighbors):
        return VERDICT_WAIT, None
    if any(_overlap(frame.sampling(key), frame.sampling(n)) for n in neighbors):
        return VERDICT_WAIT, None
    if key in flipped or any(_overlap(frame.banded(key, frame.both), frame.banded(n, frame.both)) for n in neighbors):
        return VERDICT_INSTRUMENT, _pick_source(frame, key, neighbors)
    return VERDICT_NONE, None


def _component(frame: _Frame, key: str, kind: str, neighbors: list, stats: dict, flipped: dict) -> dict:
    row = frame.row_of[key]
    lo, hi = frame.recon(key, frame.both)
    se = frame.se(key)
    verdict, source = _verdict(frame, key, neighbors, flipped)
    return {
        "key": key, "kind": kind,
        "share_active": row["share_of_active"],
        "per_session": row["minutes_per_session"],
        "interval": [lo, hi],
        "combined": None if se is None else [lo - se, hi + se],
        "discrepancies": {
            pair: stats[pair] for src in AMBIGUITY_SOURCES.values()
            if _selected(key, src["donors"] + src["receivers"]) for pair in src["pairs"]
        },
        "coverage": _component_coverage(key, frame.top),
        "flipped_items": flipped.get(key, []),
        "verdict": verdict, "source": source,
    }


def uncertainty(agg: Aggregate, profiles, ctx: Ctx | None = None) -> UncertaintyReport:
    """Per heavy component of `agg`: sampling SE, reconstruction interval, combined
    interval, cross-source agreement, coverage and a verdict (see the module docstring)."""
    ctx = ctx or Ctx()
    top = [p for p in profiles if p.child_of is None]
    stats = cross_source_pairs(top, ctx)
    fractions = {name: source_fraction(src, stats) for name, src in AMBIGUITY_SOURCES.items()}
    row_of = {r["key"]: r for r in agg.rows}
    receivers = receivers_by_source(sorted(row_of))
    both = _variant_rows(agg, top, fractions, receivers) if top else ({}, {})
    frame = _Frame(agg, top, fractions, receivers, both, row_of)
    flipped = _flipped_mechanisms(ctx, *both)
    components = []
    for kind in ("partition", "overlay"):
        heavy = heavy_keys(agg, kind)
        for i, key in enumerate(heavy):
            neighbors = heavy[max(i - 1, 0):i] + heavy[i + 1:i + 2]
            components.append(_component(frame, key, kind, neighbors, stats, flipped))
    return UncertaintyReport(
        half_life_days=agg.half_life_days, sessions=agg.sessions, components=components,
        overall=overall_recommendation(components), discrepancies=stats, coverage=_coverage(top),
        fractions=fractions,
    )
