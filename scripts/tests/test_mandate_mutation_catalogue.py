"""Runs the mandate mutation catalogue in-suite: one test per entry, plus the unmutated control.

The test id of each entry is its catalogue name, so the verify command can require a PASSED line
for every named bound.
"""
from __future__ import annotations

import os

import pytest

import mandate_mutation_control as control

pytestmark = pytest.mark.skipif(
    os.environ.get("MANDATE_MUTATION_CHILD") == "1", reason="running inside a mutant subprocess",
)

EXPECTED_ENTRIES = (
    "constitution-classifier", "constitution-rename-old-side", "veto-label", "previous-digest-window",
    "severity-high-user-only", "file-notifier-excluded", "expiry", "breaker-open", "daily-budget",
    "weekly-budget", "triage-filter", "user-authority-verbs", "cycle-minutes-cap",
    "any-open-pr-excluded", "owner-authored-only", "stop-idle-no-kill",
    "baseline-relative-tests", "no-trunk-push", "push-checked-sha", "push-sha-shape",
    "head-pinned-before-review", "worktree-rechecked-before-push", "scope-heartbeat",
    "spawn-bound-by-cycle-cap", "item-limit-event", "item-row-defaults", "digest-kill-switch",
    "digest-limits", "digest-skipped-comments", "digest-delivery-eligibility", "notifier-precedence",
    "overrun-not-breaker", "single-instance-lock", "triage-label-logged", "no-issue-create",
    "no-user-authority-calls", "mandate-state-tamper", "triage-org-neutral",
    "skipped-is-settled", "optional-not-depended", "optional-max-two", "optional-coverage-rule",
    "reject-spares-skipped", "reject-names-skipped-refused", "push-refuses-skipped-origin",
    "agent-cannot-revive-declined", "declined-follows-identity",
)


def test_the_catalogue_names_exactly_the_expected_bounds():
    assert tuple(control.CATALOGUE) == EXPECTED_ENTRIES


@pytest.mark.parametrize("entry", sorted(control.CATALOGUE))
def test_mutant_is_killed(entry):
    assert control.run_mutant(entry) == control.KILLED


def test_the_control_is_green_on_the_unmutated_copy():
    assert control.run_control() == 0
