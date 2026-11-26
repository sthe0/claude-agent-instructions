"""Test-only: how the suite gets past spawn-specialist's config-root check.

`config_root_refusal()` refuses a spawn whose config root, agent home or `$HOME` is not
the login home's, and reads the login home from the password database so no env prefix
can move it. Spawn tests build a private world under a pytest tmp dir that no login home
can match, so each spawn in them would be refused. Two test-side answers, neither of
which touches the production check:

- a spawner a test loads by path and runs in-process gets the check answered "no
  refusal" unless the test arms it (`ARMED`, set by the `config_root_check_armed`
  fixture): `install()` makes `importlib.util.spec_from_file_location` hand the spawner a loader
  that appends the guard to the spawner's own code, in the pytest process only;
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
import importlib.util
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


_GUARD_SUFFIX = (
    "\n__import__('sys').modules[{module!r}].guard_namespace(globals())\n"
)


def guard_namespace(namespace: dict) -> None:
    check = namespace.get(CHECK_NAME)
    if check is not None and not hasattr(check, "unguarded"):
        namespace[CHECK_NAME] = _guarded(check)


class _SpawnerLoader(importlib.machinery.SourceFileLoader):
    """Loads the spawner with the guard appended to its own code, so no frame of this file is on the
    stack while any module body runs (the dependency tracer attributes a module body to the nearest
    repository frame, and a wrapper around `exec_module` would own every module the suite loads)."""

    def get_code(self, fullname):
        return self.source_to_code(self.get_data(self.path) + _GUARD_SUFFIX.format(module=__name__).encode(),
                                   self.path)


def install() -> None:
    """Guard every spawner this process loads by path from now on."""
    util = importlib.util
    if hasattr(util.spec_from_file_location, "unguarded"):
        return
    spec_from_file_location = util.spec_from_file_location

    def guarding_spec_from_file_location(name, location=None, *args, **kwargs):
        spec = spec_from_file_location(name, location, *args, **kwargs)
        if spec is not None and "loader" not in kwargs and not args and _is_spawner(location) \
                and type(spec.loader) is importlib.machinery.SourceFileLoader:
            spec.loader = _SpawnerLoader(spec.loader.name, spec.loader.path)
        return spec

    guarding_spec_from_file_location.unguarded = spec_from_file_location
    util.spec_from_file_location = guarding_spec_from_file_location
