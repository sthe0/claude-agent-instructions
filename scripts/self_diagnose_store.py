#!/usr/bin/env python3
"""Durable keyed store for self-diagnose findings — the spine that makes a
detected finding outlive the moment of detection.

Difficulty removed: `self-diagnose.py` already enumerates every self-friction
finding and `hook-self-diagnose-due.py` already runs it at session start — but
the worklist went to stderr, where it scrolled away unread. The same broken hook
registration was printed every session for four days and nothing closed it. A
stderr line has no CLOSURE STATE: nothing records that a finding was seen, acted
on, or deliberately deferred, so the detector fires and the finding dies.

This module supplies the missing half. Each finding is keyed on the STABLE
(kind, path) pair — already the identity of a `Difficulty` in self-diagnose's
dataclass — so one standing finding is one row across sessions, and the row
carries first_seen / times_surfaced / status. `hook-turn-end-gate.py` reads the
open ACTIONABLE rows and blocks the turn boundary once per session until each is
closed; the advisory remainder reaches a channel with closure state instead of a
stderr line.

Three named policy switches, each one line, each the applied answer to a
registered planning question:

  ACTIONABLE_KINDS         which kinds may block. An explicit per-kind table over
                           self-diagnose's exhaustive vocabulary, NOT a severity
                           heuristic — this table is the single place actionability
                           is decided.
  ACTIONABLE_MIN_AGE_DAYS  a finding must have been open this long before it may
                           block, so a leaf written this session and not yet
                           indexed does not block its own author.
  ACK_TTL_DAYS             an ack expires and the row returns to `open`. A
                           PERMANENT ack would freeze times_surfaced, making the
                           "fired and ignored more than twice in 30 days ->
                           retire or downgrade" disposal rule unobservable by
                           construction — a terminal silent drain, which is the
                           exact failure mode this module removes.
  ADVISORY_CHANNEL         where the non-blocking remainder goes.

Never edits repo content. The store itself is machine-local runtime state under
~/.local/state and is never committed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

STORE_ENV = "CLAUDE_SELF_DIAGNOSE_STORE"
DEFAULT_STORE = Path.home() / ".local" / "state" / "claude-self-diagnose-findings.jsonl"

# --- policy switches --------------------------------------------------------

# Kinds that may BLOCK the turn boundary. Derived from the live population, not
# asserted: each one's remediation is a single concrete edit (index the leaf in
# its owning MEMORY.md, delete it, or re-run the hook installer).
#
# `ceiling-proximity` is deliberately EXCLUDED and must stay excluded:
# hook-instruction-grooming-due.py already owns that kind with a per-file
# debounced OFFER at UserPromptSubmit. A second inbox for one finding
# redistributes noise instead of reducing it — the alert-fatigue anti-pattern,
# not a fix.
ACTIONABLE_KINDS = frozenset(
    {
        "broken-hook-registration",
        "orphan-leaf",
        "orphan-index",
        "policy-flag",
    }
)

# Kinds contributed by a producer OTHER than self-diagnose.py. Declared, not
# inferred: the vocabulary test below asserts that every kind the DETECTOR emits
# is placed in exactly one table, and without this set a second producer's kind
# would either fail that test or force it to be weakened into accepting anything.
#
# `policy-flag` is policy-scorecard.py's. It is ACTIONABLE for the same reason
# the three above are: the remediation is one concrete named act (invoke
# `self-improvement`, then record the movement), and the failure mode it removes
# is precisely a detection with no closure state — the scorecard printed its
# flags to stdout once per session and nothing recorded that any of them was
# ever acted on.
KIND_POLICY_FLAG = "policy-flag"
EXTERNAL_KINDS = frozenset({KIND_POLICY_FLAG})

# The declared complement. Nothing reads this at runtime — `is_actionable` stays
# a membership test against ACTIONABLE_KINDS alone, so an UNKNOWN kind still
# defaults to advisory and can never block. It exists so the suite can assert
# ACTIONABLE_KINDS | ADVISORY_KINDS == self-diagnose.py's own vocabulary: a ninth
# kind added to the detector then fails a test until someone places it in exactly
# one of the two tables, instead of defaulting silently.
ADVISORY_KINDS = frozenset(
    {
        "ceiling-proximity",
        "near-duplicate",
        "dangling-pointer",
        "oversized-index",
        "no-root-index",
    }
)

ACTIONABLE_MIN_AGE_DAYS = 2

# int -> ack expires after this many days and the row returns to `open`;
# None -> acks are permanent (see the module docstring for why that is a trap).
ACK_TTL_DAYS = 30

# "backlog" | "digest" | "stderr".
#
# `digest` rather than `backlog`, and the reason is machine-specific rather than
# stylistic: scripts/file-difficulty.py refuses EVERY filing on a machine that
# holds Core push rights (it exits 2 with "edit Core directly via the planner ->
# approval -> developer spine"), which is this machine. An auto-filer wired to
# `backlog` here would be a permanent no-op — the same drain this module exists
# to close, reached through a different hole. Advisory findings therefore
# accumulate locally in the digest and are worked through the normal spine.
#
# The `backlog` branch and its tier filter are still implemented and tested: a
# guard that only exists on the path currently taken is not a guard.
ADVISORY_CHANNEL = "digest"

# What to DO about each kind — attached to the finding so the block carries
# rectification data, not just detection.
REMEDIATION = {
    "broken-hook-registration": "re-run scripts/install-reminder-hooks.sh (it now prunes dangling managed registrations), or remove the entry by hand",
    "orphan-leaf": "add a pointer line in the owning MEMORY.md index, or delete the leaf",
    "orphan-index": "link the sub-index from its parent MEMORY.md, or delete it",
    "ceiling-proximity": "invoke the instruction-grooming skill for that file",
    "near-duplicate": "generalize and group the two leaves, or record why they stay separate",
    "dangling-pointer": "fix the link target, or drop the pointer line",
    "oversized-index": "spin off a sub-index (memory-hierarchy.md)",
    "no-root-index": "create the root MEMORY.md for that memory root",
    "policy-flag": "invoke `self-improvement` to adjust the policy, then record the adjustment and the observed metric movement in memory-global/leaves/policy-effectiveness-tracking.md",
}

# --- producers --------------------------------------------------------------
#
# One store, several detectors. `source` PARTITIONS the resolve-out in
# `upsert_findings`: a scan may only retire rows it could itself have produced.
# Without it the second producer's every run would silently resolve away the
# first's whole worklist, because "absent from this scan" would be read as "the
# condition is gone" for rows the scan never looked for.
SOURCE_SELF_DIAGNOSE = "self-diagnose"
SOURCE_POLICY_SCORECARD = "policy-scorecard"


def row_source(row: dict) -> str:
    """A row written before `source` existed is self-diagnose's — that was the
    only producer then, so the default is a fact about the history, not a guess."""
    return str(row.get("source") or SOURCE_SELF_DIAGNOSE)


_STATUS_OPEN = "open"
_STATUS_ACKED = "acked"
_STATUS_SNOOZED = "snoozed"


# --- time helpers -----------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_ts(raw) -> "datetime | None":
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --- store I/O --------------------------------------------------------------

def store_path(path: "str | Path | None" = None) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ.get(STORE_ENV)
    return Path(env) if env else DEFAULT_STORE


def finding_key(kind: str, path: str) -> str:
    """Stable dedup identity of a CONDITION, never of a notification: the same
    standing finding produces the same key on every scan, so one finding is one
    row across sessions."""
    return hashlib.sha256(f"{kind}\0{path}".encode("utf-8")).hexdigest()[:12]


def load_rows(path: "str | Path | None" = None, now: "datetime | None" = None) -> "list[dict]":
    """Every stored row, with expiries applied. Fail-open: a missing, empty or
    corrupt store yields the rows it can parse (possibly none) and never raises,
    because no finding is worth a wedged turn boundary.

    `kind` is coerced to str here: a hand-edited or partially-corrupt row can
    carry any JSON type, and an unhashable one turns every `kind in ...`
    membership test downstream into a TypeError."""
    now = now or _utcnow()
    sp = store_path(path)
    rows: "list[dict]" = []
    try:
        text = sp.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # a truncated mid-row write must not lose the rest
        if isinstance(row, dict) and row.get("key"):
            row["kind"] = str(row.get("kind") or "")
            rows.append(_apply_expiry(row, now))
    return rows


def save_rows(rows: "list[dict]", path: "str | Path | None" = None) -> None:
    """Replace the store atomically — a crash mid-write leaves the previous store
    intact rather than a truncated one. The temp name carries the pid so two
    writers cannot interleave into one another's partial file."""
    sp = store_path(path)
    sp.parent.mkdir(parents=True, exist_ok=True)
    tmp = sp.with_name(f"{sp.name}.{os.getpid()}.tmp")
    tmp.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    os.replace(tmp, sp)


def _apply_expiry(row: dict, now: datetime) -> dict:
    """A snooze past its deadline, or an ack past ACK_TTL_DAYS, returns the row
    to `open`. times_surfaced is never reset — it counts scans in which the
    condition was still present, which is what the disposal rule reads."""
    status = row.get("status") or _STATUS_OPEN
    if status == _STATUS_SNOOZED:
        until = _parse_ts(row.get("snooze_until"))
        if until is None or until <= now:
            row["status"] = _STATUS_OPEN
            row["snooze_until"] = None
    elif status == _STATUS_ACKED and ACK_TTL_DAYS is not None:
        acked_at = _parse_ts(row.get("acked_at"))
        if acked_at is None or (now - acked_at) >= timedelta(days=ACK_TTL_DAYS):
            row["status"] = _STATUS_OPEN
            row["acked_at"] = None
    return row


# --- the upsert -------------------------------------------------------------

def _as_record(finding) -> "tuple[str, str, str]":
    if isinstance(finding, dict):
        return (
            str(finding.get("kind", "")),
            str(finding.get("path", "")),
            str(finding.get("detail", "")),
        )
    return (str(finding.kind), str(finding.path), str(finding.detail))


def upsert_findings(
    findings,
    path: "str | Path | None" = None,
    now: "datetime | None" = None,
    source: str = SOURCE_SELF_DIAGNOSE,
) -> "list[dict]":
    """Merge one scan's findings into the store and return the resulting rows.

    A row absent from the newest scan is RESOLVED — the condition is gone, so it
    is dropped rather than lingering as a permanent accusation. Callers must
    therefore pass the findings of a scan that actually COMPLETED; a failed scan
    is not an empty one.

    That resolve-out is scoped to `source`: rows written by a DIFFERENT producer
    pass through untouched, because this scan never looked for their conditions
    and so cannot have observed them gone. Returned rows are foreign-first then
    this scan's, so the caller still sees the whole store.

    One scan may legitimately emit the same (kind, path) twice — `near-duplicate`
    keys on the first leaf of each pair, so A-vs-B and A-vs-C both key on A — so
    the second occurrence is dropped rather than appended as a second row with a
    double-counted times_surfaced."""
    now = now or _utcnow()
    stored = load_rows(path, now)
    existing = {r["key"]: r for r in stored if row_source(r) == source}
    rows: "list[dict]" = [r for r in stored if row_source(r) != source]
    seen: "set[str]" = set()
    for finding in findings:
        kind, fpath, detail = _as_record(finding)
        if not kind:
            continue
        key = finding_key(kind, fpath)
        if key in seen:
            continue
        seen.add(key)
        row = existing.get(key)
        if row is None:
            row = {
                "key": key,
                "kind": kind,
                "path": fpath,
                "detail": detail,
                "source": source,
                "first_seen": _iso(now),
                "last_seen": _iso(now),
                "times_surfaced": 1,
                "status": _STATUS_OPEN,
                "ack_reason": None,
                "acked_at": None,
                "snooze_until": None,
                "filed_ref": None,
            }
        else:
            row["detail"] = detail
            row["source"] = source  # stamps rows written before `source` existed
            row["last_seen"] = _iso(now)
            row["times_surfaced"] = int(row.get("times_surfaced") or 0) + 1
        rows.append(row)
    save_rows(rows, path)
    return rows


# --- views ------------------------------------------------------------------

def is_actionable(kind: str) -> bool:
    return str(kind) in ACTIONABLE_KINDS


def _age_days(row: dict, now: datetime) -> float:
    first = _parse_ts(row.get("first_seen"))
    if first is None:
        return 0.0
    return (now - first).total_seconds() / 86400.0


def _is_open(row: dict) -> bool:
    return (row.get("status") or _STATUS_OPEN) == _STATUS_OPEN


def open_actionable(
    rows: "list[dict]",
    now: "datetime | None" = None,
    min_age_days: "float | None" = None,
) -> "list[dict]":
    """Actionable, open, and past the debounce — the rows that may block."""
    now = now or _utcnow()
    floor = ACTIONABLE_MIN_AGE_DAYS if min_age_days is None else min_age_days
    return [
        r
        for r in rows
        if is_actionable(r.get("kind", ""))
        and _is_open(r)
        and _age_days(r, now) >= floor
    ]


def debounced_actionable(
    rows: "list[dict]",
    now: "datetime | None" = None,
    min_age_days: "float | None" = None,
) -> "list[dict]":
    """Actionable and open, but still inside ACTIONABLE_MIN_AGE_DAYS.

    The third bucket. Without it a fresh actionable finding is counted in neither
    `open_actionable` nor `advisory_open`, so the very line that reports where
    each finding went would silently account for none of them. Together the three
    views PARTITION the open rows: every open row is in exactly one."""
    now = now or _utcnow()
    floor = ACTIONABLE_MIN_AGE_DAYS if min_age_days is None else min_age_days
    return [
        r
        for r in rows
        if is_actionable(r.get("kind", ""))
        and _is_open(r)
        and _age_days(r, now) < floor
    ]


def advisory_open(rows: "list[dict]", now: "datetime | None" = None) -> "list[dict]":
    return [r for r in rows if not is_actionable(r.get("kind", "")) and _is_open(r)]


def describe(row: dict, now: "datetime | None" = None) -> str:
    """One human-readable line carrying the finding AND its remediation, so a
    surfaced finding supplies rectification data rather than only detection."""
    kind = str(row.get("kind", "?"))
    fix = REMEDIATION.get(kind, "investigate and close")
    return (
        f"[{row.get('key')}] {kind}: {row.get('path')} — {row.get('detail')} "
        f"(open {int(_age_days(row, now or _utcnow()))}d, surfaced {row.get('times_surfaced')}x; fix: {fix})"
    )


def describe_rows(rows: "list[dict]", now: "datetime | None" = None) -> "list[str]":
    """One line per DISTINCT condition, collapsing rows that differ only in which
    directory the file sits in.

    Presentation only — the store still keys on (kind, path), each row keeps its
    own key and stays independently closable, and the collapsed line carries
    every one of those keys. What it removes is the block text reading as noise:
    the live population has one leaf replicated across four project-memory roots,
    which is four rows naming one condition."""
    now = now or _utcnow()
    groups: "dict[tuple[str, str, str], list[dict]]" = {}
    for row in rows:
        key = (
            str(row.get("kind", "")),
            Path(str(row.get("path", ""))).name,
            str(row.get("detail", "")),
        )
        groups.setdefault(key, []).append(row)

    lines: "list[str]" = []
    for (kind, basename, detail), members in groups.items():
        if len(members) == 1:
            lines.append(describe(members[0], now))
            continue
        keys = " ".join(str(m.get("key")) for m in members)
        parents = ", ".join(str(Path(str(m.get("path", ""))).parent) for m in members)
        age = max(int(_age_days(m, now)) for m in members)
        surfaced = max(int(m.get("times_surfaced") or 0) for m in members)
        lines.append(
            f"[{keys}] {kind}: {basename} — {detail} "
            f"({len(members)} paths: {parents}; open {age}d, surfaced {surfaced}x; "
            f"fix: {REMEDIATION.get(kind, 'investigate and close')})"
        )
    return lines


def digest_lines(rows: "list[dict]", now: "datetime | None" = None) -> "list[str]":
    now = now or _utcnow()
    return [
        f"[{r.get('key')}] {r.get('kind')}: {r.get('path')} — {r.get('detail')}"
        + ("" if not r.get("filed_ref") else f" (filed: {r['filed_ref']})")
        for r in advisory_open(rows, now)
    ]


# --- disposal ---------------------------------------------------------------

def ack(key: str, reason: str, path: "str | Path | None" = None, now: "datetime | None" = None) -> bool:
    now = now or _utcnow()
    rows = load_rows(path, now)
    hit = False
    for row in rows:
        if row.get("key") == key:
            row["status"] = _STATUS_ACKED
            row["ack_reason"] = reason
            row["acked_at"] = _iso(now)
            row["snooze_until"] = None
            hit = True
    if hit:
        save_rows(rows, path)
    return hit


def snooze(key: str, days: float, path: "str | Path | None" = None, now: "datetime | None" = None) -> bool:
    now = now or _utcnow()
    rows = load_rows(path, now)
    hit = False
    for row in rows:
        if row.get("key") == key:
            row["status"] = _STATUS_SNOOZED
            row["snooze_until"] = _iso(now + timedelta(days=days))
            hit = True
    if hit:
        save_rows(rows, path)
    return hit


# --- advisory routing -------------------------------------------------------

def inside_core(finding_path: str, core_root: "Path | None" = None) -> bool:
    """True iff this finding's path lies inside the Core repo.

    Symlink resolution is deliberate: ~/.claude-agent/memory-global is a symlink
    INTO the Core repo, so those findings genuinely are public Core content,
    while personal and project memory resolve elsewhere and are filtered out.
    Fails SAFE — any resolution error means "not Core", hence not publishable.

    A RELATIVE finding path is resolved against `core_root`, never against the
    process CWD. Only the ceiling-proximity scan emits one, and it emits it as
    `relative_to(repo_root)`, so the repo root is its real base — but the reason
    to pin it is stronger than accuracy: resolving against the CWD makes this
    guard answer True or False for the same finding depending on where the caller
    happened to be started, and a guard whose verdict moves with the caller's
    working directory is not a guard."""
    try:
        root = Path(core_root or REPO_ROOT).resolve()
        candidate = Path(finding_path).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        return root in candidate.resolve().parents
    except (OSError, RuntimeError, ValueError):
        return False


def _default_filer(row: dict) -> "tuple[int, str]":
    """Invoke file-difficulty.py for one advisory row. Returns (returncode, ref)."""
    argv = [
        sys.executable,
        str(SCRIPT_DIR / "file-difficulty.py"),
        "--target", row.get("path", ""),
        "--ground", f"self-diagnose {row.get('kind')}: {row.get('detail')}",
        "--severity", "low",
        "--stream", "backlog",
        "--reporter", "self-diagnose",
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except Exception:
        return 1, ""
    out = (proc.stdout or "").strip().splitlines()
    return proc.returncode, out[-1] if out else ""


def route_advisory(
    rows: "list[dict]",
    channel: "str | None" = None,
    filer=None,
    core_root: "Path | None" = None,
    path: "str | Path | None" = None,
    now: "datetime | None" = None,
) -> "list[dict]":
    """Send the not-yet-filed advisory rows to ADVISORY_CHANNEL; return the rows
    actually filed.

    Only the `backlog` branch files anything. Two guards on it, both load-bearing:

      * TIER FILTER — only a finding whose path resolves inside the Core repo may
        auto-file, because that venue is a PUBLIC GitHub repo and most live
        advisory findings are personal-memory paths under ~/.claude-agent/.
        Publishing one is irrecoverable (the venue e-mails the body to watchers
        at creation), so the filter is mandatory even though nothing files today.

      * A NON-ZERO filer exit means NOT FILED. The row keeps its unfiled state and
        stays in the digest rather than being marked filed and disappearing — a
        channel that can silently swallow a finding is a drain wearing a
        channel's clothes.
    """
    ch = ADVISORY_CHANNEL if channel is None else channel
    if ch != "backlog":
        return []
    filer = filer or _default_filer
    filed: "list[dict]" = []
    for row in advisory_open(rows, now):
        if row.get("filed_ref"):
            continue
        if not inside_core(row.get("path", ""), core_root):
            continue
        try:
            rc, ref = filer(row)
        except Exception:
            continue
        if rc != 0:
            continue
        row["filed_ref"] = ref or "filed"
        filed.append(row)
    if filed:
        save_rows(rows, path)
    return filed


# --- CLI --------------------------------------------------------------------

def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--store", default=None, help="override the store path")
    parser.add_argument("--list", action="store_true", help="print every stored row")
    parser.add_argument("--digest", action="store_true", help="print the open advisory digest")
    parser.add_argument("--ack", metavar="KEY", default=None, help="acknowledge a finding")
    parser.add_argument("--reason", default="", help="why the finding is acknowledged")
    parser.add_argument("--snooze", metavar="KEY", default=None, help="snooze a finding")
    parser.add_argument("--days", type=float, default=7.0, help="snooze window in days")
    args = parser.parse_args(argv)

    if args.ack:
        if not args.reason:
            print("error: --ack requires --reason", file=sys.stderr)
            return 2
        return 0 if ack(args.ack, args.reason, args.store) else 1

    if args.snooze:
        return 0 if snooze(args.snooze, args.days, args.store) else 1

    rows = load_rows(args.store)
    if args.digest:
        for line in digest_lines(rows):
            print(line)
        return 0

    blocking = {r["key"] for r in open_actionable(rows)}
    for row in rows:
        marker = "!" if row.get("key") in blocking else " "
        print(f"{marker} {row.get('status'):8s} {describe(row)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
