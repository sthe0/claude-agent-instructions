#!/usr/bin/env python3
"""Pre-crash snapshot of live claude sessions and FUSE mounts, and boot-aware snapshot choice.

Why: after a VM reboot nothing under <config>/sessions/ describes the sessions that lived
before it, and the first snapshot taken after the reboot is newer than the one we need. The
state that must survive is therefore copied to a durable local directory while the system is
healthy, and a restorer picks the copy by boot identity (``select_snapshot``), not by recency.

Usage:
    claude-recover.py snapshot [--force]       take a snapshot (writes only if it changed)
    claude-recover.py status [--check-fresh N] summary; with --check-fresh, exit 0 only if the
                                               last-ok stamp is <= N seconds old and the last
                                               snapshot's sessionId set equals the live one
    claude-recover.py select [--any-boot]      print the snapshot a restore would use

State: $CLAUDE_RECOVER_STATE_DIR (default ~/.local/state/claude-recover):
    snapshots/<boot_id>/<UTC ts>.json   one directory per boot, 50 newest kept per boot,
                                        plus the last snapshot of the three previous boots
    last-ok                             epoch seconds of the last successful snapshot run
                                        (refreshed even when the snapshot was unchanged)

Test seams (all optional): TMUX_BIN, PROC_ROOT, CLAUDE_RECOVER_STATE_DIR,
CLAUDE_RECOVER_CONFIG_DIRS (os.pathsep-separated), CLAUDE_RECOVER_NOW (epoch seconds),
CLAUDE_RECOVER_BOOT_ID, HOME.

Nothing is deleted outside the state dir. The snapshot carries no token or key: the process
environment is reduced to a whitelist and the command line to flags without the prompt.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SNAPSHOT_VERSION = 1
KEEP_PER_BOOT = 50
KEEP_PREV_BOOTS = 3
ENV_WHITELIST = ("CLAUDE_PERSONAL", "CLAUDE_CONFIG_DIR")
PANE_FIELDS = (
    "session_name",
    "window_id",
    "pane_id",
    "pane_pid",
    "window_name",
    "pane_current_path",
)
TMUX_FORMAT = "\t".join("#{%s}" % f for f in PANE_FIELDS)
# Flags whose value is worth keeping for a restart; every other value (prompt, settings JSON,
# system prompt) is dropped because it can be long or carry secrets.
KEPT_VALUE_FLAGS = frozenset(
    {"--resume", "-r", "--model", "--permission-mode", "--effort", "--add-dir", "--agent",
     "--session-id", "--name", "-n"}
)
_BOOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def proc_root() -> Path:
    return Path(os.environ.get("PROC_ROOT", "/proc"))


def state_dir() -> Path:
    override = os.environ.get("CLAUDE_RECOVER_STATE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".local" / "state" / "claude-recover"


def config_dirs() -> list[Path]:
    raw = os.environ.get("CLAUDE_RECOVER_CONFIG_DIRS")
    if raw is not None:
        return [Path(p) for p in raw.split(os.pathsep) if p]
    return [Path.home() / ".claude-agent", Path.home() / ".claude"]


def now() -> float:
    override = os.environ.get("CLAUDE_RECOVER_NOW")
    return float(override) if override else time.time()


def current_boot_id() -> str:
    override = os.environ.get("CLAUDE_RECOVER_BOOT_ID")
    if override:
        return override
    return (proc_root() / "sys/kernel/random/boot_id").read_text().strip()


def boot_time() -> int | None:
    try:
        for line in (proc_root() / "stat").read_text().splitlines():
            if line.startswith("btime "):
                return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return None


def iso_ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    with open(tmp, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# --- reading the live world -------------------------------------------------------------


def list_panes() -> list[dict]:
    tmux = os.environ.get("TMUX_BIN", "tmux")
    try:
        res = subprocess.run(
            [tmux, "list-panes", "-a", "-F", TMUX_FORMAT],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if res.returncode != 0:
        return []
    panes = []
    for line in res.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != len(PANE_FIELDS):
            continue
        pane = dict(zip(PANE_FIELDS, parts))
        try:
            pane["pane_pid"] = int(pane["pane_pid"])
        except ValueError:
            continue
        panes.append(pane)
    return panes


def read_environ(pid: int) -> dict[str, str]:
    try:
        raw = (proc_root() / str(pid) / "environ").read_bytes()
    except OSError:
        return {}
    env = {}
    for item in raw.split(b"\0"):
        key, sep, value = item.partition(b"=")
        if sep:
            env[key.decode(errors="replace")] = value.decode(errors="replace")
    return env


def read_argv(pid: int) -> list[str]:
    try:
        raw = (proc_root() / str(pid) / "cmdline").read_bytes()
    except OSError:
        return []
    return [a.decode(errors="replace") for a in raw.split(b"\0") if a]


def read_ppid(pid: int) -> int | None:
    """Parent pid of a live process, or None when the process is gone."""
    try:
        stat = (proc_root() / str(pid) / "stat").read_text()
    except OSError:
        return None
    # comm may contain spaces and parentheses; the fields after the last ')' are fixed.
    fields = stat.rsplit(")", 1)[-1].split()
    try:
        return int(fields[1])
    except (IndexError, ValueError):
        return None


def whitelist_env(environ: dict[str, str]) -> dict:
    out = {k: environ[k] for k in ENV_WHITELIST if k in environ}
    headers = environ.get("ANTHROPIC_CUSTOM_HEADERS")
    if headers:
        out["custom_header_names"] = [
            line.split(":", 1)[0].strip() for line in headers.splitlines() if ":" in line
        ]
    return out


def strip_prompt(argv: list[str]) -> list[str]:
    """Keep the executable and flags; keep a flag's value only for KEPT_VALUE_FLAGS."""
    kept = []
    skip_value_of = None
    for i, arg in enumerate(argv):
        if i == 0:
            kept.append(Path(arg).name)
            continue
        if skip_value_of is not None and not arg.startswith("-"):
            if skip_value_of == "keep":
                kept.append(arg)
            skip_value_of = None
            continue
        skip_value_of = None
        if arg.startswith("-"):
            name, eq, _value = arg.partition("=")
            if eq:
                if name in KEPT_VALUE_FLAGS:
                    kept.append(arg)
                else:
                    kept.append(name)
                continue
            kept.append(arg)
            skip_value_of = "keep" if arg in KEPT_VALUE_FLAGS else "drop"
            continue
        # a positional argument is the prompt
    return kept


def transcript_path(config_dir: Path, cwd: str, session_id: str) -> str:
    return str(config_dir / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd) / f"{session_id}.jsonl")


def load_session_files() -> list[dict]:
    records = []
    for cdir in config_dirs():
        for path in sorted((cdir / "sessions").glob("*.json")):
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            if not isinstance(data.get("pid"), int) or not data.get("sessionId"):
                continue
            data["_config_dir"] = str(cdir)
            records.append(data)
    return records


def _top_key(item: tuple[dict, dict]) -> tuple[int, int]:
    rec, pane = item
    return (0 if rec["pid"] == pane["pane_pid"] else 1, rec["pid"])


def collect_sessions(panes: list[dict], records: list[dict]) -> list[dict]:
    """The top claude of each tmux pane: pid == pane_pid or ppid == pane_pid, one per pane."""
    by_pane: dict[str, list[tuple[dict, dict]]] = {}
    for rec in records:
        pid = rec["pid"]
        ppid = read_ppid(pid)
        if ppid is None or "claude" not in " ".join(read_argv(pid)):
            continue
        for pane in panes:
            if pid == pane["pane_pid"] or ppid == pane["pane_pid"]:
                by_pane.setdefault(pane["pane_id"], []).append((rec, pane))
    sessions = []
    for items in by_pane.values():
        chosen = [min(items, key=_top_key)]
        for rec, pane in chosen:
            pid = rec["pid"]
            cwd = rec.get("cwd") or pane["pane_current_path"]
            cdir = Path(rec["_config_dir"])
            sessions.append({
                "sessionId": rec["sessionId"],
                "pid": pid,
                "cwd": cwd,
                "config_dir": str(cdir),
                "pane": pane,
                "env": whitelist_env(read_environ(pid)),
                "argv": strip_prompt(read_argv(pid)),
                "status": rec.get("status"),
                "waitingFor": rec.get("waitingFor"),
                "transcript": transcript_path(cdir, cwd, rec["sessionId"]),
            })
    sessions.sort(key=lambda s: (s["pane"]["session_name"], s["pane"]["window_id"], s["pane"]["pane_id"]))
    return sessions


def _unescape_mountinfo(field: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def fuse_mounts(home: str) -> list[dict]:
    try:
        text = (proc_root() / "self/mountinfo").read_text()
    except OSError:
        return []
    mounts = []
    for line in text.splitlines():
        left, sep, right = line.partition(" - ")
        if not sep:
            continue
        lparts = left.split()
        rparts = right.split()
        if len(lparts) < 5 or len(rparts) < 2:
            continue
        mountpoint = _unescape_mountinfo(lparts[4])
        fstype = rparts[0]
        if not fstype.startswith("fuse"):
            continue
        if mountpoint != home and not mountpoint.startswith(home.rstrip("/") + "/"):
            continue
        mounts.append({"mountpoint": mountpoint, "fstype": fstype, "source": _unescape_mountinfo(rparts[1])})
    mounts.sort(key=lambda m: m["mountpoint"])
    return mounts


def live_sessions() -> list[dict]:
    return collect_sessions(list_panes(), load_session_files())


def build_snapshot() -> dict:
    panes = list_panes()
    return {
        "version": SNAPSHOT_VERSION,
        "boot_id": current_boot_id(),
        "btime": boot_time(),
        "tmux_panes": sorted(panes, key=lambda p: (p["session_name"], p["window_id"], p["pane_id"])),
        "sessions": collect_sessions(panes, load_session_files()),
        "mounts": fuse_mounts(os.environ.get("HOME") or str(Path.home())),
    }


def content_hash(snapshot: dict) -> str:
    body = {k: v for k, v in snapshot.items() if k not in ("taken_at", "sha256")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# --- snapshot store ---------------------------------------------------------------------


def snapshots_root(state: Path) -> Path:
    return state / "snapshots"


def boot_snapshots(state: Path, boot_id: str) -> list[Path]:
    return sorted((snapshots_root(state) / boot_id).glob("*.json"), key=lambda p: p.name)


def select_snapshot(state_dir: Path, current_boot_id: str, any_boot: bool = False) -> Path | None:
    """Newest snapshot of a boot other than the current one; with any_boot, newest overall.

    An empty snapshot (a boot that recorded no sessions, e.g. between two quick reboots)
    loses to the newest snapshot that has sessions (not applied with any_boot); if none has,
    the newest one wins.
    """
    root = snapshots_root(Path(state_dir))
    if not root.is_dir():
        return None
    candidates: list[Path] = []
    for bdir in root.iterdir():
        if not bdir.is_dir() or (bdir.name == current_boot_id and not any_boot):
            continue
        candidates.extend(bdir.glob("*.json"))
    candidates.sort(key=lambda p: p.name)
    for snap in reversed(candidates):
        if any_boot or _has_sessions(snap):
            return snap
    return candidates[-1] if candidates else None


def _has_sessions(path: Path) -> bool:
    try:
        return bool(json.loads(path.read_text()).get("sessions"))
    except (OSError, ValueError, AttributeError):
        return False


def rotate(state: Path, boot_id: str) -> None:
    root = snapshots_root(state)
    for old in boot_snapshots(state, boot_id)[:-KEEP_PER_BOOT]:
        old.unlink()
    prev = []
    for bdir in root.iterdir():
        if bdir.is_dir() and bdir.name != boot_id:
            prev.append((max((p.name for p in bdir.glob("*.json")), default=""), bdir))
    prev.sort(key=lambda item: item[0])
    for _newest, bdir in prev[:-KEEP_PREV_BOOTS]:
        for snap in bdir.glob("*.json"):
            snap.unlink()
        _rmdir_quiet(bdir)
    for _newest, bdir in prev[-KEEP_PREV_BOOTS:]:
        files = sorted(bdir.glob("*.json"), key=lambda p: p.name)
        for stale in files[:-1]:
            stale.unlink()


def _rmdir_quiet(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


def touch_stamp(state: Path, ts: float) -> None:
    write_atomic(state / "last-ok", f"{ts:.0f}\n")


def read_stamp(state: Path) -> float | None:
    try:
        return float((state / "last-ok").read_text().strip())
    except (OSError, ValueError):
        return None


def _guard_not_fuse(state: Path) -> None:
    resolved = str(state.resolve())
    for mount in fuse_mounts("/"):
        mp = mount["mountpoint"].rstrip("/")
        if resolved == mp or resolved.startswith(mp + "/"):
            raise SystemExit(f"claude-recover: state dir {resolved} is under FUSE mount {mp}")


class _Lock:
    def __init__(self, state: Path):
        state.mkdir(parents=True, exist_ok=True)
        self._fh = open(state / ".lock", "w")

    def __enter__(self):
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        self._fh.close()


# --- commands ---------------------------------------------------------------------------


def cmd_snapshot(args: argparse.Namespace) -> int:
    sd = state_dir()
    boot_id = current_boot_id()
    if not _BOOT_ID_RE.match(boot_id):
        print(f"claude-recover: unsafe boot id {boot_id!r}", file=sys.stderr)
        return 2
    _guard_not_fuse(sd)
    ts = now()
    with _Lock(sd):
        snap = build_snapshot()
        snap["sha256"] = content_hash(snap)
        existing = boot_snapshots(sd, boot_id)
        previous_sha = None
        if existing:
            try:
                previous_sha = json.loads(existing[-1].read_text()).get("sha256")
            except (OSError, ValueError):
                previous_sha = None
        written = None
        if args.force or snap["sha256"] != previous_sha:
            snap["taken_at"] = ts
            name = iso_ts(ts)
            target = snapshots_root(sd) / boot_id / f"{name}.json"
            n = 1
            while target.exists():
                target = target.with_name(f"{name}-{n:02d}.json")
                n += 1
            write_atomic(target, json.dumps(snap, indent=2, sort_keys=True) + "\n")
            written = str(target)
            rotate(sd, boot_id)
        touch_stamp(sd, ts)
    print(json.dumps({"written": written, "sha256": snap["sha256"], "sessions": len(snap["sessions"])}))
    return 0


def _fail(msg: str) -> int:
    print(f"claude-recover: not fresh: {msg}", file=sys.stderr)
    return 1


def cmd_status(args: argparse.Namespace) -> int:
    sd = state_dir()
    boot_id = current_boot_id()
    stamp = read_stamp(sd)
    snaps = boot_snapshots(sd, boot_id)
    latest = json.loads(snaps[-1].read_text()) if snaps else None
    live_ids = {s["sessionId"] for s in live_sessions()}
    snap_ids = {s["sessionId"] for s in latest["sessions"]} if latest else set()
    if args.check_fresh is None:
        print(json.dumps({
            "boot_id": boot_id,
            "stamp_age_s": None if stamp is None else now() - stamp,
            "snapshots_this_boot": len(snaps),
            "snapshot_sessions": sorted(snap_ids),
            "live_sessions": sorted(live_ids),
        }))
        return 0
    if stamp is None:
        return _fail("no last-ok stamp")
    if now() - stamp > args.check_fresh:
        return _fail(f"last-ok stamp is {now() - stamp:.0f}s old (limit {args.check_fresh}s)")
    if latest is None:
        return _fail("no snapshot for this boot")
    if live_ids and not snap_ids:
        return _fail("snapshot is empty while sessions are live")
    if snap_ids != live_ids:
        return _fail(f"sessionId sets differ: snapshot-only {sorted(snap_ids - live_ids)}, live-only {sorted(live_ids - snap_ids)}")
    return 0


def cmd_select(args: argparse.Namespace) -> int:
    chosen = select_snapshot(state_dir(), current_boot_id(), any_boot=args.any_boot)
    if chosen is None:
        print("claude-recover: no snapshot to restore from", file=sys.stderr)
        return 1
    print(chosen)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="claude-recover", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_snap = sub.add_parser("snapshot", help="take a snapshot of live sessions and FUSE mounts")
    p_snap.add_argument("--force", action="store_true", help="write even if nothing changed")
    p_snap.set_defaults(func=cmd_snapshot)
    p_stat = sub.add_parser("status", help="show snapshot state; --check-fresh N gates on freshness")
    p_stat.add_argument("--check-fresh", type=int, metavar="N",
                        help="exit 0 only if the last-ok stamp is at most N seconds old and the "
                             "snapshot matches the live sessions")
    p_stat.set_defaults(func=cmd_status)
    p_sel = sub.add_parser("select", help="print the snapshot a restore would use")
    p_sel.add_argument("--any-boot", action="store_true", help="newest snapshot of any boot")
    p_sel.set_defaults(func=cmd_select)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
