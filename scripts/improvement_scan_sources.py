"""The rules of `improvement-scan.py sources`: what is read, what is kept, what is shown.

Difficulty removed: the agent system improves from its own recorded difficulties, but the
best prior art on self-improving applied agents is published elsewhere, and nothing read
it. Reading is cheap; the risk is a report that fills with plausible ideas nobody asked
for. So this module is the RULE half of a perception/rule split. The skill-running model
judges every entry (is it relevant, what difficulty does it remove, how strong is the
evidence, which of our open difficulties does it address); this module only validates
those judgements against closed vocabularies, keeps the state (what was seen, what is
queued, which claims are cached), decides the order in which ideas are shown, and assembles
a dossier row for each new source. No text is ever matched against a word list to decide
what it means: an `addresses` entry either names one of our open difficulties by its exact
key or does not.

Pure by construction: no network and no subprocess. The only urllib reach is
`improvement_scan_fetch.py`, and the URL-shape and domain predicates it owns are passed in
where a rule needs them.
"""
from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

EVIDENCE_LEVELS = ("measured", "anecdote", "opinion")
STATUS_VOCAB = ("ok", "error", "n/a")
SOURCE_KINDS = ("rss", "atom", "html", "telegram")
TELEGRAM_PREFIX = "https://t.me/s/"

# Dossier signal name -> part, exactly the [[signal]] tables of the approved format file.
DOSSIER_SIGNALS = {
    "descriptive": "descriptive",
    "feed": "feed",
    "local_refs": "local",
    "hn": "external",
    "github": "external",
    "openalex": "external",
    "evidence_profile": "evidence",
}

# The fields of the `descriptive` signal in the approved format; every statement names one.
DESCRIPTIVE_FIELDS = ("author_or_publisher", "affiliation", "notable_works", "topic_fit")

_META_INTS =("review_after_weeks", "max_items_per_run", "queue_cap", "max_external_calls_per_run")
_SOURCE_KEYS = frozenset({"id", "name", "url", "kind", "enabled", "reason"})
_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
_CLAIM_REQUIRED = ("title", "difficulty_removed", "mechanism", "evidence_level", "evidence_basis")
_CLAIM_OPTIONAL = ("addresses", "cited_sources", "candidate_sources")
_MAX_STATEMENTS = 6


class SourcesError(ValueError):
    """A configuration or model-supplied file the producer refuses; always exits 2."""


def _problems(problems: "list[str]") -> None:
    if problems:
        raise SourcesError("; ".join(problems))


# --- the sources file -----------------------------------------------------------

def parse_sources(text: str, is_url: Callable[[object], bool]) -> "tuple[dict, list[dict]]":
    """Parse and validate the sources file: `(meta, sources)`. `meta["armed"]` is a date."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SourcesError(f"sources file is not TOML: {exc}") from exc
    problems: "list[str]" = []
    meta = dict(doc.get("meta") or {})
    armed = meta.get("armed")
    if isinstance(armed, datetime):
        armed = armed.date()
    elif isinstance(armed, str):
        try:
            armed = date.fromisoformat(armed)
        except ValueError:
            armed = None
    if not isinstance(armed, date):
        problems.append("meta.armed must be an ISO date")
    meta["armed"] = armed
    if not isinstance(meta.get("enabled"), bool):
        problems.append("meta.enabled must be a bool")
    for key in _META_INTS:
        value = meta.get(key)
        floor = 0 if key == "max_external_calls_per_run" else 1
        if not isinstance(value, int) or isinstance(value, bool) or value < floor:
            problems.append(f"meta.{key} must be an integer >= {floor}")
    sources = list(doc.get("source") or [])
    seen_ids: "set[str]" = set()
    for source in sources:
        sid = source.get("id")
        label = repr(sid)
        if set(source) != _SOURCE_KEYS:
            problems.append(f"source {label}: keys {sorted(source)} != {sorted(_SOURCE_KEYS)}")
            continue
        if not (isinstance(sid, str) and _ID_RE.match(sid)) or sid in seen_ids:
            problems.append(f"source {label}: id must be unique lowercase-with-dashes")
        seen_ids.add(sid)
        if not is_url(source["url"]):
            problems.append(f"source {label}: url is not http(s)")
        if source["kind"] not in SOURCE_KINDS:
            problems.append(f"source {label}: kind {source['kind']!r} not in {list(SOURCE_KINDS)}")
        if not isinstance(source["enabled"], bool):
            problems.append(f"source {label}: enabled must be a bool")
        if source["kind"] == "html" and source["enabled"] is True:
            problems.append(f"source {label}: html cannot be enabled (no html reader)")
        if source["kind"] == "telegram" and not str(source["url"]).startswith(TELEGRAM_PREFIX):
            problems.append(f"source {label}: telegram url must start with {TELEGRAM_PREFIX}")
        if not str(source["reason"]).strip():
            problems.append(f"source {label}: reason is required")
    _problems(problems)
    return meta, sources


def review_due(meta: dict, today: date) -> bool:
    return today >= meta["armed"] + timedelta(weeks=meta["review_after_weeks"])


# --- durable state ----------------------------------------------------------------

@dataclass
class StateDir:
    """The producer's own files, one JSON document each, replaced atomically."""

    path: Path

    def load(self, name: str, default):
        try:
            data = json.loads((self.path / name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return default
        return data if isinstance(data, type(default)) else default

    def save(self, name: str, data) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        target = self.path / name
        tmp = target.with_name(f"{name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, target)

    def remove(self, name: str) -> None:
        try:
            (self.path / name).unlink()
        except FileNotFoundError:
            pass


@dataclass
class SeenCursor:
    """Entries a producer run has already dispositioned, per source.

    An entry is marked only when phase B stores its disposition (or when queue overflow
    drops it), never when it is merely read, so a phase B that never happened loses nothing.
    """

    seen: "dict[str, set[str]]" = field(default_factory=dict)

    @classmethod
    def load(cls, state: StateDir) -> "SeenCursor":
        raw = state.load("seen.json", {})
        return cls({sid: set(ids) for sid, ids in raw.items() if isinstance(ids, list)})

    def save(self, state: StateDir) -> None:
        state.save("seen.json", {sid: sorted(ids) for sid, ids in sorted(self.seen.items())})

    def is_new(self, source_id: str, entry_id: str) -> bool:
        return entry_id not in self.seen.get(source_id, ())

    def mark(self, source_id: str, entry_id: str) -> None:
        self.seen.setdefault(source_id, set()).add(entry_id)


def enqueue(
    queue: "list[dict]", cursor: SeenCursor, fetched: "dict[str, list[dict]]", cap: int,
) -> "tuple[list[dict], dict[str, int], int]":
    """Append entries not yet seen or queued, oldest first; past `cap` the oldest queued
    entries are dropped and marked seen. Returns `(queue, new_per_source, dropped)`."""
    queued = {e["entry_id"] for e in queue}
    queue = list(queue)
    new: "dict[str, int]" = {}
    for source_id, entries in fetched.items():
        new[source_id] = 0
        for entry in sorted(entries, key=lambda e: e["published"]):
            if cursor.is_new(source_id, entry["entry_id"]) and entry["entry_id"] not in queued:
                queue.append(entry)
                queued.add(entry["entry_id"])
                new[source_id] += 1
    dropped = max(0, len(queue) - cap)
    for entry in queue[:dropped]:
        cursor.mark(entry["source_id"], entry["entry_id"])
    return queue[dropped:], new, dropped


def select_items(queue: "list[dict]", ok_source_ids: "list[str]", bound: int) -> "list[dict]":
    """Round-robin over the sources that read cleanly this run, at most `bound` entries."""
    lanes = {sid: [e for e in queue if e["source_id"] == sid] for sid in ok_source_ids}
    chosen: "list[dict]" = []
    while len(chosen) < bound and any(lanes.values()):
        for sid in ok_source_ids:
            if lanes[sid] and len(chosen) < bound:
                chosen.append(lanes[sid].pop(0))
    return chosen


# --- our own open difficulties, and the claims that may address them -------------------

def difficulty_ref(finding: dict, telemetry_kind: str) -> str:
    """How a claim names one of our open difficulties: a telemetry row by its store key,
    a backlog item by its source ref."""
    return finding["key"] if finding["source"] == telemetry_kind else finding["source_ref"]


def open_difficulties(findings: "list[dict]", telemetry_kind: str) -> "list[dict]":
    """The refs a claim may address, with the text the model judges relevance against."""
    out: "list[dict]" = []
    for finding in findings:
        ref = difficulty_ref(finding, telemetry_kind)
        if ref:
            out.append({"ref": ref, "title": finding["title"], "functional_ground": finding["functional_ground"]})
    return out


def rejoin_entries(claims: "dict[str, dict]", difficulties: "list[dict]") -> "list[dict]":
    """Cached claims paired with the open difficulties they were never judged against."""
    out: "list[dict]" = []
    for entry_id, claim in claims.items():
        unconsidered = [d for d in difficulties if d["ref"] not in claim.get("considered", ())]
        if unconsidered:
            out.append({"entry_id": entry_id, "title": claim["title"], "open_difficulties": unconsidered})
    return out


def _nonempty_str(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _str_list(value) -> bool:
    return isinstance(value, list) and all(_nonempty_str(v) for v in value)


def validate_claims(
    raw, emitted: dict, cached: "dict[str, dict]", is_url: Callable[[object], bool],
) -> dict:
    """Check a claims file against what phase A emitted. Returns the dispositions:
    `{"claims": {entry_id: claim}, "skips": [entry_id], "amend": {entry_id: [address]}}`.
    Raises SourcesError listing every problem; the caller writes nothing on error."""
    if not isinstance(raw, dict) or not isinstance(raw.get("claims"), list):
        raise SourcesError("claims file must be an object with a 'claims' list")
    items, rejoin = emitted["items"], emitted["rejoin"]
    problems: "list[str]" = []
    plan: dict = {"claims": {}, "skips": [], "amend": {}}
    seen: "set[str]" = set()
    for claim in raw["claims"]:
        entry_id = claim.get("entry_id") if isinstance(claim, dict) else None
        if not isinstance(entry_id, str):
            problems.append(f"claim without an entry_id: {claim!r:.80}")
            continue
        if entry_id in seen:
            problems.append(f"{entry_id}: more than one disposition")
            continue
        seen.add(entry_id)
        keys = set(claim)
        if entry_id in items:
            if keys == {"entry_id", "skip"}:
                if _nonempty_str(claim["skip"]):
                    plan["skips"].append(entry_id)
                else:
                    problems.append(f"{entry_id}: skip needs a reason")
            else:
                _check_full_claim(entry_id, claim, is_url, problems, plan)
        elif entry_id in rejoin and entry_id in cached:
            if keys == {"entry_id", "addresses"} and _str_list(claim["addresses"]):
                plan["amend"][entry_id] = list(dict.fromkeys(claim["addresses"]))
            else:
                problems.append(f"{entry_id}: a rejoin amendment is exactly {{entry_id, addresses: [..]}}")
        else:
            problems.append(f"{entry_id}: not emitted by the last --emit-items")
    missing = sorted(set(items) - seen)
    if missing:
        problems.append("no disposition for emitted entries: " + ", ".join(missing))
    _problems(problems)
    return plan


def _check_full_claim(entry_id: str, claim: dict, is_url, problems: "list[str]", plan: dict) -> None:
    allowed = {"entry_id", *_CLAIM_REQUIRED, *_CLAIM_OPTIONAL}
    before = len(problems)
    if set(claim) - allowed:
        problems.append(f"{entry_id}: unknown keys {sorted(set(claim) - allowed)}")
    for key in _CLAIM_REQUIRED:
        if not _nonempty_str(claim.get(key)):
            problems.append(f"{entry_id}: {key} must be a non-empty string")
    if claim.get("evidence_level") not in EVIDENCE_LEVELS:
        problems.append(f"{entry_id}: evidence_level must be one of {list(EVIDENCE_LEVELS)}")
    for key in _CLAIM_OPTIONAL:
        if not _str_list(claim.get(key, [])):
            problems.append(f"{entry_id}: {key} must be a list of non-empty strings")
    if _str_list(claim.get("cited_sources", [])) and not all(is_url(u) for u in claim.get("cited_sources", [])):
        problems.append(f"{entry_id}: cited_sources must be http(s) urls")
    if len(problems) == before:
        plan["claims"][entry_id] = claim


def apply_plan(
    cache: "dict[str, dict]", plan: dict, emitted: dict, today: str,
    listed_urls: "set[str]", is_url: Callable[[object], bool],
) -> "dict[str, dict]":
    """The claim cache after a validated plan: new claims stored (judged against the
    difficulties phase A showed), rejoin amendments merged, shown difficulties marked
    considered. Candidate sources keep only http(s) urls not already in the sources file."""
    cache = {k: dict(v) for k, v in cache.items()}
    shown = [d["ref"] for d in emitted["difficulties"]]
    for entry_id, claim in plan["claims"].items():
        item = emitted["items"][entry_id]
        candidates = [u for u in claim.get("candidate_sources", []) if is_url(u) and u not in listed_urls]
        cache[entry_id] = {
            "entry_id": entry_id, "source_id": item["source_id"], "url": item["url"],
            "published": item["published"], "first_seen": today,
            **{k: claim[k] for k in _CLAIM_REQUIRED},
            "addresses": list(dict.fromkeys(claim.get("addresses", []))),
            "cited_sources": list(claim.get("cited_sources", [])),
            "candidate_sources": list(dict.fromkeys(candidates)),
            "considered": shown,
        }
    for entry_id, refs in emitted["rejoin"].items():
        if entry_id in cache:
            cache[entry_id]["considered"] = sorted({*cache[entry_id].get("considered", []), *refs})
    for entry_id, addresses in plan["amend"].items():
        cache[entry_id]["addresses"] = list(dict.fromkeys([*cache[entry_id]["addresses"], *addresses]))
    return cache


def idea_payload(claim: dict) -> dict:
    """The part of a cached claim a store row carries: everything the report shows."""
    keys = ("entry_id", "source_id", "url", "published", *_CLAIM_REQUIRED, "addresses", "cited_sources")
    return {k: claim[k] for k in keys}


# --- the join and the order ideas are shown in ------------------------------------------

def split_by_join(ideas: "list[dict]", open_refs: "set[str]") -> "tuple[list[dict], list[dict]]":
    """Resolve each idea's addresses against the open difficulty refs of the same report.
    An idea with at least one resolving address is matched; one with none goes to the
    unmatched lane, by first_seen. Every idea carries `resolved` and `unresolved` lists."""
    matched: "list[dict]" = []
    unmatched: "list[dict]" = []
    for idea in ideas:
        resolved = [a for a in idea.get("addresses", []) if a in open_refs]
        unresolved = [a for a in idea.get("addresses", []) if a not in open_refs]
        annotated = {**idea, "resolved": resolved, "unresolved": unresolved}
        (matched if resolved else unmatched).append(annotated)
    return matched, sorted(unmatched, key=lambda i: i.get("first_seen", ""))


def rank_ideas(ideas: "list[dict]", difficulty_rank: "dict[str, int]") -> "list[dict]":
    """Matched ideas: evidence level first (measured, anecdote, opinion), then the best
    rank among the difficulties the idea addresses, then first_seen."""
    def key(idea: dict) -> tuple:
        ranks = [difficulty_rank[a] for a in idea.get("addresses", []) if a in difficulty_rank]
        return (
            EVIDENCE_LEVELS.index(idea["evidence_level"]),
            min(ranks) if ranks else len(difficulty_rank),
            idea.get("first_seen", ""),
        )

    return sorted(ideas, key=key)


# --- candidate sources and their dossiers ---------------------------------------------

def candidate_pool(claims: "dict[str, dict]", listed_urls: "set[str]") -> "dict[str, list[str]]":
    """Deduplicated candidate urls (not in the sources file) -> titles of the citing ideas."""
    pool: "dict[str, list[str]]" = {}
    for claim in claims.values():
        for url in claim.get("candidate_sources", []):
            if url not in listed_urls:
                pool.setdefault(url, [])
                if claim["title"] not in pool[url]:
                    pool[url].append(claim["title"])
    return pool


def validate_descriptions(raw, is_url: Callable[[object], bool]) -> "list[dict]":
    if not isinstance(raw, dict) or not isinstance(raw.get("descriptions"), list):
        raise SourcesError("descriptions file must be an object with a 'descriptions' list")
    problems: "list[str]" = []
    for desc in raw["descriptions"]:
        subject = desc.get("subject") if isinstance(desc, dict) else None
        if not _nonempty_str(subject):
            problems.append(f"description without a subject: {desc!r:.80}")
            continue
        for key in ("author", "github_login"):
            if key in desc and not _nonempty_str(desc[key]):
                problems.append(f"{subject}: {key} must be a non-empty string")
        statements = desc.get("statements")
        if not isinstance(statements, list) or not 1 <= len(statements) <= _MAX_STATEMENTS:
            problems.append(f"{subject}: statements must be a list of 1..{_MAX_STATEMENTS}")
            continue
        for statement in statements:
            ok = isinstance(statement, dict) and _nonempty_str(statement.get("text")) \
                and is_url(statement.get("url"))
            if not ok:
                problems.append(f"{subject}: every statement needs text and an http(s) url")
            elif statement.get("field") not in DESCRIPTIVE_FIELDS:
                problems.append(f"{subject}: every statement needs a field, one of {', '.join(DESCRIPTIVE_FIELDS)}")
    _problems(problems)
    return raw["descriptions"]


def local_refs_signal(domain: str, claims: "dict[str, dict]", listed_ids: "set[str]",
                      domain_of: Callable[[str], str]) -> dict:
    """Entries of listed sources whose cited or candidate urls name the subject's domain."""
    count = sum(
        1 for claim in claims.values()
        if claim["source_id"] in listed_ids and any(
            domain_of(u) == domain for u in [*claim.get("cited_sources", []), *claim.get("candidate_sources", [])]
        )
    )
    return {"status": "ok", "referencing_entries": count}


def evidence_profile_signal(subject: str, claims: "dict[str, dict]") -> dict:
    own = [c for c in claims.values() if c["source_id"] == subject]
    if not own:
        return {"status": "n/a", "note": "no claims yet"}
    measured = sum(1 for c in own if c["evidence_level"] == "measured")
    return {"status": "ok", "measured_share": round(measured / len(own), 2), "n": len(own)}


def build_dossier(subject: str, description: dict, signals: "dict[str, dict]", described_at: str) -> dict:
    """One dossier: every format signal present with a status in the vocabulary, the model's
    statements beside them, and no combined score."""
    statements = [{"field": s["field"], "text": s["text"], "url": s["url"]} for s in description["statements"]]
    uncovered = [f for f in DESCRIPTIVE_FIELDS if not any(s["field"] == f for s in statements)]
    descriptive = {"status": "ok", "statements": len(statements)}
    if uncovered:
        descriptive["missing"] = ", ".join(uncovered)
    signals = {**signals, "descriptive": descriptive}
    missing = set(DOSSIER_SIGNALS) - set(signals)
    if missing or any(s["status"] not in STATUS_VOCAB for s in signals.values()):
        raise SourcesError(f"{subject}: dossier signals incomplete or out of vocabulary: {sorted(missing)}")
    ordered = {name: signals[name] for name in DOSSIER_SIGNALS}
    return {"subject": subject, "signals": ordered, "statements": statements, "described_at": described_at}


def _signal_cell(name: str, signal: dict) -> str:
    detail = " ".join(
        f"{k}={v}" for k, v in signal.items() if k not in ("status", "note") and v is not None
    ) or signal.get("note", "")
    if signal["status"] != "ok" and signal.get("note"):
        detail = signal["note"]
    return f"{name}: {signal['status']}" + (f" {detail}" if detail else "")


def render_dossier_row(dossier: dict) -> "list[str]":
    """`- <subject> | <name>: <status>[ <value>] | ...`, then the cited statements, each
    labelled with the descriptive field it supports."""
    row = f"- {dossier['subject']} | " + " | ".join(
        _signal_cell(name, dossier["signals"][name]) for name in DOSSIER_SIGNALS
    )
    return [row, *(f"  - [{s['field']}] {s['text']} ({s['url']})" for s in dossier["statements"])]


def today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()
