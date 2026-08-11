"""Hermetic tests for `lib/permission_surface.is_permission_surface` and
`.widens`. No live filesystem reads: every fixture below is an inline copy
(or, where noted, a labeled reconstruction) of a real settings-shaped file,
so the module's shape-only recognition is proven against real shapes without
this test suite depending on any file's continued existence or content.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.permission_surface import is_permission_surface, widens  # noqa: E402


# --- real settings-shape fixtures (positive cases) --------------------------
#
# Verbatim copies of five real files' `permissions` shape, read directly from
# this worktree at implementation time:
#   - settings/base.json
#   - benchmark-profile/settings.json
#   - cursor/config/cli-base.json
#
# Two of the five named files could not be read verbatim: this developer
# session is sandboxed to this worktree plus ~/.claude-agent/plans, and
# /home/the0/.claude-agent/settings.json and .claude/settings.local.json
# live outside both (the latter also does not exist in this worktree at all
# -- it is a personal, gitignored file). SETTINGS_HARNESS_GLOBAL and
# SETTINGS_PROJECT_LOCAL below are therefore labeled RECONSTRUCTIONS: they
# follow the same `permissions.allow` list-of-`Tool(pattern)`-string shape
# every sibling file below actually has, which is exactly the shape this
# module recognizes -- but they are not byte-identical to the live files.
# Flagged in this stage's COMPLETED report as a residual for the plan owner.

SETTINGS_BASE = {
    "autoCompactWindow": 210000,
    "env": {"DISABLE_TELEMETRY": "1", "MAX_MCP_OUTPUT_TOKENS": "60000"},
    "permissions": {
        "allow": [
            "Bash(ls)", "Bash(ls:*)", "Bash(git status)", "Bash(git status:*)",
            "WebSearch", "mcp__intrasearch__search", "mcp__tracker__GetIssue",
        ],
        "defaultMode": "auto",
    },
}

BENCHMARK_PROFILE_SETTINGS = {
    "env": {"DISABLE_TELEMETRY": "1", "BASH_MAX_OUTPUT_LENGTH": "30000"},
    "permissions": {
        "allow": ["Bash(ls)", "Bash(ls:*)", "Bash(git status)", "Bash(git diff:*)"],
        "deny": ["Bash(claude -p:*)", "Bash(*spawn-specialist*)", "Bash(*agentctl*)",
                 "Workflow", "ScheduleWakeup", "CronCreate", "CronCreate:*"],
        "defaultMode": "acceptEdits",
    },
}

CURSOR_CLI_BASE = {
    "approvalMode": "auto-review",
    "permissions": {
        "allow": ["Shell(ls)", "Shell(pwd)", "Shell(git)", "Shell(gh)", "Shell(python3)"],
        "deny": ["Shell(sudo)", "Shell(su)", "Shell(dd)", "Shell(mkfs)",
                 "Shell(shutdown)", "Shell(reboot)", "Shell(passwd)"],
    },
    "sandbox": {"mode": "disabled", "networkAccess": "user_config_with_defaults"},
}

# RECONSTRUCTION -- see module docstring note above. Real file reported ~74
# allow entries at plan time; this keeps a representative subset.
SETTINGS_HARNESS_GLOBAL = {
    "permissions": {
        "allow": [
            "Bash(ls:*)", "Bash(git status:*)", "Bash(git log:*)", "WebSearch",
            "mcp__intrasearch__search", "mcp__wiki__GetPageDetails",
        ],
        "defaultMode": "auto",
    },
}

# RECONSTRUCTION -- see module docstring note above.
SETTINGS_PROJECT_LOCAL = {
    "permissions": {
        "allow": ["Bash(pytest:*)", "Bash(git diff:*)", "Read(//home/the0/**)"],
    },
}

# A path under no whitelist anywhere -- proves shape-detection, not a hidden
# path list. Any resemblance of the path to a real one is not relied on by
# the module, which never sees a path at all.
SYNTHETIC_UNLISTED_PATH_SHAPE = {
    "permissions": {"allow": ["Bash(whoami)"]},
}

REAL_AND_SYNTHETIC_SURFACES = [
    ("settings/base.json", SETTINGS_BASE),
    ("benchmark-profile/settings.json", BENCHMARK_PROFILE_SETTINGS),
    ("cursor/config/cli-base.json", CURSOR_CLI_BASE),
    ("~/.claude-agent/settings.json (reconstruction)", SETTINGS_HARNESS_GLOBAL),
    (".claude/settings.local.json (reconstruction)", SETTINGS_PROJECT_LOCAL),
    ("synthetic unlisted path", SYNTHETIC_UNLISTED_PATH_SHAPE),
]


def test_real_and_synthetic_surfaces_are_recognized():
    for label, doc in REAL_AND_SYNTHETIC_SURFACES:
        assert is_permission_surface(doc), label


# --- negative case: the permissions-cli LIST shape --------------------------
#
# Verbatim shape of permissions/global.json, managed by
# scripts/permissions-cli.py: `permissions` is a LIST, not an object with
# allow/deny. A different, unrelated schema that must not be recognized.

PERMISSIONS_CLI_MANAGED = {"permissions": []}
PERMISSIONS_CLI_MANAGED_NONEMPTY = {"permissions": ["push to release", "deploy to staging"]}


def test_permissions_cli_list_shape_is_not_a_surface():
    assert not is_permission_surface(PERMISSIONS_CLI_MANAGED)
    assert not is_permission_surface(PERMISSIONS_CLI_MANAGED_NONEMPTY)


def test_non_dict_and_shapeless_inputs_are_not_surfaces():
    assert not is_permission_surface(None)
    assert not is_permission_surface([])
    assert not is_permission_surface("permissions.json")
    assert not is_permission_surface({})
    assert not is_permission_surface({"env": {"X": "1"}})
    assert not is_permission_surface({"permissions": {"defaultMode": "auto"}})


# --- widens(): allow-addition is the widening it exists to catch ------------

def test_added_allow_entry_is_reported():
    old = {"permissions": {"allow": ["Bash(ls:*)"]}}
    new = {"permissions": {"allow": ["Bash(ls:*)", "Bash(sudo:*)"]}}
    assert widens(old, new) == ["Bash(sudo:*)"]


def test_narrowing_allow_is_not_widening():
    old = {"permissions": {"allow": ["Bash(ls:*)", "Bash(sudo:*)"]}}
    new = {"permissions": {"allow": ["Bash(ls:*)"]}}
    assert widens(old, new) == []


def test_reorder_only_is_not_widening():
    old = {"permissions": {"allow": ["a", "b", "c"]}}
    new = {"permissions": {"allow": ["c", "a", "b"]}}
    assert widens(old, new) == []


def test_dedupe_only_is_not_widening():
    old = {"permissions": {"allow": ["a", "a", "b"]}}
    new = {"permissions": {"allow": ["a", "b", "b"]}}
    assert widens(old, new) == []


def test_unrelated_key_change_is_not_widening():
    old = {"permissions": {"allow": ["a"], "defaultMode": "auto"}, "env": {"X": "1"}}
    new = {"permissions": {"allow": ["a"], "defaultMode": "acceptEdits"}, "env": {"X": "2"}}
    assert widens(old, new) == []


def test_identical_documents_are_not_widening():
    doc = {"permissions": {"allow": ["Bash(ls:*)"], "deny": ["Bash(sudo)"]}}
    assert widens(doc, doc) == []


# --- widens(): deny-removal is the other half of the same difficulty --------
#
# R6 (named residual): covered here with SYNTHETIC fixtures only, kept
# separate from the real-file fixtures above for isolation even though two of
# those real fixtures (BENCHMARK_PROFILE_SETTINGS, CURSOR_CLI_BASE) do carry a
# `permissions.deny` key -- contrary to this stage's original premise that no
# live file on this machine has one. Flagged in the COMPLETED report as a
# finding: a follow-on could promote deny-removal coverage to those two real
# fixtures instead of (or in addition to) the synthetic ones below.

def test_removed_deny_entry_is_reported():
    old = {"permissions": {"allow": [], "deny": ["Bash(sudo:*)", "Bash(su)"]}}
    new = {"permissions": {"allow": [], "deny": ["Bash(su)"]}}
    assert widens(old, new) == ["Bash(sudo:*)"]


def test_added_deny_entry_is_not_widening():
    old = {"permissions": {"allow": [], "deny": ["Bash(su)"]}}
    new = {"permissions": {"allow": [], "deny": ["Bash(su)", "Bash(sudo:*)"]}}
    assert widens(old, new) == []


def test_allow_addition_and_deny_removal_combine():
    old = {"permissions": {"allow": ["a"], "deny": ["x", "y"]}}
    new = {"permissions": {"allow": ["a", "b"], "deny": ["x"]}}
    assert widens(old, new) == ["b", "y"]


# --- widens(): three outcomes, not two --------------------------------------

def test_missing_old_doc_is_unknown_not_empty():
    new = {"permissions": {"allow": ["Bash(sudo:*)"]}}
    assert widens(None, new) is None


def test_unparseable_old_doc_is_unknown():
    assert widens("not-a-json-object", {"permissions": {"allow": ["a"]}}) is None
    assert widens(["also", "not", "an", "object"], {"permissions": {"allow": ["a"]}}) is None


def test_old_doc_without_permissions_key_is_known_empty_baseline_not_unknown():
    # A parseable object with no `permissions` key is a real, empty baseline
    # -- introducing permissions where none existed is reported as widening
    # by its entries, not swallowed into UNKNOWN.
    old = {"env": {"X": "1"}}
    new = {"permissions": {"allow": ["Bash(sudo:*)"]}}
    result = widens(old, new)
    assert result == ["Bash(sudo:*)"]


def test_missing_or_unparseable_new_doc_is_empty_not_unknown():
    old = {"permissions": {"allow": ["Bash(ls:*)"]}}
    assert widens(old, None) == []
    assert widens(old, "not-a-json-object") == []
