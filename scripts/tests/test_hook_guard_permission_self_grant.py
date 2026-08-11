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


def test_writing_a_settings_file_that_does_not_exist_yet_is_a_creation_not_a_widening(tmp_path):
    # The recorded behaviour: there is no prior surface to widen, and a brand-new
    # settings file is not how a denial gets cleared.
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
