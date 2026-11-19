"""Every hook entry the installer registers leaves exactly one valid decision row.

The registered entries are read from install-reminder-hooks.sh, so a hook added there
without `hook_telemetry.run` in its `__main__` fails here. Each entry runs as a subprocess
with a benign payload in a hermetic home, the way the harness would run it."""
import functools
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
INSTALLER = SCRIPTS / "install-reminder-hooks.sh"
ENTRY_RE = re.compile(r'^\s*\("(\w+)",\s*(?:None|"[^"]*"),\s*"([^"]+)",\s*\d+\)', re.MULTILINE)
EVENTS = {"UserPromptSubmit", "PreToolUse", "PostToolUse", "SessionStart", "SessionEnd", "Stop"}
FIELDS = {"ts", "mechanism", "event", "decision", "session", "test", "layer"}


def registered_entries() -> list[tuple[str, str]]:
    return ENTRY_RE.findall(INSTALLER.read_text(encoding="utf-8"))


@functools.cache
def reaper_names() -> tuple[str, ...]:
    # The listing is itself a hook-reaper run, so its own decision row goes nowhere rather than
    # into the log of the test that happens to call this first.
    quiet = {**os.environ, "AGENTCTL_DECISION_LOG": os.devnull, "AGENTCTL_GATE_LOG": os.devnull}
    listing = subprocess.run([sys.executable, str(SCRIPTS / "hook-reaper.py"), "--list"], env=quiet,
                             capture_output=True, text=True, timeout=120, check=True)
    names = tuple(line.split()[0] for line in listing.stdout.splitlines() if line.strip())
    assert names, listing.stderr
    return names


def silence_reapers(home: Path) -> None:
    """Mark every reaper as just run, so the hooks under test start no reaper pass.

    A fresh home has no throttle stamps, so the SessionStart reaper (and the pressure pass that
    hook-branch-backup detaches) would treat every reaper as due and run a full pass over the
    real machine — plugin reapers scan machine-wide state — outliving the test's timeout under
    load and touching files that are not the test's."""
    stamps = home / ".local" / "state" / "claude-reaper"
    stamps.mkdir(parents=True)
    now = str(time.time())
    for name in reaper_names():
        (stamps / f"{name}.stamp").write_text(now, encoding="utf-8")
        (stamps / f"{name}.pressure.stamp").write_text(now, encoding="utf-8")


def test_the_installer_registers_the_44_entries():
    assert len(registered_entries()) == 44


@pytest.mark.parametrize("event,command", registered_entries(),
                         ids=lambda v: v.replace(" ", "_"))
def test_a_registered_hook_leaves_one_valid_row(event, command, tmp_path):
    assert event in EVENTS
    script, *flags = command.split()
    log = tmp_path / "decision-log.jsonl"
    home = tmp_path / "home"
    home.mkdir()
    silence_reapers(home)
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "CLAUDE_AGENT_HOME": str(home / ".claude-agent"),
        "AGENTCTL_DECISION_LOG": str(log),
        "AGENTCTL_GATE_LOG": str(tmp_path / "gate-log.jsonl"),
        "AGENT_TELEMETRY_TEST": "1",
    }
    payload = {"hook_event_name": event, "session_id": "hook-coverage", "cwd": str(tmp_path),
               "tool_name": "Read", "tool_input": {"file_path": str(tmp_path / "f")},
               "prompt": "hello", "transcript_path": str(tmp_path / "none.jsonl")}
    subprocess.run([sys.executable, str(SCRIPTS / script), *flags], input=json.dumps(payload),
                   env=env, cwd=tmp_path, capture_output=True, text=True, timeout=120)
    # A hook may spawn detached children (hook-branch-backup -> hook-reaper) that inherit the log
    # env and append their own rows at any moment, so count only the hook under test.
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    rows = [r for r in rows if r.get("mechanism") == Path(script).stem]
    assert len(rows) == 1, rows
    row = rows[0]
    assert set(row) == FIELDS
    assert row["mechanism"] == Path(script).stem
    assert row["event"] in {event, "unknown"}
    assert row["decision"] in {"deny", "nudge", "silent"}
    assert row["layer"] == "core" and row["test"] is True
