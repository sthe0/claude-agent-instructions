"""Tests for the `git-worktrees` reaper (scripts/reaper/builtin/git_worktrees.py).

Hermetic: every scenario runs against throw-away git repositories under tmp_path (a bare
origin, a main checkout, linked worktrees), with the git config, identity, TMPDIR and the
reaper's own repository redirected there. Nothing touches the real repository, the real
scope registry or ~/.claude-agent.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaper.builtin import git_worktrees as gw  # noqa: E402
from reaper.contract import KEEP, REMOVE, ReapContext, path_owned  # noqa: E402
from session_scope.registry import ScopeRecord  # noqa: E402

HOUR = 3600.0


class Repo:
    def __init__(self, root: Path):
        self.root = root
        self.origin = root / "origin.git"
        self.main = root / "main-checkout"
        self.worktrees = root / "wts"
        self.worktrees.mkdir()
        self.git(root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.git(root, "init", "-q", "-b", "main", str(self.main))
        self.git(self.main, "remote", "add", "origin", str(self.origin))
        self.commit(self.main, "base.txt", "base\n", "base")
        self.git(self.main, "push", "-q", "origin", "main")

    def git(self, cwd: Path, *args: str) -> str:
        out = subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True,
        )
        return out.stdout

    def commit(self, cwd: Path, name: str, content: str, message: str) -> str:
        (Path(cwd) / name).write_text(content, encoding="utf-8")
        self.git(cwd, "add", name)
        self.git(cwd, "commit", "-q", "-m", message)
        return self.git(cwd, "rev-parse", "HEAD").strip()

    def add_worktree(self, name: str, branch: "str | None" = None, base: str = "main") -> Path:
        path = self.worktrees / name
        if branch:
            self.git(self.main, "worktree", "add", "-q", "-b", branch, str(path), base)
        else:
            self.git(self.main, "worktree", "add", "-q", "--detach", str(path), base)
        return path.resolve()

    def land_fast_forward(self, branch: str) -> None:
        self.git(self.main, "merge", "-q", "--ff-only", branch)
        self.git(self.main, "push", "-q", "origin", "main")

    def land_by_cherry_pick(self, sha: str) -> None:
        self.git(self.main, "cherry-pick", sha)
        self.git(self.main, "push", "-q", "origin", "main")


def age(path: Path, hours: float) -> None:
    stamp = time.time() - hours * HOUR
    os.utime(path, (stamp, stamp))


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Repo:
    root = tmp_path.resolve() / "world"
    root.mkdir()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "Reaper Test")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "reaper-test@example.invalid")
    monkeypatch.setenv("TMPDIR", str(root / "tmp"))
    monkeypatch.setenv("HOME", str(root / "home"))
    made = Repo(root)
    monkeypatch.setattr(gw, "REPO_ROOT", str(made.main))
    monkeypatch.setattr(gw, "_RUNNER_CHECKOUT", str(made.main))
    return made


def make_ctx(repo: Repo, *, records=None, dry_run=False, due=True) -> ReapContext:
    return ReapContext(
        now=time.time(),
        dry_run=dry_run,
        project_dir=repo.main,
        deadletter_dir=repo.root / "deadletter",
        scope_records=list(records or []),
        due=due,
    )


def verdicts_by_path(repo: Repo, **kwargs):
    return {v.path: v for v in gw.scan(make_ctx(repo, **kwargs))}


def branch_exists(repo: Repo, branch: str) -> bool:
    return bool(repo.git(repo.main, "branch", "--list", branch).strip())


# ── landed branch worktrees are removed ───────────────────────────────────

def test_fast_forward_landed_branch_worktree_is_removed_with_its_branch(repo):
    wt = repo.add_worktree("ff", branch="feat-ff")
    sha = repo.commit(wt, "ff.txt", "ff\n", "ff work")
    repo.land_fast_forward("feat-ff")
    age(wt, 48)
    ctx = make_ctx(repo)
    verdict = {v.path: v for v in gw.scan(ctx)}[str(wt)]
    assert (verdict.action, verdict.report) == (REMOVE, False)

    gw.remove(str(wt), ctx)

    assert not wt.exists()
    assert not branch_exists(repo, "feat-ff")
    line = (ctx.deadletter_dir / gw.REAPED_BRANCHES_LOG).read_text(encoding="utf-8").strip()
    assert line.split(" ", 1)[1] == f"feat-ff {sha} {wt}"


def test_rebase_landed_branch_worktree_is_removed(repo):
    wt = repo.add_worktree("rb", branch="feat-rb")
    sha = repo.commit(wt, "rb.txt", "rb\n", "rb work")
    repo.commit(repo.main, "elsewhere.txt", "elsewhere\n", "unrelated trunk work")
    repo.land_by_cherry_pick(sha)
    age(wt, 48)
    assert repo.git(repo.main, "cherry", "origin/main", "feat-rb").startswith("- ")
    assert verdicts_by_path(repo)[str(wt)].action == REMOVE


def test_branch_without_commits_of_its_own_is_removed(repo):
    wt = repo.add_worktree("empty", branch="feat-empty")
    age(wt, 48)
    assert verdicts_by_path(repo)[str(wt)].action == REMOVE


def test_reaped_branch_is_logged_before_branch_delete_runs(repo, monkeypatch):
    wt = repo.add_worktree("order", branch="feat-order")
    age(wt, 48)
    ctx = make_ctx(repo)
    real = gw._git_checked

    def fail_on_branch_delete(cwd, *args):
        if args[:1] == ("branch",):
            raise RuntimeError("branch -D failed")
        return real(cwd, *args)

    monkeypatch.setattr(gw, "_git_checked", fail_on_branch_delete)
    with pytest.raises(RuntimeError):
        gw.remove(str(wt), ctx)
    log = (ctx.deadletter_dir / gw.REAPED_BRANCHES_LOG).read_text(encoding="utf-8")
    assert "feat-order" in log
    assert not wt.exists()


def test_reaped_branch_is_logged_before_the_worktree_is_removed(repo, monkeypatch):
    wt = repo.add_worktree("first", branch="feat-first")
    sha = repo.git(wt, "rev-parse", "HEAD").strip()
    age(wt, 48)
    ctx = make_ctx(repo)
    real = gw._git_checked
    log_at_worktree_remove = []

    def watch(cwd, *args):
        if args[:2] == ("worktree", "remove"):
            log = ctx.deadletter_dir / gw.REAPED_BRANCHES_LOG
            log_at_worktree_remove.append(log.read_text(encoding="utf-8") if log.exists() else "")
        return real(cwd, *args)

    monkeypatch.setattr(gw, "_git_checked", watch)
    gw.remove(str(wt), ctx)
    assert len(log_at_worktree_remove) == 1
    assert f"feat-first {sha} {wt}" in log_at_worktree_remove[0]


def test_remove_refuses_a_worktree_that_turned_dirty_after_the_scan(repo):
    wt = repo.add_worktree("late", branch="feat-late")
    age(wt, 48)
    ctx = make_ctx(repo)
    assert verdicts_by_path(repo)[str(wt)].action == REMOVE
    (wt / "late.txt").write_text("new work\n", encoding="utf-8")
    with pytest.raises(RuntimeError):
        gw.remove(str(wt), ctx)
    assert wt.exists()
    assert branch_exists(repo, "feat-late")


# ── kept branch worktrees ─────────────────────────────────────────────────

def test_unlanded_branch_worktree_is_kept_with_the_commit_count(repo):
    wt = repo.add_worktree("ahead", branch="feat-ahead")
    repo.commit(wt, "a.txt", "a\n", "a")
    repo.commit(wt, "b.txt", "b\n", "b")
    age(wt, 48)
    verdict = verdicts_by_path(repo)[str(wt)]
    assert (verdict.action, verdict.reason) == (KEEP, "unlanded 2")


def test_dirty_tracked_change_keeps_the_branch_worktree(repo):
    wt = repo.add_worktree("dt", branch="feat-dt")
    (wt / "base.txt").write_text("edited\n", encoding="utf-8")
    age(wt, 48)
    verdict = verdicts_by_path(repo)[str(wt)]
    assert (verdict.action, verdict.reason) == (KEEP, "dirty")


def test_untracked_file_keeps_the_branch_worktree(repo):
    wt = repo.add_worktree("du", branch="feat-du")
    (wt / "scratch.txt").write_text("x\n", encoding="utf-8")
    age(wt, 48)
    verdict = verdicts_by_path(repo)[str(wt)]
    assert (verdict.action, verdict.reason) == (KEEP, "dirty")


def test_fresh_branch_worktree_is_kept(repo):
    wt = repo.add_worktree("fresh", branch="feat-fresh")
    age(wt, 2)
    verdict = verdicts_by_path(repo)[str(wt)]
    assert verdict.action == KEEP and verdict.reason.startswith("fresh")


def test_owned_branch_worktree_is_kept(repo):
    wt = repo.add_worktree("owned", branch="feat-owned")
    age(wt, 48)
    record = ScopeRecord(session_id="s1", cwd=str(wt), pid=os.getpid(), heartbeat_ts=0.0)
    verdict = verdicts_by_path(repo, records=[record])[str(wt)]
    assert verdict.action == KEEP and verdict.reason.startswith("owned")


def test_owner_record_spelled_through_a_symlink_still_owns_the_worktree(repo):
    wt = repo.add_worktree("viasym", branch="feat-viasym")
    age(wt, 48)
    link = repo.root / "link-to-wt"
    link.symlink_to(wt)
    record = ScopeRecord(session_id="s1", cwd=str(link), pid=os.getpid(), heartbeat_ts=0.0)
    assert verdicts_by_path(repo, records=[record])[str(wt)].reason.startswith("owned")


def test_missing_trunk_ref_keeps_the_branch_worktree_instead_of_failing_the_scan(repo):
    wt = repo.add_worktree("notrunk", branch="feat-notrunk")
    age(wt, 48)
    repo.git(repo.main, "update-ref", "-d", "refs/remotes/origin/main")
    verdict = verdicts_by_path(repo)[str(wt)]
    assert verdict.action == KEEP and verdict.reason.startswith("error")


def test_main_checkout_is_never_listed_and_a_second_main_worktree_is_kept(repo):
    second = repo.worktrees / "second-main"
    repo.git(repo.main, "worktree", "add", "-q", "--force", str(second), "main")
    second = second.resolve()
    age(second, 48)
    found = verdicts_by_path(repo)
    assert str(repo.main) not in found
    assert (found[str(second)].action, found[str(second)].reason) == (KEEP, "main branch")


def test_locked_worktree_is_kept(repo):
    wt = repo.add_worktree("locked", branch="feat-locked")
    age(wt, 48)
    repo.git(repo.main, "worktree", "lock", str(wt))
    assert verdicts_by_path(repo)[str(wt)].reason == "locked"


def test_missing_directory_is_kept(repo):
    wt = repo.add_worktree("gone", branch="feat-gone")
    shutil.rmtree(wt)
    verdict = verdicts_by_path(repo)[str(wt)]
    assert (verdict.action, verdict.reason) == (KEEP, "missing directory")


# ── summary ───────────────────────────────────────────────────────────────

def test_summary_names_a_stale_unlanded_worktree_older_than_a_week(repo):
    wt = repo.add_worktree("old", branch="feat-old")
    repo.commit(wt, "o.txt", "o\n", "o")
    age(wt, 8 * 24)
    verdicts = gw.scan(make_ctx(repo))
    line = gw.summary(verdicts)
    assert line is not None and "1 branch worktree" in line


def test_summary_is_silent_within_a_week(repo):
    wt = repo.add_worktree("recent", branch="feat-recent")
    repo.commit(wt, "r.txt", "r\n", "r")
    age(wt, 3 * 24)
    assert gw.summary(gw.scan(make_ctx(repo))) is None


# ── scan coverage ─────────────────────────────────────────────────────────

def test_scan_returns_one_verdict_per_porcelain_entry_except_the_first(repo):
    branch_wt = repo.add_worktree("b", branch="feat-b")
    detached_in_temp = repo.add_worktree("d1")
    prunable = repo.add_worktree("d2")
    shutil.rmtree(prunable)
    second_main = repo.worktrees / "m2"
    repo.git(repo.main, "worktree", "add", "-q", "--force", str(second_main), "main")
    entries = gw.parse_worktree_porcelain(repo.git(repo.main, "worktree", "list", "--porcelain"))
    verdicts = gw.scan(make_ctx(repo, dry_run=True))
    assert [v.path for v in verdicts] == [e.path for e in entries[1:]]
    assert len(verdicts) == 4
    assert {str(branch_wt), str(detached_in_temp), str(prunable)} <= {v.path for v in verdicts}


def test_detached_worktree_outside_the_temp_root_is_kept(repo):
    wt = repo.add_worktree("outside")
    age(wt, 48)
    verdict = verdicts_by_path(repo)[str(wt)]
    assert (verdict.action, verdict.reason) == (KEEP, "not under temp root")


# ── detached worktrees under the temp root ────────────────────────────────

def detached_under_temp(repo: Repo, name: str) -> Path:
    temp = Path(os.environ["TMPDIR"])
    temp.mkdir(parents=True, exist_ok=True)
    path = temp / name
    repo.git(repo.main, "worktree", "add", "-q", "--detach", str(path), "main")
    return path.resolve()


def test_stale_clean_detached_worktree_under_temp_root_is_removed(repo):
    wt = detached_under_temp(repo, "scratch")
    age(wt, 48)
    ctx = make_ctx(repo)
    assert verdicts_by_path(repo)[str(wt)].action == REMOVE
    gw.remove(str(wt), ctx)
    assert not wt.exists()


def test_symlinked_temp_root_matches_the_resolved_worktree_path(repo, monkeypatch):
    real_dir = repo.root / "real-tmp"
    real_dir.mkdir()
    link = repo.root / "tmp-link"
    link.symlink_to(real_dir)
    monkeypatch.setenv("TMPDIR", str(link))
    path = real_dir / "viasym"
    repo.git(repo.main, "worktree", "add", "-q", "--detach", str(path), "main")
    age(path.resolve(), 48)
    assert verdicts_by_path(repo)[str(path.resolve())].action == REMOVE


def test_detached_worktree_with_a_commit_not_in_trunk_is_kept(repo):
    wt = detached_under_temp(repo, "unlanded")
    repo.commit(wt, "scratch.txt", "scratch\n", "scratch work")
    age(wt, 48)
    verdict = verdicts_by_path(repo)[str(wt)]
    assert (verdict.action, verdict.reason) == (KEEP, f"detached HEAD not in {gw.TRUNK_REF}")


def test_detached_worktree_that_turned_dirty_after_the_scan_survives_the_pass(repo):
    import io
    import types

    from reaper import registry, runner

    wt = detached_under_temp(repo, "latedetached")
    age(wt, 48)
    ctx = make_ctx(repo)

    def scan_then_dirty(scan_ctx):
        verdicts = gw.scan(scan_ctx)
        (wt / "base.txt").write_text("edited after scan\n", encoding="utf-8")
        return verdicts

    module = types.SimpleNamespace(NAME=gw.NAME, scan=scan_then_dirty, remove=gw.remove)
    reaper = registry.Reaper(gw.NAME, "builtin", gw.__file__, module)
    err = io.StringIO()
    runner.execute_pass(
        [reaper], ctx, dry_run=False, force_run=True, only=None,
        stamps=repo.root / "stamps", log_path=repo.root / "removed.jsonl", out=io.StringIO(), err=err,
    )
    assert "FAILED to remove" in err.getvalue()
    assert (wt / "base.txt").read_text(encoding="utf-8") == "edited after scan\n"


def test_dirty_detached_worktree_is_kept_and_dead_lettered(repo):
    wt = detached_under_temp(repo, "dirty")
    (wt / "base.txt").write_text("edited\n", encoding="utf-8")
    age(wt, 48)
    ctx = make_ctx(repo)
    verdict = {v.path: v for v in gw.scan(ctx)}[str(wt)]
    assert verdict.action == KEEP and "dirty" in verdict.reason
    snapshots = list(ctx.deadletter_dir.glob("dirty-*.diff"))
    assert len(snapshots) == 1 and "edited" in snapshots[0].read_text(encoding="utf-8")


def test_dry_run_scan_writes_no_dead_letter(repo):
    wt = detached_under_temp(repo, "dirtydry")
    (wt / "base.txt").write_text("edited\n", encoding="utf-8")
    age(wt, 48)
    ctx = make_ctx(repo, dry_run=True)
    gw.scan(ctx)
    assert not ctx.deadletter_dir.exists()


def test_scan_that_is_not_due_writes_no_dead_letter(repo):
    wt = detached_under_temp(repo, "dirtyidle")
    (wt / "base.txt").write_text("edited\n", encoding="utf-8")
    age(wt, 48)
    ctx = make_ctx(repo, due=False)
    gw.scan(ctx)
    assert not ctx.deadletter_dir.exists()


# ── remove guards ─────────────────────────────────────────────────────────

def test_remove_refuses_a_path_that_is_not_a_linked_worktree(repo):
    with pytest.raises(RuntimeError):
        gw.remove(str(repo.main), make_ctx(repo))


def test_remove_refuses_a_worktree_on_branch_main(repo):
    second = repo.worktrees / "second-main"
    repo.git(repo.main, "worktree", "add", "-q", "--force", str(second), "main")
    with pytest.raises(RuntimeError):
        gw.remove(str(second), make_ctx(repo))
    assert second.exists()


# ── pure helpers ported from the earlier sweep ────────────────────────────

def test_parse_branch_worktree():
    wts = gw.parse_worktree_porcelain("worktree /home/the0/cai-main\nHEAD abc123\nbranch refs/heads/main\n")
    assert len(wts) == 1
    assert (wts[0].path, wts[0].branch, wts[0].detached, wts[0].bare) == (
        "/home/the0/cai-main", "refs/heads/main", False, False,
    )


def test_parse_detached_and_locked_worktree():
    wts = gw.parse_worktree_porcelain("worktree /tmp/cc-scratch/x\nHEAD deadbeef\ndetached\nlocked reason\n")
    assert (wts[0].detached, wts[0].branch, wts[0].locked) == (True, None, True)


def test_parse_multiple_blocks_blank_separated():
    wts = gw.parse_worktree_porcelain(
        "worktree /a\nHEAD 111\nbranch refs/heads/main\n\nworktree /b\nHEAD 222\ndetached\n"
    )
    assert [w.path for w in wts] == ["/a", "/b"]
    assert [w.detached for w in wts] == [False, True]


def test_parse_bare_and_empty():
    assert gw.parse_worktree_porcelain("worktree /repo.git\nbare\n")[0].bare is True
    assert gw.parse_worktree_porcelain("") == []


def test_is_temp_root_matches_root_and_descendant_but_not_a_sibling_prefix():
    roots = ["/tmp/cc-scratch"]
    assert gw.is_temp_root("/tmp/cc-scratch", roots)
    assert gw.is_temp_root("/tmp/cc-scratch/foo", roots)
    assert not gw.is_temp_root("/tmp/cc-scratch-evil", roots)
    assert not gw.is_temp_root("/home/the0/cai-main", roots)


def test_path_owned_live_pid_regardless_of_heartbeat_age():
    rec = ScopeRecord(session_id="s1", cwd="/tmp/cc-scratch/wt", pid=os.getpid(), heartbeat_ts=0.0)
    assert path_owned("/tmp/cc-scratch/wt", [rec], 10_000_000.0) is True


def test_path_owned_fresh_heartbeat_without_pid():
    now = 1_000_000.0
    rec = ScopeRecord(session_id="s1", cwd="/tmp/cc-scratch/wt", pid=None, heartbeat_ts=now - 60)
    assert path_owned("/tmp/cc-scratch/wt", [rec], now) is True


def test_path_owned_false_for_dead_pid_and_stale_heartbeat():
    now = 1_000_000.0
    rec = ScopeRecord(session_id="s1", cwd="/tmp/cc-scratch/wt", pid=999_999_999, heartbeat_ts=now - 999_999)
    assert path_owned("/tmp/cc-scratch/wt", [rec], now) is False


def test_path_owned_matches_subdirectory_cwd_and_repo_root():
    inside = ScopeRecord(session_id="s1", cwd="/tmp/cc-scratch/wt/sub", pid=os.getpid(), heartbeat_ts=0.0)
    root = ScopeRecord(session_id="s2", cwd=None, repo_root="/tmp/cc-scratch/wt", pid=os.getpid(), heartbeat_ts=0.0)
    other = ScopeRecord(session_id="s3", cwd="/tmp/cc-scratch/other", pid=os.getpid(), heartbeat_ts=0.0)
    assert path_owned("/tmp/cc-scratch/wt", [inside], 1.0) is True
    assert path_owned("/tmp/cc-scratch/wt", [root], 1.0) is True
    assert path_owned("/tmp/cc-scratch/wt", [other], 1.0) is False


@pytest.mark.parametrize(
    "age_h,dirty,owned,verdict,word",
    [
        (48, False, False, REMOVE, "detached"),
        (1.0, False, False, KEEP, "fresh"),
        (48, False, True, KEEP, "owned"),
        (48, True, False, KEEP, "dirty"),
        (48, True, True, KEEP, "owned"),
    ],
)
def test_classify_detached_verdict_table(age_h, dirty, owned, verdict, word):
    got, reason = gw.classify_detached(age_h, dirty, owned)
    assert got == verdict and word in reason
