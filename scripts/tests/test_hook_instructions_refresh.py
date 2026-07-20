"""Tests for hook-instructions-refresh-due.py — daily explicit refresh OFFER.

Hermetic: every "upstream" is a local file:// git remote (bare repo + clone),
never a real network fetch. Mirrors the git-fixture pattern from
test_sync_instructions_repo.py (make_bare_and_clone / advance_main) and the
subprocess-with-stdin invocation from test_hook_engine_start.py.

Covers: forced-due Core-behind nudge, throttle-window silence, up-to-date
silence, fetch-failure/non-git fail-open silence, project-layer nudge,
stamp-write-gates-next-run, the deploy-integrity axes (branch mismatch,
multi-root settings.json, malformed settings.json fail-open), the D3
worktree-fresh rebase OFFER for a behind linked worktree (fetch-before-count),
and the D2/R4 relocate OFFER for the primary Core checkout / a registered
canon-roots entry.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = SCRIPTS_DIR / "hook-instructions-refresh-due.py"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def git(*args, cwd, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env={**os.environ, **GIT_ENV},
        check=check,
        capture_output=True,
        text=True,
    )


def make_bare_and_clone(tmp_path: Path, name: str, with_claude_dir: bool = False):
    """Bare '<name>-origin' repo seeded with one commit on main, plus a clone of it."""
    origin = tmp_path / f"{name}-origin.git"
    git("init", "--quiet", "--bare", "-b", "main", str(origin), cwd=tmp_path)

    seed = tmp_path / f"{name}-seed"
    git("clone", "--quiet", str(origin), str(seed), cwd=tmp_path)
    (seed / "README.md").write_text("seed\n")
    git("add", "README.md", cwd=seed)
    if with_claude_dir:
        claude_dir = seed / ".claude"
        claude_dir.mkdir()
        (claude_dir / "marker.md").write_text("layer\n")
        git("add", ".claude", cwd=seed)
    git("commit", "--quiet", "-m", "seed: initial content", cwd=seed)
    git("push", "--quiet", "origin", "main", cwd=seed)

    clone = tmp_path / f"{name}-clone"
    git("clone", "--quiet", "-b", "main", str(origin), str(clone), cwd=tmp_path)
    return origin, clone


def advance_remote(tmp_path: Path, origin: Path, name: str):
    """Push one unrelated commit to origin/main via a throwaway clone, leaving
    any pre-existing clone of `origin` one commit behind."""
    other = tmp_path / f"{name}-advance"
    git("clone", "--quiet", "-b", "main", str(origin), str(other), cwd=tmp_path)
    (other / "ADVANCE.md").write_text("advance\n")
    git("add", "ADVANCE.md", cwd=other)
    git("commit", "--quiet", "-m", f"advance {name}", cwd=other)
    git("push", "--quiet", "origin", "main", cwd=other)


def run_hook(home: Path, core_repo, cwd: Path, settings_path: Path | None = None):
    env = {
        **os.environ,
        **GIT_ENV,
        "HOME": str(home),
        "CLAUDE_INSTRUCTIONS_REPO": str(core_repo),
    }
    if settings_path is not None:
        env["CLAUDE_SETTINGS_PATH"] = str(settings_path)
    payload = json.dumps({"session_id": "s-1", "prompt": "hi", "cwd": str(cwd)})
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=payload,
        env=env,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def stamp_path(home: Path) -> Path:
    return home / ".local" / "state" / "claude-instructions-refresh.stamp"


# ---------------------------------------------------------------------------
# (a) forced-due + Core behind -> nudge naming Core + its pull command
# ---------------------------------------------------------------------------


def test_core_behind_emits_nudge(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    origin, clone = make_bare_and_clone(tmp_path, "core")
    advance_remote(tmp_path, origin, "core")

    proc = run_hook(home, core_repo=clone, cwd=clone)

    assert proc.returncode == 0
    assert "[instructions-refresh]" in proc.stdout
    assert "Core" in proc.stdout
    assert "sync-instructions-repo.sh pull" in proc.stdout
    assert "1 commit(s) behind" in proc.stdout
    assert "AskUserQuestion" in proc.stdout


# ---------------------------------------------------------------------------
# (b) within throttle window (stamp = current session key) -> silent
# ---------------------------------------------------------------------------


def test_within_throttle_window_silent(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    origin, clone = make_bare_and_clone(tmp_path, "core")
    advance_remote(tmp_path, origin, "core")

    stamp = stamp_path(home)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    # run_hook posts session_id="s-1"; a stamp holding that key gates the run.
    stamp.write_text("s-1", encoding="utf-8")

    proc = run_hook(home, core_repo=clone, cwd=clone)

    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# ---------------------------------------------------------------------------
# (b2) stamp from a DIFFERENT session -> a new session re-fires the offer
# ---------------------------------------------------------------------------


def test_new_session_id_refires(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    origin, clone = make_bare_and_clone(tmp_path, "core")
    advance_remote(tmp_path, origin, "core")

    stamp = stamp_path(home)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    # A prior session's key; run_hook posts session_id="s-1" (a new session).
    stamp.write_text("s-old", encoding="utf-8")

    proc = run_hook(home, core_repo=clone, cwd=clone)

    assert proc.returncode == 0
    assert "[instructions-refresh]" in proc.stdout
    assert "Core" in proc.stdout
    # the new session's key is now recorded, gating its own later prompts
    assert stamp.read_text(encoding="utf-8").strip() == "s-1"


# ---------------------------------------------------------------------------
# (b3) no session_id in payload -> falls back to the per-calendar-day key
# ---------------------------------------------------------------------------


def test_missing_session_id_falls_back_to_daily_key(tmp_path):
    import datetime as dt

    home = tmp_path / "home"
    home.mkdir()
    origin, clone = make_bare_and_clone(tmp_path, "core")
    advance_remote(tmp_path, origin, "core")

    env = {
        **os.environ,
        **GIT_ENV,
        "HOME": str(home),
        "CLAUDE_INSTRUCTIONS_REPO": str(clone),
    }
    payload = json.dumps({"prompt": "hi", "cwd": str(clone)})  # no session_id

    def run_no_sid():
        return subprocess.run(
            [sys.executable, str(HOOK_SCRIPT)],
            input=payload, env=env, cwd=str(clone),
            capture_output=True, text=True,
        )

    first = run_no_sid()
    assert first.returncode == 0
    assert "[instructions-refresh]" in first.stdout
    # fallback key is today's date; recorded so a same-day rerun stays silent
    assert stamp_path(home).read_text(encoding="utf-8").strip() == dt.date.today().isoformat()

    second = run_no_sid()
    assert second.returncode == 0
    assert second.stdout.strip() == ""


# ---------------------------------------------------------------------------
# (c) up-to-date (behind=0) -> silent
# ---------------------------------------------------------------------------


def test_up_to_date_silent(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _origin, clone = make_bare_and_clone(tmp_path, "core")
    # A session cwd distinct from the Core repo itself — otherwise cwd would
    # BE the primary Core checkout and the D2/R4 relocate OFFER (a separate,
    # orthogonal axis — see test_primary_core_checkout_emits_relocate_offer)
    # would also fire, which is not what this test is checking.
    session_cwd = tmp_path / "session-cwd"
    session_cwd.mkdir()

    proc = run_hook(home, core_repo=clone, cwd=session_cwd)

    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# ---------------------------------------------------------------------------
# (d) fetch failure / non-git cwd -> silent, exit 0 (fail-open)
# ---------------------------------------------------------------------------


def test_missing_core_repo_and_non_git_cwd_silent(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    non_git_cwd = tmp_path / "not-a-repo"
    non_git_cwd.mkdir()
    missing_core = tmp_path / "does-not-exist"

    proc = run_hook(home, core_repo=missing_core, cwd=non_git_cwd)

    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# ---------------------------------------------------------------------------
# (e) project-layer repo behind while Core current -> nudge names project layer
# ---------------------------------------------------------------------------


def test_project_layer_behind_emits_nudge(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _core_origin, core_clone = make_bare_and_clone(tmp_path, "core")

    proj_origin, proj_clone = make_bare_and_clone(tmp_path, "project", with_claude_dir=True)
    advance_remote(tmp_path, proj_origin, "project")

    proc = run_hook(home, core_repo=core_clone, cwd=proj_clone)

    assert proc.returncode == 0
    assert "Project" in proc.stdout
    assert "Core" not in proc.stdout
    assert f"git -C {proj_clone} pull --ff-only" in proc.stdout
    assert "1 commit(s) behind" in proc.stdout


def test_project_layer_without_claude_dir_ignored(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _core_origin, core_clone = make_bare_and_clone(tmp_path, "core")

    proj_origin, proj_clone = make_bare_and_clone(tmp_path, "project", with_claude_dir=False)
    advance_remote(tmp_path, proj_origin, "project")

    proc = run_hook(home, core_repo=core_clone, cwd=proj_clone)

    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# ---------------------------------------------------------------------------
# (f) stamp is written on fire and gates a second same-day run
# ---------------------------------------------------------------------------


def test_stamp_written_on_fire_gates_second_run(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    origin, clone = make_bare_and_clone(tmp_path, "core")
    advance_remote(tmp_path, origin, "core")

    stamp = stamp_path(home)
    assert not stamp.exists()

    first = run_hook(home, core_repo=clone, cwd=clone)
    assert first.returncode == 0
    assert "[instructions-refresh]" in first.stdout
    assert stamp.exists()

    second = run_hook(home, core_repo=clone, cwd=clone)
    assert second.returncode == 0
    assert second.stdout.strip() == ""


# ---------------------------------------------------------------------------
# (g) deploy-integrity: Core checkout off the default branch -> deploy warn
# ---------------------------------------------------------------------------


def test_off_main_branch_emits_warn(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _origin, clone = make_bare_and_clone(tmp_path, "core")
    git("switch", "--quiet", "-c", "feat/x", cwd=clone)

    proc = run_hook(home, core_repo=clone, cwd=clone)

    assert proc.returncode == 0
    assert "[instructions-deploy]" in proc.stdout
    assert "not main" in proc.stdout
    assert "feat/x" in proc.stdout
    assert f"git -C {clone} switch main" in proc.stdout


# ---------------------------------------------------------------------------
# (h) deploy-integrity: settings.json hooks span >1 checkout root -> deploy warn
# ---------------------------------------------------------------------------


def test_multi_root_settings_emits_warn(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _origin, clone = make_bare_and_clone(tmp_path, "core")

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [
                    {"command": "/root-a/scripts/hook-one.py", "type": "command"},
                    {"command": "/root-b/scripts/hook-two.py", "type": "command"},
                ]},
            ]
        }
    }))

    proc = run_hook(home, core_repo=clone, cwd=clone, settings_path=settings)

    assert proc.returncode == 0
    assert "[instructions-deploy]" in proc.stdout
    assert "2 distinct checkout roots" in proc.stdout
    assert "/root-a" in proc.stdout
    assert "/root-b" in proc.stdout


# ---------------------------------------------------------------------------
# (i) deploy-integrity: on-main + single-root settings.json -> silent
# ---------------------------------------------------------------------------


def test_homogeneous_settings_silent(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _origin, clone = make_bare_and_clone(tmp_path, "core")
    session_cwd = tmp_path / "session-cwd"  # kept distinct from the Core repo — see test_up_to_date_silent
    session_cwd.mkdir()

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [
                    {"command": "/root-a/scripts/hook-one.py", "type": "command"},
                    {"command": "/root-a/scripts/hook-two.py", "type": "command"},
                ]},
            ]
        }
    }))

    proc = run_hook(home, core_repo=clone, cwd=session_cwd, settings_path=settings)

    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# ---------------------------------------------------------------------------
# (j) deploy-integrity: malformed settings.json -> fail-open silence
# ---------------------------------------------------------------------------


def test_malformed_settings_fails_open(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _origin, clone = make_bare_and_clone(tmp_path, "core")
    session_cwd = tmp_path / "session-cwd"  # kept distinct from the Core repo — see test_up_to_date_silent
    session_cwd.mkdir()

    settings = tmp_path / "settings.json"
    settings.write_text("{not valid json")

    proc = run_hook(home, core_repo=clone, cwd=session_cwd, settings_path=settings)

    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# ---------------------------------------------------------------------------
# (k) worktree-fresh (D3): a linked worktree behind origin/main -> rebase
#     OFFER, with the behind-count read only AFTER a fetch
# ---------------------------------------------------------------------------


def test_linked_worktree_behind_emits_rebase_offer(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    origin, clone = make_bare_and_clone(tmp_path, "wt")
    worktree = tmp_path / "wt-linked"
    git("worktree", "add", "--quiet", "-b", "wt-branch", str(worktree), "main", cwd=clone)
    # Advances origin/main AFTER the worktree's own local ref for origin/main was
    # last updated — so the OFFER only appears if check_worktree_fresh fetches
    # before counting; a stale-ref count would read 0-behind forever.
    advance_remote(tmp_path, origin, "wt")

    missing_core = tmp_path / "does-not-exist"
    proc = run_hook(home, core_repo=missing_core, cwd=worktree)

    assert proc.returncode == 0
    assert "[worktree-fresh]" in proc.stdout
    assert str(worktree) in proc.stdout
    assert "1 commit(s) behind" in proc.stdout
    assert f"git -C {worktree} rebase origin/main" in proc.stdout


def test_linked_worktree_up_to_date_silent(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _origin, clone = make_bare_and_clone(tmp_path, "wt")
    worktree = tmp_path / "wt-linked"
    git("worktree", "add", "--quiet", "-b", "wt-branch", str(worktree), "main", cwd=clone)

    missing_core = tmp_path / "does-not-exist"
    proc = run_hook(home, core_repo=missing_core, cwd=worktree)

    assert proc.returncode == 0
    assert "[worktree-fresh]" not in proc.stdout


def test_primary_checkout_no_worktree_fresh_offer(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    origin, clone = make_bare_and_clone(tmp_path, "wt")
    advance_remote(tmp_path, origin, "wt")

    # The primary (non-linked) checkout of a behind repo — out of scope for
    # worktree-fresh (that's check_layers'/build_nudge's "Core"/"Project" job).
    proc = run_hook(home, core_repo=clone, cwd=clone)

    assert proc.returncode == 0
    assert "[worktree-fresh]" not in proc.stdout


# ---------------------------------------------------------------------------
# (l) relocate (D2/R4): primary Core checkout or a registered canon-roots
#     entry -> relocation OFFER; a linked worktree or an unregistered sibling
#     path stays silent
# ---------------------------------------------------------------------------


def test_primary_core_checkout_emits_relocate_offer(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _origin, clone = make_bare_and_clone(tmp_path, "core")

    proc = run_hook(home, core_repo=clone, cwd=clone)

    assert proc.returncode == 0
    assert "[relocate]" in proc.stdout
    assert "session-isolate.sh" in proc.stdout


def test_linked_worktree_of_core_no_relocate_offer(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _origin, clone = make_bare_and_clone(tmp_path, "core")
    worktree = tmp_path / "core-linked"
    git("worktree", "add", "--quiet", "-b", "wt-branch", str(worktree), "main", cwd=clone)

    proc = run_hook(home, core_repo=clone, cwd=worktree)

    assert proc.returncode == 0
    assert "[relocate]" not in proc.stdout


def test_canon_roots_entry_emits_relocate_offer(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    missing_core = tmp_path / "does-not-exist"

    anchor = tmp_path / "org-mirror"
    inside = anchor / "sub" / "dir"
    inside.mkdir(parents=True)
    roots_file = tmp_path / "canon-roots.local"
    roots_file.write_text(f"{anchor}\n")

    env = {
        **os.environ,
        **GIT_ENV,
        "HOME": str(home),
        "CLAUDE_INSTRUCTIONS_REPO": str(missing_core),
        "CLAUDE_CANON_ROOTS_FILE": str(roots_file),
    }
    payload = json.dumps({"session_id": "s-1", "prompt": "hi", "cwd": str(inside)})
    proc = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=payload, env=env, cwd=str(inside),
        capture_output=True, text=True,
    )

    assert proc.returncode == 0
    assert "[relocate]" in proc.stdout
    assert "session-isolate.sh" in proc.stdout


def test_sibling_of_canon_root_no_relocate_offer(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    missing_core = tmp_path / "does-not-exist"

    anchor = tmp_path / "org-mirror"
    anchor.mkdir()
    sibling = tmp_path / "org-mirror-sibling"  # shares a string prefix, not a path-part prefix
    sibling.mkdir()
    roots_file = tmp_path / "canon-roots.local"
    roots_file.write_text(f"{anchor}\n")

    env = {
        **os.environ,
        **GIT_ENV,
        "HOME": str(home),
        "CLAUDE_INSTRUCTIONS_REPO": str(missing_core),
        "CLAUDE_CANON_ROOTS_FILE": str(roots_file),
    }
    payload = json.dumps({"session_id": "s-1", "prompt": "hi", "cwd": str(sibling)})
    proc = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=payload, env=env, cwd=str(sibling),
        capture_output=True, text=True,
    )

    assert proc.returncode == 0
    assert "[relocate]" not in proc.stdout
