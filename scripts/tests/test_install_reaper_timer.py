"""Tests for scripts/install-reaper-timer.sh: units, frozen environment, uninstall, status, --check.

No test calls the real systemctl, loginctl or journalctl: the committed stubs in
fixtures/reaper_timer_stubs answer from the test's environment, and units go to a temp dir.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
INSTALLER = SCRIPTS / "install-reaper-timer.sh"
HOOK = SCRIPTS / "hook-reaper.py"
STUBS = Path(__file__).resolve().parent / "fixtures" / "reaper_timer_stubs"
UNSET_LINE = "UnsetEnvironment=SSH_AUTH_SOCK SSH_AGENT_PID GIT_ASKPASS SSH_ASKPASS"
INVOCATION = "0123456789abcdef0123456789abcdef"
CREDENTIALS = ("SSH_AUTH_SOCK", "SSH_AGENT_PID", "GIT_ASKPASS", "SSH_ASKPASS")
UNIT_NAMES = (
    "agent-reaper-pressure.timer", "agent-reaper-pressure.service",
    "agent-reaper-pass.service", "agent-reaper-only@.service",
)


@pytest.fixture
def box(tmp_path):
    root = tmp_path.resolve()
    for sub in ("home", "tmp", "agent"):
        (root / sub).mkdir()
    return {
        "root": root, "home": root / "home", "tmp": root / "tmp", "agent": root / "agent",
        "units": root / "units", "state": root / "stub", "obs_src": root / "obs.json",
    }


def _env(box, extra=None, drop=()):
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(box["home"]),
        "SYSTEMD_USER_DIR": str(box["units"]),
        "SYSTEMCTL_BIN": str(STUBS / "systemctl"),
        "LOGINCTL_BIN": str(STUBS / "loginctl"),
        "JOURNALCTL_BIN": str(STUBS / "journalctl"),
        "REAPER_TIMER_STUB_STATE": str(box["state"]),
        "REAPER_TIMER_STUB_OBS_DIR": str(box["agent"] / "reaper" / "unit-pass"),
        "CLAUDE_CONFIG_DIR": str(box["agent"]),
        "TMPDIR": str(box["tmp"]),
    }
    env.update(extra or {})
    for name in drop:
        env.pop(name, None)
    return env


def run(box, *args, extra=None, drop=()):
    return subprocess.run(
        ["bash", str(INSTALLER), *args], env=_env(box, extra, drop), capture_output=True, text=True, timeout=60,
    )


def calls(box):
    log = box["state"] / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


def unit(box, name):
    return (box["units"] / name).read_text()


def env_value(text, key):
    return re.search(rf"^Environment={key}=(.*)$", text, re.M).group(1)


def good_observation(box, **changes):
    seen = {
        "invocation_id": INVOCATION, "tmpdir": str(box["tmp"]), "agent_home": str(box["agent"]),
        "credentials_present": {name: False for name in CREDENTIALS}, "reapers_run": 3, "errors": 0,
    }
    seen.update(changes)
    return seen


def check(box, observation="good", extra=None):
    """Install, then --check against stubs; ``observation`` is a dict, None (no file) or "good"."""
    assert run(box, "--tmpdir", str(box["tmp"])).returncode == 0
    env = dict(extra or {})
    if observation == "good":
        observation = good_observation(box)
    if observation is not None:
        box["obs_src"].write_text(json.dumps(observation))
        env["REAPER_TIMER_STUB_OBS_FILE"] = str(box["obs_src"])
    return run(box, "--check", extra=env)


def failed_clause(res):
    assert res.returncode == 1, res.stdout + res.stderr
    return re.search(r"^check failed: (\w+):", res.stderr, re.M).group(1)


# ── install ─────────────────────────────────────────────────────────────────

def test_install_writes_the_units_and_enables_only_the_timer(box):
    assert run(box, "--tmpdir", str(box["tmp"])).returncode == 0
    assert sorted(p.name for p in box["units"].iterdir()) == sorted(UNIT_NAMES)
    timer = unit(box, "agent-reaper-pressure.timer")
    assert "OnCalendar=minutely" in timer and "Persistent=false" in timer and "WantedBy=timers.target" in timer
    for name, flag in (
        ("agent-reaper-pressure.service", "--on-pressure"),
        ("agent-reaper-pass.service", "--force-run"),
        ("agent-reaper-only@.service", "--only %i"),
    ):
        text = unit(box, name)
        assert "Type=oneshot" in text
        assert re.search(rf"^ExecStart=/\S+ {re.escape(str(HOOK))} {re.escape(flag)}$", text, re.M)
    assert calls(box) == [
        "--user is-system-running", "--user daemon-reload", "--user enable --now agent-reaper-pressure.timer",
    ]


def test_units_use_only_flags_the_runner_defines():
    helped = subprocess.run(["python3", str(HOOK), "--help"], capture_output=True, text=True, timeout=60).stdout
    flags = re.findall(r"^ExecStart=/\S+ \S+ (--[a-z-]+)", "".join(
        line for line in INSTALLER.read_text().replace("$PYTHON $HOOK", "/p h").splitlines(keepends=True)
    ), re.M)
    assert sorted(flags) == ["--force-run", "--on-pressure", "--only"]
    assert all(flag in helped for flag in flags)


def test_every_unit_freezes_the_tmpdir_the_agent_home_and_unsets_the_credentials(box):
    assert run(box, "--tmpdir", str(box["tmp"])).returncode == 0
    for name in UNIT_NAMES[1:]:
        text = unit(box, name)
        assert env_value(text, "TMPDIR") == str(box["tmp"])
        assert env_value(text, "CLAUDE_CONFIG_DIR") == env_value(text, "CLAUDE_AGENT_HOME") == str(box["agent"])
        assert UNSET_LINE in text.splitlines()
    assert "SSH_AUTH_SOCK" not in "".join(re.findall(r"^Environment=.*$", unit(box, "agent-reaper-pass.service"), re.M))


def test_tmpdir_argument_beats_the_installers_own_tmpdir(box):
    other = box["root"] / "other"
    other.mkdir()
    assert run(box, "--tmpdir", str(other)).returncode == 0
    assert env_value(unit(box, "agent-reaper-pass.service"), "TMPDIR") == str(other)


def test_tmpdir_comes_from_the_installers_environment_and_is_resolved(box):
    link = box["root"] / "link"
    link.symlink_to(box["tmp"])
    assert run(box, extra={"TMPDIR": str(link)}).returncode == 0
    assert env_value(unit(box, "agent-reaper-pass.service"), "TMPDIR") == str(box["tmp"])


def test_without_any_tmpdir_the_default_is_frozen_with_a_warning_naming_it(box):
    res = run(box, drop=("TMPDIR",))
    assert res.returncode == 0
    frozen = env_value(unit(box, "agent-reaper-pass.service"), "TMPDIR")
    assert frozen.startswith("/") and f"freezing the platform default {frozen}" in res.stderr


@pytest.mark.parametrize(
    "given, expected",
    [
        ({"CLAUDE_CONFIG_DIR": "cfg"}, "cfg"),
        ({"CLAUDE_AGENT_HOME": "home2"}, "home2"),
        ({"CLAUDE_CONFIG_DIR": "cfg", "CLAUDE_AGENT_HOME": "home2"}, "cfg"),
    ],
)
def test_the_agent_home_follows_the_config_root_precedence(box, given, expected):
    for name in ("cfg", "home2"):
        (box["root"] / name).mkdir()
    extra = {key: str(box["root"] / value) for key, value in given.items()}
    res = run(box, "--tmpdir", str(box["tmp"]), extra=extra, drop=("CLAUDE_CONFIG_DIR",) if "CLAUDE_CONFIG_DIR" not in given else ())
    assert res.returncode == 0
    text = unit(box, "agent-reaper-pass.service")
    assert env_value(text, "CLAUDE_CONFIG_DIR") == env_value(text, "CLAUDE_AGENT_HOME") == str(box["root"] / expected)


def test_a_path_a_unit_cannot_carry_is_refused(box):
    odd = box["root"] / "a b"
    odd.mkdir()
    res = run(box, "--tmpdir", str(odd))
    assert res.returncode == 2 and not box["units"].exists()


def test_reinstall_is_idempotent(box):
    run(box, "--tmpdir", str(box["tmp"]))
    first = {n: unit(box, n) for n in UNIT_NAMES}
    assert run(box, "--tmpdir", str(box["tmp"])).returncode == 0
    assert first == {n: unit(box, n) for n in UNIT_NAMES}


def test_a_missing_linger_warns_on_install_and_still_installs(box):
    res = run(box, "--tmpdir", str(box["tmp"]), extra={"REAPER_TIMER_STUB_LINGER": "no"})
    assert res.returncode == 0 and "linger is off" in res.stderr


def test_dry_run_prints_the_units_and_makes_no_systemctl_call(box):
    res = run(box, "--dry-run", "--tmpdir", str(box["tmp"]), extra={"REAPER_TIMER_STUB_MANAGER": "none"})
    assert res.returncode == 0
    assert UNSET_LINE in res.stdout.splitlines()
    assert calls(box) == [] and not box["units"].exists()


# ── uninstall and status ────────────────────────────────────────────────────

def test_uninstall_removes_the_units_and_disables_the_timer(box):
    run(box, "--tmpdir", str(box["tmp"]))
    assert run(box, "--uninstall").returncode == 0
    assert list(box["units"].glob("agent-reaper*")) == []
    assert "--user disable --now agent-reaper-pressure.timer" in calls(box)
    assert not (box["state"] / "enabled").exists()


def test_status_reports_installed_with_the_timer_state(box):
    run(box, "--tmpdir", str(box["tmp"]))
    res = run(box, "--status")
    assert res.returncode == 0 and "installed" in res.stdout and "is-enabled=enabled is-active=active" in res.stdout


def test_status_reports_not_installed_with_exit_3(box):
    res = run(box, "--status")
    assert res.returncode == 3 and "not installed" in res.stdout


def test_status_is_not_installed_when_one_unit_is_missing(box):
    run(box, "--tmpdir", str(box["tmp"]))
    (box["units"] / "agent-reaper-only@.service").unlink()
    assert run(box, "--status").returncode == 3


@pytest.mark.parametrize("args", [(), ("--uninstall",), ("--status",), ("--check",)])
def test_every_mode_exits_4_without_a_user_manager_and_changes_nothing(box, args):
    res = run(box, *args, extra={"REAPER_TIMER_STUB_MANAGER": "none"})
    assert res.returncode == 4 and "no systemd user manager" in res.stderr
    assert calls(box) == ["--user is-system-running"] and not box["units"].exists()


def test_a_degraded_manager_is_still_a_manager(box):
    assert run(box, "--status", extra={"REAPER_TIMER_STUB_MANAGER": "degraded"}).returncode == 3


def test_unknown_flag_and_dry_run_with_check_are_usage_errors(box):
    assert run(box, "--bogus").returncode == 2
    assert run(box, "--dry-run", "--check").returncode == 2


# ── --check ─────────────────────────────────────────────────────────────────

def test_check_passes_when_the_run_is_clean_and_the_observation_matches(box):
    res = check(box)
    assert res.returncode == 0, res.stderr
    assert "check passed" in res.stdout
    assert "--user start --wait agent-reaper-pass.service" in calls(box)


def test_check_passes_although_the_managers_environment_carries_an_ssh_agent(box):
    res = check(box, extra={"SSH_AUTH_SOCK": "/run/agent.sock"})
    assert res.returncode == 0, res.stderr


def test_check_fails_on_a_missing_unit(box):
    run(box, "--tmpdir", str(box["tmp"]))
    (box["units"] / "agent-reaper-pressure.timer").unlink()
    assert failed_clause(run(box, "--check")) == "units"


def test_check_fails_when_the_template_unit_environment_differs(box):
    run(box, "--tmpdir", str(box["tmp"]))
    path = box["units"] / "agent-reaper-only@.service"
    path.write_text(path.read_text().replace(f"Environment=TMPDIR={box['tmp']}", "Environment=TMPDIR=/elsewhere"))
    assert failed_clause(run(box, "--check")) == "environment"


def test_check_fails_on_result_failed(box):
    assert failed_clause(check(box, extra={"REAPER_TIMER_STUB_RESULT": "failed"})) == "result"


def test_check_fails_on_a_runner_error_line_in_the_journal(box):
    res = check(box, extra={"REAPER_TIMER_STUB_JOURNAL": "reaper: runner error: OSError: boom"})
    assert failed_clause(res) == "journal"
    assert "OSError: boom" in res.stderr


def test_check_fails_without_an_observation_file_for_the_invocation(box):
    assert failed_clause(check(box, observation=None)) == "observation"


def test_check_fails_when_the_observation_names_another_invocation(box):
    assert failed_clause(check(box, good_observation(box, invocation_id="ffff"))) == "observation"


def test_check_fails_on_an_observed_tmpdir_other_than_the_frozen_one(box):
    assert failed_clause(check(box, good_observation(box, tmpdir="/tmp"))) == "tmpdir"


def test_check_fails_on_an_observed_agent_home_other_than_the_frozen_one(box):
    assert failed_clause(check(box, good_observation(box, agent_home="/root/.claude"))) == "agent_home"


@pytest.mark.parametrize("variable", CREDENTIALS)
def test_check_fails_on_each_credential_flag_present(box, variable):
    creds = {name: name == variable for name in CREDENTIALS}
    res = check(box, good_observation(box, credentials_present=creds))
    assert failed_clause(res) == "credentials" and variable in res.stderr


def test_check_fails_when_no_reaper_ran(box):
    assert failed_clause(check(box, good_observation(box, reapers_run=0))) == "reapers_run"


def test_check_fails_on_observed_errors(box):
    assert failed_clause(check(box, good_observation(box, errors=2))) == "errors"


def test_check_fails_without_linger(box):
    assert failed_clause(check(box, extra={"REAPER_TIMER_STUB_LINGER": "no"})) == "linger"
