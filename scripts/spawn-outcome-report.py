#!/usr/bin/env python3
"""Longitudinal defect-rate report over the spawn-specialist cost ledger.

`scripts/cost-report.py` reads the same ledger to answer a per-session cost
question. This script answers a different one — how often a spawn returns
exit 0 and is nevertheless scored malformed, and how often the fleet then pays
for a redundant re-spawn — so it is a separate script rather than a mode of
that one.

Three modes:

  (default)          print the report for the measured window
  --freeze-baseline  WRITE a baseline JSON (refuses to overwrite without --force)
  --check-delta      READ ONLY: compare a post-fix arm against a frozen baseline

The measured window starts at the deployment of the second-pass marker
extractor (commit efd3f45, first ledger row carrying `extraction_reason`).
Rows before it cannot exhibit the failure class this measurement is about,
because the subprocess whose failures it counts did not exist yet.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import NormalDist

LEDGER = Path.home() / ".local" / "log" / "claude-spawn-costs.jsonl"

# Deployment of commit efd3f45 (the second-pass marker extractor), dated from
# the ledger itself: the timestamp of the first row carrying `extraction_reason`.
DEPLOY_TS = "2026-07-27T14:26:43Z"

# A malformed spawn counts as re-spawned when a same-kind non-malformed spawn
# follows it within this many seconds.
RESPAWN_WINDOW_S = 1800

# Recovery assumptions: the fraction of the extractor-timeout class the fix is
# assumed to remove. Look 1 is powered against the optimistic figure, look 2
# against the pessimistic one.
ASSUMED_RECOVERY = 0.60
LOOK2_RECOVERY = 0.40

# Two-look group-sequential design. The nominal per-look two-sided alpha is
# solved so that the OVERALL two-sided alpha across both looks is 0.05 at this
# design's own information fractions; the boundary constant is its z.
NOMINAL_ALPHA = 0.028365

POWER = 0.80
ONE_SIDED_95_Z = NormalDist().inv_cdf(0.95)

_ND = NormalDist()


# ── ledger reading (row shape follows scripts/cost-report.py) ────────────────


def parse_ts(s: str) -> dt.datetime:
    """Parse a ledger timestamp. Handles both '...Z' and '...+00:00'."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
        d = dt.datetime.fromisoformat(s)
    else:
        d = dt.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


def load_rows(path: Path) -> list[dict]:
    """Return the ledger's spawn rows, oldest first.

    Entries with no `event` field are spawns (the oldest era of the ledger did
    not write one); `refused` and `spawn_start` rows are not spawns and are
    dropped. Rows are returned as-is: the ledger's schema is era-dependent and
    the missing fields are a fact about the data, not something to impute.
    """
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                print(f"warning: skipping malformed jsonl line: {line[:120]}", file=sys.stderr)
                continue
            if row.get("event", "spawn") != "spawn":
                continue
            if not row.get("ts"):
                continue
            rows.append(row)
    rows.sort(key=lambda r: r["ts"])
    return rows


def ledger_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def in_window(rows: list[dict], start: dt.datetime | None, end: dt.datetime | None = None) -> list[dict]:
    out = []
    for r in rows:
        try:
            ts = parse_ts(r["ts"])
        except ValueError:
            continue
        if start is not None and ts < start:
            continue
        if end is not None and ts > end:
            continue
        out.append(r)
    return out


def is_target(row: dict) -> bool:
    """The measured subpopulation: exit 0 AND scored malformed.

    A non-zero exit is an independent failure and belongs to a different
    question, so those rows are excluded throughout rather than counted as a
    different kind of defect.
    """
    return row.get("exit_code") == 0 and bool(row.get("malformed"))


def normalize_reason(row: dict) -> str | None:
    """Normalize `extraction_reason` to a stable cause class.

    Returns None when the row carries no reason — either because it predates
    the field or because none was recorded. Such rows are counted but not
    attributed; they are excluded from the cause breakdown rather than binned
    into a catch-all that would inflate whichever class it sat next to.
    """
    raw = row.get("extraction_reason")
    if raw is None or not str(raw).strip():
        return None
    s = str(raw).strip().lower()
    if "timed out" in s or "timeout" in s:
        return "extractor_timeout"
    if "no marker" in s:
        return "no_marker_found"
    if "unrecognised token" in s or "unrecognized token" in s:
        return "unrecognised_token"
    if s == "ok":
        return "ok"
    if "extractor process failed" in s:
        return "extractor_process_failed"
    return "other:" + s[:60]


TIMEOUT_CLASS = "extractor_timeout"


# ── the re-spawn predicate ──────────────────────────────────────────────────


def respawn_pairs(rows: list[dict], window_s: int = RESPAWN_WINDOW_S) -> list[tuple[int, int]]:
    """Return (original_index, follower_index) pairs over `rows`.

    The predicate has TWO halves and both are load-bearing:

      * the candidate row must ITSELF be exit-0-and-malformed, and
      * a LATER spawn row of the same kind with malformed false must begin
        within `window_s` seconds of it.

    Dropping the first half counts ordinary back-to-back spawning as
    redundancy; measured over the plan's own window that reads 469 of 845
    rather than 107, which is why the numerator is a declared baseline field
    (`respawn_numerator`) and not an implementation detail.

    Each original is paired with its FIRST qualifying follower.
    """
    times = []
    for r in rows:
        try:
            times.append(parse_ts(r["ts"]))
        except (KeyError, ValueError):
            times.append(None)
    pairs: list[tuple[int, int]] = []
    for i, row in enumerate(rows):
        if not is_target(row):
            continue
        t0 = times[i]
        if t0 is None:
            continue
        for j in range(i + 1, len(rows)):
            tj = times[j]
            if tj is None:
                continue
            delta = (tj - t0).total_seconds()
            if delta <= 0:
                continue
            if delta > window_s:
                break
            if rows[j].get("kind") == row.get("kind") and not rows[j].get("malformed"):
                pairs.append((i, j))
                break
    return pairs


# ── statistics ──────────────────────────────────────────────────────────────


def sample_size_two_proportion(p0: float, p1: float, alpha: float, power: float = POWER) -> int:
    """Per-arm sample size for a two-sided two-proportion z-test."""
    za = _ND.inv_cdf(1 - alpha / 2)
    zb = _ND.inv_cdf(power)
    pbar = (p0 + p1) / 2
    num = (
        za * math.sqrt(2 * pbar * (1 - pbar))
        + zb * math.sqrt(p0 * (1 - p0) + p1 * (1 - p1))
    ) ** 2
    return math.ceil(num / (p0 - p1) ** 2)


def two_proportion_z(x0: int, n0: int, x1: int, n1: int) -> tuple[float, float]:
    """Return (z, two-sided p) for arm-1 minus arm-0. Negative z = rate fell."""
    if n0 == 0 or n1 == 0:
        return 0.0, 1.0
    p0, p1 = x0 / n0, x1 / n1
    pool = (x0 + x1) / (n0 + n1)
    se = math.sqrt(pool * (1 - pool) * (1 / n0 + 1 / n1))
    if se == 0:
        return 0.0, 1.0
    z = (p1 - p0) / se
    p = 2 * (1 - _ND.cdf(abs(z)))
    return z, p


def power_two_proportion(p0: float, p1: float, n: int, alpha: float) -> float:
    """Power of the two-sided test at per-arm n."""
    if p0 == p1 or n <= 0:
        return alpha
    za = _ND.inv_cdf(1 - alpha / 2)
    pbar = (p0 + p1) / 2
    se_null = math.sqrt(2 * pbar * (1 - pbar) / n)
    se_alt = math.sqrt((p0 * (1 - p0) + p1 * (1 - p1)) / n)
    return _ND.cdf((abs(p0 - p1) - za * se_null) / se_alt)


def wilson_lower_bound(k: int, n: int, z: float = ONE_SIDED_95_Z) -> float:
    """One-sided Wilson lower bound on a proportion.

    Used so that a 3-row fluke in a post-fix cause class cannot trip the
    substitution ceiling: the class has to be big enough that even its lower
    bound clears the ceiling.
    """
    if n == 0:
        return 0.0
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denom)


def timeout_ceiling(residual_share: float, n_post: int, z: float = ONE_SIDED_95_Z) -> float:
    """The extractor-timeout class's admissible share of post-fix rows.

    The assumed recovery itself implies a residual; sampling at n_post adds a
    one-sided 95% allowance on top, so an observed share below this is
    consistent with the assumption rather than evidence against it.
    """
    if n_post <= 0:
        return 1.0
    return residual_share + z * math.sqrt(residual_share * (1 - residual_share) / n_post)


# ── the baseline ────────────────────────────────────────────────────────────


def summarize(rows: list[dict]) -> dict:
    """The raw counts a window yields, with no design arithmetic on top.

    Split out from `compute_baseline` so the row-level readings (which rows
    count, which cause they carry, which of them were re-spawned) can be
    exercised on their own — including on fixtures too small for a stopping
    rule to be derivable at all.
    """
    targets = [r for r in rows if is_target(r)]
    pairs = respawn_pairs(rows)
    follower_idx = sorted({j for _, j in pairs})

    breakdown = Counter()
    for r in targets:
        cls = normalize_reason(r)
        if cls is not None:
            breakdown[cls] += 1

    target_idx = {i for i, _ in pairs}
    return {
        "window_rows": len(rows),
        "window_cost_usd": sum((r.get("cost_usd") or 0.0) for r in rows),
        "exit0_malformed_n": len(targets),
        "exit0_malformed_cost_usd": sum((r.get("cost_usd") or 0.0) for r in targets),
        "respawn_n": len(pairs),
        # The FOLLOWERS' cost, de-duplicated: the redundant spend is the money
        # the re-spawns cost, and one clean spawn that closes out two malformed
        # ones was still paid for once.
        "respawn_cost_usd": sum((rows[j].get("cost_usd") or 0.0) for j in follower_idx),
        "cause_breakdown": dict(sorted(breakdown.items())),
        "attributable_n": sum(breakdown.values()),
        "timeout_class_n": breakdown.get(TIMEOUT_CLASS, 0),
        "timeout_class_respawn_n": sum(
            1 for i in target_idx if normalize_reason(rows[i]) == TIMEOUT_CLASS
        ),
    }


def compute_baseline(
    rows: list[dict],
    window_start: str,
    ledger_hash: str,
    assumed_recovery: float = ASSUMED_RECOVERY,
    look2_recovery: float = LOOK2_RECOVERY,
    nominal_alpha: float = NOMINAL_ALPHA,
) -> dict:
    """Compute every frozen figure FROM `rows`.

    Nothing here is copied in from a plan or a note: the freeze runs against a
    ledger that has kept growing, so a stopping rule quoting a planning-time
    constant would document a rule its own baseline does not imply.
    """
    window_rows = len(rows)
    if window_rows == 0:
        raise SystemExit("spawn-outcome-report: no spawn rows in the window; nothing to freeze")

    counts = summarize(rows)
    exit0_malformed_n = counts["exit0_malformed_n"]
    respawn_n = counts["respawn_n"]
    breakdown = counts["cause_breakdown"]
    attributable_n = counts["attributable_n"]
    timeout_class_n = counts["timeout_class_n"]
    timeout_class_respawn_n = counts["timeout_class_respawn_n"]

    window_end = max(r["ts"] for r in rows)
    start_dt = parse_ts(min(r["ts"] for r in rows) if window_start == "all" else window_start)
    span_days = max((parse_ts(window_end) - start_dt).total_seconds() / 86400.0, 1.0)
    # One day of observed traffic: the finest granularity at which the gate can
    # be run without the look becoming a sequence of looks.
    look_window_rows = max(1, math.ceil(window_rows / span_days))

    p0_malformed = exit0_malformed_n / window_rows
    p0_respawn = respawn_n / window_rows
    timeout_share = timeout_class_n / window_rows
    timeout_respawn_share = timeout_class_respawn_n / window_rows

    def _sizes(recovery: float) -> tuple[int, int]:
        p1_m = p0_malformed - recovery * timeout_share
        p1_r = p0_respawn - recovery * timeout_respawn_share
        if p1_m <= 0 or p1_r <= 0 or p1_m == p0_malformed or p1_r == p0_respawn:
            raise SystemExit(
                "spawn-outcome-report: the assumed recovery implies no detectable "
                "effect (a predicted rate at or below zero, or equal to the baseline); "
                "the recovery assumption needs re-deriving, not clamping"
            )
        return (
            sample_size_two_proportion(p0_malformed, p1_m, nominal_alpha),
            sample_size_two_proportion(p0_respawn, p1_r, nominal_alpha),
        )

    n_m1, n_r1 = _sizes(assumed_recovery)
    n_m2, n_r2 = _sizes(look2_recovery)

    residual_share = (1 - assumed_recovery) * timeout_share

    non_timeout = {k: v for k, v in breakdown.items() if k != TIMEOUT_CLASS}
    substitution_ceiling = (
        max(non_timeout.values()) / attributable_n if non_timeout and attributable_n else 0.0
    )

    return {
        "window_start": min(r["ts"] for r in rows) if window_start == "all" else window_start,
        "window_end": window_end,
        "ledger_sha256": ledger_hash,
        "window_rows": window_rows,
        "window_cost_usd": counts["window_cost_usd"],
        "exit0_malformed_n": exit0_malformed_n,
        "exit0_malformed_rate": p0_malformed,
        "exit0_malformed_cost_usd": counts["exit0_malformed_cost_usd"],
        "respawn_n": respawn_n,
        "respawn_rate": p0_respawn,
        "respawn_cost_usd": counts["respawn_cost_usd"],
        "respawn_denominator": "all_spawn_rows_in_window",
        "respawn_numerator": "exit0_and_malformed_rows_in_window",
        "respawn_window_seconds": RESPAWN_WINDOW_S,
        "cause_breakdown": breakdown,
        "attributable_n": attributable_n,
        "assumed_recovery": assumed_recovery,
        "look2_recovery": look2_recovery,
        "timeout_class_n": timeout_class_n,
        "timeout_class_respawn_n": timeout_class_respawn_n,
        "timeout_class_residual_share": residual_share,
        "substitution_ceiling_share_of_attributable": substitution_ceiling,
        "stopping_rule_nominal_alpha": nominal_alpha,
        "stopping_rule_boundary_z": _ND.inv_cdf(1 - nominal_alpha / 2),
        "stopping_rule_n_malformed_rate": n_m1,
        "stopping_rule_n_respawn_rate": n_r1,
        "stopping_rule_min_n": max(n_m1, n_r1),
        "stopping_rule_look2_n_malformed_rate": n_m2,
        "stopping_rule_look2_n_respawn_rate": n_r2,
        "stopping_rule_look2_min_n": max(n_m2, n_r2),
        "look_window_rows": look_window_rows,
    }


# ── report mode ─────────────────────────────────────────────────────────────


def _pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def render_report(rows: list[dict], window_start: str, mixed_regime: bool) -> str:
    out: list[str] = []
    if mixed_regime:
        out.append(
            "!! WARNING: --window-start reaches back before the second-pass extractor "
            "(efd3f45) existed. The result MIXES TWO REGIMES: rows before that commit "
            "carry no cause field and could not exhibit the extractor's own failures."
        )
        out.append("")
    n = len(rows)
    if n == 0:
        return "no spawn rows in the window"
    total_cost = sum((r.get("cost_usd") or 0.0) for r in rows)
    targets = [r for r in rows if is_target(r)]
    tcost = sum((r.get("cost_usd") or 0.0) for r in targets)
    pairs = respawn_pairs(rows)
    followers = sorted({j for _, j in pairs})
    rcost = sum((rows[j].get("cost_usd") or 0.0) for j in followers)

    out.append(f"window start : {window_start}")
    out.append(f"window end   : {max(r['ts'] for r in rows)}")
    out.append(f"spawn rows   : {n}")
    out.append(f"total cost   : ${total_cost:.2f}")
    out.append("")
    out.append("exit-0-and-malformed subpopulation")
    out.append(f"  rows       : {len(targets)}  ({_pct(len(targets) / n)} of rows)")
    out.append(f"  cost       : ${tcost:.2f}  ({_pct(tcost / total_cost) if total_cost else 'n/a'} of cost)")
    out.append("")
    out.append("  by kind")
    for k, c in Counter(r.get("kind") or "?" for r in targets).most_common():
        out.append(f"    {k:<16} {c}")
    out.append("")
    out.append("  by cause (normalized extraction_reason)")
    causes = Counter(normalize_reason(r) for r in targets)
    unattributed = causes.pop(None, 0)
    attributable = sum(causes.values())
    for k, c in causes.most_common():
        share = c / attributable if attributable else 0.0
        out.append(f"    {k:<26} {c:>4}  ({_pct(share)} of attributable)")
    out.append(f"    {'(unattributed)':<26} {unattributed:>4}")
    out.append("")
    out.append(f"re-spawn (same kind, non-malformed, within {RESPAWN_WINDOW_S}s)")
    out.append(f"  numerator  : exit0_and_malformed_rows_in_window")
    out.append(f"  denominator: all_spawn_rows_in_window")
    out.append(f"  rows       : {len(pairs)}  ({_pct(len(pairs) / n)} of rows)")
    out.append(f"  redundant cost: ${rcost:.2f}")
    out.append("")
    out.append("per-month series")
    by_month: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_month[r["ts"][:7]].append(r)
    respawn_originals = {i for i, _ in pairs}
    month_of = {i: rows[i]["ts"][:7] for i in respawn_originals}
    rs_by_month = Counter(month_of.values())
    out.append(f"  {'month':<9} {'rows':>6} {'malformed':>11} {'re-spawn':>10}")
    for m in sorted(by_month):
        mr = by_month[m]
        mt = sum(1 for r in mr if is_target(r))
        out.append(
            f"  {m:<9} {len(mr):>6} {_pct(mt / len(mr)):>11} {_pct(rs_by_month.get(m, 0) / len(mr)):>10}"
        )
    return "\n".join(out)


# ── delta mode ──────────────────────────────────────────────────────────────


def first_outcome_class_ts(rows: list[dict]) -> str | None:
    """The ledger's own date for unit 1's landing.

    `outcome_class` cannot appear on a row written by any binary predating it,
    so the first row carrying it dates the landing exactly as the first
    `extraction_reason` row dates efd3f45. The post arm is discovered this way
    rather than inherited from the baseline's `window_end`, because the rows
    between the freeze and the landing are pre-fix traffic that belongs to
    neither arm.
    """
    for r in rows:
        if "outcome_class" in r:
            return r["ts"]
    return None


def check_delta(all_rows: list[dict], baseline: dict, post_start_arg: str | None) -> tuple[int, str]:
    out: list[str] = []
    fail: list[str] = []

    landing_ts = first_outcome_class_ts(all_rows)
    if landing_ts is None:
        return 2, (
            "no ledger row carries `outcome_class` yet, so unit 1 has not reached "
            "production traffic and there is no post arm to measure. Not yet measurable."
        )
    if post_start_arg:
        if parse_ts(post_start_arg) <= parse_ts(landing_ts):
            return 2, (
                f"--post-start {post_start_arg} is not later than the first ledger row "
                f"carrying outcome_class ({landing_ts}); the post arm may only be moved "
                f"later, never earlier."
            )
        post_start = post_start_arg
    else:
        post_start = landing_ts

    # Compare parsed instants, never raw strings: the ledger writes '+00:00'
    # while an operator passes '...Z', and those two spellings of one moment do
    # not compare lexicographically.
    post_start_dt = parse_ts(post_start)
    baseline_end_dt = parse_ts(baseline["window_end"])
    gap = [
        r for r in all_rows
        if baseline_end_dt < parse_ts(r["ts"]) < post_start_dt
    ]
    out.append(f"baseline window_end : {baseline['window_end']}")
    out.append(f"first outcome_class : {landing_ts}")
    out.append(f"post arm starts at  : {post_start}")
    out.append(f"rows excluded in the freeze-to-landing gap: {len(gap)}")

    post = [r for r in all_rows if parse_ts(r["ts"]) >= post_start_dt]
    n_post = len(post)
    n_pre = baseline["window_rows"]
    out.append(f"post rows: {n_post}")

    min_n = baseline["stopping_rule_min_n"]
    win = baseline["look_window_rows"]
    look2 = baseline["stopping_rule_look2_min_n"]
    if n_post < min_n:
        return 2, "\n".join(
            out + [f"FAIL: insufficient evidence — look 1 needs {min_n} post rows, "
                   f"{min_n - n_post} more to go."]
        )
    if min_n + win < n_post < look2:
        return 2, "\n".join(
            out + [f"FAIL: n_post={n_post} lies between the two pre-registered look "
                   f"windows ([{min_n}, {min_n + win}] and [{look2}, ...)). Reading it "
                   f"here would be a third look where the design pre-registers two; "
                   f"wait for {look2 - n_post} more rows."]
        )
    out.append(f"look window: {'1' if n_post <= min_n + win else '2'}")

    alpha = baseline["stopping_rule_nominal_alpha"]

    post_targets = [r for r in post if is_target(r)]
    z_m, p_m = two_proportion_z(baseline["exit0_malformed_n"], n_pre, len(post_targets), n_post)
    rate_m = len(post_targets) / n_post
    out.append(
        f"malformed rate: {baseline['exit0_malformed_rate']:.4f} -> {rate_m:.4f}  "
        f"(z={z_m:.3f}, p={p_m:.5f})"
    )
    if not (rate_m < baseline["exit0_malformed_rate"] and p_m < alpha):
        fail.append(f"the malformed rate did not fall at p < {alpha}")

    post_pairs = respawn_pairs(post)
    rate_r = len(post_pairs) / n_post
    z_r, p_r = two_proportion_z(baseline["respawn_n"], n_pre, len(post_pairs), n_post)
    out.append(
        f"re-spawn rate : {baseline['respawn_rate']:.4f} -> {rate_r:.4f}  "
        f"(z={z_r:.3f}, p={p_r:.5f})"
    )
    if not (rate_r < baseline["respawn_rate"] and p_r < alpha):
        fail.append(f"the re-spawn rate did not fall at p < {alpha}")

    post_causes = Counter()
    for r in post_targets:
        cls = normalize_reason(r)
        if cls is not None:
            post_causes[cls] += 1
    post_attributable = sum(post_causes.values())

    ceiling = timeout_ceiling(baseline["timeout_class_residual_share"], n_post)
    timeout_share = post_causes.get(TIMEOUT_CLASS, 0) / n_post
    out.append(f"timeout class share of post rows: {timeout_share:.6f} (ceiling {ceiling:.6f})")
    if timeout_share > ceiling:
        fail.append(
            f"the extractor-timeout class still holds {timeout_share:.4f} of post rows, "
            f"above the {ceiling:.4f} the assumed recovery allows at n={n_post}"
        )

    sub_ceiling = baseline["substitution_ceiling_share_of_attributable"]
    for cls, k in sorted(post_causes.items()):
        if cls in baseline["cause_breakdown"]:
            continue
        lb = wilson_lower_bound(k, post_attributable)
        out.append(
            f"new cause class {cls!r}: {k}/{post_attributable}, "
            f"Wilson one-sided 95% LB {lb:.6f} (ceiling {sub_ceiling:.6f})"
        )
        if lb > sub_ceiling:
            fail.append(
                f"cause class {cls!r} is absent from the frozen breakdown and its share "
                f"of post attributable rows has a lower bound above the substitution "
                f"ceiling: the fix substituted one failure class for another"
            )

    if fail:
        return 1, "\n".join(out + ["FAIL:"] + [f"  - {f}" for f in fail])
    return 0, "\n".join(out + ["PASS: every pre-registered condition held."])


# ── cli ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ledger", type=Path, default=LEDGER, help="ledger path (default: the live ledger)")
    ap.add_argument(
        "--window-start",
        default=DEPLOY_TS,
        help=f"ISO timestamp, or 'all' for the ledger's own beginning (default: {DEPLOY_TS})",
    )
    ap.add_argument("--freeze-baseline", type=Path, metavar="PATH", help="WRITE the baseline JSON")
    ap.add_argument("--force", action="store_true", help="allow --freeze-baseline to overwrite")
    ap.add_argument("--check-delta", action="store_true", help="READ-ONLY comparison against a baseline")
    ap.add_argument("--baseline", type=Path, help="baseline JSON for --check-delta")
    ap.add_argument("--post-start", help="explicit post-arm start (may only move the arm LATER)")
    args = ap.parse_args(argv)

    rows = load_rows(args.ledger)
    if not rows:
        print(f"spawn-outcome-report: no spawn rows in {args.ledger}", file=sys.stderr)
        return 2

    if args.check_delta:
        if not args.baseline:
            ap.error("--check-delta requires --baseline")
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        code, text = check_delta(rows, baseline, args.post_start)
        print(text)
        return code

    mixed = args.window_start == "all"
    start = None if mixed else parse_ts(args.window_start)
    window = in_window(rows, start)

    if args.freeze_baseline:
        target: Path = args.freeze_baseline
        if target.exists() and not args.force:
            print(
                f"spawn-outcome-report: {target} already exists. A baseline is frozen "
                f"once; pass --force only if you mean to re-freeze it, and expect the "
                f"note beside it to disagree until you rewrite that too.",
                file=sys.stderr,
            )
            return 3
        baseline = compute_baseline(window, args.window_start, ledger_sha256(args.ledger))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"froze baseline -> {target}")
        print(json.dumps(baseline, indent=2, sort_keys=True))
        return 0

    print(render_report(window, args.window_start, mixed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
