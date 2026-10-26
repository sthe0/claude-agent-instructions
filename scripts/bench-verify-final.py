"""Benchmark of verify-final's wall time across two agentctl engines.

Why it exists: the verify-final speed-up (a green-check cache, de-duplication, parallel
re-runs and the opt-in `verify_reads` narrowing) is only worth keeping while it stays
measurably faster than the engine it replaced, and the cache decisions behind the speed
are the part a lucky timing could hide. This script times two consecutive `verify-final`
runs on one fixture under a base engine and a candidate engine and gates both the wall
ratios and the per-check decisions the candidate logged.

Fixture (a temporary git repo, never this one). Check costs are `sleep`, so CPU load
does not move the ratio:

    stage A   cost-a (default 20 s)
    stage B   cost-b (default 10 s), declares `verify_reads = []` where the engine knows it
    stage C   cost-c (default 10 s)
    final_check 1 identical to A, final_check 2 identical to C

Every stage green is recorded with `record-result`, then one docs-only commit changes
docs/bench/note.md, then `verify-final` runs twice. The candidate must log, first run:
A miss, B narrowed, C miss, final 1 and final 2 dedup; second run: A, B and C hit, and
the finals still dedup, because a final_check identical to a stage shares that stage's
cache key within the run and is served by it instead of by a lookup of its own.

Each engine runs in its own child process with a private HOME and no GIT_* variables:
the real repository's index, refs and worktree list are only read, by `git archive`
when an engine comes from a ref.

Usage:
    python3 scripts/bench-verify-final.py --base-ref <ref> [--candidate-ref <ref>]
        [--assert-decisions] [--max-first-ratio 0.7] [--max-repeat-ratio 0.2]
        [--cost-a 20 --cost-b 10 --cost-c 10] [--json-out FILE]

Without --candidate-ref the candidate is the engine of the working tree around this
script. Prints a JSON report; exit 0 when every gate holds, 1 when one fails, 2 when an
engine could not be measured.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve()
SCRIPTS_DIR = SCRIPT.parent
SESSION = "bench"
SERIAL_ENV = "AGENTCTL_VERIFY_FINAL_PARALLEL"
CHECKS = ("stage:1", "stage:2", "stage:3", "final_check:1", "final_check:2")
EXPECTED_DECISIONS = {
    "first": dict(zip(CHECKS, ("miss", "narrowed", "miss", "dedup", "dedup"))),
    "second": dict(zip(CHECKS, ("hit", "hit", "hit", "dedup", "dedup"))),
}
DOCS_NOTE = "docs/bench/note.md"


def _git(repo: Path, *argv: str, env: dict | None = None) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *argv], capture_output=True, text=True, env=env, check=False,
    )
    if done.returncode:
        raise RuntimeError(f"git {' '.join(argv)} failed: {done.stderr.strip()}")
    return done.stdout.strip()


def _clean_env(home: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        HOME=str(home), CLAUDE_CONFIG_DIR=str(home), PYTHONDONTWRITEBYTECODE="1",
        AGENTCTL_STAGE_REVIEW="0", AGENTCTL_ADVISOR="off", AGENTCTL_CHILD_LINKING="off",
        AGENTCTL_GATE_LOG=str(home / "gate-log.jsonl"), LC_ALL="C.UTF-8",
    )
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    env.pop(SERIAL_ENV, None)
    return env


def extract_engine(source_repo: Path, ref: str, dest: Path) -> tuple[Path, str]:
    """The `scripts/` tree of `ref` under `dest`, read with `git archive`; and its sha."""
    sha = _git(source_repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    done = subprocess.run(
        ["git", "-C", str(source_repo), "archive", "--format=tar", sha],
        capture_output=True, check=False,
    )
    if done.returncode:
        raise RuntimeError(f"git archive {ref} failed: {done.stderr.decode().strip()}")
    with tarfile.open(fileobj=io.BytesIO(done.stdout)) as tar:
        members = [m for m in tar.getmembers() if not m.name.startswith("scripts/tests/")]
        tar.extractall(dest, members=members, filter="data")
    return dest / "scripts", sha


def _supports_verify_reads(Criterion) -> bool:
    return "verify_reads" in Criterion.__dataclass_fields__


def _decisions_of_last_run(history: list[dict]) -> dict:
    ends = [i for i, e in enumerate(history) if e.get("event") == "verify_final_run"]
    if not ends:
        return {}
    start = ends[-2] if len(ends) > 1 else -1
    return {
        e["check"]: e["decision"]
        for e in history[start + 1:ends[-1]] if e.get("event") == "verify_final_check"
    }


def _build_state(repo: Path, commands: dict, Criterion, FinalCheck):
    from agentctl.state import (
        Actor, CriterionType, GateRecord, Means, Node, Outcome, Partition, Route,
        SessionState, Stage, StageStatus, Subject, WeightClass,
    )

    reads = {"verify_reads": []} if _supports_verify_reads(Criterion) else {}
    stages = []
    for index, name in enumerate("ABC", 1):
        stages.append(Stage(
            index=index, title=f"stage {name}",
            subject=Subject(material="fixture repo", result="the check exits 0"),
            means=Means(means="sleep", method="run the check"),
            actor=Actor(executor="in_thread"),
            criterion=Criterion(
                criterion_type=CriterionType.MEASURABLE.value, done_criterion="the check passes",
                verify_command=commands[name], expected_exit=0, **(reads if name == "B" else {}),
            ),
            outcome=Outcome(
                status=(StageStatus.ACTIVE if index == 1 else StageStatus.PENDING).value),
        ))
    return SessionState(
        session_id=SESSION, task_id="bench", goal="time verify-final",
        overall_done_criterion="the checks pass", overall_criterion_type=CriterionType.MEASURABLE.value,
        weight_class=WeightClass.SMALL_CHANGE.value, route=Route.IN_THREAD.value,
        node=Node.EXECUTING.value,
        approval=GateRecord("plan_approval", armed=True, passed=True, by="bench"),
        partition=Partition(m1=True, verdict="recommended"),
        stages=stages, current_stage=1, repo_root=str(repo),
        final_check=[FinalCheck(command=commands["A"]), FinalCheck(command=commands["C"])],
    )


def _cli(main, *argv: str) -> tuple[int, str]:
    out = io.StringIO()
    real = sys.stdout
    sys.stdout = out
    try:
        code = main(list(argv))
    finally:
        sys.stdout = real
    return code, out.getvalue()


def measure(job: dict) -> dict:
    """Child-process body: build the fixture under this engine, then time verify-final."""
    sys.path[:] = [job["engine"]] + [p for p in sys.path if Path(p or ".").resolve() != SCRIPTS_DIR]
    from agentctl.cli import main
    from agentctl.state import Criterion, FinalCheck, Node, GateRecord
    from agentctl.store import FileStateStore

    work = Path(job["work"])
    repo = work / "repo"
    home = Path(os.environ["HOME"])
    cost = job["costs"]
    commands = {
        "A": f"sleep {cost['a']:g}", "B": f"sleep {cost['b']:g}", "C": f"sleep {cost['c']:g} && true",
    }
    author = ("-c", "user.name=bench", "-c", "user.email=bench@example.invalid")
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('bench')\n")
    (repo / "docs" / "bench").mkdir(parents=True)
    (repo / DOCS_NOTE).write_text("first\n")
    _git(repo, "add", "-A")
    _git(repo, *author, "commit", "-q", "-m", "bench: code and docs")

    store_root = work / "state"
    FileStateStore(store_root).save(_build_state(repo, commands, Criterion, FinalCheck))
    os.chdir(repo)
    for index in (1, 2, 3):
        if index > 1:
            code, text = _cli(main, "--state-root", str(store_root), "next-stage", "--session", SESSION)
            if code:
                raise RuntimeError(f"next-stage before stage {index} refused: {text.strip()[-600:]}")
        code, text = _cli(
            main, "--state-root", str(store_root), "record-result", "--session", SESSION,
            "--status", "passed",
            "--actual", f"stage {index} ran", "--observation",
            f"the sleep command of stage {index} exited 0 on the fixture tree",
        )
        if code:
            raise RuntimeError(f"record-result stage {index} refused: {text.strip()[-600:]}")

    (repo / DOCS_NOTE).write_text("first\nsecond\n")
    _git(repo, "add", "-A")
    _git(repo, *author, "commit", "-q", "-m", "bench: docs-only note")
    commits = [
        {"sha": line.split()[0], "subject": line.split(" ", 1)[1]}
        for line in reversed(_git(repo, "log", "--format=%h %s").splitlines())
    ]

    runs = {}
    for mode in job["modes"]:
        root = work / f"state-{mode}"
        shutil.copytree(store_root, root)
        if mode == "serial":
            os.environ[SERIAL_ENV] = "1"
        else:
            os.environ.pop(SERIAL_ENV, None)
        timings, decisions = [], []
        for _ in range(2):
            store = FileStateStore(root)
            state = store.load(SESSION)
            state.node = Node.VERIFYING.value
            state.resolution = GateRecord("resolution", armed=False, passed=False)
            store.save(state)
            started = time.perf_counter()
            code, text = _cli(
                main, "--state-root", str(root), "verify-final", "--session", SESSION,
                "--cost-log", str(home / "cost.jsonl"))
            timings.append(time.perf_counter() - started)
            if code:
                raise RuntimeError(f"verify-final ({mode}) refused: {text.strip()[-600:]}")
            decisions.append(_decisions_of_last_run(FileStateStore(root).load(SESSION).history))
        runs[mode] = {
            "first_s": round(timings[0], 3), "second_s": round(timings[1], 3),
            "decisions": {"first": decisions[0], "second": decisions[1]},
        }
    return {"commits": commits, "commands": commands, "runs": runs}


def _run_engine(engine: Path, work: Path, costs: dict, modes: list[str]) -> dict:
    work.mkdir(parents=True)
    home = work / "home"
    home.mkdir()
    out = work / "report.json"
    job = {"engine": str(engine), "work": str(work / "fixture"), "costs": costs, "modes": modes}
    (work / "fixture").mkdir()
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--measure", json.dumps(job), "--measure-out", str(out)],
        capture_output=True, text=True, env=_clean_env(home), check=False,
    )
    if done.returncode or not out.exists():
        raise RuntimeError(f"measuring {engine} failed:\n{done.stderr.strip()[-1500:]}")
    return json.loads(out.read_text())


def _ratio(new: float, old: float) -> float | None:
    return round(new / old, 3) if old > 0 else None


def gate_failures(args: argparse.Namespace, ratios: dict, decisions: dict) -> list[str]:
    failures = []
    if ratios["first"] is None or ratios["first"] > args.max_first_ratio:
        failures.append(f"first-run ratio {ratios['first']} exceeds {args.max_first_ratio}")
    if ratios["repeat"] is None or ratios["repeat"] > args.max_repeat_ratio:
        failures.append(f"repeated-run ratio {ratios['repeat']} exceeds {args.max_repeat_ratio}")
    if args.assert_decisions:
        for run, expected in EXPECTED_DECISIONS.items():
            if decisions[run] != expected:
                failures.append(f"{run}-run decisions {decisions[run]} differ from {expected}")
    return failures


def build_report(args: argparse.Namespace) -> dict:
    repo = Path(_git(SCRIPTS_DIR, "rev-parse", "--show-toplevel"))
    costs = {"a": args.cost_a, "b": args.cost_b, "c": args.cost_c}
    with tempfile.TemporaryDirectory(prefix="bench-verify-final-") as tmp:
        tmp = Path(tmp)
        base_engine, base_sha = extract_engine(repo, args.base_ref, tmp / "base-src")
        if args.candidate_ref:
            candidate_engine, candidate_sha = extract_engine(repo, args.candidate_ref, tmp / "candidate-src")
        else:
            candidate_engine, candidate_sha = SCRIPTS_DIR, "working-tree"
        base = _run_engine(base_engine, tmp / "base", costs, ["serial"])
        candidate = _run_engine(candidate_engine, tmp / "candidate", costs, ["serial", "parallel"])

    base_serial = base["runs"]["serial"]
    serial = candidate["runs"]["serial"]
    parallel = candidate["runs"]["parallel"]
    ratios = {
        "first": _ratio(serial["first_s"], base_serial["first_s"]),
        "repeat": _ratio(serial["second_s"], base_serial["second_s"]),
        "parallel_first": _ratio(parallel["first_s"], base_serial["first_s"]),
        "parallel_repeat": _ratio(parallel["second_s"], base_serial["second_s"]),
    }
    failures = gate_failures(args, ratios, serial["decisions"])
    return {
        "base": {"ref": args.base_ref, "sha": base_sha, "serial": base_serial},
        "candidate": {
            "ref": args.candidate_ref or "working-tree", "sha": candidate_sha,
            "serial": serial, "parallel": parallel,
        },
        "fixture": {"costs_s": costs, "commits": candidate["commits"], "commands": candidate["commands"],
                    "docs_only_change": DOCS_NOTE},
        "ratios": ratios,
        "limits": {"max_first_ratio": args.max_first_ratio, "max_repeat_ratio": args.max_repeat_ratio,
                   "assert_decisions": args.assert_decisions},
        "failures": failures,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--base-ref", help="git ref whose engine is the baseline")
    p.add_argument("--candidate-ref", help="git ref of the engine under test (default: the working tree)")
    p.add_argument("--cost-a", type=float, default=20.0)
    p.add_argument("--cost-b", type=float, default=10.0)
    p.add_argument("--cost-c", type=float, default=10.0)
    p.add_argument("--max-first-ratio", type=float, default=0.7)
    p.add_argument("--max-repeat-ratio", type=float, default=0.2)
    p.add_argument("--assert-decisions", action="store_true",
                   help="also require the candidate's per-check verify_final_check decisions")
    p.add_argument("--json-out", help="also write the report to this file")
    p.add_argument("--measure", help=argparse.SUPPRESS)
    p.add_argument("--measure-out", help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if not args.measure and not args.base_ref:
        p.error("--base-ref is required")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.measure:
        Path(args.measure_out).write_text(json.dumps(measure(json.loads(args.measure))))
        return 0
    try:
        report = build_report(args)
    except (RuntimeError, OSError) as exc:
        print(f"bench-verify-final: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(report, indent=2)
    if args.json_out:
        Path(args.json_out).write_text(text + "\n")
    print(text)
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
