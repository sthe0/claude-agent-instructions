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
from agentctl.dispatch import venue_settings_digests
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


# --- dispatched_venue / project_settings_refusal: only dispatch's venue, on the default store ---

SESSION = "project-settings-venue"


def _default_store(tmp_path: Path, monkeypatch) -> FileStateStore:
    """The default engine store under `tmp_path`, with no scratch roots so a venue there counts."""
    monkeypatch.setattr("agentctl.store.DEFAULT_ROOT", tmp_path / "state")
    monkeypatch.setattr(MOD.exempt_paths, "builtin_scratch_roots", lambda: [])
    return FileStateStore()


def _session_at_venue(store: FileStateStore, venue: Path) -> None:
    """A session whose delivery venue is `venue`, approved with its settings as they are now."""
    cli.cmd_start(Namespace(session=SESSION, task="t", goal="g", done_criterion="dc",
                            criterion_type="measurable", recursion_depth=0), store=store)
    state = store.load(SESSION)
    state.delivery_worktree = str(venue)
    state.repo_root = str(venue)
    state.venue_settings_at_approve = venue_settings_digests(str(venue))
    store.save(state)


def _checkout(directory: Path) -> Path:
    (directory / ".git").mkdir(parents=True)
    (directory / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return directory


def _venue_with_settings(tmp_path: Path, monkeypatch) -> Path:
    venue = _checkout(tmp_path / "proj")
    _write_settings(venue, {"permissions": {"allow": ["Bash(make test:*)"]}})
    _session_at_venue(_default_store(tmp_path, monkeypatch), venue)
    return venue


def _settings_of(directory: Path) -> Path:
    return directory / ".claude" / "settings.local.json"


def _refusal(project_settings: Path, workdir: Path, session, state_root=None):
    venue_settings, not_venue = MOD.dispatched_venue(str(workdir), session, state_root)
    return MOD.project_settings_refusal(project_settings, venue_settings, not_venue)


def test_project_settings_refusal_accepts_the_venue_settings_for_a_child_in_the_venue(tmp_path, monkeypatch):
    venue = _venue_with_settings(tmp_path, monkeypatch)
    assert _refusal(_settings_of(venue), venue, SESSION) is None


def test_project_settings_refusal_compares_by_realpath(tmp_path, monkeypatch):
    venue = _venue_with_settings(tmp_path, monkeypatch)
    link = tmp_path / "proj-link"
    link.symlink_to(venue)
    assert _refusal(_settings_of(link), link, SESSION) is None


def test_project_settings_refusal_refuses_without_a_session(tmp_path, monkeypatch):
    venue = _venue_with_settings(tmp_path, monkeypatch)
    assert "--session" in _refusal(_settings_of(venue), venue, None)


def test_project_settings_refusal_refuses_an_explicit_state_root(tmp_path, monkeypatch):
    venue = _venue_with_settings(tmp_path, monkeypatch)
    refusal = _refusal(_settings_of(venue), venue, SESSION, tmp_path / "state")
    assert "--state-root" in refusal


def test_project_settings_refusal_refuses_an_unknown_session(tmp_path, monkeypatch):
    venue = _venue_with_settings(tmp_path, monkeypatch)
    assert "no engine session no-such-session" in _refusal(_settings_of(venue), venue, "no-such-session")


def test_project_settings_refusal_refuses_an_unreadable_session_state(tmp_path, monkeypatch):
    venue = _venue_with_settings(tmp_path, monkeypatch)
    (tmp_path / "state" / f"{SESSION}.json").write_text("{not json", encoding="utf-8")
    assert "unreadable" in _refusal(_settings_of(venue), venue, SESSION)


def test_project_settings_refusal_refuses_a_forged_file_outside_the_venue(tmp_path, monkeypatch):
    venue = _venue_with_settings(tmp_path, monkeypatch)
    forged = _write_settings(tmp_path / "scratch" / "x", {"permissions": {"allow": ["Bash(git push:*)"]}})
    assert "is not the delivery venue's settings file" in _refusal(forged, venue, SESSION)


def test_project_settings_refusal_refuses_when_the_venue_has_no_settings_file(tmp_path, monkeypatch):
    venue = _checkout(tmp_path / "proj")
    _session_at_venue(_default_store(tmp_path, monkeypatch), venue)
    assert "(none)" in _refusal(_settings_of(venue), venue, SESSION)


def test_project_settings_refusal_refuses_a_child_started_outside_the_venue(tmp_path, monkeypatch):
    venue = _venue_with_settings(tmp_path, monkeypatch)
    elsewhere = tmp_path / "scratch"
    elsewhere.mkdir()
    assert "not in the delivery venue" in _refusal(_settings_of(venue), elsewhere, SESSION)


def test_project_settings_refusal_refuses_a_venue_whose_git_file_gitdir_dangles(tmp_path, monkeypatch):
    venue = tmp_path / "proj"
    venue.mkdir()
    (venue / ".git").write_text("gitdir: /elsewhere/.git/worktrees/proj\n")
    _write_settings(venue, {"permissions": {"allow": ["Bash(git push:*)"]}})
    _session_at_venue(_default_store(tmp_path, monkeypatch), venue)
    assert "is no repository checkout" in _refusal(_settings_of(venue), venue, SESSION)


def test_project_settings_refusal_refuses_an_unmarked_venue_outside_scratch(tmp_path, monkeypatch):
    venue = tmp_path / "proj"
    venue.mkdir()
    _write_settings(venue, {"permissions": {"allow": ["Bash(git push:*)"]}})
    _session_at_venue(_default_store(tmp_path, monkeypatch), venue)
    monkeypatch.setattr(MOD.exempt_paths, "builtin_scratch_roots", lambda: [])
    assert "is no repository checkout" in _refusal(_settings_of(venue), venue, SESSION)


def test_venue_settings_changed_after_approve_leave_the_child_user_only(tmp_path, monkeypatch):
    venue = _venue_with_settings(tmp_path, monkeypatch)
    _write_settings(venue, {"permissions": {"allow": ["Bash(git push:*)"]}})
    venue_settings, not_venue = MOD.dispatched_venue(str(venue), SESSION, None)
    assert venue_settings is None
    assert f"{_settings_of(venue)} changed since `agentctl approve`" in not_venue


def test_venue_under_a_scratch_root_leaves_the_child_user_only(tmp_path, monkeypatch):
    venue = _venue_with_settings(tmp_path, monkeypatch)
    monkeypatch.setattr(MOD.exempt_paths, "builtin_scratch_roots", lambda: [str(tmp_path)])
    venue_settings, not_venue = MOD.dispatched_venue(str(venue), SESSION, None)
    assert venue_settings is None
    assert "is under a scratch root" in not_venue


_BUILTIN_SCRATCH_ROOTS = MOD.exempt_paths.builtin_scratch_roots


def test_an_env_scratch_root_list_does_not_move_the_scratch_decision(tmp_path, monkeypatch):
    venue = _venue_with_settings(tmp_path, monkeypatch)
    monkeypatch.setattr(MOD.exempt_paths, "builtin_scratch_roots", _BUILTIN_SCRATCH_ROOTS)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    monkeypatch.setenv("AGENTCTL_SCRATCH_ROOTS", str(unrelated))
    venue_settings, not_venue = MOD.dispatched_venue(str(venue), SESSION, None)
    assert venue_settings is None
    assert "is under a scratch root" in not_venue


# --- config_root_refusal / starts_in_parent_project: the parent found under the login home ---

PARENT_SESSION = "parent-under-login-home"


def _login_home_with_transcript(tmp_path: Path, monkeypatch, cwd: Path) -> Path:
    home = tmp_path / "home"
    folder = home / ".claude-agent" / "projects" / MOD._project_dir_name(str(cwd))
    folder.mkdir(parents=True)
    (folder / f"{PARENT_SESSION}.jsonl").write_text(json.dumps({"type": "user", "cwd": str(cwd)}) + "\n")
    monkeypatch.setattr(MOD, "_login_home", lambda: str(home))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", PARENT_SESSION)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_AGENT_HOME", raising=False)
    return home


def test_config_root_refusal_refuses_a_config_dir_other_than_the_parents(tmp_path, monkeypatch):
    _login_home_with_transcript(tmp_path, monkeypatch, tmp_path / "proj")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "forged"))
    refusal = MOD.config_root_refusal()
    assert refusal is not None and str(tmp_path / "forged") in refusal


def test_config_root_refusal_refuses_a_config_dir_when_the_session_is_unknown(tmp_path, monkeypatch):
    _login_home_with_transcript(tmp_path, monkeypatch, tmp_path / "proj")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "no-such-session")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "forged"))
    refusal = MOD.config_root_refusal()
    assert refusal is not None and "the child's config root" in refusal


def test_config_root_refusal_passes_the_parents_own_config_root(tmp_path, monkeypatch):
    home = _login_home_with_transcript(tmp_path, monkeypatch, tmp_path / "proj")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude-agent"))
    assert MOD.config_root_refusal() is None


def test_child_started_in_the_parents_project_root_loads_project_sources(tmp_path, monkeypatch):
    project = _checkout(tmp_path / "proj")
    _login_home_with_transcript(tmp_path, monkeypatch, project)
    monkeypatch.setattr(MOD.exempt_paths, "builtin_scratch_roots", lambda: [])
    assert MOD.user_only_reason(str(project), "not the delivery venue", PARENT_SESSION) is None
