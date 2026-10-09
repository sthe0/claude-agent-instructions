"""Tests for scripts/install-claude-recover-systemd.sh — units, entry point, systemctl call log."""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
INSTALLER = SCRIPTS / "install-claude-recover-systemd.sh"
RECOVER = SCRIPTS / "claude-recover.py"


def _exec(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


@pytest.fixture
def box(tmp_path):
    fakes = tmp_path / "fakebin"
    log = tmp_path / "systemctl.log"
    _exec(fakes / "systemctl", f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{log}"\n')
    _exec(fakes / "loginctl", "#!/usr/bin/env bash\necho yes\n")
    _exec(fakes / "claude", "#!/usr/bin/env bash\nexit 0\n")
    _exec(fakes / "tmux", "#!/usr/bin/env bash\nexit 0\n")
    return {
        "unit_dir": tmp_path / "units",
        "bin_dir": tmp_path / "bin",
        "log": log,
        "fakes": fakes,
        "home": tmp_path / "home",
    }


def _run(box, *args, extra_env=None):
    (box["home"]).mkdir(exist_ok=True)
    env = {
        "PATH": f"{box['fakes']}:/usr/bin:/bin",
        "HOME": str(box["home"]),
        "SYSTEMD_USER_DIR": str(box["unit_dir"]),
        "LOCAL_BIN_DIR": str(box["bin_dir"]),
        "SYSTEMCTL_BIN": str(box["fakes"] / "systemctl"),
        "LOGINCTL_BIN": str(box["fakes"] / "loginctl"),
    }
    env.update(extra_env or {})
    return subprocess.run(["bash", str(INSTALLER), *args], env=env, capture_output=True, text=True, timeout=60)


def _calls(box):
    return box["log"].read_text().splitlines() if box["log"].exists() else []


def _unit(box, name):
    return (box["unit_dir"] / name).read_text()


def _module_deadline_s():
    spec = importlib.util.spec_from_file_location("claude_recover_mod", RECOVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.DEADLINE_S


def test_restore_unit_content(box):
    assert _run(box).returncode == 0
    text = _unit(box, "claude-recover.service")
    assert "Type=exec" in text
    assert "network-online.target" not in text
    assert "TimeoutStartSec" not in text
    assert "Restart=" not in text
    assert "tmux" not in re.search(r"^ExecStart=.*$", text, re.M).group(0)
    assert re.search(r"^ExecStart=\S+claude-recover\.py restore --auto$", text, re.M)
    assert "After=ccgram.service" in text and "Wants=ccgram.service" in text
    assert "WantedBy=default.target" in text
    assert re.search(r"^Environment=PATH=.*" + re.escape(str(box["fakes"])), text, re.M)


def test_runtime_max_exceeds_module_deadline(box):
    assert _run(box).returncode == 0
    minutes = int(re.search(r"^RuntimeMaxSec=(\d+)min$", _unit(box, "claude-recover.service"), re.M).group(1))
    assert minutes * 60 > _module_deadline_s()


def test_after_unit_parameter(box):
    assert _run(box, "--after-unit", "other.service").returncode == 0
    text = _unit(box, "claude-recover.service")
    assert "After=other.service" in text and "Wants=other.service" in text
    assert "ccgram" not in text


def test_snapshot_units(box):
    assert _run(box).returncode == 0
    service = _unit(box, "claude-recover-snapshot.service")
    timer = _unit(box, "claude-recover-snapshot.timer")
    assert re.search(r"^ExecStart=\S+claude-recover\.py snapshot$", service, re.M)
    assert "--force" not in service
    assert "Type=oneshot" in service
    assert "WantedBy=timers.target" in timer
    assert "OnUnitActiveSec=1min" in timer


def test_tmux_tmpdir_and_config_dirs_pinned(box):
    env = {"TMUX_TMPDIR": "/run/tmux-x", "CLAUDE_RECOVER_CONFIG_DIRS": "/a:/b"}
    assert _run(box, extra_env=env).returncode == 0
    for name in ("claude-recover.service", "claude-recover-snapshot.service"):
        text = _unit(box, name)
        assert "Environment=TMUX_TMPDIR=/run/tmux-x" in text
        assert "Environment=CLAUDE_RECOVER_CONFIG_DIRS=/a:/b" in text


def test_systemctl_enables_restore_without_starting_it(box):
    assert _run(box).returncode == 0
    calls = _calls(box)
    assert "--user enable --now claude-recover-snapshot.timer" in calls
    assert "--user enable claude-recover.service" in calls
    for call in calls:
        if "claude-recover.service" in call:
            assert "--now" not in call
            assert not re.search(r"\b(start|restart)\b", call)
    assert [c for c in calls if "--now" in c] == ["--user enable --now claude-recover-snapshot.timer"]


def test_entry_point_symlink_and_help(box):
    assert _run(box).returncode == 0
    link = box["bin_dir"] / "claude-recover"
    assert link.is_symlink() and link.resolve() == RECOVER.resolve()
    res = subprocess.run([str(link), "--help"], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0 and "--dry-run" in res.stdout


@pytest.mark.parametrize("sub", ["snapshot", "status"])
def test_subcommand_help_exits_zero(sub):
    res = subprocess.run(["python3", str(RECOVER), sub, "--help"], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0


def test_reinstall_is_idempotent(box):
    assert _run(box).returncode == 0
    first = {n: _unit(box, n) for n in ("claude-recover.service", "claude-recover-snapshot.timer")}
    assert _run(box).returncode == 0
    assert first == {n: _unit(box, n) for n in first}


def test_dry_run_writes_nothing(box):
    res = _run(box, "--dry-run")
    assert res.returncode == 0
    assert not box["unit_dir"].exists() and not box["bin_dir"].exists()
    assert _calls(box) == []


def test_uninstall_removes_units_and_link(box):
    assert _run(box).returncode == 0
    assert _run(box, "--uninstall").returncode == 0
    assert not list(box["unit_dir"].glob("claude-recover*"))
    assert not (box["bin_dir"] / "claude-recover").exists()
    assert "--user disable --now claude-recover-snapshot.timer" in _calls(box)


def test_unknown_flag_rejected(box):
    assert _run(box, "--bogus").returncode == 2
