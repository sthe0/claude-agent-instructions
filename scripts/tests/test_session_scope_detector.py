"""Tests for session_scope.detector — VCS-agnostic path-overlap conflict
detection + block/warn severity classification.

Mirrors test_session_scope_registry.py's style: an injected now_ts/ttl instead
of ambient state, and a small _rec helper building ScopeRecord fixtures.
"""
from __future__ import annotations

from session_scope.detector import Conflict, classify_severity, detect_conflicts, path_overlaps
from session_scope.registry import ScopeRecord


def _rec(session_id, heartbeat_ts, touched_paths=None, repo_root=None, vcs="none"):
    return ScopeRecord(
        session_id=session_id,
        heartbeat_ts=heartbeat_ts,
        repo_root=repo_root,
        vcs=vcs,
        touched_paths=list(touched_paths or []),
    )


# ── path_overlaps: ancestor-or-equal, symmetric, reflexive-safe ────────────

def test_path_overlaps_identical_paths():
    assert path_overlaps("/repo/a.py", "/repo/a.py") is True


def test_path_overlaps_is_reflexive():
    assert path_overlaps("/repo/a.py", "/repo/a.py") is True


def test_path_overlaps_ancestor_and_descendant():
    assert path_overlaps("/repo", "/repo/sub/a.py") is True


def test_path_overlaps_is_symmetric():
    assert path_overlaps("/repo/sub/a.py", "/repo") is True
    assert path_overlaps("/repo", "/repo/sub/a.py") is True


def test_path_overlaps_siblings_do_not_overlap():
    assert path_overlaps("/repo/a.py", "/repo/b.py") is False


def test_path_overlaps_distinct_roots_do_not_overlap():
    assert path_overlaps("/repo-A/a.py", "/repo-B/a.py") is False


def test_path_overlaps_normalizes_dotdot_and_relative_segments():
    assert path_overlaps("/repo/./sub/../a.py", "/repo/a.py") is True


def test_path_overlaps_arc_mounts_same_mount_overlap():
    assert path_overlaps("/arc/mount1/src/file.py", "/arc/mount1/src") is True


def test_path_overlaps_arc_mounts_distinct_mounts_no_overlap():
    assert path_overlaps("/arc/mount1/src/file.py", "/arc/mount2/src/file.py") is False


# ── detect_conflicts: liveness + overlap combined ───────────────────────────

def test_detect_conflicts_single_live_session_no_conflict():
    # Only this_session itself is live -> never conflicts with itself.
    records = [_rec("s1", 100.0, touched_paths=["/repo/a.py"])]
    conflicts = detect_conflicts(
        records, this_session="s1", candidate_paths=["/repo/a.py"], now_ts=100.0, ttl_s=30.0
    )
    assert conflicts == []


def test_detect_conflicts_two_sessions_same_tree_overlap():
    records = [
        _rec("s1", 100.0, touched_paths=["/repo/a.py"]),
        _rec("s2", 100.0, touched_paths=["/repo/a.py"]),
    ]
    conflicts = detect_conflicts(
        records, this_session="s1", candidate_paths=["/repo/a.py"], now_ts=100.0, ttl_s=30.0
    )
    assert conflicts == [Conflict(other_session="s2", held_path="/repo/a.py", candidate="/repo/a.py")]


def test_detect_conflicts_two_sessions_distinct_worktrees_no_conflict():
    records = [
        _rec("s1", 100.0, touched_paths=["/repo-A/a.py"], repo_root="/repo-A"),
        _rec("s2", 100.0, touched_paths=["/repo-B/a.py"], repo_root="/repo-B"),
    ]
    conflicts = detect_conflicts(
        records, this_session="s1", candidate_paths=["/repo-A/b.py"], now_ts=100.0, ttl_s=30.0
    )
    assert conflicts == []


def test_detect_conflicts_ignores_stale_other_session():
    records = [
        _rec("s1", 100.0, touched_paths=["/repo/a.py"]),
        _rec("s2", 10.0, touched_paths=["/repo/a.py"]),  # beyond ttl -> not live
    ]
    conflicts = detect_conflicts(
        records, this_session="s1", candidate_paths=["/repo/a.py"], now_ts=100.0, ttl_s=30.0
    )
    assert conflicts == []


def test_detect_conflicts_arc_mount_paths_overlap():
    records = [
        _rec("s1", 100.0, touched_paths=["/arc/mount1/src/a.py"], vcs="arc"),
        _rec("s2", 100.0, touched_paths=["/arc/mount1/src/a.py"], vcs="arc"),
    ]
    conflicts = detect_conflicts(
        records, this_session="s1", candidate_paths=["/arc/mount1/src/a.py"], now_ts=100.0, ttl_s=30.0
    )
    assert len(conflicts) == 1
    assert conflicts[0].other_session == "s2"


def test_detect_conflicts_arc_distinct_mounts_no_conflict():
    records = [
        _rec("s1", 100.0, touched_paths=["/arc/mount1/src/a.py"], vcs="arc"),
        _rec("s2", 100.0, touched_paths=["/arc/mount2/src/a.py"], vcs="arc"),
    ]
    conflicts = detect_conflicts(
        records, this_session="s1", candidate_paths=["/arc/mount1/src/a.py"], now_ts=100.0, ttl_s=30.0
    )
    assert conflicts == []


def test_detect_conflicts_no_other_sessions_returns_empty():
    assert (
        detect_conflicts([], this_session="s1", candidate_paths=["/repo/a.py"], now_ts=100.0, ttl_s=30.0)
        == []
    )


def test_detect_conflicts_checks_every_candidate():
    records = [_rec("s2", 100.0, touched_paths=["/repo/a.py"])]
    conflicts = detect_conflicts(
        records,
        this_session="s1",
        candidate_paths=["/repo/a.py", "/repo/b.py"],
        now_ts=100.0,
        ttl_s=30.0,
    )
    assert len(conflicts) == 1
    assert conflicts[0].candidate == "/repo/a.py"


def test_detect_conflicts_extra_live_check_gates_further():
    records = [_rec("s1", 100.0), _rec("s2", 100.0, touched_paths=["/repo/a.py"])]
    conflicts = detect_conflicts(
        records,
        this_session="s1",
        candidate_paths=["/repo/a.py"],
        now_ts=100.0,
        ttl_s=30.0,
        extra_live_check=lambda sid: sid == "s1",  # s2's process is gone
    )
    assert conflicts == []


# ── lineage awareness: a parent + its synchronously-spawned descendants are one writer ──

def test_detect_conflicts_writer_lineage_exempts_ancestor_held_path():
    # 'child' writes a path 'parent' holds; child's writer_lineage names parent,
    # so the two are one actor and there is no conflict.
    records = [_rec("parent", 100.0, touched_paths=["/repo/a.py"])]
    conflicts = detect_conflicts(
        records, this_session="child", candidate_paths=["/repo/a.py"],
        now_ts=100.0, ttl_s=30.0, writer_lineage={"parent"},
    )
    assert conflicts == []


def test_detect_conflicts_record_lineage_exempts_descendant_from_ancestor_writer():
    # 'parent' writes a path a 'child' record holds; the child's record names
    # parent in its lineage_ids, so the parent (writer) is exempt from its own
    # descendant even though the parent's env carries no lineage.
    child = _rec("child", 100.0, touched_paths=["/repo/a.py"])
    child.lineage_ids = ["parent"]
    conflicts = detect_conflicts(
        [child], this_session="parent", candidate_paths=["/repo/a.py"],
        now_ts=100.0, ttl_s=30.0,
    )
    assert conflicts == []


def test_detect_conflicts_siblings_sharing_lineage_are_exempt():
    # two children of the same parent (documented accepted consequence).
    c2 = _rec("c2", 100.0, touched_paths=["/repo/a.py"])
    c2.lineage_ids = ["parent"]
    conflicts = detect_conflicts(
        [c2], this_session="c1", candidate_paths=["/repo/a.py"],
        now_ts=100.0, ttl_s=30.0, writer_lineage={"parent"},
    )
    assert conflicts == []


def test_detect_conflicts_disjoint_lineage_still_conflicts():
    foreign = _rec("foreign", 100.0, touched_paths=["/repo/a.py"])
    foreign.lineage_ids = ["parentB"]
    conflicts = detect_conflicts(
        [foreign], this_session="c1", candidate_paths=["/repo/a.py"],
        now_ts=100.0, ttl_s=30.0, writer_lineage={"parentA"},
    )
    assert len(conflicts) == 1
    assert conflicts[0].other_session == "foreign"


def test_detect_conflicts_legacy_record_no_lineage_behaves_as_singleton():
    # backward-compat: a record with default (empty) lineage_ids still conflicts
    # with a disjoint-lineage writer exactly as before the lineage change.
    records = [_rec("other", 100.0, touched_paths=["/repo/a.py"])]  # lineage_ids == []
    conflicts = detect_conflicts(
        records, this_session="me", candidate_paths=["/repo/a.py"],
        now_ts=100.0, ttl_s=30.0, writer_lineage={"someparent"},
    )
    assert len(conflicts) == 1


def test_detect_conflicts_no_writer_lineage_is_identical_to_pre_change_behavior():
    # writer_lineage omitted => writer_set is {this_session} => today's behavior.
    records = [_rec("other", 100.0, touched_paths=["/repo/a.py"])]
    conflicts = detect_conflicts(
        records, this_session="me", candidate_paths=["/repo/a.py"],
        now_ts=100.0, ttl_s=30.0,
    )
    assert len(conflicts) == 1


def test_detect_conflicts_stale_holder_never_conflicts_regardless_of_lineage():
    stale = _rec("foreign", 10.0, touched_paths=["/repo/a.py"])  # beyond ttl
    stale.lineage_ids = ["parentB"]
    conflicts = detect_conflicts(
        [stale], this_session="me", candidate_paths=["/repo/a.py"],
        now_ts=100.0, ttl_s=30.0, writer_lineage={"parentA"},
    )
    assert conflicts == []


# ── classify_severity: block vs warn ────────────────────────────────────────

def test_classify_severity_blocks_gated_path_held_by_other_live():
    assert classify_severity("/home/u/.claude/CLAUDE.md", held_by_other_live=True) == "block"


def test_classify_severity_warns_gated_path_when_not_held():
    assert classify_severity("/home/u/.claude/CLAUDE.md", held_by_other_live=False) == "warn"


def test_classify_severity_warns_memory_path_even_if_held():
    assert (
        classify_severity(
            "/home/u/.claude/memory-global/leaves/foo.md", held_by_other_live=True
        )
        == "warn"
    )


def test_classify_severity_warns_tmp_scratch_even_if_held():
    assert classify_severity("/tmp/cc-scratch/x.py", held_by_other_live=True) == "warn"


def test_classify_severity_warns_non_production_extension_even_if_held():
    assert classify_severity("/repo/notes.txt", held_by_other_live=True) == "warn"
