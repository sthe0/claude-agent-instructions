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
from lib import dispatch_witness_snapshot  # noqa: E402
from lib import hook_wiring  # noqa: E402

HOOK = "hook-plan-delivery-gate.py"


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A config root whose whole chain lives under tmp — including the managed
    policy member and the project members, so neither a real managed policy nor
    the project the test runner happens to sit in can colour a result. The env
    var is deleted rather than pointed at tmp: these tests are about the
    user-level chain, and a project root left set would silently qualify (or
    un-qualify) every answer below."""
    r = tmp_path / "root"
    r.mkdir()
    monkeypatch.setattr(
        hook_wiring, "managed_settings_path", lambda: tmp_path / "managed-settings.json")
    monkeypatch.delenv(hook_wiring.PROJECT_DIR_ENV, raising=False)
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


def test_the_settings_chain_carries_the_project_member_only_when_it_is_locatable(
    root, monkeypatch, tmp_path
):
    """The chain was three user-level members and nothing else, and an ABSENT
    read off it was spent as "not registered" — a claim the harness can refute
    from a project settings file the probe never opened. The project members are
    in the chain now, but only when the harness has said WHERE the project is:
    guessing from the cwd would make the answer depend on which directory a
    verification command happened to run in."""
    unset = hook_wiring.settings_chain(root)
    assert [p.name for p in unset] == [
        "settings.json", "settings.local.json", "managed-settings.json"
    ]

    project = tmp_path / "proj"
    monkeypatch.setenv(hook_wiring.PROJECT_DIR_ENV, str(project))
    named = hook_wiring.settings_chain(root)
    assert named[3:] == [
        project / ".claude" / "settings.json",
        project / ".claude" / "settings.local.json",
    ]
    # The user-level head is unchanged: the project members are appended, so a
    # registration found in them cannot displace one found above.
    assert named[:3] == unset


def test_a_project_level_registration_is_no_longer_a_false_absent(root, monkeypatch, tmp_path):
    """The defect the widened chain removes, driven end to end: the hook is
    wired in the project's own settings and nowhere else. The old chain read
    three user-level members, found nothing, and answered ABSENT."""
    _write(root / "settings.json", {"PreToolUse": [_group("python3 /x/other.py")]})
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    _write(project / ".claude" / "settings.json",
           {"Stop": [_group(f"python3 {SCRIPTS / HOOK}")]})
    monkeypatch.setenv(hook_wiring.PROJECT_DIR_ENV, str(project))

    w = hook_wiring.probe(HOOK, root)
    assert w.status == hook_wiring.WIRED
    assert list(w.events) == ["Stop"]


def test_a_project_level_timeout_is_no_longer_invisible(root, monkeypatch, tmp_path):
    """The WIRED half of the same hole. old_timeout() takes the max over the
    registrations the probe found, so a project-level entry the probe never read
    made the recorded limit a lower bound presented as the limit — and a witness
    beating a lower bound proves nothing."""
    _write(root / "settings.json", {"Stop": [_timed_group(str(SCRIPTS / HOOK), 5)]})
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    _write(project / ".claude" / "settings.local.json",
           {"Stop": [_timed_group(f"python3 {SCRIPTS / HOOK}", 45)]})
    monkeypatch.setenv(hook_wiring.PROJECT_DIR_ENV, str(project))

    w = hook_wiring.probe(HOOK, root)
    assert sorted(r.timeout for r in w.registrations) == [5, 45]
    # And through the one function that spends the number: the recorded limit a
    # witness must outlive is the project entry's 45s, not the 5s the old
    # user-level-only chain would have handed it.
    assert dispatch_witness_snapshot.old_timeout(w) == 45


def test_scope_coverage_is_read_off_what_the_probe_reached(root, monkeypatch, tmp_path):
    """`project_scope_covered` is evidence, not an assertion: it says the probe
    accounted for the project members, and a member that is simply not on disk
    IS accounted for — there is nothing there to register anything. There are
    THREE ways to miss them, not two: an unnamed project root, a project file
    that would not parse, and a project file that parses and then carries a
    shape `_scan_settings` does not model — whose entries are skipped unread
    even though the file opened fine. All three must leave the answer
    qualified."""
    _write(root / "settings.json", {"PreToolUse": [_group("python3 /x/other.py")]})

    # No project root named: the probe cannot know whether one exists.
    assert not hook_wiring.probe(HOOK, root).project_scope_covered

    # Named, and the members are absent from disk — accounted for.
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setenv(hook_wiring.PROJECT_DIR_ENV, str(project))
    w = hook_wiring.probe(HOOK, root)
    assert w.project_scope_covered
    assert w.status == hook_wiring.ABSENT
    assert "project-level included" in w.describe()

    # Named, present, and unparseable — NOT accounted for, and the status
    # degrades to UNKNOWN for the same reason any unreadable member does.
    (project / ".claude").mkdir()
    (project / ".claude" / "settings.json").write_text("{not json", encoding="utf-8")
    w = hook_wiring.probe(HOOK, root)
    assert not w.project_scope_covered
    assert w.status == hook_wiring.UNKNOWN

    # Named, present, PARSES — and then carries a shape the scanner does not
    # model (an event whose value is a dict where a list of groups belongs), so
    # its entries were skipped unread. The quiet one: the file opened, so a
    # predicate asking only "did every project member parse" answers yes and
    # certifies a scope the probe never reached.
    (project / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"Stop": {"0": _group(f"python3 {SCRIPTS / HOOK}")}}}),
        encoding="utf-8")
    w = hook_wiring.probe(HOOK, root)
    assert project / ".claude" / "settings.json" in w.members_read
    assert project / ".claude" / "settings.json" in w.members_unmodelled
    assert not w.project_scope_covered
    assert w.status == hook_wiring.UNKNOWN
    assert "settings shape not modelled" in w.describe()


def test_an_unmodelled_project_member_does_not_pass_for_covered_under_wired(
    root, monkeypatch, tmp_path
):
    """The same hole on the branch that actually reaches a caller, and the reason
    the case above is blocking rather than cosmetic.

    A user-level registration makes the status WIRED, and `probe` records that
    outcome from `result.events` without ever consulting the `modelled` flag — so
    the UNKNOWN degradation the case above relies on is not available here.
    Nothing but the coverage predicate stands between an unread project member
    and a snapshot entry claiming full scope: `scope_qualified: false` makes
    check-dispatch-witness.py skip `qualified_scope_verdict()` and certify a
    recorded call against the 5s it could see, when the entry it skipped says
    60s and a 6s call proves nothing at all."""
    _write(root / "settings.json", {"Stop": [_timed_group(str(SCRIPTS / HOOK), 5)]})
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    # Valid JSON, and "Stop" is a dict where a list of groups belongs.
    (project / ".claude" / "settings.json").write_text(
        json.dumps({
            "hooks": {"Stop": {"0": _timed_group(f"python3 {SCRIPTS / HOOK}", 60)}}
        }),
        encoding="utf-8")
    monkeypatch.setenv(hook_wiring.PROJECT_DIR_ENV, str(project))

    w = hook_wiring.probe(HOOK, root)

    assert w.status == hook_wiring.WIRED
    assert project / ".claude" / "settings.json" in w.members_unmodelled
    assert not w.project_scope_covered
    entry = dispatch_witness_snapshot.entry_for(w)
    assert entry["scope_qualified"]
    # The 60s registration was skipped, so the recorded limit is the 5s one —
    # which is exactly why the entry must not be spent unqualified.
    assert entry["timeout"] == 5


def test_the_chain_does_not_list_one_member_twice(tmp_path, monkeypatch):
    """A session whose project root is the config root's parent — the ordinary
    shape of a `~/.claude` machine whose $CLAUDE_PROJECT_DIR is $HOME — names the
    same two files at user level and at project level. Listed twice they are read
    twice, every registration is extended twice, and `duplicate_registration_note`
    reports a hook registered exactly once as wired more than once: a fabricated
    finding, on the SessionStart banner."""
    monkeypatch.setattr(
        hook_wiring, "managed_settings_path", lambda: tmp_path / "managed-settings.json")
    home = tmp_path / "home"
    config = home / ".claude"
    config.mkdir(parents=True)
    monkeypatch.setenv(hook_wiring.PROJECT_DIR_ENV, str(home))

    # The fabricated finding first, because it is the harm; the chain shape
    # below is only the mechanism, and asserting it first would hide whether
    # the harm is actually reached.
    _write(config / "settings.json", {"Stop": [_timed_group(str(SCRIPTS / HOOK), 35)]})
    w = hook_wiring.probe(HOOK, config)
    assert len(w.registrations) == 1
    assert hook_wiring.duplicate_registration_note(w) is None
    assert not hook_wiring.runs_more_than_once(w)

    chain = hook_wiring.settings_chain(config)
    assert len(chain) == len(set(chain)) == 3
    # Deduplication removes the second SPELLING, not the project scope: both
    # project members are still accounted for by the read.
    assert w.project_scope_covered


def test_an_unmodelled_user_level_member_qualifies_the_entry_too(
    root, monkeypatch, tmp_path
):
    """The same hole one scope over, and the reason the snapshot reads
    `scope_fully_covered` rather than `project_scope_covered`.

    The project members here are provably absent from disk, so the chain reached
    as wide as it can and `project_scope_covered` is rightly True. But a
    USER-LEVEL member parsed and then carried a shape the scanner does not
    model, hiding a 60s registration. A predicate about the project scope calls
    that a fact; check-dispatch-witness.py then skips `qualified_scope_verdict()`
    and certifies a 6s call against the 5s it could see."""
    _write(root / "settings.json", {"Stop": [_timed_group(str(SCRIPTS / HOOK), 5)]})
    # Valid JSON, and "Stop" is a dict where a list of groups belongs.
    (root / "settings.local.json").write_text(
        json.dumps({
            "hooks": {"Stop": {"0": _timed_group(f"python3 {SCRIPTS / HOOK}", 60)}}
        }),
        encoding="utf-8")
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setenv(hook_wiring.PROJECT_DIR_ENV, str(project))

    w = hook_wiring.probe(HOOK, root)

    assert w.status == hook_wiring.WIRED
    assert root / "settings.local.json" in w.members_unmodelled
    # The two fields disagree here, which is the whole point of having both.
    assert w.project_scope_covered
    assert not w.scope_fully_covered

    entry = dispatch_witness_snapshot.entry_for(w)
    assert entry["scope_qualified"]
    # The 60s registration was skipped, so the recorded limit is the 5s one.
    assert entry["timeout"] == 5


def test_an_unreadable_user_level_member_qualifies_the_entry_too(
    root, monkeypatch, tmp_path
):
    """The other clause of the same predicate, on the same scope. Dropping
    `members_unreadable` from `scope_fully_covered` survives every other test
    here, because everywhere else an unreadable member is a PROJECT one and
    `project_scope_covered` is already False — the clause only earns its place on
    a user-level file."""
    (root / "settings.json").write_text("{not json", encoding="utf-8")
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setenv(hook_wiring.PROJECT_DIR_ENV, str(project))

    w = hook_wiring.probe(HOOK, root)

    assert w.members_unreadable == [root / "settings.json"]
    assert w.project_scope_covered
    assert not w.scope_fully_covered
    assert dispatch_witness_snapshot.entry_for(w)["scope_qualified"]


def test_an_unsearchable_chain_member_is_unaccounted_for_not_absent(
    root, monkeypatch, tmp_path
):
    """`Path.is_file()` swallows only ENOENT/ENOTDIR/EBADF/ELOOP; EACCES
    propagates (measured, python 3.12.3). So a member under a directory this
    process cannot search used to raise PermissionError straight out of
    `probe()`, into every caller's catch-all, and the enforcement-is-OFF banner
    went silent with no trace. The shape is ordinary, not exotic: a root-owned
    mode-700 /etc/claude-code is in every chain on the machine.

    The member is the MANAGED one and the project root is set and empty, so
    `project_scope_covered` stays True and the False on `scope_fully_covered`
    can only come from the unreadable clause. An existence the filesystem will
    not report is not a proven absence — it is unaccounted for, which is
    UNKNOWN, not ABSENT."""
    locked = tmp_path / "policy"
    locked.mkdir()
    managed = locked / "managed-settings.json"
    managed.write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    monkeypatch.setattr(hook_wiring, "managed_settings_path", lambda: managed)
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setenv(hook_wiring.PROJECT_DIR_ENV, str(project))
    _write(root / "settings.json", {"PreToolUse": [_group("python3 /x/other.py")]})

    locked.chmod(0o000)
    try:
        w = hook_wiring.probe(HOOK, root)
    finally:
        # Before any assertion: a leaked mode-000 directory survives the failure
        # and breaks tmp_path teardown for every later test in the session.
        locked.chmod(0o755)

    assert w.members_unreadable == [managed]
    assert w.status == hook_wiring.UNKNOWN
    assert w.project_scope_covered
    assert not w.scope_fully_covered
    assert dispatch_witness_snapshot.entry_for(w)["scope_qualified"]


def test_a_looping_symlink_does_not_take_the_whole_chain_down(root, monkeypatch, tmp_path):
    """`$CLAUDE_PROJECT_DIR` is externally supplied, and `Path.resolve()` on a
    symlink loop raises RuntimeError rather than OSError. Every caller of this
    module sits under a catch-all that goes quiet, so an escaping RuntimeError
    would suppress the enforcement-is-OFF banner outright — one looping symlink,
    every session, no trace."""
    loop = tmp_path / "loop"
    loop.mkdir()
    (loop / "a").symlink_to(loop / "b")
    (loop / "b").symlink_to(loop / "a")
    monkeypatch.setenv(hook_wiring.PROJECT_DIR_ENV, str(loop / "a"))
    _write(root / "settings.json", {"PreToolUse": [_group("python3 /x/other.py")]})

    chain = hook_wiring.settings_chain(root)
    assert loop / "a" / ".claude" / "settings.json" in chain
    # And through the probe, which is what the callers actually run: an
    # unresolvable member is not on disk, so it is silence rather than evidence.
    w = hook_wiring.probe(HOOK, root)
    assert w.status == hook_wiring.ABSENT
    # Full coverage for a project root that could not be resolved AT ALL is
    # correct, not fail-open: the harness resolves the same $CLAUDE_PROJECT_DIR
    # and hits the same loop, so nothing can be registered under it and there
    # is no hidden registration for the claim to miss. `is_file()` answers
    # False for a loop rather than raising (measured, python 3.12.3), so the
    # EACCES guard in probe() deliberately does not divert this case into
    # members_unreadable. Anyone changing the miss-list logic re-decides this.
    assert w.scope_fully_covered


def test_a_symlinked_project_root_keeps_the_config_root_spelling(tmp_path, monkeypatch):
    """Dedup is observable only when one file is reached under two spellings,
    and then WHICH survivor is kept decides which path every report names —
    `members_read`, `Registration.member`, `describe()`'s UNKNOWN reasons. The
    caller asked about the config root; a report naming a root it never
    mentioned sends the reader looking in the wrong tree.

    Third assertion pins the coverage predicate's resolved comparison, which
    only becomes load-bearing once the survivor is the config-root spelling: the
    shared unreadable file is then recorded under one spelling while
    `project_settings_chain()` holds the other, so a raw membership test would
    read the project scope as covered."""
    monkeypatch.setattr(
        hook_wiring, "managed_settings_path", lambda: tmp_path / "managed-settings.json")
    home = tmp_path / "home"
    config = home / ".claude"
    config.mkdir(parents=True)
    link = tmp_path / "proj-link"
    link.symlink_to(home)
    monkeypatch.setenv(hook_wiring.PROJECT_DIR_ENV, str(link))

    chain = hook_wiring.settings_chain(config)
    assert len(chain) == 3
    assert chain[:2] == [config / "settings.json", config / "settings.local.json"]

    (config / "settings.json").write_text("{not json", encoding="utf-8")
    w = hook_wiring.probe(HOOK, config)
    assert w.members_unreadable == [config / "settings.json"]
    assert not w.project_scope_covered
    assert w.status == hook_wiring.UNKNOWN


def test_the_absence_sentence_names_the_scope_it_actually_reached(root, monkeypatch, tmp_path):
    """A gate quotes this sentence at the user. "Not registered in any settings
    member" would be a claim the probe cannot make from a user-level-only read,
    so the wording tracks the coverage rather than being fixed prose."""
    _write(root / "settings.json", {"PreToolUse": [_group("python3 /x/other.py")]})
    assert "any user-level settings member" in hook_wiring.probe(HOOK, root).describe()

    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setenv(hook_wiring.PROJECT_DIR_ENV, str(project))
    described = hook_wiring.probe(HOOK, root).describe()
    assert "project-level included" in described
    assert "user-level" not in described
    # And it names BOTH roots: the project member is not under `root`, so a
    # sentence attributing it there sends a reader looking in a directory that
    # does not contain the file the claim rests on.
    assert str(root) in described and str(project) in described


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


def test_timeout_shortfall_message_names_the_matcher(root):
    """should-fix #1: the check-timeouts remediation text tells a reader to
    diff a listed registration's matcher against install-reminder-hooks.sh's
    DESIRED table and remove by hand anything wired under a different one --
    but the message itself used to drop `reg.matcher` on the floor, so two
    registrations under two different matchers printed identically and the
    reader had no way to follow that advice."""
    _write(root / "settings.json", {
        "Stop": [_timed_group(str(SCRIPTS / HOOK), 5, "Edit|Write")],
    })
    shortfalls = hook_wiring.timeout_shortfalls(hook_wiring.probe(HOOK, root), 30)
    assert len(shortfalls) == 1
    assert "Edit|Write" in shortfalls[0]


def test_timeout_shortfall_message_uses_star_when_matcher_is_absent(root):
    """A registration with no `matcher` key (matches every tool) must print as
    `*`, not the Python-internal `matcher None`."""
    _write(root / "settings.json", {"Stop": [_timed_group(str(SCRIPTS / HOOK), 5)]})
    shortfalls = hook_wiring.timeout_shortfalls(hook_wiring.probe(HOOK, root), 30)
    assert len(shortfalls) == 1
    assert "matcher *" in shortfalls[0]
    assert "None" not in shortfalls[0]


def test_timeout_unknown_message_names_the_matcher(root):
    _write(root / "settings.json", {"Stop": [_group(str(SCRIPTS / HOOK), "Bash")]})
    unknowns = hook_wiring.timeout_unknowns(hook_wiring.probe(HOOK, root))
    assert len(unknowns) == 1
    assert "Bash" in unknowns[0]


def test_two_shortfalls_under_different_matchers_are_distinguishable(root):
    """The reviewer's exact reproduction: two live registrations of the same
    hook, wired under two different matchers, both below the minimum -- the two
    reported lines must not read identically, or a reader following the
    installer-vs-hand-removal advice cannot tell which one the installer would
    actually reconcile.

    Both groups live in the SAME member on purpose. Split across two members,
    the two lines differed on `reg.member` even before the matcher was printed,
    so the inequality below would have passed against the very defect it is
    meant to catch. One member is the only configuration where the matcher is
    the sole distinguishing field."""
    _write(root / "settings.json", {
        "Stop": [
            _timed_group(str(SCRIPTS / HOOK), 5, "Edit|Write"),
            _timed_group(str(SCRIPTS / HOOK), 5),
        ],
    })
    shortfalls = hook_wiring.timeout_shortfalls(hook_wiring.probe(HOOK, root), 30)
    assert len(shortfalls) == 2
    assert shortfalls[0] != shortfalls[1]
    assert any("Edit|Write" in s for s in shortfalls)
    assert any("matcher *" in s for s in shortfalls)


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


def _hooks_that_call_a_judge() -> "set[str]":
    """Every `scripts/hook-*.py` that CALLS A JUDGE — the scope
    TIMEOUT_REQUIREMENTS claims to cover, discovered mechanically rather than
    assumed to already equal the table it is checked against.

    The predicate is the conjunction "imports `agentctl.advisor` AND calls an
    identifier named `judge_*`", read from the module's AST. It replaces a
    `"JudgeBudget(" in text` substring match, which keyed the scope on the
    REMEDY rather than the hazard: a hook that calls a judge and forgets the
    budget entirely — precisely the regression this table exists to catch — was
    invisible to it, while a hook that opened a budget for something else would
    have been demanded a row it does not need. What makes a registration need a
    long timeout is the model call, not the accounting object wrapped around it.

    Structural, not textual, in both halves. The import half accepts either
    style (`from agentctl import advisor`, `import agentctl.advisor`). The call
    half looks at the identifier actually being CALLED (`ast.Call` func's `attr`
    or `id`), which is what excludes `judge_budget.JudgeBudget(...)` — the called
    name there is `JudgeBudget` — without a special case, and which ignores every
    mention of a judge inside a docstring or comment. `_hook_scripts()` confines
    the sweep to `scripts/hook-*.py`."""
    found = set()
    for p in _hook_scripts():
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:  # pragma: no cover - a hook that will not parse
            continue
        imports_advisor = False
        calls_judge = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module == "agentctl" and any(
                    a.name == "advisor" for a in node.names
                ):
                    imports_advisor = True
            elif isinstance(node, ast.Import):
                if any(
                    a.name in ("agentctl.advisor", "agentctl")
                    for a in node.names
                ):
                    imports_advisor = True
            elif isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                if isinstance(name, str) and name.startswith("judge_"):
                    calls_judge = True
        if imports_advisor and calls_judge:
            found.add(p.name)
    return found


def test_timeout_requirements_scope_matches_every_judge_caller():
    """should-fix #3: nothing tied TIMEOUT_REQUIREMENTS' membership to its own
    stated scope -- 'every hook that calls a slow judge'. The set happens to
    equal the table today (three hooks, three rows), so a hook calling a judge
    with no row would reproduce the stage's original symptom (wired,
    presence-green, killed mid-judge) and nothing would catch it. Checked both
    directions: a caller absent from the table, and a table row naming a hook
    that no longer calls a judge."""
    callers = _hooks_that_call_a_judge()
    required = {name for name, _minimum, _why in hook_wiring.TIMEOUT_REQUIREMENTS}
    missing_rows = sorted(callers - required)
    assert not missing_rows, (
        "hooks that call an agentctl.advisor judge_* but carry no "
        f"TIMEOUT_REQUIREMENTS row: {missing_rows}"
    )
    stale_rows = sorted(required - callers)
    assert not stale_rows, (
        "TIMEOUT_REQUIREMENTS rows for hooks that no longer call an "
        f"agentctl.advisor judge_*: {stale_rows}"
    )


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
        const_name = hook_wiring.TIMEOUT_REQUIREMENT_OWN_CONSTANT.get(name)
        assert const_name, (
            f"{name} has no TIMEOUT_REQUIREMENT_OWN_CONSTANT entry — see "
            "test_timeout_requirement_own_constant_covers_every_requirement"
        )
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


def test_calls_per_hook_agrees_with_the_calibrations_call_sequence():
    """K lives in two places by necessity — this table (a number, next to the
    budget it justifies) and lib/judge_latency.HOOK_CALL_SEQUENCE (which judges,
    in order, since the sizing rule needs their individual medians). Same fact,
    two shapes, so the count must agree with the length. Covers every row in both
    directions: a hook with a sequence but no K would silently escape the size
    inequality below."""
    from lib import judge_latency

    assert set(hook_wiring.TIMEOUT_REQUIREMENT_CALLS) == set(
        judge_latency.HOOK_CALL_SEQUENCE
    )
    for name, k in hook_wiring.TIMEOUT_REQUIREMENT_CALLS.items():
        sequence = judge_latency.HOOK_CALL_SEQUENCE[name]
        assert k == len(sequence), (
            f"{name} declares K={k} but HOOK_CALL_SEQUENCE names {len(sequence)} "
            f"judges: {sequence}"
        )


def test_every_timeout_requirement_declares_its_call_count():
    """Bidirectional coverage, the same shape as
    test_timeout_requirement_own_constant_covers_every_requirement: a budget
    without a declared K cannot be checked against the sizing rule at all."""
    required = {name for name, _minimum, _why in hook_wiring.TIMEOUT_REQUIREMENTS}
    assert required == set(hook_wiring.TIMEOUT_REQUIREMENT_CALLS)
    for name, k in hook_wiring.TIMEOUT_REQUIREMENT_CALLS.items():
        assert isinstance(k, int) and k >= 1, f"{name} declares a nonsense K: {k}"


def test_each_hooks_budget_covers_the_calls_it_declares():
    """The size inequality, for EVERY hook making more than one call: the budget
    must cover the medians of the calls that precede the last one, plus a floor
    for the last, plus a named headroom (lib/judge_latency.required_budget_s).

    Without it a multi-judge budget is only plausible: 30s looked ample for
    three judges right up to the point where their measured medians (11.86 +
    7.46) left less than the outage judge's own 20s floor, so the third judge
    was dropped on every turn that reached it — recorded in `judges_skipped`, but
    the budget itself never said it was too small. K = 1 hooks are checked too;
    there the rule degenerates to one floor plus headroom.

    hook-published-text-writer-gate.py is the first UNMEASURED-sequence row
    this test has seen: its one call (published_attachment) has n=0, so
    `required_budget_s` cannot compute a p90 floor for it (`call_floor_s`
    raises KeyError on an unmeasured row) — the rule for such a hook is
    instead `minimum >= LAST_RESORT_CEILING_S + SIZE_HEADROOM_S`, which this
    hook's declared budget satisfies with headroom (LAST_RESORT_CEILING_S is
    a running max over measured rows, so the exact figures shift as samples
    are added — this test recomputes them live rather than pinning a value
    here). That rule is
    STRICTER than the measured-row rule it replaces: a measured K = 1 hook
    only needs to clear its own judge's p90 floor, typically well under the
    last-resort ceiling (the worst latency observed on ANY judge on this
    model) — see judge_latency.call_floor_s vs .last_resort_ceiling_s."""
    from lib import judge_latency

    for name, minimum, _why in hook_wiring.TIMEOUT_REQUIREMENTS:
        sequence = judge_latency.HOOK_CALL_SEQUENCE[name]
        if any(not judge_latency.row(j).measured for j in sequence):
            needed = judge_latency.LAST_RESORT_CEILING_S + judge_latency.SIZE_HEADROOM_S
            assert minimum >= needed, (
                f"{name}'s {minimum}s budget cannot cover its unmeasured "
                f"call(s) {sequence} — it needs at least {needed}s "
                f"(LAST_RESORT_CEILING_S + SIZE_HEADROOM_S, since no "
                f"per-judge floor exists to size against)"
            )
            continue
        needed = judge_latency.required_budget_s(name)
        assert minimum >= needed, (
            f"{name}'s {minimum}s budget cannot fund the "
            f"{hook_wiring.TIMEOUT_REQUIREMENT_CALLS[name]} calls it declares — "
            f"it needs at least {needed}s "
            f"(medians of {judge_latency.HOOK_CALL_SEQUENCE[name][:-1]} + the "
            f"floor of {judge_latency.HOOK_CALL_SEQUENCE[name][-1]} + "
            f"{judge_latency.SIZE_HEADROOM_S}s headroom)"
        )
