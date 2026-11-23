"""Test-only: how the suite gets past spawn-specialist's config-root check.

`config_root_refusal()` refuses a spawn whose config root, agent home or `$HOME` is not
the login home's, and reads the login home from the password database so no env prefix
can move it. Spawn tests build a private world under a pytest tmp dir that no login home
can match, so each spawn in them would be refused. Two test-side answers, neither of
which touches the production check:

- a spawner a test loads by path and runs in-process gets the check answered "no
  refusal" unless the test arms it (`ARMED`, set by the `config_root_check_armed`
  fixture): `install()` wraps `SourceFileLoader.exec_module` in the pytest process only;
- a spawner a test starts as a subprocess is started through `launcher()`: a script
  written into a fresh temp dir at call time that loads `spawn-specialist.py`, replaces
  `config_root_refusal` with one that never refuses, and runs its `main()`. A test that
  launches `spawn-specialist.py` directly gets the real check.

Nothing committed disables the check: the launcher exists only as a file under the
session's temp dir, and no environment variable, flag or interpreter hook is involved.
"""
from __future__ import annotations

import atexit
import importlib.machinery
import os
import shutil
import sys
import tempfile
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SPAWNER = SCRIPTS_DIR / "spawn-specialist.py"
CHECK_NAME = "config_root_refusal"
ARMED = False

_LAUNCHER_SOURCE = '''\
import importlib.util
import sys

SPAWNER = {spawner!r}
sys.path.insert(0, {scripts!r})
spec = importlib.util.spec_from_file_location("spawn_specialist", SPAWNER)
module = importlib.util.module_from_spec(spec)
sys.modules["spawn_specialist"] = module
spec.loader.exec_module(module)
module.config_root_refusal = lambda: None
sys.argv[0] = SPAWNER
sys.exit(module.main())
'''

_launcher_path: str | None = None


def launcher() -> str:
    """Path of the launcher script; written on the first call, removed at process exit."""
    global _launcher_path
    if _launcher_path is None or not os.path.exists(_launcher_path):
        directory = tempfile.mkdtemp(prefix="spawner-launcher-")
        atexit.register(shutil.rmtree, directory, ignore_errors=True)
        _launcher_path = os.path.join(directory, "launch-spawner.py")
        with open(_launcher_path, "w") as handle:
            handle.write(_LAUNCHER_SOURCE.format(spawner=str(SPAWNER), scripts=str(SCRIPTS_DIR)))
    return _launcher_path


_TOPO_DRIVER_SOURCE = '''\
import importlib.util
import subprocess
import sys

DRIVER = {driver!r}
sys.path.insert(0, {scripts!r})
spec = importlib.util.spec_from_file_location("plan_review_topological", DRIVER)
module = importlib.util.module_from_spec(spec)
sys.modules["plan_review_topological"] = module
spec.loader.exec_module(module)


def spawn_pair(argv):
    proc = subprocess.run([sys.executable, {launcher!r}, *argv], capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


module.spawn_pair = spawn_pair
sys.argv[0] = DRIVER
sys.exit(module.main())
'''

_topo_driver_path: str | None = None


def topo_driver_launcher() -> str:
    """Path of a script that runs `plan-review-topological.py` with its `spawn_pair` seam
    starting `launcher()`, so the pair spawns it makes get the same treatment."""
    global _topo_driver_path
    if _topo_driver_path is None or not os.path.exists(_topo_driver_path):
        directory = tempfile.mkdtemp(prefix="topo-driver-launcher-")
        atexit.register(shutil.rmtree, directory, ignore_errors=True)
        _topo_driver_path = os.path.join(directory, "launch-topo-driver.py")
        with open(_topo_driver_path, "w") as handle:
            handle.write(_TOPO_DRIVER_SOURCE.format(
                driver=str(SCRIPTS_DIR / "plan-review-topological.py"),
                scripts=str(SCRIPTS_DIR), launcher=launcher()))
    return _topo_driver_path


def _guarded(check):
    def config_root_refusal():
        return check() if ARMED else None

    config_root_refusal.unguarded = check
    return config_root_refusal


def _is_spawner(path) -> bool:
    return os.path.basename(str(path or "")) == SPAWNER.name


def install() -> None:
    """Guard every spawner this process loads by path from now on."""
    loader = importlib.machinery.SourceFileLoader
    if hasattr(loader.exec_module, "unguarded"):
        return
    exec_module = loader.exec_module

    def guarding_exec_module(self, module):
        exec_module(self, module)
        if _is_spawner(getattr(module, "__file__", None)):
            check = module.__dict__.get(CHECK_NAME)
            if check is not None and not hasattr(check, "unguarded"):
                module.__dict__[CHECK_NAME] = _guarded(check)

    guarding_exec_module.unguarded = exec_module
    loader.exec_module = guarding_exec_module
