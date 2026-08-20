#!/usr/bin/env python3
"""Measure lib.marker_extract's own extraction latency, idle AND under load.

Sibling of measure-advisor-latency.py, same method (drive the real call
through a runner timeout far above any expected value, so no datapoint is
truncated by the bound under measurement) applied to a DIFFERENT workload:
the marker extractor's payload is bounded at _WINDOW_MAX=12000 chars, an
order of magnitude below the advisor enumeration's, so the sizes sampled
here span the extractor's own range rather than reusing the advisor's.

Unlike the advisor dataset (which happened to be sampled while the fleet was
already busy, loadavg 5.8-13.0), this machine is not reliably busy on its
own, so the loaded condition is INDUCED: a bank of CPU-bound stress
subprocesses is started before the loaded phase and torn down after, and
every row records the 1-minute loadavg sampled at that row's own start so a
later reader can verify the two conditions actually differed.

The sampled specialist-output TEXTS are deliberately not committed (repo
venue is public, texts here are synthetic-but-real prose drawn from this
repo's own commit messages); only the measured numbers land in
docs/operations/marker-extractor-calibration.jsonl.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import pathlib
import subprocess
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from lib import host_llm, marker_extract  # noqa: E402
from lib.runtime_models import HOST_CLAUDE  # noqa: E402

OUT = REPO_ROOT / "docs" / "operations" / "marker-extractor-calibration.jsonl"
LOG = pathlib.Path("/tmp/marker-calib.log")

# Spans the extractor's real range: build_prompt windows whole-text up to
# _WINDOW_MAX (12000); sampling up to that boundary exercises the un-elided
# path at its most expensive point without ever exceeding what the pass
# actually reads.
SIZES = (1200, 3500, 7000, 12000)
REPEATS_PER_CONDITION = 3
MEASURE_CAP = 300  # far above any expected single-attempt latency

IDLE_LOADAVG_MAX = 2.0
LOADED_LOADAVG_MIN = 5.0
IDLE_WAIT_POLL_S = 5
IDLE_WAIT_TIMEOUT_S = 180
LOAD_RAMP_WAIT_S = 45
STRESS_DURATION_S = 600  # torn down explicitly once the loaded phase ends


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with LOG.open("a") as fh:
        fh.write(line + "\n")


def _corpus() -> str:
    """Real prose to pad synthetic specialist-output samples with: this
    repo's own commit messages, many of them literally a spawned developer's
    COMPLETED summary (Co-Authored-By: Claude). Real shape, not filler."""
    proc = subprocess.run(
        ["git", "log", "--format=%B", "-n", "150"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
    )
    text = proc.stdout.strip()
    if len(text) < max(SIZES):
        text = (text + "\n\n") * (max(SIZES) // max(len(text), 1) + 2)
    return text


def _sample_text(target_chars: int, corpus: str) -> str:
    header = (
        "COMPLETED: measured a marker extraction latency sample; this text "
        "exists only to occupy the extractor's input window and is never "
        "committed.\n\n"
    )
    body_len = max(target_chars - len(header), 0)
    body = (corpus * (body_len // len(corpus) + 2))[:body_len]
    return (header + body)[:target_chars]


def _stress_workers(n: int, duration_s: int) -> list[subprocess.Popen]:
    code = f"import time\nt = time.time() + {duration_s}\nwhile time.time() < t:\n    pass\n"
    return [
        subprocess.Popen([sys.executable, "-c", code])
        for _ in range(n)
    ]


def _stop_workers(procs: list[subprocess.Popen]) -> None:
    for p in procs:
        p.terminate()
    for p in procs:
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


def _one_call(text: str) -> tuple[int, float, float]:
    """Returns (returncode, elapsed_s, loadavg_at_start)."""
    prompt = marker_extract.build_prompt(
        text, marker_extract.RETURN_MARKERS,
        hint=marker_extract.hint_markers_for("developer"),
    )
    argv = host_llm.build_prompt_argv(HOST_CLAUDE, marker_extract.model(HOST_CLAUDE), prompt)
    loadavg = os.getloadavg()[0]
    t0 = time.monotonic()
    res = marker_extract.subprocess_runner(argv, timeout=MEASURE_CAP)
    elapsed = time.monotonic() - t0
    return res.returncode, elapsed, loadavg


def _row(size: int, returncode: int, elapsed: float, loadavg: float, condition: str) -> dict:
    return {
        "input_chars": size,
        "elapsed_s": round(elapsed, 3),
        "returncode": returncode,
        "loadavg": round(loadavg, 2),
        "load_condition": condition,
        "model": marker_extract.model(HOST_CLAUDE),
    }


def _append(row: dict) -> None:
    with OUT.open("a") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _wait_for_idle() -> None:
    deadline = time.monotonic() + IDLE_WAIT_TIMEOUT_S
    while os.getloadavg()[0] >= IDLE_LOADAVG_MAX and time.monotonic() < deadline:
        time.sleep(IDLE_WAIT_POLL_S)


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    corpus = _corpus()
    log(f"sampling sizes: {SIZES}")

    # --- idle phase: smallest size first, sequential, each preceded by a
    # wait-for-idle so the recorded loadavg genuinely clears the threshold.
    for size in SIZES:
        text = _sample_text(size, corpus)
        for rep in range(1, REPEATS_PER_CONDITION + 1):
            _wait_for_idle()
            rc, elapsed, loadavg = _one_call(text)
            row = _row(size, rc, elapsed, loadavg, "idle")
            _append(row)
            log(f"idle   {size:>5}ch rep{rep} -> {elapsed:6.1f}s rc={rc} load={loadavg:.2f}")

    # --- loaded phase: induce load once, then run every size's loaded
    # repeats concurrently (in flight together, the fleet's own normal
    # shape) while the stress bank keeps loadavg elevated.
    n_workers = max(os.cpu_count() or 4, 4)
    stress = _stress_workers(n_workers, STRESS_DURATION_S)
    try:
        log(f"started {n_workers} stress workers; waiting {LOAD_RAMP_WAIT_S}s to ramp")
        time.sleep(LOAD_RAMP_WAIT_S)
        for size in SIZES:
            text = _sample_text(size, corpus)
            with concurrent.futures.ThreadPoolExecutor(max_workers=REPEATS_PER_CONDITION) as ex:
                futures = [ex.submit(_one_call, text) for _ in range(REPEATS_PER_CONDITION)]
                for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
                    rc, elapsed, loadavg = fut.result()
                    row = _row(size, rc, elapsed, loadavg, "loaded")
                    _append(row)
                    log(f"loaded {size:>5}ch rep{i} -> {elapsed:6.1f}s rc={rc} load={loadavg:.2f}")
    finally:
        _stop_workers(stress)
        log("stress workers stopped")

    log(f"DONE -> {OUT}")


if __name__ == "__main__":
    main()
