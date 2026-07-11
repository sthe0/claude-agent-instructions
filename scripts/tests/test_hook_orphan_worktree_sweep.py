"""Unit tests for hook-orphan-worktree-sweep.py's pure decision functions.

Exercises parse_worktree_porcelain (against captured `git worktree list
--porcelain` text, both branch and detached shapes), is_temp_root, is_owned
(against synthetic ScopeRecord objects — no real scope files on disk), and
classify (the removal-verdict gate, driven with precomputed age/dirty/owned
so it needs no filesystem or git access). The crafted end-to-end scenarios
(actual removal, fresh-kept, owned-kept, dirty-kept, throttle) are covered
separately by the plan's shell verify_command, which drives real git worktrees.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_HOOK_PATH = Path(__file__).resolve().parent.parent / "hook-orphan-worktree-sweep.py"
_spec = importlib.util.spec_from_file_location("orphan_sweep", _HOOK_PATH)
sweep = importlib.util.module_from_spec(_spec)
sys.modules["orphan_sweep"] = sweep  # dataclass string-annotation resolution needs this
_spec.loader.exec_module(sweep)

from session_scope.registry import ScopeRecord  # noqa: E402


# ── parse_worktree_porcelain ────────────────────────────────────────────────

def test_parse_branch_worktree():
    text = "worktree /home/the0/cai-main\nHEAD abc123\nbranch refs/heads/main\n"
    wts = sweep.parse_worktree_porcelain(text)
    assert len(wts) == 1
    assert wts[0].path == "/home/the0/cai-main"
    assert wts[0].branch == "refs/heads/main"
    assert wts[0].detached is False
    assert wts[0].bare is False


def test_parse_detached_worktree():
    text = "worktree /tmp/cc-scratch/x\nHEAD deadbeef\ndetached\n"
    wts = sweep.parse_worktree_porcelain(text)
    assert len(wts) == 1
    assert wts[0].detached is True
    assert wts[0].branch is None


def test_parse_multiple_blocks_blank_separated():
    text = (
        "worktree /a\nHEAD 111\nbranch refs/heads/main\n"
        "\n"
        "worktree /b\nHEAD 222\ndetached\n"
    )
    wts = sweep.parse_worktree_porcelain(text)
    assert [w.path for w in wts] == ["/a", "/b"]
    assert wts[0].detached is False
    assert wts[1].detached is True


def test_parse_bare_worktree():
    text = "worktree /repo.git\nbare\n"
    wts = sweep.parse_worktree_porcelain(text)
    assert wts[0].bare is True


def test_parse_empty_text():
    assert sweep.parse_worktree_porcelain("") == []


# ── is_temp_root ─────────────────────────────────────────────────────────

def test_is_temp_root_matches_root_itself():
    assert sweep.is_temp_root("/tmp/cc-scratch", ["/tmp/cc-scratch"]) is True


def test_is_temp_root_matches_descendant():
    assert sweep.is_temp_root("/tmp/cc-scratch/foo", ["/tmp/cc-scratch"]) is True


def test_is_temp_root_rejects_sibling_prefix():
    # /tmp/cc-scratch-evil must NOT match root /tmp/cc-scratch (prefix, not path).
    assert sweep.is_temp_root("/tmp/cc-scratch-evil", ["/tmp/cc-scratch"]) is False


def test_is_temp_root_rejects_unrelated_path():
    assert sweep.is_temp_root("/home/the0/cai-main", ["/tmp/cc-scratch"]) is False


# ── is_owned ─────────────────────────────────────────────────────────────

def test_is_owned_true_for_live_pid_regardless_of_heartbeat_age():
    rec = ScopeRecord(session_id="s1", cwd="/tmp/cc-scratch/wt", pid=os.getpid(), heartbeat_ts=0.0)
    assert sweep.is_owned("/tmp/cc-scratch/wt", [rec], now_ts=10_000_000.0) is True


def test_is_owned_true_for_fresh_heartbeat_without_pid():
    now = 1_000_000.0
    rec = ScopeRecord(session_id="s1", cwd="/tmp/cc-scratch/wt", pid=None, heartbeat_ts=now - 60)
    assert sweep.is_owned("/tmp/cc-scratch/wt", [rec], now_ts=now) is True


def test_is_owned_false_for_dead_pid_and_stale_heartbeat():
    now = 1_000_000.0
    dead_pid = 999_999_999  # astronomically unlikely to be a live pid
    rec = ScopeRecord(session_id="s1", cwd="/tmp/cc-scratch/wt", pid=dead_pid, heartbeat_ts=now - 999_999)
    assert sweep.is_owned("/tmp/cc-scratch/wt", [rec], now_ts=now) is False


def test_is_owned_matches_subdirectory_cwd():
    rec = ScopeRecord(session_id="s1", cwd="/tmp/cc-scratch/wt/sub/dir", pid=os.getpid(), heartbeat_ts=0.0)
    assert sweep.is_owned("/tmp/cc-scratch/wt", [rec], now_ts=1.0) is True


def test_is_owned_false_when_no_record_matches_path():
    rec = ScopeRecord(session_id="s1", cwd="/tmp/cc-scratch/other", pid=os.getpid(), heartbeat_ts=0.0)
    assert sweep.is_owned("/tmp/cc-scratch/wt", [rec], now_ts=1.0) is False


def test_is_owned_checks_repo_root_too():
    rec = ScopeRecord(session_id="s1", cwd=None, repo_root="/tmp/cc-scratch/wt", pid=os.getpid(), heartbeat_ts=0.0)
    assert sweep.is_owned("/tmp/cc-scratch/wt", [rec], now_ts=1.0) is True


# ── classify ─────────────────────────────────────────────────────────────

ROOTS = ["/tmp/cc-scratch"]


def _wt(path="/tmp/cc-scratch/x", detached=True, bare=False, branch=None):
    return sweep.WorktreeInfo(path=path, head="abc", branch=branch, detached=detached, bare=bare)


def test_classify_removes_stale_unowned_clean_detached_temp_worktree():
    verdict, _ = sweep.classify(_wt(), age_h=48, dirty=False, owned=False, roots=ROOTS)
    assert verdict == "remove"


def test_classify_keeps_bare_worktree():
    verdict, _ = sweep.classify(_wt(bare=True), age_h=999, dirty=False, owned=False, roots=ROOTS)
    assert verdict == "keep"


def test_classify_keeps_outside_temp_root():
    verdict, _ = sweep.classify(_wt(path="/home/the0/cai-main"), age_h=999, dirty=False, owned=False, roots=ROOTS)
    assert verdict == "keep"


def test_classify_keeps_branch_worktree():
    verdict, _ = sweep.classify(_wt(detached=False, branch="refs/heads/main"), age_h=999, dirty=False, owned=False, roots=ROOTS)
    assert verdict == "keep"


def test_classify_keeps_fresh_worktree():
    verdict, reason = sweep.classify(_wt(), age_h=1.0, dirty=False, owned=False, roots=ROOTS)
    assert verdict == "keep"
    assert "fresh" in reason


def test_classify_keeps_owned_worktree():
    verdict, reason = sweep.classify(_wt(), age_h=48, dirty=False, owned=True, roots=ROOTS)
    assert verdict == "keep"
    assert "owned" in reason


def test_classify_keeps_dirty_worktree():
    verdict, reason = sweep.classify(_wt(), age_h=48, dirty=True, owned=False, roots=ROOTS)
    assert verdict == "keep"
    assert "dirty" in reason


def test_classify_owned_takes_priority_over_dirty_check_order():
    # owned is checked before dirty; an owned+dirty worktree still reads as "owned".
    verdict, reason = sweep.classify(_wt(), age_h=48, dirty=True, owned=True, roots=ROOTS)
    assert verdict == "keep"
    assert "owned" in reason
