"""The flaky-test list and the machine-local journal the suite gate feeds it from.

Difficulty removed: a test that fails once and passes on both reruns no longer holds a landing, so
without a trace it would never be looked at again. The gate appends each such test to a journal that
lives in the git common dir (``suite-gate/flaky-pending.jsonl``, or the file ``SUITE_GATE_FLAKY_JOURNAL``
names); a landing folds the journal into scripts/tests/flaky.toml as one separate commit
``tests: record flaky tests`` whose ``Flake-Entries:`` trailer names the folded entry ids. The gate
itself never writes the tree.

An entry is counted once: the fold skips entries whose ids a commit already carries (the branch's own
history, which contains origin/main), and the journal drops an entry once origin/main carries its id.
flaky.toml gives a listed test no treatment at the gate; it is the worklist for fixing each test at its
cause, and an entry whose count reached ``REVIEW_COUNT`` is printed.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import secrets
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from . import landing_gate as shared

JOURNAL_ENV = "SUITE_GATE_FLAKY_JOURNAL"
JOURNAL_RELATIVE = "suite-gate/flaky-pending.jsonl"
LIST_PATH = "scripts/tests/flaky.toml"
TABLE = "flaky"
TRAILER = "Flake-Entries"
COMMIT_SUBJECT = "tests: record flaky tests"
REVIEW_COUNT = 3
HEADER = (
    "# Tests that failed on a full suite run and then passed on both reruns of the pre-land suite gate.\n"
    "# Written only by the landing fold (scripts/lib/flake_list.py), never by hand or by the gate.\n"
    "# count: times the gate saw the test flake; first_seen / last_seen: UTC instants of the journal entries.\n"
    "# A listed test is treated like any other by the gate; each entry is a task to fix the test at its cause.\n"
)


def journal_path(common_dir: str | Path) -> Path:
    override = os.environ.get(JOURNAL_ENV)
    return Path(override) if override else Path(common_dir) / JOURNAL_RELATIVE


def new_entry(test: str, candidate: str, tip: str, at: str) -> dict:
    return {"id": secrets.token_hex(8), "test": test, "candidate": candidate, "tip": tip, "at": at}


@contextlib.contextmanager
def _locked(journal: Path):
    lock = journal.with_name(journal.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _parse_entries(text: str) -> list[dict]:
    entries = []
    for raw in text.splitlines():
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and all(isinstance(entry.get(k), str) and entry[k]
                                           for k in ("id", "test", "at")):
            entries.append(entry)
    return entries


def read_entries(journal: Path) -> list[dict]:
    """The journal's entries; a torn or malformed line is skipped, an absent journal has none."""
    try:
        return _parse_entries(journal.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


def append_entries(journal: Path, entries: Iterable[dict]) -> None:
    lines = "".join(json.dumps(entry, sort_keys=True) + "\n" for entry in entries)
    if not lines:
        return
    with _locked(journal):
        with open(journal, "a", encoding="utf-8") as fh:
            fh.write(lines)
            fh.flush()
            os.fsync(fh.fileno())


# ── flaky.toml ────────────────────────────────────────────────────────────

def parse_list(text: str) -> dict[str, dict]:
    """``{test id: {count, first_seen, last_seen}}`` of a flaky.toml text."""
    table = tomllib.loads(text).get(TABLE, {})
    return {test: dict(fields) for test, fields in table.items()}


def render_list(entries: Mapping[str, Mapping]) -> str:
    rows = "".join(
        f"{json.dumps(test, ensure_ascii=False)} = {{ count = {int(fields['count'])}, "
        f"first_seen = {json.dumps(fields['first_seen'])}, last_seen = {json.dumps(fields['last_seen'])} }}\n"
        for test, fields in sorted(entries.items()))
    return f"{HEADER}\n[{TABLE}]\n{rows}"


def due_lines(entries: Mapping[str, Mapping]) -> list[str]:
    """One line per listed test whose count reached ``REVIEW_COUNT``."""
    return [f"{test}: flaked {fields['count']} times (last {fields['last_seen']}); fix it at its cause"
            for test, fields in sorted(entries.items()) if fields["count"] >= REVIEW_COUNT]


def merge_entries(table: Mapping[str, Mapping], entries: Sequence[dict]) -> dict[str, dict]:
    """``table`` with the journal ``entries`` counted in: an unlisted test is added, a listed one's count raised."""
    merged = {test: dict(fields) for test, fields in table.items()}
    for entry in sorted(entries, key=lambda e: e["at"]):
        fields = merged.get(entry["test"])
        if fields is None:
            merged[entry["test"]] = {"count": 1, "first_seen": entry["at"], "last_seen": entry["at"]}
        else:
            fields["count"] += 1
            fields["first_seen"] = min(fields["first_seen"], entry["at"])
            fields["last_seen"] = max(fields["last_seen"], entry["at"])
    return merged


# ── what a commit already carries ─────────────────────────────────────────

def carried_ids(repo: str | Path, ref: str) -> set[str]:
    """The entry ids the ``Flake-Entries:`` trailers in ``ref``'s history name."""
    out = _git_with(repo, shared.clean_env(), "log", f"--grep=^{TRAILER}:",
                    f"--format=%(trailers:key={TRAILER},valueonly)", ref)
    return {token for line in out.splitlines() for token in line.split()}


def drop_landed(repo: str | Path, journal: Path, trunk_ref: str) -> int:
    """Rewrite the journal without the entries ``trunk_ref`` carries; the number dropped."""
    if not journal.exists():
        return 0
    landed = carried_ids(repo, trunk_ref)
    with _locked(journal):
        entries = read_entries(journal)
        kept = [entry for entry in entries if entry["id"] not in landed]
        if len(kept) == len(entries):
            return 0
        fd, tmp = tempfile.mkstemp(dir=journal.parent, prefix=".tmp-", suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("".join(json.dumps(entry, sort_keys=True) + "\n" for entry in kept))
        os.replace(tmp, journal)
    return len(entries) - len(kept)


# ── the fold ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Fold:
    sha: str
    entry_ids: tuple[str, ...]
    table: dict[str, dict]


def _list_text_at(repo: str | Path, sha: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), "show", f"{sha}:{LIST_PATH}"], capture_output=True, text=True,
                          env=shared.clean_env())
    return proc.stdout if proc.returncode == 0 else render_list({})


def _git_with(repo: str | Path, env: Mapping[str, str], *args: str, stdin: str | None = None) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], input=stdin, capture_output=True, text=True,
                          env=dict(env))
    if proc.returncode != 0:
        raise shared.GateError(f"git {args[0]} failed (exit {proc.returncode}): "
                               f"{shared.one_line(proc.stderr or proc.stdout, 200)}")
    return proc.stdout.strip()


def _write_commit(repo: str | Path, parent: str, text: str, entry_ids: Sequence[str],
                  identity: Mapping[str, str]) -> str:
    plain = {**shared.clean_env(), **identity}
    blob = _git_with(repo, plain, "hash-object", "-w", "--stdin", stdin=text)
    with tempfile.TemporaryDirectory(prefix="flake-fold-") as scratch:
        env = {**plain, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
        _git_with(repo, env, "read-tree", parent)
        _git_with(repo, env, "update-index", "--add", "--cacheinfo", f"100644,{blob},{LIST_PATH}")
        tree = _git_with(repo, env, "write-tree")
        message = f"{COMMIT_SUBJECT}\n\n{TRAILER}: {' '.join(entry_ids)}\n"
        return _git_with(repo, env, "commit-tree", tree, "-p", parent, "-m", message)


def fold(repo: str | Path, journal: Path, branch_sha: str, identity: Mapping[str, str]) -> Fold | None:
    """A commit on ``branch_sha`` that counts the journal's entries the branch's history does not carry.

    Nothing is moved: the caller decides what to do with the commit. ``None`` when nothing is pending.
    """
    carried = carried_ids(repo, branch_sha)
    pending = [entry for entry in read_entries(journal) if entry["id"] not in carried]
    if not pending:
        return None
    table = merge_entries(parse_list(_list_text_at(repo, branch_sha)), pending)
    ids = tuple(entry["id"] for entry in pending)
    sha = _write_commit(repo, branch_sha, render_list(table), ids, identity)
    return Fold(sha, ids, table)
