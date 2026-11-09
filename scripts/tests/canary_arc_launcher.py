"""Canary for arc_launcher_recorder: one real launcher run from a literal environment.

Not a test_* module: conftest collects it only when ARC_CANARY_RUN=1, so it never runs as an
ordinary suite member. Run under the recorder (``arc_launcher_recorder.py --canary-check``)
it proves a launcher call made with a fresh HOME and a literal PATH is recorded with this
test's id, and that the call downloads nothing — the sandbox HOME holds one release copied
from the invoking user's own, found through the password database because a scrubbed HOME
would hide it.
"""
from __future__ import annotations

import os
import pwd
import shutil
import subprocess
from pathlib import Path

import pytest
from model_cli_path import path_without_model_cli


def _link_or_copy(src: Path, dst: Path) -> None:
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _seed_home(real_home: Path, home: Path) -> set[str]:
    """Copy the releases the channel links point at into `home` and relink the channels."""
    channels = real_home / ".arc" / "channels"
    releases = set()
    (home / ".arc" / "channels").mkdir(parents=True)
    (home / ".arc" / "releases").mkdir()
    for link in channels.iterdir():
        if not link.is_symlink():
            continue
        target = Path(os.readlink(link))
        if not target.is_absolute():
            target = (channels / target).resolve()
        if not target.is_dir():
            continue
        new_release = home / ".arc" / "releases" / target.name
        if not new_release.exists():
            new_release.mkdir()
            for entry in target.iterdir():
                _link_or_copy(entry, new_release / entry.name)
        (home / ".arc" / "channels" / link.name).symlink_to(new_release)
        releases.add(target.name)
    return releases


def test_launcher_run_downloads_nothing(tmp_path):
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    if not (real_home / ".arc" / "channels").is_dir():
        pytest.skip(f"no arc release under {real_home}/.arc to copy")
    home = tmp_path / "home"
    home.mkdir()
    seeded = _seed_home(real_home, home)
    assert seeded, "no channel link resolved to a release directory"

    env = {
        "PATH": path_without_model_cli(real_arc=True),
        "HOME": str(home),
        "ARC_UPDATE_INTERVAL": "0",
        "PYTEST_CURRENT_TEST": os.environ.get("PYTEST_CURRENT_TEST", ""),
    }
    proc = subprocess.run(["arc", "--version"], env=env, capture_output=True, text=True, timeout=120)

    assert proc.returncode == 0, proc.stderr[-500:]
    assert {p.name for p in (home / ".arc" / "releases").iterdir()} == seeded
