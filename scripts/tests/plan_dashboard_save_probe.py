#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyte==0.8.2"]
# ///
"""Does the dashboard's clean exit meet the consumer's limit on this machine's live state?

The consumer is the stage-1 timing harness: its save run (``RealRunner.do_save``) starts the dashboard on a
pseudo-terminal, waits for ``live_check_end`` on the event stream, sends SIGTERM to the process group and
waits ``term_wait_s`` (6 s by default) for the stream to deliver ``snapshot_written`` whole before it gives up.
Whether a dashboard keeps within that depends on how much state there is and how busy the machine is, so it is
measured, not asserted by a test: this script calls the harness's own ``do_save`` N times on the live state
with a scratch state directory and reports per run the seconds from the SIGTERM to ``snapshot_written``.
It wraps the harness module's ``run_pty`` and the stop predicate only to observe them; every argument goes
through unchanged.

    uv run scripts/tests/plan_dashboard_save_probe.py --state-dir <scratch dir> --new <tree> --runs 10

Exit 0 only when every run produced ``snapshot_written`` and the stream had no unparsable line.
``--phase-log`` adds a stamp per shutdown phase (signal received, snapshot build, fsync, replace, event
written) from a ``sitecustomize`` the probe puts on ``PYTHONPATH`` of the dashboard's processes, and prints
them after each run: for finding where the time went when a run misses.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plan_dashboard_harness as harness  # noqa: E402

PHASE_SITECUSTOMIZE = '''
import faulthandler
import functools
import importlib.abc
import importlib.machinery
import os
import signal
import sys
import time

LOG = os.environ.get("PLAN_DASHBOARD_PHASE_LOG")
STACKS_AFTER_S = 3.0


def stamp(name):
    with open(LOG, "a") as handle:
        handle.write(f"{time.monotonic_ns()} {os.getpid()} {name}\\n")


def wrap(owner, attr, name):
    original = getattr(owner, attr)

    @functools.wraps(original)
    def inner(*args, **kwargs):
        stamp(name + " start")
        try:
            return original(*args, **kwargs)
        finally:
            stamp(name + " end")

    setattr(owner, attr, inner)


def patch_events(module):
    original = module.emit

    def emit(ev, **payload):
        stamp(f"emit {ev} start")
        original(ev, **payload)
        stamp(f"emit {ev} end")

    module.emit = emit


PATCHES = {
    "plan_dashboard.tui": lambda m: [wrap(m, "snapshot_section", "snapshot_section"),
                                     wrap(m, "write_snapshot", "write_snapshot")],
    "plan_dashboard.snapshot": lambda m: [wrap(m, name, "store." + name) for name in (
        "write", "load", "body_sha256", "code_version", "identities_of", "export_values")],
    "plan_dashboard.events": patch_events,
}


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname not in PATCHES:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return None
        original = spec.loader.exec_module

        def exec_module(module):
            original(module)
            PATCHES[fullname](module)

        spec.loader.exec_module = exec_module
        return spec


if LOG:
    dumped = []
    real_signal = signal.signal

    def signal_hook(sig, handler):
        if sig == signal.SIGTERM and callable(handler):
            inner = handler

            def stamped(signum, frame):
                stamp("sigterm handler start")
                if not dumped:
                    dumped.append(1)
                    faulthandler.dump_traceback_later(STACKS_AFTER_S, file=open(LOG + ".stacks", "a"))
                inner(signum, frame)
                stamp("sigterm handler end")

            handler = stamped
        return real_signal(sig, handler)

    signal.signal = signal_hook
    wrap(os, "fsync", "fsync")
    wrap(os, "replace", "replace")
    sys.meta_path.insert(0, Finder())
'''


STACKS_AFTER_S = 3.0  # as in the sitecustomize
STACK_FRAMES = 3


class Observed:
    """What the wrapped ``run_pty`` saw: the trace of each run and when the stop predicate first held."""

    def __init__(self):
        self.runs: list[tuple[object, int | None]] = []

    def install(self) -> None:
        run_pty = harness.run_pty

        def observing_run_pty(*args, **kwargs):
            stopped_ns: list[int] = []
            stop = kwargs.get("stop")
            if stop is not None:
                def observing_stop(trace):
                    held = stop(trace)
                    if held and not stopped_ns:
                        stopped_ns.append(time.monotonic_ns())
                    return held
                kwargs["stop"] = observing_stop
            trace = run_pty(*args, **kwargs)
            self.runs.append((trace, stopped_ns[0] if stopped_ns else None))
            return trace

        harness.run_pty = observing_run_pty


def seconds_to_snapshot_written(trace, stopped_ns: int | None) -> float | None:
    seen = [e["seen_ns"] for e in trace.events if e.get("ev") == "snapshot_written"]
    if not seen or stopped_ns is None:
        return None
    return (seen[0] - stopped_ns) / 1e9


def print_phases(log: Path, stopped_ns: int | None) -> None:
    if stopped_ns is None or not log.exists():
        return
    for line in log.read_text().splitlines():
        stamp, pid, name = line.split(" ", 2)
        if int(stamp) >= stopped_ns:
            print(f"    +{(int(stamp) - stopped_ns) / 1e9:7.3f}s  pid {pid}  {name}")
    stacks = Path(str(log) + ".stacks")
    if stacks.exists():
        print(f"    threads {STACKS_AFTER_S:g} s after the signal, innermost {STACK_FRAMES} frames each:")
        shown = 0
        for line in stacks.read_text().splitlines():
            if line.startswith(("Thread", "Current thread")):
                shown = 0
                print("      " + line.split(" (")[0])
            elif line.startswith("  File") and shown < STACK_FRAMES:
                shown += 1
                print("        " + line.strip().replace(str(Path(__file__).resolve().parents[2]) + "/", ""))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--new", type=Path, required=True)
    parser.add_argument("--runs", type=int, required=True)
    parser.add_argument("--phase-log", action="store_true")
    args = parser.parse_args(argv)
    state_dir, tree = args.state_dir.resolve(), args.new.resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    harness.EVENTS_DIR = state_dir / "events"
    phase_log = state_dir / "phases.log"
    if args.phase_log:
        site = state_dir / "site"
        site.mkdir(exist_ok=True)
        (site / "sitecustomize.py").write_text(PHASE_SITECUSTOMIZE, encoding="utf-8")
        os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, [str(site), os.environ.get("PYTHONPATH")]))
        os.environ["PLAN_DASHBOARD_PHASE_LOG"] = str(phase_log)
    observed = Observed()
    observed.install()
    runner = harness.RealRunner(state_dir, tree, tree, harness.load_json(harness.CONTRACT_PATH), [])
    failed = 0
    for run in range(1, args.runs + 1):
        phase_log.unlink(missing_ok=True)
        Path(str(phase_log) + ".stacks").unlink(missing_ok=True)
        saved = runner("save", None)
        trace, stopped_ns = observed.runs[-1]
        delay = seconds_to_snapshot_written(trace, stopped_ns)
        problems = []
        if saved is None or delay is None:
            problems.append("no snapshot_written")
        if trace.bad_lines:
            problems.append(f"{len(trace.bad_lines)} bad line(s)")
        failed += bool(problems)
        shown = "-" if delay is None else f"{delay:.3f}"
        print(f"run {run}: SIGTERM to snapshot_written {shown} s, exit {trace.exit_code}, "
              f"events {len(trace.events)}, {'ok' if not problems else 'FAILED: ' + ', '.join(problems)}", flush=True)
        if args.phase_log:
            print_phases(phase_log, stopped_ns)
    print(f"{args.runs - failed} of {args.runs} runs ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
