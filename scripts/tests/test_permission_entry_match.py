"""Hermetic tests for `lib/permission_entry_match.covers`. No live filesystem
or transcript reads: every fixture below is either a synthetic call built to
exercise one branch, or -- where labeled -- a RECONSTRUCTION consistent with
the plan-time corpus measurement (PROVENANCE § 1: 3 078 transcripts, 1 341
denial rows, Bash the plurality at 875) rather than a byte-identical row from
that corpus. This developer session cannot read the live transcript store
(outside this worktree and `~/.claude-agent/plans`), so no row is presented
as verbatim when it is not.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.permission_entry_match import covers  # noqa: E402


# --- the three live entry forms, each covering and not covering a call ------

def test_prefix_form_covers_matching_operand():
    assert covers("Bash(git:*)", "Bash", {"command": "git status"}) is True


def test_prefix_form_does_not_cover_other_operand():
    assert covers("Bash(git:*)", "Bash", {"command": "ls -la"}) is False


def test_exact_form_covers_matching_operand():
    assert covers("Bash(ls)", "Bash", {"command": "ls"}) is True


def test_exact_form_does_not_cover_other_operand():
    assert covers("Bash(ls)", "Bash", {"command": "ls -la"}) is False


def test_bare_form_covers_every_call_of_that_tool():
    assert covers("Bash", "Bash", {"command": "anything at all"}) is True


def test_bare_form_does_not_cover_a_different_tool():
    assert covers("Bash", "Edit", {"file_path": "/x"}) is False


# --- wildcard-everything: bare Tool and Tool(*) both cover any call --------

def test_wildcard_star_form_covers_an_arbitrary_bash_call():
    assert covers("Bash(*)", "Bash", {"command": "rm -rf /"}) is True


def test_bare_bash_covers_an_arbitrary_bash_call():
    assert covers("Bash", "Bash", {"command": "rm -rf /"}) is True


# --- same-tool-different-prefix must NOT cover ------------------------------

def test_same_tool_different_prefix_does_not_cover():
    assert covers("Bash(arc:*)", "Bash", {"command": "ya tool build"}) is False


# --- cross-tool must NOT cover -----------------------------------------------

def test_cross_tool_entry_does_not_cover_denied_edit():
    assert covers("Bash(git:*)", "Edit", {"file_path": "/repo/x.py"}) is False


# --- file-tool glob: covering and not covering a file_path ------------------

def test_file_tool_prefix_covers_matching_path():
    assert covers("Edit(/repo/*)", "Edit", {"file_path": "/repo/x.py"}) is True


def test_file_tool_prefix_does_not_cover_other_path():
    assert covers("Edit(/repo/*)", "Edit", {"file_path": "/other/x.py"}) is False


def test_file_tool_glob_exact_form_covers_matching_path():
    assert covers("Read(/home/the0/**)", "Read", {"file_path": "/home/the0/x.py"}) is True


def test_file_tool_glob_exact_form_does_not_cover_other_path():
    assert covers("Read(/home/the0/**)", "Read", {"file_path": "/etc/passwd"}) is False


# --- fail-toward-covering: unparseable entry, unmodelled tool, missing operand

def test_unparseable_entry_covers():
    assert covers("Bash(unterminated", "Bash", {"command": "ls"}) is True


def test_unmodelled_tool_with_specifier_covers():
    # WebSearch is a real denied tool in the corpus (PROVENANCE § 1) but this
    # module models no operand resolution for it, so a non-wildcard specifier
    # cannot be evaluated and must resolve to covering.
    assert covers("WebSearch(some-query)", "WebSearch", {"query": "some-query"}) is True


def test_missing_operand_field_covers_bash():
    assert covers("Bash(git:*)", "Bash", {}) is True


def test_missing_operand_field_covers_file_tool():
    assert covers("Edit(/repo/*)", "Edit", {}) is True


# --- the compound-command case the plan review forced -----------------------

def test_compound_command_second_segment_covers_despite_first():
    # `cd /repo && git push` segments correctly (space-delimited && is the one
    # spelling `shlex.split` + `split_segments` handle); the second segment
    # starts with `git` even though the whole string starts with `cd`, so
    # segment-wise matching -- not whole-string matching -- is what produces
    # `True` here. A mutation collapsing to whole-string prefix matching
    # would make this row and its `arc:*` negative control both wrong in
    # opposite directions.
    assert covers("Bash(git:*)", "Bash", {"command": "cd /repo && git push"}) is True


# --- the full compound row-set the two replan reviews forced ----------------

COMPOUND_SPELLINGS = [
    "cd /repo\ngit push",
    "cd /repo; git push",
    "cd /repo;git push",
    "cd /repo&&git push",
    "cd /repo|git push",
]


def test_every_compound_spelling_resolves_covering_via_the_raw_string_rule():
    # None of these five spellings segments correctly under `shlex.split` +
    # `split_segments` (PROVENANCE § 8's six-row table) -- each is False
    # without the raw-string separator short-circuit, measured at commit A.
    # This is a mutation test on that rule, not decoration: delete the rule
    # and every row here goes red.
    for command in COMPOUND_SPELLINGS:
        assert covers("Bash(git:*)", "Bash", {"command": command}) is True, command


# --- two negative controls the raw-string rule must NOT break ---------------

def test_well_formed_spaced_and_still_resolves_through_real_segmentation():
    # The standalone-token carve-out is what keeps relevance narrowing on the
    # trustworthy, space-delimited `&&` form: "arc" does not match the "git"
    # segment, so this must still return False. Deleting the carve-out (i.e.
    # triggering the raw-string rule on every `&&` regardless of spacing)
    # must make this exact row go red.
    assert covers("Bash(arc:*)", "Bash", {"command": "cd /repo && git push"}) is False


def test_raw_string_rule_does_not_defeat_the_tool_name_check():
    # The raw-string rule only widens the Bash OPERAND check; an Edit entry
    # must never cover a Bash call regardless of how the command is spelled.
    assert covers("Edit", "Bash", {"command": "a\nb"}) is False


# --- entry taken from the plan-time live surface inventory (PROVENANCE § 2) -
#
# `Bash(git status:*)` is one of the 74 real allow entries inventoried on
# ~/.claude-agent/settings.json at plan time (the multi-word prefix form).
# Verbatim specifier string; the call below is synthetic.

def test_multi_word_prefix_entry_from_the_live_surface_inventory():
    assert covers("Bash(git status:*)", "Bash", {"command": "git status --porcelain"}) is True
    assert covers("Bash(git status:*)", "Bash", {"command": "git log"}) is False


# --- a denied call consistent with the measured corpus (RECONSTRUCTION) -----
#
# This developer session cannot read the live transcript store, so no byte-
# identical corpus row is available. RECONSTRUCTION: a compound Bash denial
# shaped like the corpus's own majority case (PROVENANCE § 1: Bash 875/1341
# denials; § 8: 302/875 of those are multi-line before any `;`/`&&` form is
# counted) -- an agent denied on a multi-line `git push` attempt, self-
# granting `Bash(git:*)` in response. Flagged as a residual in this stage's
# COMPLETED report: the plan's done_criterion calls for a verbatim corpus
# row, and neither [meta] nor PROVENANCE embeds one to copy.

RECONSTRUCTED_CORPUS_DENIAL_COMMAND = "cd /home/the0/cai-wt-perm-self-grant\ngit push origin perm-self-grant"


def test_reconstructed_corpus_shaped_denial_is_covered():
    assert covers("Bash(git:*)", "Bash", {"command": RECONSTRUCTED_CORPUS_DENIAL_COMMAND}) is True
