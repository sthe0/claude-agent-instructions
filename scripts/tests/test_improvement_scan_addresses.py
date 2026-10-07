"""A backlog item's `addresses` links it to the telemetry clusters it removes; the
report joins them at report time, so an item sits in the measured band at the cost of
the cluster it addresses instead of ranking only by report recurrence.

Loaded by path like the other improvement-scan tests (the module filename has a dash).
References to new script symbols stay inside test bodies so a pre-change script fails
on assertions, not on collection.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))


def _load_scan():
    spec = importlib.util.spec_from_file_location(
        "improvement_scan_addresses", SCRIPTS_DIR / "improvement-scan.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


scan = _load_scan()
sds = scan.sds

K1 = "aaaaaaaaaaaa"
K2 = "bbbbbbbbbbbb"
GONE = "cccccccccccc"


@pytest.fixture(autouse=True)
def _no_judge(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTCTL_ADVISOR", "0")
    monkeypatch.setenv("IMPROVEMENT_SCAN_BOARD_STATE", str(tmp_path / "board.json"))


def _cost(usd=None, measured=True, attention=None):
    return scan.CostSignal(
        usd_per_week=usd, attention_per_week=attention,
        basis="measured" if measured else "unmeasured", measured=measured,
    )


def _telemetry(signal, usd=None, measured=True):
    return scan.Finding(
        kind=sds.KIND_TELEMETRY_PATTERN, signal=signal, title=f"cluster {signal}",
        functional_ground=f"ground {signal}", evidence=("e",),
        cost_signal=_cost(usd, measured), source_ref="det",
        recommended_next_step="self-improvement",
    )


def _key(signal):
    return sds.finding_key(sds.KIND_TELEMETRY_PATTERN, signal)


def _backlog(signal, addresses=(), score=1.0, cost=None):
    return scan.Finding(
        kind=sds.KIND_BACKLOG_ITEM, signal=signal, title=f"item {signal}",
        functional_ground=f"item ground {signal}", evidence=("e",),
        cost_signal=cost or _cost(measured=False), source_ref=signal,
        recommended_next_step="planner", proxy_score=score, addresses=tuple(addresses),
    )


def _report(tmp_path, telemetry=(), backlog=()):
    store = tmp_path / "store.jsonl"
    scan.store_findings(telemetry, kinds=frozenset([sds.KIND_TELEMETRY_PATTERN]), store_path=store)
    scan.store_findings(backlog, kinds=frozenset([sds.KIND_BACKLOG_ITEM]), store_path=store)
    return scan._rank_findings(scan._report_findings(store))


def _by_signal(ranked):
    return {f["path"]: f for f in ranked}


def _order(ranked):
    return [f["path"] for f in ranked]


def _classification(**overrides):
    item = {
        "title": "t", "functional_ground": "g", "severity": "high", "source_digest": "d1",
        "breadth": "narrow", "cost_to_resolve": "small", "in_flight": "none",
        "recommended_next_step": "planner", "severity_labeled": True,
    }
    item.update(overrides)
    return item


def _phase_b(tmp_path, items):
    cls = tmp_path / "classifications.json"
    cls.write_text(json.dumps({"items": items, "closed_refs": []}), encoding="utf-8")
    args = argparse.Namespace(
        prior=None, classifications=str(cls), worklist=None, out=None,
        store=str(tmp_path / "store.jsonl"), dry_run=False,
    )
    return scan._run_backlog_phase_b(args)


def _board(tmp_path):
    return json.loads((tmp_path / "board.json").read_text(encoding="utf-8"))["items"]


# --- (a) resolved key: measured band at the cluster's cost -------------------

def test_resolved_addresses_rank_in_measured_band_via_the_cluster(tmp_path):
    ranked = _report(tmp_path, [_telemetry("t1", 100.0)], [_backlog("b1", [_key("t1")])])

    item = _by_signal(ranked)["b1"]
    assert item["cost_signal"]["measured"] and item["cost_signal"]["usd_per_week"] == 100.0
    assert item["via"] == _key("t1") and item["dangling"] == []
    assert f"via {_key('t1')}" in scan._render_markdown(ranked)


# --- (b) shared cluster: ordered after the cluster, by proxy, cost not summed ----

def test_items_sharing_a_cluster_follow_it_by_proxy_score_without_summing(tmp_path):
    ranked = _report(
        tmp_path,
        [_telemetry("t1", 100.0), _telemetry("t2", 50.0)],
        [_backlog("low", [_key("t1")], score=1.0), _backlog("high", [_key("t1")], score=9.0)],
    )

    assert _order(ranked) == ["t1", "high", "low", "t2"]
    by = _by_signal(ranked)
    assert by["high"]["cost_signal"]["usd_per_week"] == by["low"]["cost_signal"]["usd_per_week"] == 100.0
    assert _order(ranked).count("t1") == 1


# --- (c)/(d)/(e) dangling and unmeasured resolution ---------------------------

def test_one_resolved_and_one_dangling_key_ranks_measured_and_lists_dangling(tmp_path):
    ranked = _report(
        tmp_path, [_telemetry("t1", 100.0)], [_backlog("b1", [_key("t1"), GONE])]
    )

    item = _by_signal(ranked)["b1"]
    assert item["cost_signal"]["measured"] and item["dangling"] == [GONE]
    assert GONE in scan._render_markdown(ranked)


def test_all_unresolved_keys_stay_unmeasured_and_dangling(tmp_path):
    ranked = _report(tmp_path, [_telemetry("t1", 100.0)], [_backlog("b1", [GONE])])

    item = _by_signal(ranked)["b1"]
    assert not item["cost_signal"]["measured"] and item["dangling"] == [GONE]
    assert "via" not in item


def test_key_matching_only_an_unmeasured_row_is_unmeasured_not_dangling(tmp_path):
    ranked = _report(
        tmp_path, [_telemetry("t1", measured=False)], [_backlog("b1", [_key("t1")])]
    )

    item = _by_signal(ranked)["b1"]
    assert not item["cost_signal"]["measured"] and item["dangling"] == []


# --- (f) malformed addresses exit 2 at phase B --------------------------------

@pytest.mark.parametrize("bad", ["aaaaaaaaaaaa", ["not-hex-12345"], ["aaaaaaaaaaa"], ["AAAAAAAAAAAA"], [1]])
def test_malformed_addresses_exit_2_naming_the_ref_and_write_nothing(tmp_path, capsys, bad):
    rc = _phase_b(tmp_path, {"ref-1": _classification(addresses=bad)})

    assert rc == 2
    assert "ref-1" in capsys.readouterr().err
    assert not (tmp_path / "board.json").exists()
    assert not (tmp_path / "store.jsonl").exists()


# --- (g) carried item keeps addresses and follows the cluster's current cost ----

def test_carried_item_keeps_addresses_and_reflects_the_cluster_cost_change(tmp_path):
    key = _key("t1")
    assert _phase_b(tmp_path, {"ref-1": _classification(addresses=[key])}) == 0
    assert _board(tmp_path)["ref-1"]["addresses"] == [key]

    prior = scan.load_prior_board(tmp_path / "board.json")
    board, findings, _ = scan.classify_and_score(prior, {}, [])
    assert board.items["ref-1"].addresses == (key,) and findings[0].addresses == (key,)

    store = tmp_path / "store.jsonl"
    cluster = lambda usd: scan.Finding(  # noqa: E731
        kind=sds.KIND_TELEMETRY_PATTERN, signal="t1", title="c", functional_ground="g",
        evidence=(), cost_signal=_cost(usd), source_ref="d", recommended_next_step="self-improvement",
    )
    for usd in (10.0, 80.0):
        scan.store_findings([cluster(usd)], kinds=frozenset([sds.KIND_TELEMETRY_PATTERN]), store_path=store)
        scan.store_findings(findings, kinds=frozenset([sds.KIND_BACKLOG_ITEM]), store_path=store)
        item = _by_signal(scan._rank_findings(scan._report_findings(store)))["ref-1"]
        assert item["cost_signal"]["usd_per_week"] == usd
        assert item["via"] == key and item["dangling"] == []


# --- (h)/(l) addresses-only amendments -----------------------------------------

def test_addresses_only_classification_amends_an_item_already_on_the_board(tmp_path):
    assert _phase_b(tmp_path, {"ref-1": _classification()}) == 0
    before = _board(tmp_path)["ref-1"]

    assert _phase_b(tmp_path, {"ref-1": {"addresses": [K1]}}) == 0
    after = _board(tmp_path)["ref-1"]
    assert after["addresses"] == [K1]
    assert {k: v for k, v in after.items() if k != "addresses"} == {
        k: v for k, v in before.items() if k != "addresses"
    }


def test_amendment_with_an_extra_field_or_unknown_ref_exits_2(tmp_path, capsys):
    assert _phase_b(tmp_path, {"ref-1": _classification()}) == 0

    assert _phase_b(tmp_path, {"ref-1": {"addresses": [K1], "title": "x"}}) == 2
    assert "ref-1" in capsys.readouterr().err
    assert _phase_b(tmp_path, {"ghost": {"addresses": [K1]}}) == 2
    assert "ghost" in capsys.readouterr().err


def test_amendment_replaces_existing_addresses_and_empty_list_clears(tmp_path):
    assert _phase_b(tmp_path, {"ref-1": _classification(addresses=[K1])}) == 0

    assert _phase_b(tmp_path, {"ref-1": {"addresses": [K2]}}) == 0
    assert _board(tmp_path)["ref-1"]["addresses"] == [K2]
    assert _phase_b(tmp_path, {"ref-1": {"addresses": []}}) == 0
    assert _board(tmp_path)["ref-1"]["addresses"] == []


# --- (i) absent addresses leaves ranking identical ------------------------------

def test_absent_addresses_leave_the_existing_ranking_unchanged():
    fixture = Path(__file__).resolve().parent / "fixtures" / "improvement_scan_mixed_store.jsonl"
    findings = scan._report_findings(fixture)

    assert all("addresses" not in f for f in findings)
    assert scan._join_addressed_clusters(findings) == findings
    ranked = scan._rank_findings(findings)
    # Order frozen from the pre-change script's output on this fixture.
    assert [f["source_ref"] for f in ranked] == [
        "cost-concentration/sess-a", "core-issue-201", "core-issue-207", "delegation-misses/proj-x",
    ]
    assert not any("via" in f or "dangling" in f for f in ranked)


# --- (j) several measured keys: the highest wins ---------------------------------

def test_two_measured_keys_rank_at_the_higher_cost_via_that_key(tmp_path):
    ranked = _report(
        tmp_path,
        [_telemetry("t1", 10.0), _telemetry("t2", 70.0)],
        [_backlog("b1", [_key("t1"), _key("t2")])],
    )

    item = _by_signal(ranked)["b1"]
    assert item["cost_signal"]["usd_per_week"] == 70.0 and item["via"] == _key("t2")


# --- (k) equal cost: the cluster row first ---------------------------------------

def test_at_equal_cost_the_cluster_row_precedes_the_item_via_it(tmp_path):
    ranked = _report(
        tmp_path,
        [_telemetry("t1", 40.0)],
        [_backlog("b1", [_key("t1")], score=99.0)],
    )

    assert _order(ranked).index("t1") < _order(ranked).index("b1")


# --- (m) the item's own larger measured cost is kept -----------------------------

def test_item_with_larger_own_measured_cost_keeps_it_without_a_via_note(tmp_path):
    ranked = _report(
        tmp_path,
        [_telemetry("t1", 10.0)],
        [_backlog("b1", [_key("t1")], cost=_cost(500.0))],
    )

    item = _by_signal(ranked)["b1"]
    assert item["cost_signal"]["usd_per_week"] == 500.0 and "via" not in item
    assert _order(ranked) == ["b1", "t1"]
    assert "via " not in scan._render_markdown(ranked)


# --- reclassified items keep their addresses --------------------------------------

def _changed_worklist_phase_b(tmp_path, classification):
    assert _phase_b(tmp_path, {"ref-1": _classification(addresses=[K1])}) == 0
    prior = scan.load_prior_board(tmp_path / "board.json")
    record = scan.DifficultyRecord(
        ts="2026-10-08T00:00:00Z", layer="core", target="t", functional_ground="g",
        severity=scan.Severity.MEDIUM, reporter="agent", evidence="ev",
        cost_estimate="not estimable: n/a", ref="ref-1",
    )
    worklist = scan.build_worklist([], [("ref-1", record)], [], [], prior=prior)
    assert worklist["items"][0]["addresses"] == [K1]
    wl, cls = tmp_path / "worklist.json", tmp_path / "classifications.json"
    wl.write_text(json.dumps(worklist), encoding="utf-8")
    cls.write_text(json.dumps({"items": {"ref-1": classification}}), encoding="utf-8")
    args = argparse.Namespace(
        prior=None, classifications=str(cls), worklist=str(wl), out=None,
        store=str(tmp_path / "store.jsonl"), dry_run=False,
    )
    assert scan._run_backlog_phase_b(args) == 0
    return _board(tmp_path)["ref-1"]["addresses"]


@pytest.mark.parametrize(
    "overrides, expected",
    [({}, [K1]), ({"addresses": [K2]}, [K2]), ({"addresses": []}, [])],
    ids=["omitted-keeps", "supplied-replaces", "empty-clears"],
)
def test_changed_item_arrives_with_prior_addresses(tmp_path, overrides, expected):
    classification = _classification(**overrides)
    assert _changed_worklist_phase_b(tmp_path, classification) == expected


# --- (n) a resolved telemetry row is not open -------------------------------------

def test_key_whose_telemetry_row_is_resolved_is_dangling(tmp_path):
    store = tmp_path / "store.jsonl"
    kinds = frozenset([sds.KIND_TELEMETRY_PATTERN])
    scan.store_findings([_telemetry("t1", 100.0)], kinds=kinds, store_path=store)
    scan.store_findings([_telemetry("t2", 5.0)], kinds=kinds, store_path=store)
    scan.store_findings(
        [_backlog("b1", [_key("t1")])],
        kinds=frozenset([sds.KIND_BACKLOG_ITEM]), store_path=store,
    )

    item = _by_signal(scan._rank_findings(scan._report_findings(store)))["b1"]
    assert item["dangling"] == [_key("t1")] and "via" not in item
    assert not item["cost_signal"]["measured"]


# --- (o) an unranked item produces no Finding, so it is not joined ---------------

def test_unranked_item_with_addresses_yields_no_finding():
    board, findings, no_urgency = scan.classify_and_score(
        scan._empty_board(),
        {"ref-1": _classification(severity_labeled=False, addresses=[K1])},
        [],
    )

    assert findings == [] and no_urgency == ["ref-1"]
    assert board.items["ref-1"].addresses == (K1,)


# --- tie-break: equal cost and proxy score order by source_ref --------------------

def test_equal_cost_and_proxy_score_order_by_source_ref(tmp_path):
    ranked = _report(
        tmp_path,
        [_telemetry("t1", 40.0)],
        [_backlog("b-zeta", [_key("t1")]), _backlog("b-alpha", [_key("t1")])],
    )

    assert _order(ranked) == ["t1", "b-alpha", "b-zeta"]
