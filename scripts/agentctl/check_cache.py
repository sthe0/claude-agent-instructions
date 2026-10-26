"""verify-final's check evaluation: session green-check cache, dedupe, worker budget.

`cmd_verify_final` gathers its checks (stage verify_commands, then final_checks)
as `CheckSpec`s; `decide` says which are served from a cache, which repeat an
earlier check of the same run, and which must run; `run_bounded` runs the latter
under the concurrency cap and worker budget. Only green results are cached, and a
check whose venue tree cannot be identified is never cached or grouped.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import shlex
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Callable, Mapping

from .plan import pytest_segments

PARALLEL_ENV = "AGENTCTL_VERIFY_FINAL_PARALLEL"
CPU_BUDGET_ENV = "AGENTCTL_VERIFY_FINAL_CPU_BUDGET"
CACHE_MAX_ENTRIES = 256

HIT = "hit"
DEDUP = "dedup"
MISS = "miss"


def check_key(venue: str, tree_identity: str, command: str, expected_exit: int) -> str:
    digest = hashlib.sha256()
    for part in (venue, tree_identity, command, str(expected_exit)):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


@dataclass
class CheckSpec:
    """One shell check of a verify-final run. `venue` is the directory it runs in and
    `tree_id` that directory's tree identity (None: unidentifiable); `cacheable` says
    the session cache may serve it; `outcome_hit` says the stage's own recorded
    outcome already proves it green."""
    id: str
    command: str
    expected_exit: int
    venue: str
    tree_id: str | None
    cacheable: bool
    outcome_hit: bool = False
    reads: list[str] | None = None
    tree_sha: str | None = None
    outcome_base_tree: str | None = None

    @property
    def key(self) -> str | None:
        if self.tree_id is None:
            return None
        return check_key(self.venue, self.tree_id, self.command, self.expected_exit)

    @property
    def definition(self) -> str:
        return definition_key(self.venue, self.command, self.expected_exit)


@dataclass
class Decision:
    spec: CheckSpec
    decision: str
    reason: str
    same_as: str | None = None


def decide(specs: list[CheckSpec], cache: Mapping[str, object]) -> list[Decision]:
    """Per spec, in order: serve from a cache, repeat an earlier spec of this run, or run."""
    first_by_group: dict[tuple, CheckSpec] = {}
    decisions: list[Decision] = []
    for spec in specs:
        key = spec.key
        if key is None:
            decisions.append(Decision(spec, MISS, "identity_unavailable"))
            continue
        group = (key, None if spec.reads is None else tuple(spec.reads))
        if group in first_by_group:
            earlier = first_by_group[group]
            decisions.append(Decision(spec, DEDUP, f"same_as:{earlier.id}", earlier.id))
            continue
        first_by_group[group] = spec
        if spec.outcome_hit:
            decisions.append(Decision(spec, HIT, "stage_outcome"))
        elif not spec.cacheable:
            decisions.append(Decision(spec, MISS, "not_cacheable"))
        elif key in cache and _serves(cache[key], spec):
            decisions.append(Decision(spec, HIT, "session_cache"))
        else:
            decisions.append(Decision(spec, MISS, "no_cached_green"))
    return decisions


def _serves(entry: object, spec: CheckSpec) -> bool:
    """A real green serves any check of its key; a derived (narrowed) one only a check that
    declares the very `verify_reads` it was narrowed under."""
    if not isinstance(entry, dict) or not entry.get("derived"):
        return True
    return spec.reads is not None and entry.get("reads") == spec.reads


def definition_key(venue: str, command: str, expected_exit: int) -> str:
    return check_key(venue, "", command, expected_exit)


def store_green(
    cache: dict, key: str, *, at: float, label: str,
    tree: str | None = None, definition: str | None = None, derived: bool = False,
    reads: list[str] | None = None,
) -> None:
    held = cache.get(key)
    if derived and isinstance(held, dict) and not held.get("derived"):
        return
    entry: dict = {"at": at, "label": label}
    if tree is not None:
        entry["tree"] = tree
        entry["def"] = definition
    if derived:
        entry["derived"] = True
        entry["reads"] = list(reads) if reads is not None else None
    cache[key] = entry
    if len(cache) > CACHE_MAX_ENTRIES:
        oldest = sorted(cache, key=lambda k: cache[k].get("at", 0.0))
        for stale in oldest[: len(cache) - CACHE_MAX_ENTRIES]:
            del cache[stale]


NARROWED = "narrowed"
REFUSED = "narrow_refused:"
ALLOWLIST_ROOTS = ("docs/", "memory-global/")
ALLOWLIST_SUFFIX = ".md"


def allowlisted(path: str) -> bool:
    """A path whose change a declared-`verify_reads` check is allowed to ignore: a Markdown
    file under docs/ or memory-global/. A closed constant, not configuration."""
    if path.startswith("/") or ".." in path.split("/"):
        return False
    name = path.rsplit("/", 1)[-1]
    return path.startswith(ALLOWLIST_ROOTS) and name.endswith(ALLOWLIST_SUFFIX) and name != ALLOWLIST_SUFFIX


def _expand_braces(pattern: str) -> list[str]:
    start = pattern.find("{")
    if start < 0:
        return [pattern]
    depth = 0
    for i in range(start, len(pattern)):
        if pattern[i] == "{":
            depth += 1
        elif pattern[i] == "}":
            depth -= 1
            if depth == 0:
                parts, level, cut = [], 0, start + 1
                for j in range(start + 1, i):
                    if pattern[j] == "{":
                        level += 1
                    elif pattern[j] == "}":
                        level -= 1
                    elif pattern[j] == "," and level == 0:
                        parts.append(pattern[cut:j])
                        cut = j + 1
                parts.append(pattern[cut:i])
                return [
                    expanded
                    for part in parts
                    for expanded in _expand_braces(pattern[:start] + part + pattern[i + 1:])
                ]
    return [pattern]


def _glob_regex(pattern: str) -> re.Pattern:
    out, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif ch == "*":
            out.append(".*")
            i += 1
        elif ch == "?":
            out.append(".")
            i += 1
        elif ch == "[":
            end = pattern.find("]", i + 2)
            if end < 0:
                out.append(re.escape(ch))
                i += 1
            else:
                body = pattern[i + 1:end]
                body = "^" + body[1:] if body[:1] in ("!", "^") else body
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = end + 1
        else:
            out.append(re.escape(ch))
            i += 1
    return re.compile("".join(out))


def reads_match(path: str, patterns: list[str]) -> bool:
    """True when `path` may be among the files the declared `patterns` name. Over-approximates:
    `*` matches any run of characters including `/` and `?` any one character (fnmatch
    style), a directory pattern covers everything under it, and a bracket class is matched
    by its literal directory prefix alone."""
    for raw in patterns:
        pattern = raw[2:] if raw.startswith("./") else raw
        if pattern in ("", ".", "./"):
            return True
        classes = [pattern.index(c) for c in "[]" if c in pattern]
        if classes:
            prefix = pattern[:min(classes)]
            if path.startswith(prefix[:prefix.rfind("/") + 1]):
                return True
        directory = pattern.rstrip("/")
        if path == directory or path.startswith(directory + "/"):
            return True
        if any(_glob_regex(alt).fullmatch(path) for alt in _expand_braces(pattern)):
            return True
    return False


def names_path(command: str, path: str) -> bool:
    """True when `command` may name `path`, or a directory containing it, anywhere. Any
    ancestor prefix of `path` (`docs`, `docs/a`, `docs/a/x.md`) occurring as a substring
    of the command (case-insensitive, after brace expansion), a word or a fragment of a
    word (split at quotes, brackets, `;`, `,`) that is a glob matching one of those
    prefixes, and any word that cannot be resolved to a repo-relative path (a variable,
    a `..`) count as naming it, whatever shell form carries the name (absolute path, `~`,
    redirection, code). A backstop: the `verify_reads` declaration carries the guarantee."""
    parts = path.split("/")
    prefixes = ["/".join(parts[:n]) for n in range(1, len(parts) + 1)]
    lowered = [prefix.lower() for prefix in prefixes]
    if any(prefix in alt for alt in _expand_braces(command.lower()) for prefix in lowered):
        return True
    try:
        words = shlex.split(command)
    except ValueError:
        return True
    for word in words:
        if any(c in word for c in "$`") or ".." in word.split("/"):
            return True
        for candidate in [word, *re.split(r"""[\s'"()\[\]{};,<>|&=:]+""", word)]:
            if not any(c in candidate for c in "*?["):
                continue
            glob = candidate[2:] if candidate.startswith("./") else candidate
            if any(
                _glob_regex(alt).fullmatch(prefix)
                for alt in _expand_braces(glob) for prefix in prefixes
            ):
                return True
        for piece in re.split(r"[=:,]", word) + [word]:
            if not piece:
                continue
            while piece.startswith("./") and len(piece) > 2:
                piece = piece[2:]
            if piece in (".", "./"):
                return True
            if reads_match(path, [piece]):
                return True
    return False


class TreeProbe:
    """What the narrowed lookup asks git: whether a tree object is readable, and which
    paths differ between two trees (None: git failed)."""

    def tree_exists(self, tree: str) -> bool:
        raise NotImplementedError

    def changed_paths(self, old: str, new: str) -> list[str] | None:
        raise NotImplementedError


def narrow(spec: CheckSpec, cache: Mapping[str, dict], probe: TreeProbe) -> tuple[bool, str]:
    """Whether `spec` — which declared `verify_reads` and missed the exact cache — may be
    served from the newest earlier green of the same command in the same venue, and why
    not otherwise. Served only if every path changed since is allowlisted and neither
    declared in `reads` nor named in the command; every doubt is a refusal."""
    if spec.reads is None or spec.tree_sha is None:
        return False, REFUSED + "no_candidate"
    base = None
    candidates = [
        e for e in cache.values()
        if e.get("def") == spec.definition and e.get("tree") and not e.get("derived")
    ]
    if candidates:
        base = max(candidates, key=lambda e: e.get("at", 0.0))["tree"]
    elif spec.outcome_base_tree:
        base = spec.outcome_base_tree
    if base is None:
        return False, REFUSED + "no_candidate"
    if not probe.tree_exists(base):
        return False, REFUSED + "tree_object_missing"
    changed = probe.changed_paths(base, spec.tree_sha)
    if changed is None:
        return False, REFUSED + "git_failed"
    if not all(allowlisted(p) for p in changed):
        return False, REFUSED + "path_outside_allowlist"
    if any(reads_match(p, spec.reads) for p in changed):
        return False, REFUSED + "path_in_verify_reads"
    if any(names_path(spec.command, p) for p in changed):
        return False, REFUSED + "path_named_in_command"
    return True, f"{NARROWED}: {len(changed)} docs path(s) changed since an earlier green"


def narrow_misses(
    decisions: list[Decision], cache: Mapping[str, dict], probe_for: Callable[[CheckSpec], TreeProbe | None],
) -> list[Decision]:
    """Re-decide the exact misses of specs that declared `verify_reads`: `narrowed` when
    `narrow` allows it, else the same miss with the refusal as its reason."""
    out: list[Decision] = []
    for d in decisions:
        if d.decision != MISS or d.reason != "no_cached_green" or d.spec.reads is None:
            out.append(d)
            continue
        try:
            probe = probe_for(d.spec)
            ok, reason = narrow(d.spec, cache, probe) if probe is not None else (False, REFUSED + "git_failed")
        except Exception:
            ok, reason = False, REFUSED + "git_failed"
        out.append(Decision(d.spec, NARROWED if ok else MISS, reason))
    running = {d.spec.key: d.spec.id for d in out if d.decision == MISS and d.spec.key is not None}
    return [
        Decision(d.spec, DEDUP, f"same_as:{running[d.spec.key]}", running[d.spec.key])
        if d.decision == NARROWED and d.spec.key in running else d
        for d in out
    ]


def pytest_weight(command: str, cpu_count: int) -> int:
    """CPUs a check occupies: its pytest `-n` value (auto/logical = every CPU), else 1."""
    weight = 1
    for seg in pytest_segments(command):
        for i, tok in enumerate(seg):
            if tok == "-n" or tok == "--numprocesses":
                value = seg[i + 1] if i + 1 < len(seg) else ""
            elif tok.startswith("--numprocesses="):
                value = tok.split("=", 1)[1]
            elif tok.startswith("-n") and len(tok) > 2:
                value = tok[2:]
            else:
                continue
            if value in ("auto", "logical"):
                weight = max(weight, cpu_count)
            elif value.isdigit():
                weight = max(weight, int(value))
    return max(weight, 1)


def _env_int(name: str, env: Mapping[str, str], minimum: int) -> int | None:
    try:
        value = int(env.get(name, "").strip())
    except ValueError:
        return None
    return value if value >= minimum else None


def resolve_cap(configured: int, env: Mapping[str, str] | None = None) -> int:
    override = _env_int(PARALLEL_ENV, os.environ if env is None else env, 1)
    return override if override is not None else max(configured, 1)


def resolve_budget(cpu_count: int, load1: float, env: Mapping[str, str] | None = None) -> int:
    """CPUs this verify-final may occupy: those the 1-minute load average leaves free."""
    override = _env_int(CPU_BUDGET_ENV, os.environ if env is None else env, 0)
    if override is not None:
        return override
    return max(math.floor(cpu_count - load1), 0)


def system_probe() -> tuple[int, float]:
    """(CPU count, 1-minute load average)."""
    cpus = os.cpu_count() or 1
    try:
        return cpus, os.getloadavg()[0]
    except OSError:
        return cpus, 0.0


@dataclass
class Job:
    weight: int
    run: Callable[[], object]


def run_bounded(
    jobs: list[Job], *, cap: int, budget: int,
    on_error: Callable[[Exception], object] | None = None,
) -> tuple[list[object], int]:
    """Run `jobs` in order of submission, at most `cap` at once and never more than
    `budget` weight at once — except that a job always starts when none is running.
    A job that raises yields `on_error(exc)` as its result, or propagates without
    `on_error`. Returns (results in job order, peak number running at once)."""
    if not jobs:
        return [], 0

    def call(job: Job) -> object:
        try:
            return job.run()
        except Exception as exc:
            if on_error is None:
                raise
            return on_error(exc)

    if cap <= 1 or len(jobs) == 1:
        return [call(job) for job in jobs], 1
    results: list[object] = [None] * len(jobs)
    pending = deque(range(len(jobs)))
    running: dict = {}
    weight_running = 0
    peak = 0
    with ThreadPoolExecutor(max_workers=cap) as pool:
        while pending or running:
            while (
                pending and len(running) < cap
                and (not running or weight_running + jobs[pending[0]].weight <= budget)
            ):
                i = pending.popleft()
                running[pool.submit(call, jobs[i])] = i
                weight_running += jobs[i].weight
                peak = max(peak, len(running))
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in done:
                i = running.pop(future)
                weight_running -= jobs[i].weight
                results[i] = future.result()
    return results, peak
