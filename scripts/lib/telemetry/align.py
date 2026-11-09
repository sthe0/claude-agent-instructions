"""One-off alignment of the 235 hand-classified replan causes to the engine state histories.

Difficulty removed: the hand analysis of 2026-11-03 (replan-causes-2026-11-03.jsonl) kept only a
category and a machinery flag per cause, with no task, session, text or time. To give the
imported causes a mechanism, a place and a rework interval where the history still shows them,
each hand row is matched to a replan event of the engine state histories by category and order:

1. `enumerate_replans` reads the state snapshot (tar of state/*.json, never extracted) and lists
   every `replan` event stamped at or before `fixture_analysis_at`, ordered by (replan time,
   session, replan index). A replan's interval is the time the session spent in DIAGNOSING
   before it. The snapshot hash must equal the one stage 1 recorded.
2. `write_draft` writes one row per replan without the hand categories and without critique
   text (only its sha256): position, session, difficulty index, replan time, interval and an
   `aligned_category` the developer fills in from the critique's functional ground and replanning
   task, shown by `show_texts` into an uncommitted file.
3. `build_fixture` compares the judged draft with the hand enumeration. The alignment is refused
   (no row aligned, every row `category_default`) unless the count is 235, at most a tenth of the
   positions disagree, no more than 5 consecutive positions disagree and the disagreement shares
   at offsets -1 and +1 are each at least twice the offset-0 share and 0.05 above it. Under an
   accepted alignment a disagreeing position is unaligned; the others are aligned.
4. A mechanism is observed only when the aligned replan's text names exactly one registry entry
   (its id or a source key); every other value is `category_default`.

`report` rebuilds the committed alignment report from the fixture rows alone.

Stdlib, `lib.telemetry.tables`, `lib.telemetry.phases` and `lib.telemetry.cause` only.
"""
from __future__ import annotations

import hashlib
import json
import re
import tarfile
import tomllib
from datetime import datetime
from pathlib import Path

from lib.telemetry import tables

EXPECTED_COUNT = 235
MAX_DISAGREE_SHARE = 0.1
MAX_RUN = 5
OFFSET_FACTOR = 2
OFFSET_MARGIN = 0.05
OFFSETS = (-1, 0, 1)
STEM = "replan_causes_2026_11_03"
SOURCE = f"fixture:{STEM}"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
DEFAULTS_PATH = Path(__file__).resolve().parent / "tables" / "category_defaults.toml"
HAND_FILE = "replan-causes-2026-11-03.jsonl"
SNAPSHOT_FILE = "state-snapshot.tar"
PREMISES_FILE = "premises.json"
OBSERVED = "observed"
DEFAULTED = "category_default"
UNKNOWN = "unknown"
NONE = "none"
DRAFT_FIELDS = ("position", "session", "difficulty_index", "replan_time", "interval_minutes",
                "critique_sha256", "aligned_category")


class AlignmentError(Exception):
    """An input that must not be aligned from: a hash that differs, a missing file, a stale draft."""


def fixture_path(name: str = "") -> Path:
    return FIXTURES / f"{STEM}{name}"


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_jsonl(path) -> list:
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_jsonl(path, rows) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8")


def _ms(value) -> "int | None":
    from lib import time_profile
    return time_profile.epoch_or_iso_ms(value)


def _iso_ms(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def critique_text(replan: dict) -> str:
    return (replan.get("functional_ground") or "") + "\n" + (replan.get("replanning_task") or replan.get("reason") or "")


def critique_sha256(replan: dict) -> str:
    return hashlib.sha256(critique_text(replan).encode("utf-8")).hexdigest()


# --- step 1: the snapshot ------------------------------------------------------------------

def check_premises(inputs, hand: bool = False) -> dict:
    """The premises of stage 1, after the snapshot (and with `hand` the causes file) is shown to
    have the sha256 recorded there."""
    inputs = Path(inputs)
    premises = json.loads((inputs / PREMISES_FILE).read_text(encoding="utf-8"))
    pairs = [(SNAPSHOT_FILE, "state_snapshot_sha256")] + ([(HAND_FILE, "causes_sha256")] if hand else [])
    for name, key in pairs:
        if sha256_file(inputs / name) != premises.get(key):
            raise AlignmentError(f"{name}: sha256 differs from premises.json {key}")
    return premises


def _diagnosing_minutes(points: list, index: int) -> "float | None":
    """Minutes between the entry into DIAGNOSING and the history event at `index`, which leaves
    it; None when the session was not in DIAGNOSING just before, or a time is missing."""
    if index == 0 or points[index - 1][1] != "DIAGNOSING":
        return None
    start = index - 1
    while start > 0 and points[start - 1][1] == "DIAGNOSING":
        start -= 1
    entered, left = points[start][0], points[index][0]
    return None if entered is None or left is None else round(max(0, left - entered) / 60000, 2)


def _replans_of(data: dict, session: str, cutoff_ms: int) -> list:
    from lib.telemetry import phases
    history = data.get("history") or []
    points = phases.history_points(history)
    found = []
    ordinal = 0
    for index, event in enumerate(history):
        if not isinstance(event, dict) or event.get("event") != "replan":
            continue
        at = _ms(event.get("ts"))
        replan_index, ordinal = ordinal, ordinal + 1
        if at is None or at > cutoff_ms:
            continue
        found.append({"session": session, "difficulty_index": replan_index, "at_ms": at,
                      "replan_time": str(event["ts"]), "interval_minutes": _diagnosing_minutes(points, index),
                      "functional_ground": event.get("functional_ground"),
                      "replanning_task": event.get("replanning_task"), "reason": event.get("reason")})
    return found


def enumerate_replans(inputs, cutoff: "str | None" = None) -> list:
    """The timed replans of the state snapshot at or before the hand analysis, in the analysis order."""
    inputs = Path(inputs)
    premises = check_premises(inputs)
    cutoff_ms = _iso_ms(cutoff or premises["fixture_analysis_at"])
    found = []
    with tarfile.open(inputs / SNAPSHOT_FILE, "r:*") as tar:
        for member in tar:
            if not member.isfile() or not member.name.endswith(".json"):
                continue
            try:
                data = json.load(tar.extractfile(member))
            except ValueError:
                continue
            if isinstance(data, dict):
                found += _replans_of(data, str(data.get("session_id") or Path(member.name).stem), cutoff_ms)
    found.sort(key=lambda r: (r["at_ms"], r["session"], r["difficulty_index"]))
    return found


# --- step 2: the draft ---------------------------------------------------------------------

def draft_rows(replans: list, judged: "dict | None" = None) -> list:
    """The draft rows: no hand category and no critique text. `judged` maps (session, index,
    critique sha256) to the aligned_category already judged, kept across a re-run."""
    judged = judged or {}
    rows = []
    for position, replan in enumerate(replans):
        digest = critique_sha256(replan)
        rows.append({"position": position, "session": replan["session"],
                     "difficulty_index": replan["difficulty_index"], "replan_time": replan["replan_time"],
                     "interval_minutes": replan["interval_minutes"], "critique_sha256": digest,
                     "aligned_category": judged.get((replan["session"], replan["difficulty_index"], digest))})
    return rows


def judged_categories(draft: list) -> dict:
    return {(r["session"], r["difficulty_index"], r["critique_sha256"]): r.get("aligned_category")
            for r in draft if r.get("aligned_category")}


def write_draft(inputs, path=None) -> list:
    path = Path(path) if path else fixture_path(".alignment.jsonl")
    replans = enumerate_replans(inputs)
    previous = judged_categories(read_jsonl(path)) if path.exists() else {}
    rows = draft_rows(replans, previous)
    write_jsonl(path, rows)
    return rows


def show_texts(inputs, out) -> int:
    """Write position and critique text of every replan to `out`, a file outside the repository,
    for judging the categories; the committed draft never holds the text."""
    replans = enumerate_replans(inputs)
    write_jsonl(out, [{"position": i, "text": critique_text(r)} for i, r in enumerate(replans)])
    return len(replans)


# --- step 3: acceptance --------------------------------------------------------------------

def _share(mismatches: int, total: int) -> float:
    return round(mismatches / total, 4) if total else 0.0


def offset_shares(aligned: list, hand: list) -> dict:
    """Per offset in -1, 0, +1 the share of hand positions whose category differs from the aligned
    category `offset` positions on (a position with no counterpart differs)."""
    shares = {}
    for offset in OFFSETS:
        wrong = 0
        for position, category in enumerate(hand):
            other = position + offset
            if not (0 <= other < len(aligned)) or aligned[other] != category:
                wrong += 1
        shares[str(offset)] = _share(wrong, len(hand))
    return shares


def longest_run(flags: list) -> int:
    best = run = 0
    for flag in flags:
        run = run + 1 if flag else 0
        best = max(best, run)
    return best


def refusal_reason(found: int, aligned: list, hand: list) -> "str | None":
    """Why the alignment is refused, or None when it is accepted."""
    if found != EXPECTED_COUNT or len(hand) != EXPECTED_COUNT:
        return f"replan count {found} (hand rows {len(hand)}) is not {EXPECTED_COUNT}"
    if any(category is None for category in aligned):
        return "the draft has positions without an aligned_category"
    wrong = [aligned[p] != hand[p] for p in range(len(hand))]
    share = _share(sum(wrong), len(hand))
    if share > MAX_DISAGREE_SHARE:
        return f"category disagreement share {share} exceeds {MAX_DISAGREE_SHARE}"
    run = longest_run(wrong)
    if run > MAX_RUN:
        return f"{run} consecutive positions disagree, more than {MAX_RUN}"
    shares = offset_shares(aligned, hand)
    for side in ("-1", "1"):
        if shares[side] < OFFSET_FACTOR * shares["0"] or shares[side] < shares["0"] + OFFSET_MARGIN:
            return f"offset {side} share {shares[side]} is not above offset 0 share {shares['0']}"
    return None


def load_defaults(path=None) -> dict:
    with open(path or DEFAULTS_PATH, "rb") as handle:
        data = tomllib.load(handle)
    return {"non_machinery_place": data["non_machinery_place"],
            "categories": {c["name"]: c for c in data["categories"]}}


def registry_matches(text: str, root=None) -> list:
    """The registry entries whose id or source key the text names as a whole token."""
    table = tables.load("registry", root)
    named = []
    for name, member in table.members.items():
        if not member.live_at(table.version):
            continue
        keys = {name, *member.attrs.get("source_keys", [])}
        if any(re.search(r"(?<![\w:.-])" + re.escape(key) + r"(?![\w:-])", text) for key in keys):
            named.append(name)
    return named


def _row(position: int, hand: dict, defaults: dict, draft: "dict | None", refusal: "str | None",
         texts: dict, found: int, root=None) -> dict:
    category = hand["category"]
    machinery = bool(hand["machinery_induced"])
    base = defaults["categories"][category]
    judged = draft.get("aligned_category") if draft else None
    aligned = refusal is None and draft is not None and judged == category
    mechanism, place = (UNKNOWN, base["place"]) if machinery else (NONE, defaults["non_machinery_place"])
    basis = DEFAULTED
    if machinery and aligned:
        named = registry_matches(texts.get(position, ""), root)
        if len(named) == 1:
            mechanism = named[0]
            place = tables.load("registry", root).members[mechanism].attrs["place"]
            basis = OBSERVED
    interval = draft.get("interval_minutes") if aligned else None
    return {
        "i": hand["i"], "category": category, "machinery_induced": machinery,
        "machinery_override": machinery != bool(base["machinery_induced"]),
        "mechanism": mechanism, "functional_place": place,
        "mechanism_basis": basis, "place_basis": basis,
        "alignment": ({"session": draft["session"], "difficulty_index": draft["difficulty_index"]} if aligned else None),
        "interval_minutes": interval, "interval_unknown": interval is None,
        "aligned_category": judged, "replans_found": found, "alignment_refusal": refusal,
        "imported": True,
    }


def build_fixture(inputs, draft: list, defaults: "dict | None" = None, root=None) -> list:
    """The 235 fixture rows: the hand values, the alignment where accepted, defaults elsewhere.
    The snapshot is read again so a stale draft is refused."""
    inputs = Path(inputs)
    check_premises(inputs, hand=True)
    hand = read_jsonl(inputs / HAND_FILE)
    replans = enumerate_replans(inputs)
    if [(r["position"], r["session"], r["difficulty_index"], r["critique_sha256"]) for r in draft] != [
            (i, r["session"], r["difficulty_index"], critique_sha256(r)) for i, r in enumerate(replans)]:
        raise AlignmentError("the committed draft does not match the replans of the snapshot")
    defaults = defaults or load_defaults()
    aligned = [r.get("aligned_category") for r in draft]
    refusal = refusal_reason(len(replans), aligned, [h["category"] for h in hand])
    texts = {i: critique_text(r) for i, r in enumerate(replans)}
    return [_row(i, h, defaults, draft[i] if i < len(draft) else None, refusal, texts, len(replans), root)
            for i, h in enumerate(hand)]


def counts_of(hand: list, causes_sha256: str) -> dict:
    categories = {}
    for row in hand:
        slot = categories.setdefault(row["category"], {"false": 0, "true": 0})
        slot["true" if row["machinery_induced"] else "false"] += 1
    return {"causes_sha256": causes_sha256, "total": len(hand),
            "categories": {k: categories[k] for k in sorted(categories)}}


def run_align(inputs, fixture_dir=None) -> dict:
    """Write the fixture file and its counts from the committed draft; returns what it wrote."""
    inputs = Path(inputs)
    directory = Path(fixture_dir) if fixture_dir else FIXTURES
    draft = read_jsonl(directory / f"{STEM}.alignment.jsonl")
    rows = build_fixture(inputs, draft)
    write_jsonl(directory / f"{STEM}.jsonl", rows)
    hand = read_jsonl(inputs / HAND_FILE)
    counts = counts_of(hand, sha256_file(inputs / HAND_FILE))
    (directory / f"{STEM}.counts.json").write_text(json.dumps(counts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"rows": len(rows), "refusal": rows[0]["alignment_refusal"] if rows else None}


# --- the report ----------------------------------------------------------------------------

def report_of(rows: list) -> dict:
    """The alignment report, computed from the fixture rows alone."""
    total = len(rows)
    categories = [r["category"] for r in rows]
    aligned_categories = [r.get("aligned_category") for r in rows]
    disagreeing = [i for i, r in enumerate(rows) if r.get("aligned_category") != r["category"]]
    aligned = sum(1 for r in rows if r["alignment"] is not None)
    observed = sum(1 for r in rows if r["mechanism_basis"] == OBSERVED)
    refusal = rows[0].get("alignment_refusal") if rows else None
    if observed == 0 and refusal is None:
        refusal = "no mechanism was observed: no aligned critique names exactly one registry entry"
    return {"count_found": rows[0].get("replans_found") if rows else 0, "rows": total,
            "disagreeing_positions": disagreeing, "aligned_share": _share(aligned, total),
            "observed_mechanism_share": _share(observed, total),
            "offset_shares": offset_shares(aligned_categories, categories), "refusal_reason": refusal}


def write_report(fixture_dir=None) -> dict:
    directory = Path(fixture_dir) if fixture_dir else FIXTURES
    report = report_of(read_jsonl(directory / f"{STEM}.jsonl"))
    (directory / f"{STEM}.alignment.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report
