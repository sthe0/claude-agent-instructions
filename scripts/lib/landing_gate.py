"""Gate-generic pieces of a pre-land gate: git edges, a keyed record store, binding, pre-push loop.

Difficulty removed: a gate that blocks a landing until a recorded check passed needs the same
plumbing whatever it checks - fail-closed git calls, an atomic record store, the binding of a record
to a commit and to the live remote tip, and the loop over git's pre-push stdin. Written once here,
each gate (scripts/lib/instruction_smoke_gate.py is the first) supplies only its own rule, its
record schema and its wording.

Fail closed throughout: a missing, unreadable or inconsistent record and any git error are
refusals, never admissions.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Protocol

# Must equal the no-push-rights regex of scripts/sync-instructions-repo.sh push_and_degrade
# (a test pins the two together): text matching it makes that script report "no push rights".
PUSH_RIGHTS_PATTERN = r"permission|denied|forbidden|403|read[ -]?only|not authorized|access rights"
_PUSH_RIGHTS = re.compile(PUSH_RIGHTS_PATTERN, re.IGNORECASE)

ZERO_SHA = re.compile(r"0+")
_SHA = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")

NOT_REQUIRED, ADMITTED, REFUSED = "not-required", "admitted", "refused"


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


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def normalize_reason(reason: object) -> str:
    """A waiver reason as one printable line; an empty one is refused."""
    cleaned = one_line(reason)
    if not cleaned:
        raise GateError("waiver needs a reason", refused=True)
    return cleaned


# ── git edges ─────────────────────────────────────────────────────────────

def _clean_env() -> dict[str, str]:
    env = dict(os.environ)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        env.pop(name, None)
    return env


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


def path_in_tree(repo: str | Path, sha: str, path: str) -> bool:
    return bool(_git_ok(repo, "ls-tree", "--name-only", sha, "--", path).strip())


def fetch_remote_tip(repo: str | Path, remote: str, trunk: str) -> str:
    tracking = f"refs/remotes/{remote}/{trunk}"
    _git_ok(repo, "fetch", "--quiet", remote, f"+refs/heads/{trunk}:{tracking}")
    return resolve_commit(repo, tracking)


def is_ancestor(repo: str | Path, ancestor: str, descendant: str) -> bool:
    proc = _git(repo, "merge-base", "--is-ancestor", ancestor, descendant)
    if proc.returncode not in (0, 1):
        raise GateError(f"git merge-base failed: {one_line(proc.stderr, 200)}")
    return proc.returncode == 0


# ── record store ──────────────────────────────────────────────────────────

def record_path(common_dir: str | Path, dirname: str, key_sha: str) -> Path:
    """``<common dir>/<dirname>/<key_sha>.json``; ``dirname`` may be nested."""
    if not is_valid_sha(key_sha):
        raise RecordError(f"not a full commit sha: {one_line(key_sha, 80)}")
    return Path(common_dir) / dirname / f"{key_sha}.json"


def read_record(path: str | Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RecordError(f"{Path(path).name} is unreadable: {one_line(exc, 160)}") from exc
    if not isinstance(data, dict):
        raise RecordError(f"{Path(path).name} is not a JSON object")
    return data


def load_record(common_dir: str | Path, dirname: str, key_sha: str) -> dict | None:
    path = record_path(common_dir, dirname, key_sha)
    if not path.exists():
        return None
    return read_record(path)


def write_record(common_dir: str | Path, dirname: str, key_sha: str, record: dict) -> Path:
    path = record_path(common_dir, dirname, key_sha)
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


# ── binding of a record to the candidate and the live remote tip ──────────

def check_binding(record: dict, *, schema: str, candidate_sha: str, live_remote_sha: str,
                  rerun_hint: str) -> tuple[bool, str]:
    """(bound, reason): the record is of ``schema``, for the candidate, built from it, on the live tip."""
    if record.get("schema") != schema:
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
            f"{short(live_remote_sha)}; {rerun_hint}"
        )
    return True, ""


# ── landing decision and the pre-push loop ────────────────────────────────

@dataclass(frozen=True)
class Decision:
    allowed: bool
    kind: str
    reason: str


@dataclass(frozen=True)
class PrePushResult:
    allowed: bool
    messages: tuple[str, ...]


class Wording(Protocol):
    """The gate's own lines: what a refusal prints, what an allowed decision prints."""

    def refused(self, reason: str) -> Iterable[str]: ...

    def decision(self, local_sha: str, decision: Decision) -> Iterable[str]: ...

    def no_remote(self, trunk: str) -> str: ...

    def unknown_remote(self, trunk: str, remote_sha: str) -> str: ...


def prepush_loop(lines: Iterable[str], repo: str | Path, trunk: str, *,
                 evaluate: Callable[[str, str], Decision], wording: Wording) -> PrePushResult:
    """Decide a push from git's pre-push stdin lines: ``<local-ref> <local-sha> <remote-ref> <remote-sha>``."""
    messages: list[str] = []
    allowed = True

    def refuse(reason: str) -> None:
        nonlocal allowed
        allowed = False
        messages.extend(wording.refused(reason))

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
            refuse(wording.no_remote(trunk))
            continue
        if not object_exists(repo, remote_sha):
            refuse(wording.unknown_remote(trunk, remote_sha))
            continue
        decision = evaluate(local_sha, remote_sha)
        if decision.allowed:
            messages.extend(wording.decision(local_sha, decision))
        else:
            refuse(decision.reason)
    return PrePushResult(allowed, tuple(defuse(m) for m in messages))
