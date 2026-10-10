"""Pre-land smoke gate: decide, from observable state, whether an instruction change may land.

Difficulty removed: the isolated-root smoke test (scripts/instruction-sandbox.sh +
instruction-sandbox-verify.sh) proves a fresh session still loads the candidate instructions, but
nothing made anyone run it. The rule "a change that touches the instruction surface lands on main
only with a PASS smoke record for that exact commit, built on the live tip of main" is decidable
from the diff, the record and the remote tip, so it lives here as pure functions; git, the
filesystem and the sandbox are the edges (``run_smoke``, ``evaluate_landing``).

Two enforcement points call this module: scripts/land-branch.py (before its push) and
githooks/pre-push (through scripts/instruction-smoke-gate.py). Contract of the record:
``instruction-smoke/v1`` JSON under ``<git common dir>/instruction-smoke/<candidate_sha>.json``.

Fail closed throughout: a missing, unreadable or inconsistent record and any git error are
refusals, never admissions.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

SCHEMA = "instruction-smoke/v1"
RECORD_DIR = "instruction-smoke"
SANDBOX_SCRIPT = "scripts/instruction-sandbox.sh"
DEFAULT_REMOTE = "origin"
DEFAULT_TRUNK = "main"
DEFAULT_TIMEOUT_S = 480  # instruction-sandbox-live.py's DEFAULT_TIMEOUT_S: one live launch
BUILD_TIMEOUT_S = 600
STATIC_BUDGET_S = 900

PASS, FAIL, UNAVAILABLE = "PASS", "FAIL", "UNAVAILABLE"
STATUSES = (PASS, FAIL, UNAVAILABLE)
VERIFY_EXIT = {PASS: 0, FAIL: 1, UNAVAILABLE: 3}

REQUIRED_CHECKS = (
    "static:lint-prose-length",
    "static:verify-layout-contract",
    "static:verify-instructions-sync",
    "static:lint-hooks-executable",
    "live:core-marker",
    "canon:unchanged",
)

EXEMPT_DIRS = ("docs/", "memory-global/", "scripts/tests/")
EXEMPT_TOP_LEVEL_FILES = ("README.md",)

# Must equal the no-push-rights regex of scripts/sync-instructions-repo.sh push_and_degrade
# (a test pins the two together): text matching it makes that script report "no push rights".
PUSH_RIGHTS_PATTERN = r"permission|denied|forbidden|403|read[ -]?only|not authorized|access rights"
_PUSH_RIGHTS = re.compile(PUSH_RIGHTS_PATTERN, re.IGNORECASE)

ZERO_SHA = re.compile(r"0+")
_SHA = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")

NOT_REQUIRED, ADMITTED, REFUSED = "not-required", "admitted", "refused"
RUN_COMMAND = "python3 scripts/instruction-smoke-gate.py run"


class GateError(Exception):
    """An environment or usage failure (exit 2), or with ``refused`` a state refusal (exit 1)."""

    def __init__(self, message: str, refused: bool = False):
        super().__init__(message)
        self.refused = refused


class RecordError(GateError):
    pass


# ── text helpers ──────────────────────────────────────────────────────────

def one_line(text: object, limit: int = 300) -> str:
    cleaned = " ".join(str(text).split())
    return "".join(ch for ch in cleaned if ch.isprintable())[:limit]


def defuse(text: str) -> str:
    """Break any substring that push_and_degrade would read as a missing-push-rights failure."""
    return _PUSH_RIGHTS.sub(lambda m: m.group(0)[0] + "·" + m.group(0)[1:], text)


def short(sha: str) -> str:
    return str(sha)[:12]


def admission_line(candidate_sha: str) -> str:
    """The one line the hook prints on admitting a push; nothing else may print it."""
    return f"pre-push: instruction smoke record admitted {candidate_sha}"


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── surface classifier (R1) ───────────────────────────────────────────────

def is_exempt(path: str) -> bool:
    parts = path.split("/")
    if not path or path.startswith("/") or "" in parts or "." in parts or ".." in parts:
        return False
    if path in EXEMPT_TOP_LEVEL_FILES:
        return True
    return path.startswith(EXEMPT_DIRS)


def surface_paths(paths: Iterable[str]) -> list[str]:
    return [p for p in paths if not is_exempt(p)]


def touches_surface(paths: Iterable[str]) -> bool:
    """Complement form: only the named exempt paths are exempt, so an unknown path is surface."""
    return bool(surface_paths(paths))


# ── git edges ─────────────────────────────────────────────────────────────

def _git(repo: str | Path, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, encoding="utf-8", errors="surrogateescape",
        )
    except OSError as exc:
        raise GateError(f"cannot run git: {exc}") from exc


def _git_ok(repo: str | Path, *args: str) -> str:
    proc = _git(repo, *args)
    if proc.returncode != 0:
        tail = one_line(proc.stderr or proc.stdout, 200)
        raise GateError(f"git {args[0]} failed (exit {proc.returncode}): {tail}")
    return proc.stdout


def is_valid_sha(value: object) -> bool:
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def resolve_commit(repo: str | Path, ref: str) -> str:
    proc = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    sha = proc.stdout.strip()
    if proc.returncode != 0 or not is_valid_sha(sha):
        raise GateError(f"{ref} does not resolve to a commit in {repo}")
    return sha


def object_exists(repo: str | Path, sha: str) -> bool:
    return _git(repo, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0


def git_common_dir(repo: str | Path) -> Path:
    out = _git_ok(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    return Path(out)


def changed_paths(repo: str | Path, base: str, candidate: str) -> list[str]:
    """Paths that differ between two commits' trees; a rename lists both its old and new path.

    The two-commit plumbing form compares a merge candidate as a whole against the remote tip,
    and ``--no-renames`` keeps a move of a surface file into an exempt directory visible.
    """
    out = _git_ok(repo, "diff-tree", "-r", "--no-renames", "-z", "--name-only", base, candidate)
    return [p for p in out.split("\0") if p]


def _sandbox_script_in(repo: str | Path, sha: str) -> bool:
    return bool(_git_ok(repo, "ls-tree", "--name-only", sha, "--", SANDBOX_SCRIPT).strip())


def fetch_remote_tip(repo: str | Path, remote: str, trunk: str) -> str:
    tracking = f"refs/remotes/{remote}/{trunk}"
    _git_ok(repo, "fetch", "--quiet", remote, f"+refs/heads/{trunk}:{tracking}")
    return resolve_commit(repo, tracking)


def is_ancestor(repo: str | Path, ancestor: str, descendant: str) -> bool:
    proc = _git(repo, "merge-base", "--is-ancestor", ancestor, descendant)
    if proc.returncode not in (0, 1):
        raise GateError(f"git merge-base failed: {one_line(proc.stderr, 200)}")
    return proc.returncode == 0


# ── verify-output parser ──────────────────────────────────────────────────

def derive_result(checks: Sequence[dict]) -> str:
    statuses = {c["status"] for c in checks}
    if FAIL in statuses:
        return FAIL
    if UNAVAILABLE in statuses:
        return UNAVAILABLE
    return PASS


def parse_verify_output(text: str, verify_exit: int | None = None) -> tuple[str, list[dict]]:
    """Parse instruction-sandbox-verify.sh's ``CHECK`` / ``RESULT:`` lines. Fails closed."""
    checks: list[dict] = []
    results: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("CHECK "):
            fields = line.split(None, 3)
            if len(fields) < 3:
                raise GateError(f"malformed CHECK line: {one_line(line, 120)}")
            _, name, status = fields[:3]
            if status not in STATUSES:
                raise GateError(f"unknown status {one_line(status, 40)!r} on check {one_line(name, 80)}")
            if any(c["name"] == name for c in checks):
                raise GateError(f"duplicate check {one_line(name, 80)}")
            checks.append({"name": name, "status": status, "detail": one_line(fields[3]) if len(fields) > 3 else ""})
        elif line.startswith("RESULT:"):
            results.append(line[len("RESULT:"):].strip())
    if len(results) != 1:
        raise GateError(f"expected one RESULT line, found {len(results)}")
    result = results[0]
    if result not in STATUSES:
        raise GateError(f"unknown RESULT {one_line(result, 40)!r}")
    if not checks:
        raise GateError("no CHECK lines")
    if derive_result(checks) != result:
        raise GateError(f"RESULT {result} contradicts its check lines")
    if verify_exit is not None and VERIFY_EXIT[result] != verify_exit:
        raise GateError(f"RESULT {result} contradicts verify exit {verify_exit}")
    return result, checks


# ── waiver, admission ─────────────────────────────────────────────────────

def _check_list(raw: object) -> tuple[list[dict], str]:
    if not isinstance(raw, list) or not raw:
        return [], "record has no checks"
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            return [], "record has a malformed check"
        if item.get("status") not in STATUSES:
            return [], f"check {one_line(item['name'], 80)} has an unknown status"
        if item["name"] in seen:
            return [], f"record repeats check {one_line(item['name'], 80)}"
        seen.add(item["name"])
    return raw, ""


def _is_live(name: str) -> bool:
    return name.startswith("live:")


def waiver_allowed(record: dict) -> tuple[bool, str]:
    """A waiver covers only UNAVAILABLE live launches: no FAIL, and every non-PASS check is live:*."""
    checks, problem = _check_list(record.get("checks"))
    if problem:
        return False, problem
    failed = [c["name"] for c in checks if c["status"] == FAIL]
    if failed:
        return False, f"{one_line(failed[0], 80)} FAILed"
    offenders = [
        c["name"] for c in checks
        if c["status"] != PASS and not (c["status"] == UNAVAILABLE and _is_live(c["name"]))
    ]
    if offenders:
        return False, f"{one_line(offenders[0], 80)} is not a live check that was merely UNAVAILABLE"
    if all(c["status"] == PASS for c in checks):
        return False, "nothing to waive: every check PASSed"
    return True, ""


def unavailable_detail(record: dict) -> str:
    parts = [
        f"{c['name']}: {c.get('detail') or 'no detail'}"
        for c in record.get("checks", []) if isinstance(c, dict) and c.get("status") == UNAVAILABLE
    ]
    return one_line("; ".join(parts))


def make_waiver(record: dict, reason: str, at: str) -> dict:
    ok, why = waiver_allowed(record)
    if not ok:
        raise GateError(f"waiver not accepted: {why}", refused=True)
    cleaned = one_line(reason)
    if not cleaned:
        raise GateError("waiver needs a reason", refused=True)
    return {"reason": cleaned, "at": at, "detail": unavailable_detail(record)}


def admission_summary(record: dict) -> str:
    waiver = record.get("waiver")
    if record.get("result") == UNAVAILABLE and isinstance(waiver, dict):
        detail = one_line(waiver.get("detail") or "")
        return one_line(f"UNAVAILABLE, waived: {waiver.get('reason', '')}" + (f" [{detail}]" if detail else ""), 600)
    return str(record.get("result"))


def record_admits(record: object, candidate_sha: str, live_remote_sha: str) -> tuple[bool, str]:
    """(admitted, reason). Binds the record to the candidate and to the live remote tip."""
    if not isinstance(record, dict):
        return False, "record is not a JSON object"
    if record.get("schema") != SCHEMA:
        return False, f"unsupported record schema {one_line(record.get('schema'), 60)!r}"
    if record.get("candidate_sha") != candidate_sha:
        return False, f"record is for {short(record.get('candidate_sha'))}, not {short(candidate_sha)}"
    if record.get("sandbox_core_sha") != candidate_sha:
        return False, (
            f"record's sandbox was built from {short(record.get('sandbox_core_sha'))}, "
            f"not the candidate {short(candidate_sha)}"
        )
    if record.get("base_sha") != live_remote_sha:
        return False, (
            f"record was built on {short(record.get('base_sha'))} but the remote tip is "
            f"{short(live_remote_sha)}; merge it into the candidate and rerun the smoke"
        )
    checks, problem = _check_list(record.get("checks"))
    if problem:
        return False, problem
    names = {c["name"] for c in checks}
    for required in REQUIRED_CHECKS:
        if required not in names:
            return False, f"record lacks the required check {required}"
    derived = derive_result(checks)
    if record.get("result") != derived or record.get("verify_exit") != VERIFY_EXIT[derived]:
        return False, "record's result contradicts its own checks"
    if derived == FAIL:
        failed = next(c for c in checks if c["status"] == FAIL)
        return False, f"smoke result is FAIL ({one_line(failed['name'], 80)}: {one_line(failed.get('detail', ''), 120)})"
    if derived == PASS:
        return True, PASS
    waiver = record.get("waiver")
    if not (isinstance(waiver, dict) and one_line(waiver.get("reason") or "") and waiver.get("at")):
        return False, (
            f"smoke result is UNAVAILABLE and carries no waiver ({unavailable_detail(record)}); "
            "rerun it, or waive with a recorded reason"
        )
    allowed, why = waiver_allowed(record)
    if not allowed:
        return False, f"waiver does not cover this record: {why}"
    return True, admission_summary(record)


# ── record store (R4) ─────────────────────────────────────────────────────

def record_path(common_dir: str | Path, candidate_sha: str) -> Path:
    if not is_valid_sha(candidate_sha):
        raise RecordError(f"not a full commit sha: {one_line(candidate_sha, 80)}")
    return Path(common_dir) / RECORD_DIR / f"{candidate_sha}.json"


def read_record(path: str | Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RecordError(f"{Path(path).name} is unreadable: {one_line(exc, 160)}") from exc
    if not isinstance(data, dict):
        raise RecordError(f"{Path(path).name} is not a JSON object")
    return data


def load_record(common_dir: str | Path, candidate_sha: str) -> dict | None:
    path = record_path(common_dir, candidate_sha)
    if not path.exists():
        return None
    return read_record(path)


def write_record(common_dir: str | Path, record: dict) -> Path:
    path = record_path(common_dir, record["candidate_sha"])
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


# ── landing decision, shared by both enforcement points (R1) ──────────────

@dataclass(frozen=True)
class Decision:
    allowed: bool
    kind: str
    reason: str


def decide(paths: Sequence[str], gate_applies: bool, record: object | None,
           candidate_sha: str, live_remote_sha: str) -> Decision:
    """The rule as a pure function of the diff paths, the record and the live remote tip."""
    if not gate_applies:
        return Decision(True, NOT_REQUIRED, "repository has no instruction sandbox")
    if not touches_surface(paths):
        return Decision(True, NOT_REQUIRED, "diff touches only exempt paths")
    if record is None:
        shown = surface_paths(paths)
        extra = f", +{len(shown) - 3} more" if len(shown) > 3 else ""
        return Decision(False, REFUSED, (
            f"no smoke record for {short(candidate_sha)} "
            f"(surface paths: {', '.join(one_line(p, 80) for p in shown[:3])}{extra})"
        ))
    ok, reason = record_admits(record, candidate_sha, live_remote_sha)
    return Decision(ok, ADMITTED if ok else REFUSED, reason)


def evaluate_landing(repo: str | Path, candidate_sha: str, remote_sha: str,
                     common_dir: str | Path | None = None) -> Decision:
    """Read the edges (trees, diff, record store) and decide; any error refuses."""
    try:
        applies = _sandbox_script_in(repo, remote_sha) or _sandbox_script_in(repo, candidate_sha)
        paths = changed_paths(repo, remote_sha, candidate_sha) if applies else []
        record = None
        if applies and touches_surface(paths):
            store = Path(common_dir) if common_dir else git_common_dir(repo)
            record = load_record(store, candidate_sha)
    except GateError as exc:
        return Decision(False, REFUSED, f"cannot decide: {exc}")
    return decide(paths, applies, record, candidate_sha, remote_sha)


# ── pre-push hook (R1) ────────────────────────────────────────────────────

@dataclass(frozen=True)
class PrePushResult:
    allowed: bool
    messages: tuple[str, ...]


def prepush_decision(lines: Iterable[str], repo: str | Path, trunk: str = DEFAULT_TRUNK,
                     common_dir: str | Path | None = None) -> PrePushResult:
    """Decide a push from git's pre-push stdin lines: ``<local-ref> <local-sha> <remote-ref> <remote-sha>``."""
    messages: list[str] = []
    allowed = True

    def refuse(reason: str) -> None:
        nonlocal allowed
        allowed = False
        messages.append(f"pre-push: refused: {one_line(reason, 600)}")
        messages.append(f"pre-push: to land an instruction change run: {RUN_COMMAND}")

    for raw in lines:
        if not raw.strip():
            continue
        fields = raw.split()
        if len(fields) != 4:
            refuse(f"malformed pre-push input: {one_line(raw, 120)}")
            continue
        _, local_sha, remote_ref, remote_sha = fields
        if remote_ref != f"refs/heads/{trunk}" or ZERO_SHA.fullmatch(local_sha):
            continue
        if ZERO_SHA.fullmatch(remote_sha):
            refuse(f"{trunk} does not exist on the remote, so no smoke record can be bound to a base")
            continue
        if not object_exists(repo, remote_sha):
            refuse(f"remote {trunk} tip {short(remote_sha)} is not known locally; fetch it, merge it and rerun the smoke")
            continue
        decision = evaluate_landing(repo, local_sha, remote_sha, common_dir)
        if decision.kind == ADMITTED:
            messages.append(admission_line(local_sha))
            if decision.reason != PASS:
                messages.append(f"pre-push: waiver in effect ({decision.reason})")
        elif decision.kind == NOT_REQUIRED:
            messages.append(f"pre-push: instruction smoke not required ({decision.reason})")
        else:
            refuse(decision.reason)
    return PrePushResult(allowed, tuple(defuse(m) for m in messages))


# ── run: build the sandbox, verify, write the record (R2, R4) ─────────────

@dataclass(frozen=True)
class SandboxRun:
    verify_exit: int
    verify_output: str
    sandbox_core_sha: str


Runner = Callable[[Path, str, Path, int], SandboxRun]


@dataclass
class RunOutcome:
    record: dict
    record_path: Path
    admitted: bool
    reason: str
    sandbox_root: Path | None
    waiver_note: str = ""


def _clean_env() -> dict[str, str]:
    env = dict(os.environ)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        env.pop(name, None)
    return env


def _run_process(argv: list[str], timeout_s: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s, env=_clean_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise GateError(f"{Path(argv[0]).name} did not finish within {timeout_s}s") from exc
    except OSError as exc:
        raise GateError(f"cannot run {Path(argv[0]).name}: {exc}") from exc


def default_runner(repo: Path, candidate_sha: str, root: Path, timeout_s: int) -> SandboxRun:
    """Build the sandbox of the candidate, then run the verify recipe (one live ``claude -p``)."""
    scripts = Path(__file__).resolve().parents[1]
    build = _run_process(
        [str(scripts / "instruction-sandbox.sh"), "--source", str(repo),
         "--core-ref", candidate_sha, "--root", str(root)],
        BUILD_TIMEOUT_S,
    )
    if build.returncode != 0:
        raise GateError(f"sandbox build failed (exit {build.returncode}): {one_line(build.stderr, 240)}")
    core_sha = ""
    try:
        for line in (root / "sandbox.env").read_text(encoding="utf-8").splitlines():
            if line.startswith("ISB_CORE_SHA="):
                core_sha = line.split("=", 1)[1].strip()
    except OSError as exc:
        raise GateError(f"sandbox.env unreadable: {exc}") from exc
    verify = _run_process(
        [str(scripts / "instruction-sandbox-verify.sh"), "--timeout", str(timeout_s), str(root)],
        timeout_s + STATIC_BUDGET_S,
    )
    if verify.returncode not in VERIFY_EXIT.values():
        raise GateError(f"verify exited {verify.returncode}: {one_line(verify.stderr, 240)}")
    return SandboxRun(verify.returncode, verify.stdout, core_sha)


def run_smoke(repo: str | Path, ref: str = "HEAD", remote: str = DEFAULT_REMOTE,
              trunk: str = DEFAULT_TRUNK, timeout_s: int = DEFAULT_TIMEOUT_S,
              waiver: str | None = None, runner: Runner | None = None,
              now: Callable[[], str] | None = None,
              sandbox_parent: str | Path | None = None) -> RunOutcome:
    """Fetch the remote tip, sandbox the candidate, verify, and write the record.

    Raises ``GateError`` before any record exists (not a descendant of the remote tip, git or
    sandbox failure). The sandbox is removed after an admitted PASS and kept otherwise.
    """
    repo = Path(repo)
    run = runner or default_runner
    clock = now or _utc_now
    candidate = resolve_commit(repo, ref)
    fetched_at = clock()
    base = fetch_remote_tip(repo, remote, trunk)
    if not is_ancestor(repo, base, candidate):
        raise GateError(
            f"{remote}/{trunk} {short(base)} is not an ancestor of {short(candidate)}; "
            f"merge {remote}/{trunk} into the candidate and rerun",
            refused=True,
        )
    root = Path(tempfile.mkdtemp(prefix="instruction-smoke-sbx.", dir=sandbox_parent))
    try:
        sandbox = run(repo, candidate, root, timeout_s)
        result, checks = parse_verify_output(sandbox.verify_output, sandbox.verify_exit)
    except GateError as exc:
        raise GateError(f"{exc}; sandbox kept at {root}") from exc
    record: dict = {
        "schema": SCHEMA,
        "candidate_sha": candidate,
        "base_sha": base,
        "remote": remote,
        "trunk": trunk,
        "fetched_at": fetched_at,
        "ran_at": clock(),
        "result": result,
        "verify_exit": sandbox.verify_exit,
        "sandbox_core_sha": sandbox.sandbox_core_sha,
        "checks": checks,
        "waiver": None,
    }
    note = ""
    if waiver is not None and result == UNAVAILABLE:
        try:
            record["waiver"] = make_waiver(record, waiver, clock())
        except GateError as exc:
            note = str(exc)
    elif waiver is not None:
        note = f"waiver not needed: result is {result}"
    path = write_record(git_common_dir(repo), record)
    admitted, reason = record_admits(record, candidate, base)
    kept: Path | None = root
    if admitted and result == PASS:
        shutil.rmtree(root, ignore_errors=True)
        kept = None
    return RunOutcome(record, path, admitted, reason, kept, note)


def waive_record(repo: str | Path, sha: str, reason: str,
                 now: Callable[[], str] | None = None) -> tuple[Path, dict]:
    """Attach a waiver to the stored record of ``sha``; refused unless ``waiver_allowed``."""
    store = git_common_dir(repo)
    record = load_record(store, sha)
    if record is None:
        raise GateError(f"no smoke record for {short(sha)}; run: {RUN_COMMAND}")
    record["waiver"] = make_waiver(record, reason, (now or _utc_now)())
    return write_record(store, record), record
