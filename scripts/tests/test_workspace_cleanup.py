"""Engine-owned workspace cleanup on resolve (agentctl/workspace_cleanup.py).

Driven against real temporary git repositories (a bare "origin", a main checkout, linked
worktrees), because the safety properties — git's own refusal of a dirty tree, ancestry
of HEAD in origin/main, `branch -d` — are properties of git itself that a stub would
only restate. HOME, the registry, the canon/anchor/store roots and every git identity
variable are pointed at the test's tmp tree, so nothing here can reach a real workspace.

Tests whose name carries `dirty`, `untracked`, `unlanded` or `protected` also assert the
decision `workspace_cleanup.classify` returns: git itself (and `execute`'s own guard)
would refuse those removals even if `classify` were wrong, so only the direct assertion
makes a broken decision rule observable.
"""
from __future__ import annotations

import os
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, workspace_cleanup as wc

SESSION = "ws-session-1"

_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}

_SEAM_ENVS = (
    "GIT_BIN", "CLAUDE_DRY_RUN", wc.ANCHOR_ROOTS_ENV, wc.STORE_ROOTS_ENV,
    wc.SCRATCH_ROOTS_ENV, wc.TEARDOWN_ENV,
)


def ns(**kw):
    return Namespace(**kw)


class Repo:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.origin = tmp / "origin.git"
        self.main = tmp / "main-checkout"
        self.worktrees = tmp / "wt"

    def git(self, *args: str, cwd: Path | None = None) -> str:
        proc = subprocess.run(["git", *args], cwd=str(cwd or self.main), check=True,
                              capture_output=True, text=True)
        return proc.stdout.strip()

    def init(self) -> "Repo":
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.main)], check=True)
        self.git("remote", "add", "origin", str(self.origin))
        self.commit(self.main, "README", "base\n")
        self.git("push", "-q", "-u", "origin", "main")
        return self

    def commit(self, path: Path, filename: str, text: str) -> None:
        (path / filename).write_text(text)
        self.git("add", filename, cwd=path)
        self.git("commit", "-q", "-m", f"add {filename}", cwd=path)

    def worktree(self, name: str) -> Path:
        path = self.worktrees / name
        self.git("worktree", "add", "-q", "-b", name, str(path))
        return path

    def land(self, path: Path, *, fast_forward_local_main: bool) -> None:
        self.git("push", "-q", "origin", "HEAD:main", cwd=path)
        if fast_forward_local_main:
            self.git("merge", "-q", "--ff-only", "origin/main")

    def branches(self) -> list[str]:
        return self.git("branch", "--format=%(refname:short)").split()

    def registered_worktrees(self) -> list[str]:
        out = self.git("worktree", "list", "--porcelain")
        return [ln.split(" ", 1)[1] for ln in out.splitlines() if ln.startswith("worktree ")]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in _SEAM_ENVS:
        monkeypatch.delenv(name, raising=False)
    for key, value in _GIT_IDENTITY.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv(wc.CLEANUP_ENV, "1")
    monkeypatch.setenv(wc.REGISTRY_ENV, str(tmp_path / "registry"))
    monkeypatch.setenv(wc.CORE_REPO_ENV, str(tmp_path / "core-repo"))
    monkeypatch.setenv("CLAUDE_CANON_ROOTS_FILE", str(tmp_path / "canon-roots.local"))
    monkeypatch.setenv("CLAUDE_PROJECT_PLUGIN_DIR", str(tmp_path / "plugins"))
    return Repo(tmp_path).init()


def _run(**kw):
    kw.setdefault("cwd", None)
    kw.setdefault("delivery_worktree", None)
    return wc.run(SESSION, **kw)


def _kept(report: dict, path: Path) -> dict:
    hits = [e for e in report["kept"] if e["path"] == str(path)]
    assert len(hits) == 1, report
    return hits[0]


def _removed(report: dict, path: Path) -> dict:
    hits = [e for e in report["removed"] if e["path"] == str(path)]
    assert len(hits) == 1, report
    return hits[0]


def _decision(path: Path, **item_kw):
    item = wc.Item(str(path), item_kw.pop("kind", wc.KIND_GIT), item_kw.pop("backend", "git"),
                   item_kw.pop("name", None))
    return wc.classify(item, wc.observe(item))


# ── clean + landed ──────────────────────────────────────────────────────────

def test_clean_landed_registered_worktree_and_branch_are_removed(repo):
    wt = repo.worktree("feature")
    wc.register(SESSION, str(wt), backend="git", name="feature")

    report = _run()

    entry = _removed(report, wt)
    assert entry["kind"] == wc.KIND_GIT
    assert entry["branch"] == {"name": "feature", "deleted": True}
    assert not wt.exists()
    assert str(wt) not in repo.registered_worktrees()
    assert "feature" not in repo.branches()
    assert report["kept"] == [] and report["would_remove"] == []


def test_clean_landed_after_commit_pushed_and_main_fast_forwarded_deletes_branch(repo):
    wt = repo.worktree("shipped")
    repo.commit(wt, "work.txt", "work\n")
    repo.land(wt, fast_forward_local_main=True)
    wc.register(SESSION, str(wt), backend="git")

    report = _run()

    assert _removed(report, wt)["branch"] == {"name": "shipped", "deleted": True}
    assert "shipped" not in repo.branches()


def test_clean_landed_with_lagging_local_main_removes_worktree_keeps_branch_with_reason(repo):
    wt = repo.worktree("lagging")
    repo.commit(wt, "work.txt", "work\n")
    repo.land(wt, fast_forward_local_main=False)
    wc.register(SESSION, str(wt), backend="git")

    report = _run()

    branch = _removed(report, wt)["branch"]
    assert branch["name"] == "lagging" and branch["deleted"] is False and branch["reason"]
    assert not wt.exists()
    assert "lagging" in repo.branches()


def test_clean_landed_removal_forgets_the_registry_record(repo):
    wt = repo.worktree("forgotten")
    wc.register(SESSION, str(wt), backend="git")

    _run()
    second = _run()

    assert second["removed"] == [] and second["kept"] == []


def test_clean_landed_delivery_worktree_of_the_plan_is_a_candidate_without_registration(repo):
    wt = repo.worktree("planned")

    report = _run(delivery_worktree=str(wt))

    assert _removed(report, wt)["kind"] == wc.KIND_GIT
    assert not wt.exists()


# ── never removed: dirty / untracked / unlanded / not a linked worktree ─────

def test_dirty_worktree_is_kept(repo):
    wt = repo.worktree("dirty")
    (wt / "README").write_text("changed\n")
    wc.register(SESSION, str(wt), backend="git")

    decision, reason = _decision(wt)
    report = _run()

    assert (decision, reason) == (wc.KEEP, "uncommitted changes")
    assert _kept(report, wt)["reason"] == "uncommitted changes"
    assert (wt / "README").read_text() == "changed\n"
    assert report["removed"] == []


def test_untracked_files_keep_the_worktree(repo):
    wt = repo.worktree("untracked")
    (wt / "scratch.txt").write_text("not yet added\n")
    wc.register(SESSION, str(wt), backend="git")

    decision, reason = _decision(wt)
    report = _run()

    assert (decision, reason) == (wc.KEEP, "untracked files")
    assert _kept(report, wt)["reason"] == "untracked files"
    assert (wt / "scratch.txt").exists()


def test_untracked_file_inside_a_new_directory_keeps_the_worktree(repo):
    wt = repo.worktree("untracked-nested")
    (wt / "newdir").mkdir()
    (wt / "newdir" / "a.txt").write_text("x\n")
    wc.register(SESSION, str(wt), backend="git")

    decision, reason = _decision(wt)

    assert (decision, reason) == (wc.KEEP, "untracked files")
    assert _run()["removed"] == []
    assert (wt / "newdir" / "a.txt").exists()


def test_unlanded_commit_keeps_the_worktree_and_branch(repo):
    wt = repo.worktree("unlanded")
    repo.commit(wt, "wip.txt", "wip\n")
    wc.register(SESSION, str(wt), backend="git")

    decision, reason = _decision(wt)
    report = _run()

    assert (decision, reason) == (wc.KEEP, f"HEAD not an ancestor of {wc.TRUNK_REF}")
    assert _kept(report, wt)["reason"] == reason
    assert wt.exists() and "unlanded" in repo.branches()


def test_unlanded_without_a_trunk_ref_is_kept(repo):
    wt = repo.worktree("no-trunk")
    repo.git("update-ref", "-d", "refs/remotes/origin/main")
    wc.register(SESSION, str(wt), backend="git")

    decision, reason = _decision(wt)

    assert (decision, reason) == (wc.KEEP, f"{wc.TRUNK_REF} unknown")
    assert wt.exists()


def test_main_checkout_is_not_a_linked_worktree_and_is_kept(repo):
    wc.register(SESSION, str(repo.main), backend="git")

    decision, reason = _decision(repo.main)
    report = _run()

    assert (decision, reason) == (wc.KEEP, "not the root of a linked worktree")
    assert _kept(report, repo.main)["reason"] == reason
    assert repo.main.exists()


def test_subdirectory_of_a_worktree_is_kept(repo):
    wt = repo.worktree("with-sub")
    sub = wt / "sub"
    sub.mkdir()
    wc.register(SESSION, str(sub), backend="git")

    decision, _ = _decision(sub)

    assert decision == wc.KEEP
    assert _run()["removed"] == []
    assert sub.exists() and wt.exists()


def test_symlinked_workspace_is_kept(repo, tmp_path):
    wt = repo.worktree("real")
    link = tmp_path / "link"
    link.symlink_to(wt)
    wc.register(SESSION, str(link), backend="git")

    decision, reason = _decision(link)

    assert (decision, reason) == (wc.KEEP, "path is a symlink")
    assert wc.run(SESSION, cwd=None, delivery_worktree=None)["removed"] == []
    assert wt.exists() and link.is_symlink()


def test_absent_path_is_kept_and_its_record_forgotten(repo, tmp_path):
    gone = tmp_path / "wt" / "gone"
    wc.register(SESSION, str(gone), backend="git")

    first = _run()
    second = _run()

    assert _kept(first, gone)["reason"] == "path already absent"
    assert second["kept"] == []


# ── protected roots ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("protect", [
    "home", "canon", "canon-roots-file", "anchor", "store", "contains-anchor",
])
def test_protected_path_is_kept(repo, tmp_path, monkeypatch, protect):
    wt = repo.worktree("prot")
    expected = {"home": "home", "canon": "canon", "canon-roots-file": "canon",
                "anchor": "anchor", "store": "store", "contains-anchor": "anchor"}[protect]
    if protect == "home":
        monkeypatch.setenv("HOME", str(wt))
    elif protect == "canon":
        monkeypatch.setenv(wc.CORE_REPO_ENV, str(wt))
    elif protect == "canon-roots-file":
        (tmp_path / "canon-roots.local").write_text(f"# machine roots\n{repo.worktrees}\n")
    elif protect == "anchor":
        monkeypatch.setenv(wc.ANCHOR_ROOTS_ENV, str(repo.worktrees))
    elif protect == "store":
        monkeypatch.setenv(wc.STORE_ROOTS_ENV, str(repo.worktrees))
    else:
        monkeypatch.setenv(wc.ANCHOR_ROOTS_ENV, str(wt / "inner-anchor"))
    wc.register(SESSION, str(wt), backend="git")

    decision, reason = _decision(wt)
    report = _run()

    assert (decision, reason) == (wc.KEEP, f"protected path ({expected})")
    assert _kept(report, wt)["reason"] == reason
    assert report["removed"] == []
    assert wt.exists() and str(wt) in repo.registered_worktrees()


def test_protected_path_refusal_of_execute_is_independent_of_classify(repo, monkeypatch):
    wt = repo.worktree("guarded")
    monkeypatch.setenv(wc.ANCHOR_ROOTS_ENV, str(repo.worktrees))
    item = wc.Item(str(wt), wc.KIND_GIT, "git", None)
    lying = wc.Observation(linked_worktree=True, trunk_known=True, landed=True,
                           common_dir=str(repo.main / ".git"), branch="guarded")

    outcome = wc.execute(item, lying, dry_run=False)

    assert outcome.status == "kept" and outcome.reason == "protected path (anchor)"
    assert wt.exists()


# ── dry run ─────────────────────────────────────────────────────────────────

def test_dry_run_reports_would_remove_and_touches_nothing(repo):
    wt = repo.worktree("dry")
    wc.register(SESSION, str(wt), backend="git")

    report = _run(dry_run=True)

    assert report["dry_run"] is True
    assert [e["path"] for e in report["would_remove"]] == [str(wt)]
    assert report["removed"] == []
    assert wt.exists() and "dry" in repo.branches()
    assert [e["path"] for e in _run(dry_run=True)["would_remove"]] == [str(wt)]


def test_dry_run_from_the_environment_seam(repo, monkeypatch):
    wt = repo.worktree("dry-env")
    wc.register(SESSION, str(wt), backend="git")
    monkeypatch.setenv("CLAUDE_DRY_RUN", "1")

    report = _run()

    assert report["dry_run"] is True and len(report["would_remove"]) == 1
    assert wt.exists()


def test_dry_run_does_not_consume_a_pending_record(repo):
    wt = repo.worktree("dry-pending")
    pending = wc.register_pending(str(wt), backend="git")

    report = _run(cwd=str(wt), dry_run=True)

    assert [e["path"] for e in report["would_remove"]] == [str(wt)]
    assert pending.exists()


def test_disabled_by_environment_does_nothing(repo, monkeypatch):
    wt = repo.worktree("off")
    wc.register(SESSION, str(wt), backend="git")
    monkeypatch.setenv(wc.CLEANUP_ENV, "0")

    report = _run()

    assert report["enabled"] is False
    assert report["removed"] == [] and report["kept"] == []
    assert wt.exists()


# ── registration: only registered or bound workspaces are candidates ────────

def test_unregistered_clean_landed_worktree_is_never_touched(repo):
    wt = repo.worktree("stranger")

    report = _run(cwd=str(repo.main))

    assert report["removed"] == [] and report["kept"] == [] and report["would_remove"] == []
    assert wt.exists()


def test_pending_bound_by_resolve_cwd_is_cleaned_and_consumed(repo):
    wt = repo.worktree("pending")
    pending = wc.register_pending(str(wt), backend="git", name="pending")
    inside = wt / "deep" / "dir"
    inside.mkdir(parents=True)

    report = _run(cwd=str(inside))

    assert not wt.exists()
    assert _removed(report, wt)["branch"]["deleted"] is True
    assert not pending.exists()
    assert _run()["removed"] == []


def test_pending_bound_by_delivery_worktree(repo):
    wt = repo.worktree("pending-plan")
    pending = wc.register_pending(str(wt), backend="git")

    report = _run(cwd=str(repo.main), delivery_worktree=str(wt))

    assert _removed(report, wt)
    assert not pending.exists()


def test_pending_foreign_record_is_left_alone(repo):
    foreign = repo.worktree("someone-else")
    mine = repo.worktree("mine")
    pending = wc.register_pending(str(foreign), backend="git")
    wc.register(SESSION, str(mine), backend="git")

    report = _run(cwd=str(mine))

    assert _removed(report, mine)
    assert foreign.exists()
    assert pending.exists()
    assert all(e["path"] != str(foreign) for e in report["kept"] + report["removed"])


def test_pending_bound_but_dirty_moves_into_the_session_and_is_kept(repo):
    wt = repo.worktree("pending-dirty")
    (wt / "x.txt").write_text("x\n")
    wc.register_pending(str(wt), backend="git")

    first = _run(cwd=str(wt))
    (wt / "x.txt").unlink()
    second = _run(cwd=str(repo.main))

    assert _kept(first, wt)["reason"] == "untracked files"
    assert _removed(second, wt)


def test_register_rejects_an_unknown_kind(repo, tmp_path):
    with pytest.raises(ValueError):
        wc.register(SESSION, str(tmp_path), kind="bogus")
    with pytest.raises(ValueError):
        wc.register_pending(str(tmp_path), kind="bogus")


# ── mounts (backend teardown) and scratch ───────────────────────────────────

def _teardown_stub(tmp_path: Path, *, exit_code: int = 0) -> tuple[Path, Path]:
    log = tmp_path / "teardown.log"
    stub = tmp_path / "teardown-stub.sh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        '[[ "$1" == "--probe" ]] && exit 0\n'
        f'printf "%s\\n" "$1" >> "{log}"\n'
        f'[[ {exit_code} -eq 0 ]] || {{ echo "refused: unpushed work" >&2; exit {exit_code}; }}\n'
    )
    stub.chmod(0o755)
    return stub, log


def test_mount_is_removed_through_the_backend_teardown(repo, tmp_path, monkeypatch):
    mount = tmp_path / "mounts" / "task-1"
    mount.mkdir(parents=True)
    stub, log = _teardown_stub(tmp_path)
    monkeypatch.setenv(wc.TEARDOWN_ENV, str(stub))
    wc.register(SESSION, str(mount), backend="arc", name="task-1")

    report = _run()

    assert _removed(report, mount)["kind"] == wc.KIND_MOUNT
    assert log.read_text().split() == ["task-1"]


def test_mount_teardown_refusal_is_kept_with_its_reason(repo, tmp_path, monkeypatch):
    mount = tmp_path / "mounts" / "task-2"
    mount.mkdir(parents=True)
    stub, _ = _teardown_stub(tmp_path, exit_code=3)
    monkeypatch.setenv(wc.TEARDOWN_ENV, str(stub))
    wc.register(SESSION, str(mount), backend="arc", name="task-2")

    report = _run()

    assert "unpushed work" in _kept(report, mount)["reason"]
    assert report["removed"] == []


def test_mount_without_an_available_backend_teardown_is_kept(repo, tmp_path):
    mount = tmp_path / "mounts" / "task-3"
    mount.mkdir(parents=True)
    wc.register(SESSION, str(mount), backend="no-such-backend", name="task-3")

    report = _run()

    assert _kept(report, mount)["reason"] == "no teardown backend available"
    assert mount.exists()


def test_mount_under_a_store_root_is_protected(repo, tmp_path, monkeypatch):
    mount = tmp_path / "mounts" / "task-4"
    mount.mkdir(parents=True)
    stub, log = _teardown_stub(tmp_path)
    monkeypatch.setenv(wc.TEARDOWN_ENV, str(stub))
    monkeypatch.setenv(wc.STORE_ROOTS_ENV, str(tmp_path / "mounts"))
    wc.register(SESSION, str(mount), backend="arc", name="task-4")

    report = _run()

    assert _kept(report, mount)["reason"] == "protected path (store)"
    assert not log.exists()


def test_scratch_under_an_allowed_root_is_removed_and_the_root_itself_is_not(repo, tmp_path, monkeypatch):
    root = tmp_path / "scratch-root"
    scratch = root / "session"
    scratch.mkdir(parents=True)
    (scratch / "note.txt").write_text("n\n")
    monkeypatch.setenv(wc.SCRATCH_ROOTS_ENV, str(root))
    wc.register(SESSION, str(scratch), kind=wc.KIND_SCRATCH)
    wc.register(SESSION, str(root), kind=wc.KIND_SCRATCH)

    report = _run()

    assert _removed(report, scratch)["kind"] == wc.KIND_SCRATCH
    assert _kept(report, root)["reason"] == "scratch outside the allowed roots"
    assert not scratch.exists() and root.exists()


def test_scratch_outside_the_allowed_roots_is_kept(repo, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv(wc.SCRATCH_ROOTS_ENV, str(tmp_path / "scratch-root"))
    wc.register(SESSION, str(elsewhere), kind=wc.KIND_SCRATCH)

    report = _run()

    assert _kept(report, elsewhere)["reason"] == "scratch outside the allowed roots"
    assert elsewhere.exists()


# ── fail-open ───────────────────────────────────────────────────────────────

def test_fail_open_observation_fault_is_kept_and_the_next_item_is_still_processed(repo, monkeypatch):
    first = repo.worktree("faulty")
    second = repo.worktree("healthy")
    wc.register(SESSION, str(first), backend="git")
    wc.register(SESSION, str(second), backend="git")
    real_observe = wc.observe

    def flaky(item):
        if item.path == str(first):
            raise RuntimeError("disk on fire")
        return real_observe(item)

    monkeypatch.setattr(wc, "observe", flaky)

    report = _run()

    assert "disk on fire" in _kept(report, first)["reason"]
    assert first.exists()
    assert _removed(report, second)


def test_fail_open_git_binary_missing_keeps_everything(repo, monkeypatch):
    wt = repo.worktree("no-git")
    wc.register(SESSION, str(wt), backend="git")
    monkeypatch.setenv(wc.GIT_BIN_ENV, str(repo.tmp / "no-such-git"))

    report = _run()

    assert report["removed"] == []
    assert _kept(report, wt)["reason"].startswith("not a git checkout")
    assert wt.exists()


def test_fail_open_git_refusal_of_a_locked_worktree_is_reported_as_kept(repo):
    wt = repo.worktree("locked")
    repo.git("worktree", "lock", str(wt))
    wc.register(SESSION, str(wt), backend="git")

    report = _run()

    assert _kept(report, wt)["reason"].startswith("git refused the removal")
    assert wt.exists()


def test_branch_removal_never_deletes_a_trunk_branch():
    obs = wc.Observation(branch="main", common_dir="/nonexistent/.git")

    assert wc._remove_branch(obs) == {"name": "main", "deleted": False,
                                     "reason": "trunk branch is never deleted"}


# ── the workspace-register verb ─────────────────────────────────────────────

def test_workspace_register_verb_writes_a_session_record(repo, store, tmp_path):
    target = tmp_path / "wt" / "verb"

    d = cli.cmd_workspace_register(
        ns(path=str(target), kind=None, ws_backend="git", ws_name="verb",
           pending=False, session=SESSION), store=store)

    assert d.ok is True and d.data["scope"] == "session"
    assert [i.path for _, i in wc._read_dir(wc._session_dir(SESSION, None))] == [str(target)]


def test_workspace_register_verb_pending_needs_no_session(repo, store, tmp_path):
    target = tmp_path / "wt" / "verb-pending"

    d = cli.cmd_workspace_register(
        ns(path=str(target), kind=None, ws_backend=None, ws_name=None,
           pending=True, session=None), store=store)

    assert d.ok is True and d.data["scope"] == "pending"
    assert [i.path for _, i in wc._read_dir(wc._pending_dir(None))] == [str(target)]


def test_workspace_register_verb_without_session_or_pending_is_refused(repo, store, tmp_path):
    d = cli.cmd_workspace_register(
        ns(path=str(tmp_path), kind=None, ws_backend=None, ws_name=None,
           pending=False, session=None), store=store)

    assert d.ok is False
