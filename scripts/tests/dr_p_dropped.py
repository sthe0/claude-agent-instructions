"""Shared test helper: tell DR-P's dropped entries (covered in test_grants_own_branch) from
the validator's, by the `rule` tag DR-P stamps rather than by the entry's text."""
from __future__ import annotations

from agentctl.grants import DR_P_RULE


def validator_refusals(dropped: list[dict]) -> list[dict]:
    return [d for d in dropped if d.get("rule") != DR_P_RULE]
