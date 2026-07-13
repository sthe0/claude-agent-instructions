"""Regression tests for R4 (correct) — element 7 carries NO a-priori `statement_kind`.

ADR-0004 dropped the `statement_kind` typing of the principle as a category error:
`принцип` is the most general member of the norm-series and is therefore ALWAYS a норма
(`должное`), never a знание, so tagging it `сущее`/`должное` a-priori mistypes it. The
сущее/должное character of a *fault* is a POST-HOC product of критика at closure
(`Critique.failure_address`, R2), not a label the principle wears in advance.

Invariants under test, across the three surfaces the field used to land on:
  - agentctl/state.py  — the `Principle` dataclass has no `statement_kind` field; a legacy
    dict/session still carrying the key reconstructs unchanged (grandfather on load).
  - agentctl/plan.py   — a legacy TOML principle block carrying the retired key parses; no
    code path forces or validates `сущее|должное` onto a principle.
  - verify-plan-file.py — a prose plan carrying a `statement_kind:` label is neither required
    nor rejected (tolerated, never enforced).
"""
from __future__ import annotations

import dataclasses
import importlib.util
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from agentctl.plan import parse_plan  # noqa: E402
from agentctl.state import SCHEMA_VERSION, Principle, Stage  # noqa: E402


# --- helpers ---------------------------------------------------------------

def _stage(**overrides) -> dict:
    base = {
        "index": 1,
        "title": "Do something",
        "executor": "in_thread",
        "expected_result_image": "thing done",
        "done_criterion": "check passes",
        "material": "existing code",
        "means": "Edit tool",
        "method": "add the field",
        "conditions": "EXECUTING node",
        "invariants": "legacy plans unchanged",
        "capability_required": "Python",
        "verify_command": "python3 -m pytest tests/ -q",
        "principle": {
            "statement": "a norm is never checked for truth",
            "source": "SMD norm-series",
            "confidence": "high",
            "refutation": "shown inadequate when a goal it serves is blocked",
        },
    }
    base.update(overrides)
    return base


def _meta() -> dict:
    return {"task_id": "t", "weight_class": "substantive", "external_research": "none applies"}


def _parse(stage: dict):
    return parse_plan({"meta": _meta(), "stage": [stage]})


# --- state.py: the field is gone, legacy still loads -----------------------

def test_schema_version_monotonic():
    """SCHEMA_VERSION advanced past the statement_kind era (>=18)."""
    assert SCHEMA_VERSION >= 18


def test_principle_has_no_statement_kind_field():
    """The dataclass exposes exactly the four norm subfields — no statement_kind."""
    names = {f.name for f in dataclasses.fields(Principle)}
    assert names == {"statement", "source", "confidence", "refutation"}
    assert "statement_kind" not in names


def test_principle_construction_rejects_statement_kind_kwarg():
    """A principle cannot be constructed WITH the retired kwarg (the field is gone)."""
    import pytest
    with pytest.raises(TypeError):
        Principle(
            statement="s", source="src", confidence="high", refutation="r",
            statement_kind="должное",
        )


def test_legacy_principle_dict_grandfathers_on_load():
    """A legacy JSON principle dict carrying statement_kind reconstructs, key ignored."""
    legacy = {
        "statement": "s", "source": "src", "confidence": "high", "refutation": "r",
        "statement_kind": "сущее",
    }
    p = Principle.from_dict(legacy)
    assert p.statement == "s" and p.refutation == "r"
    assert not hasattr(p, "statement_kind")


def test_legacy_stage_dict_grandfathers_via_stage_from_dict():
    """A full legacy Stage dict whose principle carries the retired key loads unchanged."""
    doc = _parse(_stage())
    d = dataclasses.asdict(doc.stages[0])
    d["principle"]["statement_kind"] = "должное"  # inject the retired key
    s = Stage.from_dict(d)
    assert s.principle is not None
    assert s.principle.statement == "a norm is never checked for truth"
    assert not hasattr(s.principle, "statement_kind")


# --- plan.py: no сущее|должное forced or validated -------------------------

def test_plain_principle_parses_without_kind():
    """A substantive plan with the four subfields (no statement_kind) parses clean."""
    doc = _parse(_stage())
    p = doc.stages[0].principle
    assert p.statement and p.source and p.confidence == "high" and p.refutation
    assert not hasattr(p, "statement_kind")


def test_legacy_toml_principle_with_kind_parses_and_ignores_it():
    """A legacy TOML principle block still carrying statement_kind parses; key is not read."""
    stage = _stage()
    stage["principle"]["statement_kind"] = "должное"
    doc = _parse(stage)
    p = doc.stages[0].principle
    assert p.statement == "a norm is never checked for truth"
    assert not hasattr(p, "statement_kind")


def test_anti_template_checks_still_fire():
    """Dropping statement_kind does not weaken the refutation!=statement anti-template."""
    import pytest
    from agentctl.plan import PlanError
    stage = _stage()
    stage["principle"]["refutation"] = stage["principle"]["statement"]
    with pytest.raises(PlanError, match="refutation"):
        _parse(stage)


# --- verify-plan-file.py: statement_kind neither required nor rejected ------

def _load_vpf():
    spec = importlib.util.spec_from_file_location(
        "verify_plan_file", SCRIPTS_DIR / "verify-plan-file.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ACTIVITY_LABELS = (
    "Capability: Python\n"
    "Material: src/foo.py in its current state\n"
    "Means & method: Edit tool\n"
    "Conditions & invariants: no other files changed\n"
    "Principle: a norm is never checked for truth\n"
    "Source: SMD norm-series\n"
    "Confidence: high\n"
    "Refutation: shown inadequate when a goal it serves is blocked\n"
)
_EXTERNAL_RESEARCH = "External research: checked wiki; none applies.\n\n"


def _body(extra_label: str = "") -> str:
    return (
        _EXTERNAL_RESEARCH
        + "## Problem and done criteria\nFix it.\n\n"
        "## Stages\n\n### Stage 1\nExpected result image: tests pass\n"
        + _ACTIVITY_LABELS
        + extra_label
        + "\n## Final verification\nRun pytest.\n\n## Risks\nNone.\n"
    )


def _check(tmp_path, content: str):
    mod = _load_vpf()
    p = tmp_path / "plan.md"
    p.write_text("weight_class: substantive\n\n" + content, encoding="utf-8")
    return mod.check(p)


def test_prose_no_statement_kind_ok(tmp_path):
    """A prose plan without any statement_kind label validates clean."""
    assert _check(tmp_path, _body()) == []


def test_prose_legacy_statement_kind_label_tolerated(tmp_path):
    """A legacy prose plan still carrying a statement_kind label is not rejected for it."""
    errors = _check(tmp_path, _body("statement_kind: должное\n"))
    assert not any("statement_kind" in e for e in errors), errors
    assert errors == []
