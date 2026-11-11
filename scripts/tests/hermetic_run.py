#!/usr/bin/env python3
"""Run a command with chosen paths hidden and the network cut, in a private namespace.

Why: a test that reads the live repository, the real agent home or the network passes on the
machine that holds them and flakes under concurrency, because those move with other sessions'
work. Hiding them makes such a read fail at once, wherever it comes from. The runner enters a
user, mount and network namespace from inside this process (the command-line ``unshare`` is
refused here, an in-process one is not), mounts an empty read-only filesystem over every hidden
path, brings up loopback only and execs the command. A mount hides the directory itself, so the
path reads as empty however it is reached: through a symlink, by an environment variable, by a
relative path. The mounts exist only inside the command's own mount namespace; nothing on the
real machine changes and other processes see nothing.

Usage:
    hermetic_run.py [--hide PATH ...] [--objects SRC:DST ...] -- CMD...
    hermetic_run.py --check

--hide PATH     mount an empty read-only tmpfs (/dev/null for a file) over PATH and over its real
                path; a path that does not exist is skipped
--objects S:D   first bind S, read-only, onto the existing directory D, so a clone that reads
                its objects through alternates keeps working after S's parent is hidden
--check         enter the namespaces and exit 0, or print why they are unavailable and exit 2
Exit status is the command's; 2 when the namespaces cannot be entered.
"""
from __future__ import annotations

import argparse
import ctypes
import fcntl
import os
import socket
import struct
import sys
from pathlib import Path

_MS_RDONLY, _MS_REMOUNT, _MS_BIND, _MS_REC, _MS_PRIVATE = 1, 32, 4096, 16384, 1 << 18
_SIOCSIFFLAGS, _IFF_UP = 0x8914, 0x1


class NamespaceError(Exception):
    pass


def _mount(source: str, target: str, flags: int, fstype: str | None = None) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    kind = fstype.encode() if fstype else None
    if libc.mount(source.encode(), target.encode(), kind, flags, None) != 0:
        raise NamespaceError(f"mount {source} -> {target}: {os.strerror(ctypes.get_errno())}")


def _loopback_up() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            fcntl.ioctl(sock, _SIOCSIFFLAGS, struct.pack("16sH14s", b"lo", _IFF_UP, b""))
        except OSError as exc:
            raise NamespaceError(f"loopback up: {exc}") from exc


def enter_namespaces() -> None:
    """Enter a private user, mount and network namespace; mount propagation private, loopback up."""
    uid, gid = os.getuid(), os.getgid()
    try:
        os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNS | os.CLONE_NEWNET)
    except (AttributeError, OSError) as exc:
        raise NamespaceError(f"user, mount and network namespaces unavailable: {exc}") from exc
    try:
        Path("/proc/self/setgroups").write_text("deny")
        Path("/proc/self/uid_map").write_text(f"{uid} {uid} 1")
        Path("/proc/self/gid_map").write_text(f"{gid} {gid} 1")
    except OSError as exc:
        raise NamespaceError(f"user namespace maps: {exc}") from exc
    _mount("none", "/", _MS_REC | _MS_PRIVATE)
    _loopback_up()


def bind_readonly(source: str, target: str) -> None:
    if not Path(source).is_dir() or not Path(target).is_dir():
        raise NamespaceError(f"--objects needs two directories: {source} -> {target}")
    _mount(source, target, _MS_BIND)
    try:
        _mount("none", target, _MS_BIND | _MS_REMOUNT | _MS_RDONLY)
    except NamespaceError:
        pass


def hide(paths: list[str]) -> list[str]:
    """Mount an empty read-only tmpfs over each existing directory (/dev/null over a file), given and real path."""
    hidden: list[str] = []
    for given in paths:
        for candidate in dict.fromkeys((os.path.abspath(given), os.path.realpath(given))):
            if not os.path.lexists(candidate):
                continue
            real = os.path.realpath(candidate)
            if real in hidden:
                continue
            if os.path.isdir(real):
                _mount("none", real, _MS_RDONLY, "tmpfs")
            else:
                _mount("/dev/null", real, _MS_BIND)
            hidden.append(real)
    return hidden


def run(hidden: list[str], objects: list[str], cmd: list[str]) -> int:
    """Enter the namespaces, bind the objects, hide the paths and exec ``cmd``; never returns on success."""
    enter_namespaces()
    for pair in objects:
        source, _, target = pair.partition(":")
        bind_readonly(source, target)
    cwd = os.path.realpath(os.getcwd())
    for path in hide(hidden):
        if cwd == path or cwd.startswith(path + os.sep):
            os.chdir(path)  # the old working directory survives the mount; its path now names the empty one
    os.execvp(cmd[0], cmd)


def check() -> str:
    """Empty string when the namespaces can be entered here, else the reason they cannot."""
    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_end)
        try:
            enter_namespaces()
        except NamespaceError as exc:
            os.write(write_end, str(exc).encode())
            os._exit(2)
        os._exit(0)
    os.close(write_end)
    with os.fdopen(read_end, "rb") as pipe:
        reason = pipe.read().decode(errors="replace")
    _, status = os.waitpid(pid, 0)
    if os.waitstatus_to_exitcode(status) == 0:
        return ""
    return reason or "namespaces cannot be entered"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    inner: list[str] = []
    if "--" in argv:
        cut = argv.index("--")
        argv, inner = argv[:cut], argv[cut + 1:]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hide", action="append", default=[], metavar="PATH")
    ap.add_argument("--objects", action="append", default=[], metavar="SRC:DST")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    if args.check:
        reason = check()
        if reason:
            print(f"hermetic_run: {reason}", file=sys.stderr)
        return 2 if reason else 0
    if not inner:
        ap.error("a command after -- is required")
    try:
        return run(args.hide, args.objects, inner)
    except (NamespaceError, OSError) as exc:
        print(f"hermetic_run: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
