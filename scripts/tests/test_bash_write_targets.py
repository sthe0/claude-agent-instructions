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
    `existing.py`. Tokenizing the whole command as one blob would let the
    second line's `echo` and `hi` ride along as spurious `sed` operands,
    resolving to a phantom `<cwd>/echo` target — the newline must segment
    the command so `sed -i`'s target is `foo.txt` only, and `echo`'s real
    redirect target `existing.py` is found on its own line."""
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
    routinely. Splitting on physical lines before resolving the
    continuation would cut the logical line prematurely: `shlex.split()` on
    the orphaned fragment `"sed -i 's/a/b/' \\"` raises (an unescaped
    trailing backslash has no following character to escape), which is why
    the continuation must be resolved before any per-line split happens —
    otherwise a parse failure on one fragment can discard every line's
    targets, a false negative on the gate's primary input shape."""
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
    characters, not a line continuation. A quote-blind join would remove the
    newline from inside the quoted string, changing its content and
    silently pulling a line that real shell keeps separate into the same
    physical line as this one. Eliding a backslash-newline pair is skipped
    while inside a single-quoted span; only a `'` character ends that span,
    so the unit comes back byte-for-byte unchanged."""
    from lib.bash_write_targets import _split_logical_units

    units = _split_logical_units("echo 'literal\\\nbreak' arg")

    assert units == ["echo 'literal\\\nbreak' arg"]


def test_backslash_newline_outside_quotes_is_still_joined():
    """Control for the quote-tracking test above: outside any quoting, a
    backslash-newline pair is still elided into nothing, so the newline
    never reaches the unit-break decision at all."""
    from lib.bash_write_targets import _split_logical_units

    units = _split_logical_units("sed -i 's/a/b/' \\\n  scripts/x.py")

    assert units == ["sed -i 's/a/b/'   scripts/x.py"]


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
    """A single-quoted argument that itself SPANS physical lines (a real
    newline embedded inside the quotes, e.g. a multi-line commit message)
    must not blank out a write target that comes later in the same command.
    A naive per-physical-line split tokenizes each physical line on its
    own; the line that only CLOSES the quote has no opening quote of its
    own and fails to tokenize, so the line carrying the real `cp` write
    would be silently dropped even though it never itself contained the bad
    quote — the quote-span-aware unit split keeps both lines as one unit
    instead."""
    eff_cwd = str(tmp_path)
    command = "git commit -m 'first line\n\nbody text' && cp evil.py /repo/x.py"

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/x.py" in targets


def test_multiline_double_quoted_message_before_write_is_found(tmp_path):
    """Double-quoted variant of the case above."""
    eff_cwd = str(tmp_path)
    command = 'git commit -m "first\n\nbody" && cp evil.py /repo/x.py'

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/x.py" in targets


def test_multiline_single_quoted_redirect_argument_is_found(tmp_path):
    """Redirect variant: the write is a plain `>` redirect rather than a
    `cp`, and the multi-line quoted argument precedes it on the same
    (single) logical/physical-after-substitution line."""
    eff_cwd = str(tmp_path)
    command = "echo 'lit\n' > /repo/out.py"

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/out.py" in targets


def test_multiline_dollar_paren_substitution_is_found(tmp_path):
    """An UNQUOTED `$(...)` command substitution whose body spans a real
    embedded newline. A newline-blind split would turn that newline into a
    `; ` statement separator with no awareness of the substitution around
    it, splitting `$(ls` and `dir)` into two segments; `cp`'s destination
    scan would then see only the fragment `$(ls` as its sole positional and
    report the fabricated target `<cwd>/$(ls`, while the command's REAL
    destination (`/repo/subst.py`) is never reached. With `$(`-depth
    tracking, the embedded newline becomes a plain space instead, the
    substitution stays one shlex token, and `cp`'s real last positional is
    found."""
    eff_cwd = str(tmp_path)
    command = "cp $(ls\ndir) /repo/subst.py"

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/subst.py" in targets
    assert os.path.join(eff_cwd, "$(ls") not in targets


def test_multiline_backtick_substitution_is_found(tmp_path):
    """Backtick variant of the case above: `` `...` `` spanning a real
    embedded newline must not fabricate a target from its broken-off tail
    either."""
    eff_cwd = str(tmp_path)
    command = "cp `ls\ndir` /repo/tick.py"

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/tick.py" in targets
    assert os.path.join(eff_cwd, "`ls") not in targets


def test_multiline_nested_dollar_paren_substitution_is_found(tmp_path):
    """Nested variant: a `$(...)` containing a second `$(...)`, with the
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
    """A quote genuinely unterminated at the END of a command does not
    destroy recovery of a well-formed logical unit that closed BEFORE it.
    The whole-stream attempt still raises (the trailing quote never closes),
    but `_split_logical_units` isolates the broken `echo 'unterminated` tail
    into its OWN unit — the earlier `git commit ... && cp e.py /repo/x.py`
    unit, itself containing a correctly-closed multi-line single-quoted
    argument, parses fine on its own and is recovered by the units-harvest
    half of the fallback's union — only the unit that actually carries the
    bad quote is unrecoverable, not the whole command; see
    test_unterminated_quote_on_the_same_unit_as_the_write_is_an_accepted_residual
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
    """Regression control: a quote-aware-only fallback (unit-by-unit, no
    splitlines union) would lose this write, because a genuinely
    unterminated quote never closes, so `_split_logical_units` folds every
    subsequent physical line into the SAME unparseable unit as the bad
    quote — the units-harvest alone finds nothing. The splitlines-harvest
    half of the union still recovers it, because on its own physical line
    `cp a.py /repo/after.py` parses fine independent of the previous line's
    bad quote."""
    eff_cwd = str(tmp_path)
    command = "echo 'unterminated\ncp a.py /repo/after.py"

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/after.py" in targets


def test_unterminated_quote_does_not_lose_multiple_later_writes(tmp_path):
    """Regression control, multi-write variant: both later physical lines'
    writes must survive the union fallback, not just the first one found."""
    eff_cwd = str(tmp_path)
    command = "echo 'unterminated\ncp a.py /repo/l1.py\ncp b.py /repo/l2.py"

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/l1.py" in targets
    assert "/repo/l2.py" in targets


def test_multiline_dollar_brace_expansion_is_found(tmp_path):
    """An UNQUOTED `${...}` parameter expansion whose body spans a real
    embedded newline — the primary whole-stream path. A newline-blind split
    would turn the embedded newline into a `;` separator with no awareness of
    the expansion around it, splitting `${x:-` and `y}` into two segments;
    `cp`'s destination scan would then see only the fragment `${x:-` as its
    sole positional and report a fabricated target, while the command's REAL
    destination (`/repo/brace.py`) is never reached. With `${`-depth
    tracking, the embedded newline becomes a plain space instead, the
    expansion stays one shlex token, and `cp`'s real last positional is
    found."""
    eff_cwd = str(tmp_path)
    command = "cp ${x:-\ny} /repo/brace.py"

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/brace.py" in targets
    assert os.path.join(eff_cwd, "${x:-") not in targets


def test_multiline_dollar_brace_expansion_fallback_still_recovers_write(tmp_path):
    """Same shape as the primary-path test above, but with a second,
    genuinely unterminated quote on a later physical line forcing the
    whole-stream `shlex.split()` to raise and the union fallback to engage.
    The units-harvest half keeps `${`-depth tracking active per unit, so the
    real destination is still found; the raw splitlines-harvest half
    independently contributes the harmless fabricated fragment
    `/repo/${x:-` (its `cp`-dest scan runs on the bare physical line, where
    the leading `${x:-` reads as a plain positional) — over-reporting here is
    the safe direction for a deny gate, so this extra fragment is accepted
    rather than asserted against."""
    eff_cwd = "/repo"
    command = "cp ${x:-\ny} /repo/brace.py\necho 'unterminated"

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/brace.py" in targets


def test_single_line_dollar_brace_expansion_unaffected(tmp_path):
    """Control: no newline inside the expansion at all — behavior is
    unaffected by the new depth counter."""
    eff_cwd = str(tmp_path)
    command = "cp ${x:-y} /repo/ok.py"

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/ok.py" in targets


def test_multiline_dollar_brace_nested_inside_dollar_paren_is_found(tmp_path):
    """A `${...}` expansion nested inside a `$(...)` substitution, with the
    embedded newline inside the innermost `${...}`. `subst_depth` and
    `brace_depth` are independent counters keyed on disjoint character pairs
    (`$(`/`)` vs `${`/`}`), so a newline protected by one is unaffected by
    the other reaching zero or not."""
    eff_cwd = str(tmp_path)
    command = "cp $(echo ${x:-\ny}) /repo/nested_brace.py"

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/nested_brace.py" in targets


def test_multiline_dollar_brace_inside_double_quotes_still_preserved(tmp_path):
    """Regression control: a `${...}` embedded inside a DOUBLE-quoted
    argument was already correctly handled before this fix (the pre-existing
    `not in_double` guard preserves every newline for the whole quoted span
    regardless of what is inside it) and must remain so — this fix must not
    touch the in_double path at all."""
    eff_cwd = str(tmp_path)
    command = 'echo "${x:-\ny}" > /repo/dq_brace.py'

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/dq_brace.py" in targets


def test_multiline_nested_dollar_brace_expansion_is_found(tmp_path):
    """Nested `${...}` inside `${...}` (a default-value expansion whose
    default is itself a parameter expansion), with the embedded newline
    inside the innermost one. `brace_depth` must reach back to 0 only at the
    OUTER closing `}`, not the inner one, or the outer expansion reopens as
    ordinary text partway through."""
    eff_cwd = str(tmp_path)
    command = "cp ${x:-${y:-\nz}} /repo/nested2.py"

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/nested2.py" in targets


def test_brace_group_does_not_corrupt_the_dollar_brace_depth_counter(tmp_path):
    """A bare (non-`$`) brace GROUP (`{ cmd; }`) is never itself tracked as
    an opener — only the two-character `${` sequence is — so its own closing
    `}` must not be misread as closing an unrelated, already-closed `${...}`
    span from earlier in the command. A real write target on a later,
    unrelated line must still be found."""
    eff_cwd = str(tmp_path)
    command = "echo ${a:-b}\n{ echo grouped; }\ncp c.py /repo/aftergroup.py"

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/aftergroup.py" in targets


def test_stray_unmatched_closing_brace_does_not_corrupt_the_depth_counter(tmp_path):
    """A stray literal `}` with no matching `${` earlier must not drive
    `brace_depth` negative (the guard is `brace_depth > 0`, so it falls
    through as an ordinary character), and must not corrupt tracking for the
    rest of the command — a real write target afterward is still found."""
    eff_cwd = str(tmp_path)
    command = "echo }\ncp c.py /repo/afterstray.py"

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/afterstray.py" in targets


def test_ansi_c_quoted_string_spanning_newline_is_found(tmp_path):
    """`$'...'` (ANSI-C quoting) needs no separate handling in this scanner:
    it is keyed on the same `'` delimiter as an ordinary single-quoted span,
    so an embedded newline inside it is already covered by the existing
    single-quote branch (the newline is preserved as a literal character
    inside the quoted string, exactly as `shlex.split()` itself reads a
    plain single-quoted argument — the leading `$` is just an ordinary
    character immediately before the quote)."""
    eff_cwd = str(tmp_path)
    command = "cp $'literal\ndata' /repo/ansic.py"

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/ansic.py" in targets


def test_double_bracket_test_construct_still_finds_a_later_real_write(tmp_path):
    """`[[ ... ]]` is a shell keyword-based compound command this scanner
    does not track as an opener at all — a newline inside one is read as a
    top-level unit break like any other. This does not cost a real write
    target in practice: the write-detecting verb on its own later, cleanly-
    split segment is found independent of the (mis-split) test construct
    around it."""
    eff_cwd = str(tmp_path)
    command = "[[ -f foo.txt &&\n -f bar.txt ]]\ncp c.py /repo/aftertest.py"

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/aftertest.py" in targets


def test_here_string_operand_unaffected_by_embedded_newline_handling(tmp_path):
    """A here-string (`<<<`) operand is a single following word, not a span
    that must stay open across a newline, so it needs no opener-style
    tracking at all; ordinary behavior (a real write target on the same
    single-line command) is unaffected."""
    eff_cwd = str(tmp_path)
    command = "cat <<< \"hello\" > /repo/herestr.py"

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/herestr.py" in targets


def test_standalone_bare_arithmetic_command_is_an_accepted_residual(tmp_path):
    """Accepted, documented residual: a standalone `((...))` arithmetic
    COMMAND (not the `$((...))` expansion form) opens with a bare `((`
    carrying no `$`, so it is never tracked as an opener at all — a newline
    inside one is read as a top-level unit break exactly as an ordinary
    newline would be, splitting the construct and potentially fabricating a
    fragment from its broken-off tail. Fixing this needs full paren-matching,
    the same materially bigger parser the bare-subshell-nested-in-`$(...)`
    residual needs, for the same reason — out of scope for a depth counter
    keyed on fixed two-character openers. This pins the current, non-
    crashing outcome; a real write target on a later, cleanly-split line is
    still found."""
    eff_cwd = str(tmp_path)
    command = "((1 +\n2))\ncp c.py /repo/afterarith.py"

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/afterarith.py" in targets


def test_unterminated_quote_fallback_still_recovers_a_multiline_dollar_paren_write(tmp_path):
    """`$(...)`-substitution variant: the same fallback-engagement shape as
    the multi-line-quoted-argument case above, but for an UNQUOTED `$(...)`
    substitution spanning a newline instead. A quote-BLIND per-line fallback
    would split `$(ls` and `dir)` onto separate physical lines, fabricating
    `<cwd>/$(ls` as `cp`'s only positional and losing the real destination.
    The units-harvest half of the union keeps `$(...)`-depth tracking active
    per unit, so `cp`'s real last positional is still found; the raw
    splitlines-harvest independently contributes the harmless fabricated
    fragment `$(ls` — over-reporting here is the safe direction for a deny
    gate."""
    eff_cwd = "/repo"
    command = "cp $(ls\ndir) /repo/subst.py\necho 'unterminated"

    targets = command_write_targets(command, eff_cwd)

    assert "/repo/subst.py" in targets
