#!/usr/bin/env python3
"""SessionStart hook: run the periodic due checks and surface what each finds.

Nine standing checks used to be nine SessionStart hooks — curator
offer, self-diagnose, phase3 readiness, promote-scan, policy-scorecard ledger
refresh, budget calibration, sigma sentinel, improvement-scan count and
judge-ceiling drift — so every session start launched nine interpreters and the
registry carried nine rows. This hook owns them all. Each check keeps its own
throttle stamp under ~/.local/state/ and its own interval (DUE_INTERVALS_S),
runs its read-only producer as a bounded subprocess and prints to stderr; the
grooming offer goes to stdout because it is an instruction to the model.

The checks share one TOTAL_BUDGET_S wall-clock budget. A check that cannot start
inside it, or whose producer is missing or killed by it, keeps its stale stamp, so
it runs again at the next session start.

Fail-open: an error in one check is swallowed and the next check still runs.
Exit status is always 0.

  --dry-run    report only: ignores the throttle, writes no stamp and no store row,
               and does not launch the scorecard refresh.
  --force-run  ignores the throttle and writes no throttle stamp; the phase3
               baseline is still established when absent.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

try:
    import self_diagnose_store as store
except Exception:
    store = None

DUE_INTERVALS_S = {
    "claude-curator.stamp.json": 7 * 86400,
    "claude-self-diagnose.stamp": 24 * 3600,
    "claude-phase3-due.stamp": 7 * 86400,
    "claude-promote-scan.stamp": 7 * 86400,
    "claude-policy-scorecard.stamp": 7 * 86400,
    "claude-budget-calibration.stamp": 7 * 86400,
    "claude-sigma-sentinel.stamp": 30 * 86400,
    "claude-improvement-scan.stamp": 7 * 86400,
    "claude-judge-ceiling-drift.stamp": 7 * 86400,
}
PHASE3_BASELINE_STAMP = "claude-phase3-baseline.stamp"

TOTAL_BUDGET_S = 20.0
MIN_SLICE_S = 1.0
MAX_PRINTED_LINES = 10

WARN_RE = re.compile(r"^lint-prose-length: WARN — ([^:]+): .*?(\d+)% of limit")
PHASE3_DUE_PREFIX = "phase3-readiness: DUE"
JUDGE_BUDGET_ENV = "CLAUDE_SAME_DIFFICULTY_JUDGE_BUDGET_S"
SCORECARD_WINDOW_DAYS = 7


def state_path(name: str) -> Path:
    return Path.home() / ".local" / "state" / name


def read_stamp(name: str) -> "float | None":
    try:
        raw = state_path(name).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        return dt.datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def record_due_stamp(name: str, ts: float) -> None:
    try:
        path = state_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(repr(ts), encoding="utf-8")
    except OSError:
        pass


def err(text: str) -> None:
    print(text, file=sys.stderr)


class Run:
    def __init__(self, dry_run: bool, force: bool, budget_s: float):
        self.dry_run = dry_run
        self.writes_stamps = not (dry_run or force)
        self.now = time.time()
        self.deadline = time.monotonic() + budget_s
        self.produced = False

    def left(self) -> float:
        return self.deadline - time.monotonic()

    def due(self, stamp: str) -> bool:
        if not self.writes_stamps:
            return True
        prev = read_stamp(stamp)
        return prev is None or self.now - prev >= DUE_INTERVALS_S[stamp]

    def age_days(self, stamp: str) -> "float | None":
        prev = read_stamp(stamp)
        return None if prev is None else (self.now - prev) / 86400

    def record(self, stamp: str) -> None:
        if self.writes_stamps:
            record_due_stamp(stamp, self.now)

    def record_produced(self, stamp: str) -> None:
        """Stamp only when the last produce() ran to completion: a producer that
        was missing, failed to start or was killed by the budget keeps its stale
        stamp, so the check runs again at the next session start."""
        if self.produced:
            self.record(stamp)

    def produce(self, script: Path, *args: str, cap_s: float,
                env: "dict | None" = None) -> "subprocess.CompletedProcess | None":
        self.produced = False
        if not script.is_file():
            return None
        try:
            proc = subprocess.run(
                [sys.executable, str(script), *args],
                capture_output=True, text=True, timeout=min(cap_s, self.left()), env=env,
            )
        except Exception:
            return None
        self.produced = True
        return proc


def repo_root() -> Path:
    return Path(os.environ.get("CLAUDE_INSTRUCTIONS_REPO", str(Path.home() / "claude-agent-instructions")))


def load_grooming_stamps(stamp: str) -> "dict[str, str]":
    try:
        raw = json.loads(state_path(stamp).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def grooming_file_due(stamps: "dict[str, str]", file: str, now: float, interval_s: float) -> bool:
    try:
        prev = dt.datetime.fromisoformat(stamps[file]).timestamp()
    except (KeyError, ValueError, TypeError):
        return True
    return now - prev >= interval_s


def check_grooming(run: Run, stamp: str) -> None:
    proc = run.produce(repo_root() / "scripts" / "lint-prose-length.py", cap_s=5)
    lines = proc.stdout.splitlines() if proc and proc.stdout else []
    warnings = [m.groups() for m in map(WARN_RE.match, lines) if m]
    if not warnings:
        return
    stamps = load_grooming_stamps(stamp)
    interval_s = DUE_INTERVALS_S[stamp]
    due = [(f, pct) for f, pct in warnings
           if not run.writes_stamps or grooming_file_due(stamps, f, run.now, interval_s)]
    if not due:
        return
    if run.writes_stamps:
        stamped = dt.datetime.fromtimestamp(run.now).isoformat()
        stamps.update({f: stamped for f, _ in due})
        try:
            path = state_path(stamp)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(stamps), encoding="utf-8")
        except OSError:
            pass
    parts = [f"{f} ({pct}% of ceiling)" for f, pct in due]
    print(
        "[curator] " + ", ".join(parts) + " past the WARN threshold. "
        "OFFER the `curator` skill (instructions mode) to the user via AskUserQuestion before "
        "running it — grooming edits still go through the normal plan-approval spine."
    )


def scan_self_diagnose(run: Run) -> "list[dict] | None":
    """The scanner's worklist, or None when the scan did not COMPLETE.

    None and [] must stay distinct: [] is a genuinely clean tree and resolves every
    stored row out, so a failed scan reported as [] would wipe the store's
    first_seen, times_surfaced and acks on one hiccup."""
    proc = run.produce(SCRIPTS_DIR / "self-diagnose.py", "--json", cap_s=15)
    if proc is None or proc.returncode not in (0, 1) or not (proc.stdout or "").strip():
        return None
    try:
        records = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(records, list):
        return None
    return [r for r in records if isinstance(r, dict) and r.get("kind")]


def persist_findings(findings: "list[dict]") -> "list[dict]":
    if store is None:
        return []
    try:
        rows = store.upsert_findings(findings)
        store.route_advisory(rows)
        return rows
    except Exception:
        return []


def report_self_diagnose(findings: "list[dict]", rows: "list[dict]") -> None:
    if not findings:
        return
    err(
        f"self-diagnose: {len(findings)} self-friction item(s) found — this is a "
        "STANDING difficulty (CLAUDE.md § When the work is stuck): work it through "
        "overcome-difficulty proactively, don't wait for the user to notice."
    )
    for record in findings[:MAX_PRINTED_LINES]:
        err(f"  - {record.get('kind')}: {record.get('path')} — {record.get('detail')}")
    remaining = len(findings) - MAX_PRINTED_LINES
    if remaining > 0:
        err(f"  ... and {remaining} more (run scripts/self-diagnose.py for the full list)")
    if rows and store is not None:
        try:
            blocking = store.open_actionable(rows)
            debounced = store.debounced_actionable(rows)
            advisory = store.advisory_open(rows)
        except Exception:
            return
        err(
            f"  ({len(blocking)} actionable will re-surface at the turn boundary until "
            f"closed; {len(debounced)} actionable still inside the "
            f"{store.ACTIONABLE_MIN_AGE_DAYS}d debounce; {len(advisory)} advisory in the "
            "digest — scripts/self_diagnose_store.py --list)"
        )


def check_self_diagnose(run: Run, stamp: str) -> None:
    if not run.due(stamp):
        return
    findings = scan_self_diagnose(run)
    if findings is not None:
        report_self_diagnose(findings, [] if run.dry_run else persist_findings(findings))
    run.record_produced(stamp)


def ensure_phase3_baseline(now: float) -> None:
    """Write-once: rule-salience-report.py reads this stamp but never writes it, and
    refreshing it would keep pushing Phase 3's eligibility forward."""
    try:
        path = state_path(PHASE3_BASELINE_STAMP)
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(), encoding="utf-8")
    except OSError:
        pass


def check_phase3(run: Run, stamp: str) -> None:
    if not run.dry_run:
        ensure_phase3_baseline(run.now)
    if not run.due(stamp):
        return
    proc = run.produce(SCRIPTS_DIR / "rule-salience-report.py", "--check-due", cap_s=15)
    verdict = (proc.stdout or "").strip() if proc else ""
    run.record_produced(stamp)
    if not verdict.startswith(PHASE3_DUE_PREFIX):
        return
    lines = verdict.splitlines()
    err(
        "[phase3-due] Phase 3 (instruction-surface compression) is DUE — resume "
        "~/.claude-agent/plans/instruction-surface-governance.toml stages 5-8. "
        "Verdict below (OBSERVED tier>=1 prose only; NEVER-OBSERVED is a risk "
        "signal about the delivery mechanism, never a licence to compress):"
    )
    for line in lines[:MAX_PRINTED_LINES]:
        err(f"  {line}")
    remaining = len(lines) - MAX_PRINTED_LINES
    if remaining > 0:
        err(f"  ... ({remaining} more line(s); run `scripts/rule-salience-report.py --check-due` for the full verdict)")


def scan_promote(run: Run) -> "dict | None":
    """promote-scan with a zero judge budget: a session start never waits on a model,
    so pairs the verdict cache has not decided are counted as unjudged. None means
    the scan failed; a clean empty cluster list is a different answer."""
    proc = run.produce(
        SCRIPTS_DIR / "record-experience.py", "promote-scan", "--scope", "global", "--json",
        cap_s=20, env={**os.environ, JUDGE_BUDGET_ENV: "0"},
    )
    if proc is None or proc.returncode != 0 or not (proc.stdout or "").strip():
        return None
    try:
        scan = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(scan, dict) or not isinstance(scan.get("clusters"), list):
        return None
    return scan


def report_promote(scan: dict) -> None:
    unjudged = (scan.get("stats") or {}).get("unjudged_items")
    if isinstance(unjudged, int) and unjudged:
        err(
            f"promote-scan: {unjudged} experience leaf(s) unjudged (cache-only run) — "
            "clusters may be under-merged; run `scripts/record-experience.py promote-scan` "
            "for a judged pass."
        )
    flagged = [c for c in scan["clusters"] if c.get("flagged")]
    if not flagged:
        return
    err(
        f"promote-scan: {len(flagged)} regularity-induction candidate(s) at or above "
        "principle-promotion-threshold — lift each into a regularity/v1 leaf via "
        "`scripts/record-experience.py promote-scan` (config.md § principle-promotion-threshold)."
    )
    for cluster in flagged[:MAX_PRINTED_LINES]:
        err(f"  [{cluster.get('occurrences_total', '?')} occurrence(s)] {', '.join(cluster.get('members', []))}")
    remaining = len(flagged) - MAX_PRINTED_LINES
    if remaining > 0:
        err(f"  ... and {remaining} more (run scripts/record-experience.py promote-scan)")


def check_promote_scan(run: Run, stamp: str) -> None:
    if not run.due(stamp):
        return
    scan = scan_promote(run)
    if scan is not None:
        report_promote(scan)
    run.record_produced(stamp)


def launch_scorecard(cmd: "list[str]") -> None:
    from proc_tree import launch_supervised
    launch_supervised(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)


def scorecard_upsert_cmd() -> "list[str]":
    cmd = [sys.executable, str(SCRIPTS_DIR / "policy-scorecard.py"), "--ledger-only",
           "--days", str(SCORECARD_WINDOW_DAYS), "--no-judge"]
    ledger = os.environ.get("CLAUDE_POLICY_LEDGER")
    return cmd + ["--ledger", ledger] if ledger else cmd


def check_policy_scorecard(run: Run, stamp: str) -> None:
    if run.dry_run or not run.due(stamp):
        return
    days = run.age_days(stamp)
    ago = "first refresh" if days is None else f"last refreshed {days:.0f}d ago"
    try:
        launch_scorecard(scorecard_upsert_cmd())
    except Exception:
        pass
    run.record(stamp)
    err(
        f"📊 policy-scorecard ledger refreshed in the background — review with "
        f"`scripts/policy-scorecard.py --days 7` (efficiency + effectiveness), "
        f"then rate a few flagged sessions. ({ago})"
    )


def check_budget_calibration(run: Run, stamp: str) -> None:
    if not run.due(stamp):
        return
    proc = run.produce(SCRIPTS_DIR / "budget-calibration.py", "--check", cap_s=10)
    flag = (proc.stdout or "").strip() if proc else ""
    run.record_produced(stamp)
    if flag:
        err(
            f"💵 Budget tiers may be miscalibrated: {flag}. "
            f"Run `scripts/budget-calibration.py` and, if confirmed, self-improvement "
            f"to adjust the config.md tier values."
        )


def check_sigma_sentinel(run: Run, stamp: str) -> None:
    if not run.due(stamp):
        return
    days = run.age_days(stamp)
    ago = "first reminder" if days is None else f"last checked {days:.0f}d ago"
    run.record(stamp)
    err(
        f"🛡  σ build-trigger sentinel due — run `scripts/sigma-sentinel.py` "
        f"to check whether condition (A) (re-refutation of a promoted principle) has "
        f"fired. Read-only; flags only. See docs/sigma-build-trigger.md. ({ago})"
    )


def open_improvement_findings() -> "list[dict] | None":
    """Open improvement-scan rows already in the durable store. None only when the
    store module is unavailable or unreadable — never an empty-means-missing answer.
    This check counts stored findings; running the scan stays the skill's job."""
    if store is None:
        return None
    try:
        sourced = [r for r in store.load_rows() if r.get("source") == store.SOURCE_IMPROVEMENT_SCAN]
        return store.advisory_open(sourced)
    except Exception:
        return None


def check_improvement_scan(run: Run, stamp: str) -> None:
    if not run.due(stamp):
        return
    findings = open_improvement_findings()
    if findings:
        err(
            f"improvement-scan: {len(findings)} open finding(s) from a prior scan — "
            "invoke the `improvement-scan` skill to review them, re-run the "
            "producers, and get a fresh ranked report (this hook only counts "
            "stored findings; it never runs the scan itself)."
        )
    run.record(stamp)


def check_judge_ceiling_drift(run: Run, stamp: str) -> None:
    if not run.due(stamp):
        return
    proc = run.produce(SCRIPTS_DIR / "judge-usage-report.py", "--check-drift", cap_s=15)
    drift = (proc.stdout or "").strip() if proc and proc.returncode != 0 else ""
    run.record_produced(stamp)
    if drift:
        err(
            "\N{WARNING SIGN} A judge's live latency has drifted up to meet its "
            "declared ceiling:\n" + drift + "\nRe-sample and re-derive the row "
            "(samples/judge-latency), then re-raise the ceiling."
        )


CHECKS = [
    ("claude-sigma-sentinel.stamp", check_sigma_sentinel),
    ("claude-improvement-scan.stamp", check_improvement_scan),
    ("claude-policy-scorecard.stamp", check_policy_scorecard),
    ("claude-curator.stamp.json", check_grooming),
    ("claude-budget-calibration.stamp", check_budget_calibration),
    ("claude-judge-ceiling-drift.stamp", check_judge_ceiling_drift),
    ("claude-phase3-due.stamp", check_phase3),
    ("claude-self-diagnose.stamp", check_self_diagnose),
    ("claude-promote-scan.stamp", check_promote_scan),
]


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="report only; never writes a stamp or a store row")
    parser.add_argument("--force-run", action="store_true", help="ignore the throttle without consuming it")
    args = parser.parse_args(argv)

    run = Run(args.dry_run, args.force_run, TOTAL_BUDGET_S)
    for stamp, check in CHECKS:
        if run.left() < MIN_SLICE_S:
            break
        try:
            check(run, stamp)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
