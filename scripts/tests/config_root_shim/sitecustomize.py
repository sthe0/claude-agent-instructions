"""Test-only: spawn-specialist's config-root check answers "no refusal" in the suite
unless a test arms it.

`config_root_refusal()` refuses a spawn whose config root, agent home or `$HOME` is not
the login home's, and reads the login home from the password database so no env prefix
can move it. Spawn tests build a private world under a pytest tmp dir (`CLAUDE_CONFIG_DIR`
at `<tmp>/cfg`, often with the real `$HOME`) that no login home can match, so every spawn
in them would be refused. This shim guards the check in each spawner the suite loads:

- in-process: `conftest.py` calls `install()`, which wraps `SourceFileLoader.exec_module`
  so a `spawn-specialist.py` loaded by path gets the guarded check;
- subprocess: `conftest.py` prepends this directory to `PYTHONPATH`, so Python loads this
  file as `sitecustomize`; when the script is `spawn-specialist.py` it swaps the check in
  `__main__` the moment `main()` is entered.

The real check runs when `ARMED` is true (the `config_root_check_armed` fixture sets it
in-process) and in any spawner started without this directory on `PYTHONPATH`. Nothing
outside `scripts/tests/` imports this file; production keeps the unconditional check.
Any `sitecustomize` this one shadows is still run.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys

SHIM_DIR = os.path.dirname(os.path.abspath(__file__))
SPAWNER_FILE = "spawn-specialist.py"
CHECK_NAME = "config_root_refusal"
ARMED = False


def _guarded(check):
    def config_root_refusal():
        return check() if ARMED else None

    config_root_refusal.unguarded = check
    return config_root_refusal


def _guard(namespace: dict) -> None:
    check = namespace.get(CHECK_NAME)
    if check is not None and not hasattr(check, "unguarded"):
        namespace[CHECK_NAME] = _guarded(check)


def _is_spawner(path) -> bool:
    return os.path.basename(str(path or "")) == SPAWNER_FILE


def install():
    """Guard every spawner loaded from now on; return the shim module whose `ARMED`
    those guards read — the `sitecustomize` copy when the process loaded one first."""
    loader = importlib.machinery.SourceFileLoader
    if hasattr(loader.exec_module, "unguarded"):
        return loader.exec_module.shim
    exec_module = loader.exec_module

    def guarding_exec_module(self, module):
        exec_module(self, module)
        if _is_spawner(getattr(module, "__file__", None)):
            _guard(module.__dict__)

    guarding_exec_module.unguarded = exec_module
    guarding_exec_module.shim = sys.modules[__name__]
    loader.exec_module = guarding_exec_module
    return guarding_exec_module.shim


def _guard_main_on_entry() -> None:
    def profile(frame, event, arg):
        if event == "call" and frame.f_code.co_name == "main" and frame.f_globals.get("__name__") == "__main__":
            sys.setprofile(None)
            _guard(frame.f_globals)

    sys.setprofile(profile)


def _run_shadowed_sitecustomize() -> None:
    others = [p for p in sys.path if os.path.abspath(p or os.curdir) != SHIM_DIR]
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", others)
    if spec is None or spec.loader is None:
        return
    spec.loader.exec_module(importlib.util.module_from_spec(spec))


if __name__ == "sitecustomize":
    install()
    if sys.argv and _is_spawner(sys.argv[0]):
        _guard_main_on_entry()
    _run_shadowed_sitecustomize()
