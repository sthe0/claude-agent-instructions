"""Run the suite in parallel automatically when pytest-xdist is importable.

Wired from `conftest.py` by importing the two hooks below. Each veto clause in
`CLAUSES` returns True when it forbids auto-parallel; the run fans out only when none
does. The `worker` clause is the one that matters most: pytest-xdist resets `dist` and
`numprocesses` in every worker but leaves `config.args`, so a hook that looks only at
those options re-fires in each worker (12 -> 144 -> 1728 processes, issue #304). The
worker is recognised by `config.workerinput` and `PYTEST_XDIST_WORKER`, both set before
`pytest_configure` runs.

Opt out with `-n 0`, `-p no:xdist`, or `--dist no`; set the count with
`PYTEST_XDIST_AUTO_NUM_WORKERS`. Stdlib only.

Every parallel run, auto or explicit `-n`, first takes worker slots from the machine-wide
limiter (scripts/lib/test_slots.py) and runs with as many workers as it holds, so an explicit
`-n 12` is capped to `test-slots-per-run`. A run inside one that already holds slots (the
suite gate passes `CORE_TEST_SLOTS_HELD`; a nested pytest has a holding ancestor) takes none.
Serial runs take no slot.
"""
from __future__ import annotations

import importlib.util
import os
import shlex
import sys
from pathlib import Path

_MODE_FLAGS = (
    "collectonly",
    "usepdb",
    "trace",
    "stepwise",
    "stepwise_skip",
    "looponfail",
    "showfixtures",
    "show_fixtures_per_test",
    "setupplan",
    "setuponly",
    "markers",
)

_FANNED_OUT = "_xdist_auto_workers"


def _is_dist_flag(arg: str) -> bool:
    return arg in ("-d", "--dist") or arg.startswith("--dist=")


def _addopts_tokens() -> list[str]:
    raw = os.environ.get("PYTEST_ADDOPTS", "")
    try:
        return shlex.split(raw)
    except ValueError:
        return raw.split()


def _in_worker(config) -> bool:
    return hasattr(config, "workerinput") or bool(os.environ.get("PYTEST_XDIST_WORKER"))


def _xdist_absent(config) -> bool:
    return not config.pluginmanager.hasplugin("xdist")


def _explicit(config) -> bool:
    # The defaults matter: under `-p no:xdist` the xdist options are not registered and a
    # default-less getoption raises ValueError on every serial run. The argv scan catches
    # an explicit `--dist no`, otherwise indistinguishable from the default. An ini
    # `addopts` carrying `--dist no` is not seen (this repo has no pytest ini).
    return bool(
        config.getoption("numprocesses", None) is not None
        or config.getoption("dist", "no") != "no"
        or config.getoption("tx", None)
        or any(_is_dist_flag(a) for a in config.invocation_params.args)
        or any(_is_dist_flag(a) for a in _addopts_tokens())
    )


def _mode(config) -> bool:
    # `-s` too: xdist workers swallow live output, so a `-s` run stays serial.
    return any(config.getoption(name, False) for name in _MODE_FLAGS) or (
        config.getoption("capture", "fd") == "no"
    )


def _suite_shaped(config) -> bool:
    return any(os.path.isdir(str(a).split("::", 1)[0]) for a in config.args)


def _shape(config) -> bool:
    # Only a run that names a directory is suite-shaped; files and node ids stay serial.
    return not _suite_shaped(config)


CLAUSES = (
    ("worker", _in_worker),
    ("xdist_absent", _xdist_absent),
    ("explicit", _explicit),
    ("mode", _mode),
    ("shape", _shape),
)


def wants_auto_parallel(config, disabled=frozenset()) -> bool:
    unknown = set(disabled) - {name for name, _ in CLAUSES}
    if unknown:
        raise ValueError(f"unknown clause(s): {sorted(unknown)}")
    return not any(
        veto(config) for name, veto in CLAUSES if name not in disabled
    )


def _xdist_importable() -> bool:
    return importlib.util.find_spec("xdist") is not None


def _explicit_workers(config) -> int:
    """The worker count of an explicit `-n N` run (xdist has resolved `auto` by now), else 0."""
    if _in_worker(config) or _xdist_absent(config):
        return 0
    n = config.getoption("numprocesses", None)
    return n if isinstance(n, int) else 0


def _say(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def _take_slots(config, want: int) -> int:
    """Hold up to ``want`` worker slots for the session; returns how many workers may run."""
    scripts = str(Path(__file__).resolve().parents[1])
    if scripts not in sys.path:
        sys.path.append(scripts)
    from lib import test_slots

    limits = test_slots.configured()
    holder = test_slots.acquire(
        want, limits=limits, on_wait=lambda holders: _say(test_slots.describe_wait(want, limits, holders)))
    if holder.source == test_slots.ACQUIRED:
        os.environ[test_slots.HELD_ENV] = str(holder.count)

        def release():
            os.environ.pop(test_slots.HELD_ENV, None)
            holder.release()

        config.add_cleanup(release)
    return min(want, holder.count)


def pytest_configure(config, min_workers=2):
    if wants_auto_parallel(config):
        n = config.hook.pytest_xdist_auto_num_workers(config=config)
        cap = config.getoption("maxprocesses", None)
        if cap:
            n = min(n, cap)
        if n < min_workers:
            return
        n = _take_slots(config, n)
        if n < min_workers:
            return
        config.option.numprocesses = n
        config.option.dist = "load"
        config.option.tx = ["popen"] * n
        setattr(config, _FANNED_OUT, n)
        return
    explicit = _explicit_workers(config)
    if explicit >= min_workers:
        n = _take_slots(config, explicit)
        config.option.numprocesses = n
        config.option.tx = ["popen"] * n


def pytest_terminal_summary(terminalreporter, exitstatus, config, require_dsession=True):
    if _in_worker(config):
        return
    n = getattr(config, _FANNED_OUT, None)
    if n and (not require_dsession or config.pluginmanager.hasplugin("dsession")):
        terminalreporter.write_line(
            f"auto-parallel: {n} workers (PYTEST_XDIST_AUTO_NUM_WORKERS=<k> sets the count; "
            "-n 0 or -p no:xdist disables)"
        )
    elif n is None and _suite_shaped(config) and not _xdist_importable():
        terminalreporter.write_line(
            "tests ran serially: pip install pytest-xdist to run scripts/tests in parallel"
        )
