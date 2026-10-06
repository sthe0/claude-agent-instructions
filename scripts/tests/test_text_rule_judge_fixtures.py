"""Hermetic checks on the text-rule judge calibration material (samples/text-rule-judge/).

The live run needs a model; these tests pin everything that does not: the labelled
set is well-formed, the lexical prefilter nominates a rule for every violation item
(recall 1.0 -- a violation the prefilter misses is never shown to the judge), and
check_calibration.py goes red on a calibration that should not be accepted.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SAMPLES = SCRIPTS_DIR.parent / "samples" / "text-rule-judge"
sys.path.insert(0, str(SCRIPTS_DIR))
from lib import writer_rules  # noqa: E402


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SAMPLES / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


checker = _load("check_calibration")
ITEMS = [json.loads(ln) for ln in (SAMPLES / "labelled.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]


def _run(verdict: bool, rules=("say-13",), genuine: bool = True, latency: float = 20.0) -> dict:
    return {"verdict": verdict, "genuine": genuine, "judged_rules": list(rules) if verdict else [],
            "latency_s": latency, "failed_attempts": []}


def _good_calibration() -> dict:
    items = []
    for item in ITEMS:
        violation = item["label"] == "violation"
        rules = item["label_rules"][:1]
        items.append({"id": item["id"], "label": item["label"], "label_rules": item["label_rules"],
                      "runs": [_run(violation, rules), _run(violation, rules)]})
    n = len(items)
    return {"n": n, "runs_per_item": 2, "items": items, "median_s": 20.0, "p90_s": 20.0, "max_s": 20.0,
            "flips": 0, "prefilter": {"publications": 10, "fired": 3, "rate": 0.3}}


GOOD_E2E = {"positive": {"decision": "deny", "reason": "- say-13 (...)", "returncode": 0, "latency_s": 25.0},
            "negative": {"decision": "allow", "reason": "", "returncode": 0, "latency_s": 20.0}}


def test_labelled_set_is_well_formed():
    ids = [item["id"] for item in ITEMS]
    assert len(ids) == len(set(ids)) and len(ITEMS) >= 16
    assert {"incident-2026-09-17", "incident-2026-10-05"} <= set(ids)
    assert {item["label"] for item in ITEMS} == {"violation", "clean"}
    for item in ITEMS:
        assert set(item) == {"id", "label", "label_rules", "body"}
        assert item["body"].strip()
        assert (item["label"] == "violation") == bool(item["label_rules"])


def test_prefilter_nominates_every_violation_and_every_clean_item():
    for item in ITEMS:
        assert writer_rules.find_candidates(item["body"]), item["id"]


def test_violation_labels_are_among_the_prefilter_candidates():
    for item in ITEMS:
        if item["label"] == "violation":
            fired = {rule_id for rule_id, _ in writer_rules.find_candidates(item["body"])}
            assert fired & set(item["label_rules"]), item["id"]


def test_good_calibration_passes():
    assert checker.check(_good_calibration(), GOOD_E2E) == []


def test_incident_verdict_no_is_rejected():
    calibration = _good_calibration()
    for item in calibration["items"]:
        if item["id"] == "incident-2026-10-05":
            for run in item["runs"]:
                run["verdict"], run["judged_rules"] = False, []
    assert any("incident-2026-10-05" in f for f in checker.check(calibration, GOOD_E2E))


def test_genuine_yes_on_clean_item_is_rejected():
    calibration = _good_calibration()
    clean = next(i for i in calibration["items"] if i["label"] == "clean")
    clean["runs"][0].update(verdict=True, genuine=True, judged_rules=["say-13"])
    assert any("false denies" in f for f in checker.check(calibration, GOOD_E2E))


def test_fail_open_yes_on_clean_item_is_not_a_false_deny():
    calibration = _good_calibration()
    clean = next(i for i in calibration["items"] if i["label"] == "clean")
    clean["runs"][0].update(verdict=True, genuine=False)
    assert not any("false denies" in f for f in checker.check(calibration, GOOD_E2E))


def test_non_bool_verdict_is_rejected():
    calibration = _good_calibration()
    calibration["items"][0]["runs"][0]["verdict"] = "true"
    assert any("not a JSON bool" in f for f in checker.check(calibration, GOOD_E2E))


def test_summary_field_that_disagrees_with_runs_is_rejected():
    calibration = _good_calibration()
    calibration["max_s"] = 1.0
    assert any("max_s" in f for f in checker.check(calibration, GOOD_E2E))


def test_latency_over_budget_and_small_n_are_rejected():
    calibration = _good_calibration()
    calibration["items"][0]["runs"][0]["latency_s"] = 190.0
    assert any("max latency" in f for f in checker.check(calibration, GOOD_E2E))
    small = copy.deepcopy(_good_calibration())
    small["items"] = small["items"][:5]
    assert any("labelled items" in f for f in checker.check(small, GOOD_E2E))


def test_hook_e2e_decisions_are_checked():
    allow_positive = copy.deepcopy(GOOD_E2E)
    allow_positive["positive"]["decision"] = "allow"
    deny_negative = copy.deepcopy(GOOD_E2E)
    deny_negative["negative"]["decision"] = "deny"
    assert any("positive" in f for f in checker.check(_good_calibration(), allow_positive))
    assert any("negative" in f for f in checker.check(_good_calibration(), deny_negative))
