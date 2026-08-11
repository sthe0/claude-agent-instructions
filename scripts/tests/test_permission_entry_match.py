"""Hermetic tests for `lib/permission_entry_match.covers`. The tests read no
live file: every fixture is a literal in this module. Most are synthetic calls
built to exercise one branch; the block at the end is copied VERBATIM from the
measured denial corpus, so the module is also tested against calls the harness
actually produced and denied rather than only against calls a test author
imagined. Each corpus fixture carries the `toolDenialKind` that classified it.
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


# --- a whitespace-surrounded newline must not slip the carve-out ------------

WHITESPACE_SURROUNDED_NEWLINES = [
    "cd /repo \n    git push",
    "run_it \n\tgit push --force",
    "cd /repo \r\n git push",
]


def test_whitespace_surrounded_newline_still_resolves_covering():
    # The standalone-token carve-out rests on the separator surviving lexing
    # as its own token. That holds for all six `_BASH_SEPS` and is false by
    # construction of a newline, which `shlex.split` eats as whitespace: these
    # three spellings passed the carve-out, were then mis-segmented anyway,
    # and returned False -- allowing the self-grant. COMPOUND_SPELLINGS misses
    # them because its newline row has no space before the newline.
    for command in WHITESPACE_SURROUNDED_NEWLINES:
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


# --- denied calls copied VERBATIM from the measured corpus --------------------
#
# Each `tool_input` below is byte-identical to a row in the live transcript
# store, located by the top-level `toolDenialKind` field the harness stamps on
# a denied call's result record and joined back to its originating `tool_use`
# via `sourceToolAssistantUUID`. 1 535 denial rows across 359 of 3 665
# transcripts carry it. The two values that matter here:
#   `user-rejected`  -- the harness's own allowlist matcher
#   `permission-rule` -- one of THIS repository's PreToolUse hooks
# The point of testing against these rather than against invented calls is that
# a compound command is what the harness actually denies: every Bash row below
# is compound, and none of them is the well-formed spaced `&&` spelling that
# `shlex` survives.

CORPUS_BASH_SEMICOLON_PIPE = (
    'ls -la /home/the0/.claude-agent/plans/ | grep -i smd; echo "---BRIEF---"; '
    "ls -la /tmp/cc-scratch/smd-replan-brief-v4.md"
)  # user-rejected

CORPUS_BASH_FIND_ROOT = (
    'cd /home/the0/claude-agent-instructions 2>/dev/null && pwd || echo "no canon dir here"; '
    'echo "---"; find / -maxdepth 3 -iname "claude-agent-instructions" 2>/dev/null; '
    'echo "---agentctl status from worktree---"; ls scripts/agentctl | head -5'
)  # permission-rule -- the repo's find-root-scope guard

CORPUS_BASH_NEWLINE_RM = (
    "rm -rf /tmp/phase3-installer-test.Fwoo6r\n"
    'grep -c "hook-phase3-due" /home/the0/.claude-agent/settings.json'
)  # permission-rule -- the repo's destructive-rm guard

CORPUS_EDIT_PATH = "/home/the0/claude-agent-instructions/scripts/self-diagnose.py"
# permission-rule -- the repo's canon-readonly guard


def test_corpus_compound_bash_denials_are_covered():
    for command in (
        CORPUS_BASH_SEMICOLON_PIPE,
        CORPUS_BASH_FIND_ROOT,
        CORPUS_BASH_NEWLINE_RM,
    ):
        assert covers("Bash(git:*)", "Bash", {"command": command}) is True


def test_corpus_edit_denial_matches_by_path_not_by_luck():
    assert covers("Edit", "Edit", {"file_path": CORPUS_EDIT_PATH}) is True
    assert covers("Edit(/home/the0/claude-agent-instructions/**)", "Edit", {"file_path": CORPUS_EDIT_PATH}) is True
    assert covers("Edit(/tmp/**)", "Edit", {"file_path": CORPUS_EDIT_PATH}) is False
