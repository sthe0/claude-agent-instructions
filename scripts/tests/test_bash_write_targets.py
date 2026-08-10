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


def test_unterminated_quote_after_multiline_quoted_argument_still_returns_nothing(tmp_path):
    """Accepted, irreducible residual: once a quote is genuinely unterminated
    ANYWHERE in the command, there is no well-defined lexical reading of the
    remainder, so no target is recoverable there either — this is a
    documented fail-open boundary, not a bug to chase further. The
    whole-stream attempt raises (the trailing quote never closes), and the
    per-line fallback fares no better: physical splitting lands mid-quote on
    every line this command has, so every line fails its own `shlex.split()`
    in turn."""
    eff_cwd = str(tmp_path)
    command = "git commit -m 'a\nb' && cp e.py /repo/x.py\necho 'unterminated"

    targets = command_write_targets(command, eff_cwd)

    assert targets == []
