#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyte==0.8.2"]
# ///
"""Outside timing instrument of the console dashboard (plan-dashboard speed-up).

Runs the dashboard under a pseudo-terminal, stamps with ``time.monotonic_ns`` the moment each awaited text
appears on a ``pyte`` screen, and reads the moment stream the code writes to a named pipe under ``tui --events PATH``.
It never imports the dashboard and never writes under the agent home: every file it makes lives under its
state directory (``--state-dir``, default ``~/.local/state/dashboard-speedup``).

Subcommands: ``census``, ``enumerate``, ``floors``, ``campaign``, ``status``, ``verify-floors``,
``verify-acceptance`` and ``selftest``. ``fixtures/plan_dashboard_contract.json`` is the contract between this
file and the code (snapshot rule, old marking text, event vocabulary, source-to-task rule);
``fixtures/plan_dashboard_sources.json`` lists every source the dashboard reads. The measuring subcommands run
under ``uv`` (pyte is imported only inside them); the verify subcommands use the standard library only.

Two clocks agree here: the harness and the dashboard both read CLOCK_MONOTONIC, so an event's ``t_ns`` and a
screen stamp are comparable once both are taken from the harness's launch moment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import signal
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CONTRACT_PATH = HERE / "fixtures" / "plan_dashboard_contract.json"
SOURCES_PATH = HERE / "fixtures" / "plan_dashboard_sources.json"
PROBE = HERE / "plan_dashboard_start_probe.py"
STATE_DIR_DEFAULT = Path.home() / ".local" / "state" / "dashboard-speedup"

LIVE_WORDS = ("active", "idle", "stale")
HEADER_WORDS = ("task", "wait", "session", "live")
OLD_MARK, UNREAD_MARK = "~", "?"
ROWS_COLS = (40, 200)

DEFAULT_LIMITS = {"R1": 0.70, "R2": 0.95, "R3": 0.25, "R6": 1.1}
STREAM_SCREEN_MAX_MS = 20.0
MIN_RUNS, MAX_RUNS, SPREAD_TARGET = 5, 30, 0.05
R3_CYCLES, R3_MIN_RUNS, K1_MIN_RUNS = 20, 5, 3
MIN_CHANGED = 100
BANDS = {"near-zero": (0, 10), "80-100": (80, 100), "101-130": (101, 130), "131-170": (131, 170),
         "220-280": (220, 280), "350+": (350, None)}
NO_BAND_CELLS = ("no-snapshot", "damaged")
R1_CELLS = tuple(BANDS)
R2_CELLS = ("near-zero", "80-100")
R6_CELLS = ("101-130", "131-170", "220-280", "350+", "no-snapshot", "damaged")
REQUIRED_CELLS = tuple(c for c in (*BANDS, *NO_BAND_CELLS) if c != "350+")
FORBIDDEN_ADDED = (re.compile(r"\.service$"), re.compile(r"\.timer$"), re.compile(r"(^|/)crontab$"),
                   re.compile(r"(^|/)etc/cron"))
EXIT_OK, EXIT_MISS, EXIT_BAD = 0, 1, 2


def load_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(*chunks: bytes) -> str:
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk)
    return digest.hexdigest()


# --- identity of the instrument and of the trees it judges --------------------------------------------

def instrument_sha256() -> str:
    chunks = []
    for path in (Path(__file__).resolve(), CONTRACT_PATH, SOURCES_PATH):
        chunks += [path.name.encode(), b"\0", path.read_bytes() if path.exists() else b"", b"\0"]
    return sha256_bytes(*chunks)


def tree_files(root: Path) -> list[str]:
    root = Path(root)
    names = [p.relative_to(root).as_posix() for p in (root / "scripts" / "plan_dashboard").glob("*.py")]
    if (root / "scripts" / "plan-dashboard.py").exists():
        names.append("scripts/plan-dashboard.py")
    return sorted(names)


def tree_sha256(root: Path) -> str:
    chunks = []
    for rel in tree_files(root):
        chunks += [rel.encode(), b"\0", sha256_bytes((Path(root) / rel).read_bytes()).encode(), b"\0"]
    return sha256_bytes(*chunks)


def git(tree: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(tree), *args], capture_output=True, text=True)


def committed_tree_sha256(tree: Path, commit: str) -> str:
    chunks = []
    names = git(tree, "ls-tree", "-r", "--name-only", commit, "scripts/plan_dashboard", "scripts/plan-dashboard.py")
    for rel in sorted(n for n in names.stdout.splitlines() if re.fullmatch(r"scripts/plan_dashboard/[^/]+\.py|scripts/plan-dashboard\.py", n)):
        blob = subprocess.run(["git", "-C", str(tree), "show", f"{commit}:{rel}"], capture_output=True).stdout
        chunks += [rel.encode(), b"\0", sha256_bytes(blob).encode(), b"\0"]
    return sha256_bytes(*chunks)


def added_files(tree: Path, base_commit: str) -> list[str]:
    """Files the tree adds over ``base_commit``: committed, staged or untracked."""
    committed = git(tree, "diff", "--name-only", "--diff-filter=A", base_commit, "HEAD").stdout.split()
    untracked = git(tree, "ls-files", "--others", "--exclude-standard").stdout.split()
    return sorted(set(committed) | set(untracked))


# --- sources of the dashboard and their identities --------------------------------------------------

def agent_home() -> Path:
    for var in ("CLAUDE_CONFIG_DIR", "CLAUDE_AGENT_HOME"):
        value = os.environ.get(var)
        if value:
            return Path(value).expanduser()
    isolated = Path.home() / ".claude-agent"
    return isolated if isolated.exists() else Path.home() / ".claude"


def projects_roots() -> list[Path]:
    roots = []
    for root in (agent_home() / "projects", Path.home() / ".claude" / "projects"):
        if root.is_dir() and root not in roots:
            roots.append(root)
    return roots


def cost_ledger() -> Path:
    return Path.home() / ".local" / "log" / "claude-spawn-costs.jsonl"


def identity(path, kind: str) -> dict | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return {"path": str(path), "kind": kind, "dev": st.st_dev, "ino": st.st_ino, "size": st.st_size,
            "mtime_ns": st.st_mtime_ns, "ctime_ns": st.st_ctime_ns}


def live_sources() -> list[dict]:
    found = []
    agentctl = agent_home() / "agentctl"
    for kind, folder in (("state", agentctl / "state"), ("binding", agentctl / "engine-session-bindings")):
        if folder.is_dir():
            found += [identity(p, kind) for p in sorted(folder.glob("*.json"))]
    for root in projects_roots():
        found += [identity(p, "transcript") for p in sorted(root.glob("*/*.jsonl"))]
    found.append(identity(cost_ledger(), "ledger"))
    return [item for item in found if item is not None]


IDENTITY_KEYS = ("dev", "ino", "size", "mtime_ns", "ctime_ns")


def changed_since(recorded: list[dict], live: list[dict]) -> list[dict]:
    """Sources whose identity differs from the recorded one; created ones count, deleted ones carry ``deleted``."""
    before = {item["path"]: item for item in recorded}
    now = {item["path"]: item for item in live}
    changed = [item for path, item in now.items()
               if path not in before or any(item[k] != before[path][k] for k in IDENTITY_KEYS)]
    changed += [dict(item, deleted=True) for path, item in before.items() if path not in now]
    return changed


def kind_of_path(path: str) -> str | None:
    p = Path(path)
    home = agent_home()
    if p == cost_ledger():
        return "ledger"
    try:
        rel = p.relative_to(home)
    except ValueError:
        rel = None
    if rel is not None and len(rel.parts) == 3 and rel.parts[:2] == ("agentctl", "state") and p.suffix == ".json":
        return "state"
    if rel is not None and len(rel.parts) == 3 and rel.parts[:2] == ("agentctl", "engine-session-bindings") \
            and p.suffix == ".json":
        return "binding"
    for root in (home / "projects", Path.home() / ".claude" / "projects"):
        try:
            sub = p.relative_to(root)
        except ValueError:
            continue
        if len(sub.parts) == 2 and p.suffix == ".jsonl":
            return "transcript"
    return None


def read_set(trace_paths: dict[str, list[str]], live: list[dict]) -> list[dict]:
    """The live files a row, mark or cost can come from: whatever the trace opened, every state, binding and
    ledger file, and the transcripts of harness sessions the trace read."""
    traced = {p for paths in trace_paths.values() for p in paths}
    stems = {Path(p).stem for p in trace_paths.get("transcript", [])}
    return [item for item in live
            if item["path"] in traced or item["kind"] in ("state", "binding", "ledger")
            or (item["kind"] == "transcript" and Path(item["path"]).stem in stems)]


def bindings_map(folder: Path) -> dict[str, list[str]]:
    mapping = {}
    if folder.is_dir():
        for path in folder.glob("*.json"):
            try:
                data = load_json(path)
            except (OSError, ValueError):
                continue
            driven = data.get("driven") if isinstance(data, dict) else None
            mapping[path.stem] = [e["session"] for e in driven or []
                                  if isinstance(e, dict) and isinstance(e.get("session"), str)]
    return mapping


def tasks_of_source(path: str, kind: str, bindings: dict[str, list[str]]):
    """The tasks a source belongs to by the contract's rule; ``None`` stands for every task."""
    stem = Path(path).stem
    if kind == "state":
        return {stem}
    if kind == "binding":
        return set(bindings.get(stem, []))
    if kind == "transcript":
        return set(bindings.get(stem, [])) | {stem}
    return None


def changed_map(changed: list[dict], bindings: dict[str, list[str]]) -> dict[str, set | None]:
    return {item["path"]: tasks_of_source(item["path"], item["kind"], bindings) for item in changed
            if not item.get("deleted")}


def contract_changes(copy: dict, live: list[dict]) -> list[dict]:
    """The sources changed since a saved snapshot copy, by the contract's source-to-task rule.

    State, binding and ledger files compare as ``changed_since`` compares them. A transcript is a source only of the
    tasks ``tasks_of_source`` gives it, so it counts only when one of them can be shown
    (``ContractWatcher.shown_tasks``: the copy's rows, the window, changed state and binding files), and then when it
    is recorded with another identity, recorded and gone, or unrecorded and written at or after the copy's taken
    time less 120 s (``taken_at`` is stamped to the second after a save run). Recorded transcripts are stat'ed
    directly, so one the ``*/*.jsonl`` listing misses is not taken for deleted.
    """
    try:
        taken_ns = int(datetime.fromisoformat(copy["taken_at"]).timestamp()) * 10**9
    except (KeyError, TypeError, ValueError):
        taken_ns = 0
    rows = set()
    try:
        sections = load_json(Path(copy["file"])).get("sections") or {}
    except (KeyError, OSError, ValueError, AttributeError):
        sections = {}
    for section in sections.values():
        tasks = (section.get("snapshot") or {}).get("tasks") if isinstance(section, dict) else None
        rows |= {t["session_id"] for t in tasks or [] if isinstance(t, dict) and isinstance(t.get("session_id"), str)}
    files = [item for item in live if item["kind"] != "transcript"]
    changed = changed_since([item for item in copy["sources"] if item["kind"] != "transcript"], files)
    bindings = bindings_map(agent_home() / "agentctl" / "engine-session-bindings")
    shown = ContractWatcher.shown_tasks(rows, changed, files, taken_ns, bindings)
    recorded = {item["path"]: item for item in copy["sources"] if item["kind"] == "transcript"}
    for path, before in recorded.items():
        if tasks_of_source(path, "transcript", bindings) & shown:
            now = identity(path, "transcript")
            if now is None:
                changed.append(dict(before, deleted=True))
            elif any(now[k] != before[k] for k in IDENTITY_KEYS):
                changed.append(now)
    changed += [item for item in live if item["kind"] == "transcript" and item["path"] not in recorded
                and tasks_of_source(item["path"], "transcript", bindings) & shown
                and max(item["mtime_ns"], item["ctime_ns"]) >= taken_ns - 120 * 10**9]
    return changed


# --- statistics ----------------------------------------------------------------------------------------

def median(values) -> float | None:
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def bootstrap_ci(values, *, resamples: int = 1000, seed: int = 20261106) -> tuple[float, float]:
    """95% bootstrap interval of the median; deterministic for the same input."""
    values = list(values)
    rng = random.Random(seed)
    medians = sorted(statistics.median(rng.choices(values, k=len(values))) for _ in range(resamples))
    return medians[int(0.025 * (resamples - 1))], medians[int(0.975 * (resamples - 1))]


def half_width(values) -> float:
    low, high = bootstrap_ci(values)
    return (high - low) / 2


def settled(values) -> bool:
    """At least ``MIN_RUNS`` runs and a 95% half-width under ``SPREAD_TARGET`` of the median."""
    values = list(values)
    if len(values) < MIN_RUNS:
        return False
    centre = statistics.median(values)
    return centre > 0 and half_width(values) < SPREAD_TARGET * centre


def spread(values) -> dict:
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0}
    out = {"n": len(values), "median": statistics.median(values), "min": min(values), "max": max(values)}
    if len(values) >= 2:
        out["half_width_95"] = half_width(values)
    return out


def cap_applies(new_values, old_values) -> bool:
    """Runs were added up to the cap and an arm's median is still not under the spread target."""
    return len(list(new_values)) >= MAX_RUNS and not (settled(new_values) and settled(old_values))


def within_factor(new_values, old_values, factor: float) -> bool:
    """``new <= factor * old``; at the cap the upper bound of the new median is compared instead of the median."""
    new_values, old_values = list(new_values), list(old_values)
    bound = bootstrap_ci(new_values)[1] if cap_applies(new_values, old_values) else statistics.median(new_values)
    return bound <= factor * statistics.median(old_values)


def band_of(count: int | None) -> str | None:
    if count is None:
        return None
    for name, (low, high) in BANDS.items():
        if count >= low and (high is None or count <= high):
            return name
    return None


def in_band(cell: str, count: int | None) -> bool:
    if cell in NO_BAND_CELLS:
        return True
    return band_of(count) == cell


def next_arm(runs_so_far: list[dict]) -> str:
    """Old and new alternate within a cell; old goes first."""
    old = sum(1 for r in runs_so_far if r["arm"] == "old")
    new = sum(1 for r in runs_so_far if r["arm"] == "new")
    return "old" if old <= new else "new"


def cell_complete(old_runs: list[dict], new_runs: list[dict], metrics: tuple[str, ...]) -> bool:
    if len(old_runs) < MIN_RUNS or len(new_runs) < MIN_RUNS:
        return False
    if min(len(old_runs), len(new_runs)) >= MAX_RUNS:
        return True
    for runs in (old_runs, new_runs):
        for metric in metrics:
            if not settled([r.get(metric) for r in runs if r.get(metric) is not None]):
                return False
    return True


# --- the screen ----------------------------------------------------------------------------------------

def parse_rows(display: list[str]) -> dict | None:
    """``{"rows": {session_id: wait_cell}}`` read from the overview table, or ``None`` before its header is on."""
    for index, line in enumerate(display):
        if not re.search(r"\btask\b.*\bwait\b.*\bsession\b.*\blive\b", line):
            continue
        offsets, cursor = [], 0
        for word in HEADER_WORDS:
            at = line.find(word, cursor)
            offsets.append(at)
            cursor = at + len(word)
        wait_at, session_at, live_at = offsets[1], offsets[2], offsets[3]
        rows = {}
        for row in display[index + 1:]:
            live = row[live_at:].split()
            if live and live[0] in LIVE_WORDS:
                rows[row[session_at:live_at].strip()] = row[wait_at:session_at].strip()
        return {"rows": rows}
    return None


def screen_summary(frames: list[tuple[float, dict]]) -> dict:
    """Moments, in seconds from launch, read from the screen frames ``(t, parsed)``."""
    shown = [(t, f["rows"]) for t, f in frames if f and f["rows"]]
    out = {"first_rows_s": None, "rows_confirmed_s": None, "marks_confirmed_s": None,
           "old_marking_at_first_rows": None, "first_screen_rows": 0}
    if not shown:
        return out
    first_t, first_rows = shown[0]
    out.update(first_rows_s=first_t, first_screen_rows=len(first_rows),
               old_marking_at_first_rows=all(OLD_MARK in cell for cell in first_rows.values()))
    stable = set(first_rows) & set(shown[-1][1])
    for t, rows in shown:
        if not stable or not stable <= set(rows):
            continue
        cells = [rows[i] for i in stable]
        if out["rows_confirmed_s"] is None and not any(OLD_MARK in c for c in cells):
            out["rows_confirmed_s"] = t
        if out["marks_confirmed_s"] is None and not any(OLD_MARK in c or UNREAD_MARK in c for c in cells):
            out["marks_confirmed_s"] = t
    return out


# --- one dashboard run under a pseudo-terminal -----------------------------------------------------------

class Trace:
    def __init__(self, t0_ns: int):
        self.t0_ns = t0_ns
        self.frames: list[tuple[float, dict]] = []
        self.events: list[dict] = []
        self.bad_lines: list[str] = []
        self.exit_code: int | None = None
        self.display: list[str] = []
        self.child_samples: list[tuple[float, int]] = []
        self.wall_minus_mono_ns = time.time_ns() - time.monotonic_ns()

    def seconds(self, t_ns: int) -> float:
        return (t_ns - self.t0_ns) / 1e9

    def event_times(self, name: str) -> list[float]:
        return [self.seconds(e["t_ns"]) for e in self.events if e.get("ev") == name]


def descendants(root_pid: int) -> int:
    """Live processes below ``root_pid`` read from /proc."""
    parent = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
            parent[int(entry)] = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
    count, frontier = 0, {root_pid}
    while frontier:
        frontier = {pid for pid, ppid in parent.items() if ppid in frontier}
        count += len(frontier)
    return count


EVENTS_SLOT = "<events-pipe>"
EVENTS_DIR: Path = STATE_DIR_DEFAULT / "events"


def dashboard_argv(tree: Path, *, events: bool, interval: float | None = None) -> list[str]:
    argv = [sys.executable, str(Path(tree) / "scripts" / "plan-dashboard.py"), "tui"]
    if events:
        argv += ["--events", EVENTS_SLOT]
    if interval is not None:
        argv += ["--interval", str(interval)]
    return argv


def run_pty(argv: list[str], *, cwd: Path, env: dict, events: bool, max_s: float, stop=None,
            on_event=None, terminate: bool = False, sample_children: bool = False,
            term_wait_s: float = 6.0) -> Trace:
    """Run ``argv`` on a pseudo-terminal, feed its output to a pyte screen and read its event stream.

    The stream travels on a named pipe the harness makes under ``EVENTS_DIR`` and puts in place of
    ``EVENTS_SLOT`` in ``argv`` (Textual draws on stderr, so the stream cannot use it). ``stop(trace)`` ends
    the run when true; ``terminate`` ends it with SIGTERM to the process group and keeps reading until the
    process exits (a save run), otherwise the group is killed. ``on_event(trace, event)`` sees each stream
    event as it is read.
    """
    import fcntl
    import pty
    import pyte
    import select
    import struct
    import termios

    err_read, fifo = None, None
    if events:
        EVENTS_DIR.mkdir(parents=True, exist_ok=True)
        fifo = EVENTS_DIR / f"events-{os.getpid()}-{time.monotonic_ns()}.fifo"
        os.mkfifo(fifo)
        err_read = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        argv = [str(fifo) if a == EVENTS_SLOT else a for a in argv]
    t0 = time.monotonic_ns()
    trace = Trace(t0)
    pid, master = pty.fork()
    if pid == 0:
        try:
            os.chdir(cwd)
            os.execvpe(argv[0], argv, env)
        finally:
            os._exit(127)
    rows, cols = ROWS_COLS
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    screen = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(screen)
    partial = b""
    last_state = None
    ending_at = None
    next_sample = 0.0
    deadline = time.monotonic() + max_s
    open_fds = {master} | ({err_read} if events else set())
    try:
        while open_fds:
            now = time.monotonic()
            if ending_at is None and (now >= deadline or (stop is not None and stop(trace))):
                ending_at = now
                try:
                    os.killpg(pid, signal.SIGTERM)
                except OSError:
                    pass
                if not terminate:
                    break
            if ending_at is not None and now - ending_at > term_wait_s:
                break
            ready, _, _ = select.select(sorted(open_fds), [], [], 0.01)
            if sample_children and time.monotonic() >= next_sample:
                trace.child_samples.append((trace.seconds(time.monotonic_ns()), descendants(pid)))
                next_sample = time.monotonic() + 0.1
            for fd in ready:
                try:
                    data = os.read(fd, 1 << 16)
                except OSError:
                    data = b""
                if not data:
                    open_fds.discard(fd)
                    continue
                stamp = time.monotonic_ns()
                if fd == master:
                    stream.feed(data)
                    parsed = parse_rows(screen.display)
                    key = json.dumps(parsed, sort_keys=True)
                    if key != last_state:
                        last_state = key
                        trace.frames.append((trace.seconds(stamp), parsed))
                else:
                    partial += data
                    *lines, partial = partial.split(b"\n")
                    for line in lines:
                        try:
                            event = json.loads(line)
                        except ValueError:
                            trace.bad_lines.append(line[:200].decode("utf-8", "replace"))
                            continue
                        event["seen_ns"] = stamp
                        trace.events.append(event)
                        if on_event is not None:
                            on_event(trace, event)
    finally:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pid, sig)
            except OSError:
                break
            time.sleep(0.05)
        try:
            _, status = os.waitpid(pid, 0)
            trace.exit_code = os.waitstatus_to_exitcode(status)
        except ChildProcessError:
            pass
        for fd in (master, err_read):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if fifo is not None:
            fifo.unlink(missing_ok=True)
    trace.display = list(screen.display)
    return trace


def run_env(cache_home: Path, uv_cache: str | None) -> dict:
    env = dict(os.environ)
    env["XDG_CACHE_HOME"] = str(cache_home)
    env["TERM"] = "xterm-256color"
    if uv_cache:
        env["UV_CACHE_DIR"] = uv_cache
    return env


def uv_cache_dir() -> str | None:
    try:
        out = subprocess.run(["uv", "cache", "dir"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


class StatWatcher:
    """Stats a fixed set of files on demand and reports those that changed since the last poll."""

    def __init__(self, paths: list[str]):
        self.paths = paths
        self.seen = {p: identity(p, "") for p in paths}

    def poll(self, due_wall_ns: int | None = None) -> list[str]:
        changed = []
        for path in self.paths:
            now = identity(path, "")
            before = self.seen.get(path)
            same = (now is None and before is None) or (
                now is not None and before is not None and all(now[k] == before[k] for k in IDENTITY_KEYS))
            if same:
                continue
            if now is not None and due_wall_ns is not None and now["mtime_ns"] > due_wall_ns:
                continue
            changed.append(path)
            self.seen[path] = now
        return changed


class ContractWatcher:
    """R3's due sources by the contract's rule, re-listed at every cycle start: every state, binding and ledger file
    that changed or was created, and every changed or created transcript of a task the dashboard can show, with
    ``StatWatcher``'s rule that a file modified after the cycle's start is not yet due."""

    WINDOW_NS = 24 * 3600 * 10**9

    def __init__(self):
        self.seen = {item["path"]: item for item in live_sources()}

    @staticmethod
    def shown_tasks(rows: set[str], changed: list[dict], files: list[dict], at_ns: int,
                    bindings: dict[str, list[str]]) -> set[str]:
        """The rows, the task of every state file inside the 24 h window at ``at_ns`` or changed, and the tasks
        driven by every changed binding file; the window term keeps a row the code under test drops in the set."""
        shown = set(rows)
        shown |= {Path(item["path"]).stem for item in files
                  if item["kind"] == "state" and item["mtime_ns"] >= at_ns - ContractWatcher.WINDOW_NS}
        for item in changed:
            if item["kind"] in ("state", "binding"):
                shown |= tasks_of_source(item["path"], item["kind"], bindings)
        return shown

    @staticmethod
    def screen_ids(frames: list[tuple[float, dict]]) -> set[str]:
        """The session cells of the latest screen frame that has rows."""
        for _, parsed in reversed(frames):
            if parsed and parsed["rows"]:
                return set(parsed["rows"])
        return set()

    def poll(self, due_wall_ns: int, screen_ids: set[str]) -> list[str]:
        live = {item["path"]: item for item in live_sources()}
        moved = sorted(p for p in {*self.seen, *live} if p not in live or p not in self.seen
                       or any(live[p][k] != self.seen[p][k] for k in IDENTITY_KEYS))
        # Shown tasks are those at the cycle's start: a file modified after it counts as last seen, or not at all.
        files = [item if item["mtime_ns"] <= due_wall_ns else self.seen.get(path) for path, item in live.items()]
        files = [item for item in files if item and item["kind"] != "transcript" and item["mtime_ns"] <= due_wall_ns]
        changed = [item for item in (live.get(p) or self.seen[p] for p in moved)
                   if item["kind"] != "transcript" and item["mtime_ns"] <= due_wall_ns]
        bindings = bindings_map(agent_home() / "agentctl" / "engine-session-bindings")
        rows = {Path(item["path"]).stem for item in files if item["kind"] == "state"
                and any(cell and Path(item["path"]).stem.startswith(cell) for cell in screen_ids)}
        shown = self.shown_tasks(rows, changed, files, due_wall_ns, bindings)
        due = []
        for path in moved:
            now = live.get(path)
            kind = (now or self.seen[path])["kind"]
            if kind == "transcript" and not tasks_of_source(path, kind, bindings) & shown:
                continue
            if now is not None and now["mtime_ns"] > due_wall_ns:
                continue
            due.append(path)
            if now is None:
                del self.seen[path]
            else:
                self.seen[path] = now
        return due


# --- what one run says --------------------------------------------------------------------------------

def covers(applied, required: set[str]) -> bool:
    return isinstance(applied, list) and required <= set(applied)


def event_summary(events: list[dict], t0_ns: int, *, arm: str, first_screen_ids: set[str],
                  changes: dict[str, set | None]) -> dict:
    """Moments and cover checks read from the stream. ``changes`` maps each changed path to the tasks it belongs to
    (``None`` for every task)."""
    def sec(e):
        return (e["t_ns"] - t0_ns) / 1e9

    def first(name):
        found = [e for e in events if e.get("ev") == name]
        return found[0] if found else None

    out = {"rows_painted_s": None, "from_snapshot": None, "first_screen_confirmed_s": None,
           "last_old_cleared_s": None, "live_check_end_s": None, "mark_confirmed_max_s": None,
           "marks_missing": None, "applied_ok": None}
    rows = first("rows_painted")
    if rows:
        out.update(rows_painted_s=sec(rows), from_snapshot=bool(rows.get("from_snapshot")))
    for name, field in (("first_screen_confirmed", "first_screen_confirmed_s"),
                        ("last_old_cleared", "last_old_cleared_s"), ("live_check_end", "live_check_end_s")):
        e = first(name)
        out[field] = sec(e) if e else None
    confirmed = {}
    for e in events:
        if e.get("ev") == "mark_confirmed" and isinstance(e.get("task"), str):
            confirmed.setdefault(e["task"], e)
    if confirmed:
        out["mark_confirmed_max_s"] = max(sec(e) for e in confirmed.values())
    out["marks_missing"] = sum(1 for shown in first_screen_ids if not any(t.startswith(shown) for t in confirmed))
    if arm == "new":
        ok = True
        end = first("live_check_end")
        everything = set(changes)
        ok &= bool(end) and covers(end.get("applied"), everything)
        for task, e in confirmed.items():
            needed = {p for p, tasks in changes.items() if tasks is None or task in tasks}
            ok &= covers(e.get("applied"), needed)
        out["applied_ok"] = bool(ok)
    return out


def summarize_run(trace_like: dict, *, arm: str, changed_count: int | None, changes: dict[str, set | None],
                  load1: float | None = None) -> dict:
    """One campaign record from a run's frames and events (both already in seconds / ns from launch)."""
    frames = [(t, f) for t, f in trace_like["frames"]]
    screen = screen_summary(frames)
    first_ids = set(next((f["rows"] for _, f in frames if f and f["rows"]), {}))
    record = {"arm": arm, "changed_count": changed_count, "load1": load1, **screen}
    record.update(event_summary(trace_like["events"], trace_like["t0_ns"], arm=arm, first_screen_ids=first_ids,
                                changes=changes))
    return record


def r2_value(run: dict) -> float | None:
    """The moment a new-code run's first screen and every waiting mark are confirmed: the latest of the code's
    own first_screen_confirmed, the harness's screen stamp and every mark's confirmation; ``None`` when any is absent."""
    parts = [run.get("first_screen_confirmed_s"), run.get("marks_confirmed_s"), run.get("mark_confirmed_max_s")]
    if any(p is None for p in parts) or run.get("marks_missing"):
        return None
    return max(parts)


# --- cycles (R3) --------------------------------------------------------------------------------------

def cycle_records(events: list[dict], t0_ns: int) -> list[dict]:
    starts = {e["cycle"]: e for e in events if e.get("ev") == "cycle_start"}
    ends = {e["cycle"]: e for e in events if e.get("ev") == "cycle_end"}
    out = []
    for number in sorted(set(starts) & set(ends)):
        out.append({"cycle": number, "start_s": (starts[number]["t_ns"] - t0_ns) / 1e9,
                    "duration_s": (ends[number]["t_ns"] - starts[number]["t_ns"]) / 1e9,
                    "applied": ends[number].get("applied")})
    return out


def check_cycle_coverage(cycles: list[dict], due_by_cycle: dict[int, list[str]]) -> list[dict]:
    """Adds ``covered`` to each cycle: the applied set holds every source the harness found changed at its start.
    The old code applies everything (``"all"``)."""
    out = []
    for cycle in cycles:
        due = set(due_by_cycle.get(cycle["cycle"], []))
        applied = cycle["applied"]
        out.append({**cycle, "due": len(due),
                    "covered": applied == "all" or (isinstance(applied, list) and due <= set(applied))})
    return out


def r3_run_record(arm: str, cycles: list[dict]) -> dict:
    take = [c for c in cycles if c["cycle"] >= 2][:R3_CYCLES]
    return {"arm": arm, "cycles": take, "median_s": median(c["duration_s"] for c in take),
            "covered_all": all(c["covered"] for c in take) and len(take) == R3_CYCLES}


# --- acceptance ----------------------------------------------------------------------------------------

def limits_with(overrides: dict[str, float]) -> dict[str, float]:
    return {k: min(v, overrides.get(k, v)) for k, v in DEFAULT_LIMITS.items()}


def runs_of(cell: dict, arm: str) -> list[dict]:
    return [r for r in cell.get("runs", []) if r.get("arm") == arm]


def validate_report(report, floors, *, tree: Path, old_tree: Path | None) -> list[str]:
    """Reasons the report cannot be judged at all (exit 2)."""
    bad = []
    if not isinstance(report, dict) or not isinstance(report.get("cells"), dict):
        return ["acceptance report is malformed: no cells"]
    if report.get("instrument_sha256") != instrument_sha256():
        bad.append("instrument_sha256 differs from the running harness's files")
    if report.get("new_tree_sha256") != tree_sha256(tree):
        bad.append(f"new_tree_sha256 differs from the tree at {tree}")
    old_expected = tree_sha256(old_tree) if old_tree else (floors or {}).get("base_tree_sha256")
    if report.get("old_tree_sha256") != old_expected:
        bad.append("old_tree_sha256 differs from the old tree of the floors")
    for name, cell in report["cells"].items():
        if not isinstance(cell, dict) or not isinstance(cell.get("runs"), list):
            bad.append(f"cell {name} is malformed")
            continue
        for run in cell["runs"]:
            if run.get("arm") not in ("old", "new"):
                bad.append(f"cell {name}: a run has no arm")
            elif name in BANDS and not in_band(name, run.get("changed_count")):
                bad.append(f"cell {name}: a run has changed_count {run.get('changed_count')} outside its band")
    return bad


def judge_cell_r1(name: str, cell: dict, limits: dict) -> list[str]:
    new = runs_of(cell, "new")
    misses = []
    values = [r.get("first_rows_s") for r in new]
    if len(new) < MIN_RUNS or len(runs_of(cell, "old")) < MIN_RUNS:
        return [f"{name}: fewer than {MIN_RUNS} runs"]
    if any(v is None for v in values) or median(values) >= limits["R1"]:
        misses.append(f"{name}: R1 median first-row time {median(values)} s is not under {limits['R1']} s")
    if not all(r.get("old_marking_at_first_rows") for r in new):
        misses.append(f"{name}: the screen at first-row time lacks the old marking")
    return misses


def judge_cell_r2(name: str, cell: dict, limits: dict) -> list[str]:
    new, old = runs_of(cell, "new"), runs_of(cell, "old")
    misses = []
    values = [r2_value(r) for r in new]
    if any(v is None for v in values):
        misses.append(f"{name}: a run has no first_screen_confirmed, never confirmed its screen, or lacks a mark confirmation")
    elif median(values) >= limits["R2"]:
        misses.append(f"{name}: R2 median {median(values)} s is not under {limits['R2']} s")
    if any(r.get("applied_ok") is not True for r in new):
        misses.append(f"{name}: a run's applied set misses a changed source")
    if any(r.get("last_old_cleared_s") is None or r.get("live_check_end_s") is None
           or r["last_old_cleared_s"] > r["live_check_end_s"] for r in new):
        misses.append(f"{name}: last_old_cleared is missing or comes after live_check_end")
    cleared = [r["last_old_cleared_s"] for r in new if r.get("last_old_cleared_s") is not None]
    old_rows = [r["first_rows_s"] for r in old if r.get("first_rows_s") is not None]
    if cleared and old_rows and median(cleared) > limits["R6"] * median(old_rows):
        misses.append(f"{name}: last_old_cleared median {median(cleared)} s exceeds {limits['R6']} x the old time to rows")
    return misses


R6_PAIRS = (("rows_confirmed_s", "rows_confirmed_s"), ("marks_confirmed_s", "marks_confirmed_s"),
            ("last_old_cleared_s", "first_rows_s"), ("mark_confirmed_max_s", "marks_confirmed_s"))


def judge_cell_r6(name: str, cell: dict, limits: dict) -> list[str]:
    new, old = runs_of(cell, "new"), runs_of(cell, "old")
    misses = []
    for new_metric, old_metric in R6_PAIRS:
        new_values = [r.get(new_metric) for r in new]
        old_values = [r.get(old_metric) for r in old if r.get(old_metric) is not None]
        if any(v is None for v in new_values) or not old_values:
            misses.append(f"{name}: {new_metric} is missing in a run")
        elif not within_factor(new_values, old_values, limits["R6"]):
            misses.append(f"{name}: {new_metric} exceeds {limits['R6']} x the old {old_metric}")
    if any(r.get("applied_ok") is not True for r in new):
        misses.append(f"{name}: a run's applied set misses a changed source")
    return misses


def judge_r3(report: dict, limits: dict) -> list[str]:
    runs = [r for r in (report.get("r3") or {}).get("runs", []) if r.get("arm") == "new"]
    if len(runs) < R3_MIN_RUNS:
        return [f"R3: fewer than {R3_MIN_RUNS} new-code runs of {R3_CYCLES} cycles"]
    misses = []
    if any(r.get("median_s") is None or r["median_s"] >= limits["R3"] for r in runs):
        misses.append(f"R3: a run's own median is not under {limits['R3']} s")
    if any(not r.get("covered_all") for r in runs):
        misses.append("R3: a cycle's applied set misses a source the harness found changed")
    return misses


def judge_k1(report: dict) -> list[str]:
    k1 = report.get("k1") or {}
    old, new = k1.get("old", []), k1.get("new", [])
    if len(old) < K1_MIN_RUNS or len(new) < K1_MIN_RUNS:
        return [f"K1: fewer than {K1_MIN_RUNS} strace runs of the old or the new code"]
    misses = []
    old_kinds = {k for r in old for k in r.get("exec_kinds", [])}
    new_kinds = {k for r in new for k in r.get("exec_kinds", [])}
    if new_kinds - old_kinds:
        misses.append(f"K1: the new code starts commands the old code does not: {sorted(new_kinds - old_kinds)}")
    old_children = max((c for r in old for c in r.get("children_between_cycles", [])), default=0)
    new_children = max((c for r in new for c in r.get("children_between_cycles", [])), default=0)
    if new_children > old_children:
        misses.append(f"K1: {new_children} child processes between cycles against the old code's {old_children}")
    old_created = {f for r in old for f in r.get("created_files", [])}
    stray = sorted({f for r in new for f in r.get("created_files", [])} - old_created)
    if stray:
        misses.append(f"K1: the new code leaves files outside its snapshot: {stray[:5]}")
    forbidden = [f for f in k1.get("added_files", []) if any(p.search(f) for p in FORBIDDEN_ADDED)]
    if forbidden:
        misses.append(f"K1: the diff adds a service, timer or cron path: {forbidden}")
    if "added_files" not in k1:
        misses.append("K1: the report lists no added files")
    return misses


def judge_report(report: dict, limits: dict) -> list[str]:
    misses = []
    cells = report["cells"]
    for name in REQUIRED_CELLS:
        if name not in cells:
            misses.append(f"{name}: required cell is missing")
    for name, cell in cells.items():
        if name == "350+" and (len(runs_of(cell, "new")) < MIN_RUNS or len(runs_of(cell, "old")) < MIN_RUNS):
            continue
        if name in R1_CELLS:
            misses += judge_cell_r1(name, cell, limits)
        if name in R2_CELLS:
            misses += judge_cell_r2(name, cell, limits)
        if name in R6_CELLS:
            if len(runs_of(cell, "new")) < MIN_RUNS or len(runs_of(cell, "old")) < MIN_RUNS:
                misses.append(f"{name}: fewer than {MIN_RUNS} runs")
            else:
                misses += judge_cell_r6(name, cell, limits)
    return misses + judge_r3(report, limits) + judge_k1(report)


# --- subcommands: census ------------------------------------------------------------------------------

def cmd_census(args) -> int:
    sources = live_sources()
    write_json(args.out, {"taken_at": iso_now(), "taken_epoch_ns": time.time_ns(), "sources": sources})
    print(f"census: {len(sources)} sources -> {args.out}")
    return EXIT_OK


# --- subcommands: enumerate ----------------------------------------------------------------------------

NOISE_PREFIXES = ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/proc", "/sys", "/dev", "/etc", "/opt",
                  "/var/lib", "/run", "/snap")
SITE_PATTERN = re.compile(r"\.(read_text|read_bytes|open|glob|rglob|iterdir|scandir|stat|exists|is_file|is_dir|"
                          r"listdir|getmtime|getsize)\(|subprocess\.|\bopen\(|os\.stat|os\.scandir|Popen")
KIND_HINTS = {"state": r"state_root|\.json|_read_state", "binding": r"binding|engine_session",
              "transcript": r"transcript|\.jsonl|projects", "ledger": r"cost|ledger",
              "tmux_listing": r"tmux|windows|locate|subprocess", "locator_listing": r"locat|plugin|sessions"}
LOCATOR_INPUT_KIND_PREFIXES = ("other:<home>/.ccgram",)
STRACE_LINE = re.compile(r"^(?:(\d+)\s+)?(\d+\.\d+)\s+(\w+)\((.*)\)\s+=\s+(-?\d+|\?)(.*)$")


def parse_strace(text: str) -> list[dict]:
    """Syscalls of an ``-f -ttt`` strace as ``{pid, t, call, path, argv, ok, creates}``."""
    out = []
    for line in text.splitlines():
        m = STRACE_LINE.match(line)
        if not m:
            continue
        pid, stamp, call, args, result, _ = m.groups()
        strings = re.findall(r'"((?:[^"\\]|\\.)*)"', args)
        if not strings:
            continue
        argv = None
        if call == "execve":
            listed = re.search(r"\[(.*?)\]", args)
            argv = re.findall(r'"((?:[^"\\]|\\.)*)"', listed.group(1)) if listed else [strings[0]]
        out.append({"pid": int(pid) if pid else 0, "t": float(stamp), "call": call, "path": strings[0],
                    "argv": argv, "ok": result != "?" and int(result) >= 0,
                    "creates": call == "openat" and "O_CREAT" in args and result != "?" and int(result) >= 0})
    return out


def exec_kind(argv: list[str]) -> str:
    name = Path(argv[0]).name
    if name in ("tmux", "git") and len(argv) > 1:
        sub = next((a for a in argv[1:] if not a.startswith("-")), "")
        return f"{name} {sub}".strip()
    return name


def placeholder(path: str) -> str:
    home = str(agent_home())
    if path.startswith(home):
        return "<agent>" + path[len(home):]
    user = str(Path.home())
    return "<home>" + path[len(user):] if path.startswith(user) else path


def classify_trace(calls: list[dict], tree: Path) -> dict:
    """Group the opens and stats of a trace by the kind of source they read."""
    tree_prefix = str(tree)
    kinds: dict[str, dict[str, list[float]]] = {}
    execs = []
    for c in calls:
        if c["call"] == "execve" and c["ok"]:
            execs.append(c)
            continue
        path = c["path"]
        if path.startswith(NOISE_PREFIXES) or path.startswith(tree_prefix) or "__pycache__" in path \
                or path.endswith((".pyc", ".so")) or "/site-packages/" in path or "/uv/" in path \
                or "/.cache/" in path:
            continue
        kind = kind_of_path(path)
        if kind is None:
            if not path.startswith(str(Path.home())):
                continue
            parts = placeholder(path).split("/")
            depth = 3 if parts[0] == "<agent>" and parts[1:2] not in (["projects"], []) else 2
            kind = "other:" + "/".join(parts[:depth])
        kinds.setdefault(kind, {}).setdefault(path, []).append(c["t"])
    return {"kinds": kinds, "execs": execs}


def code_read_sites(tree: Path) -> list[str]:
    sites = []
    files = sorted((Path(tree) / "scripts" / "plan_dashboard").glob("*.py")) + [Path(tree) / "scripts" / "plan-dashboard.py"]
    for path in files:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if SITE_PATTERN.search(line) and not line.lstrip().startswith("#"):
                sites.append(f"{path.relative_to(tree).as_posix()}:{number}: {line.strip()[:110]}")
    return sites


def cmd_enumerate(args) -> int:
    base, state_dir = Path(args.base), Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    trace_file = state_dir / "strace-enumerate.txt"
    cache = state_dir / "cache" / "enumerate"
    cache.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    if args.from_trace:
        trace_file = Path(args.from_trace)
        trace = Trace(time.monotonic_ns())
    else:
        argv = ["strace", "-f", "-e", "trace=openat,execve,stat,newfstatat,statx", "-ttt", "-s", "400", "-o",
                str(trace_file), *dashboard_argv(base, events=True, interval=2.0)]
        trace = run_pty(argv, cwd=base, env=run_env(cache, uv_cache_dir()), events=True, max_s=args.seconds,
                        stop=lambda t: len(cycle_records(t.events, t.t0_ns)) >= 1)
    calls = parse_strace(trace_file.read_text(errors="replace"))
    if args.also_snapshot_pass:
        for name, view in (("snapshot", ["snapshot", "--attention", "--format", "json"]), ("bench", ["bench"])):
            pass_file = state_dir / f"strace-enumerate-{name}.txt"
            try:
                subprocess.run(["strace", "-f", "-e", "trace=openat,execve,stat,newfstatat,statx", "-ttt", "-s", "400",
                                "-o", str(pass_file), sys.executable, str(base / "scripts" / "plan-dashboard.py"), *view],
                               cwd=base, env=run_env(cache, uv_cache_dir()), capture_output=True, timeout=args.seconds)
            except subprocess.TimeoutExpired:
                pass
            calls += parse_strace(pass_file.read_text(errors="replace"))
    grouped = classify_trace(calls, base)
    cycles = cycle_records(trace.events, trace.t0_ns)
    windows = [(trace.wall_minus_mono_ns / 1e9 + (trace.t0_ns / 1e9) + c["start_s"],
                trace.wall_minus_mono_ns / 1e9 + (trace.t0_ns / 1e9) + c["start_s"] + c["duration_s"])
               for c in cycles]
    sites = code_read_sites(base)
    kinds_out, trace_paths = {}, {}
    for kind in [k for k in grouped["kinds"] if k.startswith(LOCATOR_INPUT_KIND_PREFIXES)]:
        grouped["kinds"].setdefault("locator_listing", {}).update(grouped["kinds"].pop(kind))
    for kind, paths in sorted(grouped["kinds"].items()):
        trace_paths[kind] = sorted(paths)
        duplicated = []
        if windows:
            low, high = windows[-1]
            for path, stamps in paths.items():
                reads = sum(1 for s in stamps if low <= s <= high)
                if reads > 1:
                    duplicated.append({"path": placeholder(path), "reads_in_cycle": reads})
        hint = re.compile(KIND_HINTS.get(kind.split(":")[0], r"."), re.I)
        kinds_out[kind] = {"files": True, "traced_files": len(paths),
                           "example": placeholder(sorted(paths)[0]),
                           "read_sites": [s for s in sites if hint.search(s)][:12],
                           "duplicated_reads": sorted(duplicated, key=lambda d: d["path"])[:20]}
    commands = {}
    for c in grouped["execs"]:
        commands.setdefault(exec_kind(c["argv"]), c["argv"])
    for kind, argv_ in sorted(commands.items()):
        if kind.split()[0] in ("tmux",):
            kinds_out["tmux_listing:" + kind] = {"files": False, "command": [Path(argv_[0]).name, *argv_[1:]],
                                                 "refreshed": "every cycle",
                                                 "read_sites": [s for s in sites if "tmux" in s.lower() or "subprocess" in s]}
    non_file = {}
    if not any(kind.split()[0] == "tmux" for kind in commands):
        non_file["tmux_listing"] = {"executed": False, "exec_kinds_seen": sorted(commands),
                                    "stands_in": "locator_listing: the locator plugin lists the bridge windows from a file"}
    fixture = {"version": 1, "non_file_sources": non_file,
               "enumerated": {"strace_calls": len(calls), "cycles_traced": len(cycles),
                              "seconds": round(time.time() - t_start, 1)},
               "kinds": kinds_out,
               "exec_kinds": sorted(commands),
               "code_read_sites": sites}
    write_json(args.out, fixture)
    write_json(state_dir / "enumeration-trace.json", {"paths": trace_paths})
    names = sorted({e.get("ev", "?") for e in trace.events})
    print(f"enumerate: {len(calls)} syscalls, {len(trace.events)} events {names}, {len(cycles)} cycles, "
          f"exit {trace.exit_code}, {len(kinds_out)} kinds -> {args.out}")
    return EXIT_OK if cycles or (args.from_trace and kinds_out) else EXIT_BAD


# --- subcommands: floors -------------------------------------------------------------------------------

def mtime_resolution(state_dir: Path) -> dict:
    """Smallest step of a file's mtime, measured by writing and re-statting a probe file under the state directory."""
    state_dir.mkdir(parents=True, exist_ok=True)
    probe = state_dir / f"mtime-probe-{os.getpid()}"
    stamps = set()
    try:
        for index in range(400):
            probe.write_bytes(str(index).encode())
            stamps.add(os.stat(probe).st_mtime_ns)
            time.sleep(0.0003)
    finally:
        probe.unlink(missing_ok=True)
    ordered = sorted(stamps)
    steps = [b - a for a, b in zip(ordered, ordered[1:])]
    value = min(steps) if steps else 1_000_000_000
    if all(s % 1_000_000_000 == 0 for s in ordered):
        value = 1_000_000_000
    dev = os.stat(state_dir).st_dev
    return {"value_ns": value, "filesystem": filesystem_of(state_dir),
            "method": "write and re-stat a probe file under the state directory, 400 writes 0.3 ms apart",
            "agent_home_same_device": os.stat(agent_home()).st_dev == dev,
            "xdg_cache_same_device": os.stat(Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))).st_dev == dev
            if Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")).exists() else None}


def filesystem_of(path: Path) -> str:
    best = ("", "unknown")
    resolved = str(Path(path).resolve())
    try:
        for line in Path("/proc/self/mounts").read_text().splitlines():
            parts = line.split()
            if len(parts) > 2 and resolved.startswith(parts[1]) and len(parts[1]) >= len(best[0]):
                best = (parts[1], parts[2])
    except OSError:
        pass
    return best[1]


def read_changed(item: dict, before: dict | None) -> int:
    """Read what a refresh of ``item`` must read: whole small files, only the appended bytes of logs."""
    try:
        with open(item["path"], "rb") as handle:
            if item["kind"] in ("transcript", "ledger") and before and item["size"] >= before["size"] \
                    and before["ino"] == item["ino"]:
                handle.seek(before["size"])
                return len(handle.read(1 << 20))
            if item["kind"] == "transcript":
                handle.seek(max(item["size"] - (1 << 20), 0))
            return len(handle.read(1 << 20 if item["kind"] == "transcript" else -1))
    except OSError:
        return 0


def tmux_command(sources: dict) -> list[str] | None:
    for name, spec in sorted(sources.get("kinds", {}).items()):
        if name.startswith("tmux_listing") and spec.get("command"):
            return spec["command"]
    return None


def live_check_floor(read_items: list[dict], recorded: dict[str, dict], tmux: list[str] | None) -> dict:
    """The least a live check does: stat every source, read each changed one, list the tmux windows."""
    t0 = time.perf_counter()
    live = [identity(i["path"], i["kind"]) for i in read_items]
    stat_s = time.perf_counter() - t0
    changed = [n for n in live if n is not None and (
        n["path"] not in recorded or any(n[k] != recorded[n["path"]][k] for k in IDENTITY_KEYS))]
    t1 = time.perf_counter()
    read_bytes = sum(read_changed(n, recorded.get(n["path"])) for n in changed)
    read_s = time.perf_counter() - t1
    tmux_s = 0.0
    if tmux:
        t2 = time.perf_counter()
        try:
            subprocess.run(tmux, capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass
        tmux_s = time.perf_counter() - t2
    return {"stat_s": stat_s, "read_s": read_s, "tmux_s": tmux_s, "total_s": stat_s + read_s + tmux_s,
            "changed": len(changed), "read_bytes": read_bytes}


def probe_start(env: dict, extra: list[str]) -> float | None:
    argv = ["uv", "run", str(PROBE), *extra]
    trace = run_pty(argv, cwd=REPO, env=env, events=False, max_s=30,
                    stop=lambda t: t.frames and t.frames[-1][1] and t.frames[-1][1]["rows"]
                    and time.monotonic_ns() - t.t0_ns > (t.frames[-1][0] + 0.3) * 1e9)
    return screen_summary(trace.frames)["first_rows_s"]


def python_start_s() -> float:
    t0 = time.perf_counter()
    subprocess.run([sys.executable, "-c", "pass"], capture_output=True)
    return time.perf_counter() - t0


def old_code_runs(base: Path, env: dict, *, events: bool, runs: int) -> dict:
    rows, marks, refresh, stream_ms = [], [], [], []
    for _ in range(runs):
        def done(t, events=events):
            s = screen_summary(t.frames)
            return s["marks_confirmed_s"] is not None and (not events or len(cycle_records(t.events, t.t0_ns)) >= 3)
        trace = run_pty(dashboard_argv(base, events=events), cwd=base, env=env, events=events, max_s=60, stop=done)
        summary = screen_summary(trace.frames)
        rows.append(summary["first_rows_s"])
        marks.append(summary["marks_confirmed_s"])
        if events:
            cycles = cycle_records(trace.events, trace.t0_ns)
            refresh.append(median(c["duration_s"] for c in cycles[1:]))
            painted = trace.event_times("rows_painted")
            if painted and summary["first_rows_s"] is not None:
                stream_ms.append(abs(painted[0] - summary["first_rows_s"]) * 1000)
    out = {"first_rows_s": spread(rows), "marks_confirmed_s": spread(marks)}
    if events:
        out["refresh_s"] = spread(refresh)
        out["stream_screen_ms"] = stream_ms
    return out


def strace_run(tree: Path, state_dir: Path, env: dict, *, new: bool, label: str, max_s: float = 25.0) -> dict:
    """One K1 run under strace: command kinds, files created, child processes between cycles."""
    out_file = state_dir / f"k1-{label}.strace"
    argv = ["strace", "-f", "-e", "trace=openat,execve,stat,newfstatat,statx", "-ttt", "-s", "400", "-o",
            str(out_file), *dashboard_argv(tree, events=True, interval=2.0)]
    snapshot_dir = Path(env["XDG_CACHE_HOME"]) / "plan-dashboard"
    trace = run_pty(argv, cwd=tree, env=env, events=True, max_s=max_s, sample_children=True,
                    stop=lambda t: len(cycle_records(t.events, t.t0_ns)) >= 4)
    calls = parse_strace(out_file.read_text(errors="replace"))
    out_file.unlink(missing_ok=True)
    execs = sorted({exec_kind(c["argv"]) for c in calls if c["call"] == "execve" and c["ok"]})
    created = sorted({c["path"] for c in calls if c["creates"] and not c["path"].startswith(("/dev", "/proc", "/tmp"))
                      and "__pycache__" not in c["path"] and "/uv/" not in c["path"]
                      and not c["path"].startswith(str(snapshot_dir))})
    cycles = cycle_records(trace.events, trace.t0_ns)
    between = []
    for a, b in zip(cycles, cycles[1:]):
        gap = [n for t, n in trace.child_samples if a["start_s"] + a["duration_s"] < t < b["start_s"]]
        if gap:
            between.append(max(gap))
    return {"exec_kinds": execs, "created_files": [placeholder(p) for p in created],
            "children_between_cycles": between, "arm": "new" if new else "old"}


def cmd_floors(args) -> int:
    state_dir, base = Path(args.state_dir), Path(args.base)
    state_dir.mkdir(parents=True, exist_ok=True)
    status = git(base, "status", "--porcelain")
    head = git(base, "rev-parse", "HEAD").stdout.strip()
    if status.returncode != 0 or status.stdout.strip() or not head \
            or git(base, "cat-file", "-e", f"{head}^{{tree}}").returncode != 0:
        print("floors: the base tree is missing or has uncommitted changes", file=sys.stderr)
        return EXIT_BAD
    base_hash = tree_sha256(base)
    if base_hash != committed_tree_sha256(base, head):
        print("floors: the base tree differs from its commit", file=sys.stderr)
        return EXIT_BAD
    census = load_json(args.census)
    trace_file = Path(args.trace) if args.trace else state_dir / "enumeration-trace.json"
    traced = load_json(trace_file)["paths"] if trace_file.exists() else {}
    live = live_sources()
    items = read_set(traced, live)
    changed = changed_since(census["sources"], items)
    changed_live = [c for c in changed if not c.get("deleted")]
    if len(changed_live) < MIN_CHANGED:
        print(f"floors: only {len(changed_live)} read-set sources changed since the census, need {MIN_CHANGED}",
              file=sys.stderr)
        return EXIT_BAD
    sources = load_json(args.sources)
    tmux = tmux_command(sources)
    recorded = {i["path"]: i for i in census["sources"]}
    env = run_env(state_dir / "cache" / "floors", uv_cache_dir())
    snapshot_like = state_dir / "snapshot-like.json"
    write_json(snapshot_like, [{k: v for k, v in i.items()} for i in items])
    reps = args.runs
    probe_plain = [probe_start(env, []) for _ in range(reps)]
    probe_snapshot = [probe_start(env, ["--read", str(snapshot_like)]) for _ in range(reps)]
    py_s = median(python_start_s() for _ in range(reps))
    checks = [live_check_floor(items, recorded, tmux) for _ in range(reps)]
    r1 = median(probe_snapshot) + py_s
    r2_checks = median(c["total_s"] for c in checks)
    r2 = r1 + r2_checks
    per_changed = median(c["read_s"] / max(c["changed"], 1) for c in checks)
    est_400 = r1 + median(c["stat_s"] for c in checks) + per_changed * 400 + median(c["tmux_s"] for c in checks)
    r3_runs = []
    for _ in range(R3_MIN_RUNS):
        seen = {i["path"]: i for i in (identity(x["path"], x["kind"]) for x in items) if i}
        durations = []
        for _ in range(R3_CYCLES):
            time.sleep(args.cycle_s)
            cycle = live_check_floor(items, seen, tmux)
            durations.append(cycle["total_s"])
            seen = {i["path"]: i for i in (identity(x["path"], x["kind"]) for x in items) if i}
        r3_runs.append(median(durations))
    old_off = old_code_runs(base, env, events=False, runs=reps)
    old_on = old_code_runs(base, env, events=True, runs=reps)
    stream_ms = old_on.get("stream_screen_ms", [])
    old_proc = strace_run(base, state_dir, env, new=False, label="floors-old")
    report = {
        "version": 1, "taken_at": iso_now(), "instrument_sha256": instrument_sha256(),
        "base_commit": head, "base_tree_sha256": base_hash,
        "census": {"taken_at": census.get("taken_at"), "sources": len(census["sources"])},
        "changed_count": len(changed_live),
        "changed_by_kind": {k: sum(1 for c in changed_live if c["kind"] == k) for k in sorted({c["kind"] for c in changed_live})},
        "read_set": {k: sum(1 for i in items if i["kind"] == k) for k in sorted({i["kind"] for i in items})},
        "floors": {
            "R1": {"median_s": r1, "runs_s": probe_snapshot, "probe_plain_median_s": median(probe_plain),
                   "python_start_s": py_s,
                   "composition": "start probe with a snapshot-sized file read, first row on the screen, plus python start"},
            "R2": {"median_s": r2, "live_check_floor_s": r2_checks, "changed": checks[0]["changed"],
                   "composition": "R1 floor plus stat of every read-set source, read of each changed one, tmux listing"},
            "R3": {"median_s": median(r3_runs), "runs_median_s": r3_runs,
                   "composition": "per cycle: stat of every read-set source, read of each source changed since the last cycle, tmux listing"}},
        "estimate_400_floor_s": est_400,
        "mtime_resolution": mtime_resolution(state_dir),
        "stream_screen_max_ms": max(stream_ms) if stream_ms else None,
        "stream_screen_samples_ms": stream_ms,
        "old_code": {"events_off": old_off, "events_on": {k: v for k, v in old_on.items() if k != "stream_screen_ms"}},
        "old_code_processes": old_proc,
    }
    write_json(args.out, report)
    print(f"floors: R1 {r1:.3f}s R2 {r2:.3f}s R3 {report['floors']['R3']['median_s']:.3f}s, "
          f"changed {len(changed_live)}, stream-screen max {report['stream_screen_max_ms']} ms -> {args.out}")
    return EXIT_OK


def verify_floors_report(report, limits: dict, sources) -> tuple[int, list[str]]:
    """(exit code, reasons) for a floors report."""
    bad = []
    if not isinstance(report, dict):
        return EXIT_BAD, ["floors.json is malformed"]
    needed = ("floors", "changed_count", "instrument_sha256", "base_commit", "base_tree_sha256")
    if any(k not in report for k in needed):
        bad.append("floors.json lacks " + ", ".join(k for k in needed if k not in report))
    res = report.get("mtime_resolution")
    if not isinstance(res, dict) or not isinstance(res.get("value_ns"), int) or res["value_ns"] <= 0:
        bad.append("floors.json lacks mtime_resolution")
    if isinstance(report.get("changed_count"), int) and report["changed_count"] < MIN_CHANGED:
        bad.append(f"changed_count {report['changed_count']} is under {MIN_CHANGED}")
    if report.get("instrument_sha256") != instrument_sha256():
        bad.append("instrument_sha256 differs from the running harness's files")
    floors = report.get("floors")
    for name in ("R1", "R2", "R3"):
        if not isinstance(floors, dict) or not isinstance(floors.get(name), dict) \
                or not isinstance(floors[name].get("median_s"), (int, float)):
            bad.append(f"floors.json lacks the {name} floor")
    if not isinstance(sources, dict) or not isinstance(sources.get("kinds"), dict) or not sources["kinds"]:
        bad.append("the sources fixture is malformed")
    if bad:
        return EXIT_BAD, bad
    misses = []
    for name in ("R1", "R2", "R3"):
        if floors[name]["median_s"] >= limits[name]:
            misses.append(f"{name} floor {floors[name]['median_s']:.4f} s leaves no room under the limit {limits[name]} s")
    stream = report.get("stream_screen_max_ms")
    if not isinstance(stream, (int, float)) or stream > STREAM_SCREEN_MAX_MS:
        misses.append(f"stream-to-screen bound {stream} ms exceeds {STREAM_SCREEN_MAX_MS} ms")
    known = set(sources["kinds"])
    unknown = sorted(k for k in report.get("read_set", {}) if k not in known)
    if unknown:
        misses.append(f"the read set has kinds the sources fixture does not list: {unknown}")
    return (EXIT_MISS, misses) if misses else (EXIT_OK, [])


def parse_limits(pairs: list[str]) -> dict[str, float]:
    out = {}
    for pair in pairs or []:
        name, _, value = pair.partition("=")
        if name not in DEFAULT_LIMITS or not value:
            raise SystemExit(f"bad --limit {pair!r}; use R1, R2, R3 or R6 = number")
        out[name] = float(value)
    return out


def cmd_verify_floors(args) -> int:
    try:
        report, sources = load_json(args.report), load_json(args.sources)
    except (OSError, ValueError) as err:
        print(f"verify-floors: unreadable input: {err}", file=sys.stderr)
        return EXIT_BAD
    code, reasons = verify_floors_report(report, limits_with(parse_limits(args.limit)), sources)
    for reason in reasons:
        print(f"verify-floors: {reason}", file=sys.stderr)
    if code == EXIT_OK:
        print("verify-floors: ok")
    return code


# --- subcommands: verify-acceptance -------------------------------------------------------------------

def cmd_verify_acceptance(args) -> int:
    try:
        report, floors = load_json(args.report), load_json(args.floors)
    except (OSError, ValueError) as err:
        print(f"verify-acceptance: unreadable input: {err}", file=sys.stderr)
        return EXIT_BAD
    bad = validate_report(report, floors, tree=Path(args.tree), old_tree=Path(args.old_tree) if args.old_tree else None)
    if bad:
        for reason in bad:
            print(f"verify-acceptance: {reason}", file=sys.stderr)
        return EXIT_BAD
    misses = judge_report(report, limits_with(parse_limits(args.limit)))
    for miss in misses:
        print(f"verify-acceptance: {miss}", file=sys.stderr)
    if not misses:
        print("verify-acceptance: ok")
    return EXIT_MISS if misses else EXIT_OK


# --- subcommands: campaign and status -----------------------------------------------------------------

class Campaign:
    """Alternating old/new runs over bands of changed sources, a few hours long. The runner is injected so the
    scheduling can be tested without a terminal."""

    def __init__(self, state_dir: Path, old: Path, new: Path, *, save_every: float, until: float,
                 watch: list[dict], runner, sleep=time.sleep, clock=time.time, log=print):
        self.state_dir, self.old, self.new = Path(state_dir), Path(old), Path(new)
        self.save_every, self.until = save_every, until
        self.watch, self.runner, self.sleep, self.clock, self.log = watch, runner, sleep, clock, log
        self.copies: list[dict] = []
        self.runs: dict[str, list[dict]] = {c: [] for c in (*BANDS, *NO_BAND_CELLS)}
        self.dropped = {c: 0 for c in self.runs}
        self.r3: list[dict] = []
        self.k1: list[dict] = []
        self.largest = 0
        self.last_save = 0.0

    def metrics(self, cell: str) -> tuple[str, ...]:
        base = ("first_rows_s",) if cell in R1_CELLS else ()
        if cell in R2_CELLS:
            base += ("marks_confirmed_s",)
        if cell in R6_CELLS:
            base += ("rows_confirmed_s", "marks_confirmed_s")
        return tuple(dict.fromkeys(base))

    def runs_of(self, cell: str, arm: str) -> list[dict]:
        return [r for r in self.runs[cell] if r["arm"] == arm]

    def cell_done(self, cell: str) -> bool:
        return cell_complete(self.runs_of(cell, "old"), self.runs_of(cell, "new"), self.metrics(cell))

    def pick_copy(self, cell: str, live: list[dict]):
        """A saved snapshot copy whose changed count now falls in ``cell``'s band, with that count."""
        low, high = BANDS[cell]
        best = None
        for copy in self.copies:
            count = len(contract_changes(copy, live_sources()))
            self.largest = max(self.largest, count)
            if band_of(count) == cell and (best is None or count < best[1]):
                best = (copy, count)
        return best

    def step(self) -> bool:
        """One scheduling step; ``False`` when there was nothing to run."""
        now = self.clock()
        if not self.copies or now - self.last_save >= self.save_every:
            self.last_save = now
            copy = self.runner("save", None)
            if copy:
                self.copies.append(copy)
                self.log(f"saved snapshot copy {len(self.copies)}")
        live = self.runner("live", None)
        for cell in (*NO_BAND_CELLS, "near-zero", *[c for c in BANDS if c != "near-zero"]):
            if self.cell_done(cell):
                continue
            arm = next_arm(self.runs[cell])
            if cell in NO_BAND_CELLS:
                record = self.runner("run", {"cell": cell, "arm": arm, "copy": None, "live": live})
            elif cell == "near-zero":
                fresh = self.runner("save", None)
                if not fresh:
                    continue
                record = self.runner("run", {"cell": cell, "arm": arm, "copy": fresh, "live": self.runner("live", None)})
            else:
                found = self.pick_copy(cell, live)
                if not found:
                    continue
                record = self.runner("run", {"cell": cell, "arm": arm, "copy": found[0], "live": live})
            if record is None:
                continue
            if not in_band(cell, record.get("changed_count")):
                self.dropped[cell] += 1
                self.log(f"dropped a {cell} run: changed_count {record.get('changed_count')} outside the band")
                continue
            self.runs[cell].append(record)
            return True
        for arm in ("old", "new"):
            have = [r for r in self.r3 if r["arm"] == arm]
            other = [r for r in self.r3 if r["arm"] != arm]
            if len(have) < R3_MIN_RUNS and len(have) <= len(other):
                self.r3.append(self.runner("r3", {"arm": arm}))
                return True
        for arm in ("old", "new"):
            if len([r for r in self.k1 if r["arm"] == arm]) < K1_MIN_RUNS:
                self.k1.append(self.runner("k1", {"arm": arm, "index": len(self.k1)}))
                return True
        return False

    def finished(self) -> bool:
        required = all(self.cell_done(c) for c in REQUIRED_CELLS)
        return required and len(self.r3) >= 2 * R3_MIN_RUNS and len(self.k1) >= 2 * K1_MIN_RUNS

    def run(self) -> dict:
        while self.clock() < self.until and not self.finished():
            if not self.step():
                self.log("waiting for the changed-source counts to reach the next band")
                self.sleep(60)
        return self.report()

    def report(self) -> dict:
        cells = {}
        for name, runs in self.runs.items():
            if runs or name in REQUIRED_CELLS:
                cells[name] = {"band": list(BANDS[name]) if name in BANDS else None, "runs": runs,
                               "dropped_out_of_band": self.dropped[name]}
        return {"version": 1, "taken_at": iso_now(), "instrument_sha256": instrument_sha256(),
                "new_tree_sha256": tree_sha256(self.new), "old_tree_sha256": tree_sha256(self.old),
                "largest_changed_count": self.largest, "cells": cells,
                "r3": {"runs": self.r3}, "k1": {"old": [r for r in self.k1 if r["arm"] == "old"],
                                                "new": [r for r in self.k1 if r["arm"] == "new"],
                                                "added_files": []}}


class RealRunner:
    """Runs the real dashboard trees on the terminal; everything it writes is under the state directory."""

    def __init__(self, state_dir: Path, old: Path, new: Path, contract: dict, watch_paths: list[str]):
        self.state_dir, self.old, self.new, self.contract = state_dir, old, new, contract
        self.uv_cache = uv_cache_dir()
        self.watch_paths = watch_paths
        self.copy_dir = state_dir / "snapshots"
        self.copy_dir.mkdir(parents=True, exist_ok=True)
        self.counter = 0

    def cache(self, arm: str) -> Path:
        path = self.state_dir / "cache" / arm
        path.mkdir(parents=True, exist_ok=True)
        return path

    def snapshot_path(self, arm: str) -> Path:
        return self.cache(arm) / "plan-dashboard" / "snapshot.json"

    def env(self, arm: str) -> dict:
        return run_env(self.cache(arm), self.uv_cache)

    def __call__(self, what: str, arg):
        return getattr(self, "do_" + what)(arg)

    def do_live(self, _):
        return read_set({"transcript": [], "state": [], "binding": [], "ledger": []}, live_sources()) \
            if not self.watch_paths else [i for i in live_sources() if i["path"] in set(self.watch_paths)
                                          or i["kind"] in ("state", "binding", "ledger")]

    def do_save(self, _):
        snapshot = self.snapshot_path("new")
        snapshot.unlink(missing_ok=True)
        sources = self.do_live(None)
        trace = run_pty(dashboard_argv(self.new, events=True), cwd=self.new, env=self.env("new"), events=True,
                        max_s=60, terminate=True,
                        stop=lambda t: bool(t.event_times("live_check_end")))
        if not trace.event_times("snapshot_written") or not snapshot.exists():
            self.log_failure("save run emitted no snapshot_written")
            return None
        self.counter += 1
        keep = self.copy_dir / f"copy-{self.counter:04d}.json"
        keep.write_bytes(snapshot.read_bytes())
        try:
            recorded = live_sources()
        except (OSError, ValueError):
            recorded = sources
        return {"file": str(keep), "taken_at": iso_now(), "sources": recorded}

    def log_failure(self, text: str) -> None:
        print(f"{iso_now()} {text}", flush=True)

    def do_run(self, job):
        cell, arm, copy, live = job["cell"], job["arm"], job["copy"], job["live"]
        snapshot = self.snapshot_path(arm)
        snapshot.unlink(missing_ok=True)
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        changed_count, changes = None, {}
        if cell == "damaged":
            snapshot.write_bytes(b'{"version": 1, "body_sha256": "x", "sections": {"all": [')
        elif copy is not None:
            snapshot.write_bytes(Path(copy["file"]).read_bytes())
            live_now = self.do_live(None)
            changed = contract_changes(copy, live_sources())
            changed_count = len(changed)
            changes = changed_map(changed, bindings_map(agent_home() / "agentctl" / "engine-session-bindings"))
        tree = self.new if arm == "new" else self.old
        events = arm == "new"
        done = lambda t: (screen_summary(t.frames)["marks_confirmed_s"] is not None  # noqa: E731
                          and (not events or bool(t.event_times("live_check_end"))))
        trace = run_pty(dashboard_argv(tree, events=events), cwd=tree, env=self.env(arm), events=events,
                        max_s=90, stop=done)
        load1 = os.getloadavg()[0]
        record = summarize_run({"frames": trace.frames, "events": trace.events, "t0_ns": trace.t0_ns},
                               arm=arm, changed_count=changed_count, changes=changes, load1=load1)
        record["cell"] = cell
        return record

    def do_r3(self, job):
        arm = job["arm"]
        tree = self.new if arm == "new" else self.old
        watcher = ContractWatcher()
        due: dict[int, list[str]] = {}

        def on_event(trace, event):
            if event.get("ev") == "cycle_start":
                wall_ns = event["t_ns"] + trace.wall_minus_mono_ns
                due[event["cycle"]] = watcher.poll(wall_ns, ContractWatcher.screen_ids(trace.frames))

        trace = run_pty(dashboard_argv(tree, events=True), cwd=tree, env=self.env(arm), events=True, max_s=180,
                        on_event=on_event,
                        stop=lambda t: len(cycle_records(t.events, t.t0_ns)) >= R3_CYCLES + 2)
        cycles = check_cycle_coverage(cycle_records(trace.events, trace.t0_ns), due)
        return r3_run_record(arm, cycles)

    def do_k1(self, job):
        arm = job["arm"]
        tree = self.new if arm == "new" else self.old
        return strace_run(tree, self.state_dir, self.env(arm), new=arm == "new", label=f"{arm}-{job['index']}")


def cmd_campaign(args) -> int:
    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    log_path = state_dir / "campaign.log"
    pid_path = state_dir / "campaign.pid"
    until = args.until if isinstance(args.until, (int, float)) else parse_until(args.until)
    if args.detach:
        pid = os.fork()
        if pid:
            print(f"campaign: started pid {pid}, log {log_path}")
            return EXIT_OK
        os.setsid()
        log = open(log_path, "a", buffering=1)
        os.dup2(log.fileno(), 1)
        os.dup2(log.fileno(), 2)
    pid_path.write_text(str(os.getpid()))
    print(f"{iso_now()} campaign start until {datetime.fromtimestamp(until, timezone.utc).isoformat()}", flush=True)
    trace_file = state_dir / "enumeration-trace.json"
    watch = sorted({p for paths in (load_json(trace_file)["paths"].values() if trace_file.exists() else [])
                    for p in paths})
    runner = RealRunner(state_dir, Path(args.old), Path(args.new), load_json(CONTRACT_PATH), watch)
    campaign = Campaign(state_dir, Path(args.old), Path(args.new), save_every=args.save_every, until=until,
                        watch=[], runner=runner,
                        log=lambda m: print(f"{iso_now()} {m}", flush=True))
    report = campaign.run()
    report["k1"]["added_files"] = added_files(Path(args.new), load_json(args.floors)["base_commit"]) \
        if args.floors and Path(args.floors).exists() else []
    write_json(state_dir / "acceptance.json", report)
    print(f"campaign done {iso_now()}", flush=True)
    pid_path.unlink(missing_ok=True)
    return EXIT_OK


def parse_until(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() \
            if datetime.fromisoformat(text).tzinfo is None else datetime.fromisoformat(text).timestamp()


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def campaign_status(state_dir: Path) -> str:
    log = state_dir / "campaign.log"
    if not log.exists():
        return "not started"
    tail = log.read_text(errors="replace").splitlines()[-5:]
    if any(line.split(" ", 1)[-1].startswith("campaign done") for line in tail):
        return "done"
    pid_path = state_dir / "campaign.pid"
    if pid_path.exists() and pid_alive(int(pid_path.read_text() or 0)):
        return "running"
    return "stopped"


def cmd_status(args) -> int:
    print(campaign_status(Path(args.state_dir)))
    return EXIT_OK


def cmd_selftest(args) -> int:
    """The start probe under the pseudo-terminal: header, a row and the old-marking-free state are read."""
    state_dir = Path(args.state_dir)
    env = run_env(state_dir / "cache" / "selftest", uv_cache_dir())
    first = probe_start(env, [])
    print(f"selftest: start probe first row at {first} s")
    if args.tree is None:
        return EXIT_OK if first is not None else EXIT_MISS
    trace = run_pty(dashboard_argv(args.tree, events=True), cwd=args.tree, env=env, events=True, max_s=args.seconds,
                    stop=lambda t: len(cycle_records(t.events, t.t0_ns)) >= 3)
    names = sorted({e.get("ev", "?") for e in trace.events})
    summary = screen_summary(trace.frames)
    print(f"selftest: dashboard events {names}, cycles {len(cycle_records(trace.events, trace.t0_ns))}, "
          f"first rows {summary['first_rows_s']} s, marks confirmed {summary['marks_confirmed_s']} s, "
          f"exit {trace.exit_code}, unparsed stderr lines {trace.bad_lines[:3]}")
    print("selftest: screen tail " + json.dumps([l.rstrip() for l in trace.display if l.strip()][:6]))
    return EXIT_OK if first is not None and trace.events else EXIT_MISS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plan_dashboard_harness", description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR_DEFAULT)
    sub = parser.add_subparsers(dest="command", required=True)
    census = sub.add_parser("census")
    census.add_argument("--out", type=Path, required=True)
    census.set_defaults(func=cmd_census)
    enum = sub.add_parser("enumerate")
    enum.add_argument("--base", type=Path, required=True)
    enum.add_argument("--out", type=Path, required=True)
    enum.add_argument("--seconds", type=float, default=120.0)
    enum.add_argument("--from-trace", type=Path, default=None,
                      help="classify an existing strace output (no cycle windows) instead of running the dashboard")
    enum.add_argument("--also-snapshot-pass", action="store_true",
                      help="add the reads of one `snapshot --attention` and one `bench` pass (they end, unlike a TUI cycle on a large state root)")
    enum.set_defaults(func=cmd_enumerate)
    floors = sub.add_parser("floors")
    floors.add_argument("--census", type=Path, required=True)
    floors.add_argument("--base", type=Path, required=True)
    floors.add_argument("--out", type=Path, required=True)
    floors.add_argument("--sources", type=Path, default=SOURCES_PATH)
    floors.add_argument("--trace", type=Path, default=None)
    floors.add_argument("--runs", type=int, default=MIN_RUNS)
    floors.add_argument("--cycle-s", type=float, default=2.0)
    floors.set_defaults(func=cmd_floors)
    camp = sub.add_parser("campaign")
    camp.add_argument("--old", type=Path, required=True)
    camp.add_argument("--new", type=Path, required=True)
    camp.add_argument("--until", required=True, help="epoch seconds or an ISO time (UTC when naive)")
    camp.add_argument("--save-every", type=float, default=3600.0)
    camp.add_argument("--floors", type=Path, default=None)
    camp.add_argument("--detach", action="store_true")
    camp.set_defaults(func=cmd_campaign)
    sub.add_parser("status").set_defaults(func=cmd_status)
    selftest = sub.add_parser("selftest")
    selftest.add_argument("--tree", type=Path, default=None, help="also run this tree's dashboard with --events")
    selftest.add_argument("--seconds", type=float, default=60.0)
    selftest.set_defaults(func=cmd_selftest)
    vf = sub.add_parser("verify-floors")
    vf.add_argument("--report", type=Path, required=True)
    vf.add_argument("--sources", type=Path, default=SOURCES_PATH)
    vf.add_argument("--limit", action="append", default=[])
    vf.set_defaults(func=cmd_verify_floors)
    va = sub.add_parser("verify-acceptance")
    va.add_argument("--report", type=Path, default=STATE_DIR_DEFAULT / "acceptance.json")
    va.add_argument("--floors", type=Path, default=STATE_DIR_DEFAULT / "floors.json")
    va.add_argument("--tree", type=Path, default=Path.cwd())
    va.add_argument("--old-tree", type=Path, default=None)
    va.add_argument("--limit", action="append", default=[])
    va.set_defaults(func=cmd_verify_acceptance)
    return parser


def main(argv=None) -> int:
    global EVENTS_DIR
    args = build_parser().parse_args(argv)
    EVENTS_DIR = Path(args.state_dir) / "events"
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
