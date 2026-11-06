#!/usr/bin/env python3
"""Record every execution of the arc launcher made by a command and its descendants.

Why: a test run that reaches the real launcher in a fresh HOME makes it download a
release (up to tens of GB per sandbox). A PATH wrapper or preload only sees children
that keep the environment they were given, and tests build literal environments, so the
recorder lies below the environment: it enters a private user and mount namespace and
binds a recording script over the launcher path. Whatever PATH or HOME a descendant
holds, executing the launcher path runs the recorder, which appends one line
``pid TAB $PYTEST_CURRENT_TEST TAB arguments`` to the log and execs the real launcher.
The bind exists only inside this process's namespace (propagation private); the real
launcher file is never modified and other processes on the machine see nothing.

Usage:
    arc_launcher_recorder.py --log FILE [--launcher PATH] -- CMD...
    arc_launcher_recorder.py --selfcheck [--no-attach] [--launcher PATH] [--require-launcher]
    arc_launcher_recorder.py --canary-check [--require-launcher]

--selfcheck makes two deliberate launcher calls (by absolute path, and through PATH from
a scrubbed environment) and requires exactly those two records; with --no-attach nothing
is bound, so it must fail (the negative control). --canary-check runs the canary test
under the recorder from a scrubbed environment and requires a record carrying the
canary's own test id. Where the launcher or user namespaces are missing the checks print
a skip reason and exit 0, unless --require-launcher is given, when that is exit 1.
"""
from __future__ import annotations

import argparse
import ctypes
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_LAUNCHER = "/usr/bin/arc"
LITERAL_PATH = "/usr/bin:/bin"
CANARY = Path(__file__).resolve().parent / "canary_arc_launcher.py"
CANARY_TEST_ID = "canary_arc_launcher.py::test_launcher_run_downloads_nothing"

_MS_BIND, _MS_REC, _MS_PRIVATE, _MNT_DETACH = 4096, 16384, 1 << 18, 2


class AttachError(Exception):
    pass


def _mount(source: str, target: str, flags: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.mount(source.encode(), target.encode(), None, flags, None) != 0:
        err = ctypes.get_errno()
        raise AttachError(f"mount {source} -> {target}: {os.strerror(err)}")


def _enter_namespace() -> None:
    uid, gid = os.getuid(), os.getgid()
    try:
        os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNS)
    except (AttributeError, OSError) as exc:
        raise AttachError(f"user and mount namespaces unavailable: {exc}") from exc
    Path("/proc/self/setgroups").write_text("deny")
    Path("/proc/self/uid_map").write_text(f"{uid} {uid} 1")
    Path("/proc/self/gid_map").write_text(f"{gid} {gid} 1")
    _mount("none", "/", _MS_REC | _MS_PRIVATE)


def attach(launcher: str, log: Path, scratch: Path) -> None:
    """Bind a recording script over `launcher` in a private namespace of this process."""
    real = Path(launcher)
    if not real.is_file():
        raise AttachError(f"no launcher at {launcher}")
    _enter_namespace()
    preserved = scratch / "real-launcher"
    preserved.touch()
    _mount(str(real), str(preserved), _MS_BIND)
    recorder = scratch / "recorder.sh"
    recorder.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\t%s\\t%s\\n' \"$$\" \"${{PYTEST_CURRENT_TEST-}}\" \"$*\" >> {shlex.quote(str(log))}\n"
        f"exec {shlex.quote(str(preserved))} \"$@\"\n")
    recorder.chmod(0o755)
    _mount(str(recorder), str(real), _MS_BIND)


def read_records(log: Path) -> list[tuple[str, str, str]]:
    if not log.exists():
        return []
    rows = []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        pid, _, rest = line.partition("\t")
        test_id, _, args = rest.partition("\t")
        rows.append((pid, test_id, args))
    return rows


def run_wrapped(launcher: str, log: Path, cmd: list[str], attach_it: bool = True) -> int:
    """Run `cmd` with the launcher recorded; exit status is the command's."""
    with tempfile.TemporaryDirectory(prefix="arc-recorder-") as scratch:
        try:
            if attach_it:
                attach(launcher, log, Path(scratch))
            return subprocess.run(cmd).returncode
        finally:
            # A mountpoint cannot be unlinked while mounted; the namespace dies with us anyway.
            libc = ctypes.CDLL(None, use_errno=True)
            libc.umount2(str(Path(scratch) / "real-launcher").encode(), _MNT_DETACH)


def _scrubbed(home: str, **extra: str) -> list[str]:
    return ["env", "-i", f"PATH={LITERAL_PATH}", f"HOME={home}", *[f"{k}={v}" for k, v in extra.items()]]


def _wrapper_cmd(launcher: str, log: Path, inner: list[str], no_attach: bool = False) -> list[str]:
    cmd = [sys.executable, str(Path(__file__).resolve()), "--log", str(log), "--launcher", launcher]
    if no_attach:
        cmd.append("--no-attach")
    return [*cmd, "--", *inner]


def _skip_or_fail(reason: str, require: bool) -> int:
    if require:
        print(f"FAIL (--require-launcher): {reason}", file=sys.stderr)
        return 1
    print(f"SKIP: {reason}")
    return 0


def _can_attach(launcher: str) -> str:
    """Empty string when attaching works here, else the reason it does not."""
    if not Path(launcher).is_file():
        return f"no launcher at {launcher}"
    with tempfile.TemporaryDirectory(prefix="arc-recorder-probe-") as tmp:
        log = Path(tmp) / "log"
        proc = subprocess.run(
            _wrapper_cmd(launcher, log, ["true"]), capture_output=True, text=True)
        if proc.returncode != 0:
            return (proc.stderr.strip().splitlines() or ["attach failed"])[-1]
    return ""


def selfcheck(launcher: str, no_attach: bool, require: bool) -> int:
    reason = _can_attach(launcher)
    if reason:
        return _skip_or_fail(reason, require)
    name = Path(launcher).name
    with tempfile.TemporaryDirectory(prefix="arc-selfcheck-") as tmp:
        log = Path(tmp) / "records"
        home = Path(tmp) / "home"
        home.mkdir()
        path = f"{Path(launcher).parent}:{LITERAL_PATH}"
        inner = ["env", "-i", f"PATH={path}", f"HOME={home}", "sh", "-c",
                 f"{shlex.quote(launcher)} --is-launcher; {name} --is-launcher"]
        status = subprocess.run(_wrapper_cmd(launcher, log, inner, no_attach)).returncode
        records = read_records(log)
        ok = status == 0 and len(records) == 2 and all(r[2] == "--is-launcher" for r in records)
        print(f"selfcheck: inner exit {status}, {len(records)} record(s) (want 2)")
        return 0 if ok else 1


def canary_check(launcher: str, require: bool) -> int:
    reason = _can_attach(launcher)
    if reason:
        return _skip_or_fail(reason, require)
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="arc-canary-check-") as tmp:
        log = Path(tmp) / "records"
        home = Path(tmp) / "home"
        home.mkdir()
        inner = [*_scrubbed(str(home), ARC_CANARY_RUN="1"), sys.executable, "-m", "pytest",
                 "-q", "-p", "no:xdist", "-p", "no:cacheprovider", str(CANARY)]
        proc = subprocess.run(_wrapper_cmd(launcher, log, inner), cwd=root,
                              capture_output=True, text=True)
        records = read_records(log)
        carried = [r for r in records if CANARY_TEST_ID in r[1]]
        print(f"canary-check: pytest exit {proc.returncode}, {len(records)} record(s), "
              f"{len(carried)} carrying the canary id")
        if proc.returncode != 0 or not carried:
            print(proc.stdout[-1500:], file=sys.stderr)
        return 0 if proc.returncode == 0 and carried else 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    inner: list[str] = []
    if "--" in argv:
        cut = argv.index("--")
        argv, inner = argv[:cut], argv[cut + 1:]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log")
    ap.add_argument("--launcher", default=DEFAULT_LAUNCHER)
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--canary-check", action="store_true")
    ap.add_argument("--no-attach", action="store_true")
    ap.add_argument("--require-launcher", action="store_true")
    args = ap.parse_args(argv)
    if args.selfcheck:
        return selfcheck(args.launcher, args.no_attach, args.require_launcher)
    if args.canary_check:
        return canary_check(args.launcher, args.require_launcher)
    if not args.log or not inner:
        ap.error("--log and a command after -- are required")
    try:
        return run_wrapped(args.launcher, Path(args.log), inner, not args.no_attach)
    except AttachError as exc:
        print(f"arc_launcher_recorder: cannot attach: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
