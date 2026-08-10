"""Tests for lib/bash_write_targets.py — the generic Bash write-target lexer
extracted out of hook-guard-canon-readonly.py.

Covers the newline-segmentation bug: `shlex.split()` treats a newline exactly
like a space (no token is ever emitted for it), so a multi-line command was
tokenized as ONE segment. That let a second line's command word ride along as
a spurious operand of an unrelated verb on the first line — e.g. a `sed -i`
segment's operand scan does not stop at a newline, so it swept up a following
`echo` as if it were one of `sed`'s own arguments and reported it as a write
target under the current directory.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from lib.bash_write_targets import command_write_targets  # noqa: E402


def test_multiline_command_segments_at_the_newline(tmp_path):
    """First line: `sed -i` on `foo.txt`. Second line: a real redirect into
    `existing.py`. Before the fix, tokenizing the whole command as one blob let
    the second line's `echo` and `hi` ride along as spurious `sed` operands,
    resolving to a phantom `<cwd>/echo` target. After the fix, each physical
    line is its own segment: `sed -i`'s target is `foo.txt` only, and `echo`'s
    real redirect target `existing.py` is found on its own line."""
    eff_cwd = str(tmp_path)
    command = "sed -i 's/x/y/' foo.txt\necho hi > existing.py"

    targets = command_write_targets(command, eff_cwd)

    assert os.path.join(eff_cwd, "foo.txt") in targets
    assert os.path.join(eff_cwd, "existing.py") in targets
    assert os.path.join(eff_cwd, "echo") not in targets
    assert os.path.join(eff_cwd, "hi") not in targets


def test_multiline_command_second_line_redirect_alone_is_found(tmp_path):
    """A plain non-write first line followed by a real redirect on the second
    line — the minimal case a merged-segment scan already got right (the
    output-redirection scan (a) walks the whole token stream, not just the
    first line), kept here so a future regression in the per-line split is
    caught even when no verb-based writer is involved."""
    eff_cwd = str(tmp_path)
    command = "echo just a status line\ncat foo > bar.txt"

    targets = command_write_targets(command, eff_cwd)

    assert os.path.join(eff_cwd, "bar.txt") in targets


def test_single_line_command_unaffected(tmp_path):
    """No newline present: behavior is identical to before the fix."""
    eff_cwd = str(tmp_path)
    command = "sed -i 's/x/y/' foo.txt"

    targets = command_write_targets(command, eff_cwd)

    assert os.path.join(eff_cwd, "foo.txt") in targets
    assert os.path.join(eff_cwd, "echo") not in targets


def test_backslash_continued_command_finds_real_target(tmp_path):
    """A single LOGICAL command wrapped across two PHYSICAL lines with a
    trailing backslash — the shape agent-authored multi-line Bash produces
    routinely. Before the fix, `command.splitlines()` cut the logical line
    before the continuation resolved, `shlex.split()` on the orphaned
    fragment `"sed -i 's/a/b/' \\"` raised (an unescaped trailing backslash
    has no following character to escape), and the blanket `except
    Exception: return []` around the whole comprehension discarded every
    line's targets — a false negative on the gate's primary input shape."""
    eff_cwd = str(tmp_path)
    command = "sed -i 's/a/b/' \\\n  scripts/x.py"

    targets = command_write_targets(command, eff_cwd)

    assert os.path.join(eff_cwd, "scripts/x.py") in targets


def test_poisoned_line_does_not_discard_other_lines_targets(tmp_path):
    """One malformed physical line (an unterminated quote) must not blank
    out every OTHER line's real targets. Splitting by line made per-line
    recovery available; nothing used it until this fix — a single `try`
    around the whole comprehension meant one bad line poisoned the batch."""
    eff_cwd = str(tmp_path)
    command = "cp foo.txt bar.txt\necho 'unterminated"

    targets = command_write_targets(command, eff_cwd)

    assert os.path.join(eff_cwd, "bar.txt") in targets


def test_backslash_newline_inside_single_quotes_is_not_a_continuation():
    """Inside single quotes, shell performs NO escape processing at all —
    not even of a backslash — so `\\<newline>` there is two literal
    characters, not a line continuation. A blind (quote-blind) join would
    remove the newline from inside the quoted string, changing its content
    and silently pulling a line that real shell keeps separate into the
    same physical line as this one. Joining is elided while inside a
    single-quoted span; only a `'` character ends that span."""
    from lib.bash_write_targets import _join_backslash_continuations

    joined = _join_backslash_continuations("echo 'literal\\\nbreak' arg")

    assert joined == "echo 'literal\\\nbreak' arg"


def test_backslash_newline_outside_quotes_is_still_joined():
    """Control for the quote-tracking test above: outside any quoting, the
    continuation is still elided exactly as the F1 fix requires."""
    from lib.bash_write_targets import _join_backslash_continuations

    joined = _join_backslash_continuations("sed -i 's/a/b/' \\\n  scripts/x.py")

    assert joined == "sed -i 's/a/b/'   scripts/x.py"


def test_single_quoted_embedded_backslash_newline_does_not_crash_or_fabricate(tmp_path):
    """End-to-end control for the same case: a command whose FIRST line
    opens a single-quoted string that itself contains a literal
    backslash-newline (never joined, per the test above) must not crash the
    whole batch and must not fabricate a bogus target from the split
    quote — each malformed physical line is recovered independently
    (per-line try/except) while a real target on a later, well-formed line
    is still found."""
    eff_cwd = str(tmp_path)
    command = "echo 'literal\\\nbreak' arg\ncp src.txt dest.txt"

    targets = command_write_targets(command, eff_cwd)

    assert os.path.join(eff_cwd, "dest.txt") in targets


def test_multiline_single_quoted_message_before_write_is_found(tmp_path):
    """F3: a single-quoted argument that itself SPANS physical lines (a real
    newline embedded inside the quotes, e.g. a multi-line commit message)
    must not blank out a write target that comes later in the same command.
    Per-line splitting (the F1/F2 fix) tokenizes each physical line on its
    own; the line that only CLOSES the quote has no opening quote of its
    own and fails to tokenize, so the line carrying the real `cp` write was
    silently dropped even though it never itself contained the bad quote."""
    eff_cwd = str(tmp_path)
    command = "git commit -m 'first line\n\nbody text' && cp evil.py /repo/x.py"

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/x.py" in targets


def test_multiline_double_quoted_message_before_write_is_found(tmp_path):
    """F3, double-quoted variant of the case above."""
    eff_cwd = str(tmp_path)
    command = 'git commit -m "first\n\nbody" && cp evil.py /repo/x.py'

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/x.py" in targets


def test_multiline_single_quoted_redirect_argument_is_found(tmp_path):
    """F3, redirect variant: the write is a plain `>` redirect rather than a
    `cp`, and the multi-line quoted argument precedes it on the same
    (single) logical/physical-after-substitution line."""
    eff_cwd = str(tmp_path)
    command = "echo 'lit\n' > /repo/out.py"

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/out.py" in targets


def test_multiline_dollar_paren_substitution_is_found(tmp_path):
    """F4: an UNQUOTED `$(...)` command substitution whose body spans a real
    embedded newline. Before the fix, `_prepare_command_for_whole_stream`
    turned that newline into a `; ` statement separator with no awareness of
    the substitution around it, splitting `$(ls` and `dir)` into two
    segments; `cp`'s destination scan then saw only the fragment `$(ls` as
    its sole positional and reported the fabricated target
    `<cwd>/$(ls`, while the command's REAL destination (`/repo/subst.py`)
    was never reached. After the fix the embedded newline becomes a plain
    space, the substitution stays one shlex token, and `cp`'s real last
    positional is found."""
    eff_cwd = str(tmp_path)
    command = "cp $(ls\ndir) /repo/subst.py"

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/subst.py" in targets
    assert os.path.join(eff_cwd, "$(ls") not in targets


def test_multiline_backtick_substitution_is_found(tmp_path):
    """F4, backtick variant of the case above: `` `...` `` spanning a real
    embedded newline must not fabricate a target from its broken-off tail
    either."""
    eff_cwd = str(tmp_path)
    command = "cp `ls\ndir` /repo/tick.py"

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/tick.py" in targets
    assert os.path.join(eff_cwd, "`ls") not in targets


def test_multiline_nested_dollar_paren_substitution_is_found(tmp_path):
    """F4, nested variant: a `$(...)` containing a second `$(...)`, with the
    embedded newline inside the innermost one. The depth counter must reach
    back to 0 only at the OUTER closing `)`, not the inner one, or the outer
    substitution reopens as ordinary text partway through."""
    eff_cwd = str(tmp_path)
    command = "cp $(echo $(ls\nd)) /repo/deep.py"

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/deep.py" in targets
    assert os.path.join(eff_cwd, "$(echo") not in targets


def test_multiline_backtick_nested_inside_dollar_paren_is_found(tmp_path):
    """Mixed nesting: a backtick span embedded inside a `$(...)`. Depth and
    the backtick toggle are independent flags in the same pass — a newline
    inside the backtick span must still be protected via `in_backtick` even
    though `subst_depth` is simultaneously > 0 from the outer `$(`."""
    eff_cwd = str(tmp_path)
    command = "cp $(echo `ls\nd`) /repo/mixed.py"

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/mixed.py" in targets


def test_multiline_escaped_backtick_nested_inside_backtick_is_found(tmp_path):
    """Legacy nested-backtick syntax: an inner backtick pair escaped with a
    backslash (`` \\` ... \\` ``) so the OUTER backtick parsing does not end
    at the first inner backtick. The pre-existing backslash-escape branch
    (checked before the backtick toggle) already consumes an escaped
    backtick as a literal pair without flipping `in_backtick`, so the outer
    span stays open across the embedded newline with no extra code."""
    eff_cwd = str(tmp_path)
    command = "cp `echo \\`ls\\`\nd` /repo/nested_tick.py"

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/nested_tick.py" in targets


def test_multiline_arithmetic_expansion_is_unaffected(tmp_path):
    """`$((...))` arithmetic expansion shares the same `$(` counter with no
    special case: the digraph's inner `(` is not itself a tracked opener, so
    it is appended as an ordinary character and the surrounding `$(`/`)`
    pair still protects a newline inside the expression."""
    eff_cwd = str(tmp_path)
    command = "echo $(( 1 +\n 2 )) > /repo/arith.py"

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/arith.py" in targets


def test_multiline_double_quoted_command_substitution_still_preserved(tmp_path):
    """Regression control: a `$(...)` embedded inside a DOUBLE-quoted
    argument was already correctly handled before this fix (the pre-existing
    `not in_double` guard preserves every newline for the whole quoted span
    regardless of what is inside it) and must remain so — this fix must not
    touch the in_double path at all."""
    eff_cwd = str(tmp_path)
    command = 'echo "$(ls\ndir)" > /repo/dq.py'

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/dq.py" in targets


def test_process_substitution_spanning_newline_is_an_accepted_residual(tmp_path):
    """Accepted, documented residual: `<(...)`/`>(...)` process substitution
    is NOT tracked by the `$(`/backtick depth counter (recognizing it would
    collide with `segment_write_target`'s separate, pre-existing
    `tok.startswith(">")` redirect heuristic — out of scope for this
    newline-focused fix, see the `_prepare_command_for_whole_stream`
    docstring). An embedded newline inside it is still read as a `;`
    separator, which can split a verb's real destination away from its
    write-target scan; this pins the current, non-fabricating-but-lossy
    outcome (an empty result here, not a crash and not a wrong path) so a
    future change to this area does not silently drop the documentation of
    the gap."""
    eff_cwd = str(tmp_path)
    command = "cp <(sort\na.txt) /repo/out.py"

    targets = command_write_targets(command, eff_cwd)

    assert targets == []


def test_bare_subshell_nested_inside_dollar_paren_is_an_accepted_residual(tmp_path):
    """Accepted, documented residual: a bare (non-`$`) subshell/group nested
    inside a `$(...)` closes the SAME depth counter as the outer
    substitution at its own `)`, so depth reaches 0 one `)` early and a
    newline after that point is wrongly read as a top-level `;` separator
    again — fixed only by full paren-matching, materially more than the
    depth counter this commit adds (see the docstring). This pins the
    current fabricated-but-non-crashing outcome so the gap stays visible
    rather than being silently "fixed" by a future incidental change."""
    eff_cwd = str(tmp_path)
    command = "cp $(echo a && (echo b)\nc) /repo/paren.py"

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/paren.py" not in targets
    assert os.path.join(eff_cwd, "a") in targets


def test_unterminated_quote_after_multiline_quoted_argument_still_recovers_earlier_write(tmp_path):
    """F5: a quote genuinely unterminated at the END of a command does not
    destroy recovery of a well-formed logical unit that closed BEFORE it.
    The whole-stream attempt still raises (the trailing quote never closes),
    but `_split_logical_units` isolates the broken `echo 'unterminated` tail
    into its OWN unit — the earlier `git commit ... && cp e.py /repo/x.py`
    unit, itself containing a correctly-closed multi-line single-quoted
    argument, parses fine on its own and is recovered by the units-harvest
    half of the fallback's union. This narrows what an earlier, wrong
    adjudication treated as an irreducible residual for the WHOLE command;
    see test_unterminated_quote_on_the_same_unit_as_the_write_is_an_accepted_residual
    for the genuinely irreducible case — the write on the SAME unit as the
    bad quote."""
    eff_cwd = str(tmp_path)
    command = "git commit -m 'a\nb' && cp e.py /repo/x.py\necho 'unterminated"

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/x.py" in targets


def test_unterminated_quote_on_the_same_unit_as_the_write_is_an_accepted_residual(tmp_path):
    """Accepted, irreducible residual, narrowed from the over-broad claim the
    test above replaces: when the write and the bad quote share the SAME
    logical unit (here, one physical line joined by `&&`), there is no
    well-defined lexical reading of that unit at all — the units-harvest
    fails on it (whole unit is unparseable) and the splitlines-harvest fails
    on it too (the physical line is identical to the unit here, so it is the
    same unparseable text). Nothing recovers a write that never had a
    closed reading in the first place."""
    eff_cwd = str(tmp_path)
    command = "echo 'unterminated && cp a.py /repo/onsame.py"

    targets = command_write_targets(command, eff_cwd)

    assert targets == []


def test_unterminated_quote_does_not_lose_a_write_on_a_later_physical_line(tmp_path):
    """Round-6-direction regression control: a naive quote-aware-only
    fallback (unit-by-unit, no splitlines union) REGRESSES relative to the
    prior physical-line-only fallback here, because a genuinely unterminated
    quote never closes, so `_split_logical_units` folds every subsequent
    physical line into the SAME unparseable unit as the bad quote — the
    units-harvest alone finds nothing. The splitlines-harvest half of the
    union still recovers it, exactly as the prior physical-line fallback
    did, because on its own physical line `cp a.py /repo/after.py` parses
    fine independent of the previous line's bad quote."""
    eff_cwd = str(tmp_path)
    command = "echo 'unterminated\ncp a.py /repo/after.py"

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/after.py" in targets


def test_unterminated_quote_does_not_lose_multiple_later_writes(tmp_path):
    """Round-6-direction regression control, multi-write variant: both
    later physical lines' writes must survive the union fallback, not just
    the first one found."""
    eff_cwd = str(tmp_path)
    command = "echo 'unterminated\ncp a.py /repo/l1.py\ncp b.py /repo/l2.py"

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/l1.py" in targets
    assert "/repo/l2.py" in targets


def test_unterminated_quote_fallback_still_recovers_a_multiline_dollar_paren_write(tmp_path):
    """F5, `$(...)`-substitution variant: the same fallback-engagement
    failure as the multi-line-quoted-argument case above, but for an
    UNQUOTED `$(...)` substitution spanning a newline instead. Before this
    fix, engaging the (then quote-BLIND) per-line fallback split `$(ls` and
    `dir)` onto separate physical lines exactly like the primary path's old
    F4 bug, fabricating `<cwd>/$(ls` as `cp`'s only positional and losing
    the real destination. The units-harvest half of the union keeps
    `$(...)`-depth tracking active per unit, so `cp`'s real last positional
    is still found; the raw splitlines-harvest independently contributes the
    harmless fabricated fragment `$(ls` — over-reporting here is the safe
    direction for a deny gate."""
    eff_cwd = "/repo"
    command = "cp $(ls\ndir) /repo/subst.py\necho 'unterminated"

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/subst.py" in targets
