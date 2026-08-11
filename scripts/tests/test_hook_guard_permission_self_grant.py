"""Tests for hook-guard-permission-self-grant.decide() -- the three-way conjunction
(widening x armed x relevant) that refuses a permission SELF-GRANT.

Hermetic. Every settings file is written under `tmp_path`; every transcript is a
SYNTHETIC JSONL fixture under `fixtures/` -- no real session transcript is read by
these tests, and none may be. Fixtures shared with the arming primitive are reused
from `fixtures/denial_arming/` rather than duplicated; the four under
`fixtures/permission_self_grant/` exist because this suite needs control over the
DENIED CALL itself (which tool, which operand) in order to exercise relevance, and no
existing fixture offers that.

What each block proves:

  * the three DENY directions, one per tool path;
  * that each conjunct is load-bearing -- (b) via the not-armed and non-arming-kind
    allows, (c) via the different-tool and different-prefix allows, and both again as
    explicit MUTATION CONTROLS that force the conjunct's value and watch an ALLOW flip;
  * that (a) really is a WIDENING test rather than a settings-file guard -- narrowing,
    reordering, reformatting and an unrelated key all pass while armed;
  * that (a) is THREE-valued: a target ABSENT from disk is a creation and is allowed
    outright, while every way of NOT being able to answer (a) -- an unreadable target,
    an untokenizable command, a payload missing the fields the gate reads -- is UNKNOWN
    and is pinned in BOTH directions: allowed when the session is not armed, resolved
    through `_ON_ERROR` when it is. Each direction is a real row, because a one-
    directional pin cannot tell the intended routing from either flat verdict;
  * the two fail directions the primitives hand up: an unresolvable denied call fails
    toward COVERING (deny), and everything internal fails through `_ON_ERROR`, asserted
    for BOTH of its values so flipping the constant rots no test;
  * the version-STABLE Bash rows, including the ones that pin a destination DIRECTORY as
    the files written inside it (`cp x d/`, `install -m 600 x d`) against the controls that
    keep the resolution from turning `cp` itself into a refusal. The two-line
    phantom-target row is deliberately absent: its verdict depends on the lexer revision,
    so pinning it would plant a test that goes red on another stage's landing with nobody
    owning the fix.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from lib.denial_arming import Arming, DeniedCall, Verdict  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "guard_permission_self_grant", SCRIPTS_DIR / "hook-guard-permission-self-grant.py"
)
hook = importlib.util.module_from_spec(_SPEC)
# Registered before execution because `@dataclass` resolves annotations through
# sys.modules[cls.__module__]; an unregistered module makes that lookup return None.
sys.modules[_SPEC.name] = hook
_SPEC.loader.exec_module(hook)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "permission_self_grant"
ARMING_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "denial_arming"

# Armed by a permission-rule denial of `Read /srv/secrets/notes.md`.
ARMED_READ = FIXTURES / "armed_read_denied.jsonl"
# Armed by a user-rejected denial of `Bash git push --force`.
ARMED_BASH = FIXTURES / "armed_bash_denied.jsonl"
# Armed, but the denied call does not resolve -- the fail-toward-covering case.
UNRESOLVED = FIXTURES / "unresolved_only.jsonl"
# Readable, no denial of any kind.
NO_DENIAL = FIXTURES / "no_denial.jsonl"
# Readable, denials present but all of the four NON-arming kinds.
NON_ARMING = ARMING_FIXTURES / "only_non_arming.jsonl"

COVERING = "Read(/srv/secrets/**)"        # would have permitted the denied Read
OTHER_TOOL = "Bash(git status:*)"          # different tool from the denied call
OTHER_PREFIX = "Read(/etc/**)"             # same tool, unrelated path
SEED = "Bash(ls:*)"


def settings_text(allow=(SEED,), deny=(), indent=2) -> str:
    return json.dumps(
        {"permissions": {"allow": list(allow), "deny": list(deny)}}, indent=indent
    ) + "\n"


def write_settings(tmp_path: Path, allow=(SEED,), deny=(), name="settings.json") -> Path:
    path = tmp_path / name
    path.write_text(settings_text(allow, deny), encoding="utf-8")
    return path


def add_entry_edit(path: Path, entry: str) -> dict:
    """An Edit that inserts `entry` into the seeded allow list -- the self-grant shape."""
    return {
        "file_path": str(path),
        "old_string": f'"{SEED}"',
        "new_string": f'"{SEED}",\n      "{entry}"',
    }


def add_entry_edit_relative(name: str, entry: str) -> dict:
    """`add_entry_edit` against a RELATIVE target, so the payload's `cwd` is load-bearing.

    The gate consults `cwd` only to resolve a relative target; against an absolute one it
    never reads the field at all. A test that varies `cwd` while passing an absolute path
    therefore asserts something the code cannot violate -- it stays green under a mutation
    that removes the very handling it was written to pin. Measured, not assumed: with the
    type check dropped from `_str_field`, the absolute form of the rows below passed and the
    relative form fails.
    """
    return {"file_path": name, "old_string": f'"{SEED}"',
            "new_string": f'"{SEED}",\n      "{entry}"'}


def replace_all_edit(path: Path, new_text: str) -> dict:
    return {"file_path": str(path), "old_string": path.read_text(), "new_string": new_text}


def armed_transcript_with_call(tmp_path: Path, name, tool_input) -> Path:
    """`ARMED_READ`, rebuilt with the denied `tool_use` block's fields under test control.

    The static fixtures cover the shapes a healthy client writes. This builds the same two
    rows -- an assistant `tool_use` and the `permission-rule` row whose
    `sourceToolAssistantUUID` resolves to it -- with `name`/`input` set to anything, because
    the transcript is a file written by ANOTHER process and its field types are as untrusted
    as the payload's. Defaults reproduce the fixture exactly, so a row that varies one field
    varies only that field.
    """
    path = tmp_path / "armed_custom.jsonl"
    rows = [
        {"type": "assistant", "uuid": "asst-c1", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_c1", "name": name, "input": tool_input}]}},
        {"type": "user", "uuid": "den-c1", "toolDenialKind": "permission-rule",
         "sourceToolAssistantUUID": "asst-c1",
         "toolUseResult": "Error: Claude requested permissions to read from "
                          "/srv/secrets/notes.md, but you haven't granted it yet."},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def payload(tool_name: str, tool_input: dict, transcript: Path, cwd: Path) -> dict:
    return {
        "tool_name": tool_name,
        "tool_input": tool_input,
        "transcript_path": str(transcript),
        "cwd": str(cwd),
    }


# --- the shipped policy -----------------------------------------------------------

def test_on_error_ships_fail_closed():
    # One constant, one value. Every _ON_ERROR-governed case below is asserted for
    # both values, so changing this line is a one-line change and rots no test.
    assert hook._ON_ERROR == "deny"


# --- widening x armed x relevant -> DENY, one per tool path -----------------------

def test_edit_that_grants_the_denied_read_is_denied(tmp_path):
    s = write_settings(tmp_path)
    reason = hook.decide(payload("Edit", add_entry_edit(s, COVERING), ARMED_READ, tmp_path))
    assert reason is not None
    assert COVERING in reason
    assert "permission-rule" in reason
    assert "stop and ask the user" in reason


def test_write_that_grants_the_denied_read_is_denied(tmp_path):
    s = write_settings(tmp_path)
    tool_input = {"file_path": str(s), "content": settings_text([SEED, COVERING])}
    reason = hook.decide(payload("Write", tool_input, ARMED_READ, tmp_path))
    assert reason is not None
    assert COVERING in reason


def test_bash_write_to_a_permission_surface_while_armed_is_denied(tmp_path):
    s = write_settings(tmp_path)
    reason = hook.decide(payload(
        "Bash", {"command": f"cp evil.json {s}"}, ARMED_BASH, tmp_path))
    assert reason is not None
    assert str(s) in reason
    # The Bash path cannot check relevance, so its message must hand over the escape
    # for the one false-positive shape it is known to have.
    assert "PHANTOM" in reason


def test_bash_denies_on_an_unrelated_denial_too_the_r7_asymmetry(tmp_path):
    # R7 shipped, not merely described: the Bash path has no not-relevant branch, so an
    # arming denial of a completely unrelated tool still denies a write to the surface.
    # This is the one place the gate is knowingly coarser than its own contract.
    s = write_settings(tmp_path)
    assert hook.decide(payload(
        "Bash", {"command": f"cp evil.json {s}"}, ARMED_READ, tmp_path)) is not None


# --- widening x NOT armed -> ALLOW, one per tool path -----------------------------

def test_widening_without_any_denial_is_allowed_on_every_path(tmp_path):
    s = write_settings(tmp_path)
    assert hook.decide(payload("Edit", add_entry_edit(s, COVERING), NO_DENIAL, tmp_path)) is None
    assert hook.decide(payload(
        "Write", {"file_path": str(s), "content": settings_text([SEED, COVERING])},
        NO_DENIAL, tmp_path)) is None
    assert hook.decide(payload(
        "Bash", {"command": f"cp evil.json {s}"}, NO_DENIAL, tmp_path)) is None


def test_widening_after_only_non_arming_denials_is_allowed_on_every_path(tmp_path):
    # cancelled / interrupted / automode-unavailable / automode-parsing-error express no
    # permission judgement. Arming on them would turn an Esc keypress into a lock on the
    # settings surface.
    s = write_settings(tmp_path)
    assert hook.decide(payload("Edit", add_entry_edit(s, COVERING), NON_ARMING, tmp_path)) is None
    assert hook.decide(payload(
        "Write", {"file_path": str(s), "content": settings_text([SEED, COVERING])},
        NON_ARMING, tmp_path)) is None
    assert hook.decide(payload(
        "Bash", {"command": f"cp evil.json {s}"}, NON_ARMING, tmp_path)) is None


# --- widening x armed x NOT relevant -> ALLOW (Edit and Write, both forms) ---------

@pytest.mark.parametrize("tool", ["Edit", "Write"])
@pytest.mark.parametrize("entry", [OTHER_TOOL, OTHER_PREFIX])
def test_widening_that_does_not_cover_the_denied_call_is_allowed(tmp_path, tool, entry):
    # Editing a permission surface is ordinary maintenance; only granting yourself the
    # thing you were just refused is the act being caught. Both non-covering forms:
    # a different tool, and the same tool with an unrelated path prefix.
    s = write_settings(tmp_path)
    if tool == "Edit":
        tool_input = add_entry_edit(s, entry)
    else:
        tool_input = {"file_path": str(s), "content": settings_text([SEED, entry])}
    assert hook.decide(payload(tool, tool_input, ARMED_READ, tmp_path)) is None


# --- (a) is a WIDENING test, not a settings-file guard ----------------------------

def test_removing_an_allow_entry_while_armed_is_allowed(tmp_path):
    s = write_settings(tmp_path, allow=[SEED, COVERING])
    tool_input = replace_all_edit(s, settings_text([SEED]))
    assert hook.decide(payload("Edit", tool_input, ARMED_READ, tmp_path)) is None


def test_adding_a_deny_entry_while_armed_is_allowed(tmp_path):
    s = write_settings(tmp_path)
    tool_input = replace_all_edit(s, settings_text([SEED], deny=["Bash(rm:*)"]))
    assert hook.decide(payload("Edit", tool_input, ARMED_READ, tmp_path)) is None


def test_reordering_reformatting_and_an_unrelated_key_while_armed_are_allowed(tmp_path):
    s = write_settings(tmp_path, allow=[SEED, OTHER_PREFIX])

    reordered = settings_text([OTHER_PREFIX, SEED])
    assert hook.decide(payload(
        "Edit", replace_all_edit(s, reordered), ARMED_READ, tmp_path)) is None

    reformatted = settings_text([SEED, OTHER_PREFIX], indent=4)
    assert hook.decide(payload(
        "Edit", replace_all_edit(s, reformatted), ARMED_READ, tmp_path)) is None

    doc = json.loads(s.read_text())
    doc["model"] = "opus"
    assert hook.decide(payload(
        "Edit", replace_all_edit(s, json.dumps(doc, indent=2) + "\n"),
        ARMED_READ, tmp_path)) is None


def test_a_json_file_that_is_not_a_permission_surface_is_allowed(tmp_path):
    # The `permissions/*.json` shape managed by scripts/permissions-cli.py is a LIST,
    # a different schema entirely -- recognition is by shape, so it must not be caught.
    other = tmp_path / "workflow.json"
    other.write_text(json.dumps({"permissions": ["Read(/srv/**)"]}, indent=2) + "\n")
    tool_input = replace_all_edit(
        other, json.dumps({"permissions": ["Read(/srv/**)", COVERING]}, indent=2) + "\n")
    assert hook.decide(payload("Edit", tool_input, ARMED_READ, tmp_path)) is None


def test_a_non_json_file_is_allowed(tmp_path):
    note = tmp_path / "note.md"
    note.write_text(f"we should add {COVERING} one day\n")
    tool_input = {"file_path": str(note), "old_string": "one day", "new_string": "later"}
    assert hook.decide(payload("Edit", tool_input, ARMED_READ, tmp_path)) is None


def test_writing_a_settings_file_absent_from_disk_is_a_creation_not_a_widening(tmp_path):
    # The ABSENT branch specifically, and only it: nothing is on the path, so there is no
    # prior surface to widen, and a brand-new settings file is not how a denial gets
    # cleared. A path that EXISTS but cannot be read is a different fact on a different
    # branch -- it is UNKNOWN, not a creation; see the UNKNOWN block below. The two shared
    # one `None` once, which is how "I could not look" came to answer "nothing here".
    fresh = tmp_path / "new-settings.json"
    tool_input = {"file_path": str(fresh), "content": settings_text([COVERING])}
    assert hook.decide(payload("Write", tool_input, ARMED_READ, tmp_path)) is None


def test_a_call_touching_no_permission_surface_never_opens_the_transcript(tmp_path):
    # The ordering claim, made observable: the transcript path does not exist, so
    # reaching conjunct (b) would return UNREADABLE and deny under the shipped
    # _ON_ERROR. Allowing proves (a) short-circuited first.
    note = tmp_path / "note.md"
    note.write_text("hello\n")
    tool_input = {"file_path": str(note), "old_string": "hello", "new_string": "bye"}
    assert hook.decide(payload(
        "Edit", tool_input, tmp_path / "no-such-transcript.jsonl", tmp_path)) is None


def test_an_unmodelled_tool_is_allowed(tmp_path):
    assert hook.decide(payload(
        "Read", {"file_path": "/srv/secrets/notes.md"},
        tmp_path / "no-such-transcript.jsonl", tmp_path)) is None


def test_an_unmodelled_tool_is_allowed_whatever_shape_its_input_has(tmp_path):
    # Tool dispatch happens BEFORE any payload field is read, so the malformed-input
    # UNKNOWN below can never reach a tool this gate does not model -- even while armed,
    # where an UNKNOWN would otherwise deny. A gate that denied unmodelled tools would be
    # far outside its remit, and their input shape is not its business.
    for tool_input in ["not an object", None, 17, {"anything": "at all"}]:
        assert hook.decide({
            "tool_name": "WebFetch",
            "tool_input": tool_input,
            "transcript_path": str(ARMED_READ),
            "cwd": str(tmp_path),
        }) is None


# --- (a) is THREE-valued: could-not-look is not looked-and-found-nothing -----------

# Every way conjunct (a) can come out UNKNOWN, by label. Both directions of each are
# pinned below: UNKNOWN alone is neither an allow nor a deny.
UNKNOWN_A_LABELS = (
    "edit-target-unreadable",
    "write-target-unreadable",
    "bash-target-unreadable",
    "edit-without-file-path",
    "write-without-content-string",
    "edit-without-old-and-new-strings",
    "bash-without-command-string",
    "tool-input-is-not-an-object",
    "target-path-carrying-a-nul-byte",
)

# Every string the gate reads out of the payload ITSELF, with a value of the wrong type.
# These are not UNKNOWN-(a) shapes -- each must be absorbed by `_str_field` and behave
# exactly as if the field were absent, so the call is judged on what remains. The class,
# not the `transcript_path` instance a review named: a per-field fix leaves the next field
# open, and a wrong-typed value reaches `main()`'s catch-all, which denies unconditionally.
# Measured under the mutation that drops `_str_field`'s type check: the `cwd` and
# `transcript_path` rows go red (both build a path from the value), the `tool_name` rows stay
# green -- that field is only ever COMPARED, so a wrong-typed value already matches nothing,
# exactly as an absent one does. Kept anyway, as the third instance of a class invariant the
# review named on one field; honest about which two rows are the load-bearing ones.
WRONG_TYPED_PAYLOAD_FIELDS = ("tool_name", "cwd", "transcript_path")
WRONG_TYPES = (7, ["a"], {"a": 1}, True)
# For a field declared `dict`, `{"a": 1}` is the RIGHT type -- reusing `WRONG_TYPES` there
# ships one param per row that asserts nothing, which is the inert-row defect in miniature
# (measured: under a mutation trusting `input`, three of four params failed and the dict one
# passed). A field's wrong-type set has to be derived from the type it declares.
NON_DICT_TYPES = (7, ["a"], "str", True)


def unknown_a_call(tmp_path: Path, label: str):
    """`(tool_name, tool_input)` for one UNKNOWN-(a) shape.

    The unreadable target is a DANGLING SYMLINK, and the staging is load-bearing rather
    than incidental. It used to be a DIRECTORY, on the reasoning that EISDIR stages an
    unreadable target for any uid where root would read straight through a chmod. That
    reasoning is now wrong: a directory is a target the gate IDENTIFIED and can say a
    definite no about (`_Read.NOT_A_SURFACE` — a directory is not a JSON document), so
    these three rows were pinning UNKNOWN on a shape that is an established negative, and
    they went red the moment the gate started answering it. A dangling symlink is the
    honest staging and is still uid-independent: `os.stat` raises ENOENT while
    `os.path.lexists` is True, which is exactly "something IS on this path and the gate
    could not read it". The EACCES form has its own row; the established-negative shapes
    have theirs.
    """
    blocked = tmp_path / "settings.json"
    if not blocked.is_symlink():
        blocked.symlink_to(tmp_path / "nowhere.json")
    real = write_settings(tmp_path, name="real-settings.json")
    return {
        "edit-target-unreadable": (
            "Edit", {"file_path": str(blocked), "old_string": "a", "new_string": "b"}),
        "write-target-unreadable": (
            "Write", {"file_path": str(blocked), "content": settings_text([COVERING])}),
        "bash-target-unreadable": ("Bash", {"command": f"cp evil.json {blocked}"}),
        "edit-without-file-path": ("Edit", {"old_string": "a", "new_string": "b"}),
        "write-without-content-string": ("Write", {"file_path": str(real), "content": None}),
        "edit-without-old-and-new-strings": ("Edit", {"file_path": str(real)}),
        "bash-without-command-string": ("Bash", {}),
        "tool-input-is-not-an-object": ("Edit", "not an object"),
        # A NUL in the path raises ValueError BEFORE the syscall, so it is not an OSError
        # and only reaches `_Unreadable` because _read_text catches it explicitly.
        # Uncaught it escapes to `main()`'s catch-all, which denies unconditionally -- this
        # row is the one that keeps that deny out of a session carrying no denial at all.
        "target-path-carrying-a-nul-byte": (
            "Edit",
            {"file_path": f"{tmp_path}/se\x00ttings.json", "old_string": "a", "new_string": "b"}),
    }[label]


@pytest.mark.parametrize("label", UNKNOWN_A_LABELS)
def test_an_unknown_conjunct_a_is_allowed_when_the_session_is_not_armed(tmp_path, label):
    # `UNKNOWN AND False` is False. Routing an unresolved (a) straight to `_ON_ERROR`
    # would deny before (b) was ever asked -- refusing these calls in every session that
    # carries no permission denial at all, where a self-grant is impossible by
    # construction. This row is what keeps that broad false-deny out.
    tool_name, tool_input = unknown_a_call(tmp_path, label)
    assert hook.decide(payload(tool_name, tool_input, NO_DENIAL, tmp_path)) is None


@pytest.mark.parametrize("label", UNKNOWN_A_LABELS)
@pytest.mark.parametrize("on_error,denies", [("deny", True), ("allow", False)])
def test_an_unknown_conjunct_a_resolves_through_on_error_while_armed(
    tmp_path, monkeypatch, label, on_error, denies
):
    # The other direction, and the one the gate exists for: armed, and the gate could not
    # establish that this call does NOT widen a permission surface. A self-grant cannot be
    # ruled out, so the shipped fail-closed constant refuses.
    monkeypatch.setattr(hook, "_ON_ERROR", on_error)
    tool_name, tool_input = unknown_a_call(tmp_path, label)
    reason = hook.decide(payload(tool_name, tool_input, ARMED_READ, tmp_path))
    assert (reason is not None) is denies


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file, so EACCES cannot be staged")
def test_an_edit_target_unreadable_by_mode_is_unknown_too(tmp_path):
    # EACCES specifically, beside the dangling-symlink ENOENT form above: the branch is
    # "the read failed while something IS on the path", not any one errno. A regular file
    # within the size cap, so it reaches the open() rather than being answered by `stat`.
    blocked = write_settings(tmp_path)
    blocked.chmod(0o000)
    call = {
        "file_path": str(blocked), "old_string": f'"{SEED}"', "new_string": f'"{SEED}", "x"'}
    assert hook.decide(payload("Edit", call, NO_DENIAL, tmp_path)) is None
    assert hook.decide(payload("Edit", call, ARMED_READ, tmp_path)) is not None


@pytest.fixture
def hermetic_process_cwd(tmp_path, monkeypatch):
    """Park the PROCESS cwd in an empty directory for the duration of one test.

    Rows that suppress the payload's `cwd` fall back to the process cwd, so without this
    they read `<pytest invocation dir>/settings.json` -- outside `tmp_path`, against this
    suite's hermeticity claim. Harmless in practice today (measured: green from a hostile cwd
    holding both a widening `settings.json` and a directory of that name), but a test whose
    inputs include the developer's shell state is one environment change from lying.
    """
    empty = tmp_path / "process-cwd-with-no-settings"
    empty.mkdir()
    monkeypatch.chdir(empty)
    return empty


@pytest.mark.parametrize("field", WRONG_TYPED_PAYLOAD_FIELDS)
@pytest.mark.parametrize("value", WRONG_TYPES)
def test_a_wrong_typed_payload_field_behaves_exactly_as_an_absent_one(
    tmp_path, hermetic_process_cwd, field, value
):
    # The class invariant. `_str_field` absorbs a wrong-typed field, so the verdict is
    # whatever the gate reaches on what remains -- never an exception escaping into
    # `main()`'s catch-all, which denies unconditionally and so would answer a question
    # about the payload's SHAPE with a verdict about the CALL.
    write_settings(tmp_path)
    base = payload("Edit", add_entry_edit_relative("settings.json", COVERING), ARMED_READ, tmp_path)
    absent = {k: v for k, v in base.items() if k != field}
    assert hook.decide({**base, field: value}) == hook.decide(absent)


@pytest.mark.parametrize("value", WRONG_TYPES)
@pytest.mark.parametrize("transcript,denies", [(NO_DENIAL, False), (ARMED_READ, True)],
                         ids=["not-armed", "armed"])
def test_a_wrong_typed_cwd_falls_back_without_changing_the_verdict(
    tmp_path, monkeypatch, value, transcript, denies
):
    # A malformed payload field must manufacture neither a deny nor an allow. THREE drafts
    # were needed to assert that, and each earlier one was inert on a different axis -- worth
    # recording, because the trap is not obvious and it caught the same author twice:
    #
    #   v1: wrong-typed `cwd` + an ABSOLUTE target. The gate never consults `cwd` for an
    #       absolute path, so the `cwd` axis asserted nothing the code could violate.
    #   v2: relative target, and both arming states -- but the fallback landed in an EMPTY
    #       process directory, (a) came back "does not widen", and the transcript was never
    #       opened. `armed_calls=0` on all eight params; the ARMING axis was now the dead one.
    #   v3, below: relative target AND a process directory that really holds the surface, so
    #       BOTH axes are load-bearing at once. The wrong-typed `cwd` must fall back to the
    #       process directory (killed by dropping `_str_field`'s type check: the value reaches
    #       `os.path.join` and raises into the catch-all, which denies the not-armed row), and
    #       the verdict must still be decided by arming (killed by any mutation to (b)).
    #
    # The general lesson the third draft encodes: a row that varies one input must place every
    # OTHER input so the varied one can actually change the outcome.
    write_settings(tmp_path)
    monkeypatch.chdir(tmp_path)
    call = payload(
        "Edit", add_entry_edit_relative("settings.json", COVERING), transcript, tmp_path)
    assert (hook.decide({**call, "cwd": value}) is not None) is denies


@pytest.mark.parametrize("transcript,denies", [(NO_DENIAL, False), (ARMED_READ, True)],
                         ids=["not-armed", "armed"])
def test_a_relative_cwd_leaves_both_paths_unable_to_locate_the_target(
    tmp_path, monkeypatch, transcript, denies
):
    # The two paths used to disagree, and that disagreement is the whole finding. The
    # file-tool path tested the BASE before joining (`if not cwd`) while the Bash path tested
    # the RESULT after (`os.path.isabs`). A relative but non-empty `cwd` -- "sub" -- passes
    # "the base is non-empty" and still produces a relative join, so one and the same
    # unresolvable target was answered `Bash` DENY, `Edit` ALLOW in one and the same session.
    #
    # Asserting the two TOGETHER is deliberate: either alone would have passed throughout the
    # revision that had the bug. What is pinned is that they agree, and on which answer.
    (tmp_path / "sub").mkdir()
    write_settings(tmp_path / "sub")
    doomed = tmp_path / "about-to-vanish"
    doomed.mkdir()
    monkeypatch.chdir(doomed)
    doomed.rmdir()
    edit = payload("Edit", add_entry_edit_relative("settings.json", COVERING),
                   transcript, tmp_path)
    bash = payload("Bash", {"command": "cp /tmp/evil.json settings.json"},
                   transcript, tmp_path)
    verdicts = [hook.decide({**edit, "cwd": "sub"}), hook.decide({**bash, "cwd": "sub"})]
    assert [v is not None for v in verdicts] == [denies, denies]


def test_a_relative_cwd_does_not_silently_resolve_against_the_process_directory(
    tmp_path, monkeypatch
):
    # The same defect in its dangerous form. With the process directory ALIVE, a relative
    # `cwd` joined into a path that resolved against it -- so the gate opened, diffed and
    # ruled on a settings file that nothing in the payload named. It denied, which looks like
    # the right answer and is not: the evidence was a different file's contents.
    #
    # The decoy is a surface the gate WOULD deny on if it read it, so an implementation that
    # resolves against the process directory fails here rather than passing by luck.
    (tmp_path / "sub").mkdir()
    write_settings(tmp_path / "sub")          # the payload's own, and never reachable
    live = tmp_path / "live"
    (live / "sub").mkdir(parents=True)
    write_settings(live / "sub")              # the decoy the ambient cwd would reach
    monkeypatch.chdir(live)
    call = payload("Edit", add_entry_edit_relative("settings.json", COVERING),
                   ARMED_READ, tmp_path)
    reason = hook.decide({**call, "cwd": "sub"})
    assert reason is not None                  # unresolvable + armed -> fail closed
    assert COVERING not in reason              # ... on `_ON_ERROR`, NOT on a widening it
    assert str(live) not in reason             #     "read" out of the decoy


@pytest.mark.parametrize("value", NON_DICT_TYPES)
def test_a_wrong_typed_tool_use_input_in_the_transcript_still_denies(tmp_path, value):
    # THE THIRD INPUT AXIS. Two reviews closed wrong-typed data on the payload and on ambient
    # process state, and each time the prose claimed the enumeration was complete. It was not:
    # the transcript is a file written by another process, and its `tool_use` blocks were
    # stored straight off `json.loads`. `covers()` rescues `None` and `{}` but not a TRUTHY
    # non-dict, so `["x"]`/`"str"`/`7`/`True` raised `AttributeError` out of `decide()` into
    # the catch-all -- which denies every call in the session, over a transcript the gate
    # merely failed to model.
    #
    # The assertion is DENY, and specifically the widening deny that names the entry: the
    # denial still arms, the call it denied is merely unknown, and an unknown denied call is
    # already modelled as covering. A row asserting only "does not raise" would pass on the
    # catch-all's unconditional deny, i.e. on the bug.
    settings = write_settings(tmp_path)
    transcript = armed_transcript_with_call(tmp_path, "Read", value)
    reason = hook.decide(payload("Edit", add_entry_edit(settings, COVERING),
                                 transcript, tmp_path))
    assert reason is not None
    assert COVERING in reason


@pytest.mark.parametrize("value", WRONG_TYPES)
def test_a_wrong_typed_tool_use_name_in_the_transcript_does_not_open_a_hole(tmp_path, value):
    # The sibling field, and the reason the fix belongs at the parse boundary rather than at
    # either use site: this one fails in the OPPOSITE direction. A non-str `name` raises
    # nothing at all -- it simply compares unequal to every entry's tool, so conjunct (c)
    # concluded "covered by no entry" and the gate ALLOWED a widening that answers a real
    # denial. Measured before the fix: `name=7` -> ALLOW where `name="Read"` -> DENY, with
    # everything else identical. A silent hole, which no crash would ever have surfaced.
    #
    # `reason is not None` alone was not enough here, and the sibling row's own comment says
    # why: an unconditional `_ON_ERROR` deny satisfies it just as well as the intended
    # verdict, so the row would have passed on a gate that denied for the wrong reason. The
    # entry must appear in the message -- that is what says the widening was judged and found
    # to cover the denied call, rather than the transcript merely having defeated the gate.
    settings = write_settings(tmp_path)
    transcript = armed_transcript_with_call(
        tmp_path, value, {"file_path": "/srv/secrets/notes.md"})
    reason = hook.decide(payload("Edit", add_entry_edit(settings, COVERING),
                                 transcript, tmp_path))
    assert reason is not None
    assert COVERING in reason


def test_the_custom_transcript_builder_reproduces_the_static_fixture(tmp_path):
    # The two rows above rest entirely on this builder being a faithful stand-in for
    # `ARMED_READ`. If a client-schema change made the builder's rows stop arming, both rows
    # would keep passing for the wrong reason on the `name` one and start failing
    # uninformatively on the `input` one. Pinned with the DEFAULTS, so the control is the
    # fixture's own shape.
    settings = write_settings(tmp_path)
    faithful = armed_transcript_with_call(
        tmp_path, "Read", {"file_path": "/srv/secrets/notes.md"})
    call = add_entry_edit(settings, COVERING)
    assert (hook.decide(payload("Edit", call, faithful, tmp_path))
            == hook.decide(payload("Edit", call, ARMED_READ, tmp_path)))
    # ... and it is the COVERING relationship that decides, not merely "armed".
    assert hook.decide(payload(
        "Edit", add_entry_edit(settings, OTHER_PREFIX), faithful, tmp_path)) is None


def test_a_relative_target_resolves_against_the_payload_cwd(tmp_path):
    # Nothing else pins this: a mutation that ignores `cwd` and treats a relative
    # `file_path` as-is killed no row before this one existed. It is also what makes `cwd`
    # a real input rather than decoration -- the same call is a self-grant or is nothing,
    # depending only on the directory the payload names.
    write_settings(tmp_path)
    elsewhere = tmp_path / "no-settings-here"
    elsewhere.mkdir()
    call = add_entry_edit_relative("settings.json", COVERING)
    assert hook.decide(payload("Edit", call, ARMED_READ, tmp_path)) is not None
    assert hook.decide(payload("Edit", call, ARMED_READ, elsewhere)) is None


@pytest.mark.parametrize("transcript,denies", [(NO_DENIAL, False), (ARMED_READ, True)],
                         ids=["not-armed", "armed"])
def test_the_gate_still_judges_correctly_when_its_own_directory_is_gone(
    tmp_path, monkeypatch, transcript, denies
):
    # A regression this suite's own author introduced and a review caught. `os.getcwd()` as
    # a default ARGUMENT is evaluated eagerly on every modelled call, and it raises once the
    # process's directory has been removed -- so the gate crashed into `main()`'s catch-all
    # and denied unconditionally, on a payload that was completely well-formed. Live in this
    # fleet: landing a unit deletes its worktree under any session still sitting in it.
    #
    # The assertion is the correct VERDICT in both arming states, not merely "does not
    # raise": a gate that survives by allowing everything would pass the weaker check.
    settings = write_settings(tmp_path)
    doomed = tmp_path / "about-to-vanish"
    doomed.mkdir()
    monkeypatch.chdir(doomed)
    doomed.rmdir()
    with pytest.raises(OSError):
        os.getcwd()  # the ambient precondition, asserted rather than assumed
    call = payload("Edit", add_entry_edit(settings, COVERING), transcript, tmp_path)
    assert (hook.decide(call) is not None) is denies


def test_no_resolvable_directory_at_all_makes_a_relative_target_an_unknown(tmp_path, monkeypatch):
    # The other half of the same regression, and the one the fix's own prose claims: when
    # NEITHER the payload nor the process supplies a directory, a relative target cannot be
    # resolved, and an unresolvable target is an UNKNOWN (a) -- which routes to (b) instead
    # of crashing. Pinning it matters because the `except OSError` that makes this reachable
    # survived every other row in this file: an unpinned rescue clause is one refactor from
    # being deleted as dead code.
    settings = write_settings(tmp_path)
    doomed = tmp_path / "about-to-vanish"
    doomed.mkdir()
    monkeypatch.chdir(doomed)
    doomed.rmdir()
    absolute = add_entry_edit(settings, COVERING)
    relative = add_entry_edit_relative("settings.json", COVERING)

    def decide_without_cwd(tool_input, transcript):
        call = payload("Edit", tool_input, transcript, tmp_path)
        return hook.decide({k: v for k, v in call.items() if k != "cwd"})

    # An absolute target needs no directory, so the verdict is the ordinary one.
    assert decide_without_cwd(absolute, NO_DENIAL) is None
    assert decide_without_cwd(absolute, ARMED_READ) is not None
    # A relative one is unresolvable, so (a) is UNKNOWN and (b) decides -- and the deny it
    # produces is the `_ON_ERROR` message, which names no surface, NOT the widening message.
    assert decide_without_cwd(relative, NO_DENIAL) is None
    unknown_deny = decide_without_cwd(relative, ARMED_READ)
    assert unknown_deny is not None
    assert COVERING not in unknown_deny


@pytest.mark.parametrize("transcript,denies", [(NO_DENIAL, False), (ARMED_READ, True)],
                         ids=["not-armed", "armed"])
def test_an_unresolvable_bash_write_target_is_an_unknown_too(tmp_path, monkeypatch,
                                                             transcript, denies):
    # The Bash path has the same hole and needed the same guard, so it needs its own row:
    # the file-tool row above passes with the Bash guard removed. `command_write_targets`
    # hands back a relative target unchanged when there is no directory to join it to, and a
    # target the gate cannot locate must not read as "writes no permission surface".
    write_settings(tmp_path)
    doomed = tmp_path / "about-to-vanish"
    doomed.mkdir()
    monkeypatch.chdir(doomed)
    doomed.rmdir()
    call = payload("Bash", {"command": "cp /tmp/evil.json settings.json"}, transcript, tmp_path)
    assert (hook.decide({k: v for k, v in call.items() if k != "cwd"}) is not None) is denies


def test_a_definite_surface_among_bash_targets_outranks_an_unreadable_one(tmp_path):
    # A deny must not be downgraded to an `_ON_ERROR` by an unrelated unreadable target:
    # the deny message names the surface, the `_ON_ERROR` message does not.
    #
    # The unreadable target is a dangling symlink for the reason `unknown_a_call`'s
    # docstring gives: this row used to stage a DIRECTORY, which the gate now answers with
    # a definite no. Observed: with that staging it stayed GREEN under the round-6 gate
    # while no longer testing what it names — the ordering pinned would have been between a
    # definite surface and a definite NON-surface, which is not the ordering at issue. A row
    # that survives the change to the very behaviour it exercises is the inert-row defect.
    blocked = tmp_path / "settings.json"
    blocked.symlink_to(tmp_path / "nowhere.json")
    real = write_settings(tmp_path, name="real-settings.json")
    reason = hook.decide(payload(
        "Bash", {"command": f"cp a {blocked} && cp evil.json {real}"}, ARMED_BASH, tmp_path))
    assert reason is not None
    assert str(real) in reason


# --- no baseline on disk: UNKNOWN is not coerced to "no widening" ------------------

@pytest.mark.parametrize(
    "entry,denied", [(COVERING, True), (OTHER_PREFIX, False)]
)
def test_a_surface_written_over_unparseable_content_counts_every_entry_as_granted(
    tmp_path, entry, denied
):
    # `widens()` answers UNKNOWN when the old document is not a JSON object: no
    # baseline exists. Nothing then shows any entry to be pre-existing, so all of them
    # count as granted -- and relevance still narrows the result, which is what keeps
    # this from being a blanket deny.
    corrupt = tmp_path / "settings.json"
    corrupt.write_text("this file is not JSON at all\n")
    tool_input = {"file_path": str(corrupt), "content": settings_text([entry])}
    reason = hook.decide(payload("Write", tool_input, ARMED_READ, tmp_path))
    assert (reason is not None) is denied


# --- fail toward COVERING when the denied call cannot be resolved ------------------

def test_an_unresolvable_denied_call_is_treated_as_covered(tmp_path):
    # The entry added here covers nothing -- it would be ALLOWED against a resolvable
    # denial (see the not-relevant cases above). It is denied only because the denial's
    # call is unknown, and a widening cannot be shown NOT to answer an unknown call.
    s = write_settings(tmp_path)
    reason = hook.decide(payload("Edit", add_entry_edit(s, OTHER_PREFIX), UNRESOLVED, tmp_path))
    assert reason is not None
    assert "unresolved call counts as covered" in reason


# --- everything internal routes through the ONE constant --------------------------

@pytest.mark.parametrize("on_error,denies", [("deny", True), ("allow", False)])
def test_an_unreadable_transcript_resolves_through_on_error(tmp_path, monkeypatch, on_error, denies):
    monkeypatch.setattr(hook, "_ON_ERROR", on_error)
    s = write_settings(tmp_path)
    reason = hook.decide(payload(
        "Edit", add_entry_edit(s, COVERING), ARMING_FIXTURES / "empty.jsonl", tmp_path))
    assert (reason is not None) is denies


@pytest.mark.parametrize("on_error,denies", [("deny", True), ("allow", False)])
def test_a_missing_transcript_resolves_through_on_error(tmp_path, monkeypatch, on_error, denies):
    monkeypatch.setattr(hook, "_ON_ERROR", on_error)
    s = write_settings(tmp_path)
    reason = hook.decide(payload(
        "Edit", add_entry_edit(s, COVERING), tmp_path / "gone.jsonl", tmp_path))
    assert (reason is not None) is denies


# --- the four version-STABLE Bash rows (armed throughout) -------------------------

@pytest.mark.parametrize("template,denies", [
    ("sed -i s/a/b/ {s}", True),            # in-place edit of the surface
    ("cp evil.json {s}", True),             # overwrite the surface
    ("jq . {s}", False),                    # READ-ONLY: no write target, so allowed
    ("echo hi && cp evil.json {s}", True),  # separator handled, second segment writes
])
def test_bash_write_target_rows(tmp_path, template, denies):
    # The `jq` row is the load-bearing one: it is the only place a Bash call reaches
    # ALLOW past the arming check, so it proves the write-target model does the work
    # rather than the path denying every Bash call in an armed session.
    s = write_settings(tmp_path)
    reason = hook.decide(payload(
        "Bash", {"command": template.format(s=s)}, ARMED_BASH, tmp_path))
    assert (reason is not None) is denies


@pytest.mark.parametrize("on_error,denies", [("deny", True), ("allow", False)])
def test_an_untokenizable_bash_command_resolves_through_on_error(
    tmp_path, monkeypatch, on_error, denies
):
    # R7 axis 2, pinned. `command_write_targets` reports an unbalanced quote as an EMPTY
    # target list -- byte-identical to a clean parse that found no write. An
    # implementation that reads that `[]` as "no write target" allows this command under
    # BOTH constant values and so passes neither row: one apostrophe would defeat the
    # whole limb.
    monkeypatch.setattr(hook, "_ON_ERROR", on_error)
    s = write_settings(tmp_path)
    reason = hook.decide(payload(
        "Bash", {"command": f"echo don't ; cp evil.json {s}"}, ARMED_BASH, tmp_path))
    assert (reason is not None) is denies


# --- mutation controls: one per conjunct that could silently vanish ----------------

def test_mutation_forcing_armed_reddens_an_allow_case(tmp_path, monkeypatch):
    # Conjunct (b). Without it this gate is a file guard on settings.json, refusing
    # ordinary maintenance. Forcing the arming verdict flips a widening that is allowed
    # ONLY because the session carries no permission denial.
    s = write_settings(tmp_path)
    call = payload("Edit", add_entry_edit(s, COVERING), NO_DENIAL, tmp_path)
    assert hook.decide(call) is None

    forced = Arming(Verdict.ARMED, (DeniedCall("permission-rule", "Read",
                                               {"file_path": "/srv/secrets/notes.md"}),))
    monkeypatch.setattr(hook.denial_arming, "armed", lambda _path: forced)
    assert hook.decide(call) is not None


def test_mutation_forcing_covers_true_reddens_an_allow_case(tmp_path, monkeypatch):
    # Conjunct (c). Without it this is the two-condition gate the reviewer rejected:
    # any widening in an armed session denies, however unrelated to the denial.
    s = write_settings(tmp_path)
    call = payload("Edit", add_entry_edit(s, OTHER_PREFIX), ARMED_READ, tmp_path)
    assert hook.decide(call) is None

    monkeypatch.setattr(hook.permission_entry_match, "covers", lambda *a, **k: True)
    assert hook.decide(call) is not None


# --- a target the gate IDENTIFIED and can say a definite no about -------------------

def _kind_target(tmp_path: Path, kind: str) -> str:
    """A write target of one non-document kind, as an absolute path.

    `/dev/zero` is the one target not under `tmp_path`, and it is deliberate: a character
    device that yields bytes without end is the shape that OOM-killed the hook process, and
    no `tmp_path` file reproduces it. Nothing here opens it -- the whole point of the rows
    below is that `stat` answers before any open -- and it is read-only to this suite.
    """
    if kind == "directory":
        target = tmp_path / "settings.json"
        target.mkdir()
        return str(target)
    if kind == "fifo":
        target = tmp_path / "fifo-settings.json"
        os.mkfifo(target)
        return str(target)
    if kind == "char-device":
        return "/dev/zero"
    raise AssertionError(f"unknown kind {kind}")


def _padded_surface(tmp_path: Path, name="huge-settings.json") -> Path:
    """A REAL permission document padded one byte past the gate's cap.

    The padding is TRAILING WHITESPACE, so the whole file still parses as the very JSON the
    gate is looking for and `st_size` is the only thing that can change the verdict. NUL
    bytes were the first padding tried and made an inert row: a document with NULs stapled
    to it no longer parses, so the gate reached its answer by a route that had nothing to do
    with the size. Whitespace is the one padding JSON tolerates.
    """
    body = settings_text([SEED])
    target = tmp_path / name
    target.write_text(
        body + " " * (hook._MAX_SURFACE_BYTES + 1 - len(body.encode("utf-8"))),
        encoding="utf-8",
    )
    return target


# `oversize` was the fourth member of this tuple until round 7, and taking it out is the
# fix rather than a tidy-up: a file too large to read is not a file whose KIND rules out
# being a permission document, so answering "definitely not a widening" about it published
# a verdict over content nobody looked at -- and made padding a real permissions document
# past the cap a way to launder a widening. It has its own row below, asserting the
# OPPOSITE direction (UNKNOWN, hence deny while armed).
KINDS_THAT_ARE_NOT_DOCUMENTS = ("directory", "fifo", "char-device")

# The same three for the transcript route. The transcript cap is 512 MiB, no test writes
# that, and staging a 1 MiB file instead measured as an inert row -- the padded permission
# document is pretty-printed over several lines, so no single line both opens and closes a
# JSON object, and `_looks_like_a_row` answered UNREADABLE for a reason that had nothing to
# do with the cap. Under the unbounded read the row still passed. That cap gets its own row
# below, over a transcript that really would arm.
KINDS_THAT_ARE_NOT_TRANSCRIPTS = ("directory", "fifo", "char-device")

# 5 s is the PreToolUse budget the whole gate is sized against, so a verdict slower than
# this has already failed in production whatever it returns. A ceiling, not an expectation:
# the measured runs land in single-digit milliseconds.
PROMPT_SECONDS = 5.0


@pytest.mark.parametrize("kind", KINDS_THAT_ARE_NOT_DOCUMENTS)
@pytest.mark.parametrize("tool_name", ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"))
def test_a_target_that_cannot_be_a_json_document_is_a_definite_no_not_an_unknown(
    tmp_path, kind, tool_name
):
    # ARMED, and with the shipped fail-closed `_ON_ERROR`, so an UNKNOWN here would DENY:
    # the assertion is that the gate ANSWERS instead. A directory, a FIFO and a device are
    # all targets the gate identified, and no file of those KINDS can be a
    # `permissions.allow` JSON document, so the honest verdict is "this call does not widen a
    # permission surface" -- not a shrug resolved by policy. Kind is the whole basis, which
    # is why a merely over-large file is not in this tuple and denies instead.
    #
    # Both halves are asserted and both matter. The VERDICT, because "did not raise" is
    # satisfied by the catch-all's unconditional deny, i.e. by the bug. And PROMPTNESS,
    # because the two shapes that motivated this row do not fail by returning a wrong
    # answer: `/dev/zero` read whole is a `MemoryError` under a memory limit and an OOM kill
    # without one, and a FIFO blocks in `open()` until someone writes to it.
    target = _kind_target(tmp_path, kind)
    if tool_name == "Bash":
        tool_input = {"command": f"cp evil.json {target}"}
    elif tool_name == "Write":
        tool_input = {"file_path": target, "content": settings_text([SEED, COVERING])}
    elif tool_name == "MultiEdit":
        tool_input = {"file_path": target,
                      "edits": [{"old_string": f'"{SEED}"', "new_string": f'"{COVERING}"'}]}
    elif tool_name == "NotebookEdit":
        tool_input = {"notebook_path": target, "new_source": settings_text([COVERING])}
    else:
        tool_input = add_entry_edit(Path(target), COVERING)

    started = time.monotonic()
    reason = hook.decide(payload(tool_name, tool_input, ARMED_READ, tmp_path))
    elapsed = time.monotonic() - started

    assert reason is None
    assert elapsed < PROMPT_SECONDS


@pytest.mark.parametrize("kind", KINDS_THAT_ARE_NOT_TRANSCRIPTS)
def test_a_transcript_of_a_kind_that_cannot_be_a_transcript_is_unreadable_not_a_hang(
    tmp_path, kind, monkeypatch
):
    # THE SAME CLASS ONE ROUTE OVER, which is why this is a row and not a footnote: the gate
    # bounded the read of the file it writes and left `transcript_path` -- a field out of the
    # same untrusted payload -- being read whole by `denial_arming`. `/dev/zero` there is the
    # identical OOM and a FIFO there is the identical block.
    #
    # The DIRECTION differs from the row above, and correctly so: a path that is not a
    # session transcript does not answer "was this session armed", so it is UNREADABLE, which
    # is fail-CLOSED. A self-grant carrying such a path must still DENY.
    # AND NOTHING IS EVER OPENED, asserted directly rather than inferred from the verdict.
    # That is the half the `stat` call actually buys, and for `/dev/zero` it is the ONLY half:
    # measured, a device path answers UNREADABLE by several independent routes (the truncation
    # check, then the row-shape check), so no mutation to the shipped code changes this row's
    # verdict, and a verdict-only row would be inert for that kind. The bound the guard really
    # establishes is that no unbounded read and no blocking `open()` happens at all -- so the
    # spy records the path and refuses, which both fails the row and keeps the mutation that
    # removes the guard from hanging this suite on a FIFO or eating memory on the device.
    opened: list[str] = []

    def refuse_to_open(path, *args, **kwargs):
        opened.append(str(path))
        raise AssertionError(f"the arming reader opened {path} instead of stat-ing it")

    monkeypatch.setattr(hook.denial_arming, "open", refuse_to_open, raising=False)

    settings = write_settings(tmp_path, name="real-settings.json")
    transcript = _kind_target(tmp_path, kind)

    started = time.monotonic()
    verdict = hook.denial_arming.armed(transcript).verdict
    reason = hook.decide(payload("Edit", add_entry_edit(settings, COVERING),
                                 Path(transcript), tmp_path))
    elapsed = time.monotonic() - started

    assert opened == []
    assert verdict is Verdict.UNREADABLE
    assert reason is not None
    assert elapsed < PROMPT_SECONDS


def test_a_transcript_past_its_size_cap_is_unreadable_even_though_it_would_have_armed(
    tmp_path, monkeypatch
):
    # THE CAP TESTED AS A CAP. The staged file is `ARMED_READ` itself -- byte for byte a
    # transcript that arms, over a denial the entry below covers -- so the only thing that can
    # make the answer UNREADABLE is its size. The cap is lowered instead of the file being
    # grown because the shipped cap is 512 MiB: a real one would have to be written to disk on
    # every run of this suite, and a 1 MiB stand-in was measured to pass for the wrong reason.
    #
    # The direction is the one that matters. A transcript too big to read must NOT come back
    # NOT_ARMED -- that is a silent ALLOW of the self-grant -- and it must not come back
    # truncated either, because a truncated read drops the file's NEWEST rows, which is
    # precisely where the arming denial is.
    # The copy is `ARMED_READ` plus ONE trailing newline -- content-identical once blank lines
    # are stripped, one byte over the cap. The cap is the original's exact size, so the
    # reference assertion below is a real control rather than a copy of the row: same content,
    # same code, one byte apart, opposite verdicts.
    settings = write_settings(tmp_path)
    transcript = tmp_path / "huge-transcript.jsonl"
    transcript.write_bytes(ARMED_READ.read_bytes() + b"\n")
    monkeypatch.setattr(hook.denial_arming, "_MAX_TRANSCRIPT_BYTES", ARMED_READ.stat().st_size)

    assert hook.denial_arming.armed(ARMED_READ).verdict is Verdict.ARMED  # the same bytes arm
    assert hook.denial_arming.armed(transcript).verdict is Verdict.UNREADABLE
    assert hook.decide(payload("Edit", add_entry_edit(settings, COVERING),
                               transcript, tmp_path)) is not None


# --- a target the gate DECLINED TO READ is not a target it answered about ------------

@pytest.mark.parametrize("tool_name", ("Edit", "Write", "Bash"))
def test_a_permission_document_padded_past_the_cap_is_unknown_not_a_definite_no(
    tmp_path, tool_name
):
    # GROW, THEN GRANT -- measured, on all three tool paths: an 83 B permissions document
    # denied this widening while the same document padded to 1 048 577 B with legitimate
    # entries allowed it, because over the cap `_read_text` answered NOT_A_SURFACE, a
    # definite negative, about a file it had just declined to open. One `cp` of padding
    # bought a permanent self-grant, and nothing in the deny path was even reached.
    #
    # The cap itself is not the fix and cannot be: any cap has an over-cap side. What is
    # fixed is the DIRECTION -- over-cap is UNREADABLE, i.e. UNKNOWN, which an armed session
    # pays `_ON_ERROR` for. So this row asserts the deny, and the two controls around it are
    # what make the deny mean something: the 83 B twin denies too (so the row is not passing
    # because the cap made everything deny), and the same padded call in a session carrying
    # no denial allows (so the deny came from the UNKNOWN routing through (b), not from a
    # size guard on settings files).
    padded = _padded_surface(tmp_path)
    small = write_settings(tmp_path, name="small-settings.json")

    def call_for(target: Path) -> dict:
        if tool_name == "Bash":
            return {"command": f"cp evil.json {target}"}
        if tool_name == "Write":
            return {"file_path": str(target), "content": settings_text([SEED, COVERING])}
        return add_entry_edit(target, COVERING)

    assert hook.denial_arming.armed(ARMED_READ).verdict is Verdict.ARMED
    reason = hook.decide(payload(tool_name, call_for(padded), ARMED_READ, tmp_path))
    assert reason is not None

    # AND THE REFUSAL NAMES THE CAP. This deny is the cost the cap's own comment promises a
    # user will "sanction in a sentence" -- and measured before this round, the whole account
    # it gave of itself was "could not be read", with nothing anywhere in it pointing at a
    # size limit, so that sentence was unwritable. Pinned on the three figures a reader needs
    # to write it: the file's real size, the cap in bytes, and the constant's name to grep.
    assert str(padded.stat().st_size) in reason
    assert str(hook._MAX_SURFACE_BYTES) in reason
    assert "_MAX_SURFACE_BYTES" in reason

    assert hook.decide(payload(tool_name, call_for(small), ARMED_READ, tmp_path)) is not None
    assert hook.decide(payload(tool_name, call_for(padded), NO_DENIAL, tmp_path)) is None


def _understate_size(monkeypatch, module, target: Path, size: int) -> None:
    """Make `os.stat` report `size` for `target` alone, and the truth for everything else."""
    real_stat = os.stat

    def lying_stat(path, *args, **kwargs):
        st = real_stat(path, *args, **kwargs)
        if isinstance(path, (str, os.PathLike)) and str(path) == str(target):
            fields = list(st)
            fields[6] = size
            return os.stat_result(tuple(fields))
        return st

    monkeypatch.setattr(module.os, "stat", lying_stat)


def test_a_target_that_yields_more_than_stat_promised_is_unreadable_not_diffed(
    tmp_path, monkeypatch
):
    # THE SECOND BOUND, PINNED. `_read_text` gates on `st_size` and then reads one character
    # past the cap and checks the length, and until this row the second half was unpinned:
    # measured, deleting it left the entire suite green, so the check was documented as
    # load-bearing and mechanically was not.
    #
    # `st_size` is what one earlier syscall reported, not a promise about a later `read` --
    # an ordinary file grows in the window between the two, and a kernel-backed file need not
    # account its content there at all. That is what the lying `stat` reproduces, and it is
    # the only way to reproduce it: for a real static file a UTF-8 decode can only yield
    # FEWER characters than bytes, so no fixture makes a read overrun its own `st_size`.
    #
    # The DIRECTION is why it matters, and it is the allow direction. Truncated to the cap,
    # this document loses the text `add_entry_edit` replaces, so the edit applies to nothing,
    # the after-text equals the before-text, neither parses, and the gate concludes "no
    # widening" -- a silent ALLOW of a real self-grant, off a partial read. The control below
    # is the same call in an unarmed session, so the deny here is the UNKNOWN routing rather
    # than the lowered cap refusing everything.
    s = write_settings(tmp_path)
    monkeypatch.setattr(hook, "_MAX_SURFACE_BYTES", 20)
    _understate_size(monkeypatch, hook, s, 0)

    assert hook.denial_arming.armed(ARMED_READ).verdict is Verdict.ARMED
    reason = hook.decide(payload("Edit", add_entry_edit(s, COVERING), ARMED_READ, tmp_path))
    assert reason is not None
    assert str(s) in reason
    assert hook.decide(payload("Edit", add_entry_edit(s, COVERING), NO_DENIAL, tmp_path)) is None


def test_a_write_over_an_unparseable_document_says_there_is_no_baseline(tmp_path):
    # WHAT THE DENY MESSAGE MAY CLAIM IS BOUNDED BY WHAT WAS COMPUTED. With the file on disk
    # not a JSON object there is no before-document to subtract, so `_widening_between` lists
    # every entry the NEW document grants -- including ones the file already had in whatever
    # shape it is in. Calling that "adding X, Y" states a diff that was never taken, and the
    # user reads it as "this call introduces both". The message has to say which of the two
    # it is looking at.
    target = tmp_path / "settings.json"
    target.write_text('{"permissions": {"allow": ["' + SEED + '"\n', encoding="utf-8")

    reason = hook.decide(payload(
        "Write", {"file_path": str(target), "content": settings_text([SEED, COVERING])},
        ARMED_READ, tmp_path))

    assert reason is not None
    assert "there is no baseline" in reason
    assert COVERING in reason and SEED in reason   # every entry listed, not only the new one


# --- applying a patch is not writing a settings file --------------------------------

@pytest.mark.parametrize("command", [
    "git apply fix.patch",
    "patch -p1 < fix.patch",
    "git apply --check fix.patch",
])
def test_applying_a_patch_while_armed_is_allowed(tmp_path, command):
    # THE COLLATERAL THIS GATE INFLICTED FOR FIVE REVIEW ROUNDS, pinned so it cannot come
    # back. `command_write_targets` cannot know which files a patch body touches, so for
    # `patch` and `git apply` it reports the WORKING DIRECTORY as the write target. A
    # directory read as "a settings file I could not read" made every one of these commands
    # UNKNOWN, hence -- while armed -- denied: measured, all three of these, including
    # `--check`, which writes nothing at all. Applying a patch is most of what a developer
    # does inside a session that has hit one permission denial.
    #
    # The control below is what keeps this row from being satisfied by a gate that has simply
    # stopped judging Bash: the same session, the same transcript, a real self-grant, denied.
    s = write_settings(tmp_path)
    assert hook.decide(payload("Bash", {"command": command}, ARMED_BASH, tmp_path)) is None
    assert hook.decide(payload(
        "Bash", {"command": f"cp evil.json {s}"}, ARMED_BASH, tmp_path)) is not None


@pytest.mark.parametrize("command", ["echo hi > note.txt", "sed -i s/a/b/ note.txt"])
def test_an_ordinary_write_to_an_ordinary_file_while_armed_is_allowed(tmp_path, command):
    # The other half of the same measurement, and the reason the fix above is not "stop
    # judging Bash": these two name a REAL write target that is a real file, and they were
    # allowed before the change and are allowed after it. If a future fix to the patch
    # residual starts denying by command verb, these rows are what fails.
    (tmp_path / "note.txt").write_text("a\n", encoding="utf-8")
    assert hook.decide(payload("Bash", {"command": command}, ARMED_BASH, tmp_path)) is None


# --- a destination DIRECTORY names the files written inside it -----------------------

def _copy_into_a_directory(tmp_path):
    """A live permission document in a directory, plus a widened copy staged beside it.

    The shape is the manoeuvre itself: write the document you want where you may write, then
    copy it ONTO the live one by naming the destination DIRECTORY rather than the file. Same
    basename, so `DEST/basename(SRC)` is the surface -- which is exactly what `cp` means and
    what the lexer used not to say.
    """
    config = tmp_path / "config"
    config.mkdir()
    surface = write_settings(config)
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "settings.json").write_text(settings_text([SEED, COVERING]), encoding="utf-8")
    (staged / "other.json").write_text("{}\n", encoding="utf-8")
    return config, surface, staged


@pytest.mark.parametrize("template", [
    "cp {src} {dir}/",                 # the spelling that SAYS directory
    "cp {src} {dir}",                  # the ambiguous bare token
    "mv {src} {dir}/",                 # the same verb family, moving rather than copying
    # WHAT THIS ROW ACTUALLY PINS, measured rather than reasoned: that `install` is in
    # `_COPY_VERBS` at all. It does NOT pin `-m` being a value-taking option, and it does NOT
    # pin the directory resolution -- both of those were mutated in place and all 146 rows here
    # still passed. An earlier draft of this comment claimed either mutation alone flips the row
    # to allow; that was false, and the reason is worth more than the claim was. Both mutations
    # OVER-emit (a spurious `d/600`, a spurious `d/`) while still emitting the real
    # `d/settings.json`, and `_bash_widening` stops at the first candidate that is a definite
    # surface -- so a deny survives every over-emission and reports the same file. That is the
    # verdict-vs-candidates gap this suite cannot see and `test_bash_write_targets.py` exists
    # to close: those two mutations die there, in exact candidate lists, and only there.
    "install -m 600 {src} {dir}",
    "cp {other} {src} {dir}/",         # multi-source: one target per source
    "cp -t {dir} {src}",               # the destination as an option value
])
def test_a_copy_into_a_directory_is_judged_on_the_files_it_writes(tmp_path, template):
    # MEASURED, ALL SIX ALLOWED: the lexer answered a directory destination with the
    # DIRECTORY, the gate asked "is that a JSON document", got a correct no, and allowed the
    # write -- so the whole gate was one trailing slash away from being bypassed. The verb
    # names the files on its command line; answering with their parent was a real defect, not
    # a bound like the patch verbs' (whose file names live inside the patch body).
    #
    # `install` is the second half of the same finding: it is an ordinary write verb that was
    # simply absent from the table, so it reported NO target and allowed even when the
    # destination was named in full. Its `-m 600` is here deliberately -- an option that
    # consumes the next token, which without modelling reads `600` as a source.
    #
    # Each row asserts the transcript really arms BEFORE concluding anything from a deny: a
    # hand-built or mis-referenced transcript is silently NOT_ARMED, which makes every row in
    # this file pass for the wrong reason, and that has happened three times in this task.
    config, surface, staged = _copy_into_a_directory(tmp_path)
    command = template.format(
        src=staged / "settings.json", dir=config, other=staged / "other.json")

    assert hook.denial_arming.armed(ARMED_BASH).verdict is Verdict.ARMED
    reason = hook.decide(payload("Bash", {"command": command}, ARMED_BASH, tmp_path))
    assert reason is not None
    assert str(surface) in reason          # the FILE it would write, not the directory
    # The conjunct control: the identical command in a session carrying no denial allows, so
    # what these rows pin is the gate, not a new refusal of `cp` on sight.
    assert hook.decide(payload("Bash", {"command": command}, NO_DENIAL, tmp_path)) is None


def test_the_controls_the_directory_resolution_must_not_break(tmp_path):
    # The other side of the same change, in one row per thing that must NOT move. Resolving a
    # destination directory means the lexer now emits paths it did not emit before, and an
    # over-emitting lexer is only safe while a candidate that is not a permission document
    # still allows -- so the last two are as load-bearing as the first.
    config, surface, staged = _copy_into_a_directory(tmp_path)
    src = staged / "settings.json"
    (staged / "p.patch").write_text("--- a\n+++ b\n", encoding="utf-8")

    # naming the destination FILE always denied, and still does
    assert hook.decide(payload(
        "Bash", {"command": f"cp {src} {surface}"}, ARMED_BASH, tmp_path)) is not None
    # the patch verbs still answer with the working directory, which is still not a document
    for command in (f"git apply {staged}/p.patch", f"patch -p1 -i {staged}/p.patch"):
        assert hook.decide(payload("Bash", {"command": command}, ARMED_BASH, tmp_path)) is None
    # an ordinary redirect to an ordinary file
    assert hook.decide(payload(
        "Bash", {"command": "echo hi > note.txt"}, ARMED_BASH, tmp_path)) is None
    # a copy into a directory that holds no document of that name: the joined candidate is
    # ABSENT, which answers the question with a no rather than shrugging
    assert hook.decide(payload(
        "Bash", {"command": f"cp {staged}/other.json {config}/"}, ARMED_BASH, tmp_path)) is None


@pytest.mark.parametrize("dest_template, denies", [
    ("~/.claude/settings.json", True),    # the spelling every runbook and every human uses
    ("~/.claude/", True),                 # and it composes with the directory resolution
    ("$HOME/.claude/settings.json", False),   # the named residual, asserted as a residual
    ("${HOME}/.claude/settings.json", False),
])
def test_a_home_relative_destination_is_resolved_and_a_variable_one_is_not(
    tmp_path, monkeypatch, dest_template, denies
):
    # MEASURED AT 067ea09: `cp <widened> ~/.claude/settings.json`, armed, with the real
    # `~/.claude/settings.json` a live permission surface, was ALLOWED. The lexer joined the
    # literal `~` under the cwd, `<cwd>/~/.claude/settings.json` is a path nothing is on, so
    # the gate read ABSENT -- "a creation, nothing to widen" -- about the single most likely
    # spelling of the manoeuvre this whole hook exists to stop. `_abs` now expands it.
    #
    # THE TWO HALVES ARE ONE ROW ON PURPOSE. `$HOME` is NOT expanded and these rows pin that
    # it stays allowed, because the asymmetry is a decision and an unpinned decision drifts:
    # the hook's environment is not the command's (`HOME=/x cp ...`), so expanding `$VAR`
    # would answer confidently about a path the command may never write. `~` carries no such
    # ambiguity -- it means the invoking user's home and this process IS that user. Flipping
    # the false rows to deny is not a regression to fix here; it needs an expansion model.
    home = tmp_path / "fakehome"
    (home / ".claude").mkdir(parents=True)
    surface = write_settings(home / ".claude")
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "settings.json").write_text(settings_text([SEED, COVERING]), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    command = f"cp {staged / 'settings.json'} {dest_template}"

    assert hook.denial_arming.armed(ARMED_BASH).verdict is Verdict.ARMED
    reason = hook.decide(payload("Bash", {"command": command}, ARMED_BASH, tmp_path))
    if not denies:
        assert reason is None
        return
    assert reason is not None
    assert str(surface) in reason
    # The conjunct control: the same command with no denial in the session allows, so what
    # this pins is the gate rather than a blanket refusal of writes under `~`.
    assert hook.decide(payload("Bash", {"command": command}, NO_DENIAL, tmp_path)) is None


def test_the_controls_the_home_expansion_must_not_break(tmp_path, monkeypatch):
    # Expanding `~` makes the lexer emit paths under a real home that it used to bury under
    # the cwd, so every one of these was reachable-but-benign before and must stay allowed.
    # An over-emitting lexer is only safe while a candidate that is not a permission document
    # still allows, and `~` is where a session does most of its ordinary writing.
    home = tmp_path / "fakehome"
    (home / ".claude").mkdir(parents=True)
    write_settings(home / ".claude")
    (home / "notes").mkdir()
    (home / "notes" / "a.txt").write_text("hello\n", encoding="utf-8")
    (home / "notes" / "plain.json").write_text('{"colour": "blue"}\n', encoding="utf-8")
    (home / "notes" / "p.patch").write_text("--- a\n+++ b\n", encoding="utf-8")
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "settings.json").write_text(settings_text([SEED, COVERING]), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    for command in (
        f"cp {home}/notes/a.txt ~/notes/b.txt",        # an ordinary file, an ordinary copy
        f"mv {home}/notes/a.txt ~/notes/c.txt",        # the same, moving
        f"cp {home}/notes/plain.json ~/notes/",        # a JSON that is not a permission one
        f"cp {home}/notes/plain.json ~/notes",         # and its ambiguous bare spelling
        "echo hi > ~/notes/note.txt",                  # a redirect, the other `_abs` caller
        "git apply ~/notes/p.patch",                   # the patch verbs, under a real home
        # a REAL widened permission document, copied into a directory holding no document of
        # that name: the joined candidate is ABSENT, which answers with a no rather than a
        # shrug -- the row that would fail if expansion had been paired with a laxer arm
        f"cp {staged}/settings.json ~/notes/",
    ):
        assert hook.decide(
            payload("Bash", {"command": command}, ARMED_BASH, tmp_path)) is None, command


# --- the transcript path is a payload field, and gets a payload field's discipline ---

def test_a_relative_transcript_path_does_not_resolve_against_the_process_directory(
    tmp_path, monkeypatch
):
    # THE DECOY FLIPS THE VERDICT, which is why this is not a tidiness row. `transcript_path`
    # was read from disk without ever being located, so a RELATIVE one resolved against
    # whatever directory the hook process happened to be in. Stage a not-armed transcript
    # there under the same relative name and the gate concludes "this session had no denial"
    # -- and a real self-grant, in a really armed session, is ALLOWED. A silent hole, in the
    # direction this gate exists to prevent.
    #
    # Both files are real transcripts of OPPOSITE verdict, asserted as controls, so the row
    # cannot pass by neither file being readable.
    settings = write_settings(tmp_path)
    (tmp_path / "session.jsonl").write_bytes(ARMED_READ.read_bytes())

    decoy_dir = tmp_path / "elsewhere"
    decoy_dir.mkdir()
    (decoy_dir / "session.jsonl").write_bytes(NO_DENIAL.read_bytes())
    monkeypatch.chdir(decoy_dir)

    assert hook.denial_arming.armed(tmp_path / "session.jsonl").verdict is Verdict.ARMED
    assert hook.denial_arming.armed(decoy_dir / "session.jsonl").verdict is Verdict.NOT_ARMED

    call = payload("Edit", add_entry_edit(settings, COVERING), ARMED_READ, tmp_path)
    reason = hook.decide({**call, "transcript_path": "session.jsonl"})
    assert reason is not None
    assert COVERING in reason          # denied on the widening, not on a shrug


# --- every file tool the client actually has, not the two the gate started with -----

def test_the_other_file_tools_deny_the_very_payload_edit_denies(tmp_path):
    # MEASURED, on one identical self-grant: `Edit` denied while `MultiEdit` and
    # `NotebookEdit` -- both real tools in this client, both able to write this file --
    # allowed. A gate whose coverage depends on which of two equivalent tools the agent
    # reaches for is not a gate; the modelled set has to be the set that exists.
    s = write_settings(tmp_path)
    multi = {"file_path": str(s),
             "edits": [{"old_string": f'"{SEED}"', "new_string": f'"{SEED}", "{COVERING}"'}]}
    notebook = {"notebook_path": str(s), "new_source": settings_text([SEED, COVERING])}

    edit_reason = hook.decide(payload("Edit", add_entry_edit(s, COVERING), ARMED_READ, tmp_path))
    multi_reason = hook.decide(payload("MultiEdit", multi, ARMED_READ, tmp_path))
    notebook_reason = hook.decide(payload("NotebookEdit", notebook, ARMED_READ, tmp_path))

    assert edit_reason is not None
    assert multi_reason is not None
    assert notebook_reason is not None
    # `MultiEdit` applies its edits and diffs the result, so it names the entry it caught, as
    # `Edit` does. `NotebookEdit` is judged coarsely -- the target already IS a permission
    # surface and the call rewrites it -- so it is not required to name one.
    assert COVERING in multi_reason


def test_a_multi_edit_that_grants_something_unrelated_is_still_allowed(tmp_path):
    # The control that keeps the row above off "MultiEdit denies everything while armed": the
    # same tool, a real widening, an entry that does not cover the denied call -- allowed.
    #
    # What it rules out is the COARSE verdict -- judging on "the target is a permission
    # surface today", as the NotebookEdit path does, would deny here. It does NOT by itself
    # show the edits are applied and diffed, because an ALLOW is equally what an UNMODELLED
    # tool produces; the row above, which denies, is what establishes that MultiEdit is
    # judged at all. The pair is the claim, neither row alone.
    s = write_settings(tmp_path)
    multi = {"file_path": str(s),
             "edits": [{"old_string": f'"{SEED}"', "new_string": f'"{SEED}", "{OTHER_PREFIX}"'}]}
    assert hook.decide(payload("MultiEdit", multi, ARMED_READ, tmp_path)) is None
