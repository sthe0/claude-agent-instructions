"""Mutants for scripts/lib/time_profile.py, applied to the tests through the environment.

A test file that imports `time_profile_mutant` (an autouse fixture) runs against one
deliberately wrong extractor when TIME_PROFILE_MUTANT names an entry of MUTANTS. The
negative control is: the same test file must FAIL (pytest exit 1) under every mutant.

A mutant that cannot be applied, or a name that is not in MUTANTS, ends the run with
exit 4, so a typo or a renamed hook cannot read as "the tests caught it".

Mutants are pure transforms over the extractor's own results, and a `Skipped` passes
through unchanged. Tests reach the extractor only through `time_profile` module
attributes, which is what makes a monkeypatch of those attributes visible to them.
"""
from __future__ import annotations

import dataclasses
import os

import pytest

from lib import time_profile

ENV = "TIME_PROFILE_MUTANT"


def _on_profile(transform):
    original = time_profile.profile_session

    def mutated(transcript_path, ctx):
        result = original(transcript_path, ctx)
        if isinstance(result, time_profile.Skipped):
            return result
        return transform(result)

    return mutated


def _all_to_model_gap(profile):
    total = sum(profile.partition.values())
    return dataclasses.replace(profile, partition={time_profile.MODEL_GAP: total})


def _overlay_into_partition(profile):
    partition = dict(profile.partition)
    for key, ms in profile.overlays.items():
        partition[key] = partition.get(key, 0) + ms
    return dataclasses.replace(profile, partition=partition)


def _apply_all_to_model_gap(monkeypatch):
    monkeypatch.setattr(time_profile, "profile_session", _on_profile(_all_to_model_gap))


def _apply_overlay_into_partition(monkeypatch):
    monkeypatch.setattr(time_profile, "profile_session", _on_profile(_overlay_into_partition))


def _apply_cursor_from_zero(monkeypatch):
    monkeypatch.setattr(time_profile, "_resume_offset", lambda saved, stat: 0)


def _apply_rotation_by_size_only(monkeypatch):
    def resume(saved, stat):
        offset = int((saved or {}).get("offset") or 0)
        return 0 if stat.st_size < offset else offset

    monkeypatch.setattr(time_profile, "_resume_offset", resume)


def _apply_cache_ignores_mtime(monkeypatch):
    monkeypatch.setattr(time_profile, "_cache_fresh", lambda entry, fingerprint: entry is not None)


def _apply_cache_never_fresh(monkeypatch):
    monkeypatch.setattr(time_profile, "_cache_fresh", lambda entry, fingerprint: False)


MUTANTS = {
    "all_to_model_gap": (
        "the whole partition is reported as model:gap; wall-clock is still conserved",
        _apply_all_to_model_gap,
    ),
    "overlay_into_partition": (
        "each overlay's ms is added into the partition, so overlapping time is counted twice",
        _apply_overlay_into_partition,
    ),
    "cursor_from_zero": (
        "ledgers are always re-read from byte 0 instead of the stored offset",
        _apply_cursor_from_zero,
    ),
    "rotation_by_size_only": (
        "a replaced ledger (new inode) at or above the stored size is not rescanned",
        _apply_rotation_by_size_only,
    ),
    "cache_ignores_mtime": (
        "a changed transcript is served from the cache",
        _apply_cache_ignores_mtime,
    ),
    "cache_never_fresh": (
        "every transcript is re-parsed on every run",
        _apply_cache_never_fresh,
    ),
}


@pytest.fixture(scope="session", autouse=True)
def time_profile_mutant():
    # Session scope: pytest.exit from a function-scoped fixture crashes the teardown of
    # tmp_path and ends the run with exit 1, which would read as "the tests caught it".
    name = os.environ.get(ENV)
    if not name:
        yield
        return
    if name not in MUTANTS:
        pytest.exit(f"{ENV}={name!r} is not one of {sorted(MUTANTS)}", returncode=4)
    patcher = pytest.MonkeyPatch()
    try:
        MUTANTS[name][1](patcher)
    except Exception as exc:
        patcher.undo()
        pytest.exit(f"could not apply mutant {name!r}: {exc!r}", returncode=4)
    yield
    patcher.undo()
