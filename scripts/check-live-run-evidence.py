#!/usr/bin/env python3
"""Recompute every number `live-run-evidence.md` claims, from the evidence it cites.

Difficulty removed: a stage that reports "three hooks ran green" leaves a document
whose numbers nobody ever counts. The previous attempt at this stage produced
exactly that — three passing runs on paper and a judge that never fired in
reality — so the artifact is only worth what an independent recount of it is
worth. Everything below is recomputed from the raw sample files committed under
``samples/judge-latency/`` and from the hook sources themselves; a claim that
does not survive the recount fails the check rather than being believed.

What is recounted, and why each one can otherwise be inflated:

  - the MODEL TAG, against ``advisor._JUDGE_MODEL`` — the constant that actually
    reaches the judge's argv. Latency belongs to the model that ran, so a row
    filed under a neighbouring constant is evidence for a call nobody makes.
  - ``n``, ``min``, ``median``, ``p90``, ``max`` of every measured row, from the
    cited ``file:series`` pairs, with the SAME estimator the table is contracted
    on (``lib.judge_latency.p90`` — nearest rank). Without pinning the estimator
    the check would bless any number in the plan: four standard estimators on the
    n=18 deferring sample give 29.94 / 32.23 / 37.58 / 37.82.
  - ``n >= 15``, so a row cannot be built from a handful of calls.
  - the CEILING each judge call actually gets, read out of the hook source by
    ``ast`` (no import, no side effects) — so the document cannot quote a ceiling
    the code does not use.
  - the fail-open share AND its 95% upper bound. THE ZERO RULE: an observed count
    of zero exceedances is not a residual of zero. A finite sample from a
    heavy-tailed distribution cannot establish zero, so a row claiming 0 without
    an upper bound is a FAILURE here; at k=0 the bound is the rule of three
    (3/n), and at k>0 an exact (Clopper-Pearson) upper bound computed by
    bisection on the binomial CDF.

Exit 0 when every claim survives; 1 with a per-failure list otherwise.
"""
from __future__ import annotations

import ast
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from agentctl import advisor  # noqa: E402
from lib import judge_latency  # noqa: E402

SAMPLES = ROOT / "samples" / "judge-latency"
MIN_N = 15
TOL = 0.005  # claims are written to 2dp; anything closer than half a unit matches

# Which module-level constant is the per-call ceiling for a (hook, judge) pair.
# Read out of the source by ast rather than imported: the point is to compare the
# document against the CODE, and importing a hook drags in its whole environment.
CEILING_CONST = {
    ("hook-deferring-disposition-gate.py", "deferring_disposition"): "_ASK_JUDGE_BUDGET_S",
    ("hook-escalation-diagnosis-gate.py", "outage_escalation"): "_JUDGE_BUDGET_S",
    ("hook-turn-end-gate.py", "feedback_signal"): "_TURN_FEEDBACK_CALL_CAP_S",
    ("hook-turn-end-gate.py", "binary_ask"): "_TURN_BINARY_ASK_CALL_CAP_S",
    ("hook-turn-end-gate.py", "outage_escalation"): "_TURN_OUTAGE_CALL_CAP_S",
}


# --- estimators ---------------------------------------------------------------

def rule_of_three(n: int) -> float:
    """95% upper bound on a rate when zero events were observed in n trials.

    The standard 3/n approximation: with p = 3/n the chance of seeing zero in n
    trials is about 5%, so anything larger would likely have shown up already.
    """
    return 3.0 / n


def _binom_cdf(k: int, n: int, p: float) -> float:
    return sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k + 1))


def exact_upper_bound(k: int, n: int) -> float:
    """One-sided 95% upper confidence bound on a binomial rate (Clopper-Pearson).

    The largest p for which observing k or fewer events in n trials still has
    probability >= 5%. Found by bisection on the CDF, which is monotone in p, so
    no special-function library is needed for a check that has to run anywhere.
    """
    if k >= n:
        return 1.0
    if k == 0:
        return rule_of_three(n)
    lo, hi = k / n, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if _binom_cdf(k, n, mid) < 0.05:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


# --- the document -------------------------------------------------------------

def _section(text: str, anchor: str) -> list[list[str]]:
    """Every markdown table row under an ``<!-- anchor -->`` comment, as cells.

    Anchored on an HTML comment rather than a heading so re-wording a heading
    never silently detaches the check from the table it is meant to police.
    """
    idx = text.find(f"<!-- {anchor} -->")
    if idx < 0:
        return []
    rows: list[list[str]] = []
    for line in text[idx:].splitlines()[1:]:
        line = line.strip()
        if not line.startswith("|"):
            if rows:
                break
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(set(c) <= set("-: ") for c in cells):
            continue  # separator row
        rows.append(cells)
    return rows[1:] if rows else []  # drop the header row


def _num(cell: str) -> float | None:
    cell = cell.replace("**", "").replace("%", "").strip()
    try:
        return float(cell)
    except ValueError:
        return None


def _series(spec: str) -> list[tuple[str, str]]:
    out = []
    for part in re.split(r"\s*\+\s*", spec.replace("`", "").strip()):
        if not part:
            continue
        fname, _, series = part.partition(":")
        out.append((fname.strip(), series.strip()))
    return out


def _latencies(pairs: list[tuple[str, str]], fails: list[str], where: str) -> list[float]:
    xs: list[float] = []
    for fname, series in pairs:
        path = SAMPLES / fname
        if not path.exists():
            fails.append(f"{where}: cited sample {fname} does not exist")
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if series not in data:
            fails.append(f"{where}: {fname} has no series {series!r}")
            return []
        xs.extend(float(r["latency_s"]) for r in data[series])
    return xs


def _hook_constant(hook: str, name: str) -> int | None:
    src = (ROOT / "scripts" / hook).read_text(encoding="utf-8")
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    if isinstance(node.value, ast.Constant):
                        return int(node.value.value)
    return None


# --- the checks ---------------------------------------------------------------

def check_live_runs(text: str, fails: list[str]) -> int:
    rows = _section(text, "live-runs")
    if not rows:
        fails.append("no <!-- live-runs --> table found")
        return 0
    for r in rows:
        if len(r) < 6:
            fails.append(f"live-runs row has {len(r)} cells, expected >= 6: {r}")
            continue
        hook, _judges, expected, actual, wall, target = r[0], r[1], r[2], r[3], r[4], r[5]
        hook = hook.replace("`", "").strip()
        if expected.strip() != actual.strip():
            fails.append(f"{hook}: expected verdict {expected!r} but recorded {actual!r}")
        w, t = _num(wall), _num(target)
        if w is None or t is None:
            fails.append(f"{hook}: wall-clock/target not numeric ({wall!r}/{target!r})")
        elif w >= t:
            fails.append(f"{hook}: wall-clock {w} is not below its target timeout {t}")
    return len(rows)


def check_measured(text: str, fails: list[str]) -> int:
    rows = _section(text, "measured")
    if not rows:
        fails.append("no <!-- measured --> table found")
        return 0

    model = advisor._JUDGE_MODEL
    table = judge_latency.MEASURED.get(model, {})
    seen_judges: set[str] = set()

    for r in rows:
        if len(r) < 13:
            fails.append(f"measured row has {len(r)} cells, expected 13: {r[:3]}")
            continue
        (hook, judge, claimed_model, sources, n_c, min_c, med_c,
         p90_c, max_c, ceil_c, k_c, share_c, ub_c) = r[:13]
        hook = hook.replace("`", "").strip()
        judge = judge.replace("`", "").strip()
        where = f"{hook}/{judge}"
        seen_judges.add(judge)

        if claimed_model.replace("`", "").strip() != model:
            fails.append(
                f"{where}: model tag {claimed_model!r} != advisor._JUDGE_MODEL {model!r}")
            continue

        row = table.get(judge)
        if row is None:
            fails.append(f"{where}: no MEASURED row for this judge under model {model}")
            continue

        # An unmeasured judge must say so, and claim nothing else.
        if row.n == 0:
            if "UNMEASURED" not in " ".join(r).upper():
                fails.append(f"{where}: MEASURED carries n=0 but the row does not say UNMEASURED")
            continue

        xs = _latencies(_series(sources), fails, where)
        if not xs:
            continue

        n = len(xs)
        if n < MIN_N:
            fails.append(f"{where}: n={n} is below the required minimum of {MIN_N}")
        recomputed = {
            "n": (float(n), _num(n_c)),
            "min": (min(xs), _num(min_c)),
            "median": (judge_latency.median(xs), _num(med_c)),
            "p90": (judge_latency.p90(xs), _num(p90_c)),
            "max": (max(xs), _num(max_c)),
        }
        for label, (got, claimed) in recomputed.items():
            if claimed is None or abs(got - claimed) > TOL:
                fails.append(
                    f"{where}: {label} claimed {claimed} but recomputes to {round(got, 2)} "
                    f"from {sources.strip()}")

        # The row must also agree with the frozen contract it summarises.
        for label, got, contracted in (
            ("n", float(n), float(row.n)),
            ("min", min(xs), row.min_s),
            ("median", judge_latency.median(xs), row.median_s),
            ("p90", judge_latency.p90(xs), row.p90_s),
            ("max", max(xs), row.max_s),
        ):
            if contracted is None or abs(got - contracted) > TOL:
                fails.append(
                    f"{where}: {label} recomputes to {round(got, 2)} but lib/judge_latency.py "
                    f"contracts {contracted}")

        # Ceiling: what the hook's own source actually hands the call.
        ceiling = _num(ceil_c)
        const = CEILING_CONST.get((hook, judge))
        if const is None:
            fails.append(f"{where}: no known ceiling constant for this (hook, judge) pair")
        else:
            in_code = _hook_constant(hook, const)
            if in_code is None:
                fails.append(f"{where}: cannot read {const} out of {hook}")
            elif ceiling is None or abs(ceiling - in_code) > TOL:
                fails.append(
                    f"{where}: ceiling claimed {ceil_c!r} but {hook}:{const} is {in_code}")
                ceiling = float(in_code)

        if ceiling is None:
            continue

        # Fail-open share, recounted, and its bound — with the zero rule.
        k = sum(1 for x in xs if x >= ceiling)
        claimed_k = _num(k_c)
        if claimed_k is None or int(claimed_k) != k:
            fails.append(
                f"{where}: {claimed_k} run(s) claimed at/above the {ceiling:g}s ceiling, "
                f"but {k} of {n} recount that way")
        share = k / n
        claimed_share = _num(share_c)
        if claimed_share is None or abs(share - claimed_share) > TOL:
            fails.append(
                f"{where}: fail-open share claimed {share_c!r} but recomputes to {share:.4f}")
        ub = _num(ub_c)
        if ub is None:
            fails.append(
                f"{where}: fail-open share is stated without a 95% upper bound — a finite "
                f"sample cannot establish a residual of zero (rule of three: {rule_of_three(n):.4f})")
        else:
            expected_ub = exact_upper_bound(k, n)
            if abs(ub - expected_ub) > TOL:
                fails.append(
                    f"{where}: 95% upper bound claimed {ub} but computes to "
                    f"{expected_ub:.4f} for {k}/{n}")

    for judge in table:
        if judge not in seen_judges:
            fails.append(
                f"MEASURED carries a row for {judge!r} that the artifact never mentions")
    return len(rows)


def check_prose(text: str, fails: list[str]) -> None:
    """The three statements the artifact exists to make and a checker can police:
    the rejected alternative, the honestly unmeasured one, and the boundary of
    what an stdin run can prove at all."""
    alts = _section(text, "alternatives")
    if not alts:
        fails.append("no <!-- alternatives --> table found")
    else:
        joined = " ".join(" ".join(r) for r in alts).upper()
        if "MEASURED AND REJECTED" not in joined:
            fails.append(
                "alternatives: no alternative is recorded as MEASURED AND REJECTED")
        if "NOT MEASURED" not in joined:
            fails.append(
                "alternatives: no alternative is honestly recorded as NOT MEASURED")
    idx = text.find("<!-- not-checked -->")
    if idx < 0:
        fails.append("no <!-- not-checked --> section found")
        return
    bullets = [
        ln for ln in text[idx:].splitlines()[1:]
        if ln.strip().startswith(("-", "*"))
    ]
    if len(bullets) < 3:
        fails.append(
            f"not-checked: only {len(bullets)} item(s) — an stdin run misses more than that")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check-live-run-evidence.py <live-run-evidence.md>", file=sys.stderr)
        return 2
    path = Path(argv[1])
    if not path.exists():
        path = ROOT / argv[1]
    if not path.exists():
        print(f"FAIL: {argv[1]} not found (also tried {ROOT})", file=sys.stderr)
        return 1

    text = path.read_text(encoding="utf-8")
    fails: list[str] = []
    n_runs = check_live_runs(text, fails)
    n_rows = check_measured(text, fails)
    check_prose(text, fails)

    if fails:
        print(f"FAIL — {len(fails)} claim(s) did not survive the recount:")
        for f in fails:
            print(f"  - {f}")
        return 1
    print(f"OK — {n_runs} live run(s) and {n_rows} measured row(s) recomputed from "
          f"{SAMPLES.name}/ and the hook sources; every claim matches")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
