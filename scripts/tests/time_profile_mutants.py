"""Mutants for scripts/lib/time_profile.py, applied to the tests through the environment.

A test file that imports `time_profile_mutant` (an autouse fixture) runs against one
deliberately wrong `time_profile` (extractor or aggregation) when TIME_PROFILE_MUTANT
names an entry of MUTANTS. The negative control is: the test file that owns the mutant
(see suite_for) must FAIL (pytest exit 1) under it.

A mutant that cannot be applied, or a name that is not in MUTANTS, ends the run with
exit 4, so a typo or a renamed hook cannot read as "the tests caught it".

Each mutant names the suite that must catch it: the aggregation mutants
(AGGREGATE_MUTANTS) belong to test_time_profile_aggregate.py, all others to
test_time_profile_extract.py; see suite_for().

Extractor mutants are pure transforms over the extractor's own results, and a `Skipped`
passes through unchanged. Tests reach `time_profile` only through its module
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


def _apply_spawn_join_on_engine_id(monkeypatch):
    original = time_profile._FOLDS["spawn"]

    def engine_keyed(agg, row):
        original(agg, {k: v for k, v in row.items() if k != "session_id"})

    monkeypatch.setitem(time_profile._FOLDS, "spawn", engine_keyed)


def _apply_node_ignores_bindings(monkeypatch):
    monkeypatch.setattr(time_profile, "_engine_sessions", lambda harness_session, state_dir: [harness_session])


def _apply_same_ts_hook_dropped(monkeypatch):
    monkeypatch.setattr(time_profile._Walk, "_carve_same_ts", lambda self, entry, ts: None)


def _apply_typed_message_is_background(monkeypatch):
    monkeypatch.setattr(time_profile, "_is_typed_mid_turn", lambda entry, attachment: False)


def _apply_inter_agent_message_is_model_time(monkeypatch):
    monkeypatch.setattr(time_profile, "_is_injected", lambda entry, attachment: (
        attachment.get("commandMode") == "task-notification"))


def _apply_zero_share_tail_not_skipped(monkeypatch):
    def carve(self, entry, ts):
        hook = self._hook_attachment(entry)
        if hook is None or ts != self._tail_ts:
            return
        key, want = hook
        for i in range(len(self._tail) - 1, -1, -1):
            if not want:
                break
            tail_key, ms = self._tail[i]
            taken = min(ms, want)
            want -= taken
            self._tail[i] = (tail_key, ms - taken)
            self.partition[tail_key] -= taken
            if not self.partition[tail_key]:
                del self.partition[tail_key]
            self._add(key, taken)
            self.hook_ms += taken

    monkeypatch.setattr(time_profile._Walk, "_carve_same_ts", carve)


def _apply_node_dwell_unclipped(monkeypatch):
    original = time_profile.node_dwell
    monkeypatch.setattr(time_profile, "node_dwell", lambda state_path, window=None: original(state_path))


def _apply_huge_float_accepted(monkeypatch):
    monkeypatch.setattr(
        time_profile, "_finite_number",
        lambda value: not isinstance(value, bool) and isinstance(value, (int, float)) and value == value
        and value not in (float("inf"), float("-inf")),
    )


def _apply_skip_detail_dropped(monkeypatch):
    original = time_profile.profile_session

    def hiding(transcript_path, ctx):
        try:
            return original(transcript_path, ctx)
        except Exception:
            return time_profile.Skipped(time_profile.Path(transcript_path).stem, str(transcript_path), "unparseable")

    monkeypatch.setattr(time_profile, "profile_session", hiding)


def _apply_cache_ignores_version(monkeypatch):
    def get(self, path):
        entry = time_profile._read_json(self._file(path))
        return entry if isinstance(entry, dict) else None

    monkeypatch.setattr(time_profile.SessionCache, "get", get)


def _apply_node_epoch_ts_dropped(monkeypatch):
    monkeypatch.setattr(time_profile, "_epoch_or_iso_ms", time_profile._ts_ms)


def _apply_non_finite_accepted(monkeypatch):
    monkeypatch.setattr(
        time_profile, "_finite_number",
        lambda value: not isinstance(value, bool) and isinstance(value, (int, float)),
    )


def _apply_commit_index_follows_head(monkeypatch):
    monkeypatch.setattr(time_profile, "TRUNK_REFS", ("HEAD",))


def _apply_human_origin_is_background(monkeypatch):
    monkeypatch.setattr(time_profile, "_is_non_human_origin", bool)


def _apply_half_life_ignores_config(monkeypatch):
    monkeypatch.setattr(time_profile, "half_life_days", lambda config_path=None: 7.0)


def _apply_figure_without_n_eff(monkeypatch):
    monkeypatch.setattr(time_profile, "figure", lambda mean, se, n_eff: {"mean": mean, "se": se})


MUTANTS = {
    "half_life_ignores_config": (
        "the default half-life is 7 days whatever config.md says",
        _apply_half_life_ignores_config,
    ),
    "figure_without_n_eff": (
        "every estimate is built without its n_eff member",
        _apply_figure_without_n_eff,
    ),
    "human_origin_is_background": (
        "any truthy message origin, a human's included, counts as an injected message",
        _apply_human_origin_is_background,
    ),
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
    "spawn_join_on_engine_id": (
        "spawn rows are keyed by the engine session id instead of the spawning harness session",
        _apply_spawn_join_on_engine_id,
    ),
    "node_ignores_bindings": (
        "node dwell opens only the state file named like the transcript, never a bound engine session",
        _apply_node_ignores_bindings,
    ),
    "same_ts_hook_dropped": (
        "a hook attachment stamped like the entry before it contributes nothing",
        _apply_same_ts_hook_dropped,
    ),
    "typed_message_is_background": (
        "every queued_command, a message typed mid-turn included, ends a background wait",
        _apply_typed_message_is_background,
    ),
    "inter_agent_message_is_model_time": (
        "an isMeta / origin / <...-message> queued_command ends a model:gap, not a background wait",
        _apply_inter_agent_message_is_model_time,
    ),
    "zero_share_tail_not_skipped": (
        "the same-stamp hook carve touches a zero-ms tail share that is not in the partition",
        _apply_zero_share_tail_not_skipped,
    ),
    "node_dwell_unclipped": (
        "node dwell ignores the transcript window, so every driving session reports the full span",
        _apply_node_dwell_unclipped,
    ),
    "huge_float_accepted": (
        "a finite float too large to scale by 1000 reaches round() and overflows",
        _apply_huge_float_accepted,
    ),
    "skip_detail_dropped": (
        "a session that raised is skipped without the exception type and text",
        _apply_skip_detail_dropped,
    ),
    "cache_ignores_version": (
        "a cached session figure is served after the walker's rules changed",
        _apply_cache_ignores_version,
    ),
    "node_epoch_ts_dropped": (
        "an epoch-seconds history ts is counted as untimed",
        _apply_node_epoch_ts_dropped,
    ),
    "non_finite_accepted": (
        "NaN and infinite ledger numbers reach int() instead of being skipped",
        _apply_non_finite_accepted,
    ),
    "commit_index_follows_head": (
        "the commit index is read from the checked-out HEAD, not from trunk",
        _apply_commit_index_follows_head,
    ),
}

AGGREGATE_MUTANTS = frozenset({"half_life_ignores_config", "figure_without_n_eff"})
assert AGGREGATE_MUTANTS <= set(MUTANTS)


def suite_for(mutant: str) -> str:
    """The test file that must fail under `mutant`."""
    return "test_time_profile_aggregate.py" if mutant in AGGREGATE_MUTANTS else "test_time_profile_extract.py"


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
