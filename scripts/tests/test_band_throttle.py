"""The shared per-session band throttle used by the advisory UserPromptSubmit hooks.

Two properties, and they pull in opposite directions, which is why both are
pinned: the throttle must actually suppress a repeat (or a nudge becomes noise
on every prompt), and every failure of the store must degrade to speaking again
rather than to falling silent or raising on a user's prompt.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib import band_throttle as bt  # noqa: E402


# --- remembering --------------------------------------------------------------

def test_records_and_reads_back_a_band(tmp_path):
    assert bt.fired_band("s1", tmp_path) == 0
    bt.record_band("s1", 2, tmp_path)
    assert bt.fired_band("s1", tmp_path) == 2


def test_sessions_do_not_share_a_stamp(tmp_path):
    bt.record_band("s1", 1, tmp_path)
    assert bt.fired_band("s2", tmp_path) == 0


def test_prefix_keeps_stamps_as_files_in_a_shared_directory(tmp_path):
    bt.record_band("s1", 1, tmp_path, "cc-context-nudge-")
    assert (tmp_path / "cc-context-nudge-s1").is_file()
    assert bt.fired_band("s1", tmp_path, "cc-context-nudge-") == 1
    assert bt.fired_band("s1", tmp_path) == 0  # a different namespace


def test_session_id_cannot_escape_the_root(tmp_path):
    """Path separators are dropped, not escaped, so a traversal collapses to a
    harmless name inside the root."""
    bt.record_band("../../etc/passwd", 1, tmp_path)
    written = list(tmp_path.iterdir())
    assert len(written) == 1
    assert written[0].parent == tmp_path


def test_empty_session_id_gets_a_stable_name(tmp_path):
    bt.record_band("", 1, tmp_path)
    assert bt.fired_band("", tmp_path) == 1
    assert (tmp_path / "nosession").is_file()


# --- failing open -------------------------------------------------------------

def test_unwritable_root_does_not_raise_and_reads_as_never_fired(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    root = blocker / "sub"
    bt.record_band("s1", 2, root)
    assert bt.fired_band("s1", root) == 0


def test_corrupt_stamp_reads_as_never_fired(tmp_path):
    bt.stamp_path("s1", tmp_path).write_text("not a number", encoding="utf-8")
    assert bt.fired_band("s1", tmp_path) == 0


def test_stamp_replaced_by_a_directory_reads_as_never_fired(tmp_path):
    bt.stamp_path("s1", tmp_path).mkdir(parents=True)
    assert bt.fired_band("s1", tmp_path) == 0


# --- pruning ------------------------------------------------------------------

def test_write_prunes_stamps_past_the_age_limit(tmp_path):
    """A ~/.local/state root is not cleaned by anything else, so a per-session
    stamp would accumulate there forever."""
    old = bt.stamp_path("ancient", tmp_path)
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text("1", encoding="utf-8")
    stale = time.time() - (bt.MAX_AGE_DAYS + 1) * 86400
    os.utime(old, (stale, stale))

    bt.record_band("fresh", 1, tmp_path)
    assert not old.exists()
    assert bt.fired_band("fresh", tmp_path) == 1


def test_pruning_leaves_recent_stamps_alone(tmp_path):
    bt.record_band("recent", 1, tmp_path)
    bt.record_band("other", 1, tmp_path)
    assert bt.fired_band("recent", tmp_path) == 1


def test_pruning_only_touches_its_own_prefix(tmp_path):
    """Shared-directory callers must not prune each other's stamps."""
    theirs = tmp_path / "someone-elses-file"
    theirs.write_text("keep me", encoding="utf-8")
    stale = time.time() - (bt.MAX_AGE_DAYS + 1) * 86400
    os.utime(theirs, (stale, stale))

    bt.record_band("s1", 1, tmp_path, "mine-")
    assert theirs.exists()
