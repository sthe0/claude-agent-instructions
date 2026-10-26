"""Pre-land suite gate: a change may land on main only if it adds no test failure the tip lacks.

Difficulty removed: a change that passes review can still break a test far from the files it
touched, and nothing ran the whole suite before the push. The rule "landing into origin/main
refuses a commit whose full-suite run fails a test that the remote tip's own run does not" is
decidable from two suite runs, so the decision is pure functions of recorded failed node ids; git,
the filesystem, the sandbox and pytest are the edges (``run_suite``, ``default_runner``).

Both runs happen in an isolated sandbox root built by scripts/instruction-sandbox.sh: the candidate
in one, and - only when the candidate fails something - the tip in another, cached by tip sha. A
failure present in the tip's baseline is not the candidate's doing and does not block; a failure
absent from it is rerun once and blocks when it fails again. Memory and docs markdown cannot fail
a test, so a landing that changes nothing else is exempt, decided from git's own path list.

Built on scripts/lib/landing_gate.py (binding, record store, pre-push loop); records are
``suite-run/v1`` under ``<git common dir>/suite-gate/<candidate_sha>.json`` and ``suite-baseline/v1``
under ``<git common dir>/suite-gate/baseline/<tip_sha>.json``.

Fail closed throughout: a missing, unreadable or inconsistent record, an unverifiable run and any
git error are refusals, never admissions.
"""
from __future__ import annotations

import contextlib
import fcntl
import importlib.util
import os
import re
import shutil
import signal
import site
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Protocol, Sequence

from . import landing_gate as shared
from .landing_gate import ADMITTED, NOT_REQUIRED, REFUSED, Decision, GateError, PrePushResult, defuse, one_line, short

SCHEMA = "suite-run/v1"
BASELINE_SCHEMA = "suite-baseline/v1"
RECORD_DIR = "suite-gate"
BASELINE_DIR = "suite-gate/baseline"
GATE_SCRIPT = "scripts/suite-gate.py"
DEFAULT_REMOTE = "origin"
DEFAULT_TRUNK = "main"
SANDBOX_SCRIPT = "instruction-sandbox.sh"
SANDBOX_PARENT = "/tmp"
SANDBOX_PREFIX = "instruction-sandbox."

CONFIG_KEY = "suite-gate-timeout-s"
DEFAULT_TIMEOUT_S = 1800  # config.md: suite-gate-timeout-s (a test keeps the two equal)
BUILD_TIMEOUT_S = 600
KILL_GRACE_S = 10
LOG_TAIL_LINES = 400
CAP_RUN_SLACK_S = 30

PASS, FAIL, UNAVAILABLE = "PASS", "FAIL", "UNAVAILABLE"
STATUSES = (PASS, FAIL, UNAVAILABLE)
COMPLETE = "COMPLETE"

EXEMPT = "exempt"
REQUIRED = "required"
EXEMPT_DIRS = ("memory-global/", "docs/")
EXEMPT_SUFFIX = ".md"

RUN_COMMAND = "python3 scripts/suite-gate.py run"

FAILURES_ENV = "SUITE_GATE_FAILURES"
PLUGIN_MODULE = "suite_gate_plugin"
PLUGIN_SOURCE = '''\
"""pytest plugin of the suite gate: record the node id of every failed test and collection."""
import os


class _Recorder:
    def __init__(self, path):
        self.path = path
        self.failed = set()

    def pytest_runtest_logreport(self, report):
        if report.failed:
            self.failed.add(report.nodeid)

    def pytest_collectreport(self, report):
        if report.failed:
            self.failed.add(report.nodeid)

    def pytest_sessionfinish(self, session):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("".join(node + "\\n" for node in sorted(self.failed)))


def pytest_configure(config):
    path = os.environ.get(FAILURES_ENV)
    if path and not hasattr(config, "workerinput"):
        config.pluginmanager.register(_Recorder(path), "suite-gate-recorder")
'''.replace("FAILURES_ENV", repr(FAILURES_ENV))

GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "suite-gate", "GIT_AUTHOR_EMAIL": "suite-gate@localhost",
    "GIT_COMMITTER_NAME": "suite-gate", "GIT_COMMITTER_EMAIL": "suite-gate@localhost",
}


def admission_line(candidate_sha: str) -> str:
    """The one line the hook prints on admitting a push through a suite record."""
    return f"pre-push: suite gate admitted {candidate_sha}"


# ── configuration ─────────────────────────────────────────────────────────

def configured_timeout_s(config_path: str | Path | None = None) -> int:
    """Per-run wall-clock limit: the config.md row, else the equal code constant."""
    try:
        from agentctl.config import parse_config_md

        raw = parse_config_md(Path(config_path) if config_path else None).get(CONFIG_KEY, "")
        value = int(raw)
    except (ImportError, OSError, ValueError):
        return DEFAULT_TIMEOUT_S
    return value if value > 0 else DEFAULT_TIMEOUT_S


# ── applicability and exemption ───────────────────────────────────────────

def is_exempt_path(path: str) -> bool:
    parts = path.split("/")
    if not path or path.startswith("/") or "" in parts or "." in parts or ".." in parts:
        return False
    return path.endswith(EXEMPT_SUFFIX) and path.startswith(EXEMPT_DIRS)


def gate_applies(repo: str | Path, tip: str, candidate: str) -> bool:
    """The gate binds a repository that carries the suite gate in the tip or the candidate."""
    return shared.path_in_tree(repo, tip, GATE_SCRIPT) or shared.path_in_tree(repo, candidate, GATE_SCRIPT)


def landing_exemption(repo: str | Path, tip: str, candidate: str) -> list[str] | None:
    """The changed files when the landing is memory/docs markdown only, else None.

    The only input is git's own path list for the two commits; nothing a caller passes can
    name a file exempt, and an empty list is not an exemption.
    """
    paths = shared.changed_paths(repo, tip, candidate)
    if paths and all(is_exempt_path(p) for p in paths):
        return paths
    return None


def landing_scope(repo: str | Path, tip: str, candidate: str) -> tuple[str, list[str]]:
    """(NOT_REQUIRED | EXEMPT | REQUIRED, the exempt files)."""
    if not gate_applies(repo, tip, candidate):
        return NOT_REQUIRED, []
    exempt = landing_exemption(repo, tip, candidate)
    if exempt is not None:
        return EXEMPT, exempt
    return REQUIRED, []


# ── run results and their classification ──────────────────────────────────

@dataclass(frozen=True)
class SuiteRun:
    pytest_exit: int | None
    failures: tuple[str, ...] = ()
    sandbox_core_sha: str = ""
    timed_out: bool = False
    build_ok: bool = True
    xdist_ok: bool = True
    detail: str = ""
    log_tail: str = ""
    build_s: float = 0.0
    duration_s: float = 0.0


class Runner(Protocol):
    def __call__(self, repo: Path, sha: str, root: Path, *,
                 select: Sequence[str], timeout_s: int) -> SuiteRun: ...


def classify(run: SuiteRun, expected_sha: str) -> tuple[tuple[str, ...], str]:
    """(failed node ids, unavailable reason); a non-empty reason means the ids prove nothing."""
    if run.timed_out:
        return (), f"pytest did not finish within the timeout{_detail(run)}"
    if not run.build_ok:
        return (), f"sandbox build failed{_detail(run)}"
    if not run.xdist_ok:
        return (), "pytest-xdist is not importable, so the suite cannot finish in time"
    if run.sandbox_core_sha != expected_sha:
        return (), (f"sandbox was built from {short(run.sandbox_core_sha)}, "
                    f"not the commit under test {short(expected_sha)}")
    ids = tuple(sorted(set(run.failures)))
    if run.pytest_exit == 0:
        if ids:
            return (), "pytest exited 0 yet the recorder saw failures"
        return (), ""
    if run.pytest_exit == 1:
        if not ids:
            return (), "pytest exited 1 but the recorder saw no failed test"
        return ids, ""
    return (), f"pytest exited {run.pytest_exit}{_detail(run)}"


def _detail(run: SuiteRun) -> str:
    return f": {one_line(run.detail, 200)}" if run.detail else ""


def subtract(candidate_ids: Iterable[str], baseline_ids: Iterable[str]) -> list[str]:
    """Candidate failures the baseline does not have, by node id."""
    known = set(baseline_ids)
    return sorted(i for i in set(candidate_ids) if i not in known)


def derive_status(new_failures: Sequence[str], unavailable_reason: str) -> str:
    if new_failures:
        return FAIL
    if unavailable_reason:
        return UNAVAILABLE
    return PASS


# ── admission ─────────────────────────────────────────────────────────────

def _waiver_ok(waiver: object) -> bool:
    return isinstance(waiver, dict) and bool(one_line(waiver.get("reason") or "")) and bool(waiver.get("at"))


def admission_summary(record: dict) -> str:
    waiver = record.get("waiver")
    if record.get("status") == UNAVAILABLE and isinstance(waiver, dict):
        return one_line(f"UNAVAILABLE, waived: {waiver.get('reason', '')} [{record.get('unavailable_reason', '')}]", 600)
    return str(record.get("status"))


def record_admits(record: object, candidate_sha: str, tip: str) -> tuple[bool, str]:
    """(admitted, reason). Binds the record to the candidate and to the live remote tip."""
    if not isinstance(record, dict):
        return False, "record is not a JSON object"
    bound, why_not = shared.check_binding(
        record, schema=SCHEMA, candidate_sha=candidate_sha, live_remote_sha=tip,
        rerun_hint="merge it into the candidate and rerun the suite gate",
    )
    if not bound:
        return False, why_not
    status = record.get("status")
    new_failures = record.get("new_failures")
    reason = record.get("unavailable_reason")
    if status not in STATUSES or not isinstance(new_failures, list) or not isinstance(reason, str):
        return False, "record is malformed"
    if derive_status(new_failures, reason) != status:
        return False, "record's status contradicts its own failure lists"
    if status == FAIL:
        shown = ", ".join(one_line(i, 120) for i in new_failures[:3])
        more = f", +{len(new_failures) - 3} more" if len(new_failures) > 3 else ""
        return False, f"the candidate adds {len(new_failures)} test failure(s) absent on the tip: {shown}{more}"
    if status == PASS:
        return True, PASS
    if not _waiver_ok(record.get("waiver")):
        return False, (
            f"suite result is UNAVAILABLE and carries no waiver ({one_line(reason, 240)}); "
            "rerun it, or waive with a recorded reason"
        )
    return True, admission_summary(record)


# ── records ───────────────────────────────────────────────────────────────

def candidate_record(common_dir: str | Path, candidate_sha: str) -> dict | None:
    return shared.load_record(common_dir, RECORD_DIR, candidate_sha)


def baseline_record(common_dir: str | Path, tip: str) -> dict | None:
    return shared.load_record(common_dir, BASELINE_DIR, tip)


def usable_baseline(record: object, tip: str) -> bool:
    """A cached baseline is reused only when complete, keyed by this tip and built from it."""
    return (
        isinstance(record, dict)
        and record.get("schema") == BASELINE_SCHEMA
        and record.get("status") == COMPLETE
        and record.get("tip_sha") == tip
        and record.get("sandbox_core_sha") == tip
        and isinstance(record.get("failures"), list)
    )


def _write_log(common_dir: Path, dirname: str, sha: str, tail: str) -> str:
    path = shared.record_path(common_dir, dirname, sha).with_suffix(".log")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = tail.splitlines()[-LOG_TAIL_LINES:]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


# ── decisions ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LandingDecision:
    admitted: bool
    kind: str
    line: str
    record_path: Path | None = None
    reason: str = ""


def exempt_files(paths: Sequence[str]) -> str:
    return ", ".join(paths)


def _decision(admitted: bool, kind: str, reason: str, record: Path | None = None) -> LandingDecision:
    if kind == NOT_REQUIRED:
        line = f"SUITE: not required ({reason})"
    elif kind == EXEMPT:
        line = f"SUITE: exempt (memory/docs-only): {reason}"
    elif admitted:
        line = f"SUITE: ADMITTED ({reason})"
    else:
        line = f"SUITE: REFUSED: {one_line(reason, 600)}"
    return LandingDecision(admitted, kind, defuse(line), record, reason)


def landing_decision(repo: str | Path, candidate_sha: str, tip: str,
                     common_dir: str | Path | None = None,
                     record_file: str | Path | None = None) -> LandingDecision:
    """Read the edges (trees, diff, record) and decide; never runs the suite; any error refuses."""
    try:
        kind, files = landing_scope(repo, tip, candidate_sha)
        if kind == NOT_REQUIRED:
            return _decision(True, NOT_REQUIRED, "repository has no suite gate")
        if kind == EXEMPT:
            return _decision(True, EXEMPT, exempt_files(files))
        store = Path(common_dir) if common_dir else shared.git_common_dir(repo)
        path = Path(record_file) if record_file else shared.record_path(store, RECORD_DIR, candidate_sha)
        if not path.exists():
            return _decision(False, REFUSED, f"no suite record for {short(candidate_sha)}; run: {RUN_COMMAND}")
        record = shared.read_record(path)
    except GateError as exc:
        return _decision(False, REFUSED, f"cannot decide: {exc}")
    ok, reason = record_admits(record, candidate_sha, tip)
    return _decision(ok, ADMITTED if ok else REFUSED, reason, path)


def suite_status(repo: str | Path, candidate_sha: str, tip: str) -> str:
    """The decision line for a landing, from local state only."""
    return landing_decision(repo, candidate_sha, tip).line


# ── pre-push hook ─────────────────────────────────────────────────────────

class _SuiteWording:
    def refused(self, reason: str) -> list[str]:
        return [
            f"pre-push: refused: {one_line(reason, 600)}",
            f"pre-push: to land a change run: {RUN_COMMAND}",
        ]

    def decision(self, local_sha: str, decision: Decision) -> list[str]:
        if decision.kind == EXEMPT:
            return [f"pre-push: suite gate exempt (memory/docs-only): {decision.reason}"]
        if decision.kind == ADMITTED:
            lines = [admission_line(local_sha)]
            if decision.reason != PASS:
                lines.append(f"pre-push: waiver in effect ({decision.reason})")
            return lines
        return [f"pre-push: suite gate not required ({decision.reason})"]

    def no_remote(self, trunk: str) -> str:
        return f"{trunk} does not exist on the remote, so no suite record can be bound to a tip"

    def unknown_remote(self, trunk: str, remote_sha: str) -> str:
        return f"remote {trunk} tip {short(remote_sha)} is not known locally; fetch it, merge it and rerun the suite gate"


def prepush_decision(lines: Iterable[str], repo: str | Path, trunk: str = "main",
                     common_dir: str | Path | None = None) -> PrePushResult:
    """Decide a push from git's pre-push stdin lines; reads local state only."""
    def evaluate(local_sha: str, remote_sha: str) -> Decision:
        found = landing_decision(repo, local_sha, remote_sha, common_dir)
        return Decision(found.admitted, found.kind, found.reason)

    return shared.prepush_loop(lines, repo, trunk, evaluate=evaluate, wording=_SuiteWording())


# ── the process edge: sandbox build and pytest in their own process group ─

def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, sig)


def run_group(argv: Sequence[str], *, cwd: str | Path | None, env: dict[str, str],
              timeout_s: float, grace_s: float = KILL_GRACE_S) -> tuple[int, str, bool]:
    """(exit code, merged output, timed out). On timeout the whole group gets SIGTERM, then SIGKILL."""
    try:
        proc = subprocess.Popen(
            list(argv), cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", start_new_session=True,
        )
    except OSError as exc:
        raise GateError(f"cannot run {Path(argv[0]).name}: {exc}") from exc
    try:
        out, _ = proc.communicate(timeout=timeout_s)
        return proc.returncode, out or "", False
    except subprocess.TimeoutExpired:
        _signal_group(proc, signal.SIGTERM)
        try:
            out, _ = proc.communicate(timeout=grace_s)
        except subprocess.TimeoutExpired:
            _signal_group(proc, signal.SIGKILL)
            out, _ = proc.communicate()
        return proc.returncode, out or "", True


def sandbox_scrub_names(script: str | Path) -> list[str]:
    """The environment names instruction-sandbox.sh refuses to pass on (its ``scrub=(...)`` list)."""
    match = re.search(r"^scrub=\(([^)]*)\)", Path(script).read_text(encoding="utf-8"), re.MULTILINE)
    names = re.findall(r"[A-Z][A-Z0-9_]+", match.group(1)) if match else []
    if not names:
        raise GateError(f"{Path(script).name} has no scrub list to mirror")
    return names


def suite_env(root: Path, userbase: str, failures_file: Path, scrub: Sequence[str],
              base_env: dict[str, str] | None = None) -> dict[str, str]:
    """The environment of the sandboxed pytest: scrubbed, rooted in the sandbox, same user site."""
    env = dict(base_env if base_env is not None else shared._clean_env())
    for name in list(env):
        if name in scrub or name.startswith("AGENTCTL_") or name in ("PYTEST_ADDOPTS", "PYTHONPATH"):
            del env[name]
    home = root / "home"
    env.update(
        HOME=str(home),
        CLAUDE_AGENT_HOME=str(home / ".claude-agent"),
        CLAUDE_CONFIG_DIR=str(home / ".claude-agent"),
        CLAUDE_INSTRUCTIONS_REPO=str(root / "core"),
        PYTHONUSERBASE=userbase,
        TMPDIR=str(root / "tmp"),
        PYTHONPATH=str(root / "plugin"),
        **{FAILURES_ENV: str(failures_file)},
        **GIT_IDENTITY,
    )
    return env


def pytest_argv(root: Path, select: Sequence[str], timeout_s: int) -> list[str]:
    cap_run = Path(__file__).resolve().parents[1] / "cap-run.sh"
    return [
        str(cap_run), "--timeout", str(timeout_s + KILL_GRACE_S + CAP_RUN_SLACK_S), "--",
        sys.executable, "-m", "pytest", *(select or ["scripts/tests"]),
        "-q", "--continue-on-collection-errors", "--rootdir", str(root / "core"),
        "-p", PLUGIN_MODULE, "-p", "no:cacheprovider",
    ]


def read_failures(path: Path) -> tuple[str, ...]:
    try:
        return tuple(sorted({line for line in path.read_text(encoding="utf-8").splitlines() if line}))
    except OSError:
        return ()


def default_runner(repo: Path, sha: str, root: Path, *, select: Sequence[str], timeout_s: int) -> SuiteRun:
    """Build the sandbox of ``sha`` under ``root`` (once), then run pytest in it."""
    scripts = Path(__file__).resolve().parents[1]
    userbase = site.getuserbase()
    xdist_ok = importlib.util.find_spec("xdist") is not None
    started = time.monotonic()
    build_s = 0.0
    if not (root / "sandbox.env").exists():
        code, out, timed_out = run_group(
            [str(scripts / SANDBOX_SCRIPT), "--source", str(repo), "--core-ref", sha, "--root", str(root)],
            cwd=None, env=shared._clean_env(), timeout_s=BUILD_TIMEOUT_S,
        )
        build_s = time.monotonic() - started
        if code != 0 or timed_out:
            return SuiteRun(None, build_ok=False, xdist_ok=xdist_ok, build_s=build_s,
                            detail="timed out" if timed_out else f"exit {code}: {one_line(out[-400:], 240)}")
    proc = subprocess.run(
        ["git", "-C", str(root / "core"), "rev-parse", "HEAD"], capture_output=True, text=True,
        env=shared._clean_env(),
    )
    core_sha = proc.stdout.strip() if proc.returncode == 0 else ""
    (root / "plugin").mkdir(exist_ok=True)
    (root / "plugin" / f"{PLUGIN_MODULE}.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    (root / "tmp").mkdir(exist_ok=True)
    failures_file = root / "failures.txt"
    failures_file.unlink(missing_ok=True)
    env = suite_env(root, userbase, failures_file, sandbox_scrub_names(scripts / SANDBOX_SCRIPT))
    ran = time.monotonic()
    code, out, timed_out = run_group(pytest_argv(root, select, timeout_s), cwd=root / "core", env=env,
                                     timeout_s=timeout_s)
    return SuiteRun(
        None if timed_out else code, read_failures(failures_file), core_sha, timed_out, True, xdist_ok,
        log_tail="\n".join(out.splitlines()[-LOG_TAIL_LINES:]), build_s=build_s,
        duration_s=time.monotonic() - ran,
    )


# ── run: candidate, baseline, rerun, record ───────────────────────────────

@contextlib.contextmanager
def _exclusive(path: Path):
    """An exclusive lock on ``path``: take it without waiting if free, else wait for it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def remove_sandbox(root: Path, parent: str | Path) -> None:
    """Remove a sandbox root, but only a directory of the expected name directly under ``parent``."""
    if root.name.startswith(SANDBOX_PREFIX) and root.parent == Path(parent) and root.is_dir():
        shutil.rmtree(root, ignore_errors=True)


@dataclass
class SuiteOutcome:
    record: dict
    record_path: Path
    decision: LandingDecision


def _new_root(sandbox_parent: str | Path) -> Path:
    return Path(tempfile.mkdtemp(prefix=SANDBOX_PREFIX, dir=sandbox_parent))


def _baseline(repo: Path, tip: str, common_dir: Path, runner: Runner, timeout_s: int,
              clock: Callable[[], str], sandbox_parent: str | Path) -> tuple[dict, str]:
    """(baseline record, cache state: hit | built). Built only from the tip's own tree."""
    with _exclusive(shared.record_path(common_dir, BASELINE_DIR, tip).with_suffix(".lock")):
        cached = baseline_record(common_dir, tip)
        if usable_baseline(cached, tip):
            return cached, "hit"
        root = _new_root(sandbox_parent)
        try:
            run = runner(repo, tip, root, select=(), timeout_s=timeout_s)
            ids, reason = classify(run, tip)
            record = {
                "schema": BASELINE_SCHEMA,
                "tip_sha": tip,
                "sandbox_core_sha": run.sandbox_core_sha,
                "sandbox_root": str(root),
                "ran_at": clock(),
                "build_s": round(run.build_s, 1),
                "duration_s": round(run.duration_s, 1),
                "pytest_exit": run.pytest_exit,
                "status": COMPLETE if not reason else UNAVAILABLE,
                "failures": list(ids),
                "unavailable_reason": reason,
                "log": _write_log(common_dir, BASELINE_DIR, tip, run.log_tail),
            }
            shared.write_record(common_dir, BASELINE_DIR, tip, record)
            return record, "built"
        finally:
            remove_sandbox(root, sandbox_parent)


def _confirmed(repo: Path, candidate: str, root: Path, new: list[str], runner: Runner,
               timeout_s: int) -> tuple[list[str], list[str], str]:
    """Rerun the new failures once in the candidate sandbox: (still failing, passed now, unavailable reason)."""
    rerun = runner(repo, candidate, root, select=tuple(new), timeout_s=timeout_s)
    ids, reason = classify(rerun, candidate)
    if reason:
        return [], [], f"rerun of {len(new)} new failure(s) unavailable: {reason}"
    return [i for i in new if i in ids], [i for i in new if i not in ids], ""


def run_suite(repo: str | Path, candidate_sha: str, tip: str, *, runner: Runner | None = None,
              timeout_s: int | None = None, waiver: str | None = None, reuse: bool = False,
              clock: Callable[[], str] | None = None,
              sandbox_parent: str | Path | None = None) -> SuiteOutcome:
    """Run the suite on the candidate, subtract the tip's baseline, write the record.

    Raises ``GateError`` before any record exists (not a descendant of the tip, git failure).
    ``reuse`` returns a stored record that already admits instead of running again.
    """
    repo = Path(repo)
    run = runner or default_runner
    limit = timeout_s or configured_timeout_s()
    now = clock or shared._utc_now
    sandbox_parent = sandbox_parent or SANDBOX_PARENT
    if not shared.is_ancestor(repo, tip, candidate_sha):
        raise GateError(
            f"{short(tip)} is not an ancestor of {short(candidate_sha)}; merge the remote tip into the "
            "candidate and rerun",
            refused=True,
        )
    common_dir = shared.git_common_dir(repo)
    with _exclusive(shared.record_path(common_dir, RECORD_DIR, candidate_sha).with_suffix(".lock")):
        if reuse:
            stored = candidate_record(common_dir, candidate_sha)
            if stored is not None and record_admits(stored, candidate_sha, tip)[0]:
                path = shared.record_path(common_dir, RECORD_DIR, candidate_sha)
                return SuiteOutcome(stored, path, _decision(True, ADMITTED, admission_summary(stored), path))
        root = _new_root(sandbox_parent)
        try:
            candidate_run = run(repo, candidate_sha, root, select=(), timeout_s=limit)
            candidate_ids, reason = classify(candidate_run, candidate_sha)
            baseline_failures: list[str] = []
            baseline_cache = "none"
            new_failures: list[str] = []
            unconfirmed: list[str] = []
            if not reason and candidate_ids:
                baseline, baseline_cache = _baseline(repo, tip, common_dir, run, limit, now, sandbox_parent)
                if baseline.get("status") != COMPLETE:
                    reason = f"the tip's baseline is unavailable: {one_line(baseline.get('unavailable_reason'), 240)}"
                else:
                    baseline_failures = list(baseline["failures"])
                    new = subtract(candidate_ids, baseline_failures)
                    if new:
                        new_failures, unconfirmed, reason = _confirmed(repo, candidate_sha, root, new, run, limit)
                    if unconfirmed and not new_failures and not reason:
                        reason = f"{len(unconfirmed)} new failure(s) did not fail again on rerun: " + ", ".join(
                            one_line(i, 120) for i in unconfirmed[:5])
        finally:
            remove_sandbox(root, sandbox_parent)
    status = derive_status(new_failures, reason)
    record = {
        "schema": SCHEMA,
        "candidate_sha": candidate_sha,
        "base_sha": tip,
        "sandbox_core_sha": candidate_run.sandbox_core_sha,
        "sandbox_root": str(root),
        "ran_at": now(),
        "build_s": round(candidate_run.build_s, 1),
        "duration_s": round(candidate_run.duration_s, 1),
        "pytest_exit": candidate_run.pytest_exit,
        "status": status,
        "candidate_failures": list(candidate_ids),
        "baseline_failures": baseline_failures,
        "baseline_cache": baseline_cache,
        "new_failures": new_failures,
        "unconfirmed": unconfirmed,
        "unavailable_reason": reason,
        "waiver": None,
        "log": _write_log(common_dir, RECORD_DIR, candidate_sha, candidate_run.log_tail),
    }
    if waiver is not None and status == UNAVAILABLE:
        record["waiver"] = {"reason": shared.normalize_reason(waiver), "at": now()}
    path = shared.write_record(common_dir, RECORD_DIR, candidate_sha, record)
    ok, why = record_admits(record, candidate_sha, tip)
    return SuiteOutcome(record, path, _decision(ok, ADMITTED if ok else REFUSED, why, path))


def waive_record(repo: str | Path, sha: str, reason: str,
                 now: Callable[[], str] | None = None) -> tuple[Path, dict]:
    """Attach a waiver to the stored record of ``sha``; only an UNAVAILABLE record takes one."""
    common_dir = shared.git_common_dir(repo)
    record = candidate_record(common_dir, sha)
    if record is None:
        raise GateError(f"no suite record for {short(sha)}; run: {RUN_COMMAND}")
    if record.get("status") != UNAVAILABLE:
        raise GateError(f"waiver not accepted: the record is {record.get('status')}, not UNAVAILABLE", refused=True)
    record["waiver"] = {"reason": shared.normalize_reason(reason), "at": (now or shared._utc_now)()}
    return shared.write_record(common_dir, RECORD_DIR, sha, record), record


def evaluate_landing(repo: str | Path, candidate_sha: str, tip: str, *, runner: Runner | None = None,
                     timeout_s: int | None = None, waiver: str | None = None,
                     reuse: bool = True) -> LandingDecision:
    """The landing decision for the candidate: reuse a bound admitting record, else run the suite."""
    try:
        kind, files = landing_scope(repo, tip, candidate_sha)
        if kind == NOT_REQUIRED:
            return _decision(True, NOT_REQUIRED, "repository has no suite gate")
        if kind == EXEMPT:
            return _decision(True, EXEMPT, exempt_files(files))
        return run_suite(repo, candidate_sha, tip, runner=runner, timeout_s=timeout_s, waiver=waiver,
                         reuse=reuse).decision
    except GateError as exc:
        return _decision(False, REFUSED, f"cannot decide: {exc}")
