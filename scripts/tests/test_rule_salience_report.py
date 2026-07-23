"""Tests for scripts/rule-salience-report.py.

Covers: the --check-registry two-directional drift gate (deleted entry,
edited/stale locator), the three-way OBSERVED/NEVER-OBSERVED/TRIGGER-ABSENT/
UNINSTRUMENTED state distinction (never collapsed), and determinism of the
ranked report over a fixed fixture transcript."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "rule_salience_report", SCRIPTS_DIR / "rule-salience-report.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rsr = _load_module()


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

CLAUDE_MD_TEXT = """# Title

## Section One

### Sub A

Rule A body with a **bold phrase** inside it, plain text after.

### Sub B

Rule B body text.
"""


def base_rules():
    return [
        {
            "id": "rule-a",
            "tier": 0,
            "locator_heading": "### Sub A",
            "locator_phrase": "bold phrase",
            "kernel_reason": "FRAME",
            "delivery_kind": "",
            "delivery_marker": "",
        },
        {
            "id": "rule-b",
            "tier": 1,
            "locator_heading": "### Sub B",
            "locator_phrase": "Rule B body text",
            "kernel_reason": "",
            "delivery_kind": "bracket_tag",
            "delivery_marker": "[my-tag]",
        },
    ]


def write_transcript(path: Path, lines: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def transcript_line(ts: str, text: str) -> dict:
    return {
        "timestamp": ts,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


# ---------------------------------------------------------------------------
# --check-registry drift gate
# ---------------------------------------------------------------------------

def test_check_registry_passes_on_consistent_fixture():
    errors = rsr.check_registry(base_rules(), CLAUDE_MD_TEXT)
    assert errors == []


def test_check_registry_fails_when_entry_deleted():
    """Direction A: deleting the registry entry for a rule-bearing heading
    (Sub B) leaves that heading's body text unregistered."""
    rules = [r for r in base_rules() if r["id"] != "rule-b"]
    errors = rsr.check_registry(rules, CLAUDE_MD_TEXT)
    assert any("### Sub B" in e for e in errors)


def test_check_registry_fails_when_locator_phrase_edited_to_absent_text():
    """Direction B: a locator_phrase edited to text no longer present in
    CLAUDE.md must be flagged, not silently accepted."""
    rules = base_rules()
    rules[0]["locator_phrase"] = "this text does not exist anywhere in the doc"
    errors = rsr.check_registry(rules, CLAUDE_MD_TEXT)
    assert any("rule-a" in e and "locator_phrase" in e for e in errors)


def test_check_registry_fails_when_locator_heading_edited_to_absent_text():
    rules = base_rules()
    rules[0]["locator_heading"] = "### Sub Z (renamed, no longer exists)"
    errors = rsr.check_registry(rules, CLAUDE_MD_TEXT)
    assert any("rule-a" in e and "locator_heading" in e for e in errors)


def test_check_registry_flags_schema_violations():
    rules = base_rules()
    rules[0]["kernel_reason"] = "MAYBE"  # not FRAME/NOT-NOTICING
    errors = rsr.check_registry(rules, CLAUDE_MD_TEXT)
    assert any("kernel_reason" in e for e in errors)


def test_check_registry_cli_exit_codes(tmp_path):
    good_registry = tmp_path / "good.toml"
    _write_toml(good_registry, base_rules())
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text(CLAUDE_MD_TEXT)

    assert rsr.main(["--check-registry", "--registry", str(good_registry), "--claude-md", str(claude_md)]) == 0

    bad_rules = base_rules()
    bad_rules[0]["locator_phrase"] = "absent text"
    bad_registry = tmp_path / "bad.toml"
    _write_toml(bad_registry, bad_rules)
    assert rsr.main(["--check-registry", "--registry", str(bad_registry), "--claude-md", str(claude_md)]) == 1


def _write_toml(path: Path, rules: list[dict]) -> None:
    lines = []
    for r in rules:
        lines.append("[[rule]]")
        for k, v in r.items():
            if isinstance(v, str):
                escaped = v.replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'{k} = "{escaped}"')
            else:
                lines.append(f"{k} = {v}")
        lines.append("")
    path.write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Three-state distinction — must never collapse
# ---------------------------------------------------------------------------

def test_classify_state_observed_when_fired():
    rule = base_rules()[1]
    state, reason = rsr.classify_state(rule, sessions_scanned=5, fired=True)
    assert state == "OBSERVED"


def test_classify_state_never_observed_default_no_proxy():
    rule = base_rules()[1]
    state, reason = rsr.classify_state(rule, sessions_scanned=5, fired=False, trigger_status=None)
    assert state == "NEVER-OBSERVED"
    assert "marker never appeared" in reason


def test_classify_state_trigger_absent_when_proxy_says_no():
    rule = base_rules()[1]
    state, reason = rsr.classify_state(rule, sessions_scanned=5, fired=False, trigger_status=False)
    assert state == "TRIGGER-ABSENT"
    assert "trigger condition itself did not occur" in reason


def test_classify_state_never_observed_when_trigger_occurred_but_silent():
    """Distinct from the no-proxy NEVER-OBSERVED: here we have positive
    evidence the trigger DID happen, making the reason more actionable, but
    the state name is intentionally the same NEVER-OBSERVED bucket (not
    TRIGGER-ABSENT, which asserts the opposite)."""
    rule = base_rules()[1]
    state, reason = rsr.classify_state(rule, sessions_scanned=5, fired=False, trigger_status=True)
    assert state == "NEVER-OBSERVED"
    assert "trigger DID occur" in reason


def test_classify_state_uninstrumented_tier0():
    rule = base_rules()[0]
    state, reason = rsr.classify_state(rule, sessions_scanned=5, fired=False)
    assert state == "UNINSTRUMENTED"
    assert "tier-0" in reason


def test_classify_state_uninstrumented_structured_hook():
    rule = {"id": "x", "tier": 1, "delivery_kind": "structured_hook"}
    state, reason = rsr.classify_state(rule, sessions_scanned=5, fired=False)
    assert state == "UNINSTRUMENTED"
    assert "no bracket tag" in reason


def test_classify_state_uninstrumented_harness():
    rule = {"id": "x", "tier": 1, "delivery_kind": "harness"}
    state, reason = rsr.classify_state(rule, sessions_scanned=5, fired=False)
    assert state == "UNINSTRUMENTED"
    assert "harness-level" in reason


def test_classify_state_uninstrumented_no_transcripts():
    rule = base_rules()[1]
    state, reason = rsr.classify_state(rule, sessions_scanned=0, fired=False)
    assert state == "UNINSTRUMENTED"
    assert "no transcripts" in reason


def test_three_states_are_pairwise_distinct_identifiers():
    """Direct proof the three 'unobserved' outcomes never collapse into one
    string: NEVER-OBSERVED, TRIGGER-ABSENT, UNINSTRUMENTED are three distinct
    values, and OBSERVED is a fourth."""
    rule = base_rules()[1]
    tier0_rule = base_rules()[0]
    states = {
        rsr.classify_state(rule, 5, fired=True)[0],
        rsr.classify_state(rule, 5, fired=False, trigger_status=None)[0],
        rsr.classify_state(rule, 5, fired=False, trigger_status=False)[0],
        rsr.classify_state(tier0_rule, 5, fired=False)[0],
    }
    assert states == {"OBSERVED", "NEVER-OBSERVED", "TRIGGER-ABSENT", "UNINSTRUMENTED"}


def test_eval_trigger_proxy_true_false_none():
    rows = [{"attention": {"corrections": 2}}, {"attention": {"corrections": 0}}]
    assert rsr.eval_trigger_proxy(rows, "attention.corrections", ">", 0) is True
    assert rsr.eval_trigger_proxy([{"attention": {"corrections": 0}}], "attention.corrections", ">", 0) is False
    assert rsr.eval_trigger_proxy(None, "attention.corrections", ">", 0) is None
    assert rsr.eval_trigger_proxy(rows, "", "", None) is None


# ---------------------------------------------------------------------------
# Fixture-transcript firing scan + end-to-end report determinism
# ---------------------------------------------------------------------------

@pytest.fixture
def fixture_transcript(tmp_path):
    jsonl = tmp_path / "session.jsonl"
    write_transcript(
        jsonl,
        [
            transcript_line("2026-06-01T00:00:00Z", "some preamble"),
            transcript_line("2026-06-01T00:01:00Z", "hook fired: [my-tag] please read this"),
            transcript_line("2026-06-01T00:02:00Z", "unrelated text [my-tag] again"),
        ],
    )
    return jsonl


def test_scan_transcripts_counts_marker_firings(fixture_transcript):
    rules = base_rules()
    sessions_scanned, firing_counts, sessions_with_firing = rsr.scan_transcripts(
        rules, cutoff=None, transcripts=[fixture_transcript]
    )
    assert sessions_scanned == 1
    assert firing_counts["[my-tag]"] == 2
    assert sessions_with_firing["[my-tag]"] == 1


def test_scan_transcripts_zero_when_marker_absent(tmp_path):
    jsonl = tmp_path / "session.jsonl"
    write_transcript(jsonl, [transcript_line("2026-06-01T00:00:00Z", "nothing relevant here")])
    rules = base_rules()
    sessions_scanned, firing_counts, sessions_with_firing = rsr.scan_transcripts(
        rules, cutoff=None, transcripts=[jsonl]
    )
    assert sessions_scanned == 1
    assert firing_counts.get("[my-tag]", 0) == 0


def test_build_report_rows_reaches_trigger_absent_via_real_ledger_rows(tmp_path):
    """End-to-end proof that TRIGGER-ABSENT is reachable through the actual
    build_report_rows() wiring (eval_trigger_proxy fed real ledger rows), not
    only via a hand-crafted classify_state() call. rule-b's marker never
    fires (no transcript hit) AND a synthetic ledger of two rows both fails
    its coded trigger_proxy predicate, so it must land in TRIGGER-ABSENT
    rather than the unrefined NEVER-OBSERVED bucket."""
    rule_with_proxy = dict(base_rules()[1])
    rule_with_proxy["trigger_proxy_field"] = "attention.corrections"
    rule_with_proxy["trigger_proxy_op"] = ">"
    rule_with_proxy["trigger_proxy_value"] = 0
    ledger_rows = [
        {"session_id": "s1", "attention": {"corrections": 0}},
        {"session_id": "s2", "attention": {"corrections": 0}},
    ]

    rows = rsr.build_report_rows(
        [rule_with_proxy],
        sessions_scanned=2,
        firing_counts={},
        sessions_with_firing={},
        trigger_ledger_rows=ledger_rows,
    )

    assert rows[0]["state"] == "TRIGGER-ABSENT"


def test_build_report_rows_end_to_end(fixture_transcript):
    rules = base_rules()
    sessions_scanned, firing_counts, sessions_with_firing = rsr.scan_transcripts(
        rules, cutoff=None, transcripts=[fixture_transcript]
    )
    rows = rsr.build_report_rows(rules, sessions_scanned, firing_counts, sessions_with_firing)
    by_id = {r["id"]: r for r in rows}
    assert by_id["rule-b"]["state"] == "OBSERVED"
    assert by_id["rule-b"]["firing_count"] == 2
    assert by_id["rule-a"]["state"] == "UNINSTRUMENTED"


def test_report_is_deterministic(fixture_transcript):
    rules = base_rules()
    results = []
    for _ in range(3):
        sessions_scanned, firing_counts, sessions_with_firing = rsr.scan_transcripts(
            rules, cutoff=None, transcripts=[fixture_transcript]
        )
        rows = rsr.build_report_rows(rules, sessions_scanned, firing_counts, sessions_with_firing)
        results.append(rsr.render_report(rows, sessions_scanned, denom_source="test"))
    assert results[0] == results[1] == results[2]


def test_missing_ledger_degrades_gracefully(tmp_path):
    missing = tmp_path / "does-not-exist.jsonl"
    assert rsr.count_ledger_sessions(missing, cutoff=None) is None
    assert rsr.load_ledger_rows(missing, cutoff=None) is None


def test_main_default_mode_exits_zero_with_no_transcripts(tmp_path, monkeypatch):
    registry = tmp_path / "registry.toml"
    _write_toml(registry, base_rules())
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text(CLAUDE_MD_TEXT)
    empty_projects_root = tmp_path / "empty-projects"
    empty_projects_root.mkdir()
    monkeypatch.setattr(rsr, "PROJECTS_ROOT", empty_projects_root)
    monkeypatch.setattr(rsr, "POLICY_LEDGER_PATH", tmp_path / "no-policy.jsonl")
    monkeypatch.setattr(rsr, "QUALITY_LEDGER_PATH", tmp_path / "no-quality.jsonl")

    rc = rsr.main(["--registry", str(registry), "--claude-md", str(claude_md)])
    assert rc == 0
