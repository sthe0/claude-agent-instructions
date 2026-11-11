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
      Run the committed tip of REPO once, the way the gate does (a private clone in a sandbox
      with a fake HOME and a scrubbed environment), and write PATH: the collected count, the
      collection errors and every failing id (an error counts as failing). Exit 0 when the run
      completed and the report was written, whatever the test outcomes; 2 when it could not run.
      ``--select`` runs only the named test paths, serially; ``--root`` reuses and keeps a sandbox.
      The whole suite takes about 40 minutes.

  gate_speedup_control.py verify-named --report PATH --named DOC [--max-named N]
      Compare a sandbox-run report with the document that names the known failures (default at
      most 20 ids): 0 every failing id is named and DOC names at most N; 1 a failing id is not
      named; 3 DOC names more than N ids; 4 the run did not finish (fewer than 15000 collected,
      or a collection error); 2 an input could not be read.

Exit 2 on a usage or environment error (not enough free disk, git failure).
"""
from __future__ import annotations

import argparse
import collections
import datetime
import fcntl
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
import xml.etree.ElementTree as ET
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = SCRIPTS_DIR.parent
for _path in (SCRIPTS_DIR, SCRIPTS_DIR / "tests"):
    if str(_path) not in sys.path:
        sys.path.append(str(_path))

import _test_report  # noqa: E402
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
    """PASS or FAIL; UNAVAILABLE only when it is a flaky rerun, never a timeout or a broken run."""
    if record.get("status") in (gate.PASS, gate.FAIL):
        return True
    return record.get("status") == gate.UNAVAILABLE and "did not fail again on rerun" in str(
        record.get("unavailable_reason"))


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

def sandbox_run(repo: Path, out: Path, select: list[str], root: Path | None, timeout_s: int) -> int:
    """One gate-shaped run of the committed tip of ``repo``; the report names every failing id."""
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
        run = gate.default_runner(repo, sha, work, select=select, timeout_s=timeout_s)
        if not run.build_ok or run.timed_out or run.pytest_exit is None:
            _say(f"the run did not complete: {'timed out' if run.timed_out else run.detail or 'no pytest exit'}")
            return 2
        stem = out.with_suffix("")
        lines = _test_report.read_lines(run.report_path) if run.report_path else []
        collected = _test_report.collected_ids(lines)
        collected_set = set(collected)
        report = {
            "commit": sha, "select": select, "pytest_exit": run.pytest_exit,
            "duration_s": round(run.duration_s, 1), "collected": len(collected),
            "collection_errors": [i for i in run.failures if "::" not in i and i not in collected_set],
            "failing": list(run.failures),
        }
        out.parent.mkdir(parents=True, exist_ok=True)
        for src, suffix in ((run.junit_path, ".junit.xml"), (run.report_path, ".tests.jsonl")):
            if src:
                shutil.copyfile(src, stem.with_name(stem.name + suffix))
        out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        _say(f"{report['collected']} collected, {len(report['failing'])} failing, pytest exit {run.pytest_exit}, "
             f"{report['duration_s']} s; report {out}")
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
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _say(f"cannot read the inputs: {exc}")
        return 2
    if collected < MIN_COLLECTED or collection_errors:
        _say(f"INCOMPLETE: {collected} tests collected (need {MIN_COLLECTED}), "
             f"{len(collection_errors)} collection errors {' '.join(collection_errors[:3])}")
        return 4
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
    named = sub.add_parser("verify-named", help="check a sandbox-run report against the documented failures")
    named.add_argument("--report", required=True, type=Path)
    named.add_argument("--named", required=True, type=Path)
    named.add_argument("--max-named", type=int, default=DEFAULT_MAX_NAMED)
    fake = sub.add_parser("_fake-run")
    fake.add_argument("--dir", required=True)
    fake.add_argument("--want", type=int, required=True)
    fake.add_argument("--hold-s", type=float, required=True)
    fake.add_argument("--limiter", choices=LIMITERS, required=True)
    args = parser.parse_args(argv)
    if args.command == "slots-selftest":
        return slots_selftest(args.limiter)
    if args.command == "concurrent-gates":
        return concurrent_gates(args.repo.resolve(), args.runs, args.out, args.tests)
    if args.command == "sandbox-run":
        return sandbox_run(args.repo.resolve(), args.out, args.select, args.root, args.timeout_s)
    if args.command == "verify-named":
        return verify_named(args.report, args.named, args.max_named)
    return _fake_run(args)


if __name__ == "__main__":
    sys.exit(main())
