"""Per-segment effort accounting: the segment timeline, its armings and their judgment.

A session's effort is booked into segments (planning, each stage by identity, closing),
each worked under one or more armings. An arming's actual is its carried part (adopted
from an earlier session of the order) plus its booked part; `conserve` reads only the
booked part, so a figure adopted across sessions is counted once, by the session that
booked it.

Pure functions over plain dicts: no I/O, standard library only, and every config-derived
value is a parameter. The cli_* modules and the prompt hook load the state, call this
module and save.
"""
from __future__ import annotations

import logging
from collections import Counter

LOG = logging.getLogger(__name__)

PLANNING = "planning"
CLOSING = "closing"
STAGE_PREFIX = "stage:"

SCALE_USD = "usd"
SCALE_MINUTES = "minutes"
SCALES = (SCALE_USD, SCALE_MINUTES)

SOURCE_UNBUDGETED = "unbudgeted"
SOURCE_STRETCH = "unjudged-stretch"
SOURCE_WINDOW = "migrated-window"
SOURCE_USER_APPROVED = "user-approved"
SOURCE_DECLARED = "agent-declared"
SOURCE_CARRIED = "carried"
SOURCE_AGREED = "agreed-estimate"
SOURCE_FALLBACK = "fallback"
SOURCE_FALLBACK_CAPPED = "fallback-capped"

OPENED_BY_MIGRATION = "migration"
TIER_ABOVE_LADDER = "above_large"
CLOCK_SKEW_TOLERANCE_S = 1.0

ACT_PASSING_RECORD_RESULT = "passing-record-result"
ACT_SUBPLAN_POP = "pop-subplan"
ACT_FAILING_RECORD_RESULT = "failing-record-result"
ACT_DISPATCH = "dispatch"
ACT_AGENT_SKIP = "agent-skip"
ACT_APPROVE_BY_AGENT = "approve-by-agent"
ACT_REPLAN = "replan"
ACT_SUBMIT_PLAN = "submit-plan"
ACT_PLAN_REVIEW = "plan-review"
ACT_PRESENT_PLAN = "present-plan"
ACT_STAMP = "prompt-stamp"
ACT_DESCENDANT_RECORD_RESULT = "descendant-record-result"
ACT_DESCENDANT_DISPATCH = "descendant-dispatch"
ACT_DESCENDANT_STAMP = "descendant-stamp"
ACT_SPAWNED_PLANNER_ACT = "spawned-planner-act"
ACT_SPAWNED_PLANNER_RUN_END = "spawned-planner-run-end"
ACT_APPROVE_BY_USER = "approve-by-user"
ACT_FIRE_ACK_BY_USER = "fire-acknowledge-by-user"
ACT_ORDER_AGREE = "order-agree"

LIVE_STAGE_JUDGING_ACTS = (
    ACT_FAILING_RECORD_RESULT,
    ACT_DISPATCH,
    ACT_AGENT_SKIP,
    ACT_APPROVE_BY_AGENT,
    ACT_REPLAN,
    ACT_SUBMIT_PLAN,
    ACT_PLAN_REVIEW,
    ACT_PRESENT_PLAN,
    ACT_STAMP,
    ACT_DESCENDANT_RECORD_RESULT,
    ACT_DESCENDANT_DISPATCH,
    ACT_DESCENDANT_STAMP,
)
PLANNING_JUDGING_ACTS = (
    ACT_SUBMIT_PLAN,
    ACT_PLAN_REVIEW,
    ACT_PRESENT_PLAN,
    ACT_APPROVE_BY_AGENT,
    ACT_STAMP,
    ACT_SPAWNED_PLANNER_ACT,
    ACT_SPAWNED_PLANNER_RUN_END,
)
CLOSING_REPLANS_JUDGING_ACTS = (
    ACT_SUBMIT_PLAN,
    ACT_PLAN_REVIEW,
    ACT_PRESENT_PLAN,
    ACT_APPROVE_BY_AGENT,
)


def judging_acts_of(segment: str) -> tuple:
    if segment == PLANNING:
        return PLANNING_JUDGING_ACTS
    if segment == CLOSING:
        return ()
    return LIVE_STAGE_JUDGING_ACTS


def replans_judged_at(segment: str, act: str) -> bool:
    """Whether `act` raises a replans-scale fire owed in `segment`. The replan verb never does: the
    replan ceiling bounds that act itself, so the fire falls at the next act of its review cycle.
    In closing only that cycle judges, and a spawned planner's acts never do."""
    if act == ACT_REPLAN:
        return False
    if segment == CLOSING:
        return act in CLOSING_REPLANS_JUDGING_ACTS
    return act in LIVE_STAGE_JUDGING_ACTS or act in CLOSING_REPLANS_JUDGING_ACTS or act == ACT_STAMP


def stage_segment(uid: str) -> str:
    return f"{STAGE_PREFIX}{uid}"


def _amounts(usd: float = 0.0, minutes: float = 0.0) -> dict:
    return {SCALE_USD: float(usd), SCALE_MINUTES: float(minutes)}


def arming_actual(arming: dict) -> dict:
    """What the arming is judged on: its carried part plus its booked part."""
    return {
        scale: float(arming["carried"][scale]) + float(arming["booked"][scale])
        for scale in SCALES
    }


def open_arming(ledger: dict, segment: str, budget: dict | None, budget_source: str,
                at: float, by: str, spanned: bool = True) -> dict:
    """Open an arming. Its first span starts at `at` unless `spanned` is False: an adopted
    arming holds no span until `resume_arming` starts one at the handoff."""
    seq = int(ledger.get("seq") or 0) + 1
    ledger["seq"] = seq
    arming = {
        "arming_id": f"a{seq}",
        "seq": seq,
        "segment": segment,
        "opened_at": float(at),
        "opened_by": by,
        "budget": dict(budget) if budget else None,
        "budget_source": budget_source,
        "review_terms": None,
        "carried": _amounts(),
        "booked": _amounts(),
        "fire": None,
        "closed_at": None,
        "spans": [_span(at)] if spanned else [],
    }
    ledger.setdefault("armings", []).append(arming)
    return arming


def _span(start: float) -> dict:
    return {"start": float(start), "end": None, "end_act": None}


def spans_of(arming: dict) -> list[dict]:
    """The arming's spans: append-only, so a park and a resume lose no history. An arming
    saved before spans existed reads as one span from its opening to its closing."""
    spans = arming.get("spans")
    if spans is None:
        spans = arming["spans"] = [{"start": arming["opened_at"], "end": arming["closed_at"],
                                    "end_act": None}]
    return spans


def open_span_of(arming: dict) -> dict | None:
    spans = spans_of(arming)
    return spans[-1] if spans and spans[-1]["end"] is None else None


def park_arming(ledger: dict, arming_id: str, at: float) -> None:
    """End the arming's open span: it owns no booking written from `at` until it resumes."""
    span = open_span_of(_get(ledger, arming_id))
    if span is not None:
        span["end"] = float(at)
        span["end_act"] = "parked"


def resume_arming(ledger: dict, arming_id: str, at: float) -> dict:
    """Start a new span at `at`, unless the arming already holds an open one."""
    arming = _get(ledger, arming_id)
    if open_span_of(arming) is None:
        spans_of(arming).append(_span(at))
    return arming


def _get(ledger: dict, arming_id: str) -> dict:
    arming = find_arming(ledger, arming_id)
    if arming is None:
        raise KeyError(arming_id)
    return arming


def find_arming(ledger: dict, arming_id: str) -> dict | None:
    for arming in ledger.get("armings") or ():
        if arming["arming_id"] == arming_id:
            return arming
    return None


def open_arming_of(ledger: dict, segment: str) -> dict | None:
    """The segment's open arming, the latest when more than one is open."""
    found = None
    for arming in ledger.get("armings") or ():
        if arming["segment"] == segment and arming["closed_at"] is None:
            found = arming
    return found


def window_arming(ledger: dict) -> dict | None:
    """The migrated session's open window arming: it owns every booking until the switch."""
    for arming in ledger.get("armings") or ():
        if arming["budget_source"] == SOURCE_WINDOW and arming["closed_at"] is None:
            return arming
    return None


def current_arming(ledger: dict) -> dict | None:
    """The open arming holding the latest-started open span: the segment an act happening now
    belongs to. A parked arming, and an adopted one not yet resumed, is never current."""
    held = [(open_span_of(a)["start"], a["seq"], a) for a in ledger.get("armings") or []
            if a["closed_at"] is None and open_span_of(a) is not None]
    return max(held, key=lambda h: h[:2])[2] if held else None


def add_review_term(arming: dict, term: dict) -> None:
    """Join one review's term to the arming's budget; `review_terms` keeps their sum apart."""
    terms = arming["review_terms"] or {}
    for scale in SCALES:
        arming["budget"][scale] = arming["budget"].get(scale, 0.0) + term[scale]
        terms[scale] = terms.get(scale, 0.0) + term[scale]
    arming["review_terms"] = terms


def remove_review_term(arming: dict, term: dict) -> None:
    terms = arming["review_terms"]
    for scale in SCALES:
        arming["budget"][scale] -= term[scale]
        terms[scale] -= term[scale]


def _crossed_multiple(arming: dict, multiple: float) -> list[str]:
    budget = arming.get("budget")
    if not budget or arming["budget_source"] == SOURCE_WINDOW:
        return []
    actual = arming_actual(arming)
    return [
        scale for scale in SCALES
        if (budget.get(scale) or 0) > 0 and actual[scale] >= multiple * budget[scale]
    ]


def _observed_tiers(arming: dict, actual: dict) -> dict | None:
    ladder = arming.get("tier_ladder")
    if not ladder:
        return None
    return {
        scale: next((tier for tier, estimate in ladder if estimate[scale] >= actual[scale]),
                    TIER_ABOVE_LADDER)
        for scale in SCALES
    }


def _retier_record(arming: dict, crossed: list[str], multiple: float, act: str, at: float) -> dict:
    actual = arming_actual(arming)
    return {
        "arming_id": arming["arming_id"],
        "segment": arming["segment"],
        "title": arming.get("title"),
        "declared_tier": arming.get("declared_tier"),
        "observed_tier": _observed_tiers(arming, actual),
        "budget_source": arming["budget_source"],
        "budget": dict(arming["budget"]),
        "actual": actual,
        "multiple": multiple,
        "scales": crossed,
        "act": act,
        "at": at,
    }


def close_arming(ledger: dict, arming_id: str, act: str, at: float, multiple: float) -> dict | None:
    """Close the arming; returns its retier record when its actual reached `multiple` times
    its budget on either scale (never for an unbudgeted or migrated-window arming)."""
    arming = _get(ledger, arming_id)
    span = open_span_of(arming)
    if span is not None:
        span["end"] = float(at)
        span["end_act"] = "closed"
    arming["closed_at"] = float(at)
    arming["closed_by"] = act
    crossed = _crossed_multiple(arming, multiple)
    if not crossed:
        return None
    record = _retier_record(arming, crossed, multiple, act, at)
    ledger.setdefault("retier", []).append(record)
    return record


def retier_after_late_booking(ledger: dict, arming: dict, multiple: float) -> dict | None:
    """A row booked after `arming` closed: update its retier record, or create one when the
    row lifted the arming to the multiple. Never fires; a window arming writes none."""
    if arming["closed_at"] is None:
        return None
    records = ledger.setdefault("retier", [])
    existing = next((r for r in records if r["arming_id"] == arming["arming_id"]), None)
    crossed = _crossed_multiple(arming, multiple)
    if not crossed:
        return None
    act = existing["act"] if existing else arming.get("closed_by")
    at = existing["at"] if existing else arming["closed_at"]
    record = _retier_record(arming, crossed, multiple, act, at)
    if existing:
        existing.update(record)
        return existing
    records.append(record)
    return record


def _owner_at(ledger: dict, ts: float) -> dict | None:
    """The arming whose span covers `ts`: the innermost one, the span that started latest (the
    later-opened arming on a tie), an open span covering every later time. After the last span
    ended, the arming that ended last owns the trailing time. None inside a gap or before
    the first span."""
    armings = ledger.get("armings") or []
    window = window_arming(ledger)
    if window is not None:
        return window
    covering = [(span["start"], a["seq"], a) for a in armings for span in spans_of(a)
                if span["start"] <= ts and (span["end"] is None or ts < span["end"])]
    if covering:
        return max(covering, key=lambda c: c[:2])[2]
    if any(open_span_of(a) is not None for a in armings):
        return None
    ended = [(span["end"], a["seq"], a) for a in armings for span in spans_of(a)]
    if ended and ts >= max(e[0] for e in ended):
        return max(ended, key=lambda e: e[:2])[2]
    return None


def _nearest_arming(ledger: dict, ts: float) -> dict:
    """The arming with a span closest to `ts`, the later-opened one on a tie."""
    def gap(span):
        end = span["end"] if span["end"] is not None else ts
        return max(span["start"] - ts, ts - end, 0.0)
    return min(((gap(span), -a["seq"], a) for a in ledger["armings"] for span in spans_of(a)),
               key=lambda c: c[:2])[2]


def book(ledger: dict, written_ts: float, usd: float, minutes: float, origin: str) -> dict | None:
    """Add a booking to the arming whose span covers its write time and return that arming, a
    closed one included: a row written while it was current and ingested later still belongs to
    it. A booking older than the ledger's session_floor is not booked. From the floor on every
    write time has an owner; an uncovered one is a handoff the engine failed to make, booked to
    the nearest arming under a logged warning so the spend is not lost. A ledger
    holding no session_floor takes no such bound and books nothing before its first span.

    Contract change from stage 3, which asserted that every write time at or after the floor is
    covered: `book` runs on the stamp and refresh paths of every user act, and an assertion
    there would block the user's act over an accounting gap. The gap is logged and the spend
    kept in the ledger, near where it was written, instead of being lost or crashing the act.
    One engine path that can leave such a gap: a settling act whose stage held the interval
    while no stage is ready yet and some are unsettled (a pending stage waiting on a supplier
    still running); the interval stays uncovered until the next stage is armed."""
    floor = ledger.get("session_floor")
    if floor is not None and written_ts < floor:
        return None
    arming = _owner_at(ledger, written_ts)
    if arming is None:
        if floor is None or not ledger.get("armings"):
            return None
        arming = _nearest_arming(ledger, written_ts)
        LOG.warning("write time %s at or after session_floor %s is covered by no span; booked to "
                    "the nearest arming %s", written_ts, floor, arming["arming_id"])
    arming["booked"][SCALE_USD] += float(usd)
    arming["booked"][SCALE_MINUTES] += float(minutes)
    origins = arming.setdefault("origins", {})
    origins[origin] = origins.get(origin, 0) + 1
    return arming


def book_interval(ledger: dict, start: float, end: float, minutes: float, origin: str) -> list[dict]:
    """Book `minutes` of the interval to the armings it crosses, in proportion to the time
    spent in each. A boundary within the clock-skew tolerance of an end does not split."""
    if minutes <= 0:
        return []
    if end <= start:
        arming = book(ledger, end, 0.0, minutes, origin)
        return [arming] if arming else []
    edges = {
        edge for arming in ledger.get("armings") or () for span in spans_of(arming)
        for edge in (span["start"], span["end"])
        if edge is not None
        and start + CLOCK_SKEW_TOLERANCE_S < edge < end - CLOCK_SKEW_TOLERANCE_S
    }
    floor = ledger.get("session_floor")
    if floor is not None and start + CLOCK_SKEW_TOLERANCE_S < floor < end - CLOCK_SKEW_TOLERANCE_S:
        edges.add(floor)
    points = [start, *sorted(edges), end]
    booked = []
    for lo, hi in zip(points, points[1:]):
        arming = book(ledger, (lo + hi) / 2.0, 0.0, minutes * (hi - lo) / (end - start), origin)
        if arming is not None:
            booked.append(arming)
    return booked


def conserve(ledger: dict) -> dict:
    """Per-segment and total booked actuals. The carried part is left out: it was booked,
    and is counted, by the session that adopted it from."""
    segments: dict[str, dict] = {}
    total = _amounts()
    for arming in ledger.get("armings") or ():
        into = segments.setdefault(arming["segment"], _amounts())
        for scale in SCALES:
            into[scale] += arming["booked"][scale]
            total[scale] += arming["booked"][scale]
    return {"segments": segments, "total": total}


def timeline(ledger: dict) -> list[dict]:
    """The segments in order, planning first and closing last, stages by first opening,
    each with its armings in opening order."""
    order: dict[str, list[dict]] = {}
    for arming in sorted(ledger.get("armings") or (), key=lambda a: a["seq"]):
        order.setdefault(arming["segment"], []).append(arming)
    rank = {PLANNING: 0, CLOSING: 2}
    keys = sorted(order, key=lambda s: rank.get(s, 1))
    return [{"segment": segment, "armings": order[segment]} for segment in keys]


def _freeze(value):
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    return value


def _identity_key(record: dict, supplier_uid: dict) -> tuple:
    edges = sorted(
        (str(supplier_uid[edge[0]]), *(str(part) for part in edge[1:]))
        for edge in record.get("edges") or ()
    )
    return (_freeze(record["key"]), tuple(edges))


def match_identities(prev: list[dict], new: list[dict],
                     pinned: dict[int, str] | None = None) -> dict[int, str | None]:
    """Each new stage's prior identity, by new index; None means a fresh identity.

    A record is {uid (prev only), index, key, edges: [(supplier_index, *edge_rest)]}: `key` is
    the stage's definition without its edges. Suppliers resolve first, so the identity key
    compared holds supplier identities, never indices: a stage survives reindexing, and a
    stage whose supplier got a fresh identity, or whose key two prior or two new stages share,
    gets a fresh one. `pinned` ({new index: uid}) fixes a stage's identity before its consumers
    resolve, so they keep theirs; a pinned uid is no other stage's match."""
    pinned = pinned or {}
    prev_uid = {r["index"]: r["uid"] for r in prev}
    prev_counts: Counter = Counter()
    prev_by_key: dict[tuple, str] = {}
    for record in prev:
        if all(edge[0] in prev_uid for edge in record.get("edges") or ()):
            key = _identity_key(record, prev_uid)
            prev_counts[key] += 1
            prev_by_key[key] = record["uid"]

    resolved: dict[int, str | None] = {}
    tokens: dict[int, str] = {}
    pending = list(new)
    while pending:
        ready = [
            r for r in pending
            if all(edge[0] in tokens for edge in r.get("edges") or ())
        ]
        if not ready:
            break
        keys = {r["index"]: _identity_key(r, tokens) for r in ready}
        new_counts = Counter(keys.values())
        for record in ready:
            key = keys[record["index"]]
            if record["index"] in pinned:
                uid = pinned[record["index"]]
            else:
                uid = prev_by_key.get(key) if prev_counts[key] == 1 and new_counts[key] == 1 else None
                if uid in pinned.values():
                    uid = None
            resolved[record["index"]] = uid
            tokens[record["index"]] = uid if uid is not None else f"fresh:{record['index']}"
        pending = [r for r in pending if r["index"] not in resolved]
    for record in pending:
        resolved[record["index"]] = None
    return resolved


def judge(arming: dict, multiple: float, floor: dict | None) -> list[str]:
    """The scales whose fire point is reached: actual at least `multiple` times the budget AND
    excess over the budget at least the floor. A scale with no floor needs the multiple only."""
    budget = arming.get("budget")
    if not budget:
        return []
    actual = arming_actual(arming)
    crossed = []
    for scale in SCALES:
        limit = budget.get(scale)
        if not limit or limit <= 0 or actual[scale] < multiple * limit:
            continue
        minimum = (floor or {}).get(scale)
        if minimum is not None and actual[scale] - limit < minimum:
            continue
        crossed.append(scale)
    return crossed


def unfired_scales(arming: dict, crossed: list[str]) -> list[str]:
    """`crossed` less what the arming's fire already names: an arming fires once per scale."""
    fired = (arming.get("fire") or {}).get("scales") or ()
    return [scale for scale in crossed if scale not in fired]


def scales_to_fire(arming: dict, act: str, multiple: float, floor: dict | None) -> list[str]:
    """What `act` fires on `arming`: nothing outside its segment's judging acts or on a migrated
    window, else the scales crossed and not yet fired."""
    if act not in judging_acts_of(arming["segment"]) or arming["budget_source"] == SOURCE_WINDOW:
        return []
    return unfired_scales(arming, judge(arming, multiple, floor))


def note_fire(arming: dict, scales: list[str], at: float) -> None:
    fire = arming["fire"] or {"scales": [], "ack": None, "ts": at}
    fire["scales"].extend(scale for scale in scales if scale not in fire["scales"])
    arming["fire"] = fire
