#!/usr/bin/env python3
"""Control of the worker-slot limiter and of what a suite-gate run records about its cost.

Difficulty removed: concurrent suite runs oversubscribed the machine and cut each other's
timeouts, and a gate record said nothing about what a run cost or which tests it finished. These
two checks show the limiter holds and the record carries the cost, on real processes.

  gate_speedup_control.py slots-selftest [--limiter slots|none]
      Three fake runs of 4 slots each against a private slot directory: at most 8 are ever
      held, the third waits, and killing a holder lets it in. ``--limiter none`` bypasses the
      limiter, so the control must fail: it prints ``LIMIT EXCEEDED: held=<n> > 8`` and exits 1.
      Takes a few seconds; touches nothing outside its own temporary directory.

  gate_speedup_control.py concurrent-gates --runs N --out PATH [-C REPO] [--tests PATH ...]
      Start N ``suite-gate.py run`` at once on N throwaway commits of HEAD's tree (unreferenced
      ``git commit-tree`` objects; the records land in the repository's suite-gate store) and
      check each record: a verdict without timeout, pytest under scripts/cap-run.sh, slots
      taken (1..test-slots-per-run) before the timeout started, the slot wait equal to the gap
      between queueing and the timeout start, cpu_s, peak_disk_mb, a parsing JUnit XML, and a
      per-test report whose collected ids equal ``--collect-only``'s. At least one run must have
      waited for slots. Writes a JSON summary to PATH; exit 0 when every check holds, else 1.
      Each run is a full suite, so this takes as long as the slowest queued gate. ``--tests``
      drops every other ``test_*.py`` from the throwaway trees, so each gate runs the real code
      path on a few files and the check takes minutes; the commits are never landed, so a
      reduced record can never admit a real landing.

  gate_speedup_control.py sandbox-run --out PATH [-C REPO] [--select PATH ...] [--root DIR] [--timeout-s N]
                                      [--reruns N]
      Run the committed tip of REPO once, the way the gate does (a private clone in a sandbox
      with a fake HOME and a scrubbed environment), and write PATH: the collected count, the
      collection errors and every failing id (an error counts as failing). Exit 0 when the run
      completed and the report was written, whatever the test outcomes; 2 when it could not run.
      ``--select`` runs only the named test paths, serially; ``--root`` reuses and keeps a sandbox.
      ``--reruns N`` then reruns every failing id N times, serially, in the same sandbox: an id
      that fails any rerun goes to ``confirmed``, one that passes them all to ``rerun_passed``.
      The whole suite takes about 40 minutes.

  gate_speedup_control.py verify-named --report PATH --named DOC [--max-named N]
      Compare a sandbox-run report with the document that names the known failures (default at
      most 20 ids): 0 every failing id is named and DOC names at most N; 1 a failing id is not
      named; 3 DOC names more than N ids; 4 the run did not finish (fewer than 15000 collected,
      or a collection error); 2 an input could not be read. When the report has ``confirmed``,
      only those ids must be named; the ids that passed on rerun are printed.

  gate_speedup_control.py hermetic-run --out PATH --named DOC [--max-named N] [-C REPO] [--root DIR]
                                       [--reruns N] [--no-hide] [--probe-only]
      One sandbox-run inside the hermetic runner (scripts/lib/hermetic_run.py: the source work tree,
      its git directory, the canonical checkout and the agent homes hidden, no network; the rest of
      the home stays visible) with a probe test planted in the sandbox that asserts the hidden
      source work tree reads as empty, then verify-named.
      0: the report says ``hermetic: true``, the probe passed and every failure is named; 1: the run
      was not hermetic, or the probe failed, or a failure is not named, or verify-named refuses the
      run (incomplete) or the document (too many ids); 2: the probe was not collected, or any other
      error. ``--no-hide`` hides nothing and the exit rests on the probe alone: 1 when it ran and
      failed on the visible tree (the negative control fired), 2 when it passed (the control did
      not fire) or was not collected (no evidence); ``--probe-only``
      collects only the probe (minutes instead of the full run).

  gate_speedup_control.py shuffle-pair --seeds A,B --out PATH [--runs all|unshuffled|shuffled] [--exceptions TOML] [-C REPO] [--timeout-s N]
      Isolation judged by outcomes. Three whole-suite runs of REPO's committed tip, each in the hermetic
      runner under the slot limiter (at most test-slots-per-run slots each): unshuffled, then seed A and
      seed B (``--core-shuffle-seed``) started together. Per-test outcomes come from each run's per-test
      report; PATH gets all three (the JUnit XML and report of each beside it). The exit code is the
      verdict: 0 outcomes equal apart from the ids in the exceptions file (default
      scripts/tests/isolation_exceptions.toml, each entry with a reason); 1 an unlisted id differs; 2 an
      exceptions entry has no reason; 5 the shuffled runs overlapped for under half of the shorter one
      (inconclusive, never a pass; time queued for slots does not count); 6 a run was not hermetic;
      3 any other error. About 2 h on an idle machine. When a caller cannot hold one process that long,
      ``--runs unshuffled`` (about 30 min) and ``--runs shuffled`` (about 90 min) each save their records
      beside PATH (``<stem>.<run>.record.json``); the second launch to find all three records of the same
      commit judges them, the first exits 4 (recorded, not judged yet).

  gate_speedup_control.py compare --report PATH [--exceptions TOML]
      The same comparison on a saved pair report: 0, 1, 2 and 3 as above (it never judges overlap or
      hermeticity; shuffle-pair does, for the runs it makes).

  gate_speedup_control.py verdict-scenario --case additive|unfinished|flake-list|confirmed|first-rerun-fail|
                                                  listed-confirmed [--sabotage single-rerun] --out PATH [-C REPO]
      The additive verdict on planted tests, through the real gate (in-process ``run_suite``, the default
      runner on two slots, a private flaky journal). Each case is a throwaway commit pair: a tip whose
      scripts/tests holds only an anchor test, and a candidate adding planted tests that fail on their
      first run (``additive``: one fails, one kills its xdist worker; ``unfinished``: one SIGKILLs the
      whole pytest; ``flake-list``: an unlisted and a listed one) or on a chosen rerun round
      (``confirmed``: round 2; ``first-rerun-fail``: round 1; ``listed-confirmed``: round 2, listed in
      flaky.toml). A failure that passes both reruns must leave the verdict PASS with the id unconfirmed
      and journaled; one failing a rerun must make it FAIL with the id confirmed. ``flake-list`` also
      checks that ``suite-gate.py check`` admits the record, the worktree stays clean and a fold adds the
      unlisted id and raises the listed id's count. PATH gets expected against observed per planted id.
      Exit 0 every expectation holds; 4 a verdict or classification differs, or the live journal changed;
      1 any other error. ``--sabotage single-rerun`` counts a test unconfirmed when any one rerun passes,
      so ``first-rerun-fail`` must then exit 4.

  gate_speedup_control.py disk-series [--runs pass,fail] [--keep-max-mb N] [--leak] --out PATH [-C REPO]
      What a gate run leaves behind. Throwaway candidates on a one-test tip (``pass``: a planted test that
      passes; ``fail``: one that fails) run through ``suite-gate.py run`` under ``scripts/cap-run.sh``, in a
      process whose /tmp is a private directory (a mount namespace; the slot directory is bound into it, so
      the worker-slot limit stays shared). Checks that every run ends as planted, that a failing run keeps
      its evidence and a passing one does not, that the kept store (<git common dir>/suite-gate) grew by at
      most N MB (default: config.md suite-gate-keep-max-mb) and holds at most N, and that /tmp is empty.
      PATH gets the figures per run and the names left in /tmp. Exit 0 every check holds; 3 only something
      is left in /tmp; 1 any other check fails; 2 an environment error. ``--leak`` makes the last run keep
      its sandbox (SUITE_GATE_KEEP_SANDBOX=1), so the check must exit 3 and name the sandbox.

Elsewhere exit 2 is a usage or environment error (not enough free disk, git failure).
"""
from __future__ import annotations

import argparse
import collections
import datetime
import fcntl
import hashlib
import json
import os
import queue
import re
import shutil
import signal
import site
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = SCRIPTS_DIR.parent
for _path in (SCRIPTS_DIR, SCRIPTS_DIR / "tests"):
    if str(_path) not in sys.path:
        sys.path.append(str(_path))

import _test_report  # noqa: E402
from lib import flake_list  # noqa: E402
from lib import gate_retention  # noqa: E402
from lib import hermetic_run as hermetic_mod  # noqa: E402
from lib import suite_gate as gate  # noqa: E402
from lib import test_slots  # noqa: E402

SELFTEST_TOTAL, SELFTEST_PER_RUN = 8, 4
SELFTEST_HOLD_S = 30
SELFTEST_POLL_S = 0.05
SELFTEST_WAIT_S = 1.0
MIN_FREE_GB = 10
WAIT_TOLERANCE_S = 2.0
LIMITERS = ("slots", "none")


def _say(line: str) -> None:
    print(line, flush=True)


def _wait_for(predicate, seconds: float, step: float = SELFTEST_POLL_S):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return predicate()


class HeldSampler:
    """Polls who holds slots in a directory; keeps the largest count and every change."""

    def __init__(self, directory: Path, every_s: float):
        self.directory, self.every_s = directory, every_s
        self.max_held = 0
        self.timeline: list[tuple[float, int]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def count(self) -> int:
        return len(test_slots.held(self.directory))

    def _loop(self) -> None:
        while True:
            n = self.count()
            self.max_held = max(self.max_held, n)
            if not self.timeline or self.timeline[-1][1] != n:
                self.timeline.append((round(time.time(), 3), n))
            if self._stop.wait(self.every_s):
                return

    def __enter__(self) -> "HeldSampler":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join()


# ── slots-selftest ────────────────────────────────────────────────────────

def _fake_run(args: argparse.Namespace) -> int:
    """One fake run: hold ``--want`` slots (or, without the limiter, as many free lock files) and sleep."""
    directory = Path(args.dir)
    queued = time.time()
    if args.limiter == "slots":
        limits = test_slots.Limits(SELFTEST_TOTAL, SELFTEST_PER_RUN, directory)
        holder = test_slots.acquire(args.want, limits=limits, poll_s=SELFTEST_POLL_S)
        wait_s = holder.wait_s
    else:
        fds = []
        for i in range(args.want):
            fd = os.open(directory / f"{test_slots.SLOT_PREFIX}free-{os.getpid()}-{i}", os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            fds.append(fd)
        wait_s = time.time() - queued
    _say(json.dumps({"pid": os.getpid(), "wait_s": round(wait_s, 3)}))
    time.sleep(args.hold_s)
    return 0


class FakeRun:
    """One fake run process; a single reader thread hands its acquisition line over a queue."""

    def __init__(self, directory: Path, limiter: str, want: int):
        self.proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "_fake-run", "--dir", str(directory),
             "--want", str(want), "--hold-s", str(SELFTEST_HOLD_S), "--limiter", limiter],
            stdout=subprocess.PIPE, text=True,
            env={k: v for k, v in os.environ.items() if k != test_slots.HELD_ENV})
        self.pid = self.proc.pid
        self._lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=lambda: self._lines.put(self.proc.stdout.readline()), daemon=True).start()

    def acquired(self, seconds: float) -> dict | None:
        """The acquisition line, if it comes within ``seconds``."""
        try:
            line = self._lines.get(timeout=seconds)
        except queue.Empty:
            return None
        return json.loads(line) if line.strip() else None

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()


class FakeRuns:
    def __init__(self, directory: Path, limiter: str):
        self.directory, self.limiter = directory, limiter
        self.runs: list[FakeRun] = []

    def start(self, want: int) -> FakeRun:
        run = FakeRun(self.directory, self.limiter, want)
        self.runs.append(run)
        return run

    def stop(self) -> None:
        for run in self.runs:
            run.kill()


def slots_selftest(limiter: str) -> int:
    directory = Path(tempfile.mkdtemp(prefix="cls-slots-"))
    runs = FakeRuns(directory, limiter)
    problems: list[str] = []
    try:
        with HeldSampler(directory, SELFTEST_POLL_S) as sampler:
            first = runs.start(SELFTEST_PER_RUN)
            first_line = first.acquired(15)
            second = runs.start(SELFTEST_PER_RUN)
            second_line = second.acquired(15)
            if not (first_line and second_line):
                problems.append("the first two runs did not both take their slots")
            third = runs.start(SELFTEST_PER_RUN)
            early = third.acquired(SELFTEST_WAIT_S + 0.5)
            if early is None:
                os.kill(first.pid, signal.SIGKILL)
                first.proc.wait()
                late = third.acquired(5)
                if late is None:
                    problems.append("the third run did not get slots after a holder was killed")
                elif late["wait_s"] <= 0:
                    problems.append(f"the third run waited {late['wait_s']} s")
                elif first.pid in {pid for _, pid in test_slots.held(directory)}:
                    problems.append("the killed holder still holds slots")
            else:
                problems.append(f"the third run did not wait (wait_s={early['wait_s']})")
            time.sleep(10 * SELFTEST_POLL_S)
        if sampler.max_held > SELFTEST_TOTAL:
            problems.insert(0, f"LIMIT EXCEEDED: held={sampler.max_held} > {SELFTEST_TOTAL}")
    finally:
        runs.stop()
        shutil.rmtree(directory, ignore_errors=True)
    _say(f"slots-selftest (limiter {limiter}): max held {sampler.max_held} of {SELFTEST_TOTAL}")
    for problem in problems:
        _say(problem)
    _say("FAIL" if problems else "OK")
    return 1 if problems else 0


# ── concurrent-gates: the record checks (pure) ───────────────────────────

def instant(text: str) -> float | None:
    try:
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def reached_verdict(record: dict) -> bool:
    """PASS or FAIL; UNAVAILABLE is a timeout or a broken run, never a verdict."""
    return record.get("status") in (gate.PASS, gate.FAIL)


def check_record(record: dict, *, per_run: int, collect_ids: list[str]) -> list[str]:
    """What is wrong with one gate run's record; empty when it carries everything the control needs."""
    problems = []
    if not reached_verdict(record):
        problems.append(f"no verdict: {record.get('status')} {record.get('unavailable_reason')!r}")
    argv = record.get("pytest_argv") or []
    if not argv or Path(argv[0]).name != "cap-run.sh":
        problems.append(f"pytest did not run under cap-run.sh: {argv[:1]}")
    if record.get("slots_source") != test_slots.ACQUIRED or not 1 <= (record.get("slots_held") or 0) <= per_run:
        problems.append(f"no slots taken: source={record.get('slots_source')!r} held={record.get('slots_held')}")
    queued, acquired, started = (instant(record.get(k, "")) for k in
                                 ("queued_at", "slots_acquired_at", "timeout_started_at"))
    if None in (queued, acquired, started):
        problems.append("an instant is missing")
    else:
        if not queued <= acquired <= started:
            problems.append("the timeout started before the slots were held")
        wait = record.get("slot_wait_s")
        if not isinstance(wait, (int, float)) or abs((started - queued) - wait) > WAIT_TOLERANCE_S:
            problems.append(f"slot_wait_s {wait} is not timeout_started_at - queued_at ({started - queued:.1f})")
    if not (isinstance(record.get("cpu_s"), (int, float)) and record["cpu_s"] > 0):
        problems.append(f"cpu_s missing or zero: {record.get('cpu_s')}")
    if not isinstance(record.get("peak_disk_mb"), int):
        problems.append(f"peak_disk_mb missing: {record.get('peak_disk_mb')}")
    junit = record.get("junit_xml") or ""
    try:
        ET.parse(junit)
    except (OSError, ET.ParseError) as exc:
        problems.append(f"JUnit XML {junit!r} does not parse: {exc}")
    report = record.get("tests_jsonl") or ""
    try:
        lines = _test_report.read_lines(report)
    except OSError as exc:
        problems.append(f"per-test report {report!r} unreadable: {exc}")
        return problems
    collected = _test_report.collected_ids(lines)
    duplicated = [i for i, n in collections.Counter(collected).items() if n > 1]
    if duplicated:
        problems.append(f"{len(duplicated)} id(s) collected more than once, e.g. {duplicated[0]}")
    if collections.Counter(collected) != collections.Counter(collect_ids):
        missing = sorted(set(collect_ids) - set(collected))
        extra = sorted(set(collected) - set(collect_ids))
        problems.append(f"collected ids differ from --collect-only: {len(missing)} missing, {len(extra)} extra"
                        + (f", e.g. {(missing or extra)[0]}" if missing or extra else ""))
    if record.get("status") == gate.PASS:
        unfinished = sorted(set(collected) - _test_report.finished_ids(lines))
        if unfinished:
            problems.append(f"{len(unfinished)} collected test(s) have no terminal line, e.g. {unfinished[0]}")
    return problems


def check_runs(records: list[dict], *, max_held: int, total: int) -> list[str]:
    """What is wrong across the concurrent runs: the machine limit and the queue."""
    problems = []
    if max_held > total:
        problems.append(f"LIMIT EXCEEDED: held={max_held} > {total}")
    waits = [r.get("slot_wait_s") or 0 for r in records]
    if not any(w > 0 for w in waits):
        problems.append(f"no run waited for slots: slot_wait_s {waits}")
    return problems


# ── concurrent-gates: the edges ───────────────────────────────────────────

def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def collect_only_ids(repo: Path, sha: str) -> list[str]:
    """The node ids ``pytest --collect-only -q`` prints for ``sha``, in a sandbox like the gate's."""
    root = Path(tempfile.mkdtemp(prefix=gate.SANDBOX_PREFIX, dir=gate.SANDBOX_PARENT))
    try:
        build = subprocess.run(
            [str(SCRIPTS_DIR / gate.SANDBOX_SCRIPT), "--source", str(repo), "--core-ref", sha, "--root", str(root)],
            capture_output=True, text=True, env=gate.shared.clean_env(), timeout=gate.BUILD_TIMEOUT_S)
        if build.returncode != 0:
            raise SystemExit(f"sandbox build for --collect-only failed: {build.stdout[-400:]}{build.stderr[-400:]}")
        (root / "tmp").mkdir(exist_ok=True)
        env = gate.suite_env(root, site.getuserbase(), root / "failures.txt",
                             gate.sandbox_scrub_names(SCRIPTS_DIR / gate.SANDBOX_SCRIPT))
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "scripts/tests", "--collect-only", "-q", "-p", "no:xdist",
             "-p", "no:cacheprovider", "--continue-on-collection-errors", "--rootdir", str(root / "core")],
            cwd=root / "core", env=env, capture_output=True, text=True, timeout=1800)
    finally:
        gate.remove_sandbox(root, gate.SANDBOX_PARENT)
    ids = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            break
        if "::" in line:
            ids.append(line)
    if not ids:
        raise SystemExit(f"--collect-only printed no ids (exit {proc.returncode}): {proc.stdout[-400:]}")
    return ids


def _record_path(output: str) -> str:
    for line in output.splitlines():
        if line.startswith("RECORD: "):
            return line[len("RECORD: "):].strip()
    return ""


def candidate_tree(repo: Path, keep: list[str]) -> str:
    """HEAD's tree, or, when ``keep`` names test files, HEAD's tree without every other test file.

    Only ``scripts/tests/**/test_*.py`` files are dropped; helpers, fixtures and conftest stay, so
    the kept tests run exactly as in the full suite. The tree is built in a private index file and
    never touches the working tree or the repository's own index.
    """
    if not keep:
        return _git(repo, "rev-parse", "HEAD^{tree}")
    tracked = _git(repo, "ls-tree", "-r", "--name-only", "HEAD", "--", "scripts/tests").splitlines()
    missing = [path for path in keep if path not in tracked]
    if missing:
        raise SystemExit(f"--tests names files HEAD does not track: {' '.join(missing)}")
    drop = [path for path in tracked
            if path.rsplit("/", 1)[-1].startswith("test_") and path.endswith(".py") and path not in keep]
    with tempfile.TemporaryDirectory(prefix="cls-control-index-") as scratch:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
        for args in (["read-tree", "HEAD"], ["rm", "-q", "--cached", "--", *drop], ["write-tree"]):
            proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env)
            if proc.returncode != 0:
                raise SystemExit(f"git {args[0]} for the reduced tree failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def gate_env(environ) -> dict[str, str]:
    """The environment of a gate run: the sandbox's scrub list and an enclosing run's slot marker removed."""
    scrub = {*gate.sandbox_scrub_names(SCRIPTS_DIR / gate.SANDBOX_SCRIPT), test_slots.HELD_ENV}
    return {k: v for k, v in environ.items() if k not in scrub}


def concurrent_gates(repo: Path, runs: int, out: Path, keep: list[str] | None = None) -> int:
    free_gb = shutil.disk_usage("/").free / 2**30
    if free_gb < MIN_FREE_GB:
        _say(f"only {free_gb:.1f} GB free on /; need {MIN_FREE_GB}")
        return 2
    tip = _git(repo, "merge-base", "HEAD", "origin/main")
    nonce = f"{os.getpid()}-{int(time.time())}"
    tree = candidate_tree(repo, keep or [])
    candidates = [_git(repo, "commit-tree", tree, "-p", "HEAD", "-m", f"cls-control {n} {nonce}")
                  for n in range(1, runs + 1)]
    _say(f"tip {tip[:12]}; candidates {' '.join(c[:12] for c in candidates)}")
    out.parent.mkdir(parents=True, exist_ok=True)
    limits = test_slots.configured()
    env = gate_env(os.environ)
    logs = [out.with_name(f"{out.stem}.run{n}.log") for n in range(1, runs + 1)]
    with HeldSampler(limits.directory, 1.0) as sampler:
        procs = []
        for sha, log in zip(candidates, logs):
            procs.append(subprocess.Popen(
                [sys.executable, str(SCRIPTS_DIR / "suite-gate.py"), "run", "-C", str(repo), "--ref", sha,
                 "--remote-sha", tip],
                stdout=log.open("w", encoding="utf-8"), stderr=subprocess.STDOUT, env=env))
        exits = [proc.wait() for proc in procs]
    collect_ids = collect_only_ids(repo, candidates[0])
    results, records = [], []
    for sha, log, code in zip(candidates, logs, exits):
        path = _record_path(log.read_text(encoding="utf-8", errors="replace"))
        record = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
        records.append(record)
        problems = check_record(record, per_run=limits.per_run, collect_ids=collect_ids) if record else [
            f"no record (exit {code}); see {log}"]
        results.append({"candidate": sha, "exit": code, "record": path, "log": str(log),
                        "status": record.get("status"), "problems": problems,
                        **{k: record.get(k) for k in ("slots_held", "slot_wait_s", "queued_at",
                                                      "slots_acquired_at", "timeout_started_at", "cpu_s",
                                                      "peak_disk_mb", "duration_s", "junit_xml", "tests_jsonl")}})
    overall = check_runs(records, max_held=sampler.max_held, total=limits.total)
    ok = not overall and not any(r["problems"] for r in results)
    summary = {"tip": tip, "runs": results, "max_held": sampler.max_held, "total": limits.total,
               "slot_timeline": sampler.timeline, "collect_only_ids": len(collect_ids),
               "problems": overall, "ok": ok}
    out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    for r in results:
        _say(f"{r['candidate'][:12]} {r['status']} slots={r['slots_held']} wait={r['slot_wait_s']} "
             f"cpu={r['cpu_s']} disk={r['peak_disk_mb']}MB " + ("ok" if not r["problems"] else "; ".join(r["problems"])))
    for problem in overall:
        _say(problem)
    _say(f"max held {sampler.max_held} of {limits.total}; summary {out}")
    _say("OK" if ok else "FAIL")
    return 0 if ok else 1


# ── sandbox-run, verify-named ─────────────────────────────────────────────

def _rerun_failing(repo: Path, sha: str, work: Path, failing: list[str], reruns: int,
                   timeout_s: int, hide: bool = True) -> tuple[list[str], list[str]]:
    """Rerun the failing ids ``reruns`` times, serially, in the sandbox ``work``.

    An id that fails any rerun is confirmed; one that passes every rerun passed on rerun. An id
    without ``::`` (a file that did not collect) is not a test that can pass alone: always confirmed.
    A rerun that does not complete confirms every id still under test."""
    confirmed = [i for i in failing if "::" not in i]
    candidates = [i for i in failing if "::" in i]
    for _ in range(reruns):
        if not candidates:
            break
        run = gate.default_runner(repo, sha, work, select=candidates, timeout_s=timeout_s, hide=hide)
        if not run.build_ok or run.timed_out or run.pytest_exit is None:
            _say(f"a rerun did not complete; {len(candidates)} ids stay confirmed")
            failed_again = set(candidates)
        else:
            failed_again = set(run.failures)
        confirmed += [i for i in candidates if i in failed_again]
        candidates = [i for i in candidates if i not in failed_again]
    return confirmed, candidates


PROBE_FILE = "scripts/tests/test_hermetic_probe.py"
PROBE_ID = f"{PROBE_FILE}::test_the_source_work_tree_is_hidden"


def _probe_source(source_tree: Path) -> str:
    return (
        '"""Planted by gate_speedup_control.py hermetic-run: inside a hermetic run the source work tree is empty."""\n'
        "import os\n\n\n"
        "def test_the_source_work_tree_is_hidden():\n"
        f"    assert os.listdir({str(source_tree)!r}) == []\n")


def sandbox_run(repo: Path, out: Path, select: list[str], root: Path | None, timeout_s: int,
                reruns: int = 0, hide: bool = True, probe: bool = False) -> int:
    """One gate-shaped run of the committed tip of ``repo``; the report names every failing id,
    and with ``reruns`` which of them failed again (``confirmed``) and which passed (``rerun_passed``).

    The report says whether the run was hermetic (``hide`` off or no namespaces: it was not). With
    ``probe`` a test asserting that the source work tree reads as empty is planted in the sandbox;
    whether pytest collected it is ``probe_collected``, its outcome ``probe_passed``, and its id is
    not among ``failing``."""
    free_gb = shutil.disk_usage("/").free / 2**30
    if free_gb < MIN_FREE_GB:
        _say(f"only {free_gb:.1f} GB free on /; need {MIN_FREE_GB}")
        return 2
    sha = _git(repo, "rev-parse", "HEAD")
    if _git(repo, "status", "--porcelain", "--untracked-files=no"):
        _say("warning: tracked files are modified; the sandbox runs the committed tip only")
    os.environ.pop(test_slots.HELD_ENV, None)
    own_root = root is None
    work = Path(tempfile.mkdtemp(prefix=gate.SANDBOX_PREFIX, dir=gate.SANDBOX_PARENT)) if own_root else root
    try:
        if probe:
            failed_build = gate.build_sandbox(repo, sha, work)
            if failed_build is not None:
                _say(f"the sandbox did not build: {failed_build.detail}")
                return 2
            (work / "core" / PROBE_FILE).write_text(_probe_source(repo.resolve()), encoding="utf-8")
        run = gate.default_runner(repo, sha, work, select=select, timeout_s=timeout_s, hide=hide)
        if not run.build_ok or run.timed_out or run.pytest_exit is None:
            _say(f"the run did not complete: {'timed out' if run.timed_out else run.detail or 'no pytest exit'}")
            return 2
        if run.hermetic and run.pytest_exit == gate.hermetic_run.RUNNER_FAILED_EXIT:
            _say(f"the hermetic runner failed before pytest ran: {gate.one_line(run.log_tail[-300:], 300)}")
            return 2
        stem = out.with_suffix("")
        lines = _test_report.read_lines(run.report_path) if run.report_path else []
        collected = _test_report.collected_ids(lines)
        collected_set = set(collected)
        report = {
            "commit": sha, "select": select, "pytest_exit": run.pytest_exit,
            "duration_s": round(run.duration_s, 1), "collected": len(collected),
            "collection_errors": [i for i in run.failures if "::" not in i and i not in collected_set],
            "failing": [i for i in run.failures if i != PROBE_ID],
            "hermetic": run.hermetic, "hermetic_reason": run.hermetic_reason,
        }
        if probe:
            report["probe_collected"] = PROBE_ID in collected_set
            report["probe_passed"] = report["probe_collected"] and PROBE_ID not in run.failures
        out.parent.mkdir(parents=True, exist_ok=True)
        # Copied before any rerun: a rerun in the same sandbox overwrites these files.
        for src, suffix in ((run.junit_path, ".junit.xml"), (run.report_path, ".tests.jsonl")):
            if src:
                shutil.copyfile(src, stem.with_name(stem.name + suffix))
        if reruns:
            report["confirmed"], report["rerun_passed"] = _rerun_failing(
                repo, sha, work, report["failing"], reruns, timeout_s, hide)
        out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        confirmed = f", {len(report['confirmed'])} confirmed by {reruns} reruns" if reruns else ""
        _say(f"{report['collected']} collected, {len(report['failing'])} failing{confirmed}, "
             f"pytest exit {run.pytest_exit}, {report['duration_s']} s; report {out}")
        return 0
    except (gate.GateError, OSError) as exc:
        _say(f"sandbox-run failed: {exc}")
        return 2
    finally:
        if own_root:
            gate.remove_sandbox(work, gate.SANDBOX_PARENT)


MIN_COLLECTED = 15000
DEFAULT_MAX_NAMED = 20
NAMED_SPAN = re.compile(r"`([\w./-]+\.py::[^`\n]+)`")


def named_ids(text: str) -> set[str]:
    """The test ids a document names: its backtick-delimited spans of the form ``path/to/test.py::name``.

    A span, not a word, so a parametrized id with spaces is one id; an id in prose is not named."""
    return {span.strip() for span in NAMED_SPAN.findall(text)}


def verify_named(report_path: Path, named_path: Path, max_named: int) -> int:
    """0: every failing id is named and at most ``max_named`` ids are; 1: one is not named; 3: too many
    named; 4: the report is of a run that did not finish; 2: unreadable input."""
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        text = named_path.read_text(encoding="utf-8")
        failing, collected = list(report["failing"]), int(report["collected"])
        collection_errors = list(report.get("collection_errors") or [])
        rerun_passed = list(report.get("rerun_passed") or [])
        if "confirmed" in report:
            failing = list(report["confirmed"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _say(f"cannot read the inputs: {exc}")
        return 2
    if collected < MIN_COLLECTED or collection_errors:
        _say(f"INCOMPLETE: {collected} tests collected (need {MIN_COLLECTED}), "
             f"{len(collection_errors)} collection errors {' '.join(collection_errors[:3])}")
        return 4
    if rerun_passed:
        _say(f"passed on rerun, not required to be named: {' '.join(rerun_passed)}")
    named = named_ids(text)
    unnamed = [i for i in failing if i not in named]
    if unnamed:
        _say(f"UNNAMED: {len(unnamed)} failing ids not named in {named_path.name}, first: {' '.join(unnamed[:5])}")
        return 1
    if len(named) > max_named:
        _say(f"TOO MANY NAMED: {named_path.name} names {len(named)} ids, at most {max_named} are accepted")
        return 3
    _say(f"OK: {len(failing)} failing, all named; {len(named)} ids named (at most {max_named})")
    return 0


def hermetic_run(repo: Path, out: Path, named: Path, max_named: int, root: Path | None, timeout_s: int,
                 reruns: int, no_hide: bool, probe_only: bool) -> int:
    """One run in the hermetic runner with the probe planted, judged against the documented failures.

    0: the report says ``hermetic: true``, the probe passed and every failure is named; 1: the run was
    not hermetic, or the probe failed, or a failure is not named (or the named document or the run
    do not meet verify-named's bounds); 2: the probe was not collected (pytest never ran it), or any
    other error.

    With ``no_hide`` nothing is hidden, so the verdict rests on the probe alone: 1 when it ran and
    failed (it saw the visible tree), 2 when it passed (the negative control did not fire) or was not
    collected (there is no evidence either way)."""
    select = [PROBE_FILE] if probe_only else []
    code = sandbox_run(repo, out, select, root, timeout_s, reruns, hide=not no_hide, probe=True)
    if code != 0:
        return 2
    try:
        report = json.loads(out.read_text(encoding="utf-8"))
        hermetic, probe_passed = bool(report["hermetic"]), bool(report["probe_passed"])
        probe_collected = bool(report["probe_collected"])
    except (OSError, ValueError, KeyError) as exc:
        _say(f"cannot read the run report: {exc}")
        return 2
    if not probe_collected:
        _say(f"PROBE NOT COLLECTED: {PROBE_ID} never ran (pytest exit {report.get('pytest_exit')}), "
             "so the run proves nothing about hiding")
        return 2
    if no_hide:
        if probe_passed:
            _say("NEGATIVE CONTROL DID NOT FIRE: the probe passed although nothing was hidden")
            return 2
        _say("OK: nothing hidden and the probe failed, as the negative control requires")
        return 1
    if not hermetic:
        _say(f"NOT HERMETIC: {report.get('hermetic_reason') or 'unknown reason'} (probe passed: {probe_passed})")
        return 1
    if not probe_passed:
        _say(f"PROBE FAILED: {PROBE_ID} saw the source work tree")
        return 1
    if probe_only:
        _say("OK: hermetic, probe passed")
        return 0
    verdict = verify_named(out, named, max_named)
    return {0: 0, 2: 2}.get(verdict, 1)


# ── verdict-scenario: the additive verdict on planted tests ───────────────

EXIT_VERDICT_MISMATCH, EXIT_VERDICT_ERROR = 4, 1
PLANTED_DIR = "scripts/tests/_planted"
ANCHOR_FILE = "scripts/tests/test_cls_anchor.py"
LISTED_COUNT = 2
LISTED_SEEN = ("2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z")
PLANTED_HELPERS = '''\
import os
import signal
from pathlib import Path


def count_call(name):
    """How many times this test has been called in the sandbox so far (the marker outlives a rerun)."""
    marker = Path(os.environ["TMPDIR"]) / f"planted-{name}.count"
    calls = int(marker.read_text()) + 1 if marker.exists() else 1
    marker.write_text(str(calls))
    return calls
'''
PLANTED_BODIES = {
    "fails_once": "    assert count_call('fails_once') != 1\n",
    "worker_dies": ("    if count_call('worker_dies') == 1 and os.environ.get('PYTEST_XDIST_WORKER'):\n"
                    "        os._exit(1)\n"),
    "kills_the_run": ("    if count_call('kills_the_run') == 1:\n"
                      "        victim = os.getppid() if os.environ.get('PYTEST_XDIST_WORKER') else os.getpid()\n"
                      "        os.kill(victim, signal.SIGKILL)\n"),
    "flaky_unlisted": "    assert count_call('flaky_unlisted') != 1\n",
    "flaky_listed": "    assert count_call('flaky_listed') != 1\n",
    "fails_twice": "    assert count_call('fails_twice') not in (1, 3)\n",
    "fails_first_rerun": "    assert count_call('fails_first_rerun') not in (1, 2)\n",
}
VERDICT_CASES = ("additive", "unfinished", "flake-list", "confirmed", "first-rerun-fail", "listed-confirmed")
SABOTAGES = ("single-rerun",)


@dataclass(frozen=True)
class Planted:
    """One planted test and what the gate must make of it: how the full run left it, the rerun
    rounds it fails, and whether it ends confirmed or unconfirmed."""
    name: str
    first_run: str
    result: str
    failed_rounds: tuple[int, ...] = ()
    listed: bool = False


@dataclass(frozen=True)
class VerdictCase:
    verdict: str
    planted: tuple[Planted, ...]


VERDICT_SCENARIOS = {
    "additive": VerdictCase(gate.PASS, (Planted("fails_once", "failed", "unconfirmed"),
                                        Planted("worker_dies", "failed", "unconfirmed"))),
    "unfinished": VerdictCase(gate.PASS, (Planted("kills_the_run", "unfinished", "unconfirmed"),)),
    "flake-list": VerdictCase(gate.PASS, (Planted("flaky_unlisted", "failed", "unconfirmed"),
                                          Planted("flaky_listed", "failed", "unconfirmed", listed=True))),
    "confirmed": VerdictCase(gate.FAIL, (Planted("fails_twice", "failed", "confirmed", (2,)),)),
    "first-rerun-fail": VerdictCase(gate.FAIL, (Planted("fails_first_rerun", "failed", "confirmed", (1,)),)),
    "listed-confirmed": VerdictCase(gate.FAIL, (Planted("fails_twice", "failed", "confirmed", (2,), listed=True),)),
}


def planted_id(name: str) -> str:
    return f"{PLANTED_DIR}/test_planted.py::test_{name}"


def planted_source(case: VerdictCase) -> str:
    bodies = "".join(f"\n\ndef test_{p.name}():\n{PLANTED_BODIES[p.name]}" for p in case.planted)
    return PLANTED_HELPERS + bodies


def listed_table(case: VerdictCase) -> dict[str, dict]:
    return {planted_id(p.name): {"count": LISTED_COUNT, "first_seen": LISTED_SEEN[0], "last_seen": LISTED_SEEN[1]}
            for p in case.planted if p.listed}


def sabotaged_passing_every_rerun(new, failed_rounds):
    """The single-rerun mutant: a test counts as unconfirmed when any one rerun passed it."""
    failed_every_round = set(new)
    for failed in failed_rounds:
        failed_every_round &= failed
    return frozenset(new) - frozenset(failed_every_round)


def judge_scenario(case: VerdictCase, record: dict, journal_tests: list[str]) -> tuple[dict, list[str]]:
    """Per planted test what the record says against what it must say, and every disagreement."""
    problems = []
    if record.get("status") != case.verdict:
        problems.append(f"verdict {record.get('status')} != expected {case.verdict} "
                        f"({record.get('unavailable_reason') or 'no reason'})")
    detail = record.get("reruns") or {}
    seen = {}
    for p in case.planted:
        node = planted_id(p.name)
        observed = detail.get(node) or {}
        seen[node] = {"expected": {"first_run": p.first_run, "result": p.result,
                                  "failed_rounds": list(p.failed_rounds)}, "observed": observed}
        if (observed.get("first_run"), observed.get("result"), observed.get("failed_rounds")) != (
                p.first_run, p.result, list(p.failed_rounds)):
            problems.append(f"{node}: observed {observed or 'no rerun detail'}, expected "
                            f"{p.first_run}/{p.result}/{list(p.failed_rounds)}")
        confirmed = node in (record.get("new_failures") or [])
        journaled = node in journal_tests
        if confirmed != (p.result == "confirmed") or journaled != (p.result == "unconfirmed"):
            problems.append(f"{node}: confirmed={confirmed} journaled={journaled} contradict {p.result}")
    return seen, problems


def _index_tree(repo: Path, steps: list[tuple[list[str], str | None]]) -> str:
    """The tree a sequence of git index commands leaves in a private index file (the repository's own
    index and working tree are never touched); the last step must be ``write-tree``."""
    with tempfile.TemporaryDirectory(prefix="cls-control-index-") as scratch:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
        for args, stdin in steps:
            proc = subprocess.run(["git", "-C", str(repo), *args], input=stdin, capture_output=True, text=True,
                                  env=env)
            if proc.returncode != 0:
                raise SystemExit(f"git {args[0]} for a throwaway tree failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def reduced_tree(repo: Path, files: dict[str, str]) -> str:
    """HEAD's tree without any ``test_*.py`` under scripts/tests, plus ``files`` (path -> text)."""
    tracked = _git(repo, "ls-tree", "-r", "--name-only", "HEAD", "--", "scripts/tests").splitlines()
    drop = [p for p in tracked if p.rsplit("/", 1)[-1].startswith("test_") and p.endswith(".py")]
    steps: list[tuple[list[str], str | None]] = [(["read-tree", "HEAD"], None)]
    if drop:
        steps.append((["rm", "-q", "--cached", "--", *drop], None))
    for path, text in sorted(files.items()):
        blob = _git_stdin(repo, ["hash-object", "-w", "--stdin"], text)
        steps.append((["update-index", "--add", "--cacheinfo", f"100644,{blob},{path}"], None))
    steps.append((["write-tree"], None))
    return _index_tree(repo, steps)


def _git_stdin(repo: Path, args: list[str], text: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], input=text, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"git {args[0]} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _commit(repo: Path, tree: str, parent: str, message: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), "commit-tree", tree, "-p", parent, "-m", message],
                          capture_output=True, text=True, env={**os.environ, **gate.GIT_IDENTITY})
    if proc.returncode != 0:
        raise SystemExit(f"git commit-tree failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def journal_state(path: Path) -> dict:
    """Presence, size and sha256 of a journal file; absent is a state of its own."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return {"present": False}
    return {"present": True, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def flake_list_checks(repo: Path, work: Path, candidate: str, tip: str, case: VerdictCase,
                      journal: Path, nonce: str) -> tuple[dict, list[str]]:
    """What a flake-list run leaves behind: an admitting record, a clean worktree, and a fold that
    adds the unlisted id and raises the listed id's count."""
    problems, found = [], {}
    check = subprocess.run([sys.executable, str(SCRIPTS_DIR / "suite-gate.py"), "check", "-C", str(work),
                            "--sha", candidate, "--remote-sha", tip],
                           capture_output=True, text=True, env=gate_env(os.environ))
    found["check_exit"], found["check_line"] = check.returncode, check.stdout.strip()[-200:]
    if check.returncode != 0:
        problems.append(f"suite-gate.py check refused the record: {check.stdout.strip()[-200:]}")
    dirty = _git(work, "status", "--porcelain")
    found["worktree_porcelain"] = dirty
    if dirty:
        problems.append(f"the run left the worktree dirty: {dirty[:200]}")
    branch = f"cls-control-fold-{nonce}"
    _git(repo, "branch", branch, candidate)
    try:
        folded = flake_list.fold(repo, journal, _git(repo, "rev-parse", branch), gate.GIT_IDENTITY)
    finally:
        _git(repo, "branch", "-D", branch)
    if folded is None:
        problems.append("the fold found nothing pending")
        return found, problems
    found["fold"] = {node: fields["count"] for node, fields in folded.table.items()}
    for p in case.planted:
        node = planted_id(p.name)
        expected = LISTED_COUNT + 1 if p.listed else 1
        if folded.table.get(node, {}).get("count") != expected:
            problems.append(f"fold: {node} count {folded.table.get(node, {}).get('count')} != {expected}")
    return found, problems


def verdict_scenario(repo: Path, case_name: str, sabotage: str | None, out: Path) -> int:
    """Run one planted-test case through the real gate; 0 every expectation holds, 4 a verdict or
    classification disagrees (or the live journal changed), 1 any other error."""
    free_gb = shutil.disk_usage("/").free / 2**30
    if free_gb < MIN_FREE_GB:
        _say(f"only {free_gb:.1f} GB free on /; need {MIN_FREE_GB}")
        return EXIT_VERDICT_ERROR
    case = VERDICT_SCENARIOS[case_name]
    nonce = f"{os.getpid()}-{case_name}"
    work = Path(f"/tmp/cls-control-{nonce}")
    journal = Path(f"/tmp/cls-control-{os.getpid()}-journal.jsonl")
    saved_env = os.environ.get(flake_list.JOURNAL_ENV)
    live = Path(saved_env) if saved_env else Path(gate.shared.git_common_dir(repo)) / flake_list.JOURNAL_RELATIVE
    live_before = journal_state(live)
    saved_rule = gate.passing_every_rerun
    os.environ[flake_list.JOURNAL_ENV] = str(journal)
    if sabotage == "single-rerun":
        gate.passing_every_rerun = sabotaged_passing_every_rerun
    added = False
    try:
        anchor = {ANCHOR_FILE: "def test_anchor():\n    assert True\n"}
        tip = _commit(repo, reduced_tree(repo, anchor), "HEAD", f"cls-control tip {nonce}")
        files = {**anchor, f"{PLANTED_DIR}/test_planted.py": planted_source(case)}
        table = listed_table(case)
        if table:
            files[flake_list.LIST_PATH] = flake_list.render_list(table)
        candidate = _commit(repo, reduced_tree(repo, files), tip, f"cls-control candidate {nonce}")
        _say(f"{case_name}: tip {tip[:12]}, candidate {candidate[:12]}")
        _git(repo, "worktree", "add", "--detach", str(work), candidate)
        added = True

        def runner(r, sha, root, *, select, timeout_s):
            return gate.default_runner(r, sha, root, select=select, timeout_s=timeout_s, slots=2)

        outcome = gate.run_suite(work, candidate, tip, runner=runner, tip_reader=lambda: tip)
        record = outcome.record
        entries = flake_list.read_entries(journal)
        seen, problems = judge_scenario(case, record, [e["test"] for e in entries])
        extra: dict = {}
        if case_name == "flake-list":
            extra, more = flake_list_checks(repo, work, candidate, tip, case, journal, nonce)
            problems += more
    finally:
        gate.passing_every_rerun = saved_rule
        if saved_env is None:
            os.environ.pop(flake_list.JOURNAL_ENV, None)
        else:
            os.environ[flake_list.JOURNAL_ENV] = saved_env
        if added:
            subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(work)],
                           capture_output=True, text=True)
        subprocess.run(["git", "-C", str(repo), "worktree", "prune"], capture_output=True, text=True)
        for leftover in (journal, journal.with_name(journal.name + ".lock")):
            leftover.unlink(missing_ok=True)
    live_after = journal_state(live)
    if live_before != live_after:
        problems.append(f"the live journal {live} changed: {live_before} -> {live_after}")
    summary = {"case": case_name, "sabotage": sabotage, "expected_verdict": case.verdict,
               "observed_verdict": record.get("status"), "candidate": candidate, "tip": tip,
               "unavailable_reason": record.get("unavailable_reason"), "planted": seen,
               "journal_entries": len(entries), "live_journal": {"before": live_before, "after": live_after},
               "record": str(outcome.record_path), "extra": extra, "problems": problems, "ok": not problems}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    for problem in problems:
        _say(f"MISMATCH: {problem}")
    _say(f"{case_name}: expected {case.verdict}, observed {record.get('status')}; summary {out}")
    _say("OK" if not problems else "FAIL")
    return 0 if not problems else EXIT_VERDICT_MISMATCH


# ── disk-series: what a gate run leaves behind ────────────────────────────

SERIES_RUNS = ("pass", "fail")
SERIES_TIMEOUT_S = 3600
EXIT_SERIES_FAILED, EXIT_SERIES_ENVIRONMENT, EXIT_SERIES_LEAK = 1, 2, 3
SERIES_PLANTED = {"pass": "def test_planted():\n    assert True\n", "fail": "def test_planted():\n    assert False\n"}
SERIES_STATUS = {"pass": "PASS", "fail": "FAIL"}


def series_command(repo: Path, sha: str, tip: str, timeout_s: int = SERIES_TIMEOUT_S) -> list[str]:
    """One gate run of a ``disk-series`` step, started the way every fan-out trial is: under cap-run.sh."""
    return [str(SCRIPTS_DIR / "cap-run.sh"), "--", sys.executable, str(SCRIPTS_DIR / "suite-gate.py"), "run",
            "-C", str(repo), "--ref", sha, "--remote-sha", tip, "--timeout", str(timeout_s)]


def _listing(directory: Path) -> list[str]:
    return sorted(entry.name for entry in directory.iterdir())


def _series_commits(repo: Path, runs: list[str], nonce: str) -> tuple[str, list[str]]:
    """A tip holding one anchor test and one candidate per run adding a planted test."""
    anchor = {ANCHOR_FILE: "def test_anchor():\n    assert True\n"}
    tip = _commit(repo, reduced_tree(repo, anchor), "HEAD", f"cls-control tip {nonce}")
    candidates = []
    for n, kind in enumerate(runs, 1):
        files = {**anchor, f"{PLANTED_DIR}/test_planted.py": SERIES_PLANTED[kind]}
        candidates.append(_commit(repo, reduced_tree(repo, files), tip, f"cls-control {kind} {n} {nonce}"))
    return tip, candidates


PROTECTED_SLACK_MB = 1.0  # lock files and directory blocks that are neither protected nor evictable


def bound_problem(total_mb: float, bound_mb: float, protected_mb: float | None) -> str | None:
    """The kept store must end at most at the bound, unless what exceeds it is protected (the candidate,
    the tip, runs younger than the gate timeout). ``protected_mb`` is what the last run's retention pass
    reported as protected when it ended over the bound, None when it reported nothing."""
    if total_mb <= bound_mb:
        return None
    if protected_mb is None:
        return (f"the kept store holds {total_mb:.1f} MB after the series, over the bound of {bound_mb} MB, "
                "and the last run's retention pass did not report it")
    if total_mb - protected_mb > PROTECTED_SLACK_MB:
        return (f"the kept store holds {total_mb:.1f} MB after the series, of which only {protected_mb:.1f} MB "
                f"is protected: more than {PROTECTED_SLACK_MB} MB of it stayed that retention could have evicted")
    return None


def series_summary(results: list[dict], *, tip: str, keep_max_mb: int, store_grown_mb: float, store_total_mb: float,
                   leftovers: list[str]) -> dict:
    """What the series reports: the run results, every problem found, and ``ok``."""
    problems = [p for r in results for p in r["problems"]]
    if store_grown_mb > keep_max_mb:
        problems.append(f"the kept store grew {store_grown_mb:.1f} MB, more than {keep_max_mb}")
    over = bound_problem(store_total_mb, keep_max_mb, results[-1]["protected_mb_reported"] if results else None)
    if over:
        problems.append(over)
    return {"runs": results, "tip": tip, "keep_max_mb": keep_max_mb, "store_grown_mb": round(store_grown_mb, 2),
            "store_total_mb": round(store_total_mb, 2), "leftover_in_tmp": leftovers, "problems": problems,
            "ok": not problems and not leftovers}


def _series_run(repo: Path, sha: str, tip: str, kind: str, leak: bool, common: Path, tmp: Path) -> dict:
    env = gate_env(os.environ)
    if leak:
        env[gate_retention.KEEP_ENV] = "1"
    store = common / gate_retention.STORE_DIR
    before = gate_retention.tree_bytes(store)
    proc = subprocess.run(series_command(repo, sha, tip), capture_output=True, text=True, env=env)
    record_file = _record_path(proc.stdout)
    record = json.loads(Path(record_file).read_text(encoding="utf-8")) if record_file else {}
    evidence = common / gate_retention.EVIDENCE_DIR / sha
    problems = []
    if record.get("status") != SERIES_STATUS[kind]:
        problems.append(f"{kind} run {sha[:12]}: status {record.get('status')!r}, planted {SERIES_STATUS[kind]!r} "
                        f"(exit {proc.returncode}; {proc.stdout.strip()[-200:]})")
    if kind == "fail" and not (evidence / "log.txt").is_file():
        problems.append(f"fail run {sha[:12]}: no evidence kept at {evidence}")
    if kind == "pass" and evidence.exists():
        problems.append(f"pass run {sha[:12]}: evidence kept at {evidence}")
    return {"run": kind, "candidate": sha, "exit": proc.returncode, "status": record.get("status"),
            "protected_mb_reported": gate_retention.reported_protected_mb(proc.stderr),
            "record": record_file, "evidence": evidence.is_dir(), "store_growth_mb": round(
                (gate_retention.tree_bytes(store) - before) / gate_retention.MB, 2),
            "tmp_after": _listing(tmp), "problems": problems}


def _series_inner(args: argparse.Namespace) -> int:
    """Runs inside a private mount namespace with its own /tmp (see ``disk_series``)."""
    scratch = Path(args.scratch)
    hermetic_mod.enter_namespaces()
    slots = scratch / "slots"
    hermetic_mod._mount(args.slots, str(slots), hermetic_mod._MS_BIND | hermetic_mod._MS_REC)
    os.environ[test_slots.DIR_ENV] = str(slots)
    tmp = scratch / "tmp"
    hermetic_mod._mount(str(tmp), "/tmp", hermetic_mod._MS_BIND | hermetic_mod._MS_REC)
    os.chmod("/tmp", 0o1777)
    repo = args.repo
    common = Path(gate.shared.git_common_dir(repo))
    store = common / gate_retention.STORE_DIR
    store_before = gate_retention.tree_bytes(store)
    runs = args.runs.split(",")
    tip, candidates = _series_commits(repo, runs, f"{os.getpid()}-disk")
    _say(f"tip {tip[:12]}; candidates {' '.join(c[:12] for c in candidates)}")
    results = []
    for n, (kind, sha) in enumerate(zip(runs, candidates)):
        results.append(_series_run(repo, sha, tip, kind, args.leak and n == len(runs) - 1, common,
                                  Path("/tmp")))
        _say(f"{kind}: {results[-1]['status']}, store grew {results[-1]['store_growth_mb']} MB")
    leftovers = _listing(Path("/tmp"))
    grown_mb = (gate_retention.tree_bytes(store) - store_before) / gate_retention.MB
    total_mb = gate_retention.tree_bytes(store) / gate_retention.MB
    summary = series_summary(results, tip=tip, keep_max_mb=args.keep_max_mb, store_grown_mb=grown_mb,
                             store_total_mb=total_mb, leftovers=leftovers)
    problems = summary["problems"]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    for problem in problems:
        _say(f"MISMATCH: {problem}")
    if leftovers:
        _say(f"LEFTOVER in /tmp: {' '.join(leftovers)}")
    _say(f"store grew {grown_mb:.1f} MB, holds {total_mb:.1f} MB; summary {args.out}")
    _say("OK" if summary["ok"] else "FAIL")
    if problems:
        return EXIT_SERIES_FAILED
    return EXIT_SERIES_LEAK if leftovers else 0


def disk_series(repo: Path, runs: list[str], keep_max_mb: int, leak: bool, out: Path) -> int:
    """Gate runs in a process whose /tmp is a private directory; they must leave it empty.

    Two processes: this one makes the scratch directory and removes it afterwards; the inner one
    (``_disk-series-inner``) enters the namespace and does the runs. Removal happens here, outside the
    namespace, where nothing of the scratch directory is mounted, so it cannot reach the real slot
    directory the inner process bound into it. Exit 0 every run ended as planted, left no kept-store
    growth beyond the bound and left /tmp empty; 3 only a leftover in /tmp; 1 any other mismatch; 2 an
    environment error."""
    free_gb = shutil.disk_usage("/").free / 2**30
    if free_gb < MIN_FREE_GB:
        _say(f"only {free_gb:.1f} GB free on /; need {MIN_FREE_GB}")
        return EXIT_SERIES_ENVIRONMENT
    out.parent.mkdir(parents=True, exist_ok=True)
    slots = test_slots.configured().directory
    Path(slots).mkdir(mode=0o700, parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="disk-series-", dir=out.parent))
    try:
        (scratch / "tmp").mkdir()
        (scratch / "slots").mkdir()
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "_disk-series-inner", "--scratch", str(scratch),
             "--slots", str(slots), "--runs", ",".join(runs), "--keep-max-mb", str(keep_max_mb), "--out", str(out),
             "-C", str(repo), *(["--leak"] if leak else [])], env=gate_env(os.environ))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return proc.returncode


# ── shuffle-pair, compare: isolation judged by outcomes ───────────────────

SHUFFLE_SCHEMA = "shuffle-pair/v1"
EXCEPTIONS_PATH = SCRIPTS_DIR / "tests" / "isolation_exceptions.toml"
SEED_OPTION = "--core-shuffle-seed"
UNSHUFFLED = "unshuffled"
MIN_OVERLAP_FRACTION = 0.5
EXIT_EQUAL, EXIT_DIFFERENT, EXIT_REASONLESS, EXIT_ERROR, EXIT_NO_OVERLAP, EXIT_NOT_HERMETIC = 0, 1, 2, 3, 5, 6
EXIT_PARTIAL = 4
RUNS_ALL, RUNS_UNSHUFFLED, RUNS_SHUFFLED = "all", "unshuffled", "shuffled"


class InputError(Exception):
    """A report or an exceptions file that cannot be used (exit 3)."""


def load_exceptions(path: Path) -> tuple[dict[str, str], list[str]]:
    """(id -> reason for every ``[[exception]]`` entry, the ids whose reason is empty)."""
    try:
        entries = tomllib.loads(path.read_text(encoding="utf-8")).get("exception", [])
        ids = [str(entry["id"]) for entry in entries]
        reasons = [str(entry.get("reason", "")).strip() for entry in entries]
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise InputError(f"cannot read the exceptions {path}: {exc}") from exc
    return dict(zip(ids, reasons)), [i for i, reason in zip(ids, reasons) if not reason]


def differing_ids(runs: list[dict]) -> dict[str, dict[str, str]]:
    """Every test id whose outcome is not the same in all runs: id -> run name -> outcome (``absent`` when not collected)."""
    ids = {i for run in runs for i in run["outcomes"]}
    table = {i: {run["name"]: run["outcomes"].get(i, "absent") for run in runs} for i in ids}
    return {i: seen for i, seen in sorted(table.items()) if len(set(seen.values())) > 1}


def overlap(first: dict, second: dict) -> tuple[float, float]:
    """(seconds both runs were running after their slots were held, the share of the shorter run that is)."""
    start = max(first["running_from"], second["running_from"])
    end = min(first["running_to"], second["running_to"])
    shorter = min(first["running_to"] - first["running_from"], second["running_to"] - second["running_from"])
    seconds = max(0.0, end - start)
    return seconds, (seconds / shorter if shorter > 0 else 0.0)


def _unusable_runs(runs: list[dict]) -> str:
    """Why these whole-suite runs cannot be compared (empty when they can): a run that collected
    nothing, runs that collected different numbers of tests, or a pytest exit other than 0 or 1."""
    collected = {run["name"]: run.get("collected") for run in runs}
    if any(not isinstance(count, int) or count <= 0 for count in collected.values()):
        return f"collected counts {collected}"
    if len(set(collected.values())) != 1:
        return f"the runs collected different numbers of tests {collected}"
    exits = {run["name"]: run.get("pytest_exit") for run in runs}
    if any(code not in (0, 1) for code in exits.values()):
        return f"pytest exits {exits} (only 0 or 1 means the suite ran)"
    return ""


def judge(report: dict, exceptions_path: Path, *, strict: bool) -> int:
    """The verdict of a shuffle-pair report: 0 equal apart from listed exceptions, 1 an unlisted
    difference, 2 an entry without a reason, 3 unusable input. With ``strict`` (the run's own verdict)
    also 6 a run was not hermetic, 3 a run that collected nothing, a different number of tests or did
    not finish with pytest exit 0/1, and 5 the two shuffled runs did not overlap (only when no unlisted
    id differs: a difference is exit 1 regardless)."""
    try:
        runs = report["runs"]
        names = [run["name"] for run in runs]
        listed, reasonless = load_exceptions(exceptions_path)
        differing = differing_ids(runs)
        shuffled = [run for run in runs if run.get("seed") is not None]
    except InputError as exc:
        _say(str(exc))
        return EXIT_ERROR
    except (KeyError, TypeError, AttributeError) as exc:
        _say(f"cannot read the pair report: {exc!r}")
        return EXIT_ERROR
    if len(runs) < 2 or len(set(names)) != len(names):
        _say(f"the report has {len(runs)} runs named {names}: need at least two, distinctly named")
        return EXIT_ERROR
    if reasonless:
        _say(f"REASONLESS: {len(reasonless)} exception(s) without a reason: {' '.join(reasonless[:5])}")
        return EXIT_REASONLESS
    if strict:
        outside = [run["name"] for run in runs if not run.get("hermetic")]
        if outside:
            _say(f"NOT HERMETIC: {' '.join(outside)} ran outside isolation, so the comparison proves nothing")
            return EXIT_NOT_HERMETIC
        unusable = _unusable_runs(runs)
        if unusable:
            _say(f"UNUSABLE: {unusable}: the comparison would prove nothing")
            return EXIT_ERROR
    unlisted = {i: seen for i, seen in differing.items() if i not in listed}
    for i, seen in list(unlisted.items())[:20]:
        _say(f"DIFFERS: {i}: {', '.join(f'{name}={outcome}' for name, outcome in seen.items())}")
    if len(unlisted) > 20:
        _say(f"... and {len(unlisted) - 20} more")
    # A difference is a real ordering failure whether or not the shuffled runs overlapped, so it wins
    # over "inconclusive"; NO OVERLAP only withholds a pass.
    if unlisted:
        _say(f"FAIL: {len(unlisted)} test(s) differ and are not listed in {exceptions_path.name}")
        return EXIT_DIFFERENT
    if strict and len(shuffled) == 2:
        try:
            seconds, share = overlap(*shuffled)
        except (KeyError, TypeError) as exc:
            _say(f"cannot read the shuffled runs' times: {exc!r}")
            return EXIT_ERROR
        if share < MIN_OVERLAP_FRACTION:
            _say(f"NO OVERLAP: the shuffled runs ran together for {seconds:.0f} s, {share:.0%} of the shorter "
                 f"(need {MIN_OVERLAP_FRACTION:.0%}): inconclusive")
            return EXIT_NO_OVERLAP
    used = sorted(set(differing) & set(listed))
    _say(f"OK: {len(runs)} runs, {len(differing)} differing test(s), all listed" + (f": {' '.join(used)}" if used else ""))
    return EXIT_EQUAL


def compare(report_path: Path, exceptions_path: Path) -> int:
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _say(f"cannot read the pair report {report_path}: {exc}")
        return EXIT_ERROR
    return judge(report, exceptions_path, strict=False)


def _timed_run(name: str, seed: int | None, repo: Path, sha: str, junit: Path, slots: int, timeout_s: int,
               into: dict) -> None:
    """One whole-suite run through ``suite_gate.run_tree``; its record (or its failure) goes into ``into[name]``."""
    args = [] if seed is None else [f"{SEED_OPTION}={seed}"]
    try:
        run = gate.run_tree(repo, sha, args, junit, slots, timeout_s=timeout_s)
        if not run.build_ok or run.timed_out or run.pytest_exit is None or not run.report_path:
            raise gate.GateError(f"the run did not complete: {'timed out' if run.timed_out else run.detail or 'no report'}")
        if run.hermetic and run.pytest_exit == gate.hermetic_run.RUNNER_FAILED_EXIT:
            raise gate.GateError(f"the hermetic runner failed before pytest ran: {gate.one_line(run.log_tail[-300:], 300)}")
        lines = _test_report.read_lines(run.report_path)
        started = (instant(run.slots_acquired_at) if run.slots_source == test_slots.ACQUIRED else None) \
            or instant(run.timeout_started_at)
        into[name] = {
            "name": name, "seed": seed, "pytest_exit": run.pytest_exit, "hermetic": run.hermetic,
            "hermetic_reason": run.hermetic_reason, "slots_held": run.slots_held, "slot_wait_s": run.slot_wait_s,
            "running_from": started, "running_to": instant(run.timeout_started_at) + run.duration_s,
            "duration_s": round(run.duration_s, 1), "junit_xml": run.junit_path, "tests_jsonl": run.report_path,
            "collected": len(_test_report.collected_ids(lines)), "outcomes": _test_report.outcomes(lines)}
    except (gate.GateError, OSError) as exc:
        into[name] = {"error": str(exc)}


def _run_record_file(out: Path, name: str) -> Path:
    return out.with_name(f"{out.stem}.{name}.record.json")


def _recorded_run(out: Path, name: str, seed: int | None, sha: str) -> dict | str:
    """The record an earlier ``--runs`` launch saved for ``name``, or why it cannot be used."""
    path = _run_record_file(out, name)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return f"{name}: no record yet ({path.name})"
    except (OSError, ValueError) as exc:
        return f"{name}: cannot read {path.name}: {exc}"
    if record.get("commit") != sha or record.get("seed") != seed:
        return f"{name}: {path.name} is of commit {str(record.get('commit'))[:12]} seed {record.get('seed')}, not {sha[:12]} seed {seed}"
    return record


def shuffle_pair(repo: Path, seeds: list[int], out: Path, exceptions_path: Path, timeout_s: int,
                 runs: str = RUNS_ALL) -> int:
    """Three whole-suite runs of the committed tip in the hermetic runner: unshuffled, then one per seed,
    the shuffled ones started together. The exit code is the verdict ``judge`` gives the recorded runs.

    ``runs`` limits one launch to the unshuffled run or to the two shuffled ones (each launch must fit
    the caller's own time limit); every run's record is saved beside ``out``, and the verdict comes from
    the launch that finds all three records of this commit, EXIT_PARTIAL from one that does not."""
    free_gb = shutil.disk_usage("/").free / 2**30
    if free_gb < MIN_FREE_GB:
        _say(f"only {free_gb:.1f} GB free on /; need {MIN_FREE_GB}")
        return EXIT_ERROR
    if len(seeds) != 2 or seeds[0] == seeds[1]:
        _say(f"need two different seeds, got {seeds}")
        return EXIT_ERROR
    sha = _git(repo, "rev-parse", "HEAD")
    if _git(repo, "status", "--porcelain", "--untracked-files=no"):
        _say("warning: tracked files are modified; the runs use the committed tip only")
    os.environ.pop(test_slots.HELD_ENV, None)
    out.parent.mkdir(parents=True, exist_ok=True)
    slots = test_slots.configured().per_run
    records: dict[str, dict] = {}
    plan = [(UNSHUFFLED, None)] + [(f"seed{seed}", seed) for seed in seeds]
    todo = [(name, seed) for name, seed in plan if runs == RUNS_ALL or (seed is None) == (runs == RUNS_UNSHUFFLED)]

    def start(name: str, seed: int | None) -> threading.Thread:
        junit = out.with_name(f"{out.stem}.{name}.junit.xml")
        thread = threading.Thread(target=_timed_run, args=(name, seed, repo, sha, junit, slots, timeout_s, records))
        thread.start()
        return thread

    _say(f"commit {sha[:12]}: running {' and '.join(name for name, _ in todo)}"
         + (" (the shuffled ones together)" if len(todo) > 1 and todo[0][1] is not None else ""))
    if any(seed is None for _, seed in todo):
        start(UNSHUFFLED, None).join()
    for thread in [start(name, seed) for name, seed in todo if seed is not None]:
        thread.join()
    failed = {name: rec["error"] for name, rec in records.items() if "error" in rec}
    if failed:
        for name, message in failed.items():
            _say(f"{name}: {message}")
        return EXIT_ERROR
    for name, _ in todo:
        _run_record_file(out, name).write_text(json.dumps({**records[name], "commit": sha}) + "\n", encoding="utf-8")
    for name, seed in plan:
        if name not in records:
            earlier = _recorded_run(out, name, seed, sha)
            if isinstance(earlier, str):
                _say(f"recorded {' '.join(n for n, _ in todo)}; not judged yet: {earlier}")
                return EXIT_PARTIAL
            records[name] = earlier
    runs_done = [records[name] for name, _ in plan]
    report = {"schema": SHUFFLE_SCHEMA, "commit": sha, "runs": runs_done}
    out.write_text(json.dumps(report) + "\n", encoding="utf-8")
    for run in runs_done:
        _say(f"{run['name']}: {run['collected']} collected, "
             f"{sum(o == 'failed' for o in run['outcomes'].values())} failed, pytest exit {run['pytest_exit']}, "
             f"{run['duration_s']} s, slots {run['slots_held']}, hermetic {run['hermetic']}")
    return judge(report, exceptions_path, strict=True)


def _isolation_command(args: argparse.Namespace) -> int:
    if args.command == "compare":
        return compare(args.report, args.exceptions)
    try:
        seeds = [int(part) for part in args.seeds.split(",")]
    except ValueError:
        _say(f"--seeds takes two integers, got {args.seeds!r}")
        return EXIT_ERROR
    return shuffle_pair(args.repo.resolve(), seeds, args.out, args.exceptions, args.timeout_s, args.runs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gate_speedup_control.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    selftest = sub.add_parser("slots-selftest", help="three fake runs against a private slot directory")
    selftest.add_argument("--limiter", choices=LIMITERS, default="slots")
    gates = sub.add_parser("concurrent-gates", help="N real suite-gate runs at once")
    gates.add_argument("--runs", type=int, default=3)
    gates.add_argument("--out", required=True, type=Path)
    gates.add_argument("-C", dest="repo", type=Path, default=REPO_DIR)
    gates.add_argument("--tests", nargs="+", default=None, metavar="PATH",
                       help="keep only these test files (repo-relative) in the throwaway candidates")
    sandboxed = sub.add_parser("sandbox-run", help="one gate-shaped run of the committed tip; writes a report")
    sandboxed.add_argument("--out", required=True, type=Path)
    sandboxed.add_argument("-C", dest="repo", type=Path, default=REPO_DIR)
    sandboxed.add_argument("--select", nargs="+", default=[], metavar="PATH",
                           help="run only these test paths (serial, no slots); default: the whole suite")
    sandboxed.add_argument("--root", type=Path, default=None,
                           help="an existing sandbox to reuse and keep; default: a fresh one, removed after")
    sandboxed.add_argument("--timeout-s", type=int, default=gate.DEFAULT_TIMEOUT_S)
    sandboxed.add_argument("--reruns", type=int, default=0,
                           help="rerun each failing id this many times, serially; failing any rerun confirms it")
    sandboxed.add_argument("--no-hide", action="store_true",
                           help="run without hiding anything (the run is not hermetic)")
    hermetic = sub.add_parser("hermetic-run", help="a full run in the hermetic runner with a planted probe")
    hermetic.add_argument("--out", required=True, type=Path)
    hermetic.add_argument("--named", required=True, type=Path)
    hermetic.add_argument("--max-named", type=int, default=DEFAULT_MAX_NAMED)
    hermetic.add_argument("-C", dest="repo", type=Path, default=REPO_DIR)
    hermetic.add_argument("--root", type=Path, default=None)
    hermetic.add_argument("--timeout-s", type=int, default=gate.DEFAULT_TIMEOUT_S)
    hermetic.add_argument("--reruns", type=int, default=2)
    hermetic.add_argument("--no-hide", action="store_true", help="hide nothing: the probe must fail (negative control)")
    hermetic.add_argument("--probe-only", action="store_true", help="collect only the planted probe test")
    pair = sub.add_parser("shuffle-pair", help="three whole-suite runs (unshuffled, two seeds together); judges them")
    pair.add_argument("--seeds", required=True, help="two different integers, e.g. 1,2")
    pair.add_argument("--out", required=True, type=Path)
    pair.add_argument("--exceptions", type=Path, default=EXCEPTIONS_PATH)
    pair.add_argument("--runs", choices=[RUNS_ALL, RUNS_UNSHUFFLED, RUNS_SHUFFLED], default=RUNS_ALL,
                      help="run only the unshuffled run or only the two shuffled ones (see the module docstring)")
    pair.add_argument("-C", dest="repo", type=Path, default=REPO_DIR)
    pair.add_argument("--timeout-s", type=int, default=gate.DEFAULT_TIMEOUT_S)
    cmp_ = sub.add_parser("compare", help="judge a shuffle-pair report against the isolation exceptions")
    cmp_.add_argument("--report", required=True, type=Path)
    cmp_.add_argument("--exceptions", type=Path, default=EXCEPTIONS_PATH)
    verdict = sub.add_parser("verdict-scenario", help="planted tests through the real gate: additive verdict")
    verdict.add_argument("--case", required=True, choices=VERDICT_CASES)
    verdict.add_argument("--sabotage", choices=SABOTAGES, default=None,
                         help="the gate counts a test unconfirmed when any one rerun passes (must exit 4)")
    verdict.add_argument("--out", required=True, type=Path)
    verdict.add_argument("-C", dest="repo", type=Path, default=REPO_DIR)
    series = sub.add_parser("disk-series", help="gate runs in a private /tmp: it must end empty, the store bounded")
    series.add_argument("--runs", default="pass,fail", help="comma-separated, each of: " + ", ".join(SERIES_RUNS))
    series.add_argument("--keep-max-mb", type=int, default=gate_retention.keep_max_mb())
    series.add_argument("--leak", action="store_true",
                        help="the last run keeps its sandbox: the check must exit 3 (negative control)")
    series.add_argument("--out", required=True, type=Path)
    series.add_argument("-C", dest="repo", type=Path, default=REPO_DIR)
    inner = sub.add_parser("_disk-series-inner")
    inner.add_argument("--scratch", required=True)
    inner.add_argument("--slots", required=True)
    inner.add_argument("--runs", required=True)
    inner.add_argument("--keep-max-mb", type=int, required=True)
    inner.add_argument("--leak", action="store_true")
    inner.add_argument("--out", required=True, type=Path)
    inner.add_argument("-C", dest="repo", type=Path, required=True)
    named = sub.add_parser("verify-named", help="check a sandbox-run report against the documented failures")
    named.add_argument("--report", required=True, type=Path)
    named.add_argument("--named", required=True, type=Path)
    named.add_argument("--max-named", type=int, default=DEFAULT_MAX_NAMED)
    fake = sub.add_parser("_fake-run")
    fake.add_argument("--dir", required=True)
    fake.add_argument("--want", type=int, required=True)
    fake.add_argument("--hold-s", type=float, required=True)
    fake.add_argument("--limiter", choices=LIMITERS, required=True)
    words = sys.argv[1:] if argv is None else argv
    isolation = bool(words) and words[0] in ("shuffle-pair", "compare")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits 2 on a usage error, which shuffle-pair and compare reserve for a reasonless entry.
        if isolation and exc.code not in (0, None):
            return EXIT_ERROR
        raise
    if isolation:
        try:
            return _isolation_command(args)
        except SystemExit as exc:  # _git and the sandbox helpers stop with SystemExit(<message>), exit 1
            _say(f"error: {exc}")
            return EXIT_ERROR
        except Exception as exc:  # noqa: BLE001 — every other failure is exit 3, never 1 "differs"
            _say(f"error: {exc!r}")
            return EXIT_ERROR
    if args.command == "slots-selftest":
        return slots_selftest(args.limiter)
    if args.command == "concurrent-gates":
        return concurrent_gates(args.repo.resolve(), args.runs, args.out, args.tests)
    if args.command == "sandbox-run":
        return sandbox_run(args.repo.resolve(), args.out, args.select, args.root, args.timeout_s, args.reruns,
                           hide=not args.no_hide)
    if args.command == "hermetic-run":
        return hermetic_run(args.repo.resolve(), args.out, args.named, args.max_named, args.root, args.timeout_s,
                            args.reruns, args.no_hide, args.probe_only)
    if args.command == "verify-named":
        return verify_named(args.report, args.named, args.max_named)
    if args.command in ("disk-series", "_disk-series-inner"):
        runs = args.runs.split(",")
        if not runs or any(kind not in SERIES_RUNS for kind in runs):
            _say(f"--runs takes a comma-separated list of {', '.join(SERIES_RUNS)}, got {args.runs!r}")
            return EXIT_SERIES_ENVIRONMENT
        try:
            if args.command == "disk-series":
                return disk_series(args.repo.resolve(), runs, args.keep_max_mb, args.leak, args.out)
            return _series_inner(args)
        except (SystemExit, Exception) as exc:  # noqa: BLE001 — anything but a mismatch or a leak is exit 2
            _say(f"error: {exc!r}" if not isinstance(exc, SystemExit) else f"error: {exc}")
            return EXIT_SERIES_ENVIRONMENT
    if args.command == "verdict-scenario":
        try:
            return verdict_scenario(args.repo.resolve(), args.case, args.sabotage, args.out)
        except (SystemExit, Exception) as exc:  # noqa: BLE001 — anything but a verdict mismatch is exit 1
            _say(f"error: {exc!r}" if not isinstance(exc, SystemExit) else f"error: {exc}")
            return EXIT_VERDICT_ERROR
    return _fake_run(args)


if __name__ == "__main__":
    sys.exit(main())
