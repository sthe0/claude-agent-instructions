"""Helpers the E1 to E14 cases share: the verdict rule of a stopping side, the evidence checks
``check_record`` makes, the unobservable sub-observation marker and a logged launch of a custom
``opencode`` command line in a harness world."""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
from contextlib import contextmanager

from opencode_probe import harness, record

UNOBSERVABLE = "unobservable"
DB_REL = "xdg/data/opencode/opencode.db"


def db_rows(root, sql: str, args=()) -> list[dict] | None:
    """Rows of a read-only query on the isolated usage store; None when it is absent or unreadable."""
    path = root / DB_REL
    if not path.is_file():
        return None
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(sql, args)]
    except sqlite3.Error:
        return None
    finally:
        con.close()


@contextmanager
def captured_post_headers():
    """Collect the path and headers of every POST the stub receives while the block runs.

    The stub logs no headers of its own and runs in this process, so its handler class is wrapped
    for the duration and restored afterwards."""
    from opencode_probe import stub

    seen: list[dict] = []
    original = stub._make_handler

    def wrapped(state):
        base = original(state)

        class Handler(base):
            def do_POST(self):
                seen.append({"path": self.path, "headers": dict(self.headers)})
                return super().do_POST()

        return Handler

    stub._make_handler = wrapped
    try:
        yield seen
    finally:
        stub._make_handler = original


def unobservable(reason: str) -> dict:
    """A part nobody could observe: never ``pass``, only this marker inside a case's evidence."""
    return {"status": UNOBSERVABLE, "reason": reason}


def decide(stopping: bool, stopping_is_fail: bool) -> str:
    """The record status of an observation: ``fail`` on the stopping side while the case declares
    that side a fail, ``pass`` otherwise."""
    return record.FAIL if stopping and stopping_is_fail else record.PASS


def get_path(data, dotted: str):
    """The value at a dotted key path, None when any step is missing."""
    for key in dotted.split("."):
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


def check_entry(entry: dict, case_id: str, fields: tuple[str, ...], verdict=None,
                unobservable_parts: dict[str, str] | None = None) -> list[str]:
    """Why a case's record entry is not usable: no evidence, a required evidence field missing from a
    passing entry, a passing entry whose evidence is on the stopping side, or an unobservable part
    recorded as anything but the unobservable marker."""
    evidence = entry.get("evidence")
    if not evidence:
        return [f"{case_id} has no evidence"]
    problems = []
    for part, reason in (unobservable_parts or {}).items():
        if evidence.get(part) != unobservable(reason):
            problems.append(f"{case_id}.{part} must be the unobservable marker for {reason!r}, "
                            f"got {evidence.get(part)!r}")
    if entry.get("status") != record.PASS:
        return problems
    missing = [f"{case_id}.{name} is missing from the evidence" for name in fields if name not in evidence]
    problems += missing
    if verdict is not None and not missing and verdict(evidence) != record.PASS:
        problems.append(f"{case_id} is recorded pass but its evidence is on the stopping side")
    return problems


def parse_events(stdout: str) -> list[dict]:
    events = []
    for line in stdout.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            events.append({"raw": line[:500]})
    return events


def run_argv(world, *, auto: bool = True, extra: tuple[str, ...] = (), prompt: str | None = None,
             model: str = "stub/m1") -> list[str]:
    """``opencode run`` as the harness spells it; ``auto=False`` drops only ``--auto``."""
    base = harness.CLI_BASE if auto else tuple(a for a in harness.CLI_BASE if a != "--auto")
    return [world.binary, *base, "--model", model, *extra, *([prompt] if prompt else [])]


def launch_logged(h, world, argv: list[str], label: str, *, stdin: str | None = None,
                  timeout: int = 60) -> harness.Run:
    """One process of ``argv`` in the world's root with the world's isolated environment, killed as a
    group at ``timeout``; its meta, output, probe and stub lines go to the case's event log."""
    proc = subprocess.Popen(argv, env=world.env, cwd=world.root, text=True, start_new_session=True,
                            stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    timed_out = False
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        timed_out = True
    run = harness.Run(label, world.root, argv, world.env_paths(), proc.returncode, timed_out, out, err,
                      parse_events(out), world.probe(), world.requests())
    h._record(run)
    return run
