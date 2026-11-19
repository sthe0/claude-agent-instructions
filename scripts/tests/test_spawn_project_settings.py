"""A spawned developer's `--settings` payload only ever carried the fleet-wide
KIND_BASELINES["developer"] list (scoped to this repo's own verifiers) plus the
plans-directory grant — never the TARGET project's own `.claude/settings.local.json`
permissions.allow/deny, even when that project has already, deliberately,
allow-listed its own build/test commands. `--project-permissions` looked like
it covered this but does not: it only feeds `permissions_digest`, a PROSE
summary embedded in the prompt, never the `--settings` JSON the harness
actually gates tool calls against — confirmed by reading permissions_digest
and permissions-cli.py's `digest` command, which read a permissions/*.json
audit-log shape (pattern/granted_at/context), not a settings.local.json shape.

project_settings_permission_rules + build_child_settings's new
project_settings_file parameter close that gap for kind=="developer" only.
"""
from __future__ import annotations

import importlib.util
import json
from argparse import Namespace
from pathlib import Path

from agentctl import cli
from agentctl.store import FileStateStore

SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"


def _load():
    spec = importlib.util.spec_from_file_location("spawn_specialist_project_settings", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()


def _write_settings(tmp_path: Path, payload: dict) -> Path:
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    path = settings_dir / "settings.local.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_project_settings_permission_rules_reads_allow_and_deny(tmp_path):
    path = _write_settings(tmp_path, {
        "permissions": {
            "allow": ["Bash(python3 scripts/test_render_site.py:*)"],
            "deny": ["Bash(rm:*)"],
        }
    })
    allow, deny = MOD.project_settings_permission_rules(path)
    assert allow == ["Bash(python3 scripts/test_render_site.py:*)"]
    assert deny == ["Bash(rm:*)"]


def test_project_settings_permission_rules_none_path_is_empty():
    assert MOD.project_settings_permission_rules(None) == ([], [])


def test_project_settings_permission_rules_missing_file_fails_open(tmp_path):
    missing = tmp_path / ".claude" / "settings.local.json"
    assert MOD.project_settings_permission_rules(missing) == ([], [])


def test_project_settings_permission_rules_malformed_json_fails_open(tmp_path):
    path = tmp_path / "settings.local.json"
    path.write_text("{not json", encoding="utf-8")
    assert MOD.project_settings_permission_rules(path) == ([], [])


def test_project_settings_permission_rules_non_dict_json_fails_open(tmp_path):
    path = tmp_path / "settings.local.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert MOD.project_settings_permission_rules(path) == ([], [])


def test_project_settings_permission_rules_ignores_non_string_entries(tmp_path):
    """A malformed entry (not a string) is dropped rather than crashing the
    spawn or being forwarded verbatim into the --settings JSON's allow list."""
    path = _write_settings(tmp_path, {
        "permissions": {"allow": ["Bash(ls:*)", 42, None], "deny": "not-a-list"}
    })
    allow, deny = MOD.project_settings_permission_rules(path)
    assert allow == ["Bash(ls:*)"]
    assert deny == []


def test_build_child_settings_merges_project_allow_for_developer(tmp_path):
    path = _write_settings(tmp_path, {
        "permissions": {"allow": ["Bash(python3 scripts/test_render_site.py:*)"]}
    })
    settings = MOD.build_child_settings("developer", project_settings_file=path)
    allow = settings["permissions"]["allow"]
    assert "Bash(python3 scripts/test_render_site.py:*)" in allow
    # the fleet-wide grant survives alongside the project-specific one
    assert "Bash(python3 -m pytest:*)" in allow


def test_build_child_settings_project_settings_ignored_for_non_developer_kind(tmp_path):
    path = _write_settings(tmp_path, {
        "permissions": {"allow": ["Bash(python3 scripts/test_render_site.py:*)"]}
    })
    settings = MOD.build_child_settings("thinker", project_settings_file=path)
    allow = settings.get("permissions", {}).get("allow", [])
    assert "Bash(python3 scripts/test_render_site.py:*)" not in allow


def test_build_child_settings_no_project_settings_file_unchanged():
    """Back-compat: omitting project_settings_file produces the exact same
    payload as before this parameter existed -- the developer KIND_BASELINES
    row, unmerged with anything project-specific."""
    with_none = MOD.build_child_settings("developer")
    assert with_none["permissions"]["allow"] == list(MOD.KIND_BASELINES["developer"])


def test_build_child_settings_merges_project_deny_alongside_plans_deny(tmp_path):
    project_path = _write_settings(tmp_path, {"permissions": {"deny": ["Bash(curl:*)"]}})
    plans_directory = tmp_path / "plans"
    plans_directory.mkdir()
    settings = MOD.build_child_settings(
        "developer", plans_directory=plans_directory, project_settings_file=project_path,
    )
    assert "Bash(curl:*)" in settings["permissions"]["deny"]


# --- project_settings_refusal: only the file dispatch forwards for the session ---

SESSION = "project-settings-venue"


def _session_at_venue(state_root: Path, venue: Path) -> None:
    store = FileStateStore(state_root)
    cli.cmd_start(Namespace(session=SESSION, task="t", goal="g", done_criterion="dc",
                            criterion_type="measurable", recursion_depth=0), store=store)
    state = store.load(SESSION)
    state.delivery_worktree = str(venue)
    state.repo_root = str(venue)
    store.save(state)


def _venue_with_settings(tmp_path: Path) -> Path:
    venue = tmp_path / "proj"
    _write_settings(venue, {"permissions": {"allow": ["Bash(make test:*)"]}})
    _session_at_venue(tmp_path / "state", venue)
    return venue


def _settings_of(directory: Path) -> Path:
    return directory / ".claude" / "settings.local.json"


def test_project_settings_refusal_accepts_the_venue_settings_for_a_child_in_the_venue(tmp_path):
    venue = _venue_with_settings(tmp_path)
    assert MOD.project_settings_refusal(_settings_of(venue), str(venue), SESSION, tmp_path / "state") is None


def test_project_settings_refusal_compares_by_realpath(tmp_path):
    venue = _venue_with_settings(tmp_path)
    link = tmp_path / "proj-link"
    link.symlink_to(venue)
    assert MOD.project_settings_refusal(_settings_of(link), str(link), SESSION, tmp_path / "state") is None


def test_project_settings_refusal_refuses_without_a_session(tmp_path):
    venue = _venue_with_settings(tmp_path)
    refusal = MOD.project_settings_refusal(_settings_of(venue), str(venue), None, tmp_path / "state")
    assert "--session" in refusal


def test_project_settings_refusal_refuses_an_unknown_session(tmp_path):
    venue = _venue_with_settings(tmp_path)
    refusal = MOD.project_settings_refusal(_settings_of(venue), str(venue), "no-such-session", tmp_path / "state")
    assert "no engine session no-such-session" in refusal


def test_project_settings_refusal_refuses_a_forged_file_outside_the_venue(tmp_path):
    venue = _venue_with_settings(tmp_path)
    forged = _write_settings(tmp_path / "scratch" / "x", {"permissions": {"allow": ["Bash(git push:*)"]}})
    refusal = MOD.project_settings_refusal(forged, str(venue), SESSION, tmp_path / "state")
    assert "is not the settings file of the delivery venue" in refusal


def test_project_settings_refusal_refuses_when_the_venue_has_no_settings_file(tmp_path):
    venue = tmp_path / "proj"
    venue.mkdir()
    _session_at_venue(tmp_path / "state", venue)
    refusal = MOD.project_settings_refusal(_settings_of(venue), str(venue), SESSION, tmp_path / "state")
    assert "(none)" in refusal


def test_project_settings_refusal_refuses_a_child_started_outside_the_venue(tmp_path):
    venue = _venue_with_settings(tmp_path)
    elsewhere = tmp_path / "scratch"
    elsewhere.mkdir()
    refusal = MOD.project_settings_refusal(_settings_of(venue), str(elsewhere), SESSION, tmp_path / "state")
    assert "but the child starts in" in refusal
