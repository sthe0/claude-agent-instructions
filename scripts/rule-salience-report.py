#!/usr/bin/env python3
"""Rule-salience report: does each always-loaded CLAUDE.md rule's delivery
mechanism ever actually fire?

Stage 4 of instruction-surface-governance. Two modes:

  --check-registry   Two-directional drift gate against scripts/rule-registry.toml
                      and CLAUDE.md (no ledger reads). Exit 1 on any drift.
                        Direction A: every rule unit in CLAUDE.md has a registry
                        entry (best-effort heading-coverage check).
                        Direction B: every registry entry's locator_heading and
                        locator_phrase are still verbatim-findable in CLAUDE.md.

  (default)           Ranked report: one row per registry entry, with an
                      observed-firing count/ratio when the entry's delivery
                      mechanism is observable, and one of three states when it
                      is not: NEVER-OBSERVED / TRIGGER-ABSENT / UNINSTRUMENTED
                      (see the docstring of `classify_state` for the exact
                      definitions — the three states are never collapsed).

Firing data sources, all optional and read-only; the report degrades to an
empty section + a stated reason when a source is absent:
  - Session transcripts under `<agent_home>/projects/*/*.jsonl` (bracket-tag /
    agentctl-construct substring search, mirroring skill-usage-audit.py).
  - ~/.local/log/claude-policy-ledger.jsonl (session-count denominator, not
    resolution-gated so it is the primary denominator source; also the
    trigger-occurrence proxy source for the handful of registry entries that
    declare a `trigger_proxy_field`/`trigger_proxy_op`/`trigger_proxy_value` -
    this is what makes TRIGGER-ABSENT a genuinely reachable state rather than
    a documented-but-dead vocabulary entry, see `classify_state`).
  - ~/.local/log/claude-task-quality.jsonl (fallback denominator source only;
    not used for trigger proxies).

No network, no writes.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.config_root import agent_home

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO_ROOT / "scripts" / "rule-registry.toml"
CLAUDE_MD_PATH = REPO_ROOT / "CLAUDE.md"
PROJECTS_ROOT = agent_home() / "projects"
POLICY_LEDGER_PATH = Path.home() / ".local" / "log" / "claude-policy-ledger.jsonl"
QUALITY_LEDGER_PATH = Path.home() / ".local" / "log" / "claude-task-quality.jsonl"

VALID_KERNEL_REASONS = {"FRAME", "NOT-NOTICING"}
VALID_DELIVERY_KINDS = {"bracket_tag", "agentctl_construct", "structured_hook", "harness"}
HEADING_PREFIXES = ("#### ", "### ", "## ", "# ")


def load_registry(path: Path = REGISTRY_PATH) -> list[dict]:
    with path.open("rb") as f:
        data = tomllib.load(f)
    return data.get("rule", [])


def load_claude_md(path: Path = CLAUDE_MD_PATH) -> str:
    return path.read_text()


# ---------------------------------------------------------------------------
# --check-registry : two-directional drift gate
# ---------------------------------------------------------------------------

def registry_schema_errors(rules: list[dict]) -> list[str]:
    errors = []
    seen_ids: set[str] = set()
    for i, r in enumerate(rules):
        where = f"rule[{i}] (id={r.get('id')!r})"
        rid = r.get("id")
        if not rid:
            errors.append(f"{where}: missing id")
            continue
        if rid in seen_ids:
            errors.append(f"duplicate id: {rid!r}")
        seen_ids.add(rid)

        tier = r.get("tier")
        if not isinstance(tier, int) or not (0 <= tier <= 4):
            errors.append(f"{where}: tier must be an int 0-4, got {tier!r}")

        kernel_reason = r.get("kernel_reason", "")
        if tier == 0:
            if kernel_reason not in VALID_KERNEL_REASONS:
                errors.append(
                    f"{where}: tier 0 requires kernel_reason in {sorted(VALID_KERNEL_REASONS)}, got {kernel_reason!r}"
                )
        else:
            if kernel_reason:
                errors.append(f"{where}: tier {tier} must not set kernel_reason (got {kernel_reason!r})")
            delivery_kind = r.get("delivery_kind", "")
            if delivery_kind not in VALID_DELIVERY_KINDS:
                errors.append(
                    f"{where}: tier {tier} requires delivery_kind in {sorted(VALID_DELIVERY_KINDS)}, got {delivery_kind!r}"
                )
            if delivery_kind in ("bracket_tag", "agentctl_construct") and not r.get("delivery_marker"):
                errors.append(f"{where}: delivery_kind={delivery_kind!r} requires a non-empty delivery_marker")

        if not r.get("locator_heading"):
            errors.append(f"{where}: missing locator_heading")
        if not r.get("locator_phrase"):
            errors.append(f"{where}: missing locator_phrase")
    return errors


def direction_b_drift(rules: list[dict], claude_md: str) -> list[str]:
    """Every registry entry's locator must still be verbatim-findable."""
    errors = []
    lines = set(claude_md.splitlines())
    for r in rules:
        rid = r.get("id", "<unknown>")
        heading = r.get("locator_heading", "")
        phrase = r.get("locator_phrase", "")
        if heading and heading not in lines:
            errors.append(f"{rid}: locator_heading not found as a line in CLAUDE.md: {heading!r}")
        if phrase and phrase not in claude_md:
            errors.append(f"{rid}: locator_phrase not found in CLAUDE.md: {phrase!r}")
    return errors


def _is_pure_container(lines: list[str], heading_idx: int) -> bool:
    """A heading is a pure container - exempt from needing its own registry
    entry - when every line between it and the next heading (any level) is
    blank. Such a heading only frames its subsections; the rule units live in
    those subsections' own entries."""
    for line in lines[heading_idx + 1 :]:
        stripped = line.strip()
        if not stripped:
            continue
        if any(stripped.startswith(p) for p in HEADING_PREFIXES):
            return True
        return False
    return True


def direction_a_drift(rules: list[dict], claude_md: str) -> list[str]:
    """Every heading in CLAUDE.md that owns rule-bearing body text must be
    referenced by at least one registry entry; a heading with body text and
    zero registry entries is unregistered content. A pure-container heading
    (only framing subsections, no body text of its own) is exempt. Best-effort:
    only checks headings, since sub-heading rule units (bold leads, bullets)
    aren't independently locatable from the markdown structure alone."""
    errors = []
    registered_headings = {r.get("locator_heading") for r in rules}
    lines = claude_md.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not any(stripped.startswith(p) for p in HEADING_PREFIXES):
            continue
        if _is_pure_container(lines, i):
            continue
        if stripped not in registered_headings:
            errors.append(f"unregistered heading: {stripped!r}")
    return errors


def check_registry(rules: list[dict], claude_md: str) -> list[str]:
    errors = registry_schema_errors(rules)
    errors += direction_b_drift(rules, claude_md)
    errors += direction_a_drift(rules, claude_md)
    return errors


# ---------------------------------------------------------------------------
# Transcript scanning (mirrors skill-usage-audit.py's iteration shape)
# ---------------------------------------------------------------------------

def parse_ts(s: str) -> dt.datetime | None:
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        return None


def iter_messages(jsonl: Path):
    try:
        with jsonl.open() as f:
            for line in f:
                try:
                    yield json.loads(line)
                except Exception:
                    continue
    except OSError:
        return


def _message_texts(m: dict):
    """Yield every searchable text blob in one transcript line: plain-string
    content, text parts, and tool_use inputs (as JSON so Bash command strings
    are included)."""
    msg = m.get("message", {}) if isinstance(m.get("message"), dict) else {}
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                yield part.get("text", "")
            elif part.get("type") == "tool_use":
                yield json.dumps(part.get("input") or {})
            elif part.get("type") == "tool_result":
                pc = part.get("content")
                if isinstance(pc, str):
                    yield pc
                elif isinstance(pc, list):
                    for sub in pc:
                        if isinstance(sub, dict) and sub.get("type") == "text":
                            yield sub.get("text", "")


def scan_session_for_markers(jsonl: Path, markers: set[str], cutoff: dt.datetime | None):
    """Return (in_window: bool, counts: dict[marker, int]) for one transcript."""
    counts: dict[str, int] = defaultdict(int)
    in_window = False
    for m in iter_messages(jsonl):
        ts = parse_ts(m.get("timestamp") or "")
        if ts and cutoff and ts < cutoff:
            continue
        in_window = True
        for text in _message_texts(m):
            if not text:
                continue
            for marker in markers:
                if marker in text:
                    counts[marker] += text.count(marker)
    return in_window, counts


def find_transcripts() -> list[Path]:
    if not PROJECTS_ROOT.exists():
        return []
    return sorted(PROJECTS_ROOT.glob("*/*.jsonl"))


def scan_transcripts(rules: list[dict], cutoff: dt.datetime | None, transcripts: list[Path] | None = None):
    """Return (sessions_scanned, firing_counts, sessions_with_firing) across
    all searchable markers in the registry."""
    markers = {
        r["delivery_marker"]
        for r in rules
        if r.get("delivery_kind") in ("bracket_tag", "agentctl_construct") and r.get("delivery_marker")
    }
    if transcripts is None:
        transcripts = find_transcripts()

    sessions_scanned = 0
    firing_counts: dict[str, int] = defaultdict(int)
    sessions_with_firing: dict[str, int] = defaultdict(int)

    for jsonl in transcripts:
        in_window, counts = scan_session_for_markers(jsonl, markers, cutoff)
        if not in_window:
            continue
        sessions_scanned += 1
        for marker, n in counts.items():
            firing_counts[marker] += n
            sessions_with_firing[marker] += 1

    return sessions_scanned, firing_counts, sessions_with_firing


# ---------------------------------------------------------------------------
# Ledger-based session denominator (supplementary; report degrades gracefully
# when absent)
# ---------------------------------------------------------------------------

def count_ledger_sessions(path: Path, cutoff: dt.datetime | None) -> int | None:
    """Distinct session_id/session count in a JSONL ledger within the window.
    Returns None if the ledger file is absent (graceful degradation)."""
    if not path.exists():
        return None
    seen: set[str] = set()
    for m in iter_messages(path):
        ts = parse_ts(m.get("ts") or "")
        if ts and cutoff and ts < cutoff:
            continue
        sid = m.get("session_id") or m.get("session") or m.get("task_id")
        if sid:
            seen.add(sid)
    return len(seen)


def load_ledger_rows(path: Path, cutoff: dt.datetime | None) -> list[dict] | None:
    """All rows of a JSONL ledger within the window, or None if the ledger file
    is absent (graceful degradation)."""
    if not path.exists():
        return None
    rows = []
    for m in iter_messages(path):
        ts = parse_ts(m.get("ts") or "")
        if ts and cutoff and ts < cutoff:
            continue
        rows.append(m)
    return rows


def _get_path(d: dict, dotted: str):
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


_OPS = {
    ">": lambda a, b: a is not None and a > b,
    ">=": lambda a, b: a is not None and a >= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def eval_trigger_proxy(rows: list[dict] | None, field: str, op: str, value) -> bool | None:
    """Evaluate whether ANY ledger row within the scanned window satisfies the
    rule's coded trigger-occurrence predicate.

    Returns True  - the trigger condition is positively evidenced (at least
                     one row satisfies the predicate).
    Returns False - the ledger was available and every row was checked, and
                     none satisfied the predicate: positive evidence the
                     trigger did NOT occur.
    Returns None  - no proxy is wired up for this rule, or the ledger is
                     absent: we have no independent evidence either way
                     (graceful degradation to the unrefined NEVER-OBSERVED
                     default).
    """
    if not field or rows is None:
        return None
    check = _OPS.get(op)
    if check is None:
        return None
    return any(check(_get_path(row, field), value) for row in rows)


# ---------------------------------------------------------------------------
# Three-state classification — never collapsed
# ---------------------------------------------------------------------------

def classify_state(
    rule: dict, sessions_scanned: int, fired: bool, trigger_status: bool | None = None
) -> tuple[str, str]:
    """Return (state, reason).

    NEVER-OBSERVED : the delivery mechanism IS instrumented (a marker exists
                      and was searched for) and at least one session was
                      scanned, but the marker never appeared. Either no
                      independent trigger-occurrence proxy is coded for this
                      rule (trigger_status is None), or the proxy positively
                      shows the trigger DID occur somewhere in the window yet
                      the mechanism still never fired (trigger_status is
                      True) - the more actionable of the two, called out
                      separately in the reason text.
    TRIGGER-ABSENT  : an independent, coded proxy (trigger_status is False)
                      positively shows the rule's trigger condition itself
                      never occurred anywhere in the scanned window - a
                      claim stronger than "marker absent", drawn from ledger
                      fields unrelated to the delivery marker itself.
    UNINSTRUMENTED  : no positive-firing signal exists at all for this entry
                      (tier 0 kernel prose with no delivery mechanism;
                      structured_hook with no bracket tag and no reliable
                      per-gate ledger field; harness-level enforcement; or
                      zero transcripts were available to scan).
    """
    tier = rule.get("tier")
    delivery_kind = rule.get("delivery_kind", "")

    if tier == 0:
        return "UNINSTRUMENTED", "tier-0 kernel prose has no separate firing marker"
    if delivery_kind == "structured_hook":
        return (
            "UNINSTRUMENTED",
            "PreToolUse structured-decision hook with no bracket tag; no positive "
            "ALLOW-path firing signal is logged anywhere on this machine (the "
            "denials ledger tracks a different, retired gate and carries no "
            "per-gate name field)",
        )
    if delivery_kind == "harness":
        return "UNINSTRUMENTED", "harness-level enforcement has no session-transcript marker"
    if sessions_scanned == 0:
        return "UNINSTRUMENTED", "no transcripts were available to scan in the window"
    if fired:
        return "OBSERVED", ""
    if trigger_status is False:
        return (
            "TRIGGER-ABSENT",
            f"trigger-occurrence proxy checked across {sessions_scanned} session(s) and never "
            "satisfied; the rule's trigger condition itself did not occur in this window",
        )
    if trigger_status is True:
        return (
            "NEVER-OBSERVED",
            f"trigger-occurrence proxy confirms the trigger DID occur in the window, "
            f"but the marker never appeared across {sessions_scanned} session(s) scanned",
        )
    return "NEVER-OBSERVED", f"mechanism instrumented and {sessions_scanned} session(s) scanned; marker never appeared"


def build_report_rows(
    rules: list[dict],
    sessions_scanned: int,
    firing_counts: dict[str, int],
    sessions_with_firing: dict[str, int],
    trigger_ledger_rows: list[dict] | None = None,
) -> list[dict]:
    rows = []
    for r in rules:
        marker = r.get("delivery_marker", "")
        count = firing_counts.get(marker, 0) if marker else 0
        swf = sessions_with_firing.get(marker, 0) if marker else 0
        trigger_status = eval_trigger_proxy(
            trigger_ledger_rows,
            r.get("trigger_proxy_field", ""),
            r.get("trigger_proxy_op", ""),
            r.get("trigger_proxy_value"),
        )
        state, reason = classify_state(r, sessions_scanned, fired=count > 0, trigger_status=trigger_status)
        rows.append(
            {
                "id": r["id"],
                "tier": r.get("tier"),
                "delivery_kind": r.get("delivery_kind", ""),
                "delivery_marker": marker,
                "firing_count": count,
                "sessions_with_firing": swf,
                "sessions_scanned": sessions_scanned,
                "state": state,
                "reason": reason,
            }
        )
    rows.sort(key=lambda row: (row["state"] != "OBSERVED", -row["firing_count"], row["id"]))
    return rows


def render_report(rows: list[dict], sessions_scanned: int, denom_source: str) -> str:
    lines = []
    lines.append(f"# Rule salience report ({sessions_scanned} session(s) scanned, denominator: {denom_source})\n")
    lines.append("| id | tier | state | firing | sessions w/ firing / scanned | reason |")
    lines.append("|---|---:|---|---:|---|---|")
    for row in rows:
        ratio = f"{row['sessions_with_firing']}/{row['sessions_scanned']}"
        lines.append(
            f"| `{row['id']}` | {row['tier']} | {row['state']} | {row['firing_count']} | {ratio} | {row['reason']} |"
        )
    counts = defaultdict(int)
    for row in rows:
        counts[row["state"]] += 1
    lines.append("")
    lines.append(
        "Summary: "
        + ", ".join(f"{state}={n}" for state, n in sorted(counts.items()))
        + f" (total {len(rows)})"
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check-registry", action="store_true", help="Run the two-directional drift gate and exit")
    ap.add_argument("--days", type=int, default=90, help="Scan window size in days (default 90)")
    ap.add_argument("--registry", default=str(REGISTRY_PATH), help="Path to rule-registry.toml")
    ap.add_argument("--claude-md", default=str(CLAUDE_MD_PATH), help="Path to CLAUDE.md")
    args = ap.parse_args(argv)

    registry_path = Path(args.registry)
    claude_md_path = Path(args.claude_md)
    rules = load_registry(registry_path)
    claude_md = load_claude_md(claude_md_path)

    if args.check_registry:
        errors = check_registry(rules, claude_md)
        if errors:
            print(f"Registry drift detected ({len(errors)} issue(s)):", file=sys.stderr)
            for e in errors:
                print(f"  - {e}", file=sys.stderr)
            return 1
        print(f"OK: {len(rules)} registry entries, no drift against {claude_md_path}")
        return 0

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)
    transcripts = find_transcripts()
    if not transcripts:
        print("No session transcripts found; report is empty (no --check-registry errors implied).")
        rows = build_report_rows(rules, 0, {}, {})
        print(render_report(rows, 0, denom_source="no transcripts available"))
        return 0

    sessions_scanned, firing_counts, sessions_with_firing = scan_transcripts(rules, cutoff, transcripts)

    denom_source = "transcript scan"
    policy_sessions = count_ledger_sessions(POLICY_LEDGER_PATH, cutoff)
    if policy_sessions is not None:
        denom_source = f"transcript scan; claude-policy-ledger.jsonl reports {policy_sessions} session(s) in window"
    else:
        quality_sessions = count_ledger_sessions(QUALITY_LEDGER_PATH, cutoff)
        if quality_sessions is not None:
            denom_source = f"transcript scan; claude-task-quality.jsonl reports {quality_sessions} session(s) in window"
        else:
            denom_source = "transcript scan only (both supplementary ledgers absent)"

    trigger_ledger_rows = load_ledger_rows(POLICY_LEDGER_PATH, cutoff)
    rows = build_report_rows(rules, sessions_scanned, firing_counts, sessions_with_firing, trigger_ledger_rows)
    print(render_report(rows, sessions_scanned, denom_source))
    return 0


if __name__ == "__main__":
    sys.exit(main())
