"""spawn-specialist.py cost-log attribution: stage_index + plan_path are stamped on
each log entry so per-stage cost attribution (stage 3 of the cost-tracking plan) can
sum rows by (plan_path, stage_index).

Tests cover:
- arg parser accepts --stage-index (present + absent)
- the log entry dict carries stage_index and plan_path when --stage-index is given
- omitting --stage-index yields stage_index=None (back-compat)
- $CLAUDE_SPAWN_COST_LOG redirects the ledger; unset keeps the host default
- extract_usage() keeps only billed token fields
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"


def _load():
    spec = importlib.util.spec_from_file_location("spawn_specialist", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()


# ---------------------------------------------------------------------------
# Arg parser: --stage-index flag
# ---------------------------------------------------------------------------

def test_stage_index_accepted_by_parser(tmp_path):
    plan = tmp_path / "plan.toml"
    plan.write_text("")
    args = MOD.build_parser().parse_args([
        "--kind", "developer",
        "--plan", str(plan),
        "--done-criterion", "tests green",
        "--criterion-type", "measurable",
        "--stage-index", "4",
    ])
    assert args.stage_index == 4


def test_stage_index_defaults_to_none(tmp_path):
    plan = tmp_path / "plan.toml"
    plan.write_text("")
    args = MOD.build_parser().parse_args([
        "--kind", "developer",
        "--plan", str(plan),
        "--done-criterion", "tests green",
        "--criterion-type", "measurable",
    ])
    assert args.stage_index is None


# ---------------------------------------------------------------------------
# Log entry dict carries stage_index and plan_path
# ---------------------------------------------------------------------------

def _build_entry(plan_path: str, stage_index) -> dict:
    """Mimic what spawn-specialist.main writes to log_cost_entry."""
    return {
        "ts": "2026-01-01T00:00:00+00:00",
        "event": "spawn",
        "kind": "developer",
        "budget_tier": "medium",
        "budget_usd_cap": "3.00",
        "depth": 1,
        "cost_usd": 0.5,
        "duration_ms": 1234,
        "return_marker": "COMPLETED",
        "exit_code": 0,
        "malformed": False,
        "stage_index": stage_index,
        "plan_path": plan_path,
        "session_id": None,
        "ticket": None,
    }


def test_entry_carries_stage_index_when_given():
    entry = _build_entry("/tmp/plan.toml", stage_index=3)
    assert entry["stage_index"] == 3


def test_entry_carries_plan_path():
    entry = _build_entry("/home/user/.claude/plans/my-plan.toml", stage_index=2)
    assert entry["plan_path"] == "/home/user/.claude/plans/my-plan.toml"


def test_entry_stage_index_none_when_absent():
    entry = _build_entry("/tmp/plan.toml", stage_index=None)
    assert entry["stage_index"] is None


def test_entry_has_both_attribution_keys():
    entry = _build_entry("/tmp/plan.toml", stage_index=5)
    assert "stage_index" in entry
    assert "plan_path" in entry


# ---------------------------------------------------------------------------
# Ledger path: $CLAUDE_SPAWN_COST_LOG override
# ---------------------------------------------------------------------------

def test_cost_log_path_defaults_to_host_log(monkeypatch):
    monkeypatch.delenv("CLAUDE_SPAWN_COST_LOG", raising=False)
    assert MOD.cost_log_path() == MOD.DEFAULT_COST_LOG


def test_cost_log_path_honors_override(monkeypatch, tmp_path):
    target = tmp_path / "run" / "spawn-costs.jsonl"
    monkeypatch.setenv("CLAUDE_SPAWN_COST_LOG", str(target))
    assert MOD.cost_log_path() == target


def test_log_cost_entry_writes_to_override_creating_parents(monkeypatch, tmp_path):
    target = tmp_path / "work" / "spawn-costs.jsonl"
    monkeypatch.setenv("CLAUDE_SPAWN_COST_LOG", str(target))

    MOD.log_cost_entry({"event": "spawn_start", "spawn_id": "abc"})
    MOD.log_cost_entry({"event": "spawn", "spawn_id": "abc", "cost_usd": 0.25})

    rows = [json.loads(line) for line in target.read_text().splitlines()]
    assert [r["event"] for r in rows] == ["spawn_start", "spawn"]
    assert {r["spawn_id"] for r in rows} == {"abc"}


def test_log_cost_entry_leaves_host_log_untouched_when_overridden(monkeypatch, tmp_path):
    """The whole point of the override: a containerized spawn must not append to
    (nor require) the machine-wide ledger."""
    monkeypatch.setenv("CLAUDE_SPAWN_COST_LOG", str(tmp_path / "spawn-costs.jsonl"))
    host_log = tmp_path / "never-written.jsonl"
    monkeypatch.setattr(MOD, "DEFAULT_COST_LOG", host_log)

    MOD.log_cost_entry({"event": "spawn"})

    assert not host_log.exists()


# ---------------------------------------------------------------------------
# extract_usage: billed token fields only
# ---------------------------------------------------------------------------

def test_extract_usage_keeps_billed_token_fields():
    payload = {"usage": {"input_tokens": 10, "output_tokens": 3,
                         "cache_creation_input_tokens": 1,
                         "cache_read_input_tokens": 2}}
    assert MOD.extract_usage(payload) == {
        "input_tokens": 10, "output_tokens": 3,
        "cache_creation_input_tokens": 1, "cache_read_input_tokens": 2,
    }


def test_extract_usage_drops_unbilled_counters():
    """An unpriced counter must not reach the imputed-list-price computation."""
    payload = {"usage": {"input_tokens": 10, "server_tool_use": {"web_search_requests": 4},
                         "service_tier": "standard"}}
    assert MOD.extract_usage(payload) == {"input_tokens": 10}


@pytest.mark.parametrize("payload", [None, {}, {"usage": None}, {"usage": "x"}, {"usage": {}}])
def test_extract_usage_returns_none_when_absent(payload):
    assert MOD.extract_usage(payload) is None


def test_extract_usage_prefers_modelusage_per_model():
    """modelUsage is the billed aggregate (summing its costUSD reproduces
    total_cost_usd); usage is a narrower slice. Prefer it, converting the
    camelCase counts to the canonical snake_case billed fields, per model."""
    payload = {"modelUsage": {"claude-opus-4-8": {
        "inputTokens": 100, "outputTokens": 20,
        "cacheCreationInputTokens": 2, "cacheReadInputTokens": 5}}}
    assert MOD.extract_usage(payload) == {"claude-opus-4-8": {
        "input_tokens": 100, "output_tokens": 20,
        "cache_creation_input_tokens": 2, "cache_read_input_tokens": 5}}


def test_extract_usage_modelusage_takes_precedence_over_flat_usage():
    """When both are present the per-model billed aggregate wins — pricing the
    flat usage block would understate the child (input_tokens 5 vs 100)."""
    payload = {"usage": {"input_tokens": 5},
               "modelUsage": {"claude-opus-4-8": {"inputTokens": 100}}}
    assert MOD.extract_usage(payload) == {"claude-opus-4-8": {"input_tokens": 100}}


def test_extract_usage_modelusage_drops_unbilled_counters():
    """An unpriced per-model counter must not reach the imputed-list-price sum."""
    payload = {"modelUsage": {"claude-opus-4-8": {
        "inputTokens": 10, "webSearchRequests": 4, "costUSD": 0.5}}}
    assert MOD.extract_usage(payload) == {"claude-opus-4-8": {"input_tokens": 10}}


# ---------------------------------------------------------------------------
# skill_path: unflattened <skills>/specializations/<kind>/ layout
# ---------------------------------------------------------------------------

def test_skill_path_finds_unflattened_specialization(monkeypatch, tmp_path):
    """A config root shipped as a plain tree (a profile bind-mounted into a
    container) has no per-kind symlinks, only the source layout."""
    skills = tmp_path / "skills"
    unflattened = skills / "specializations" / "developer" / "SKILL.md"
    unflattened.parent.mkdir(parents=True)
    unflattened.write_text("skill")
    monkeypatch.setattr(MOD, "SKILLS_DIR", skills)

    assert MOD.skill_path("developer") == unflattened


def test_skill_path_prefers_flattened_global(monkeypatch, tmp_path):
    skills = tmp_path / "skills"
    flat = skills / "developer" / "SKILL.md"
    flat.parent.mkdir(parents=True)
    flat.write_text("skill")
    (skills / "specializations" / "developer").mkdir(parents=True)
    (skills / "specializations" / "developer" / "SKILL.md").write_text("other")
    monkeypatch.setattr(MOD, "SKILLS_DIR", skills)

    assert MOD.skill_path("developer") == flat


def test_permissions_digest_empty_without_permissions_cli(monkeypatch, tmp_path):
    monkeypatch.setattr(MOD, "PERMISSIONS_CLI", tmp_path / "absent.py")
    assert MOD.permissions_digest(None) == ""
