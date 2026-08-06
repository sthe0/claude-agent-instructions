"""Tests for scripts/lib/hook_wiring.py — the general hook-wiring probe.

Two halves. The first pins the probe's answers, and above all pins the two ways
it must NOT answer: a false ABSENT (which would let a gate emit a confident
causal claim from evidence that does not support it) and a wired-looking answer
built from settings it does not actually understand.

The second half checks the gate-bearing registry in three directions, all
mechanical rather than hand-listed: every entry is a real file, every hook in
the tree that can block or that writes a gate-required artifact is either
registered or explicitly exempted, and every entry is something the installer
actually intends to install (an entry absent from install-reminder-hooks.sh's
DESIRED block would make the SessionStart check warn, on every machine forever,
about a hook the system never meant to wire).
"""
from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from lib import hook_wiring  # noqa: E402

HOOK = "hook-plan-delivery-gate.py"


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A config root whose whole chain lives under tmp — including the managed
    policy member, so a real one on the host machine cannot colour a result."""
    r = tmp_path / "root"
    r.mkdir()
    monkeypatch.setattr(
        hook_wiring, "managed_settings_path", lambda: tmp_path / "managed-settings.json")
    return r


def _write(path: Path, hooks: dict):
    path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")


def _group(command: str, matcher: str | None = None) -> dict:
    grp: dict = {"hooks": [{"type": "command", "command": command}]}
    if matcher is not None:
        grp["matcher"] = matcher
    return grp


def test_wired_and_present(root):
    _write(root / "settings.json",
           {"PreToolUse": [_group(str(SCRIPTS / HOOK), "AskUserQuestion")]})
    w = hook_wiring.probe(HOOK, root)
    assert w.status == hook_wiring.WIRED and w.wired
    assert list(w.events) == ["PreToolUse"]
    assert w.missing_script_paths == []


def test_absent_from_every_member(root):
    _write(root / "settings.json", {"PreToolUse": [_group("python3 /x/other-hook.py")]})
    w = hook_wiring.probe(HOOK, root)
    assert w.status == hook_wiring.ABSENT
    # The claim is always scoped — a bare "not registered" would overstate it.
    assert "any user-level settings member" in w.describe()


def test_wired_in_local_member_only(root):
    """The false-ABSENT case: nothing in settings.json, wired in
    settings.local.json. A single-file read would report a confident lie."""
    _write(root / "settings.json", {"PreToolUse": [_group("python3 /x/other-hook.py")]})
    _write(root / "settings.local.json", {"Stop": [_group(f"python3 {SCRIPTS / HOOK}")]})
    w = hook_wiring.probe(HOOK, root)
    assert w.status == hook_wiring.WIRED
    assert list(w.events) == ["Stop"]


def test_unreadable_member_degrades_to_unknown(root):
    """One member is corrupt and the others simply lack the hook: the honest
    answer is UNKNOWN, never ABSENT."""
    _write(root / "settings.json", {"PreToolUse": [_group("python3 /x/other-hook.py")]})
    (root / "settings.local.json").write_text("{not json", encoding="utf-8")
    w = hook_wiring.probe(HOOK, root)
    assert w.status == hook_wiring.UNKNOWN
    assert root / "settings.local.json" in w.members_unreadable
    assert "cannot be determined" in w.describe()


def test_wired_but_script_missing(root):
    """A stale worktree path: registered, but the file it names is gone."""
    _write(root / "settings.json",
           {"PreToolUse": [_group(f"/gone/worktree/scripts/{HOOK}")]})
    w = hook_wiring.probe(HOOK, root)
    assert w.status == hook_wiring.WIRED
    assert w.missing_script_paths == [f"/gone/worktree/scripts/{HOOK}"]
    assert "does not exist" in w.describe()


def test_interpreter_prefix_is_not_the_script_path(root):
    """A hand-wired `python3 <script>` entry must not be reported as pointing at
    a missing file called python3."""
    _write(root / "settings.json",
           {"PreToolUse": [_group(f"python3 -u {SCRIPTS / HOOK}")]})
    w = hook_wiring.probe(HOOK, root)
    assert w.status == hook_wiring.WIRED and w.missing_script_paths == []


def test_wired_under_non_pretooluse_event(root):
    """Every event section is scanned — a Stop-only hook is not invisible."""
    _write(root / "settings.json", {"Stop": [_group(f"python3 {SCRIPTS / HOOK}")]})
    assert hook_wiring.probe(HOOK, root).status == hook_wiring.WIRED


def test_malformed_settings_never_reports_wired(root):
    (root / "settings.json").write_text("[]", encoding="utf-8")
    assert hook_wiring.probe(HOOK, root).status == hook_wiring.UNKNOWN


def test_unmodelled_shape_is_unknown_not_absent(root):
    """A settings shape the scanner does not understand (an event whose value
    is a dict, not a list of groups) must not be read as 'no hook here'."""
    (root / "settings.json").write_text(
        json.dumps({"hooks": {"PreToolUse": {"matcher": "Bash"}}}), encoding="utf-8")
    assert hook_wiring.probe(HOOK, root).status == hook_wiring.UNKNOWN


def test_missing_chain_members_are_not_evidence(root):
    """A member that does not exist is silence, not corruption: with the only
    present member modelled and hook-free, ABSENT is the right answer."""
    _write(root / "settings.json", {"PreToolUse": [_group("python3 /x/other.py")]})
    w = hook_wiring.probe(HOOK, root)
    assert w.status == hook_wiring.ABSENT and w.members_unreadable == []


def test_probe_is_read_only(root):
    """No member is created, and an existing one is not rewritten."""
    _write(root / "settings.json", {"PreToolUse": [_group("python3 /x/other.py")]})
    before = (root / "settings.json").read_bytes()
    hook_wiring.probe(HOOK, root)
    assert (root / "settings.json").read_bytes() == before
    assert not (root / "settings.local.json").exists()


def test_settings_chain_excludes_project_member(root):
    """The project member is excluded by decision (no caller can locate it
    honestly) — pinned so re-adding it is a deliberate act with a test to fix."""
    chain = hook_wiring.settings_chain(root)
    assert [p.name for p in chain[:2]] == ["settings.json", "settings.local.json"]
    assert all(p.parent == root for p in chain[:2])
    assert not any(".claude" in p.parts[:-1] for p in chain[:2])


# ── The gate-bearing registry, checked in three directions ───────────────────

# High-recall discovery of hooks that can BLOCK: the PreToolUse deny decision,
# the exit(2) form, and the Stop-event block decision. Matching a mention in a
# docstring too is deliberate — this is a candidate net, narrowed by the
# exemption list below, so over-matching costs a review and under-matching
# costs a silently unregistered gate.
_BLOCK_MECHANISM_RE = re.compile(
    r'"permissionDecision"\s*:\s*"deny"'
    r'|permissionDecision.{0,4}:\s*.{0,2}deny'
    r'|sys\.exit\(2\)'
    r'|"decision"\s*:\s*"block"'
)
# The other half of the predicate: a hook that writes an artifact a gate
# requires. Today that is the delivery stamp.
_GATE_ARTIFACT_RE = re.compile(r"write_stamp\s*\(")

# Hooks that match the net but are NOT gate-bearing, each with its reason.
_NOT_GATE_BEARING: "dict[str, str]" = {}

_REGISTERED = {name for name, _ in hook_wiring.GATE_BEARING_HOOKS}


def _hook_scripts() -> "list[Path]":
    return sorted(SCRIPTS.glob("hook-*.py"))


def test_registry_entries_exist_and_are_annotated():
    for name, note in hook_wiring.GATE_BEARING_HOOKS:
        assert (SCRIPTS / name).is_file(), f"registry names a missing script: {name}"
        assert note.strip(), f"registry entry {name} has no note"
    assert len(_REGISTERED) == len(hook_wiring.GATE_BEARING_HOOKS), "duplicate entry"


def test_every_gate_bearing_hook_is_registered():
    """Reverse direction, mechanical: discover blockers and stamp-writers by
    scanning the tree, not from a hand list, so a new gate-bearing hook cannot
    be added without either registering or explicitly exempting it."""
    found = []
    for p in _hook_scripts():
        text = p.read_text(encoding="utf-8", errors="replace")
        if _BLOCK_MECHANISM_RE.search(text) or _GATE_ARTIFACT_RE.search(text):
            found.append(p.name)
    unregistered = [n for n in found if n not in _REGISTERED and n not in _NOT_GATE_BEARING]
    assert not unregistered, (
        "hooks that can block or write a gate-required artifact but are absent "
        f"from GATE_BEARING_HOOKS (register them, or exempt with a reason): {unregistered}"
    )
    stale = sorted(set(_NOT_GATE_BEARING) - set(found))
    assert not stale, f"exemptions for hooks that no longer match the net: {stale}"


def test_registry_is_a_subset_of_installer_intent():
    """Third direction: the registry is a second hand-maintained list over the
    installer's domain. An entry the installer never intends to wire would make
    the SessionStart check warn about it on every machine, forever."""
    desired = (SCRIPTS / "install-reminder-hooks.sh").read_text(encoding="utf-8")
    block = desired.split("DESIRED = [", 1)[1].split("\n]", 1)[0]
    missing = sorted(n for n in _REGISTERED if f'"{n}"' not in block)
    assert not missing, (
        "registry entries absent from install-reminder-hooks.sh's DESIRED block: "
        f"{missing}"
    )


# --- the timeout axis: a registration is not only present, it has a limit -----
#
# A hook can be perfectly WIRED and still never finish: the harness kills it at
# its registered `timeout`. Probing presence alone reported those hooks as
# healthy while the harness was killing them mid-judge on every call.

def _timed_group(command: str, timeout, matcher: str | None = None) -> dict:
    hook: dict = {"type": "command", "command": command}
    if timeout is not None:
        hook["timeout"] = timeout
    grp: dict = {"hooks": [hook]}
    if matcher is not None:
        grp["matcher"] = matcher
    return grp


def test_registration_carries_its_timeout(root):
    _write(root / "settings.json", {
        "PreToolUse": [_timed_group(str(SCRIPTS / HOOK), 25, "AskUserQuestion")],
    })
    w = hook_wiring.probe(HOOK, root)
    assert [(r.event, r.timeout) for r in w.registrations] == [("PreToolUse", 25)]


def test_a_registration_without_a_timeout_key_reads_as_unknown_not_zero(root):
    """No key means "the harness default", which this module cannot see — so the
    honest value is None. Reading it as 0 would manufacture a shortfall; reading
    it as generous would hide one."""
    _write(root / "settings.json", {"Stop": [_group(str(SCRIPTS / HOOK))]})
    w = hook_wiring.probe(HOOK, root)

    assert [r.timeout for r in w.registrations] == [None]
    assert hook_wiring.timeout_shortfalls(w, 30) == []
    assert len(hook_wiring.timeout_unknowns(w)) == 1


def test_two_registrations_of_one_hook_with_different_timeouts(root):
    """The case a per-hook verdict would swallow: one entry allows the budget and
    a second, in another member, is pinned at 5s. Reporting per REGISTRATION
    points fail-closed — the slow one is named instead of hidden behind its
    correct sibling."""
    _write(root / "settings.json", {
        "Stop": [_timed_group(str(SCRIPTS / HOOK), 35)],
    })
    _write(root / "settings.local.json", {
        "Stop": [_timed_group(f"python3 {SCRIPTS / HOOK}", 5)],
    })
    w = hook_wiring.probe(HOOK, root)

    assert sorted(r.timeout for r in w.registrations) == [5, 35]
    shortfalls = hook_wiring.timeout_shortfalls(w, 30)
    assert len(shortfalls) == 1 and "5s" in shortfalls[0]
    assert "settings.local.json" in shortfalls[0]


def test_duplicate_note_distinguishes_deduplicated_from_double_running(root):
    """Two registrations are always worth a look, but only DISTINCT command
    strings actually run twice: the harness deduplicates on the command, with
    matcher and timeout outside that key. Claiming "runs twice" of an identical
    pair would be a fabricated finding."""
    identical = str(SCRIPTS / HOOK)
    _write(root / "settings.json", {"Stop": [_timed_group(identical, 35)]})
    _write(root / "settings.local.json", {"Stop": [_timed_group(identical, 5)]})
    note = hook_wiring.duplicate_registration_note(hook_wiring.probe(HOOK, root))
    assert note is not None and "deduplicates" in note

    _write(root / "settings.local.json", {
        "Stop": [_timed_group(f"python3 {SCRIPTS / HOOK}", 5)],
    })
    note = hook_wiring.duplicate_registration_note(hook_wiring.probe(HOOK, root))
    assert note is not None and "more than once per event" in note


def test_a_single_registration_draws_no_duplicate_note(root):
    _write(root / "settings.json", {"Stop": [_timed_group(str(SCRIPTS / HOOK), 35)]})
    assert hook_wiring.duplicate_registration_note(
        hook_wiring.probe(HOOK, root)) is None


def test_timeout_requirements_are_not_derived_from_the_registry():
    """The checked set is broader than GATE_BEARING_HOOKS on purpose: a hook can
    need a long timeout without bearing a gate. Deriving one list from the other
    would silently drop such a hook from the check the moment it is added."""
    required = {name for name, _minimum, _why in hook_wiring.TIMEOUT_REQUIREMENTS}
    assert required, "the timeout requirements table must not be empty"
    for name, minimum, why in hook_wiring.TIMEOUT_REQUIREMENTS:
        assert (SCRIPTS / name).exists(), f"requirement names a missing hook: {name}"
        assert isinstance(minimum, int) and minimum > 0
        assert why.strip(), f"{name}'s requirement carries no rationale"


# --- the timeout NUMBER is not one fact, it is three copies of one fact -------
#
# `hook-turn-end-gate.py` (and its two siblings) carry their whole-invocation
# judge budget in THREE untied places: the hook module's own constant, this
# table's `minimum`, and install-reminder-hooks.sh's DESIRED row `timeout`. Each
# test below closes one direction between two of the three; together they are
# the only thing standing between "the number is 30 everywhere" and a silent
# regression in any single copy.

def _desired_rows() -> "list[tuple]":
    """Every 4-tuple in install-reminder-hooks.sh's DESIRED block, parsed as
    data (`ast.literal_eval`) rather than grepped as text — so a row's timeout
    is read as the number it actually is, not inferred from a substring match."""
    text = (SCRIPTS / "install-reminder-hooks.sh").read_text(encoding="utf-8")
    block = text.split("DESIRED = [", 1)[1].split("\n]", 1)[0]
    return ast.literal_eval("[" + block + "\n]")


def _load_hook_module(name: str):
    """Load a hook-*.py script as a module by its own file, the established
    idiom for these hyphenated filenames (mirrors test_hook_turn_end_gate.py's
    `_load_module`). A name distinct from any other test file's registration
    avoids reusing a sibling test's possibly-monkeypatched module object."""
    module_name = "wiring_check_" + name[:-3].replace("-", "_")
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / name)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_timeout_requirement_own_constant_covers_every_requirement():
    """Bidirectional coverage: every TIMEOUT_REQUIREMENTS row has a matching
    entry in TIMEOUT_REQUIREMENT_OWN_CONSTANT (so the machine-link test below
    actually runs for it), and no entry there names a hook the requirements
    table no longer lists."""
    required = {name for name, _minimum, _why in hook_wiring.TIMEOUT_REQUIREMENTS}
    linked = set(hook_wiring.TIMEOUT_REQUIREMENT_OWN_CONSTANT)
    assert required == linked


def test_timeout_requirement_minimum_matches_the_hooks_own_budget_constant():
    """should-fix #1b: TIMEOUT_REQUIREMENTS' minimum for each hook must equal
    that hook's own whole-invocation judge-budget constant — read by IMPORTING
    the hook module and reading the constant, a genuine machine link rather than
    two literals that happen to agree today. Catches the requirements table
    itself drifting out of step with the hook it describes (reviewer's
    mutation M8: minimum 30->5 for hook-turn-end-gate.py with the hook's own
    _TURN_JUDGE_BUDGET_S left at 30)."""
    for name, minimum, _why in hook_wiring.TIMEOUT_REQUIREMENTS:
        const_name = hook_wiring.TIMEOUT_REQUIREMENT_OWN_CONSTANT[name]
        module = _load_hook_module(name)
        actual = getattr(module, const_name)
        assert actual == minimum, (
            f"{name}.{const_name} is {actual}, but TIMEOUT_REQUIREMENTS says "
            f"{minimum} — the two must be the same number"
        )


def test_desired_registrations_meet_their_own_hooks_timeout_requirement():
    """should-fix #1a: every DESIRED row in install-reminder-hooks.sh for a hook
    listed in TIMEOUT_REQUIREMENTS must register it at or above that hook's
    minimum — read from the SAME rows the installer will actually write, not a
    second hand-copied literal. Covers every row of TIMEOUT_REQUIREMENTS, not
    just one (reviewer's mutation M6: DESIRED timeout 35->5 for
    hook-turn-end-gate.py, whose requirement minimum is 30)."""
    rows = _desired_rows()
    for name, minimum, _why in hook_wiring.TIMEOUT_REQUIREMENTS:
        matches = [row for row in rows if row[2].split()[0] == name]
        assert matches, f"{name} has a timeout requirement but no DESIRED row"
        for event, matcher, script, timeout in matches:
            assert timeout >= minimum, (
                f"{name} is desired at {timeout}s under ({event}, {matcher}) — "
                f"below its own {minimum}s requirement"
            )
