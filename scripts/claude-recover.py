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
    claude-recover.py [restore] [--dry-run] …  rebuild the pre-crash state (the default command)

State: $CLAUDE_RECOVER_STATE_DIR (default ~/.local/state/claude-recover):
    snapshots/<boot_id>/<UTC ts>.json   one directory per boot, 50 newest kept per boot,
                                        plus the last snapshot of the three previous boots
    last-ok                             epoch seconds of the last successful snapshot run
                                        (refreshed even when the snapshot was unchanged)
    recover.log                         restore log (rotated by size); last-plan.json is the plan
                                        of the latest real restore
    done-<boot_id>                      marker: `restore --auto` already ran in this boot
    pending-confirm.txt                 commands that need a human decision (written by --auto)

Restore runs one plan, built by build_plan() from the chosen snapshot, the live state and the
disk level, through five phases in order: disk-pre, mounts, compose, disk-post, sessions. The
first four call the machine-local hooks ~/.config/claude/recover.d/<NN>-*.sh; the last opens one
tmux window per session with `claude --resume`. A dry run prints the plan and calls the hooks
with RECOVER_DRY_RUN=1; nothing of Core's own is executed. Restore never starts a tmux server
and never touches the unit that owns it; it only adds windows to an existing tmux session.

Hook contract: env RECOVER_PHASE (disk-pre|mounts|compose|disk-post), RECOVER_DRY_RUN (0|1),
RECOVER_PLAN (JSON of the plan; the mounts hook mounts exactly the entries with
action=mount), RECOVER_LOG, RECOVER_DISK_LEVEL (ok|warn|hard). Exit 0 = done, 10 = nothing to
do, anything else = the phase failed. After the mounts phase every target is re-checked with
`mountpoint -q`; sessions under a target that is still unmounted are skipped.

Test seams (all optional): TMUX_BIN, DF_BIN, MOUNTPOINT_BIN, PROC_ROOT,
CLAUDE_RECOVER_STATE_DIR, CLAUDE_RECOVER_CONFIG_DIRS (os.pathsep-separated),
CLAUDE_RECOVER_NOW (epoch seconds), CLAUDE_RECOVER_BOOT_ID, CLAUDE_RECOVER_HOOKS_DIR,
CLAUDE_RECOVER_ESSENTIAL_FILE, CLAUDE_RECOVER_TMUX_SESSION, CLAUDE_RECOVER_MODE,
CLAUDE_RECOVER_CLAUDE_BIN, CLAUDE_RECOVER_SWEEP_BIN, CLAUDE_RECOVER_CONTINUE_PROMPT,
CLAUDE_RECOVER_{DEADLINE_S,TMUX_WAIT_S,SESSION_PAUSE_S,RETRY_PAUSE_S,MIN_MEM_GIB,
LOG_MAX_BYTES}, HOME.

Nothing is deleted outside the state dir except by the safe cleanup commands of the disk phases.
The snapshot carries no token or key: the process environment is reduced to a whitelist and
the command line to flags without the prompt.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
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
# Flags that take every following non-flag argument as a value.
VARIADIC_FLAGS = frozenset({"--add-dir"})
# Same-second collision suffix; '~' sorts after '.', so "<ts>~01.json" is newer than "<ts>.json".
COLLISION_SEP = "~"
_BOOT_ID_RE =re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


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
    try:
        return (proc_root() / "sys/kernel/random/boot_id").read_text().strip()
    except OSError as exc:
        raise SystemExit(f"claude-recover: cannot read boot id: {exc}")


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


def mkdir_private(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def write_atomic(path: Path, text: str) -> None:
    mkdir_private(path.parent)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def remove_stale_tmp(state: Path) -> None:
    """Leftovers of a writer killed between create and rename; callers hold the lock."""
    for stale in [*state.glob(".*.tmp.*"), *(state / "snapshots").glob("*/.*.tmp.*")]:
        if _tmp_in_use(stale):
            continue
        stale.unlink(missing_ok=True)


STALE_TMP_AGE_S = 300


def _tmp_in_use(path: Path) -> bool:
    """A temp file whose pid suffix is a live process and that is younger than STALE_TMP_AGE_S.

    A concurrent restore writes its plan/marker with the same temp-name scheme and holds no
    snapshot lock, so a snapshot run must not delete a file that writer is about to rename.
    The age bound covers a pid that was reused after the writer died.
    """
    try:
        pid = int(path.name.rsplit(".", 1)[1])
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except (ValueError, IndexError, OverflowError):
        return False
    try:
        return time.time() - path.stat().st_mtime <= STALE_TMP_AGE_S
    except OSError:
        return False


# --- reading the live world -------------------------------------------------------------


class TmuxError(RuntimeError):
    """tmux could not be asked (binary missing, timeout, no server) — not the same as no panes."""


def list_panes() -> list[dict]:
    tmux = os.environ.get("TMUX_BIN", "tmux")
    try:
        res = subprocess.run(
            [tmux, "list-panes", "-a", "-F", TMUX_FORMAT],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TmuxError(f"{tmux} list-panes failed: {exc}")
    if res.returncode != 0:
        raise TmuxError(f"{tmux} list-panes exited {res.returncode}: {res.stderr.strip()[:200]}")
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


def _looks_like_path(arg: str) -> bool:
    """A value of a variadic flag (--add-dir a b) vs the trailing positional prompt."""
    return arg.startswith(("/", "~", ".")) and not any(c.isspace() for c in arg)


def strip_prompt(argv: list[str]) -> list[str]:
    """Keep the executable and flags; keep a flag's value only for KEPT_VALUE_FLAGS."""
    kept = []
    skip_value_of = None
    for i, arg in enumerate(argv):
        if i == 0:
            kept.append(Path(arg).name)
            continue
        if skip_value_of is not None and not arg.startswith("-"):
            if skip_value_of == "keep" or (skip_value_of == "keep-many" and _looks_like_path(arg)):
                kept.append(arg)
            if skip_value_of != "keep-many":
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
            if arg in VARIADIC_FLAGS:
                skip_value_of = "keep-many"
            else:
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


def read_snapshot(path: Path) -> dict:
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
        raise ValueError("not a snapshot")
    return data


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
        keep = set(files[-1:])
        newest_nonempty = next((f for f in reversed(files) if _has_sessions(f)), None)
        if newest_nonempty is not None:
            keep.add(newest_nonempty)
        for stale in files:
            if stale not in keep:
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
        mkdir_private(state)
        self._fh = open(state / ".lock", "w")

    def __enter__(self):
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        self._fh.close()


# --- restore: plan ----------------------------------------------------------------------

SUBCOMMANDS = ("snapshot", "status", "select", "restore")
PHASES = ("disk-pre", "mounts", "compose", "disk-post", "sessions")
# The kernel's overall budget: up to 12 mounts x 2 min + 15 min cleanup + 15 sessions x 20 s +
# 2 min tmux wait is about 46 min; the systemd unit's RuntimeMaxSec must stay above this.
DEADLINE_S = 45 * 60
MOUNT_TIMEOUT_S = 120
TMUX_WAIT_S = 120
CLEANUP_CAP_S = 15 * 60
COMPOSE_CAP_S = 10 * 60
SESSION_PAUSE_S = 20
HOOK_RETRIES = 3
HOOK_RETRY_PAUSE_S = 30
HOOK_EXIT_NOTHING = 10
MIN_MEM_GIB = 8.0
LOG_MAX_BYTES = 1 << 20
DISK_HARD_GIB = 10
DISK_WARN_GIB = 50
DISK_WARN_PCT = 15
DEFAULT_TMUX_SESSION = "ccgram"
DEFAULT_CONTINUE_PROMPT = (
    "The VM was rebooted and the mounts were restored; check the state (agentctl status) "
    "and continue."
)
OPEN_ACTIONS = ("resume", "reopen")
# Commands that need a human decision; recover prints them and never runs them.
CONFIRM_COMMANDS = (
    ("arc gc", "object-cache GC: preview with `arc gc --dry-run`, never in parallel on a shared store"),
    ("arc unmount --forget <mount>", "irreversible: deletes the mount's registered storage"),
    ("docker system prune -a", "removes every unused image, container and network"),
    ("docker image prune -a", "removes every image not used by a container"),
)


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        return float(raw) if raw else float(default)
    except ValueError:
        return float(default)


def tmux_bin() -> str:
    return os.environ.get("TMUX_BIN", "tmux")


def home_dir() -> str:
    return os.environ.get("HOME") or str(Path.home())


def hooks_dir() -> Path:
    override = os.environ.get("CLAUDE_RECOVER_HOOKS_DIR")
    return Path(override) if override else Path(home_dir()) / ".config" / "claude" / "recover.d"


def list_hooks(directory: Path) -> list[Path]:
    try:
        return sorted(p for p in directory.glob("*.sh") if p.is_file())
    except OSError:
        return []


def read_essential(path: Path) -> list[str]:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    return [ln.strip().rstrip("/") for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def disk_level(free_gib: float, free_pct: float) -> str:
    if free_gib < DISK_HARD_GIB:
        return "hard"
    if free_gib < DISK_WARN_GIB or free_pct < DISK_WARN_PCT:
        return "warn"
    return "ok"


def measure_disk() -> dict:
    path = home_dir()
    used = avail = None
    try:
        res = subprocess.run([os.environ.get("DF_BIN", "df"), "-Pk", path],
                             capture_output=True, text=True, timeout=30)
        if res.returncode == 0:
            row = res.stdout.strip().splitlines()[-1].split()
            used, avail = int(row[2]), int(row[3])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        pass
    if avail is None:
        usage = shutil.disk_usage(path)
        used, avail = usage.used // 1024, usage.free // 1024
    total = used + avail
    free_gib = avail / 1024 / 1024
    free_pct = 100.0 * avail / total if total else 0.0
    return {"free_gib": round(free_gib, 2), "free_pct": round(free_pct, 1),
            "level": disk_level(free_gib, free_pct)}


def disk_actions(level: str) -> list[dict]:
    """The cleanup menu for a level below ok, by class; ya/arc commands run inside the hooks
    (they need the mounted anchor), the rest is run by recover itself."""
    if level == "ok":
        return []
    sweep = os.environ.get("CLAUDE_RECOVER_SWEEP_BIN") or str(Path(home_dir()) / "bin" / "agent-hygiene-sweep.sh")
    rows = [
        ("disk-pre", "run", "core", ["docker", "image", "prune", "-f"]),
        ("disk-pre", "run", "core", ["docker", "builder", "prune", "-f"]),
        ("disk-pre", "run", "core", [sweep]),
        ("disk-post", "run", "hook", ["ya", "gc", "cache"]),
        ("disk-post", "report", "hook", ["arc", "gc", "--dry-run"]),
        ("disk-post", "report", "hook", ["arc-mounts-gc.sh", "--dry-run"]),
    ]
    actions = [{"cmd": shlex.join(argv), "class": klass, "status": "planned", "phase": phase, "owner": owner}
               for phase, klass, owner, argv in rows]
    actions += [{"cmd": cmd, "class": "confirm", "status": "planned", "phase": "disk-post", "owner": "core"}
                for cmd, _why in CONFIRM_COMMANDS]
    return actions


def _under(path: str, root: str) -> bool:
    root = root.rstrip("/")
    return path == root or path.startswith(root + "/")


def decide_mounts(snap_mounts: list[dict], mounted: set[str], level: str, essential: list[str]) -> list[dict]:
    out = []
    for m in snap_mounts:
        target = m["mountpoint"]
        is_essential = target.rstrip("/") in essential
        if target in mounted:
            action = "skip-mounted"
        elif level == "hard" and not is_essential:
            action = "skip-hard-disk"
        else:
            action = "mount"
        out.append({"target": target, "essential": is_essential, "action": action,
                    "status": "planned" if action == "mount" else "skipped"})
    rank = {t: i for i, t in enumerate(essential)}
    out.sort(key=lambda e: (rank.get(e["target"].rstrip("/"), len(rank)), e["target"]))
    return out


def session_command(sess: dict, name: str, prompt: str | None, config: dict) -> list[str]:
    env = sess.get("env") or {}
    assigns = [f"{k}={shlex.quote(env[k])}" for k in ENV_WHITELIST if k in env]
    claude = [config["claude_bin"], "--resume", sess["sessionId"]] + ([prompt] if prompt else [])
    inner = " ".join(["env", *assigns, *(shlex.quote(a) for a in claude)])
    shell = f"{inner}; echo EXITED; sleep 3600"
    return [config["tmux_bin"], "new-window", "-d", "-t", f"{config['tmux_session']}:",
            "-n", name, "-c", sess.get("cwd") or home_dir(), shell]


def decide_sessions(snap_sessions: list[dict], alive: set[str], mounts: list[dict],
                    usable: set[str], config: dict) -> list[dict]:
    targets = [m["target"] for m in mounts]
    out = []
    for sess in snap_sessions:
        sid = sess["sessionId"]
        cwd = sess.get("cwd") or ""
        name = (sess.get("pane") or {}).get("window_name") or sid[:8]
        prompt = None
        detail = ""
        transcript = sess.get("transcript")
        if sid in alive:
            action, detail = "skip-alive", "the session is running"
        elif transcript and not config["transcript_exists"](transcript):
            action, detail = "skip-no-transcript", f"no transcript at {transcript}"
        elif any(_under(cwd, t) and t not in usable for t in targets):
            action, detail = "skip-unmounted", "its mount is not available"
        else:
            if config["mode"] == "continue" and sess.get("status") == "busy":
                prompt = config["continue_prompt"]
            action = "resume" if prompt else "reopen"
        entry = {"session_id": sid, "cwd": cwd, "name": name, "action": action,
                 "command": session_command(sess, name, prompt, config) if action in OPEN_ACTIONS else [],
                 "status": "planned" if action in OPEN_ACTIONS else "skipped",
                 "with_prompt": prompt is not None}
        if detail:
            entry["detail"] = detail
        out.append(entry)
    return out


def build_plan(snapshot: dict, live: dict, disk: dict, config: dict) -> dict:
    mounts = decide_mounts(snapshot.get("mounts", []), live["mounted"], disk["level"], config["essential"])
    usable = {m["target"] for m in mounts if m["action"] in ("mount", "skip-mounted")}
    sessions = decide_sessions(snapshot.get("sessions", []), live["alive"], mounts, usable, config)
    warnings = list(config["warnings"])
    foreign = sorted({(s.get("pane") or {}).get("session_name") for s in snapshot.get("sessions", [])}
                     - {None, config["tmux_session"]})
    if foreign:
        warnings.append(f"snapshot sessions lived in tmux session(s) {foreign}; "
                        f"they are reopened in {config['tmux_session']!r}")
    return {
        "dry_run": config["dry_run"],
        "snapshot": config["snapshot_path"],
        "snapshot_boot_id": snapshot.get("boot_id"),
        "current_boot_id": config["current_boot_id"],
        "mode": config["mode"],
        "simulate_reboot": config["simulate_reboot"],
        "tmux_session": config["tmux_session"],
        "phases": list(PHASES),
        "disk": {**disk, "initial_level": disk["level"], "actions": disk_actions(disk["level"])},
        "mounts": mounts,
        "sessions": sessions,
        "hooks": config["hooks"],
        "hook_runs": [],
        "warnings": warnings,
        "summary": {},
    }


# --- restore: live state ----------------------------------------------------------------


def is_mounted(path: str) -> bool:
    try:
        res = subprocess.run([os.environ.get("MOUNTPOINT_BIN", "mountpoint"), "-q", path],
                             capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0


def mounted_set(targets: list[str]) -> set[str]:
    return {t for t in targets if is_mounted(t)}


def alive_session_ids(wanted: set[str]) -> set[str]:
    """Session ids that are running now: a live claude in a registry file, or the id on the
    command line of a tmux pane's process or its direct child."""
    alive: set[str] = set()
    btime = boot_time()
    for rec in load_session_files():
        started = rec.get("startedAt")
        if isinstance(started, (int, float)) and btime and started / 1000 < btime - 5:
            continue
        pid = rec["pid"]
        if read_ppid(pid) is not None and "claude" in " ".join(read_argv(pid)):
            alive.add(rec["sessionId"])
    try:
        pane_pids = {p["pane_pid"] for p in list_panes()}
    except TmuxError:
        pane_pids = set()
    if pane_pids:
        try:
            pids = [int(p.name) for p in proc_root().iterdir() if p.name.isdigit()]
        except OSError:
            pids = []
        for pid in pids:
            if pid in pane_pids or read_ppid(pid) in pane_pids:
                cmdline = " ".join(read_argv(pid))
                alive.update(sid for sid in wanted if sid in cmdline)
    return alive


def mem_available_gib() -> float | None:
    try:
        for line in (proc_root() / "meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024 / 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def tmux_run(argv: list[str], timeout: float = 30) -> tuple[int, str]:
    try:
        res = subprocess.run([tmux_bin(), *argv], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    return res.returncode, (res.stderr or res.stdout).strip()


def wait_for_tmux_session(target: str, wait_s: float) -> bool:
    """Wait for an existing tmux session; never creates one or starts a server."""
    end = time.monotonic() + wait_s
    while True:
        if tmux_run(["has-session", "-t", target])[0] == 0:
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(min(2.0, max(0.05, end - time.monotonic())))


# --- restore: execution -----------------------------------------------------------------


class Deadline:
    def __init__(self, seconds: float):
        self._end = time.monotonic() + seconds

    def remaining(self) -> float:
        return max(0.0, self._end - time.monotonic())

    def expired(self) -> bool:
        return self.remaining() <= 0


class RunLog:
    """recover.log (rotated by size) plus stderr; a dry run logs nowhere but stderr stays quiet."""

    def __init__(self, path: Path | None, auto: bool):
        self.path = path
        self.auto = auto
        if path is not None:
            mkdir_private(path.parent)
            limit = int(env_float("CLAUDE_RECOVER_LOG_MAX_BYTES", LOG_MAX_BYTES))
            try:
                if path.stat().st_size > limit:
                    os.replace(path, path.with_name(path.name + ".1"))
            except OSError:
                pass

    def __call__(self, message: str, level: str = "INFO") -> None:
        if self.path is None:
            return
        line = f"{iso_ts(now())} {level} {message}"
        with open(self.path, "a") as fh:
            fh.write(line + "\n")
        prefix = "<4>" if self.auto and level == "WARNING" else ""
        print(prefix + line, file=sys.stderr)


def tail_text(text: str, limit: int = 2000) -> str:
    return text if len(text) <= limit else "…" + text[-limit:]


class Restorer:
    def __init__(self, plan: dict, snapshot: dict, config: dict, args: argparse.Namespace,
                 log: RunLog, plan_path: Path, hook_log: Path, hook_files: list[Path]):
        self.plan = plan
        self.snapshot = snapshot
        self.config = config
        self.auto = args.auto
        self.dry_run = args.dry_run
        self.log = log
        self.plan_path = plan_path
        self.hook_log = hook_log
        self.hook_files = hook_files
        self.deadline = Deadline(env_float("CLAUDE_RECOVER_DEADLINE_S", DEADLINE_S))
        self.cleanup_spent = 0.0

    # -- helpers
    def cleanup_left(self) -> float:
        return max(0.0, CLEANUP_CAP_S - self.cleanup_spent)

    def write_plan(self) -> None:
        write_atomic(self.plan_path, json.dumps(self.plan, indent=2) + "\n")

    def live_mounted(self) -> set[str]:
        return mounted_set([m["mountpoint"] for m in self.snapshot.get("mounts", [])])

    def redecide(self, mounted: set[str], mounts_phase_over: bool = False) -> None:
        """Recompute mount and session decisions from the current disk level and live state;
        once the mounts phase is over, only what is really mounted counts as usable."""
        level = self.plan["disk"]["level"]
        mounts = decide_mounts(self.snapshot.get("mounts", []), mounted, level, self.config["essential"])
        usable_actions = ("skip-mounted",) if mounts_phase_over else ("mount", "skip-mounted")
        usable = {m["target"] for m in mounts if m["action"] in usable_actions}
        self.plan["mounts"] = mounts
        self.plan["sessions"] = decide_sessions(self.snapshot.get("sessions", []), self.alive(),
                                                mounts, usable, self.config)

    def alive(self) -> set[str]:
        return alive_session_ids({s["sessionId"] for s in self.snapshot.get("sessions", [])})

    # -- hooks
    def run_hooks(self, phase: str, cap_s: float, disk_phase: bool = False) -> list[int | str]:
        self.write_plan()
        exits: list[int | str] = []
        for hook in self.hook_files:
            cap = min(self.deadline.remaining(), cap_s, self.cleanup_left() if disk_phase else cap_s)
            env = dict(os.environ,
                       RECOVER_PHASE=phase, RECOVER_DRY_RUN="1" if self.dry_run else "0",
                       RECOVER_PLAN=str(self.plan_path), RECOVER_LOG=str(self.hook_log),
                       RECOVER_DISK_LEVEL=self.plan["disk"]["level"])
            started = time.monotonic()
            if cap <= 0:
                rc, out = "skipped", "no time left"
            else:
                try:
                    res = subprocess.run(["bash", str(hook)], env=env, cwd=tempfile.gettempdir(),
                                         capture_output=True, text=True, timeout=cap)
                    rc, out = res.returncode, (res.stdout + res.stderr)
                except subprocess.TimeoutExpired as exc:
                    partial = exc.stdout or ""
                    rc, out = "timeout", (partial.decode(errors="replace") if isinstance(partial, bytes) else partial)
                except OSError as exc:
                    rc, out = "error", str(exc)
            if disk_phase:
                self.cleanup_spent += time.monotonic() - started
            exits.append(rc)
            self.plan["hook_runs"].append({"phase": phase, "hook": hook.name, "exit": rc,
                                           "output": tail_text(out)})
            self.log(f"hook {hook.name} phase={phase} exit={rc}",
                     "INFO" if rc in (0, HOOK_EXIT_NOTHING) else "WARNING")
            for line in out.splitlines()[-40:]:
                self.log(f"  {hook.name}: {line}")
        return exits

    @staticmethod
    def hooks_ok(exits: list[int | str]) -> bool:
        return all(rc in (0, HOOK_EXIT_NOTHING) for rc in exits)

    # -- phases
    def phase_disk(self, phase: str) -> None:
        for act in self.plan["disk"]["actions"]:
            if act["phase"] != phase:
                continue
            if act["class"] == "confirm":
                if not self.dry_run:
                    act["status"], act["detail"] = "skipped", "needs your decision; recover never runs it"
            elif act["owner"] == "core" and not self.dry_run:
                self.run_core_action(act)
        exits = self.run_hooks(phase, CLEANUP_CAP_S, disk_phase=True)
        for act in self.plan["disk"]["actions"]:
            if act["phase"] == phase and act["owner"] == "hook" and not self.dry_run:
                if not self.hook_files:
                    act["status"], act["detail"] = "skipped", "no recover.d hook installed"
                elif not self.hooks_ok(exits):
                    act["status"] = "failed"
                elif all(rc == HOOK_EXIT_NOTHING for rc in exits):
                    act["status"], act["detail"] = "skipped", "hooks had nothing to do"
                else:
                    act["status"] = "done"
        if not self.dry_run:
            self.plan["disk"].update(measure_disk())
            self.log(f"disk after {phase}: {self.plan['disk']['free_gib']} GiB free, "
                     f"level={self.plan['disk']['level']}")
            if phase == "disk-pre":
                self.redecide(self.live_mounted())

    def run_core_action(self, act: dict) -> None:
        argv = shlex.split(act["cmd"])
        cap = min(self.deadline.remaining(), self.cleanup_left())
        if not shutil.which(argv[0]):
            act["status"], act["detail"] = "skipped", f"{argv[0]} not found"
        elif cap <= 0:
            act["status"], act["detail"] = "skipped", "cleanup time cap or deadline reached"
        else:
            started = time.monotonic()
            try:
                res = subprocess.run(argv, capture_output=True, text=True, timeout=cap)
                act["status"] = "done" if res.returncode == 0 else "failed"
                if res.returncode:
                    act["detail"] = f"exit {res.returncode}"
            except subprocess.TimeoutExpired:
                act["status"], act["detail"] = "failed", "timeout"
            except OSError as exc:
                act["status"], act["detail"] = "failed", str(exc)
            self.cleanup_spent += time.monotonic() - started
        self.log(f"disk action {act['cmd']}: {act['status']}", "INFO" if act["status"] != "failed" else "WARNING")

    def phase_mounts(self) -> None:
        pending = sum(1 for m in self.plan["mounts"] if m["action"] == "mount")
        cap = MOUNT_TIMEOUT_S * max(1, pending) + 60
        if self.dry_run:
            self.run_hooks("mounts", cap)
            return
        done_by_us: set[str] = set()
        attempts = 1 + (HOOK_RETRIES if self.auto else 0)
        for attempt in range(attempts):
            before = self.live_mounted()
            self.redecide(before)
            todo = {m["target"] for m in self.plan["mounts"] if m["action"] == "mount"}
            exits = self.run_hooks("mounts", cap)
            after = self.live_mounted()
            done_by_us |= todo & after
            if self.hooks_ok(exits) or attempt == attempts - 1 or self.deadline.expired():
                break
            self.log(f"mounts phase failed (attempt {attempt + 1}/{attempts}); retrying", "WARNING")
            time.sleep(min(env_float("CLAUDE_RECOVER_RETRY_PAUSE_S", HOOK_RETRY_PAUSE_S),
                           self.deadline.remaining()))
        self.redecide(self.live_mounted(), mounts_phase_over=True)
        for entry in self.plan["mounts"]:
            if entry["target"] in done_by_us:
                entry["action"], entry["status"] = "mount", "done"
            elif entry["action"] == "mount":
                entry["status"] = "failed"
            self.log(f"mount {entry['target']}: {entry['action']} -> {entry['status']}",
                     "WARNING" if entry["status"] == "failed" else "INFO")

    def phase_sessions(self) -> None:
        pending = [s for s in self.plan["sessions"] if s["action"] in OPEN_ACTIONS]
        if self.dry_run or not pending:
            return
        target = self.config["tmux_session"]
        if not wait_for_tmux_session(target, env_float("CLAUDE_RECOVER_TMUX_WAIT_S", TMUX_WAIT_S)):
            self.log(f"tmux session {target!r} did not appear; sessions are not reopened", "WARNING")
            for sess in pending:
                sess["status"], sess["detail"] = "skipped", f"tmux session {target!r} not found"
            return
        pause = env_float("CLAUDE_RECOVER_SESSION_PAUSE_S", SESSION_PAUSE_S)
        min_mem = env_float("CLAUDE_RECOVER_MIN_MEM_GIB", MIN_MEM_GIB)
        for i, sess in enumerate(pending):
            if self.deadline.expired():
                sess["status"], sess["detail"] = "skipped", "deadline reached"
            elif sess["session_id"] in self.alive():
                sess["action"], sess["status"], sess["command"] = "skip-alive", "skipped", []
                sess["detail"] = "the session appeared while recovering"
            elif (mem := mem_available_gib()) is not None and mem < min_mem:
                sess["status"], sess["detail"] = "skipped", f"MemAvailable {mem:.1f} GiB < {min_mem:g}"
            else:
                rc, msg = tmux_run(sess["command"][1:])
                sess["status"] = "done" if rc == 0 else "failed"
                if rc:
                    sess["detail"] = tail_text(msg, 300)
                if i + 1 < len(pending):
                    time.sleep(min(pause, self.deadline.remaining()))
            self.log(f"session {sess['session_id']} ({sess['name']}) {sess['cwd']}: "
                     f"{sess['action']} -> {sess['status']}"
                     + (f" ({sess['detail']})" if sess.get("detail") else ""),
                     "WARNING" if sess["status"] == "failed" else "INFO")

    def run(self) -> None:
        for phase in PHASES:
            if self.deadline.expired():
                break
            if phase in ("disk-pre", "disk-post"):
                self.phase_disk(phase)
            elif phase == "mounts":
                self.phase_mounts()
            elif phase == "compose":
                self.run_hooks(phase, COMPOSE_CAP_S)
            else:
                self.phase_sessions()
        if not self.dry_run:
            self.expire_remaining()

    def expire_remaining(self) -> None:
        for act in self.plan["disk"]["actions"]:
            if act["status"] == "planned":
                act["status"], act["detail"] = "skipped", "deadline reached"
        for entry in [*self.plan["mounts"], *self.plan["sessions"]]:
            if entry["status"] == "planned":
                entry["status"], entry["detail"] = "skipped", "deadline reached"


def summarize(plan: dict, elapsed_s: float) -> dict:
    statuses = Counter(x["status"] for x in [*plan["disk"]["actions"], *plan["mounts"], *plan["sessions"]])
    return {
        "mounts": dict(Counter(m["action"] for m in plan["mounts"])),
        "sessions": dict(Counter(s["action"] for s in plan["sessions"])),
        "statuses": dict(statuses),
        "failed": statuses.get("failed", 0),
        "elapsed_s": round(elapsed_s, 1),
    }


def confirm_block(plan: dict) -> str:
    disk = plan["disk"]
    lines = [f"Needs your decision: disk level is {disk['level']} "
             f"({disk['free_gib']:.1f} GiB free, {disk['free_pct']:.1f}%).",
             "Recover does not run these; run them yourself if you agree:"]
    lines += [f"  {cmd}    # {why}" for cmd, why in CONFIRM_COMMANDS]
    return "\n".join(lines)


def render_text(plan: dict) -> str:
    out = [f"claude-recover {'dry-run' if plan['dry_run'] else 'restore'} "
           f"(mode={plan['mode']}{', simulated reboot' if plan['simulate_reboot'] else ''})",
           f"snapshot: {plan['snapshot']} (boot {plan['snapshot_boot_id']}; now {plan['current_boot_id']})"]
    d = plan["disk"]
    runs = plan["hook_runs"]

    def hook_lines(phase):
        for r in runs:
            if r["phase"] == phase:
                out.append(f"    hook {r['hook']} exit={r['exit']}")
                out.extend(f"      {ln}" for ln in r["output"].splitlines()[:30])

    def actions(phase):
        for a in d["actions"]:
            if a["phase"] == phase:
                note = f" ({a['detail']})" if a.get("detail") else ""
                out.append(f"  [{a['class']}] {a['cmd']}: {a['status']}{note}")

    out.append(f"phase disk-pre: {d['free_gib']:.1f} GiB free ({d['free_pct']:.1f}%), "
               f"level={d['initial_level']}")
    actions("disk-pre")
    hook_lines("disk-pre")
    out.append("phase mounts:")
    for m in plan["mounts"]:
        out.append(f"  {m['action']:<15} {m['target']}{' [essential]' if m['essential'] else ''}: {m['status']}")
    hook_lines("mounts")
    out.append("phase compose:")
    hook_lines("compose")
    out.append(f"phase disk-post: level={d['level']}")
    actions("disk-post")
    hook_lines("disk-post")
    out.append("phase sessions:")
    for s in plan["sessions"]:
        note = f" ({s['detail']})" if s.get("detail") else ""
        out.append(f"  {s['action']:<18} {s['name']} {s['session_id']} {s['cwd']}: {s['status']}{note}")
        if s["command"]:
            out.append(f"      {shlex.join(s['command'])}")
    out.extend(f"warning: {w}" for w in plan["warnings"])
    sm = plan["summary"]
    out.append(f"summary: mounts {sm.get('mounts')}, sessions {sm.get('sessions')}, "
               f"statuses {sm.get('statuses')}")
    return "\n".join(out)


# --- commands ---------------------------------------------------------------------------


# A snapshot of the current boot must not be written while this boot's restore is still
# pending: the timer fires ~2 min after boot, `restore --auto` then reopens sessions for up to
# DEADLINE_S, and a second reboot in that window would make select_snapshot prefer the partial
# new-boot snapshot (newer, non-empty) over the original full one. Rule: skip the snapshot when
# (a) restore.lock is held (a restore is running now), or (b) the newest previous-boot snapshot
# has sessions, this boot has no done-<boot> marker, and the boot is younger than
# PENDING_RESTORE_WINDOW_S (restore was expected; the window bounds the pause when a restore
# never completes, e.g. failed sessions leave no marker). With no previous sessions to
# restore, or an older boot, snapshots proceed as before.
PENDING_RESTORE_WINDOW_S = DEADLINE_S + 15 * 60


def restore_pending(sd: Path, boot_id: str) -> str | None:
    lock_path = sd / "restore.lock"
    if lock_path.exists():
        with open(lock_path, "w") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return "restore in progress (restore.lock held)"
    if (sd / f"done-{boot_id}").exists():
        return None
    prev = select_snapshot(sd, boot_id)
    if prev is None or not _has_sessions(prev):
        return None
    btime = boot_time()
    if btime is None or now() - btime > PENDING_RESTORE_WINDOW_S:
        return None
    return f"restore of {prev.parent.name} pending for this boot"


def cmd_snapshot(args: argparse.Namespace) -> int:
    sd = state_dir()
    boot_id = current_boot_id()
    if not _BOOT_ID_RE.match(boot_id):
        print(f"claude-recover: unsafe boot id {boot_id!r}", file=sys.stderr)
        return 2
    _guard_not_fuse(sd)
    ts = now()
    with _Lock(sd):
        remove_stale_tmp(sd)
        reason = restore_pending(sd, boot_id)
        if reason:
            # Not touching the snapshots keeps select_snapshot on the pre-crash one.
            print(json.dumps({"written": None, "skipped": reason}))
            return 0
        try:
            snap = build_snapshot()
        except TmuxError as exc:
            print(f"claude-recover: snapshot not taken: {exc}", file=sys.stderr)
            return 1
        snap["sha256"] = content_hash(snap)
        existing = boot_snapshots(sd, boot_id)
        previous_sha = None
        if existing:
            try:
                previous_sha = json.loads(existing[-1].read_text()).get("sha256")
            except (OSError, ValueError, AttributeError):
                previous_sha = None
        written = None
        if args.force or snap["sha256"] != previous_sha:
            snap["taken_at"] = ts
            name = iso_ts(ts)
            target = snapshots_root(sd) / boot_id / f"{name}.json"
            n = 1
            while target.exists():
                target = target.with_name(f"{name}{COLLISION_SEP}{n:02d}.json")
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
    try:
        latest = read_snapshot(snaps[-1]) if snaps else None
    except (OSError, ValueError) as exc:
        return _fail(f"latest snapshot {snaps[-1].name} is unreadable: {exc}")
    try:
        live_ids = {s["sessionId"] for s in live_sessions()}
    except TmuxError as exc:
        return _fail(str(exc))
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


def _restore_config(args: argparse.Namespace, snap_path: Path, hook_files: list[Path]) -> dict:
    warnings = []
    if not hook_files:
        warnings.append(f"no recover.d hooks in {hooks_dir()}: mounts are not restored and disk cleanup "
                        "beyond docker/hygiene is skipped")
    return {
        "dry_run": args.dry_run,
        "snapshot_path": str(snap_path),
        "current_boot_id": current_boot_id(),
        "mode": args.mode,
        "simulate_reboot": args.simulate_reboot,
        "tmux_session": os.environ.get("CLAUDE_RECOVER_TMUX_SESSION", DEFAULT_TMUX_SESSION),
        "tmux_bin": tmux_bin(),
        "claude_bin": os.environ.get("CLAUDE_RECOVER_CLAUDE_BIN", "claude"),
        "continue_prompt": os.environ.get("CLAUDE_RECOVER_CONTINUE_PROMPT", DEFAULT_CONTINUE_PROMPT),
        "transcript_exists": os.path.exists,
        "essential": read_essential(hooks_dir() / "essential-mounts.txt"),
        "hooks": [h.name for h in hook_files],
        "warnings": warnings,
    }


def _emit(plan: dict, fmt: str) -> None:
    print(json.dumps(plan, indent=2) if fmt == "json" else render_text(plan))


def cmd_restore(args: argparse.Namespace) -> int:
    if args.simulate_reboot and not args.dry_run:
        print("claude-recover: --simulate-reboot is only allowed with --dry-run", file=sys.stderr)
        return 2
    sd = state_dir()
    boot_id = current_boot_id()
    if not _BOOT_ID_RE.match(boot_id):
        print(f"claude-recover: unsafe boot id {boot_id!r}", file=sys.stderr)
        return 2
    marker = sd / f"done-{boot_id}"
    if args.auto and marker.exists():
        print(f"claude-recover: already restored on this boot ({marker.name})", file=sys.stderr)
        return 0
    if args.snapshot:
        snap_path = Path(args.snapshot)
    else:
        snap_path = select_snapshot(sd, boot_id, any_boot=args.any_boot or args.simulate_reboot)
        if snap_path is None:
            print("claude-recover: no snapshot to restore from", file=sys.stderr)
            return 0 if args.auto else 1
    try:
        snapshot = read_snapshot(snap_path)
    except (OSError, ValueError) as exc:
        print(f"claude-recover: snapshot {snap_path} is unreadable: {exc}", file=sys.stderr)
        return 1
    if "sha256" in snapshot and snapshot["sha256"] != content_hash(snapshot):
        print(f"claude-recover: snapshot {snap_path} fails its sha256 check", file=sys.stderr)
        return 1
    # An unattended boot-time restore must survive one bad entry: keep only well-formed ones.
    snapshot["sessions"] = [s for s in snapshot["sessions"]
                            if isinstance(s, dict) and isinstance(s.get("sessionId"), str)]
    mounts = snapshot.get("mounts")
    snapshot["mounts"] = ([m for m in mounts if isinstance(m, dict) and isinstance(m.get("mountpoint"), str)]
                          if isinstance(mounts, list) else [])

    hook_files = list_hooks(hooks_dir())
    config = _restore_config(args, snap_path, hook_files)
    live = {"mounted": set(), "alive": set()}
    if not args.simulate_reboot:
        live["mounted"] = mounted_set([m["mountpoint"] for m in snapshot.get("mounts", [])])
        live["alive"] = alive_session_ids({s["sessionId"] for s in snapshot.get("sessions", [])})
    plan = build_plan(snapshot, live, measure_disk(), config)

    if args.dry_run:
        with tempfile.TemporaryDirectory(prefix="claude-recover-dry-") as tmp:
            scratch = Path(tmp)
            Restorer(plan, snapshot, config, args, RunLog(None, False), scratch / "plan.json",
                     scratch / "hooks.log", hook_files).run()
            plan["summary"] = summarize(plan, 0.0)
        _emit(plan, args.format)
        return 0

    _guard_not_fuse(sd)
    mkdir_private(sd)
    lock = open(sd / "restore.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("claude-recover: another restore is running", file=sys.stderr)
        return 0 if args.auto else 1
    try:
        log_path = sd / "recover.log"
        log = RunLog(log_path, args.auto)
        log(f"restore start: snapshot={snap_path} mode={args.mode} auto={args.auto}")
        started = time.monotonic()
        restorer = Restorer(plan, snapshot, config, args, log, sd / "restore-plan.json", log_path, hook_files)
        restorer.run()
        plan["summary"] = summarize(plan, time.monotonic() - started)
        restorer.write_plan()
        pending = sd / "pending-confirm.txt"
        if plan["disk"]["level"] != "ok":
            block = confirm_block(plan)
            if args.auto:
                write_atomic(pending, block + "\n")
                for line in block.splitlines():
                    log(line, "WARNING")
            else:
                print(block, file=sys.stderr)
        elif pending.exists():
            pending.unlink()
        for w in plan["warnings"]:
            log(w, "WARNING")
        log(f"restore done: {json.dumps(plan['summary'], sort_keys=True)}")
        # A session skipped for lack of tmux or time was not reopened: leave the next run a chance.
        unfinished = [x for x in plan["sessions"] if x.get("status") == "skipped" and (
            x.get("detail") == "deadline reached" or str(x.get("detail", "")).startswith("tmux session "))]
        if args.auto and plan["summary"]["failed"] == 0 and not unfinished:
            write_atomic(marker, f"{iso_ts(now())}\n")
        _emit(plan, args.format)
        return 1 if plan["summary"]["failed"] else 0
    finally:
        lock.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-recover", description=__doc__.split("\n\n")[0],
        epilog="With no subcommand, `restore` runs: `claude-recover --dry-run` is "
               "`claude-recover restore --dry-run`.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_rest = sub.add_parser("restore", help="after a reboot: clean the disk, remount, reopen sessions "
                                            "(the default command)")
    p_rest.add_argument("--dry-run", action="store_true",
                        help="print the plan; run nothing (hooks are called with RECOVER_DRY_RUN=1)")
    p_rest.add_argument("--snapshot", metavar="PATH", help="restore from this snapshot file")
    p_rest.add_argument("--mode", choices=("open", "continue"), default="open",
                        help="open: reopen sessions and wait (default); continue: also send a "
                             "prompt to sessions that were busy")
    p_rest.add_argument("--simulate-reboot", action="store_true",
                        help="plan as if nothing is mounted or running (requires --dry-run; "
                             "implies --any-boot)")
    p_rest.add_argument("--any-boot", action="store_true", help="use the newest snapshot of any boot")
    p_rest.add_argument("--auto", action="store_true",
                        help="unattended boot run: once per boot (marker), retries the mounts phase")
    p_rest.add_argument("--format", choices=("text", "json"), default="text")
    p_rest.set_defaults(func=cmd_restore)
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
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0] not in SUBCOMMANDS and argv[0] not in ("-h", "--help")):
        argv.insert(0, "restore")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
