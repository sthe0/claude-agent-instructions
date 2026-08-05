#!/usr/bin/env python3
"""Stage 2 of advisor-timeout-f3b: measure advisor enumeration latency across the
plan-size distribution. Drives the REAL advisor.enumerate_questions_health with an
explicitly raised timeout so the 20 s cap cannot truncate a datapoint."""
import functools
import hashlib
import json
import os
import pathlib
import shutil
import statistics
import sys
import time

WORKTREE = pathlib.Path("/Users/the0/claude-agent-instructions-advisor-timeout")
sys.path.insert(0, str(WORKTREE / "scripts"))
from agentctl import advisor  # noqa: E402
from agentctl.plan import load_plan  # noqa: E402

PARK = pathlib.Path("/Users/the0/.claude-agent/plans")
OWN = "advisor-timeout-f3b.toml"
SNAP = pathlib.Path("/tmp/adv-calib-snap")
OUT = WORKTREE / "docs/operations/advisor-calibration.jsonl"
LOG = pathlib.Path("/tmp/adv-calib.log")
REPEATS = 3


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with LOG.open("a") as fh:
        fh.write(line + "\n")


def select():
    """Deterministic: min, p25, median, p75, max of the park sorted by char count,
    excluding this task's own plan. Restricted to plans that load_plan accepts —
    a file the loader rejects has no goal/done_criterion and therefore cannot be
    enumerated at all, so it is not in the operative park."""
    cand = []
    for p in sorted(PARK.glob("*.toml")):
        if p.name == OWN:
            continue
        try:
            doc = load_plan(str(p))
        except Exception:
            continue
        text = p.read_text(encoding="utf-8")
        cand.append((len(text), str(p), doc.meta.goal, doc.meta.done_criterion, text))
    cand.sort(key=lambda r: r[0])
    n = len(cand)
    idx = sorted({round(q * (n - 1)) for q in (0.0, 0.25, 0.5, 0.75, 1.0)})
    return [cand[i] for i in idx], n


def main():
    shutil.rmtree(SNAP, ignore_errors=True)
    SNAP.mkdir(parents=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    picked, park_n = select()
    log(f"operative park: {park_n} loadable plans; sampling {len(picked)} sizes")

    samples = []
    for size, path, goal, crit, text in picked:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        snap = SNAP / (pathlib.Path(path).stem + ".toml")
        snap.write_text(text, encoding="utf-8")
        samples.append(dict(chars=size, path=path, sha=digest, goal=goal, crit=crit, text=text))
        log(f"  sample {size:>7} chars  sha {digest[:12]}  {path}")

    runner = functools.partial(advisor.subprocess_runner, timeout=600)
    rows = []
    for rep in range(1, REPEATS + 1):
        for s in samples:
            load1 = os.getloadavg()[0]
            t0 = time.monotonic()
            ok, pairs = advisor.enumerate_questions_health(s["goal"], s["crit"], s["text"], runner)
            elapsed = time.monotonic() - t0
            row = {
                "plan_path": s["path"],
                "plan_sha256": s["sha"],
                "input_chars": s["chars"],
                "elapsed_s": round(elapsed, 3),
                "runner_ok": ok,
                "pair_count": len(pairs),
                "model": advisor._ADVISOR_MODEL,
                "loadavg": round(load1, 2),
                "repeat": rep,
            }
            rows.append(row)
            with OUT.open("a") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            log(f"rep{rep} {s['chars']:>7}ch -> {elapsed:7.1f}s ok={ok} pairs={len(pairs)} load={load1:.1f}")
    log(f"DONE {len(rows)} rows -> {OUT}")
    med = {s["chars"]: statistics.median([r["elapsed_s"] for r in rows if r["input_chars"] == s["chars"]])
           for s in samples}
    log("medians: " + json.dumps(med))


if __name__ == "__main__":
    main()
