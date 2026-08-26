"""Transcript cost accounting: one dated price table, message-id dedup, price weighting.

Difficulty removed
------------------

Two defects with one home.

(1) THE RATES DRIFT AND NOBODY KNOWS SINCE WHEN. The per-token USD rates are an
external value the vendor changes. They lived in ``cost-report.py`` with no
record of when they were last checked, and any second copy elsewhere would keep
reporting the old numbers forever after the first was corrected. This module is
now the single home: ``cost-report.py`` re-exports from here and
``policy-scorecard.py`` reads it through that re-export, so registering a model
or correcting a rate stays exactly one edit — and the date below says how stale
the answer may be.

(2) SUMMING A TRANSCRIPT PER LINE DOUBLE-COUNTS. A transcript JSONL carries the
same assistant message on more than one line, and every one of those lines
repeats the *message-level* ``usage`` object. Adding up ``usage`` line by line
therefore charges one message several times — measured at 1.9-2.1x inflation
over real sessions (GitHub issue #144). The dedup key is ``message.id``, and it
has to sit in the one place every reader passes through, or the next reader
rediscovers the inflation the hard way.

What "price weighting" means here
---------------------------------

The four token types are not interchangeable and must not be summed as a count:
against base input, a cache read costs 0.1x, a cache write 1.25x, and an output
token 5x. Those ratios are not a second table — they are already exactly what
the USD rates below encode, so ``token_cost`` IS the weighting, and there is no
separate weighting constant that could drift away from the prices.

Untrusted input by construction
-------------------------------

Every reader here is tolerant: a missing file, a final line truncated mid-write
by a live session, a row with no ``usage``, an unparseable timestamp. The
callers include a ``UserPromptSubmit`` hook, where raising on any of these would
put a visible error in front of the user on every prompt of every session —
strictly worse than the missing measurement it would be complaining about.

Pure: no network, no subprocess, no writes.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

# USD per 1M tokens.
#
# Rates verified: 2026-08-26 against scripts/cost-report.py, which is where this
# table lived until that date and where every ledger row priced so far was
# priced from. That is a PROVENANCE date, not a check against the vendor's
# published rate card: the table was moved here unchanged, and the version it
# came from carried no date at all, so how long these numbers have been stale is
# unknown. Re-read the rate card (the `claude-api` skill) and move
# PRICING_VERIFIED when you actually do — an undated table keeps reporting
# confidently after the rates change, which is the whole reason this line exists.
#
# cache_write = 5-minute cache-creation rate (1.25x base input); cache_read =
# 0.1x base input; output = 5x base input.
#
# This table is also the model REGISTRY: policy-scorecard.py derives its
# per-model token buckets from these keys, so a new model is one row here, not an
# edit in six places. Keys are matched against a model id by substring, so no key
# may be a substring of another.
PRICING_USD_PER_MTOK = {
    "opus":   {"input": 5.0,  "output": 25.0, "cache_write": 6.25,  "cache_read": 0.50},
    "sonnet": {"input": 3.0,  "output": 15.0, "cache_write": 3.75,  "cache_read": 0.30},
    "haiku":  {"input": 1.0,  "output": 5.0,  "cache_write": 1.25,  "cache_read": 0.10},
    "fable":  {"input": 10.0, "output": 50.0, "cache_write": 12.5,  "cache_read": 1.00},
}
PRICING_VERIFIED = "2026-08-26"

_FALLBACK_RATES = PRICING_USD_PER_MTOK["opus"]

# Short content hash of the rate table, stamped onto each ledger row as
# `priced_by` so a rate change announces itself instead of silently splitting the
# ledger across two tables. Derived, never hand-written: a version string nobody
# remembers to bump is the same rotting mirror this table's consumers exist to
# avoid.
PRICING_SHA = hashlib.sha256(
    json.dumps(PRICING_USD_PER_MTOK, sort_keys=True).encode("utf-8")
).hexdigest()[:12]

USAGE_KEYS = (
    ("input_tokens", "input"),
    ("output_tokens", "output"),
    ("cache_creation_input_tokens", "cache_write"),
    ("cache_read_input_tokens", "cache_read"),
)


def rates_for(model: str | None) -> dict:
    m = (model or "").lower()
    for key in PRICING_USD_PER_MTOK:
        if key in m:
            return PRICING_USD_PER_MTOK[key]
    return _FALLBACK_RATES


def token_cost(usage: dict, model: str | None) -> float:
    """USD for one message's usage — and the price weighting, in one function."""
    r = rates_for(model)
    return sum(
        (usage.get(raw, 0) or 0) * r[short] for raw, short in USAGE_KEYS
    ) / 1_000_000


def iter_jsonl(path: Path | str) -> Iterator[dict]:
    """Objects from a JSONL file, skipping blank and unparseable lines.

    A missing file yields nothing rather than raising: a live transcript can be
    named by a hook payload and gone by the time the hook opens it, and a
    session still being written can end in half a line.
    """
    p = Path(path)
    try:
        handle = p.open(encoding="utf-8")
    except OSError:
        return
    with handle as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                yield obj


def parse_ts(value) -> dt.datetime | None:
    """An ISO timestamp as an aware UTC datetime, or None when it will not parse.

    Deliberately NOT ``cost-report.py``'s ``parse_ts``, which raises. That one
    reads ledger rows this system wrote itself, where a bad timestamp is a bug
    worth surfacing; this one reads transcripts, where it is Tuesday. Naive
    values are read as UTC so that a single mislabelled row cannot make a whole
    window's comparisons raise.
    """
    if not isinstance(value, str) or not value:
        return None
    s = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


@dataclass(frozen=True)
class UsageRow:
    """One assistant message's billed usage, counted exactly once."""

    message_id: str
    ts: dt.datetime | None
    model: str | None
    usage: dict
    usd: float


def iter_usage_rows(path: Path | str) -> Iterator[UsageRow]:
    """Billed messages from a transcript, deduplicated by ``message.id``.

    FIRST occurrence wins. The repeats exist because the harness writes one line
    per content block of the same message and copies the message-level ``usage``
    onto each, so every occurrence carries identical numbers and the choice is
    arbitrary on the values — but first-wins is the one that needs no lookahead,
    so this stays a streaming read over a file that may be gigabytes.

    A row with no ``message.id`` is counted once on its own, never folded into
    another row: the alternative — one shared "no id" bucket — would silently
    drop every such message but the first.
    """
    seen: set[str] = set()
    anonymous = 0
    for obj in iter_jsonl(path):
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        raw_id = msg.get("id")
        if isinstance(raw_id, str) and raw_id:
            if raw_id in seen:
                continue
            seen.add(raw_id)
            key = raw_id
        else:
            anonymous += 1
            key = f"_anonymous:{anonymous}"
        model = msg.get("model")
        yield UsageRow(
            message_id=key,
            ts=parse_ts(obj.get("timestamp")),
            model=model if isinstance(model, str) else None,
            usage=usage,
            usd=token_cost(usage, model),
        )


def usage_rows(path: Path | str) -> list[UsageRow]:
    return list(iter_usage_rows(path))


def price_weighted_usd(rows: Iterable[UsageRow]) -> float:
    return sum(row.usd for row in rows)


def window_rate(
    rows: Iterable[UsageRow],
    window_h: float,
    *,
    anchor: dt.datetime | None = None,
    min_span_h: float = 0.0,
) -> float | None:
    """USD per hour over the trailing ``window_h`` hours, or None to abstain.

    The denominator is the span the transcript ACTUALLY covers inside the window,
    not the window's nominal length: a session younger than the window would
    otherwise be divided by time that never happened, and read as cheap. The
    opposite error — a session two minutes old whose first expensive turn reads
    as an enormous per-hour rate — is what ``min_span_h`` is for. Below it this
    returns None, which every caller must treat as "no answer", not as zero.
    """
    dated = [r for r in rows if r.ts is not None]
    if not dated:
        return None
    end = anchor if anchor is not None else max(r.ts for r in dated)
    start = end - dt.timedelta(hours=window_h)
    inside = [r for r in dated if start <= r.ts <= end]
    if len(inside) < 2:
        return None
    span_h = (end - min(r.ts for r in inside)).total_seconds() / 3600.0
    if span_h <= 0 or span_h < min_span_h:
        return None
    return price_weighted_usd(inside) / span_h
