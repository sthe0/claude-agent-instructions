#!/usr/bin/env python3
"""Where session wall-clock goes: decayed mean +/- SE per mechanism.

Reads session transcripts and the existing ledgers through lib/time_profile.py (cached
under $TIME_PROFILE_STATE_DIR, default ~/.local/state/time-profile/), weights each
top-level session by 2^(-age/half_life) and reports, per partition key (user wait, model
gap, each tool, exposed hooks) and per overlay key (engine node dwell, spawned
specialists, judges): minutes per session, minutes in sessions where the key occurs,
occurrence rate, share of wall-clock, share of agent-active time (wall-clock minus
user:wait and wait:background). Every estimate prints as `mean ± se (n_eff N)`;
`n/a` where fewer than two effective sessions leave no variance. Overlays run inside
partition intervals, so their shares do not sum to 1. The active share of user:wait and
wait:background is relative to a denominator that excludes them, so it can exceed 1 and
is not a share of anything.

  --half-life D     half-life in days (default: config.md `time-profile-half-life-days`)
  --by-sha          the same figures per Core sha
  --format json     machine-readable
  --uncertainty     per heavy component: reconstruction interval, cross-source agreement,
                    coverage and an instrument / wait verdict; --format md (default) | json;
                    also written to $TIME_PROFILE_STATE_DIR/uncertainty-report.{md,json};
                    excludes --check and --by-sha. The verdict that a board item's loss rank
                    flips within a component's interval activates once board items carry
                    `mechanism` (stage 4); the CLI passes no board until then
  --check           exit non-zero unless the data is sound: a session profiled, wall-clock
                    conserved on every one, every estimate complete, skipped fraction small
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import math
import os
import sys
import tempfile
from pathlib import Path

import improvement_scan_loss as loss
from lib import time_profile, time_profile_uncertainty

FIGURE_KEYS = frozenset({"mean", "se", "n_eff"})
FIGURES = (
    ("minutes_per_session", "min/session"),
    ("minutes_when_present", "min|present"),
    ("occurrence_rate", "occurrence"),
    ("share_of_wallclock", "wall share"),
    ("share_of_active", "active share"),
)
DEFAULT_MAX_SKIP_FRACTION = 0.25
JSON_DIGITS = 6
TEXT_DIGITS = 4


def _rounded(value):
    if isinstance(value, float):
        return float(f"{value:.{JSON_DIGITS}g}")
    if isinstance(value, dict):
        return {k: _rounded(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_rounded(v) for v in value]
    return value


def _payload_of(agg: time_profile.Aggregate, run) -> dict:
    return {
        **agg.to_dict(),
        "run": {
            "profiled": len(run.profiles),
            "skipped": [{"session": s.session_id, "reason": s.reason, "detail": s.detail} for s in run.skipped],
            "reparsed": list(run.reparsed),
        },
    }


def build_payload(run, half_life: float | None, by_sha: bool) -> dict:
    payload = _payload_of(time_profile.aggregate(run.profiles, half_life_days=half_life), run)
    if by_sha:
        payload["groups"] = [
            {"core_sha": sha, **group.to_dict()}
            for sha, group in time_profile.aggregate_by_sha(run.profiles, half_life_days=half_life).items()
        ]
    return payload


def format_figure(fig: dict) -> str:
    se = "n/a" if fig["se"] is None else f"{fig['se']:.{TEXT_DIGITS}g}"
    return f"{fig['mean']:.{TEXT_DIGITS}g} ± {se} (n_eff {fig['n_eff']:.1f})"


def _render_rows(rows: list) -> list:
    lines = []
    for row in rows:
        flag = f"  [{row['flag']}: not a mechanism]" if row.get("flag") else ""
        lines.append(f"{row['key']}  ({row['kind']}){flag}")
        lines.extend(f"    {label:<13}{format_figure(row[name])}" for name, label in FIGURES)
    return lines


def _render_block(block: dict) -> list:
    lines = [f"sessions: {block['sessions']}"]
    lines.extend(f"{name}: {format_figure(fig)}" for name, fig in block["totals"].items())
    lines.extend(f"children {c['kind']}: {c['sessions']} sessions, wall_minutes {format_figure(c['wall_minutes'])}"
                 for c in block["children"])
    return lines + _render_rows(block["rows"])


def render_text(payload: dict) -> str:
    run = payload["run"]
    lines = [
        f"time-profile: half-life {payload['half_life_days']:g} days, "
        f"profiled {run['profiled']}, skipped {len(run['skipped'])}",
    ]
    lines.extend(f"  skipped {s['session']}: {s['reason']}" + (f" ({s['detail']})" if s["detail"] else "")
                 for s in run["skipped"])
    lines.extend(_render_block(payload))
    for group in payload.get("groups", []):
        lines.append("")
        lines.append(f"core sha {group['core_sha']}")
        lines.extend(_render_block(group))
    return "\n".join(lines)


def rank_board(items: dict) -> dict:
    """Board rank per item ref, as improvement-scan's loss lanes order them."""
    return loss.lane_ranks(loss.build_lanes(items))


def uncertainty_payload(run, half_life: float | None) -> dict:
    agg = time_profile.aggregate(run.profiles, half_life_days=half_life)
    ctx = run.ctx or time_profile.Ctx()
    ctx = dataclasses.replace(ctx, rank_board=ctx.rank_board or rank_board)
    report = time_profile_uncertainty.uncertainty(agg, run.profiles, ctx)
    return {**report.to_dict(), "run": {"profiled": len(run.profiles), "skipped": len(run.skipped)}}


def _fmt(value) -> str:
    return "n/a" if value is None else f"{value:.{TEXT_DIGITS}g}"


def _fmt_interval(interval) -> str:
    return "n/a" if interval is None else f"[{_fmt(interval[0])}, {_fmt(interval[1])}]"


def _fmt_discrepancies(discrepancies: dict) -> str:
    if not discrepancies:
        return "none"
    return "; ".join(
        f"{pair} n={d['n']} median {_fmt(d['median'])} p90 {_fmt(d['p90'])}" for pair, d in sorted(discrepancies.items()))


def render_uncertainty_md(payload: dict) -> str:
    lines = [
        f"# time-profile uncertainty (half-life {payload['half_life_days']:g} days, {payload['sessions']} sessions)",
        "",
    ]
    for c in payload["components"]:
        verdict = c["verdict"] + (f" ({c['source']})" if c["source"] else "")
        lines.append(f"## {c['key']} ({c['kind']})")
        lines.append(f"- share of active: {format_figure(c['share_active'])}")
        lines.append(f"- min/session: {format_figure(c['per_session'])}")
        lines.append(f"- reconstruction interval: {_fmt_interval(c['interval'])}")
        lines.append(f"- combined interval: {_fmt_interval(c['combined'])}")
        lines.append(f"- cross-source: {_fmt_discrepancies(c['discrepancies'])}")
        lines.append(f"- coverage: {json.dumps(_rounded(c['coverage']), sort_keys=True)}")
        if c["flipped_items"]:
            lines.append(f"- board items whose loss rank flips: {', '.join(c['flipped_items'])}")
        lines.append(f"- verdict: {verdict}")
        lines.append("")
    cov = payload["coverage"]
    lines.append(f"exposed hook minutes ({cov['exposed_hook_label']}): {_fmt(cov['exposed_hook_minutes'])}")
    lines.append(f"untimed engine events: {_fmt(cov['node_untimed_share'])}")
    lines.append("")
    lines.append(f"overall: {payload['overall']}")
    return "\n".join(lines)


def _write_atomically(path: Path, text: str) -> None:
    """Temp file beside `path`, then rename: a reader never sees a half-written report."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def write_uncertainty_files(payload: dict, md: str) -> None:
    state_dir = time_profile.default_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    _write_atomically(state_dir / "uncertainty-report.json", json.dumps(_rounded(payload), indent=2, sort_keys=True) + "\n")
    _write_atomically(state_dir / "uncertainty-report.md", md + "\n")


def figure_problems(node, path: str = "") -> list:
    """Every way an estimate in `node` is incomplete: a figure object missing mean, se or
    n_eff, or with a null se although n_eff >= 2."""
    problems = []
    if isinstance(node, dict):
        if FIGURE_KEYS & node.keys():
            missing = sorted(FIGURE_KEYS - node.keys())
            if missing:
                problems.append(f"{path or '.'}: estimate lacks {', '.join(missing)}")
            elif node["se"] is None and node["n_eff"] >= 2:
                problems.append(f"{path or '.'}: se is null although n_eff = {node['n_eff']:.2f}")
        for key, value in node.items():
            problems.extend(figure_problems(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            problems.extend(figure_problems(value, f"{path}[{i}]"))
    return problems


def check(run, payload: dict, max_skip_fraction: float) -> list:
    problems = []
    if not run.profiles:
        problems.append("no session was profiled")
    problems.extend(
        f"{p.session_id}: partition does not sum to wall-clock (error {time_profile.conservation_error_ms(p)} ms)"
        for p in run.profiles if time_profile.conservation_error_ms(p) != 0
    )
    problems.extend(figure_problems(payload))
    total = len(run.profiles) + len(run.skipped)
    if total and len(run.skipped) / total > max_skip_fraction:
        problems.append(f"{len(run.skipped)} of {total} sessions skipped, above the allowed fraction {max_skip_fraction:g}")
    return problems


def main(argv: list[str] | None = None, *, run=None) -> int:
    parser = argparse.ArgumentParser(prog="agent-report.py time-profile", description=__doc__.splitlines()[0])
    parser.add_argument("--format", choices=("text", "md", "json"),
                        help="text (default), or md (default) / json with --uncertainty")
    parser.add_argument("--uncertainty", action="store_true",
                        help="report reconstruction bounds, cross-source agreement and the instrument / wait "
                             "verdict (not with --check or --by-sha); the board rank-flip verdict activates once "
                             "board items carry `mechanism` (stage 4)")
    parser.add_argument("--half-life", type=float, metavar="D", help="half-life in days (default: from config.md)")
    parser.add_argument("--by-sha", action="store_true", help="also group the figures per Core sha")
    parser.add_argument("--check", action="store_true", help="verify the data is sound; exit 1 if not")
    parser.add_argument("--max-skip-fraction", type=float, default=DEFAULT_MAX_SKIP_FRACTION,
                        help=f"--check fails above this skipped fraction (default {DEFAULT_MAX_SKIP_FRACTION})")
    args = parser.parse_args(argv)
    if args.half_life is not None and not (args.half_life > 0 and math.isfinite(args.half_life)):
        parser.error("--half-life must be a positive finite number")
    if args.uncertainty and (args.check or args.by_sha):
        parser.error("--uncertainty cannot be combined with --check or --by-sha")
    if args.uncertainty and args.format == "text":
        parser.error("--uncertainty renders --format md (default) or json")
    if args.format == "md" and not args.uncertainty:
        parser.error("--format md is only for --uncertainty")
    run = run if run is not None else time_profile.profile_all()
    if args.uncertainty:
        report = uncertainty_payload(run, args.half_life)
        md = render_uncertainty_md(report)
        write_uncertainty_files(report, md)
        print(json.dumps(_rounded(report), indent=2, sort_keys=True) if args.format == "json" else md)
        return 0
    payload = build_payload(run, args.half_life, args.by_sha)
    if args.check:
        problems = check(run, payload, args.max_skip_fraction)
        print(f"profiled: {len(run.profiles)}, skipped: {len(run.skipped)}")
        for skipped in run.skipped:
            print(f"  skipped {skipped.session_id}: {skipped.reason}" + (f" ({skipped.detail})" if skipped.detail else ""))
        for problem in problems:
            print(f"FAIL: {problem}")
        print("time-profile check: " + ("FAIL" if problems else "ok"))
        return 1 if problems else 0
    if args.format == "json":
        print(json.dumps(_rounded(payload), indent=2, sort_keys=True))
    else:
        print(render_text(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
