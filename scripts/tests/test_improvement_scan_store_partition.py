"""Resolve-out of the improvement-scan store is partitioned by kind, so the
backlog producer and the telemetry producer — which share one source string —
never retire each other's rows."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import self_diagnose_store as sds  # noqa: E402


def _load_scan():
    spec = importlib.util.spec_from_file_location("improvement_scan", SCRIPTS_DIR / "improvement-scan.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


scan = _load_scan()

BACKLOG = frozenset([sds.KIND_BACKLOG_ITEM])
TELEMETRY = frozenset([sds.KIND_TELEMETRY_PATTERN])


def _finding(kind, signal):
    return scan.Finding(
        kind=kind, signal=signal, title="t", functional_ground="g", evidence=("e",),
        cost_signal=scan.CostSignal(), source_ref="r", recommended_next_step="planner",
    )


def _kinds(store):
    return sorted(r["kind"] for r in sds.load_rows(store))


def test_telemetry_store_keeps_backlog_rows(tmp_path):
    store = tmp_path / "store.json"
    scan.store_findings([_finding(sds.KIND_BACKLOG_ITEM, "b1")], kinds=BACKLOG, store_path=store)
    scan.store_findings([_finding(sds.KIND_TELEMETRY_PATTERN, "t1")], kinds=TELEMETRY, store_path=store)
    assert _kinds(store) == [sds.KIND_BACKLOG_ITEM, sds.KIND_TELEMETRY_PATTERN]


def test_backlog_store_keeps_telemetry_rows(tmp_path):
    store = tmp_path / "store.json"
    scan.store_findings([_finding(sds.KIND_TELEMETRY_PATTERN, "t1")], kinds=TELEMETRY, store_path=store)
    scan.store_findings([_finding(sds.KIND_BACKLOG_ITEM, "b1")], kinds=BACKLOG, store_path=store)
    assert _kinds(store) == [sds.KIND_BACKLOG_ITEM, sds.KIND_TELEMETRY_PATTERN]


def test_own_kind_stale_row_is_still_resolved_out(tmp_path):
    store = tmp_path / "store.json"
    scan.store_findings([_finding(sds.KIND_BACKLOG_ITEM, "b1")], kinds=BACKLOG, store_path=store)
    scan.store_findings([_finding(sds.KIND_BACKLOG_ITEM, "b2")], kinds=BACKLOG, store_path=store)
    assert [r["path"] for r in sds.load_rows(store)] == ["b2"]


def test_source_only_caller_resolves_out_all_its_source_rows(tmp_path):
    store = tmp_path / "store.json"
    for kind, path in ((sds.KIND_BACKLOG_ITEM, "b1"), (sds.KIND_TELEMETRY_PATTERN, "t1")):
        sds.upsert_findings(
            [dict(kind=kind, path=path, detail="{}")],
            path=store, source=sds.SOURCE_IMPROVEMENT_SCAN,
        )
    assert [r["path"] for r in sds.load_rows(store)] == ["t1"]
