#!/usr/bin/env python3
"""Turn a policy-scorecard quality-degradation flag into an actionable shortlist.

`policy-scorecard.py` flags task-quality degradation and prints a good/bad
instruction-commit range (`_commit_range_lines`, § Task quality). That range by
itself doesn't say WHICH commit is the likely cause. This script lists every
commit in `<good>..<bad>` that touches a salience-bearing path (`CLAUDE.md`,
`config.md`, `skills/`, `agents/`, `memory-global/MEMORY.md`,
`memory-global/leaves/`, `scripts/hook-*`, `scripts/agentctl/`), tags each with
a heuristic (prose-removed / rule-moved / mechanized), and ranks the largest
net prose deletion first -- the failure-mode prior behind this whole tracking
effort is that salience lives in prose, so a prose-shrinking commit is the
first suspect.

Modes:
  quality-regression-investigate.py --good <rev> --bad <rev>
      Explicit range.
  quality-regression-investigate.py --good-days A --bad-days B
      Resolve the range from the task-quality ledger
      (~/.local/log/claude-task-quality.jsonl, written by
      `agentctl resolve --quality`): the "bad" window is the last B days
      (ts in [now-B, now]), the "good" window is the A days immediately
      before it (ts in [now-A-B, now-B)). `good` = the instructions_head of
      the earliest-ts row in the good window; `bad` = the instructions_head
      of the latest-ts row in the bad window.

Suggested flow once the scorecard flag fires:
  1. Run this script with the flagged range (or an equivalent --*-days window).
  2. Read the ranked commits top-down as ordered hypotheses.
  3. Fix per the ladder: mechanize the rule > restore lost salience without
     growing CLAUDE.md/config.md > only then re-add prose, within the
     claude-md-max-lines / claude-md-max-chars ceilings.
  4. Runbook: memory-global/leaves/quality-regression-investigation.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TASK_QUALITY_LEDGER = Path.home() / ".local" / "log" / "claude-task-quality.jsonl"
RUNBOOK_LEAF = "memory-global/leaves/quality-regression-investigation.md"
_GIT_TIMEOUT_S = 10

# Salience-bearing pathspecs, as passed to `git log/show -- <pathspecs>`.
SALIENCE_PATHSPECS = [
    "CLAUDE.md",
    "config.md",
    "skills",
    "agents",
    "memory-global/MEMORY.md",
    "memory-global/leaves",
    ":(glob)scripts/hook-*",
    "scripts/agentctl",
]
# Subset whose net line deletions feed the prose-removed heuristic / ranking --
# scripts/hook-* and scripts/agentctl are code, not the prose that carries
# salience, so they are excluded here even though they're still listed
# (tagged mechanized) in SALIENCE_PATHSPECS.
PROSE_PATHSPECS = [
    "CLAUDE.md",
    "config.md",
    "skills",
    "agents",
    "memory-global/MEMORY.md",
    "memory-global/leaves",
]
MECHANIZED_PREFIXES = ("scripts/hook-", "scripts/agentctl/")


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=_GIT_TIMEOUT_S, check=True,
    )
    return proc.stdout


def _touches_mechanized_path(paths: list[str]) -> bool:
    return any(p.startswith(MECHANIZED_PREFIXES) for p in paths)


class InvestigationError(RuntimeError):
    """Raised for any recoverable failure this script should report and exit
    non-zero on -- a failing git call or an unresolvable ledger window."""


def _is_prose_path(path: str) -> bool:
    return path in PROSE_PATHSPECS or any(
        path.startswith(pp + "/") for pp in PROSE_PATHSPECS)


def _commit_shas(repo: Path, good: str, bad: str) -> list[str]:
    """Commit SHAs in `good..bad` touching a salience-bearing path, oldest first."""
    try:
        out = _git(repo, "log", "--reverse", "--format=%H", f"{good}..{bad}",
                   "--", *SALIENCE_PATHSPECS)
    except subprocess.CalledProcessError as exc:
        raise InvestigationError(f"git log {good}..{bad} failed: {exc.stderr.strip()}") from exc
    return [line for line in out.splitlines() if line.strip()]


def _numstat(repo: Path, sha: str, pathspecs: list[str]) -> list[tuple[int, int, str]]:
    """[(insertions, deletions, path), ...] for `sha` restricted to `pathspecs`.
    Binary-file numstat rows ("-\t-\tpath") contribute (0, 0, path)."""
    out = _git(repo, "show", "--numstat", "--format=", sha, "--", *pathspecs)
    rows = []
    for line in out.splitlines():
        if not line.strip():
            continue
        ins, del_, path = line.split("\t", 2)
        rows.append((int(ins) if ins != "-" else 0, int(del_) if del_ != "-" else 0, path))
    return rows


def _name_status(repo: Path, sha: str, pathspecs: list[str]) -> list[tuple[str, list[str]]]:
    """[(status, [paths]), ...] for `sha` restricted to `pathspecs` (rename-aware)."""
    out = _git(repo, "show", "--name-status", "-M", "--format=", sha, "--", *pathspecs)
    rows = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        rows.append((parts[0], parts[1:]))
    return rows


def _inspect_commit(repo: Path, sha: str) -> dict:
    subject = _git(repo, "log", "-1", "--format=%s", sha).strip()
    numstat = _numstat(repo, sha, SALIENCE_PATHSPECS)
    name_status = _name_status(repo, sha, SALIENCE_PATHSPECS)

    total_ins = sum(ins for ins, _del, _p in numstat)
    total_del = sum(_del for _ins, _del, _p in numstat)
    prose_ins = sum(ins for ins, _del, p in numstat if _is_prose_path(p))
    prose_del = sum(_del for _ins, _del, p in numstat if _is_prose_path(p))
    net_prose_deletion = prose_del - prose_ins

    touched_paths = [p for _s, paths in name_status for p in paths]
    tags = []
    if net_prose_deletion > 0:
        tags.append("prose-removed")
    if any(status.startswith("R") for status, _paths in name_status):
        tags.append("rule-moved")
    if _touches_mechanized_path(touched_paths):
        tags.append("mechanized")

    return {
        "sha": sha,
        "subject": subject,
        "insertions": total_ins,
        "deletions": total_del,
        "net_prose_deletion": net_prose_deletion,
        "tags": tags,
    }


def investigate(repo: Path, good: str, bad: str) -> list[dict]:
    """Ranked (largest net prose deletion first) commit hypotheses in `good..bad`."""
    shas = _commit_shas(repo, good, bad)
    commits = [_inspect_commit(repo, sha) for sha in shas]
    commits.sort(key=lambda c: c["net_prose_deletion"], reverse=True)
    return commits


def format_report(commits: list[dict], good: str, bad: str) -> str:
    lines = [f"Range: {good[:12]}..{bad[:12]} ({len(commits)} salience-touching commit(s))", ""]
    if not commits:
        lines.append("(no commits in range touch a salience-bearing path)")
    for c in commits:
        tag_str = f" [{', '.join(c['tags'])}]" if c["tags"] else ""
        lines.append(
            f"  {c['sha'][:12]}  +{c['insertions']}/-{c['deletions']}"
            f"  net-prose-del={c['net_prose_deletion']:+d}{tag_str}  {c['subject']}"
        )
    lines.append("")
    lines.append(f"Runbook: {RUNBOOK_LEAF}")
    return "\n".join(lines)


# ------------------------------------------------------------- ledger window mode

def _load_quality_rows() -> list[dict]:
    if not TASK_QUALITY_LEDGER.exists():
        raise InvestigationError(
            f"task-quality ledger not found at {TASK_QUALITY_LEDGER} -- "
            "no task has resolved with `agentctl resolve --quality` yet; "
            "use --good/--bad instead"
        )
    rows = []
    for line in TASK_QUALITY_LEDGER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def resolve_window_range(good_days: float, bad_days: float,
                          now: dt.datetime | None = None) -> tuple[str, str]:
    """Resolve (good, bad) revs from the task-quality ledger. The bad window is
    the last `bad_days` days; the good window is the `good_days` days immediately
    before it. Raises InvestigationError with a clear message when the ledger is absent or
    no row in either window carries an instructions_head."""
    now = now or dt.datetime.now(dt.timezone.utc)
    bad_start = now - dt.timedelta(days=bad_days)
    good_start = bad_start - dt.timedelta(days=good_days)

    rows = _load_quality_rows()
    good_candidates = []
    bad_candidates = []
    for row in rows:
        head = row.get("instructions_head")
        ts_raw = row.get("ts")
        if not head or not ts_raw:
            continue
        try:
            ts = dt.datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if good_start <= ts < bad_start:
            good_candidates.append((ts, head))
        elif bad_start <= ts <= now:
            bad_candidates.append((ts, head))

    if not good_candidates:
        raise InvestigationError(
            f"no task-quality ledger row with an instructions_head falls in the "
            f"'good' window [{good_start.isoformat()}, {bad_start.isoformat()}) -- "
            "widen --good-days or use --good/--bad explicitly"
        )
    if not bad_candidates:
        raise InvestigationError(
            f"no task-quality ledger row with an instructions_head falls in the "
            f"'bad' window [{bad_start.isoformat()}, {now.isoformat()}] -- "
            "widen --bad-days or use --good/--bad explicitly"
        )
    good = min(good_candidates, key=lambda pair: pair[0])[1]
    bad = max(bad_candidates, key=lambda pair: pair[0])[1]
    return good, bad


# --------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--repo", type=Path, default=REPO_ROOT,
                        help="instructions repo to analyze (default: this script's own repo)")
    parser.add_argument("--good", help="good (pre-regression) revision")
    parser.add_argument("--bad", help="bad (post-regression) revision")
    parser.add_argument("--good-days", type=float,
                        help="size in days of the 'good' window, ending where --bad-days starts")
    parser.add_argument("--bad-days", type=float,
                        help="size in days of the 'bad' (most recent) window")
    args = parser.parse_args(argv)

    explicit = args.good is not None or args.bad is not None
    windowed = args.good_days is not None or args.bad_days is not None
    if explicit and windowed:
        parser.error("pass either --good/--bad or --good-days/--bad-days, not both")
    if explicit and (args.good is None or args.bad is None):
        parser.error("--good and --bad must be given together")
    if windowed and (args.good_days is None or args.bad_days is None):
        parser.error("--good-days and --bad-days must be given together")
    if not explicit and not windowed:
        parser.error("must pass --good/--bad or --good-days/--bad-days")

    try:
        if windowed:
            good, bad = resolve_window_range(args.good_days, args.bad_days)
        else:
            good, bad = args.good, args.bad
        commits = investigate(args.repo, good, bad)
    except InvestigationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(format_report(commits, good, bad))
    return 0


if __name__ == "__main__":
    sys.exit(main())
