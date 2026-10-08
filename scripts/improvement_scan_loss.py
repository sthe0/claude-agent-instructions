"""Measured-loss ranking for improvement-scan backlog items — the pure half.

Difficulty removed: the backlog triage rubric ranked items by breadth x severity /
fix cost, which measured against this machine's own transcripts is nearly
uncorrelated with the minutes the problems actually cost, and it dropped every
item without a severity label from the ranking altogether. This module supplies
the rank that answers "what costs us most": the number of distinct sessions whose
transcripts show an item's observable signature, scaled by a model-judged
precision, times the minutes lost per occurrence.

Rule/perception split: the model supplies the signature strings, the minutes per
occurrence, the precision and the family grouping; this module only counts and
sorts. A signature match is a lexical CANDIDATE filter — the precision factor,
judged by a model on sampled excerpts, is what turns a count into an estimate.

No subprocess, no network: file reads (transcripts, the hit cache) and pure
arithmetic only, asserted by scripts/tests/test_improvement_scan.py.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from difficulty_channel.port import Severity
from lib import config_root
from lib.transcript_cost import iter_jsonl, parse_ts_or_none

DEFAULT_WINDOW_DAYS = 14
DEFAULT_SAMPLE_SIZE = 10
EXCERPT_CHARS = 400

# A tool_result is discussion or filing, not an occurrence, when the command that produced
# it read or filed an issue. A candidate filter, never a semantic judgment (see module doc).
FILING_COMMAND_MARKERS = ("gh issue", "file-difficulty.py", "improvement-scan.py")

# Entry types whose own text counts as an observation of a signature. Assistant text,
# thinking, tool_use inputs and user-typed text never count: they are the agent or the
# user TALKING about a problem, not the problem happening.
_SYSTEM_TEXT_KEYS = ("content", "hookInfos", "hookErrors", "output", "stdout", "stderr")

_FIX_COST_RANK = {"small": 0, "medium": 1, "large": 2}
_UNKNOWN_FIX_COST_RANK = 3


# --- signatures --------------------------------------------------------------

def signatures_digest(signatures: Iterable[str]) -> str:
    """Digest of a signature SET (order- and duplicate-insensitive). A precision judged on
    one digest is stale under any other."""
    unique = sorted(set(signatures))
    return hashlib.sha256("\0".join(unique).encode("utf-8")).hexdigest()[:16]


def issue_number(ref: str) -> str:
    """The trailing number of a board ref (`owner/repo#305`, `ORG-123`), or ''."""
    end = len(ref)
    start = end
    while start > 0 and ref[start - 1].isdigit():
        start -= 1
    return ref[start:end]


def scan_key(signature: str, issue_no: str) -> str:
    """Hit-cache key of one signature. The issue number is part of it because the
    discussion exclusion is item-specific."""
    return hashlib.sha256(f"{signature}\0{issue_no}".encode("utf-8")).hexdigest()[:16]


# --- transcript enumeration and attribution ----------------------------------

def enumerate_transcripts(roots: "Sequence[Path] | None" = None) -> "list[Path]":
    """Every transcript, subagents included. `roots` is an injectable list of `projects`
    directories; None means every root `lib.config_root.projects_roots()` knows."""
    if roots is None:
        return config_root.iter_transcripts(pattern="**/*.jsonl")
    out: "list[Path]" = []
    for root in roots:
        out.extend(Path(root).glob("**/*.jsonl"))
    return sorted(out)


def session_id_of(path: Path) -> str:
    """`<project>/<sid>.jsonl` -> sid; `<project>/<sid>/subagents/<x>.jsonl` -> sid."""
    parts = path.parts
    if "subagents" in parts:
        idx = len(parts) - 1 - parts[::-1].index("subagents")
        if idx > 0:
            return parts[idx - 1]
    return path.stem


def window_bounds(days: int, until: "datetime | None" = None) -> "tuple[datetime, datetime]":
    end = until or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return end - timedelta(days=days), end


# --- scanning one transcript -------------------------------------------------

def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _tool_use_inputs(entry: dict) -> "dict[str, str]":
    content = (entry.get("message") or {}).get("content")
    out: "dict[str, str]" = {}
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id"):
                out[block["id"]] = "\n".join(_strings(block.get("input")))
    return out


def _counted_texts(entry: dict) -> "list[tuple[str, str | None]]":
    """(text, paired tool_use id) for every surface of this entry that counts."""
    kind = entry.get("type")
    if kind == "user":
        content = (entry.get("message") or {}).get("content")
        if not isinstance(content, list):
            return []
        return [
            (text, block.get("tool_use_id"))
            for block in content
            if isinstance(block, dict) and block.get("type") == "tool_result"
            for text in _strings(block.get("content"))
        ]
    if kind == "system":
        return [(t, None) for key in _SYSTEM_TEXT_KEYS for t in _strings(entry.get(key))]
    if kind == "attachment":
        return [(t, None) for t in _strings(entry.get("attachment"))]
    return []


def is_discussion(signature: str, issue_no: str, tool_input: str) -> bool:
    """True when the tool call that produced a result was itself discussing or filing:
    its input carries the signature, the issue number (`#N` / `issues/N`), or an
    issue-read/filing command."""
    if not tool_input:
        return False
    if signature in tool_input:
        return True
    if issue_no and any(
        _names_number(tool_input, prefix, issue_no) for prefix in ("#", "issues/")
    ):
        return True
    return any(marker in tool_input for marker in FILING_COMMAND_MARKERS)


def _names_number(text: str, prefix: str, number: str) -> bool:
    """`prefix + number` occurs in `text` and is not the start of a longer number."""
    token = prefix + number
    start = text.find(token)
    while start != -1:
        after = start + len(token)
        if after >= len(text) or not (text[after].isalnum() or text[after] == "_"):
            return True
        start = text.find(token, start + 1)
    return False


def _excerpt(text: str, signature: str) -> str:
    idx = text.find(signature)
    half = max(0, (EXCERPT_CHARS - len(signature)) // 2)
    start = max(0, idx - half)
    return text[start: idx + len(signature) + half][:EXCERPT_CHARS]


def scan_transcript(
    path: Path, wanted: "Mapping[str, tuple[str, str]]"
) -> "dict[str, list[tuple[str, str]]]":
    """`wanted` maps scan_key -> (signature, issue_no). Returns scan_key -> [(iso timestamp,
    excerpt)] for every surviving counted hit. An entry without a parseable timestamp cannot
    be placed in a window and is skipped."""
    hits: "dict[str, list[tuple[str, str]]]" = {k: [] for k in wanted}
    tool_inputs: "dict[str, str]" = {}
    for entry in iter_jsonl(path):
        tool_inputs.update(_tool_use_inputs(entry))
        ts = parse_ts_or_none(entry.get("timestamp"))
        if ts is None:
            continue
        for text, tool_use_id in _counted_texts(entry):
            tool_input = tool_inputs.get(tool_use_id, "") if tool_use_id else ""
            for key, (signature, issue_no) in wanted.items():
                if signature in text and not is_discussion(signature, issue_no, tool_input):
                    hits[key].append((ts.isoformat(), _excerpt(text, signature)))
    return hits


# --- hit cache -----------------------------------------------------------------

def load_hit_cache(path: "str | Path") -> "dict[str, dict]":
    """{transcript path: {mtime, size, scanned: {scan_key: [iso timestamps]}}}. A corrupt
    or absent cache is an empty one — a full rescan, never a crash."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    cache: "dict[str, dict]" = {}
    for transcript, entry in raw.items():
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("mtime"), (int, float))
            and isinstance(entry.get("size"), int)
            and isinstance(entry.get("scanned"), dict)
        ):
            cache[str(transcript)] = entry
    return cache


def save_hit_cache(cache: "dict[str, dict]", path: "str | Path") -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def refresh_hit_cache(
    cache: "dict[str, dict]",
    transcripts: "Sequence[Path]",
    wanted: "Mapping[str, tuple[str, str]]",
    *,
    since: "datetime | None" = None,
) -> int:
    """Bring `cache` up to date for `wanted`; returns how many transcripts were opened.

    A transcript is rescanned only when it is new, its mtime or size changed, or it lacks
    a scan for a wanted key (then only those keys are scanned). A transcript last modified
    before `since` cannot hold an entry inside the window and is skipped."""
    opened = 0
    for path in transcripts:
        try:
            st = path.stat()
        except OSError:
            continue
        if since is not None and st.st_mtime < since.timestamp():
            continue
        entry = cache.get(str(path))
        if entry is None or entry["mtime"] != st.st_mtime or entry["size"] != st.st_size:
            entry = {"mtime": st.st_mtime, "size": st.st_size, "scanned": {}}
        missing = {k: v for k, v in wanted.items() if k not in entry["scanned"]}
        if missing:
            found = scan_transcript(path, missing)
            for key in missing:
                entry["scanned"][key] = [ts for ts, _excerpt_text in found[key]]
            opened += 1
        cache[str(path)] = entry
    for stale in [p for p in cache if not os.path.exists(p)]:
        del cache[stale]
    return opened


def sessions_with_hits(
    cache: "Mapping[str, dict]",
    transcripts: "Sequence[Path]",
    keys: "Iterable[str]",
    *,
    since: datetime,
    until: datetime,
) -> "set[str]":
    """Distinct sessions with at least one cached hit for any of `keys` inside the window."""
    wanted = list(keys)
    sessions: "set[str]" = set()
    for path in transcripts:
        entry = cache.get(str(path))
        if entry is None:
            continue
        for key in wanted:
            for iso in entry["scanned"].get(key, ()):
                ts = parse_ts_or_none(iso)
                if ts is not None and since <= ts <= until:
                    sessions.add(session_id_of(path))
                    break
    return sessions


def item_scan_keys(signatures: Iterable[str], issue_no: str) -> "dict[str, tuple[str, str]]":
    return {scan_key(s, issue_no): (s, issue_no) for s in signatures}


def count_sessions(
    specs: "Mapping[str, tuple[Sequence[str], str]]",
    transcripts: "Sequence[Path]",
    cache: "dict[str, dict]",
    *,
    since: datetime,
    until: datetime,
) -> "tuple[dict[str, set[str]], int]":
    """specs: item ref -> (signatures, issue number). Returns (ref -> hit sessions, number of
    transcripts opened). Updates `cache` in place; persisting it is the caller's choice."""
    wanted: "dict[str, tuple[str, str]]" = {}
    per_item = {ref: item_scan_keys(sigs, issue_no) for ref, (sigs, issue_no) in specs.items()}
    for keys in per_item.values():
        wanted.update(keys)
    opened = refresh_hit_cache(cache, transcripts, wanted, since=since) if wanted else 0
    result = {
        ref: sessions_with_hits(cache, transcripts, keys, since=since, until=until)
        for ref, keys in per_item.items()
    }
    return result, opened


def build_samples(
    specs: "Mapping[str, tuple[Sequence[str], str]]",
    transcripts: "Sequence[Path]",
    cache: "Mapping[str, dict]",
    *,
    since: datetime,
    until: datetime,
    sample_size: int,
) -> "dict[str, list[dict]]":
    """Deterministic, seeded excerpts for the model to judge: at most one hit per session,
    ordered by a hash of (item, signature set, session, timestamp), first `sample_size`.
    Only transcripts the cache already knows hold a hit are reopened for their excerpts."""
    out: "dict[str, list[dict]]" = {}
    for ref, (signatures, issue_no) in specs.items():
        keys = item_scan_keys(signatures, issue_no)
        digest = signatures_digest(signatures)
        by_session: "dict[str, tuple[str, str]]" = {}
        for path in transcripts:
            entry = cache.get(str(path))
            if entry is None or not any(entry["scanned"].get(k) for k in keys):
                continue
            for _key, found in scan_transcript(path, keys).items():
                for iso, excerpt in found:
                    ts = parse_ts_or_none(iso)
                    if ts is None or not (since <= ts <= until):
                        continue
                    sid = session_id_of(path)
                    if sid not in by_session or iso < by_session[sid][0]:
                        by_session[sid] = (iso, excerpt)
        ordered = sorted(
            by_session.items(),
            key=lambda kv: hashlib.sha256(
                f"{ref}\0{digest}\0{kv[0]}\0{kv[1][0]}".encode("utf-8")
            ).hexdigest(),
        )
        out[ref] = [
            {"session": sid, "timestamp": iso, "excerpt": excerpt}
            for sid, (iso, excerpt) in ordered[:sample_size]
        ]
    return out


# --- loss arithmetic ---------------------------------------------------------

def effective_precision(item: Any) -> "tuple[float, bool]":
    """(precision, judged). A missing precision, or one judged on a different signature
    set than the item carries now, is counted at 1.0 — an upper bound, not a guess."""
    precision = getattr(item, "precision", None)
    signatures = tuple(getattr(item, "signatures", ()) or ())
    if precision is None or getattr(item, "signatures_digest", "") != signatures_digest(signatures):
        return 1.0, False
    return float(precision), True


@dataclass(frozen=True)
class Member:
    ref: str
    sessions: "frozenset[str]"
    precision: float
    minutes: float


@dataclass(frozen=True)
class LossFigures:
    sessions_hit: int
    sessions_est: float
    min_per_occurrence: float
    min_per_week: float


def family_loss(members: "Sequence[Member]", window_days: int) -> LossFigures:
    """Loss of one item or family over the UNION of its members' hit sessions.

    A shared session is charged once, at its costliest member (a session hit only by a cheap
    member is charged that member's cost). Hence the family total is at most the sum of its
    members' separate losses and at least its costliest member's; for a singleton it reduces
    to hits x p x minutes."""
    union: "set[str]" = set().union(*(m.sessions for m in members)) if members else set()
    cost_total = 0.0
    sessions_est = 0.0
    for sid in union:
        hitters = [m for m in members if sid in m.sessions]
        cost_total += max(m.precision * m.minutes for m in hitters)
        miss = 1.0
        for m in hitters:
            miss *= 1.0 - m.precision
        sessions_est += 1.0 - miss
    per_occurrence = (
        cost_total / sessions_est if sessions_est > 0 else max((m.minutes for m in members), default=0.0)
    )
    return LossFigures(
        sessions_hit=len(union),
        sessions_est=sessions_est,
        min_per_occurrence=per_occurrence,
        min_per_week=cost_total / (window_days / 7),
    )


def measurement_for(
    item: Any, sessions: "Iterable[str]", *, window_days: int, until: datetime, now: datetime
) -> dict:
    """The `loss_measurement` board field of one item. `sessions` is stored so a family's
    union can be recomputed from the board alone; `min_per_week` is informational (the lane
    builder recomputes it from the current precision and minutes)."""
    session_list = sorted(set(sessions))
    precision, _judged = effective_precision(item)
    minutes = getattr(item, "minutes_per_occurrence", None)
    return {
        "window_days": window_days,
        "until": until.isoformat(),
        "sessions_hit": len(session_list),
        "sessions_est": len(session_list) * precision,
        "min_per_week": (
            len(session_list) * precision * minutes / (window_days / 7) if minutes else None
        ),
        "measured_at": now.isoformat(),
        "signatures_digest": signatures_digest(getattr(item, "signatures", ()) or ()),
        "sessions": session_list,
    }


# --- ordering ------------------------------------------------------------------

def constrained_order(
    refs: "Iterable[str]",
    blocked_by: "Mapping[str, Iterable[str]]",
    key: "Callable[[str], Any]",
) -> "list[str]":
    """Order `refs` by `key` ascending, with a HARD partial order from `blocked_by` edges: a
    blocked ref never precedes its blocker. A blocker absent from `refs` imposes no
    constraint. A cycle among the remaining refs stops being enforced for just those."""
    remaining = sorted(refs, key=key)
    result: "list[str]" = []
    guard = len(remaining) + 1
    while remaining and guard:
        guard -= 1
        ready = [
            r for r in remaining
            if all(b not in remaining or b in result for b in blocked_by.get(r, ()))
        ]
        if not ready:
            ready = remaining
        pick = ready[0]
        result.append(pick)
        remaining.remove(pick)
    return result


def _severity_mass(item: Any) -> int:
    try:
        return Severity.parse(getattr(item, "severity", "") or "").mass
    except ValueError:
        return 0


def _tie_break(item: Any, ref: str) -> tuple:
    """severity mass (labelled before unlabelled, higher first), then fix-cost tier ascending,
    then the old rubric score descending, then ref."""
    labelled = bool(getattr(item, "severity_labeled", False))
    old = getattr(item, "old_score", None)
    if old is None:
        old = getattr(item, "score", None)
    return (
        0 if labelled else 1,
        -_severity_mass(item) if labelled else 0,
        _FIX_COST_RANK.get(getattr(item, "cost_to_resolve", ""), _UNKNOWN_FIX_COST_RANK),
        -(old or 0.0),
        ref,
    )


# --- lanes ---------------------------------------------------------------------

def _measurement_current(item: Any) -> bool:
    m = getattr(item, "loss_measurement", None)
    if not isinstance(m, dict):
        return False
    digest = m.get("signatures_digest")
    return digest is None or digest == signatures_digest(getattr(item, "signatures", ()) or ())


def _loss_ready(item: Any) -> bool:
    minutes = getattr(item, "minutes_per_occurrence", None)
    return bool(getattr(item, "signatures", ())) and bool(minutes) and _measurement_current(item)


def _silent_ready(item: Any) -> bool:
    return not getattr(item, "signatures", ()) and bool((getattr(item, "silent_estimate", "") or "").strip())


def _member_sessions(ref: str, item: Any) -> "frozenset[str]":
    m = item.loss_measurement
    sessions = m.get("sessions")
    if isinstance(sessions, list):
        return frozenset(str(s) for s in sessions)
    return frozenset(f"{ref}#{i}" for i in range(int(m.get("sessions_hit") or 0)))


def _basis(
    members: "Sequence[tuple[str, Any]]", figures: LossFigures, window_days: int, uncounted: int
) -> str:
    if len(members) == 1:
        ref, item = members[0]
        p, judged = effective_precision(item)
        if judged:
            sample = getattr(item, "precision_sample", None)
            sampled = f" ({sample['true']}/{sample['n']} sampled)" if isinstance(sample, dict) else ""
            precision_text = f"precision {p:.2f}{sampled}"
        else:
            precision_text = "precision unjudged (upper bound)"
        return (
            f"{figures.sessions_hit} session(s) in {window_days}d x "
            f"{getattr(item, 'minutes_per_occurrence', 0):g} min/occurrence "
            f"({getattr(item, 'minutes_basis', '') or 'no basis recorded'}); {precision_text}"
        )
    unjudged = [r for r, it in members if not effective_precision(it)[1]]
    text = (
        f"union of {len(members)} members' sessions ({figures.sessions_hit} in {window_days}d), "
        "each charged once at its costliest member"
    )
    if unjudged:
        text += "; precision unjudged (upper bound) for " + ", ".join(unjudged)
    if uncounted:
        text += f"; {uncounted} member(s) without a measurement not counted"
    return text


def _unit_tie(units: "Sequence[tuple[str, Any]]") -> tuple:
    return min(_tie_break(it, ref) for ref, it in units)


def build_lanes(items: "Mapping[str, Any]") -> dict:
    """Partition the open board items into the loss, silent and awaiting lanes, each ordered.

    Every item lands in exactly one lane, in exactly one row. Family members (shared
    `family` id) form one unit: the unit's lane is decided by its best member, its loss is
    computed once over the union of its members' sessions, and it is one row. Rows carry
    only dicts; the ordering key is internal."""
    units: "dict[str, list[tuple[str, Any]]]" = {}
    for ref, item in items.items():
        family = getattr(item, "family", "") or ""
        units.setdefault(family or f"\0{ref}", []).append((ref, item))

    loss_units: "list[tuple[str, list]]" = []
    silent_units: "list[tuple[str, list]]" = []
    awaiting: "list[tuple[str, Any]]" = []
    for unit_id, members in units.items():
        if any(_loss_ready(it) for _r, it in members):
            loss_units.append((unit_id, members))
        elif any(_silent_ready(it) for _r, it in members):
            silent_units.append((unit_id, members))
        else:
            awaiting.extend(members)

    def unit_blockers(lane_units):
        row_of = {r: uid for uid, members in lane_units for r, _it in members}
        return {
            uid: {
                row_of[b] for _r, it in members for b in (getattr(it, "blocked_by", ()) or ())
                if b in row_of and row_of[b] != uid
            }
            for uid, members in lane_units
        }

    loss_rows: "dict[str, dict]" = {}
    loss_sort: "dict[str, tuple]" = {}
    for unit_id, members in loss_units:
        ready = [(r, it) for r, it in members if _loss_ready(it)]
        window_days = max(
            int(it.loss_measurement.get("window_days") or DEFAULT_WINDOW_DAYS) for _r, it in ready
        )
        figures = family_loss(
            [
                Member(
                    r, _member_sessions(r, it), effective_precision(it)[0],
                    float(it.minutes_per_occurrence),
                )
                for r, it in ready
            ],
            window_days,
        )
        family = unit_id if not unit_id.startswith("\0") else ""
        row_ref = family or members[0][0]
        fix = min(
            (getattr(it, "cost_to_resolve", "") for _r, it in members),
            key=lambda c: _FIX_COST_RANK.get(c, _UNKNOWN_FIX_COST_RANK),
        )
        loss_rows[unit_id] = {
            "ref": row_ref,
            "family": family,
            "members": [r for r, _it in members],
            "min_per_week": figures.min_per_week,
            "sessions_hit": figures.sessions_hit,
            "sessions_est": figures.sessions_est,
            "min_per_occurrence": figures.min_per_occurrence,
            "basis": _basis(ready, figures, window_days, len(members) - len(ready)),
            "fix_cost": fix if fix in _FIX_COST_RANK else "",
            "window_days": window_days,
        }
        loss_sort[unit_id] = (-round(figures.min_per_week, 9), *_unit_tie(members))
    loss_order = constrained_order(
        loss_rows, unit_blockers(loss_units), key=lambda uid: loss_sort[uid]
    )

    silent_rows: "dict[str, dict]" = {}
    silent_sort: "dict[str, tuple]" = {}
    for unit_id, members in silent_units:
        family = unit_id if not unit_id.startswith("\0") else ""
        estimates = list(dict.fromkeys(
            it.silent_estimate.strip() for _r, it in members if _silent_ready(it)
        ))
        silent_rows[unit_id] = {
            "ref": family or members[0][0],
            "family": family,
            "members": [r for r, _it in members],
            "silent_estimate": " / ".join(estimates),
        }
        silent_sort[unit_id] = _unit_tie(members)
    silent_order = constrained_order(
        silent_rows, unit_blockers(silent_units), key=lambda uid: silent_sort[uid]
    )

    awaiting_items = dict(awaiting)
    awaiting_order = constrained_order(
        awaiting_items,
        {r: getattr(it, "blocked_by", ()) or () for r, it in awaiting},
        key=lambda r: _tie_break(awaiting_items[r], r),
    )
    return {
        "loss": [loss_rows[u] for u in loss_order],
        "silent": [silent_rows[u] for u in silent_order],
        "awaiting": [
            {"ref": r, "title": getattr(awaiting_items[r], "title", "") or r} for r in awaiting_order
        ],
    }


def lane_ranks(lanes: dict) -> "dict[str, int]":
    """Board rank per item ref: rows in lane order (loss, silent, awaiting), 1-based; the
    members of a family row share its rank."""
    ranks: "dict[str, int]" = {}
    position = 0
    for row in lanes["loss"] + lanes["silent"]:
        position += 1
        for member in row["members"]:
            ranks[member] = position
    for row in lanes["awaiting"]:
        position += 1
        ranks[row["ref"]] = position
    return ranks
