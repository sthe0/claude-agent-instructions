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
    "overrun-not-breaker", "item-local-not-breaker", "unknown-kind-opens-breaker", "local-failure-streak",
    "streak-since-granted", "failed-before", "failed-issues-passed", "retry-allowed-clears",
    "single-instance-lock", "triage-label-logged", "no-issue-create",
    "no-user-authority-calls", "mandate-state-tamper", "triage-org-neutral",
    "skipped-is-settled", "optional-not-depended", "optional-max-two", "optional-coverage-rule",
    "reject-spares-skipped", "reject-names-skipped-refused", "push-refuses-skipped-origin",
    "agent-cannot-revive-declined", "declined-follows-identity", "declined-in-ledger",
    "declined-required-in-ledger", "declined-title-half", "declined-seeded-from-ledger",
    "worktree-canonical-ids", "vanished-test-excused", "vanished-test-dirty-module",
    "vanished-test-rename-group", "rerun-verdict-ignores-excused",
    "refusal-notice-sent", "refusal-notice-throttle", "refusal-notice-file-only-suppresses",
    "refusal-notice-not-a-digest", "blocked-since-breaker-trip", "unblock-command-per-reason",
    "expiry-warning", "redeliver-no-record", "redeliver-record-without-send", "redeliver-not-idempotent",
    "inline-test-exclusion", "inline-generated-exclusion", "inline-bound-comparison",
    "inline-binary-failsafe", "inline-reason-source", "inline-memory-exclusion",
    "inline-constitution-fixed",
    "triage-admits-any-size", "basis-skips-only-under-mandate", "basis-exact-norm",
    "basis-keeps-since-user", "basis-binds-taken-item", "reject-records-actor", "child-is-linked",
    "settled-is-mandates", "stop-keeps-breaker-closed", "check-item-bound", "check-item-empty-diff",
    "covered-denials-counted", "nested-cost-charged", "salvage-kept", "salvage-fail-open",
    "account-limit-graceful",
    "ask-unknown-exceeds", "ask-fire-exceeds", "ask-permission-covered", "ask-missing-field",
    "ask-repeat-denial", "ask-fire-replans", "ask-since-user-backstop", "ask-question-for-user",
    "ask-user-only-verb", "ask-loop-detected", "ask-rounds-limit", "stop-resource-retried",
    "approval-exact-norm", "approval-open-gate", "item-norm-grants", "account-limit-precedence",
    "account-limit-error-kind", "account-limit-last-record", "account-limit-api-flag",
    "account-limit-infra-marker", "nested-cost-counted", "basis-binds-taken-worktree",
    "basis-binds-engine-repo", "review-crash-fails-item", "item-norm-escape",
    "ask-self-approval-settled", "ask-over-bound-parent-norm", "ask-check-green",
    "ask-landing-waiver", "ask-resolution-node", "ask-acceptance-active",
    "ask-wall-clock-exceeds", "ask-wall-clock-scale", "stop-limit-mapping",
    "stop-declined-fallback", "item-norm-title-cut", "item-norm-body-cut",
    "item-norm-body-cut-length", "mandate-id-compared", "driver-records-order",
)


def test_the_catalogue_names_exactly_the_expected_bounds():
    assert tuple(control.CATALOGUE) == EXPECTED_ENTRIES


@pytest.mark.parametrize("entry", sorted(control.CATALOGUE))
def test_mutant_is_killed(entry):
    assert control.run_mutant(entry) == control.KILLED


def test_the_control_is_green_on_the_unmutated_copy():
    assert control.run_control() == 0
