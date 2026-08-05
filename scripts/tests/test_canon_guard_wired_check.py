"""Tests for hook-canon-guard-wired-check.py — the SessionStart detector that
warns when the gate-bearing hooks are not wired into the root the harness
actually loads from.

Two layers, and the tests keep them apart. The canon guard has a finer
requirement than the rest — it must be in BOTH PreToolUse chains (Edit|Write
and Bash) and point at a script that exists. Every other gate-bearing hook is
checked for presence anywhere in the root's settings chain.

Hermetic: the hook reads live settings from $CLAUDE_CANON_GUARD_SETTINGS (test
seam); each test writes a crafted settings.json there and asserts on stdout.
The seam designates the chain's PRIMARY member and the rest of the chain is
derived from its parent directory, so a fixture in a tmp dir has no siblings
and the chain collapses to the one file — except for the machine-wide managed
policy member, whose contribution is asserted neutral by
`test_managed_policy_member_is_neutral_here` so that a red elsewhere in this
module cannot be blamed on it.

Every fixture here pins $CLAUDE_CONFIG_DIR, which collapses the two config-root
accessors onto one directory and so puts the hook on its AGENT-ROOT branch —
the branch this module is about. Without the pin the branch would be decided by
the developer's machine and the cwd pytest happens to run from, and the wiring
report these tests assert on is not reachable at all on the other branch.

The wiring report is asserted by the BANNER rather than by "stdout is empty":
the hook now opens every non-silent path with an unconditional `[config-root]`
status line, so emptiness stopped being the observable for "no problems found".

Non-blocking and fail-open: the hook always exits 0 and only ever warns.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))
from lib import hook_wiring  # noqa: E402

HOOK_SCRIPT = SCRIPTS_DIR / "hook-canon-guard-wired-check.py"
# A real, existing script path so "wired path exists" cases pass the os.path.exists check.
GUARD_PATH = str(SCRIPTS_DIR / "hook-guard-canon-readonly.py")
GUARD_BASENAME = "hook-guard-canon-readonly.py"
OTHER_GATE_HOOKS = [n for n, _ in hook_wiring.GATE_BEARING_HOOKS if n != GUARD_BASENAME]
BANNER = "GATE-BEARING HOOKS ARE NOT FULLY WIRED"


def _group(matcher: str, command: str) -> dict:
    return {"matcher": matcher, "hooks": [{"type": "command", "command": command}]}


def _env(settings_path: Path, root: Path) -> dict:
    return {
        **os.environ,
        "CLAUDE_CANON_GUARD_SETTINGS": str(settings_path),
        "CLAUDE_CONFIG_DIR": str(root),
    }


def _run(tmp_path: Path, settings: dict) -> subprocess.CompletedProcess:
    sp = tmp_path / "settings.json"
    sp.write_text(json.dumps(settings), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        env=_env(sp, tmp_path), capture_output=True, text=True,
    )


def _assert_no_problem(proc: subprocess.CompletedProcess) -> None:
    """The agent-root branch found nothing to report.

    Three claims, not one. Exit 0 and an empty stderr are the fail-open and
    channel halves; BANNER-absence is the wiring verdict. Asserting only the
    third would pass just as happily if the hook had crashed before reaching
    the check, which is the failure this detector exists to not have."""
    assert proc.returncode == 0
    assert proc.stderr == "", proc.stderr
    assert BANNER not in proc.stdout, proc.stdout
    assert proc.stdout.startswith("[config-root] harness="), proc.stdout


def _registry_groups(names=None) -> list:
    """PreToolUse groups wiring the non-canon gate-bearing hooks, so a fixture
    can be silent about them and the test can speak about the canon guard only."""
    return [_group("Bash", str(SCRIPTS_DIR / n)) for n in (names or OTHER_GATE_HOOKS)]


def _wired_both(edit_cmd: str, bash_cmd: str) -> dict:
    """Canon guard in both chains, and every other gate-bearing hook wired —
    otherwise a fixture written to say something about the canon guard would
    also, correctly, trip the registry sweep."""
    return {"hooks": {"PreToolUse": [
        _group("Edit|Write", edit_cmd),
        _group("Bash", bash_cmd),
    ] + _registry_groups()}}


def test_managed_policy_member_is_neutral_here():
    """The one member of the chain this module cannot write. If a machine's
    managed policy were unreadable, every probe would honestly degrade to
    UNKNOWN and the sweep would go silent — correct in production, but it would
    surface here as an unexplained red. Assert the precondition instead."""
    managed = hook_wiring.managed_settings_path()
    if not managed.is_file():
        return
    try:
        data = json.loads(managed.read_text(encoding="utf-8"))
    except Exception:
        raise AssertionError(
            f"{managed} exists but is unreadable/unparseable; the sweep tests in "
            "this module degrade to UNKNOWN through no fault of the hook")
    assert isinstance(data, dict) and not data.get("hooks"), (
        f"{managed} declares hooks; this module's fixtures assume it does not")


def test_no_problem_when_wired_in_both_chains(tmp_path):
    _assert_no_problem(_run(tmp_path, _wired_both(GUARD_PATH, GUARD_PATH)))


def test_warns_when_absent_from_edit_chain(tmp_path):
    settings = {"hooks": {"PreToolUse": [
        _group("Bash", GUARD_PATH),  # only Bash wired
    ]}}
    proc = _run(tmp_path, settings)
    assert proc.returncode == 0
    assert "Edit|Write chain" in proc.stdout


def test_warns_when_absent_from_bash_chain(tmp_path):
    settings = {"hooks": {"PreToolUse": [
        _group("Edit|Write", GUARD_PATH),  # only Edit|Write wired
    ]}}
    proc = _run(tmp_path, settings)
    assert proc.returncode == 0
    assert "Bash chain" in proc.stdout


def test_warns_when_absent_from_both_chains(tmp_path):
    settings = {"hooks": {"PreToolUse": [
        _group("Edit|Write", "/some/other/hook.py"),
    ]}}
    proc = _run(tmp_path, settings)
    assert proc.returncode == 0
    assert "Edit|Write chain" in proc.stdout
    assert "Bash chain" in proc.stdout


def test_the_warning_never_goes_to_stderr(tmp_path):
    """The channel IS the delivery. A SessionStart hook's stdout is attached to
    the session as context; its stderr reaches only the human's terminal, where
    the one reader who can act on "the gates are off" — the agent about to write
    to a gated file — never sees it. A report on the wrong channel is a report
    nobody acts on, so this pins the whole block, not merely one line of it."""
    proc = _run(tmp_path, {"hooks": {"PreToolUse": [
        _group("Edit|Write", "/some/other/hook.py"),
    ]}})
    assert BANNER in proc.stdout
    assert proc.stderr == "", proc.stderr


def test_warns_when_wired_path_missing(tmp_path):
    missing = "/nonexistent/dir/hook-guard-canon-readonly.py"
    proc = _run(tmp_path, _wired_both(missing, missing))
    assert proc.returncode == 0
    assert "missing script path" in proc.stdout


def test_wired_command_with_args_path_exists_is_no_problem(tmp_path):
    """The script path is the first token; trailing args must not break the
    exists() check."""
    cmd = f"{GUARD_PATH} --some-arg"
    _assert_no_problem(_run(tmp_path, _wired_both(cmd, cmd)))


def test_missing_settings_file_fails_open(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        env=_env(tmp_path / "nope.json", tmp_path),
        capture_output=True, text=True,
    )
    assert proc.returncode == 0
    assert proc.stderr == ""
    assert BANNER not in proc.stdout, proc.stdout


def test_the_status_line_survives_a_missing_settings_file(tmp_path):
    """Which root is live does not depend on that root's settings.json parsing,
    so the read failing must cost the wiring REPORT and not the status line."""
    proc = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        env=_env(tmp_path / "nope.json", tmp_path),
        capture_output=True, text=True,
    )
    assert proc.stdout.strip() == f"[config-root] harness={tmp_path} (= agent home)"


def test_malformed_settings_fails_open(tmp_path):
    sp = tmp_path / "settings.json"
    sp.write_text("not json", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        env=_env(sp, tmp_path), capture_output=True, text=True,
    )
    assert proc.returncode == 0
    assert proc.stderr == ""
    assert BANNER not in proc.stdout, proc.stdout


# ── The widened half: every gate-bearing hook, not just the canon guard ──────

def test_names_every_missing_registry_hook(tmp_path):
    """A root where only the canon guard is wired: every OTHER gate-bearing
    hook is named, together with the enforcement that is consequently off."""
    proc = _run(tmp_path, {"hooks": {"PreToolUse": [
        _group("Edit|Write", GUARD_PATH), _group("Bash", GUARD_PATH)]}})
    assert proc.returncode == 0
    for name in OTHER_GATE_HOOKS:
        assert name in proc.stdout, f"{name} not named in the warning"
    assert "NOT registered" in proc.stdout


def test_warning_names_the_root_it_probed(tmp_path):
    """Which root the report is about is the whole point — a green (or red)
    report from an unnamed root is what made the old check misleading."""
    proc = _run(tmp_path, {"hooks": {"PreToolUse": [
        _group("Edit|Write", GUARD_PATH), _group("Bash", GUARD_PATH)]}})
    assert str(tmp_path) in proc.stdout


def test_no_problem_when_registry_hook_wired_in_local_member(tmp_path):
    """No false alarm: a hook wired only in the chain's .local member is wired.
    A single-file read would name it as missing."""
    (tmp_path / "settings.local.json").write_text(
        json.dumps({"hooks": {"Stop": [
            {"hooks": [{"type": "command", "command": str(SCRIPTS_DIR / n)}]}
            for n in OTHER_GATE_HOOKS]}}),
        encoding="utf-8")
    _assert_no_problem(_run(tmp_path, {"hooks": {"PreToolUse": [
        _group("Edit|Write", GUARD_PATH), _group("Bash", GUARD_PATH)]}}))


def test_unknown_is_never_reported(tmp_path):
    """A corrupt secondary member cannot manufacture an ABSENT alarm: the probe
    degrades to UNKNOWN and the sweep says nothing.

    Note on the plan's 'mixed ABSENT + UNKNOWN' fixture: it is not constructible
    against this probe, and deliberately so — hook_wiring's `modelled` flag is
    chain-wide, so an unreadable or unmodelled member degrades EVERY hook's
    answer at once rather than one of them. This test pins the property that
    criterion was after (UNKNOWN is never reported) in the form the design
    admits."""
    (tmp_path / "settings.local.json").write_text("{not json", encoding="utf-8")
    _assert_no_problem(_run(tmp_path, {"hooks": {"PreToolUse": [
        _group("Edit|Write", GUARD_PATH), _group("Bash", GUARD_PATH)]}}))


def test_no_problem_when_everything_is_wired(tmp_path):
    _assert_no_problem(_run(tmp_path, _wired_both(GUARD_PATH, GUARD_PATH)))


def test_advisory_hooks_are_never_reported(tmp_path):
    """Scope discipline: the deliberate root divergence means advisory hooks are
    legitimately absent from a personal root. Naming them every session would
    train the reader to skip the block that carries the real signal."""
    proc = _run(tmp_path, {"hooks": {"PreToolUse": [
        _group("Edit|Write", GUARD_PATH), _group("Bash", GUARD_PATH)]}})
    for advisory in ("hook-self-diagnose-due.py", "hook-skill-first.py",
                     "hook-resolution-reminder.py"):
        assert advisory not in proc.stdout
