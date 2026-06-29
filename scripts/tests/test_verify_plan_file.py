"""Tests for scripts/verify-plan-file.py — baseline and substantive-plan checks."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
VERIFY_SCRIPT = SCRIPTS_DIR / "verify-plan-file.py"


def _load():
    spec = importlib.util.spec_from_file_location("verify_plan_file", VERIFY_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "plan.md"
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Helpers: minimal plan bodies
# ---------------------------------------------------------------------------

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

# Minimal plan with activity labels inside ## Stages (used for substantive tests).
def _make_body(stage_extra: str = "") -> str:
    return (
        "## Problem and done criteria\n"
        "Fix the thing.\n"
        "\n"
        "## Stages\n"
        "\n"
        "### Stage 1\n"
        "Expected result image: tests pass\n"
        + stage_extra
        + "\n"
        "## Final verification\n"
        "Run pytest.\n"
        "\n"
        "## Risks\n"
        "None.\n"
    )


_BASELINE_BODY = _make_body()
_SUBSTANTIVE_BODY = _make_body(stage_extra=_ACTIVITY_LABELS)


# ---------------------------------------------------------------------------
# 1. Legacy / non-substantive plan — only the 4 baseline sections needed
# ---------------------------------------------------------------------------


def test_legacy_plan_passes(tmp_path):
    """A plan without weight_class must pass with just the 4 baseline sections."""
    mod = _load()
    plan = _write(tmp_path, _BASELINE_BODY)
    assert mod.check(plan) == []


def test_legacy_plan_missing_section_fails(tmp_path):
    """Legacy plan missing Final verification should still fail the baseline check."""
    mod = _load()
    body = _BASELINE_BODY.replace("## Final verification\nRun pytest.\n\n", "")
    plan = _write(tmp_path, body)
    errors = mod.check(plan)
    assert any("Final verification" in e for e in errors)


# ---------------------------------------------------------------------------
# 2. Substantive plan with all required labels — must pass
# ---------------------------------------------------------------------------


def test_substantive_plan_passes(tmp_path):
    """Substantive plan with weight_class marker + all activity labels passes."""
    mod = _load()
    content = "weight_class: substantive\n\n" + _SUBSTANTIVE_BODY
    plan = _write(tmp_path, content)
    assert mod.check(plan) == []


def test_substantive_marker_case_insensitive(tmp_path):
    """weight_class marker detection is case-insensitive."""
    mod = _load()
    content = "Weight_Class: Substantive\n\n" + _SUBSTANTIVE_BODY
    plan = _write(tmp_path, content)
    assert mod.check(plan) == []


def test_substantive_marker_equals_form(tmp_path):
    """weight_class = substantive (equals sign) is also recognised."""
    mod = _load()
    content = "weight_class = substantive\n\n" + _SUBSTANTIVE_BODY
    plan = _write(tmp_path, content)
    assert mod.check(plan) == []


def test_substantive_with_separate_means_method(tmp_path):
    """Separate Means: and Method: labels satisfy the Means & method requirement."""
    mod = _load()
    labels = _ACTIVITY_LABELS.replace(
        "Means & method: Edit tool",
        "Means: Edit tool\nMethod: direct Edit call",
    )
    content = "weight_class: substantive\n\n" + _make_body(stage_extra=labels)
    plan = _write(tmp_path, content)
    assert mod.check(plan) == []


def test_substantive_with_separate_conditions_invariants(tmp_path):
    """Separate Conditions: and Invariants: satisfy the combined requirement."""
    mod = _load()
    labels = _ACTIVITY_LABELS.replace(
        "Conditions & invariants: no other files changed",
        "Conditions: CI must be green\nInvariants: no other files changed",
    )
    content = "weight_class: substantive\n\n" + _make_body(stage_extra=labels)
    plan = _write(tmp_path, content)
    assert mod.check(plan) == []


# ---------------------------------------------------------------------------
# 3. Substantive plan missing individual labels — must emit precise errors
# ---------------------------------------------------------------------------


def test_substantive_missing_material(tmp_path):
    mod = _load()
    labels = _ACTIVITY_LABELS.replace("Material: src/foo.py in its current state\n", "")
    content = "weight_class: substantive\n\n" + _make_body(stage_extra=labels)
    plan = _write(tmp_path, content)
    errors = mod.check(plan)
    assert any("Material" in e for e in errors), errors


def test_substantive_missing_means_method(tmp_path):
    mod = _load()
    labels = _ACTIVITY_LABELS.replace("Means & method: Edit tool\n", "")
    content = "weight_class: substantive\n\n" + _make_body(stage_extra=labels)
    plan = _write(tmp_path, content)
    errors = mod.check(plan)
    assert any("Means" in e for e in errors), errors


def test_substantive_missing_conditions_invariants(tmp_path):
    mod = _load()
    labels = _ACTIVITY_LABELS.replace("Conditions & invariants: no other files changed\n", "")
    content = "weight_class: substantive\n\n" + _make_body(stage_extra=labels)
    plan = _write(tmp_path, content)
    errors = mod.check(plan)
    assert any("Conditions" in e for e in errors), errors


def test_substantive_missing_principle(tmp_path):
    """Missing Principle block → single clear error; no Source/Confidence/Refutation errors."""
    mod = _load()
    labels = "\n".join(
        line for line in _ACTIVITY_LABELS.splitlines()
        if not any(kw in line for kw in ("Principle", "Source", "Confidence", "Refutation"))
    ) + "\n"
    content = "weight_class: substantive\n\n" + _make_body(stage_extra=labels)
    plan = _write(tmp_path, content)
    errors = mod.check(plan)
    assert any("Principle" in e for e in errors), errors
    # Source/Confidence/Refutation errors not emitted when Principle itself is absent
    assert not any("Source" in e for e in errors)
    assert not any("Confidence" in e for e in errors)
    assert not any("Refutation" in e for e in errors)


def test_substantive_missing_source(tmp_path):
    mod = _load()
    labels = _ACTIVITY_LABELS.replace("Source: developer SKILL.md\n", "")
    content = "weight_class: substantive\n\n" + _make_body(stage_extra=labels)
    plan = _write(tmp_path, content)
    errors = mod.check(plan)
    assert any("Source" in e for e in errors), errors


def test_substantive_missing_confidence(tmp_path):
    mod = _load()
    labels = _ACTIVITY_LABELS.replace("Confidence: high\n", "")
    content = "weight_class: substantive\n\n" + _make_body(stage_extra=labels)
    plan = _write(tmp_path, content)
    errors = mod.check(plan)
    assert any("Confidence" in e for e in errors), errors


def test_substantive_missing_refutation(tmp_path):
    mod = _load()
    labels = _ACTIVITY_LABELS.replace(
        "Refutation: if a full rewrite is needed, Write is required instead\n", ""
    )
    content = "weight_class: substantive\n\n" + _make_body(stage_extra=labels)
    plan = _write(tmp_path, content)
    errors = mod.check(plan)
    assert any("Refutation" in e for e in errors), errors


def test_substantive_multiple_missing_labels_all_reported(tmp_path):
    """Each missing label produces a separate error entry."""
    mod = _load()
    # Remove Material, Means & method, and Principle block entirely
    labels = "\n".join(
        line for line in _ACTIVITY_LABELS.splitlines()
        if not any(kw in line for kw in ("Material", "Means", "Principle", "Source", "Confidence", "Refutation"))
    ) + "\n"
    content = "weight_class: substantive\n\n" + _make_body(stage_extra=labels)
    plan = _write(tmp_path, content)
    errors = mod.check(plan)
    assert len(errors) >= 3  # Material, Means & method, Principle
    assert any("Material" in e for e in errors)
    assert any("Means" in e for e in errors)
    assert any("Principle" in e for e in errors)


# ---------------------------------------------------------------------------
# 4. Capability / Actor label (element 6 — actor capability)
# ---------------------------------------------------------------------------


def test_substantive_missing_capability_label(tmp_path):
    """Missing Capability: label on a substantive plan emits a clear error."""
    mod = _load()
    labels = _ACTIVITY_LABELS.replace("Capability: Python\n", "")
    content = "weight_class: substantive\n\n" + _make_body(stage_extra=labels)
    plan = _write(tmp_path, content)
    errors = mod.check(plan)
    assert any("Capability" in e or "Actor" in e for e in errors), errors


def test_substantive_actor_label_satisfies_capability(tmp_path):
    """Actor: is an accepted alternative to Capability: for element 6."""
    mod = _load()
    labels = _ACTIVITY_LABELS.replace("Capability: Python\n", "Actor: developer with Python\n")
    content = "weight_class: substantive\n\n" + _make_body(stage_extra=labels)
    plan = _write(tmp_path, content)
    assert mod.check(plan) == []


def test_non_substantive_missing_capability_label_ok(tmp_path):
    """Legacy plan without weight_class does not require Capability:."""
    mod = _load()
    plan = _write(tmp_path, _BASELINE_BODY)
    assert mod.check(plan) == []
