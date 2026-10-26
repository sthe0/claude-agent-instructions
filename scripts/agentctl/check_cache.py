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


def check_key(tree_identity: str, command: str, expected_exit: int) -> str:
    digest = hashlib.sha256()
    for part in (tree_identity, command, str(expected_exit)):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


@dataclass
class CheckSpec:
    """One shell check of a verify-final run. `tree_id` is the venue tree identity
    (None: unidentifiable); `cacheable` says the session cache may serve it;
    `outcome_hit` says the stage's own recorded outcome already proves it green."""
    id: str
    command: str
    expected_exit: int
    tree_id: str | None
    cacheable: bool
    outcome_hit: bool = False

    @property
    def key(self) -> str | None:
        if self.tree_id is None:
            return None
        return check_key(self.tree_id, self.command, self.expected_exit)


@dataclass
class Decision:
    spec: CheckSpec
    decision: str
    reason: str
    same_as: str | None = None


def decide(specs: list[CheckSpec], cache: Mapping[str, object]) -> list[Decision]:
    """Per spec, in order: serve from a cache, repeat an earlier spec of this run, or run."""
    first_by_key: dict[str, CheckSpec] = {}
    decisions: list[Decision] = []
    for spec in specs:
        key = spec.key
        if key is None:
            decisions.append(Decision(spec, MISS, "identity_unavailable"))
            continue
        if key in first_by_key:
            earlier = first_by_key[key]
            decisions.append(Decision(spec, DEDUP, f"same_as:{earlier.id}", earlier.id))
            continue
        first_by_key[key] = spec
        if spec.outcome_hit:
            decisions.append(Decision(spec, HIT, "stage_outcome"))
        elif not spec.cacheable:
            decisions.append(Decision(spec, MISS, "not_cacheable"))
        elif key in cache:
            decisions.append(Decision(spec, HIT, "session_cache"))
        else:
            decisions.append(Decision(spec, MISS, "no_cached_green"))
    return decisions


def store_green(cache: dict, key: str, *, at: float, label: str) -> None:
    cache[key] = {"at": at, "label": label}
    if len(cache) > CACHE_MAX_ENTRIES:
        oldest = sorted(cache, key=lambda k: cache[k].get("at", 0.0))
        for stale in oldest[: len(cache) - CACHE_MAX_ENTRIES]:
            del cache[stale]


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
    raw = env.get(name, "").strip()
    if raw.lstrip("-").isdigit() and int(raw) >= minimum:
        return int(raw)
    return None


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


def run_bounded(jobs: list[Job], *, cap: int, budget: int) -> tuple[list[object], int]:
    """Run `jobs` in order of submission, at most `cap` at once and never more than
    `budget` weight at once — except that a job always starts when none is running.
    Returns (results in job order, peak number running at once)."""
    if not jobs:
        return [], 0
    if cap <= 1 or len(jobs) == 1:
        return [job.run() for job in jobs], 1
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
                running[pool.submit(jobs[i].run)] = i
                weight_running += jobs[i].weight
                peak = max(peak, len(running))
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in done:
                i = running.pop(future)
                weight_running -= jobs[i].weight
                results[i] = future.result()
    return results, peak
