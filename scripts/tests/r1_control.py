#!/usr/bin/env python3
"""Control of the arc-launcher fix: run the suite under the launcher recorder and judge the result.

Why: a test that reaches the real arc launcher in a fresh HOME makes it download a release (up
to tens of GB per sandbox). The fix is a stub arc first on PATH; this script shows, by execution,
that no test run still reaches the launcher and that the fix lost or broke no test.

  baseline --entry plain|gate [--tree DIR]   unchanged code (default tree: the throwaway base worktree)
  control  [--base-ids FILE]                 plain pytest on this tree, canary included
  gate                                       suite-gate.py run on this tree's HEAD, canary included
  verify   --report FILE                     judge a report; exit 0 accepted, 1 refused
  search-selftest                            the .arc search must find a planted .arc

Every run: fresh HOME and TMPDIR under the state directory, the launcher recorder around the whole
command, a free-space guard before it and a watchdog during it, a search for ``.arc`` under the
sandbox root before the root is removed. Outcomes come from junit xml, so plain pytest and the gate
are read the same way. ``verify`` reads only the report; ``control`` and ``gate`` write their section
of it, with the differences from the baseline already computed (the flip rerun needs a tree to run
in). State: $R1_STATE_DIR, default ~/.local/state/disk-guard/r1.
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
REPO = SCRIPTS.parent
STATE = Path(os.environ.get("R1_STATE_DIR") or Path.home() / ".local/state/disk-guard/r1")
BASE_TREE = Path(os.environ.get("R1_BASE_TREE") or str(REPO) + "-base")
ALLOWLIST = HERE / "arc_home_allowlist.toml"
RECORDER = HERE / "arc_launcher_recorder.py"
CANARY_FILE = "scripts/tests/canary_arc_launcher.py"
CANARY_ID = CANARY_FILE + "::test_launcher_run_downloads_nothing"

START_FREE_GB = 80
ABORT_FREE_GB = 15
GATE_LIMIT_S = 1800
PLAIN_TIMEOUT_S = 3400
SCRUB = ("CLAUDE_CODE_SESSION_ID", "AGENT_LINEAGE_IDS", "CLAUDE_CODE_MESSAGING_SOCKET",
         "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_POLICY_LEDGER", "CLAUDE_PROJECT_PLUGIN_DIR",
         "CLAUDE_DIFFICULTY_PLUGIN_DIR", "CLAUDE_AUTH_PROFILE_DIR", "CLAUDE_INSTRUCTION_SANDBOX_PLUGIN_DIR",
         "CLAUDE_INSTRUCTIONS_CANON", "AGENT_RECURSION_DEPTH", "CLAUDE_PROJECT_DIR", "PYTEST_ADDOPTS",
         "PYTHONPATH", "ARC_CANARY_RUN")
BAD = ("failed", "error")


class Refused(Exception):
    pass


# ── outcomes ──────────────────────────────────────────────────────────────

TREE_TOKEN = "<tree>"


def normalize_id(node: str) -> str:
    """A parametrized id may carry the checkout's path; the base tree and the delivery tree differ there."""
    return node.replace(str(BASE_TREE), TREE_TOKEN).replace(str(REPO), TREE_TOKEN)


def parse_junit(path: Path) -> dict[str, str]:
    """node id -> failed | error | skipped | xfailed | passed, from an xunit1 junit file."""
    worst = {"passed": 0, "xfailed": 1, "skipped": 2, "error": 3, "failed": 4}
    out: dict[str, str] = {}
    for case in ET.parse(path).iter("testcase"):
        file, cls, name = case.get("file"), case.get("classname", ""), case.get("name", "")
        if file:
            module = re.sub(r"\.py$", "", file).replace("/", ".")
            rest = cls[len(module):].lstrip(".") if cls.startswith(module) else cls
            node = file + "".join("::" + part for part in rest.split(".") if part) + "::" + name
        else:
            node = (cls + "::" if cls else "") + name
        node = normalize_id(node)
        outcome = "passed"
        for child in case:
            if child.tag == "failure":
                outcome = "failed"
            elif child.tag == "error":
                outcome = "error"
            elif child.tag == "skipped":
                outcome = "xfailed" if "xfail" in (child.get("type", "") + child.get("message", "")) else "skipped"
        if worst[outcome] >= worst[out.get(node, "passed")]:
            out[node] = outcome
    return out


def diff_outcomes(base: dict[str, str], now: dict[str, str]) -> dict[str, list[str]]:
    return {
        "new_failures": sorted(i for i, o in now.items() if o in BAD and base.get(i) not in BAD),
        "new_skips": sorted(i for i, o in now.items() if o == "skipped" and base.get(i) != "skipped"),
        "lost_ids": sorted(set(base) - set(now)),
        "now_passing": sorted(i for i, o in now.items() if o == "passed" and base.get(i) in BAD),
    }


# ── the .arc search ───────────────────────────────────────────────────────

def _tree_bytes(path: Path) -> int:
    total = 0
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def find_arc(root: Path) -> list[dict]:
    """Every entry named .arc under `root`, on root's filesystem, symlinks not followed."""
    dev = os.lstat(root).st_dev
    found = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        keep = []
        for name in dirnames:
            full = os.path.join(dirpath, name)
            if name == ".arc":
                found.append({"path": os.path.relpath(full, root),
                              "bytes": 0 if os.path.islink(full) else _tree_bytes(Path(full))})
            elif os.lstat(full).st_dev == dev:
                keep.append(name)
        dirnames[:] = keep
        found += [{"path": os.path.relpath(os.path.join(dirpath, n), root), "bytes": 0}
                  for n in filenames if n == ".arc"]
    return sorted(found, key=lambda e: e["path"])


def search_selftest() -> int:
    with tempfile.TemporaryDirectory(prefix="r1-search-selftest-") as tmp:
        root = Path(tmp)
        (root / "a" / "home" / ".arc").mkdir(parents=True)
        (root / "a" / "home" / ".arc" / "blob").write_bytes(b"x" * 10)
        (root / "b").mkdir()
        (root / "b" / "link").symlink_to(root / "a")
        paths = [e["path"] for e in find_arc(root)]
        ok = paths == ["a/home/.arc"]
        print(f"search-selftest: found {paths}, want ['a/home/.arc'] (a symlink to it is not followed)")
        return 0 if ok else 1


# ── running ───────────────────────────────────────────────────────────────

def free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 2**30


def require_free(path: Path) -> None:
    if free_gb(path) < START_FREE_GB:
        raise Refused(f"only {free_gb(path):.0f} GB free under {path}; need {START_FREE_GB} GB before a run")


def run_env(root: Path, canary: bool) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTCTL_") and k not in SCRUB}
    env.update(HOME=str(root / "home"), TMPDIR=str(root / "tmp"), AGENTCTL_SLOW_TESTS="all",
               GIT_AUTHOR_NAME="r1", GIT_AUTHOR_EMAIL="r1@localhost",
               GIT_COMMITTER_NAME="r1", GIT_COMMITTER_EMAIL="r1@localhost")
    if canary:
        env["ARC_CANARY_RUN"] = "1"
    return env


def watched(argv: list[str], cwd: Path, env: dict[str, str], limit_s: int, watch: Path) -> tuple[int, str]:
    """Run argv in its own process group; kill it when free space or time runs out. (exit, abort reason)."""
    proc = subprocess.Popen(argv, cwd=cwd, env=env, start_new_session=True)
    started, reason = time.monotonic(), ""
    while True:
        try:
            return proc.wait(timeout=5), reason
        except subprocess.TimeoutExpired:
            pass
        if free_gb(watch) < ABORT_FREE_GB:
            reason = f"free space under {ABORT_FREE_GB} GB"
        elif time.monotonic() - started > limit_s:
            reason = f"over {limit_s} s"
        if reason:
            os.killpg(proc.pid, signal.SIGKILL)
            return proc.wait(), reason


def read_records(log: Path) -> list[dict]:
    sys.path.insert(0, str(HERE))
    from arc_launcher_recorder import read_records as rows  # noqa: PLC0415
    return [{"pid": p, "test": t, "args": a} for p, t, a in rows(log)]


def measured_run(label: str, inner: list[str], cwd: Path, canary: bool, limit_s: int,
                 junit_names: tuple[str, ...] = ("junit.xml",)) -> dict:
    """Run `inner` under the recorder in a fresh sandbox root; return what was observed."""
    work = STATE / "work"
    work.mkdir(parents=True, exist_ok=True)
    require_free(work)
    root = Path(tempfile.mkdtemp(prefix="r1-", dir=work))
    try:
        (root / "home").mkdir()
        (root / "tmp").mkdir()
        log = root / "launcher.log"
        cmd = [sys.executable, str(RECORDER), "--log", str(log), "--",
               *[a.replace("@ROOT@", str(root)) for a in inner]]
        started = time.monotonic()
        code, aborted = watched(cmd, cwd, run_env(root, canary), limit_s + 60, work)
        wall = time.monotonic() - started
        outcomes: dict[str, str] = {}
        for name in junit_names:
            if (root / name).exists():
                outcomes.update(parse_junit(root / name))
        extra = json.loads((root / "inner.json").read_text()) if (root / "inner.json").exists() else {}
        return {"label": label, "exit": code, "aborted": aborted, "wall_s": round(wall, 1),
                "launcher_records": read_records(log), "arc_paths": find_arc(root),
                "outcomes": outcomes, "inner": extra}
    finally:
        if root.parent == work and root.name.startswith("r1-"):
            shutil.rmtree(root, ignore_errors=True)


def canary_argv(junit: str) -> list[str]:
    """The canary in a pytest of its own: named next to its parent directory it would replace the directory."""
    return [sys.executable, "-m", "pytest", CANARY_FILE, "-q", "-p", "no:cacheprovider", "-p", "no:xdist",
            f"--junitxml={junit}", "-o", "junit_family=xunit1"]


def pytest_inner(select: list[str], canary: bool) -> list[str]:
    suite = [str(SCRIPTS / "cap-run.sh"), "--timeout", str(PLAIN_TIMEOUT_S), "--",
             sys.executable, "-m", "pytest", *select,
             "-q", "--continue-on-collection-errors", "-p", "no:cacheprovider",
             "--junitxml=@ROOT@/junit.xml", "-o", "junit_family=xunit1"]
    if not canary:
        return suite
    script = f"{shlex.join(suite)}; rc=$?; {shlex.join(canary_argv('@ROOT@/junit-canary.xml'))}; exit $rc"
    return ["sh", "-c", script]


def run_plain(tree: Path, label: str, canary: bool, select: list[str] | None = None) -> dict:
    return measured_run(label, pytest_inner(select or ["scripts/tests"], canary), tree, canary, PLAIN_TIMEOUT_S,
                        junit_names=("junit.xml", "junit-canary.xml"))


def head(tree: Path, ref: str = "HEAD") -> str:
    return subprocess.run(["git", "-C", str(tree), "rev-parse", ref], capture_output=True, text=True,
                          check=True).stdout.strip()


# ── gate entry: runs inside the recorder ──────────────────────────────────

def _load_gate():
    spec = importlib.util.spec_from_file_location("suite_gate_cli", SCRIPTS / "suite-gate.py")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SCRIPTS))
    spec.loader.exec_module(module)
    return module


def gate_inner(mode: str, repo: Path, tip: str, root: Path, log: Path) -> int:
    """Baseline mode: the gate's own pytest on `repo`'s HEAD. Run mode: suite-gate.py run on it, with a
    runner that adds the canary to the candidate's whole-suite run and notes each run's launcher records."""
    sys.path.insert(0, str(SCRIPTS))
    from lib import suite_gate  # noqa: PLC0415
    sha = head(repo)
    junit_args = lambda name: [f"--junitxml={root / name}", "-o", "junit_family=xunit1"]  # noqa: E731
    result: dict = {}
    if mode == "baseline":
        run = suite_gate.default_runner(repo, sha, root / "gatebox", select=["scripts/tests", *junit_args("junit.xml")],
                                        timeout_s=PLAIN_TIMEOUT_S)
        result = {"duration_s": round(run.duration_s, 1), "exit_code": run.pytest_exit, "timed_out": run.timed_out}
    else:
        phases: list[dict] = []

        def runner(repo_, sha_, root_, *, select, timeout_s):
            first = len(read_records(log))
            whole = sha_ == sha and not select
            args = ["scripts/tests", *junit_args("junit.xml")] if whole else list(select)
            run = suite_gate.default_runner(repo_, sha_, root_, select=args, timeout_s=timeout_s)
            if whole:
                subprocess.run(canary_argv(str(root / "junit-canary.xml")), cwd=repo_, check=False)
            phases.append({"phase": "candidate" if sha_ == sha else "baseline", "whole_suite": whole,
                           "records": read_records(log)[first:], "arc_paths": find_arc(root_),
                           "duration_s": round(run.duration_s, 1)})
            return run

        cli = _load_gate()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["run", "--ref", sha, "--remote-sha", tip, "-C", str(repo), "--timeout", str(GATE_LIMIT_S)],
                            runner=runner, sandbox_parent=root)
        record_line = [ln for ln in buf.getvalue().splitlines() if ln.startswith("RECORD: ")]
        print(buf.getvalue()[-3000:])
        gate_record = json.loads(Path(record_line[-1][8:]).read_text()) if record_line else {}
        result = {"gate_exit": code, "phases": phases,
                  "status": gate_record.get("status"), "baseline_cache": gate_record.get("baseline_cache"),
                  "duration_s": gate_record.get("duration_s"), "new_failures": gate_record.get("new_failures", []),
                  "unconfirmed": gate_record.get("unconfirmed", [])}
    (root / "inner.json").write_text(json.dumps(result))
    return 0


def run_gate(mode: str, tree: Path, tip: str) -> dict:
    inner = [sys.executable, str(Path(__file__).resolve()), "_gate-inner", "--mode", mode, "--repo", str(tree),
             "--tip", tip, "--root", "@ROOT@", "--log", "@ROOT@/launcher.log"]
    return measured_run(f"gate-{mode}", inner, tree, mode == "run", PLAIN_TIMEOUT_S * 2,
                        junit_names=("junit.xml", "junit-canary.xml"))


# ── subcommands ───────────────────────────────────────────────────────────

def _load(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")


def cmd_baseline(args) -> int:
    tree = Path(args.tree)
    tip = head(tree)
    run = run_plain(tree, "baseline-plain", False) if args.entry == "plain" else run_gate("baseline", tree, tip)
    store = _load(STATE / "baseline-ids.json")
    store[args.entry] = {"commit": tip, "tree": str(tree), "wall_s": run["wall_s"], "exit": run["exit"],
                         "aborted": run["aborted"], "ids": run["outcomes"],
                         "launcher_records": run["launcher_records"], "arc_paths": run["arc_paths"],
                         "inner": run["inner"]}
    _save(STATE / "baseline-ids.json", store)
    counts = {o: sum(1 for v in run["outcomes"].values() if v == o) for o in sorted(set(run["outcomes"].values()))}
    print(f"baseline {args.entry}: exit {run['exit']} {run['aborted']}, {run['wall_s']} s, {counts}, "
          f"{len(run['launcher_records'])} launcher record(s), {len(run['arc_paths'])} .arc path(s)")
    return 0 if run["outcomes"] and not run["aborted"] else 1


def baseline_ids(entry: str) -> dict[str, str]:
    ids = _load(STATE / "baseline-ids.json").get(entry, {}).get("ids", {})
    return {normalize_id(node): outcome for node, outcome in ids.items()}


def flaky_ids(candidates: list[str], base: dict[str, str], now: dict[str, str]) -> list[str]:
    """Candidates that flip on a rerun of their whole file: the unchanged tree's outcome differs from the
    baseline's, or this tree's differs from the run that failed them. A failure the change causes does
    neither: it reproduces here and passes in the unchanged tree."""
    known = sorted(i for i in candidates if i in base)
    if not known or not BASE_TREE.is_dir():
        return []
    files = sorted({i.split("::")[0] for i in known})
    in_base = run_plain(BASE_TREE, "flip-rerun-base", False, select=files)["outcomes"]
    in_here = run_plain(REPO, "flip-rerun-here", False, select=files)["outcomes"]
    return [i for i in known if in_base.get(i) != base[i] or in_here.get(i) != now.get(i)]


def entry_section(run: dict, base: dict[str, str], gate_info: dict | None = None) -> dict:
    diff = diff_outcomes(base, run["outcomes"])
    flaky = flaky_ids(sorted(set(diff["new_failures"]) | set(diff["new_skips"])), base, run["outcomes"])
    section = {"wall_s": run["wall_s"], "exit": run["exit"], "aborted": run["aborted"],
               "launcher_records": run["launcher_records"], "arc_paths": run["arc_paths"],
               "total_ids": len(run["outcomes"]), "canary_outcome": run["outcomes"].get(CANARY_ID, "absent"),
               "flaky": flaky, **diff}
    if gate_info:
        section["gate"] = gate_info
    return section


def _write_section(name: str, section: dict, deleted: list[str], commit: str, base_commit: str) -> None:
    report = _load(STATE / "report.json")
    report.update(schema=1, commit=commit, base=base_commit, canary_id=CANARY_ID)
    report.setdefault("entries", {})[name] = section
    report["deleted_tests"] = dict(d.split("=", 1) for d in deleted)
    _save(STATE / "report.json", report)


def cmd_control(args) -> int:
    base = baseline_ids("plain")
    if not base:
        raise Refused("no plain baseline in baseline-ids.json; run `baseline --entry plain` first")
    run = run_plain(REPO, "control-plain", True)
    section = entry_section(run, base)
    _write_section("plain", section, args.deleted, head(REPO), head(BASE_TREE))
    print(json.dumps({k: (v if not isinstance(v, list) else len(v)) for k, v in section.items()}, indent=1))
    return 0


def cmd_gate(args) -> int:
    base = baseline_ids("gate")
    if not base:
        raise Refused("no gate baseline in baseline-ids.json; run `baseline --entry gate` first")
    tip = head(REPO, "origin/main")
    run = run_gate("run", REPO, tip)
    info = run["inner"]
    candidate = [p for p in info.get("phases", []) if p["phase"] == "candidate" and p["whole_suite"]]
    run["launcher_records"] = candidate[0]["records"] if candidate else []
    run["arc_paths"] += [e for p in info.get("phases", []) if p["phase"] == "candidate" for e in p["arc_paths"]]
    run["wall_s"] = info.get("duration_s") or run["wall_s"]
    section = entry_section(run, base, {k: info.get(k) for k in
                                        ("status", "baseline_cache", "new_failures", "unconfirmed", "gate_exit")})
    section["other_phase_launcher_records"] = sum(len(p["records"]) for p in info.get("phases", [])
                                                  if p not in candidate)
    _write_section("gate", section, args.deleted, head(REPO), head(BASE_TREE))
    print(json.dumps({k: (v if not isinstance(v, list) else len(v)) for k, v in section.items()}, indent=1))
    return 0


# ── verify ────────────────────────────────────────────────────────────────

def _sanitized(test_id: str) -> str:
    return re.sub(r"\W", "_", test_id.rsplit("::", 1)[-1])[:30]


def allowed_arc(path: str, allowlist: list[dict]) -> str:
    for entry in allowlist:
        part = entry.get("path_part") or _sanitized(entry["test"])
        if part and part in path and entry.get("reason"):
            return entry["test"]
    return ""


def verify_entry(name: str, sec: dict, canary_id: str, deleted: dict, allowlist: list[dict]) -> list[str]:
    problems = []
    test_name = canary_id.split("::", 1)[-1]
    foreign = [r for r in sec.get("launcher_records", []) if test_name not in r.get("test", "")]
    if foreign:
        problems.append(f"{name}: {len(foreign)} launcher record(s) not made by the canary, first test {foreign[0].get('test')!r}")
    if not sec.get("launcher_records"):
        problems.append(f"{name}: no launcher record: the canary's own call was not seen, so the recorder was not in effect")
    if sec.get("canary_outcome") != "passed":
        problems.append(f"{name}: canary outcome {sec.get('canary_outcome')!r}, want 'passed'")
    for entry in sec.get("arc_paths", []):
        if not allowed_arc(entry["path"], allowlist):
            problems.append(f"{name}: .arc left under the sandbox root: {entry['path']} ({entry.get('bytes')} bytes)")
    flaky = set(sec.get("flaky", []))
    for key in ("new_failures", "new_skips"):
        left = [i for i in sec.get(key, []) if i not in flaky]
        if left:
            problems.append(f"{name}: {len(left)} {key.replace('_', ' ')} that did not flip on a rerun of the unchanged code, first {left[0]}")
    lost = [i for i in sec.get("lost_ids", []) if not deleted.get(i)]
    if lost:
        problems.append(f"{name}: {len(lost)} test id(s) lost without a deletion reason, first {lost[0]}")
    if sec.get("aborted") or sec.get("exit") is None:
        problems.append(f"{name}: run aborted ({sec.get('aborted')!r})")
    if name == "gate":
        if sec.get("wall_s", GATE_LIMIT_S) >= GATE_LIMIT_S:
            problems.append(f"gate: wall time {sec.get('wall_s')} s, want under {GATE_LIMIT_S}")
        if (sec.get("gate") or {}).get("status") != "PASS":
            problems.append(f"gate: suite-gate status {(sec.get('gate') or {}).get('status')!r}, want PASS")
    return problems


def cmd_verify(args) -> int:
    try:
        report = json.loads(Path(args.report).read_text())
        allowlist = tomllib.loads(ALLOWLIST.read_text()).get("allow", []) if ALLOWLIST.exists() else []
        entries = report["entries"]
        problems = [] if set(entries) == {"plain", "gate"} else [f"report holds entries {sorted(entries)}, want plain and gate"]
        for name, sec in sorted(entries.items()):
            problems += verify_entry(name, sec, report["canary_id"], report.get("deleted_tests", {}), allowlist)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        problems = [f"report unreadable or malformed: {exc!r}"]
    for line in problems:
        print("REFUSED:", line)
    if not problems:
        notes = {n: {"now_passing": len(s["now_passing"]), "flaky": s["flaky"], "wall_s": s["wall_s"]}
                 for n, s in report["entries"].items()}
        print("ACCEPTED:", json.dumps(notes))
    return 1 if problems else 0


def cmd_summarize(args) -> int:
    """Print which test files reached the launcher and which arguments they passed."""
    section = _load(Path(args.file))
    for key in filter(None, (args.entry or "").split(".")):
        section = section[key]
    records = section.get("launcher_records", [])
    by_file = collections.Counter(r["test"].split("::")[0] or "(outside a test)" for r in records)
    by_args = collections.Counter(r["args"].split(" ")[0] for r in records)
    print(f"{len(records)} record(s); files: {by_file.most_common(args.top)}")
    print(f"first argument: {by_args.most_common(args.top)}")
    for record in records[:args.top] if len(records) <= args.top else []:
        print(f"record pid={record['pid']} test={record['test']!r} args={record['args']!r}")
    for key in ("new_failures", "new_skips", "lost_ids", "now_passing", "flaky"):
        print(f"{key}: {section.get(key, [])[:args.top]}")
    for arc in section.get("arc_paths", [])[:args.top]:
        print(f".arc {arc['bytes']} B  {arc['path']}")
    return 0


def cmd_probe(args) -> int:
    """One recorded pytest run per test file, to see which file reaches the launcher."""
    for name in args.files:
        run = run_plain(Path(args.tree), "probe", False, select=[name])
        paths = [e["path"] for e in run["arc_paths"]]
        print(f"{name}: {len(run['launcher_records'])} record(s) {[r['args'] for r in run['launcher_records']][:3]}"
              f" .arc {paths[:2]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("probe")
    pr.add_argument("files", nargs="+")
    pr.add_argument("--tree", default=str(REPO))
    s = sub.add_parser("summarize")
    s.add_argument("--file", required=True)
    s.add_argument("--entry")
    s.add_argument("--top", type=int, default=10)
    b = sub.add_parser("baseline")
    b.add_argument("--entry", choices=("plain", "gate"), required=True)
    b.add_argument("--tree", default=str(BASE_TREE))
    c = sub.add_parser("control")
    g = sub.add_parser("gate")
    for p in (c, g):
        p.add_argument("--deleted", action="append", default=[], metavar="ID=REASON")
    v = sub.add_parser("verify")
    v.add_argument("--report", required=True)
    sub.add_parser("search-selftest")
    i = sub.add_parser("_gate-inner")
    i.add_argument("--mode", required=True)
    i.add_argument("--repo", required=True)
    i.add_argument("--tip", required=True)
    i.add_argument("--root", required=True)
    i.add_argument("--log", required=True)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "baseline":
            return cmd_baseline(args)
        if args.cmd == "control":
            return cmd_control(args)
        if args.cmd == "gate":
            return cmd_gate(args)
        if args.cmd == "verify":
            return cmd_verify(args)
        if args.cmd == "probe":
            return cmd_probe(args)
        if args.cmd == "summarize":
            return cmd_summarize(args)
        if args.cmd == "search-selftest":
            return search_selftest()
        return gate_inner(args.mode, Path(args.repo), args.tip, Path(args.root), Path(args.log))
    except Refused as exc:
        print(f"r1_control: refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
