"""Read-only disk-usage report: what holds a filesystem, who names it, and whether a reaper reclaims it.

Difficulty removed: a full volume was attributed by hand (ad-hoc `du`, memory of what each cache is),
so nobody could say how much of "Used" was understood, which of it an existing reaper reclaims, or
how much stayed unknown. This module walks ONE filesystem under idle priority within a deadline,
names each entry from a table, derives its reaper status by one rule over the registry the report
records, and states the unknown remainder in three parts.

Read-only: the only writes are the two dated output files, created exclusively (never overwritten).
Nothing is deleted, moved, truncated or followed through a symlink; no reaper setting is changed.

Units: bytes, allocated size (`du` default); GiB = 2^30 bytes.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Callable

from . import plugin_dir

SCHEMA = "disk-usage-report/1"
UNITS = "bytes; GiB=2^30 bytes"
GIB = 1 << 30
BOUND_LIMIT_PCT = 10.0
MAX_DEADLINE_MIN = 60
DEFAULT_MIN_BYTES = GIB
DEFAULT_MAX_DEPTH = 4
PLUGIN_ENV_VAR = "CLAUDE_REAPER_PLUGIN_DIR"
PLUGIN_DIRNAME = "reaper-plugins"
MACHINE_TABLE_RELPATH = "disk-report/names.toml"
DEFAULT_TABLE = Path(__file__).resolve().parent / "disk_report_names.default.toml"
REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_FILES = ("scripts/disk-usage-report.py", "scripts/lib/disk_report.py")
DU_PREFIX = ["ionice", "-c3", "nice", "-n19", "du", "-x", "-B1"]
KINDS = ("cache", "checkout", "tool-data", "core-state", "os", "system-logs", "service-data",
         "container", "swap", "user-files", "temp")
STATUS_WORDS = {
    "none": "no reaper covers it",
    "not-registered": "named reaper is not registered on this machine",
    "report-only": "a reaper only reports it and reclaims nothing",
    "registered-opt-in-off": "reaper registered, its opt-in is off",
    "registered-unconfigured": "reaper registered, a needed parameter is missing",
    "reclaimed": "reclaimed by an enabled reaper",
}
TEMP_TOP_N = 10
SANDBOX_TEMP_ROOT = "/tmp"

DuRunner = Callable[[list, float], "tuple[bytes, str, bool]"]


class ReportError(Exception):
    """A refusal to produce a report (bad option, missing ionice, unusable registry)."""


def sha256_file(path: "str | Path") -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tool_sha256() -> "dict[str, str]":
    return {rel: sha256_file(REPO_ROOT / rel) for rel in TOOL_FILES}


# --- naming table -----------------------------------------------------------------------------

def _expand(path: str, home: str) -> str:
    if path == "~":
        return home
    if path.startswith("~/"):
        return home.rstrip("/") + path[1:]
    return path


def load_names(paths: "list[Path]", home: str) -> "tuple[list[dict], list[dict], list[dict]]":
    """Rows and exclusions of the given tables, later tables overriding earlier rows of the same path.

    Returns (rows, excludes, sources); a source is {path, sha256}. A missing later table is skipped.
    """
    rows: "dict[str, dict]" = {}
    excludes: "dict[str, dict]" = {}
    sources = []
    for index, path in enumerate(paths):
        if not path.is_file():
            if index == 0:
                raise ReportError(f"naming table not found: {path}")
            continue
        with open(path, "rb") as handle:
            table = tomllib.load(handle)
        sources.append({"path": str(path), "sha256": sha256_file(path)})
        for row in table.get("entry", []):
            expanded = _expand(str(row["path"]), home).rstrip("/") or "/"
            rows[expanded] = {**row, "path": expanded}
        for row in table.get("exclude", []):
            expanded = _expand(str(row["path"]), home).rstrip("/") or "/"
            excludes[expanded] = {**row, "path": expanded}
    return list(rows.values()), list(excludes.values()), sources


def machine_table_path() -> Path:
    return plugin_dir.resolve_plugin_dir(PLUGIN_ENV_VAR, PLUGIN_DIRNAME) / MACHINE_TABLE_RELPATH


# --- reaper registry and the one status rule ---------------------------------------------------

def reaper_status(entry: dict, registry: dict) -> str:
    """The first matching status, in the order listed; mirrored independently by the plan's check."""
    reaper = entry.get("reaper")
    if not reaper:
        return "none"
    if reaper not in registry["discovered"]:
        return "not-registered"
    if entry.get("reaper_mode") == "report-only":
        return "report-only"
    key = entry.get("opt_in_key")
    has_key = bool(key) and key != "none"
    if has_key and registry["opt_in"].get(key) is not True:
        return "registered-opt-in-off"
    params = registry["params"].get(key if has_key else reaper, {})
    if any(p not in params for p in entry.get("needs_params", [])):
        return "registered-unconfigured"
    return "reclaimed"


def load_registry(rows: "list[dict]") -> dict:
    """Discovered reapers from `hook-reaper.py --list`; opt-in flags and parameters from the opt-in file."""
    from reaper import opt_in, registry

    # The runner's `--list` calls this same discovery; calling it here keeps the report from
    # writing the runner's telemetry into the agent home.
    skipped: "list[str]" = []
    found = registry.discover(Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()), warn=skipped.append)
    if skipped:
        raise ReportError(f"a reaper module was skipped, the registry would be incomplete: {skipped[0]}")
    discovered = {reaper.name: reaper.layer for reaper in found}
    flags: "dict[str, bool]" = {}
    param_names = set()
    try:
        with open(opt_in.opt_in_path(), "rb") as handle:
            table = tomllib.load(handle)
    except (OSError, ValueError):
        table = {}
    for key, value in table.items():
        if isinstance(value, bool):
            flags[key] = value
    if isinstance(table.get("params"), dict):
        param_names.update(table["params"])
    for row in rows:
        key = row.get("opt_in_key")
        has_key = bool(key) and key != "none"
        if has_key:
            flags.setdefault(key, opt_in.enabled(key))
        if row.get("reaper"):
            param_names.add(key if has_key else row["reaper"])
    return {
        "discovered": discovered,
        "opt_in": flags,
        "params": {name: opt_in.params(name) for name in sorted(param_names)},
    }


# --- mounts and du roots ----------------------------------------------------------------------

def _unescape_mount_field(field: str) -> str:
    out = []
    i = 0
    while i < len(field):
        if field[i] == "\\" and i + 4 <= len(field) and field[i + 1:i + 4].isdigit():
            out.append(chr(int(field[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return "".join(out)


def parse_mountinfo(text: str) -> "list[tuple[str, str]]":
    """(mount point, fstype) per mountinfo line."""
    mounts = []
    for line in text.splitlines():
        fields = line.split()
        if "-" not in fields[6:]:
            continue
        sep = fields.index("-", 6)
        if len(fields) < sep + 2:
            continue
        mounts.append((_unescape_mount_field(fields[4]), fields[sep + 1]))
    return mounts


def _under(path: str, base: str) -> bool:
    return path != base and path.startswith(base.rstrip("/") + "/")


def _glob_escape(path: str) -> str:
    return "".join("\\" + ch if ch in "*?[\\" else ch for ch in path)


def chain_dirs(root: str, home: str) -> "list[str]":
    """root, then each directory from root down to home when home lies inside root."""
    chain = [root]
    if home == root or not _under(home, root):
        return chain
    here = root
    for part in home[len(root):].strip("/").split("/"):
        here = os.path.join(here, part)
        chain.append(here)
    return chain


def plan_walk(root: str, home: str, mounts: "list[tuple[str, str]]", excludes: "list[dict]") -> dict:
    """The du roots (children of root, of home and of home's ancestors), exclusions, FUSE mounts.

    `du -x` does not test its own operand's device, so an operand that is a mount point of any type,
    sits on another device, or is an excluded path is never made a root. root itself and home with
    its ancestors are never roots; their children are.
    """
    root_dev = os.lstat(root).st_dev
    mount_points = {mp for mp, _ in mounts if _under(mp, root)}
    fuse = sorted(mp for mp, fstype in mounts if _under(mp, root) and fstype.startswith("fuse"))
    exclude_paths = {x["path"] for x in excludes if _under(x["path"], root) and os.path.lexists(x["path"])}
    skip = sorted(mount_points | exclude_paths)
    chain = chain_dirs(root, home)
    roots: "list[str]" = []
    excluded = set(skip)
    unreadable: "list[str]" = []
    for directory in chain:
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            unreadable.append(directory)
            continue
        for name in names:
            child = os.path.join(directory, name)
            if child in chain:
                continue
            try:
                dev = os.lstat(child).st_dev
            except OSError:
                unreadable.append(child)
                continue
            under_exclude = any(child == p or _under(child, p) for p in exclude_paths)
            if child in mount_points or under_exclude or dev != root_dev:
                excluded.add(child)
                continue
            roots.append(child)
    return {"roots": roots, "excluded": sorted(excluded), "fuse_mounts": fuse, "skip": skip,
            "chain": chain, "unreadable": unreadable}


def chain_inode_entries(chain: "list[str]", root: str, home: str) -> "list[dict]":
    """Own allocated bytes of home's ancestors below root (their children are measured as roots)."""
    entries = []
    for directory in chain:
        if directory == root:
            continue
        size = os.lstat(directory).st_blocks * 512
        entries.append(_entry(
            directory, size, {"label": "the directory itself, children listed on their own",
                              "kind": "user-files" if directory == home else "os"}))
    return entries


# --- du ---------------------------------------------------------------------------------------

def run_du(argv: list, timeout_s: float) -> "tuple[bytes, str, bool]":
    """Run argv; (stdout, stderr, timed_out). A timeout keeps the partial output."""
    env = {**os.environ, "LC_ALL": "C"}
    try:
        done = subprocess.run(argv, capture_output=True, timeout=max(timeout_s, 0.1), check=False, env=env)
    except subprocess.TimeoutExpired as exc:
        return exc.stdout or b"", (exc.stderr or b"").decode("utf-8", "replace"), True
    return done.stdout, done.stderr.decode("utf-8", "replace"), False


def parse_du(stdout: bytes) -> "dict[str, int]":
    sizes: "dict[str, int]" = {}
    for record in stdout.split(b"\0"):
        if not record or b"\t" not in record:
            continue
        size, _, path = record.partition(b"\t")
        try:
            sizes[os.fsdecode(path)] = int(size)
        except ValueError:
            continue
    return sizes


def du_error_paths(stderr: str) -> "tuple[list[str], list[str]]":
    """Paths named in du's `cannot ... '<path>': <reason>` lines, and the lines it could not parse."""
    paths, other = [], []
    for line in stderr.splitlines():
        if not line.strip():
            continue
        start, end = line.find("'"), line.rfind("': ")
        if line.startswith("du: ") and 0 <= start < end:
            paths.append(line[start + 1:end])
        else:
            other.append(line)
    return paths, other


# --- selection of entries ---------------------------------------------------------------------

def _entry(path: str, size: int, row: dict) -> dict:
    reaper = row.get("reaper") or None
    return {
        "path": path,
        "bytes": size,
        "label": row["label"],
        "kind": row["kind"],
        "covers": (row.get("covers") or "whole") if reaper else "none",
        "reaper": reaper,
        "reaper_mode": (row.get("reaper_mode") or "reclaims") if reaper else None,
        "opt_in_key": row.get("opt_in_key") if reaper else None,
        "needs_params": list(row.get("needs_params", [])) if reaper else [],
        "condition": row.get("condition", "") if reaper else "",
    }


class Selector:
    """Splits du's per-directory lines into disjoint named entries and unnamed (c) entries."""

    def __init__(self, sizes: "dict[str, int]", rows: "list[dict]", min_bytes: int) -> None:
        self.sizes = sizes
        self.rows = {row["path"]: row for row in rows}
        self.min_bytes = min_bytes
        self.children: "dict[str, list[str]]" = {}
        for path in sizes:
            self.children.setdefault(os.path.dirname(path), []).append(path)
        self.entries: "list[dict]" = []
        self.unnamed: "list[dict]" = []
        self.folded: "dict[str, list[int]]" = {}

    def _row_for(self, path: str) -> "dict | None":
        best = None
        for row_path, row in self.rows.items():
            if (path == row_path or _under(path, row_path)) and (best is None or len(row_path) > len(best["path"])):
                best = row
        return best

    def _row_below(self, path: str) -> bool:
        return any(_under(row_path, path) for row_path in self.rows)

    def _fold(self, parent: str, size: int) -> None:
        bucket = self.folded.setdefault(parent, [0, 0])
        bucket[0] += 1
        bucket[1] += size

    def visit(self, path: str) -> None:
        size = self.sizes[path]
        row = self._row_for(path)
        if row is not None:
            self.entries.append(_entry(path, size, row))
            return
        children = self.children.get(path, [])
        descend = children and (self._row_below(path) or size >= self.min_bytes)
        if not descend:
            if size >= self.min_bytes:
                self.unnamed.append({"path": path, "bytes": size, "folded": False})
            else:
                self._fold(os.path.dirname(path), size)
            return
        for child in children:
            self.visit(child)
        own = size - sum(self.sizes[c] for c in children)
        if own > 0:
            self._fold(path, own)

    def finish(self) -> None:
        for parent, (count, size) in self.folded.items():
            if size:
                self.unnamed.append({"path": parent, "bytes": size, "folded": True, "count": count})
        self.entries.sort(key=lambda e: (-e["bytes"], e["path"]))
        self.unnamed.sort(key=lambda e: (-e["bytes"], e["path"]))


# --- temp roots -------------------------------------------------------------------------------

def session_temp_dir() -> str:
    """The session temp root by tempfile's environment order, without tempfile.gettempdir(), which
    probes by creating a file in the candidate directory (a write)."""
    for var in ("TMPDIR", "TEMP", "TMP"):
        if os.environ.get(var):
            return os.environ[var]
    return SANDBOX_TEMP_ROOT


def temp_section(roots: "list[str]", run: DuRunner, deadline_at: float, du_excludes: "list[str]") -> dict:
    """Totals of the session temp roots, split into the temp-residue reaper's kinds and the rest."""
    from reaper.builtin import temp_residue

    session = os.path.realpath(session_temp_dir())
    out_roots = []
    seen = set()
    for raw in roots:
        real = os.path.realpath(raw)
        if real in seen or not os.path.isdir(real):
            continue
        seen.add(real)
        sandbox_only = real == os.path.realpath(SANDBOX_TEMP_ROOT) and real != session
        kinds = (temp_residue.SANDBOX_KIND,) if sandbox_only else tuple(temp_residue.KINDS[:2])
        argv = DU_PREFIX + ["-a", "-0", "--max-depth=1"] + [f"--exclude={_glob_escape(p)}" for p in du_excludes
                                                           if _under(p, real)] + [real]
        stdout, _stderr, _timed_out = run(argv, deadline_at - time.monotonic())
        sizes = parse_du(stdout)
        total = sizes.get(real)
        if total is None:
            out_roots.append({"path": raw, "realpath": real, "bytes": 0, "residue_kinds_bytes": 0,
                              "outside_kinds_bytes": 0, "top_outside": [], "unreached": True})
            continue
        residue = 0
        outside = []
        for path, size in sizes.items():
            if path == real:
                continue
            name = os.path.basename(path)
            if any(temp_residue.PATTERNS[kind].fullmatch(name) for kind in kinds):
                residue += size
            else:
                outside.append({"path": path, "bytes": size})
        outside.sort(key=lambda e: (-e["bytes"], e["path"]))
        out_roots.append({
            "path": raw, "realpath": real, "bytes": total, "residue_kinds_bytes": residue,
            "outside_kinds_bytes": total - residue, "kinds": list(kinds),
            "top_outside": outside[:TEMP_TOP_N], "unreached": False,
        })
    return {"session_root": session, "roots": out_roots}


def deleted_open_bytes(proc: str = "/proc") -> int:
    """Allocated bytes of deleted-but-open regular files held by this user's processes; recorded, never subtracted."""
    seen = set()
    total = 0
    uid = os.getuid()
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return 0
    for pid in pids:
        fd_dir = os.path.join(proc, pid, "fd")
        try:
            if os.stat(os.path.join(proc, pid)).st_uid != uid:
                continue
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            link = os.path.join(fd_dir, fd)
            try:
                if not os.readlink(link).endswith(" (deleted)"):
                    continue
                st = os.stat(link)
            except OSError:
                continue
            key = (st.st_dev, st.st_ino)
            if key not in seen and st.st_nlink == 0:
                seen.add(key)
                total += st.st_blocks * 512
    return total


# --- the report -------------------------------------------------------------------------------

def check_priority_tools() -> None:
    import shutil

    if shutil.which("ionice") is None:
        raise ReportError("ionice is not installed; refusing to walk without idle I/O priority")


def apply_self_priority() -> None:
    os.setpriority(os.PRIO_PROCESS, 0, 19)
    done = subprocess.run(["ionice", "-c3", "-p", str(os.getpid())], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise ReportError(f"could not set ionice class 3 on this process: {done.stderr.strip()}")


def build_report(
    *, root: str, home: str, names_paths: "list[Path]", mountinfo: str, deadline_min: float,
    min_bytes: int, max_depth: int, temp_roots: "list[str] | None", argv: "list[str]",
    run: DuRunner = run_du, proc: str = "/proc",
) -> dict:
    if not 0 < deadline_min <= MAX_DEADLINE_MIN:
        raise ReportError(f"--deadline-min must be in (0, {MAX_DEADLINE_MIN}]")
    check_priority_tools()
    apply_self_priority()
    started = time.monotonic()
    deadline_at = started + deadline_min * 60
    root = os.path.abspath(root)
    home = os.path.abspath(home)
    rows, excludes, sources = load_names(names_paths, home)
    registry = load_registry(rows)
    with open(mountinfo, encoding="utf-8", errors="replace") as handle:
        mounts = parse_mountinfo(handle.read())
    plan = plan_walk(root, home, mounts, excludes)
    stat = os.statvfs(root)
    used = (stat.f_blocks - stat.f_bfree) * stat.f_frsize

    du_argv = DU_PREFIX + ["-0", f"--max-depth={max_depth}"] + [
        f"--exclude={_glob_escape(p)}" for p in plan["skip"]] + plan["roots"]
    sizes: "dict[str, int]" = {}
    du_errors: "list[str]" = []
    du_unparsed: "list[str]" = []
    timed_out = False
    if plan["roots"]:
        stdout, stderr, timed_out = run(du_argv, deadline_at - time.monotonic())
        sizes = parse_du(stdout)
        du_errors, du_unparsed = du_error_paths(stderr)
    reached = [r for r in plan["roots"] if r in sizes]
    unreached = [r for r in plan["roots"] if r not in sizes]
    kept = {p: s for p, s in sizes.items() if any(p == r or _under(p, r) for r in reached)}

    rows_in_root = [r for r in rows if r["path"] == root or _under(r["path"], root)]
    selector = Selector(kept, rows_in_root, min_bytes)
    for walked in reached:
        selector.visit(walked)
    selector.finish()
    inode_entries = chain_inode_entries(plan["chain"], root, home)
    entries = selector.entries + inode_entries
    entries.sort(key=lambda e: (-e["bytes"], e["path"]))
    for entry in entries:
        entry["reaper_status"] = reaper_status(entry, registry)
    c_entries = selector.unnamed
    c_bytes = sum(c["bytes"] for c in c_entries)
    walk_total = sum(e["bytes"] for e in entries) + c_bytes
    gap = used - walk_total
    bound_pct = (max(gap, 0) + c_bytes) / used * 100.0 if used else 0.0

    a_paths = sorted(set(plan["unreadable"] + du_errors + unreached))
    temp = temp_section(
        temp_roots if temp_roots is not None else [SANDBOX_TEMP_ROOT, session_temp_dir()],
        run, deadline_at, plan["skip"])
    return {
        "schema": SCHEMA,
        "date": datetime.date.today().isoformat(),
        "units": UNITS,
        "home": home,
        "root": root,
        "fs": {"blocks": stat.f_blocks, "bfree": stat.f_bfree, "frsize": stat.f_frsize, "used_bytes": used},
        "launch": {
            "argv": argv,
            "deadline_minutes": deadline_min,
            "priority": {"ionice_class": 3, "nice": 19},
            "du_argv_prefix": DU_PREFIX,
            "du_argv": du_argv,
            "max_depth": max_depth,
            "min_bytes": min_bytes,
            "tool_sha256": tool_sha256(),
            "names_sources": sources,
        },
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "deadline_exceeded": timed_out,
        "du_roots": plan["roots"],
        "excluded": plan["excluded"],
        "fuse_mounts": plan["fuse_mounts"],
        "entries": entries,
        "walk_total_bytes": walk_total,
        "unknown": {
            "a_paths": a_paths,
            "b_gap_bytes": gap,
            "b_deleted_open_bytes": deleted_open_bytes(proc),
            "c_entries": c_entries,
            "c_bytes": c_bytes,
            "bound_pct": bound_pct,
            "bound_limit_pct": BOUND_LIMIT_PCT,
            "du_unparsed_stderr": du_unparsed,
        },
        "temp": temp,
        "reaper_registry": registry,
    }


# --- output -----------------------------------------------------------------------------------

def _gib(n: int) -> str:
    return f"{n / GIB:.2f}"


def render_md(report: dict) -> str:
    unknown, fs, temp = report["unknown"], report["fs"], report["temp"]
    lines = [
        f"# Disk usage report {report['date']}",
        "",
        f"Root `{report['root']}`, one filesystem. Units: {report['units']}. Allocated sizes (`du` default).",
        f"Used (df): {fs['used_bytes']} bytes ({_gib(fs['used_bytes'])} GiB). "
        f"Walk total: {report['walk_total_bytes']} bytes ({_gib(report['walk_total_bytes'])} GiB). "
        f"Elapsed {report['elapsed_seconds']} s of a {report['launch']['deadline_minutes']} min deadline"
        f"{' (EXCEEDED)' if report.get('deadline_exceeded') else ''}.",
        "",
        "## Entries, largest first",
        "",
        "| bytes | GiB | path | label | kind | reaper | status |",
        "|---:|---:|---|---|---|---|---|",
    ]
    for e in report["entries"]:
        status = f"{e['reaper_status']}: {STATUS_WORDS[e['reaper_status']]}"
        if e["covers"] == "kinds-inside" and e["condition"]:
            status += f" ({e['condition']})"
        lines.append(f"| {e['bytes']} | {_gib(e['bytes'])} | `{e['path']}` | {e['label']} | {e['kind']} | "
                     f"{e['reaper'] or '-'} | {status} |")
    lines += [
        "",
        "## Unknown remainder",
        "",
        f"- (a) unread or unreached paths, no sizes: {len(unknown['a_paths'])}",
    ]
    lines += [f"  - `{p}`" for p in unknown["a_paths"][:50]]
    if len(unknown["a_paths"]) > 50:
        lines.append(f"  - ... {len(unknown['a_paths']) - 50} more in the JSON")
    lines += [
        f"- (b) byte gap, df Used minus walk total: {unknown['b_gap_bytes']} bytes ({_gib(unknown['b_gap_bytes'])} GiB); "
        f"deleted-but-open bytes held by this user's processes (recorded, not subtracted): {unknown['b_deleted_open_bytes']}",
        f"- (c) walked-but-unnamed entries: {len(unknown['c_entries'])} entries, {unknown['c_bytes']} bytes "
        f"({_gib(unknown['c_bytes'])} GiB)",
    ]
    for c in unknown["c_entries"][:50]:
        what = "smaller entries and files directly in" if c["folded"] else "unnamed"
        lines.append(f"  - {c['bytes']} bytes: {what} `{c['path']}`")
    lines += [
        "",
        f"Bound (b)+(c) = max(b, 0) + c = {max(unknown['b_gap_bytes'], 0) + unknown['c_bytes']} bytes = "
        f"**{unknown['bound_pct']:.2f}%** of Used (limit {unknown['bound_limit_pct']}%).",
        "",
        "## Temp roots",
        "",
        "| root | bytes | temp-residue kinds | outside the kinds |",
        "|---|---:|---:|---:|",
    ]
    for r in temp["roots"]:
        lines.append(f"| `{r['realpath']}` | {r['bytes']} | {r['residue_kinds_bytes']} | {r['outside_kinds_bytes']} |")
    lines += ["", "## Registry the statuses derive from", ""]
    lines += [f"- {name} ({layer})" for name, layer in report["reaper_registry"]["discovered"].items()]
    lines += ["", "## Re-run", "", "```", " ".join(shlex.quote(a) for a in report["launch"]["argv"]), "```", ""]
    return "\n".join(lines)


def write_outputs(report: dict, out_dir: Path) -> "tuple[Path, Path]":
    """Create disk-usage-<date>[-n].json and .md exclusively; never overwrite."""
    out_dir.mkdir(parents=True, exist_ok=True)
    body = json.dumps(report, indent=1, sort_keys=False) + "\n"
    for n in range(1, 10000):
        stem = f"disk-usage-{report['date']}" + ("" if n == 1 else f"-{n}")
        json_path, md_path = out_dir / f"{stem}.json", out_dir / f"{stem}.md"
        try:
            with open(json_path, "x", encoding="utf-8") as handle:
                handle.write(body)
        except FileExistsError:
            continue
        try:
            with open(md_path, "x", encoding="utf-8") as handle:
                handle.write(render_md(report))
        except FileExistsError:
            continue
        return json_path, md_path
    raise ReportError("no free output name")
