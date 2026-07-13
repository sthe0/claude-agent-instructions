"""Tests for R4 — element 7's typed `statement_kind: сущее | должное`.

Covers the three surfaces the field lands on:
  - agentctl/plan.py  — TOML parse + enum validation (present-only; absent grandfathers)
  - agentctl/state.py — the Principle dataclass field + optional default (legacy load)
  - verify-plan-file.py — the OPTIONAL prose `statement_kind:` label validated when present

The invariant under test: the field is ADDITIVE and OPTIONAL, so every plan/leaf/JSON
predating it still loads, while a present value must name one of the two knowledge/norm
categories (there is no third — принцип is the most general norm-series member, not a kind).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from agentctl.plan import PlanError, parse_plan  # noqa: E402
from agentctl.state import SCHEMA_VERSION, Principle, StatementKind  # noqa: E402


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
            "statement": "additive-optional keeps backward compat",
            "source": "leaf-schema.md precedent",
            "confidence": "high",
            "refutation": "refuted if existing fixture breaks",
        },
    }
    base.update(overrides)
    return base


def _meta() -> dict:
    return {"task_id": "t", "weight_class": "substantive", "external_research": "none applies"}


def _parse(stage: dict):
    return parse_plan({"meta": _meta(), "stage": [stage]})


# --- state.py: the field and the enum --------------------------------------

def test_schema_version_bumped():
    """SCHEMA_VERSION advanced to accommodate the new grandfathered field."""
    assert SCHEMA_VERSION >= 15


def test_statement_kind_enum_values():
    """Exactly two kinds on one reflexive refutation axis — no third category."""
    assert {k.value for k in StatementKind} == {"сущее", "должное"}


def test_principle_dataclass_field_optional_default():
    """Constructing a Principle without statement_kind defaults it to None (grandfather)."""
    p = Principle(statement="s", source="src", confidence="high", refutation="r")
    assert p.statement_kind is None


def test_principle_dataclass_field_accepts_kind():
    p = Principle(
        statement="s", source="src", confidence="high", refutation="r", statement_kind="должное"
    )
    assert p.statement_kind == "должное"


def test_legacy_principle_dict_grandfathers():
    """A legacy JSON principle dict (no statement_kind key) reconstructs via **unpack."""
    legacy = {"statement": "s", "source": "src", "confidence": "high", "refutation": "r"}
    p = Principle(**legacy)
    assert p.statement_kind is None


# --- plan.py: TOML parse + enum validation ---------------------------------

@pytest.mark.parametrize("kind", ["сущее", "должное"])
def test_typed_principle_parses(kind):
    stage = _stage()
    stage["principle"]["statement_kind"] = kind
    doc = _parse(stage)
    assert doc.stages[0].principle.statement_kind == kind


def test_absent_statement_kind_grandfathers():
    """A substantive plan without statement_kind parses with the field defaulted to None."""
    doc = _parse(_stage())
    assert doc.stages[0].principle.statement_kind is None


@pytest.mark.parametrize("bogus", ["world", "goal", "is", "ought", "сущие", ""])
def test_bogus_statement_kind_rejected(bogus):
    stage = _stage()
    stage["principle"]["statement_kind"] = bogus
    with pytest.raises(PlanError, match="statement_kind"):
        _parse(stage)


def test_existing_principle_subfields_unbroken():
    """The 4 required subfields keep their required-ness — statement_kind adds no
    new requirement (a plan with the 4 fields and no statement_kind still parses)."""
    doc = _parse(_stage())
    p = doc.stages[0].principle
    assert p.statement == "additive-optional keeps backward compat"
    assert p.source and p.confidence == "high" and p.refutation


def test_anti_template_checks_still_fire_with_kind():
    """Adding statement_kind does not weaken the refutation!=statement anti-template."""
    stage = _stage()
    stage["principle"]["statement_kind"] = "должное"
    stage["principle"]["refutation"] = stage["principle"]["statement"]
    with pytest.raises(PlanError, match="refutation"):
        _parse(stage)


# --- verify-plan-file.py: the OPTIONAL prose mirror ------------------------

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
    "Principle: use narrowest-scope tool\n"
    "Source: developer SKILL.md\n"
    "Confidence: high\n"
    "Refutation: if a full rewrite is needed, Write is required instead\n"
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
    """Absent statement_kind label => grandfathered, no error."""
    assert _check(tmp_path, _body()) == []


@pytest.mark.parametrize("kind", ["сущее", "должное"])
def test_prose_valid_statement_kind_ok(tmp_path, kind):
    assert _check(tmp_path, _body(f"statement_kind: {kind}\n")) == []


def test_prose_valid_statement_kind_backticked_ok(tmp_path):
    assert _check(tmp_path, _body("statement_kind: `должное`\n")) == []


def test_prose_bogus_statement_kind_rejected(tmp_path):
    errors = _check(tmp_path, _body("statement_kind: world\n"))
    assert any("statement_kind" in e for e in errors), errors
