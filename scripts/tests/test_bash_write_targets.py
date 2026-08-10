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
