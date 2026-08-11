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
  * the four version-STABLE Bash rows. The two-line phantom-target row is deliberately
    absent: its verdict depends on the lexer revision, so pinning it would plant a test
    that goes red on another stage's landing with nobody owning the fix.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
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
    "dangling-symlink-target",
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


def unknown_a_call(tmp_path: Path, label: str):
    """`(tool_name, tool_input)` for one UNKNOWN-(a) shape.

    The unreadable target is a DIRECTORY rather than a mode-000 file: EISDIR stages the
    case for any uid, where root would read straight through a chmod. The EACCES form
    has its own row.
    """
    blocked = tmp_path / "settings.json"
    if not blocked.exists():
        blocked.mkdir()
    dangling = tmp_path / "linked-settings.json"
    if not dangling.is_symlink():
        dangling.symlink_to(tmp_path / "nowhere.json")
    real = write_settings(tmp_path, name="real-settings.json")
    return {
        "edit-target-unreadable": (
            "Edit", {"file_path": str(blocked), "old_string": "a", "new_string": "b"}),
        "write-target-unreadable": (
            "Write", {"file_path": str(blocked), "content": settings_text([COVERING])}),
        "bash-target-unreadable": ("Bash", {"command": f"cp evil.json {blocked}"}),
        "dangling-symlink-target": (
            "Write", {"file_path": str(dangling), "content": settings_text([COVERING])}),
        "edit-without-file-path": ("Edit", {"old_string": "a", "new_string": "b"}),
        "write-without-content-string": ("Write", {"file_path": str(real), "content": None}),
        "edit-without-old-and-new-strings": ("Edit", {"file_path": str(real)}),
        "bash-without-command-string": ("Bash", {}),
        "tool-input-is-not-an-object": ("Edit", "not an object"),
        # A NUL in the path raises ValueError BEFORE the syscall, so it is not an OSError
        # and only reaches _Read.UNREADABLE because _read_text catches it explicitly.
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
    # EACCES specifically, beside the EISDIR form above: the branch is "the read failed
    # while something IS on the path", not any one errno.
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
@pytest.mark.parametrize("transcript", [NO_DENIAL, ARMED_READ], ids=["not-armed", "armed"])
def test_a_wrong_typed_cwd_is_absorbed_rather_than_turned_into_a_deny(
    tmp_path, hermetic_process_cwd, value, transcript
):
    # A malformed payload field must not manufacture a deny. It is worth being exact about
    # WHY this allows, because a review found the obvious reading wrong: (b) is not what
    # keeps it out. Suppressing `cwd` sends the relative target to the process cwd, where no
    # `settings.json` exists, so (a) resolves to "does not widen" and short-circuits before
    # the transcript is ever opened -- which is why both arming states are asserted together
    # rather than only the not-armed one. A wrong-typed field makes the gate MORE permissive
    # here, never less, and that direction is the safe one for a fail-closed guard.
    write_settings(tmp_path)
    call = payload(
        "Edit", add_entry_edit_relative("settings.json", COVERING), transcript, tmp_path)
    assert hook.decide({**call, "cwd": value}) is None


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
    blocked = tmp_path / "settings.json"
    blocked.mkdir()
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
