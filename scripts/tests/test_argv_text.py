"""Unit coverage for lib.argv_text — the `@<path>` convention that keeps
potentially-large text off argv (Linux MAX_ARG_STRLEN = 32 * PAGE_SIZE).

The escaping rule has four branches (verbatim / @@escape / @file / missing-file)
plus the None-vs-empty distinctions, and every consumer across agentctl and both
spawn wrappers depends on all of them agreeing, so each branch is pinned here
rather than at a call site. The non-recursion case (a FILE whose contents begin
with `@`) is pinned explicitly: it is the obvious wrong generalization.
"""
from __future__ import annotations

import pytest

from lib import argv_text


# --- the four branches of read_arg_text ---------------------------------------

def test_plain_text_returned_verbatim():
    assert argv_text.read_arg_text("just prose") == "just prose"


def test_at_file_returns_contents(tmp_path):
    f = tmp_path / "dossier.md"
    f.write_text("the dossier body", encoding="utf-8")
    assert argv_text.read_arg_text(f"@{f}") == "the dossier body"


def test_double_at_is_an_escaped_literal():
    assert argv_text.read_arg_text("@@mention") == "@mention"


def test_escaped_literal_is_never_probed_as_a_path(tmp_path):
    # '@@/no/such/file' means the literal '@/no/such/file' — it must NOT raise,
    # because it was never a reference.
    assert argv_text.read_arg_text("@@/no/such/file") == "@/no/such/file"


def test_missing_file_raises_naming_the_path_and_the_escape(tmp_path):
    missing = tmp_path / "absent.md"
    with pytest.raises(SystemExit) as exc:
        argv_text.read_arg_text(f"@{missing}")
    msg = str(exc.value)
    assert str(missing) in msg
    assert "@@" in msg


def test_a_realistically_long_path_is_named_in_full(tmp_path):
    # The truncation guard must not fire on an ordinary deep path, or the error
    # stops naming the very thing the caller has to fix.
    deep = tmp_path / ("nested/" * 12) / "absent.md"
    with pytest.raises(SystemExit) as exc:
        argv_text.read_arg_text(f"@{deep}")
    assert str(deep) in str(exc.value)


def test_bare_at_raises():
    with pytest.raises(SystemExit):
        argv_text.read_arg_text("@")


def test_directory_reference_raises_rather_than_leaking_an_oserror(tmp_path):
    with pytest.raises(SystemExit):
        argv_text.read_arg_text(f"@{tmp_path}")


def test_overlong_reference_is_a_clean_exit_not_an_enametoolong_traceback():
    # An inline payload that happens to start with '@' would blow up inside
    # Path.is_file() with OSError ENAMETOOLONG; the guard must convert it.
    with pytest.raises(SystemExit) as exc:
        argv_text.read_arg_text("@" + "x" * 5000)
    msg = str(exc.value)
    assert "5000 chars" in msg
    assert len(msg) < 500  # the 5000-char "path" is truncated, not echoed whole


# --- absent vs empty ----------------------------------------------------------

def test_none_stays_none():
    assert argv_text.read_arg_text(None) is None


def test_empty_string_stays_empty_string():
    assert argv_text.read_arg_text("") == ""


# --- the rule is not recursive ------------------------------------------------

def test_file_contents_beginning_with_at_are_returned_untouched(tmp_path):
    inner = tmp_path / "inner.md"
    inner.write_text("@literal-at-start", encoding="utf-8")
    assert argv_text.read_arg_text(f"@{inner}") == "@literal-at-start"


def test_file_contents_are_rstripped(tmp_path):
    # Documented caveat: a staged value is NOT byte-identical to the inline form.
    f = tmp_path / "trailing.md"
    f.write_text("body\n\n", encoding="utf-8")
    assert argv_text.read_arg_text(f"@{f}") == "body"


# --- read_arg_text_list -------------------------------------------------------

def test_list_none_stays_none():
    assert argv_text.read_arg_text_list(None) is None


def test_list_empty_stays_empty():
    assert argv_text.read_arg_text_list([]) == []


def test_list_is_element_wise(tmp_path):
    f = tmp_path / "one.md"
    f.write_text("from file", encoding="utf-8")
    assert argv_text.read_arg_text_list([f"@{f}", "inline", "@@esc"]) == [
        "from file",
        "inline",
        "@esc",
    ]


def test_list_propagates_a_missing_reference(tmp_path):
    with pytest.raises(SystemExit):
        argv_text.read_arg_text_list(["fine", f"@{tmp_path / 'absent.md'}"])


# --- stage_text_to_tempfile ---------------------------------------------------

def test_staged_text_round_trips_through_read_arg_text():
    text = "a" * 200_000  # deliberately past MAX_ARG_STRLEN
    path = argv_text.stage_text_to_tempfile(text)
    try:
        assert path.is_absolute()
        assert argv_text.read_arg_text(f"@{path}") == text
    finally:
        path.unlink()


def test_staged_reference_is_short_enough_for_argv():
    path = argv_text.stage_text_to_tempfile("x" * 200_000)
    try:
        assert len(f"@{path}".encode()) <= argv_text.MAX_ARG_STRLEN
    finally:
        path.unlink()
