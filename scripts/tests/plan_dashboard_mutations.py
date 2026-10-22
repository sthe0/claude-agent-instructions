"""Pytest plugin that installs one named mutation on the plan-dashboard data layer.

Load it with ``-p plan_dashboard_mutations`` and name the mutation in
``PLAN_DASHBOARD_MUTATION``; unset or empty installs none. Each mutation wraps a public
function of the module and breaks one property the tests are meant to pin, so a mutation
that leaves the suite green is a test gap. Later dashboard stages add rows to MUTATIONS.

``test_plan_dashboard_mutation_catalogue.py`` runs every row in a subprocess and asserts the
model tests go red; to add a mutation, add a row here and that test covers it. By hand:
``PLAN_DASHBOARD_MUTATION=<name> PYTHONPATH=scripts/tests:scripts python3 -m pytest -q -rf
-p no:cacheprovider -p plan_dashboard_mutations scripts/tests/test_plan_dashboard_model.py``.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import importlib
import json
import os
from pathlib import Path

import pytest

ENV = "PLAN_DASHBOARD_MUTATION"
MODEL = "plan_dashboard.model"
applied: str | None = None


def _walk(items, visit):
    for item in items:
        visit(item)
        _walk(item.get("children", []), visit)
    return items


def _with_kwargs(**changes):
    def wrap(original):
        def mutated(*args, **kwargs):
            return original(*args, **{**kwargs, **changes})
        return mutated
    return wrap


def _collect_then(effect):
    def wrap(original):
        def mutated(state_root, *args, **kwargs):
            result = original(state_root, *args, **kwargs)
            effect(Path(state_root))
            return result
        return mutated
    return wrap


def _drops_unreadable(original):
    def mutated(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        snapshot["tasks"] = [t for t in snapshot["tasks"] if t.get("readable", True)]
        return snapshot
    return mutated


def _touch_state_file(root: Path) -> None:
    (root / ".touched").write_text("x", encoding="utf-8")


def _lock_state_dir(root: Path) -> None:
    fd = os.open(root, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
    finally:
        os.close(fd)


def _detail_writes(original):
    def mutated(state_root, *args, **kwargs):
        result = original(state_root, *args, **kwargs)
        (Path(state_root) / ".detail-touched").write_text("x", encoding="utf-8")
        return result
    return mutated


def _attribution_by_time(original):
    def mutated(row, *args, **kwargs):
        return original({**row, "stage_index": None, "review_pair": None}, *args, **kwargs)
    return mutated


def _estimate_unflagged(original):
    def mutated(*args, **kwargs):
        def clear(item):
            item["estimate"] = {key: False for key in item["estimate"]}
        return _walk(original(*args, **kwargs), clear)
    return mutated


def _root_missing_zero(original):
    def mutated(*args, **kwargs):
        def zero(item):
            if item.get("root_cost_usd") is None:
                item["root_cost_usd"] = 0
        return _walk(original(*args, **kwargs), zero)
    return mutated


def _transcript_reread(original):
    def mutated(*args, **kwargs):
        importlib.import_module(MODEL).clear_cache()
        return original(*args, **kwargs)
    return mutated


def _open_window_now(original):
    def mutated(*args, **kwargs):
        now = dt.datetime.now(dt.timezone.utc)

        def close(item):
            if item.get("start") and item.get("end") is None:
                item["end"] = now.isoformat(timespec="seconds")
                start = dt.datetime.fromisoformat(item["start"])
                item["wall_clock_s"] = (now - start).total_seconds()
        return _walk(original(*args, **kwargs), close)
    return mutated


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def locked(self):
        return False


def _cache_unlocked(original):
    def mutated(*args, **kwargs):
        model = importlib.import_module(MODEL)
        real = model.CACHE_LOCK
        model.CACHE_LOCK = _NoLock()
        try:
            return original(*args, **kwargs)
        finally:
            model.CACHE_LOCK = real
    return mutated


def _single_read(original):
    return _one_read


def _one_read(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return (data, None) if isinstance(data, dict) else (None, "not a JSON object")


def _retry_without_pause(original):
    def mutated(*args, **kwargs):
        model = importlib.import_module(MODEL)
        real = model.RETRY_PAUSE_S
        model.RETRY_PAUSE_S = 0
        try:
            return original(*args, **kwargs)
        finally:
            model.RETRY_PAUSE_S = real
    return mutated


def _sidecar_kept(original):
    def mutated(*args, **kwargs):
        return False
    return mutated


def _foreign_session_rows(original):
    def mutated(ledger, session_id, plan_path):
        own = original(ledger, session_id, plan_path)
        return own + [r for owner, rows in ledger["by_session"].items() if owner != session_id for r in rows]
    return mutated


def _foreign_plan_rows(original):
    def mutated(ledger, session_id, plan_path):
        own = original(ledger, session_id, plan_path)
        return own + [r for plan, rows in ledger["by_plan"].items() if plan != plan_path for r in rows]
    return mutated


def _root_before_first_kept(original):
    def mutated(windows, usage_rows, events, resolved):
        earliest = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        return original(windows, usage_rows, [(earliest, {})] + list(events), resolved)
    return mutated


def _root_after_resolve_kept(original):
    def mutated(windows, usage_rows, events, resolved):
        return original(windows, usage_rows, events, False)
    return mutated


def _nested_spawn_zero(original):
    def mutated(*args, **kwargs):
        def zero(item):
            for child in item["children"]:
                child["spawn_cost_usd"] = 0
                child["spawn_count"] = 0
        return _walk(original(*args, **kwargs), zero)
    return mutated


MUTATIONS = {
    "model-ignores-window": (MODEL, "collect", _with_kwargs(window_hours=10**7)),
    "model-ignores-selection": (MODEL, "collect", _with_kwargs(include_all=True)),
    "model-writes-state": (MODEL, "collect", _collect_then(_touch_state_file)),
    "model-locks": (MODEL, "collect", _collect_then(_lock_state_dir)),
    "model-drops-unreadable": (MODEL, "collect", _drops_unreadable),
    "model-detail-writes": (MODEL, "detail", _detail_writes),
    "model-attribution-by-time": (MODEL, "attribute_spawn", _attribution_by_time),
    "model-estimate-unflagged": (MODEL, "phases", _estimate_unflagged),
    "model-root-missing-zero": (MODEL, "phases", _root_missing_zero),
    "model-transcript-reread": (MODEL, "transcript_usage", _transcript_reread),
    "model-open-window-now": (MODEL, "phases", _open_window_now),
    "model-cache-unlocked": (MODEL, "detail", _cache_unlocked),
    "model-retry-single-read": (MODEL, "_read_state", _single_read),
    "model-retry-without-pause": (MODEL, "_read_state", _retry_without_pause),
    "model-sidecar-kept": (MODEL, "_is_sidecar", _sidecar_kept),
    "model-ledger-foreign-session": (MODEL, "_owned_spawn_rows", _foreign_session_rows),
    "model-ledger-foreign-plan": (MODEL, "_owned_spawn_rows", _foreign_plan_rows),
    "model-root-before-first-kept": (MODEL, "_slice_root", _root_before_first_kept),
    "model-root-after-resolve-kept": (MODEL, "_slice_root", _root_after_resolve_kept),
    "model-child-reads-parent-transcript": (MODEL, "_transcript_paths", _with_kwargs(linked_child=False)),
    "model-nested-spawn-zero": (MODEL, "phases", _nested_spawn_zero),
}


def pytest_addoption(parser):
    parser.addoption("--plan-dashboard-mutation", default="", help=f"same as {ENV}; the variable wins")


def pytest_configure(config):
    global applied
    name = os.environ.get(ENV) or config.getoption("--plan-dashboard-mutation")
    if not name:
        return
    if name not in MUTATIONS:
        raise pytest.UsageError(f"{ENV}={name!r} is not a known mutation; known: {sorted(MUTATIONS)}")
    module_name, attr, wrapper = MUTATIONS[name]
    module = importlib.import_module(module_name)
    setattr(module, attr, wrapper(getattr(module, attr)))
    applied = name


def pytest_terminal_summary(terminalreporter):
    terminalreporter.write_line(f"plan-dashboard mutation: {applied + ' applied' if applied else 'none'}")
